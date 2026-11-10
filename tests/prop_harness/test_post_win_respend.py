"""Pins for post-win re-spend latch (nine-trade BTC→SOL/NEAR class).

Counterfactual: after BTC short +$51.2, SOL $32 and NEAR $30 must block;
BTC right-tail itself must still qualify to arm; tiny wins must not arm;
kill switch / tradfi / xyz: must never bind.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from prop_harness.brains import post_win_respend as pwr
from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor
from prop_harness.families import family_of


class _FakeClient:
    def __init__(self):
        self.account_id = "acct1"
        self.placed: list = []

    def get_open_orders(self, base=None):
        return []

    def place_orders(self, body):
        self.placed.append(body)
        return [{"orderId": "urn:prp-order:new", **body["orders"][0]}]

    def cancel_order(self, order_id):
        return {}


def _exec(tmp_path: Path, **kw) -> ProprExecutor:
    kw.setdefault("profile", "evaluation")
    kw.setdefault("asset_class", "crypto")
    kw.setdefault("crypto_comove_enabled", False)  # isolate respend
    kw.setdefault("family_cap_enabled", False)
    kw.setdefault("cost_move_gate_enabled", False)
    kw.setdefault("eval_crypto_min_risk_enabled", True)
    kw.setdefault("max_positions", 7)
    kw.setdefault("slot_reserve_enabled", False)
    cfg = PropConfig(enabled=True, state_root=tmp_path, **kw)
    (tmp_path / "journal").mkdir(exist_ok=True)
    dd = DrawdownBrain(tmp_path / "dd.json", floor_line=4850.0,
                       floor_usd=150.0)
    dd.update(5000.0)
    budget = BudgetBrain(tmp_path / "budget.json", hard_zero=30.0,
                         divisor=10.0, floor_budget=40.0)
    budget.roll(150.0)
    return ProprExecutor(cfg, _FakeClient(), budget, dd)


def _btc_win_latch(now: float = 1_000_000.0) -> dict:
    return pwr.build_latch(
        asset="BTC", family="majors", side="sell",
        realized_pnl=51.2, risk_usd=50.0, coherence=9.5,
        now=now, ttl_s=5400.0, max_respend_usd=15.0,
        semantic_id="btc-win-1",
    )


class TestWinQualifies:
    def test_btc_absolute_win_arms(self):
        assert pwr.win_qualifies(51.2, 50.0, 9.5) is True

    def test_small_win_does_not_arm(self):
        assert pwr.win_qualifies(2.77, 7.0, 7.25) is False

    def test_r_arm_on_meaningful_ticket(self):
        # +$18 on $25 risk = 0.72R with coh>7
        assert pwr.win_qualifies(18.0, 25.0, 8.0) is True

    def test_r_arm_requires_coh_and_risk(self):
        assert pwr.win_qualifies(18.0, 25.0, 5.0) is False
        assert pwr.win_qualifies(18.0, 10.0, 8.0) is False

    def test_loss_never_arms(self):
        assert pwr.win_qualifies(-32.0, 32.0, 9.0) is False


class TestBetaProximity:
    def test_majors_to_large_alt(self):
        assert pwr.beta_proximate("majors", "large_alt") is True

    def test_majors_to_l1_alt_near(self):
        assert pwr.beta_proximate("majors", family_of("NEAR")) is True

    def test_meme_not_proximate_to_majors(self):
        assert pwr.beta_proximate("majors", "meme") is False

    def test_same_family(self):
        assert pwr.beta_proximate("large_alt", "large_alt") is True


class TestRespendVerdict:
    def test_sol_after_btc_win_blocked(self):
        latch = _btc_win_latch()
        reason, detail = pwr.respend_verdict(
            True, latch, "SOL", "large_alt", 32.19, latch["armed_at"] + 60)
        assert reason == pwr.RESPEND_BLOCK
        assert detail["winner_asset"] == "BTC"
        assert detail["remaining_respend_usd"] == pytest.approx(15.0)

    def test_near_after_btc_win_blocked(self):
        latch = _btc_win_latch()
        # NEAR ~80m later — still inside 90m TTL
        reason, _ = pwr.respend_verdict(
            True, latch, "NEAR", "l1_alt", 29.95,
            latch["armed_at"] + 4800)
        assert reason == pwr.RESPEND_BLOCK

    def test_ttl_expiry_allows(self):
        latch = _btc_win_latch()
        reason, _ = pwr.respend_verdict(
            True, latch, "SOL", "large_alt", 32.0,
            latch["expires_at"] + 1)
        assert reason is None

    def test_probe_under_cap_allows(self):
        latch = _btc_win_latch()
        reason, _ = pwr.respend_verdict(
            True, latch, "BCH", "large_alt", 7.0, latch["armed_at"] + 10)
        assert reason is None

    def test_cumulative_cap_binds(self):
        latch = _btc_win_latch()
        pwr.note_respend_commit(latch, 7.0)
        pwr.note_respend_commit(latch, 7.0)  # committed 14, remaining 1
        reason, detail = pwr.respend_verdict(
            True, latch, "SOL", "large_alt", 7.0, latch["armed_at"] + 10)
        assert reason == pwr.RESPEND_BLOCK
        assert detail["remaining_respend_usd"] == pytest.approx(1.0)

    def test_kill_switch_off(self):
        latch = _btc_win_latch()
        reason, _ = pwr.respend_verdict(
            False, latch, "SOL", "large_alt", 32.0, latch["armed_at"] + 10)
        assert reason is None

    def test_tradfi_sleeve(self):
        latch = _btc_win_latch()
        reason, _ = pwr.respend_verdict(
            True, latch, "SOL", "large_alt", 32.0, latch["armed_at"] + 10,
            asset_class="tradfi")
        assert reason is None

    def test_xyz_never_binds(self):
        latch = _btc_win_latch()
        reason, _ = pwr.respend_verdict(
            True, latch, "xyz:NVDA", "equity_semis", 32.0,
            latch["armed_at"] + 10)
        assert reason is None

    def test_cap_scales_with_win(self):
        latch = pwr.build_latch(
            asset="BTC", family="majors", side="sell",
            realized_pnl=51.2, risk_usd=50.0, coherence=9.5,
            now=1.0, max_respend_usd=15.0)
        # min(15, 0.30*51.2=15.36) = 15
        assert latch["max_respend_usd"] == pytest.approx(15.0)


class TestRespendExecutor:
    def test_arms_on_btc_win_and_blocks_sol(self, tmp_path):
        ex = _exec(tmp_path)
        assert ex.arm_post_win_respend({
            "asset": "BTC", "family": "majors", "side": "sell",
            "realized_pnl_final": 51.2, "risk_usd": 50.0,
            "coherence_at_entry": 9.5, "semantic_id": "btc-1",
        })
        # Wide stop so cost gates don't interfere (~1%)
        res = ex.place_bracket("SOL", "sell", 32.0, 86.378, 87.24,
                               coherence=5.0)
        assert not res.ok
        assert res.reason == pwr.RESPEND_BLOCK

    def test_nine_trade_near_blocked(self, tmp_path):
        ex = _exec(tmp_path)
        now = time.time()
        latch = _btc_win_latch(now=now)
        ex._save_respend_latch(latch)
        res = ex.place_bracket("NEAR", "sell", 30.0, 3.636, 3.673,
                               coherence=5.0)
        assert not res.ok
        assert res.reason == pwr.RESPEND_BLOCK

    def test_kill_switch_places(self, tmp_path):
        ex = _exec(tmp_path, post_win_respend_enabled=False)
        ex._save_respend_latch(_btc_win_latch())
        # BCH ~1.5% stop, risk 5 — clears cost at 10%
        res = ex.place_bracket("BCH", "sell", 5.0, 260.0, 264.0,
                               coherence=5.0)
        assert res.ok, res.reason

    def test_config_defaults(self):
        cfg = PropConfig(enabled=True)
        assert cfg.post_win_respend_enabled is True
        assert cfg.post_win_respend_ttl_s == 5400.0
        assert cfg.post_win_respend_max_usd == 15.0
