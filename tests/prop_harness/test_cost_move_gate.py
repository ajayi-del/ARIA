"""Pins for cost-to-expected-move gate + nine-trade counterfactuals.

Θ=0.25: fee_est/risk = 2×taker×entry/stop_dist.
  - BTC ~1% stop → ratio ~0.09 PASS (must not block +$53 shorts)
  - 0.35% dust → ratio ~0.257 BLOCK
  - SOL/NEAR ~0.5% → ratio ~0.18 PASS here (re-spend latch's job)
Interacts with EVAL_CRYPTO_COST_HARD_CAP: hard-cap raises allowance to
0.30; this gate is a hard ceiling that can still refuse at 0.25.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prop_harness.brains import cost_move_gate as cmg
from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor


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


TAKER = 0.00045


def _ratio_for_stop_pct(stop_pct: float, fee: float = TAKER) -> float:
    # fee/risk = 2*fee*entry / (entry*stop_pct) = 2*fee/stop_pct
    return 2.0 * fee / stop_pct


def _exec(tmp_path: Path, **kw) -> ProprExecutor:
    kw.setdefault("profile", "evaluation")
    kw.setdefault("asset_class", "crypto")
    kw.setdefault("crypto_comove_enabled", False)
    kw.setdefault("family_cap_enabled", False)
    kw.setdefault("post_win_respend_enabled", False)
    kw.setdefault("eval_crypto_min_risk_enabled", True)
    kw.setdefault("eval_crypto_cost_hard_cap", 0.30)
    kw.setdefault("cost_risk_max_frac", 0.10)
    kw.setdefault("cost_move_gate_enabled", True)
    kw.setdefault("cost_move_gate_live", True)  # pin live actuation path
    kw.setdefault("cost_move_theta", 0.25)
    kw.setdefault("taker_fee_rate", TAKER)
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


class TestPureCostMove:
    def test_theta_choice_dust_vs_btc(self):
        assert _ratio_for_stop_pct(0.0035) >= 0.25   # dust dies
        assert _ratio_for_stop_pct(0.01) < 0.25       # BTC 1% passes
        assert _ratio_for_stop_pct(0.005) < 0.25      # SOL/NEAR ~0.5% pass

    def test_btc_wide_stop_allows(self):
        # risk $50, ~1% stop on BTC ~100k → notional 5000
        fee = cmg.fee_est_usd(5_000.0, TAKER)
        move = cmg.expected_move_usd(50.0)
        reason, detail = cmg.cost_move_verdict(
            True, True, notional=5_000.0, taker_rate=TAKER,
            risk_usd=50.0, theta=0.25)
        assert reason is None
        assert detail["ratio"] == pytest.approx(fee / move)

    def test_tight_dust_blocks_live(self):
        # 0.35% stop, risk $7, entry 100 → notional = 7/0.35 = 2000
        entry, stop_pct, risk = 100.0, 0.0035, 7.0
        notional = (risk / (entry * stop_pct)) * entry
        reason, detail = cmg.cost_move_verdict(
            True, True, notional=notional, taker_rate=TAKER,
            risk_usd=risk, theta=0.25)
        assert reason == cmg.COST_MOVE_BLOCK
        assert detail["would_block"] is True
        assert detail["ratio"] >= 0.25

    def test_shadow_journals_but_allows(self):
        entry, stop_pct, risk = 100.0, 0.0035, 7.0
        notional = risk / stop_pct
        reason, detail = cmg.cost_move_verdict(
            True, False, notional=notional, taker_rate=TAKER,
            risk_usd=risk, theta=0.25)
        assert reason is None
        assert detail.get("shadow") is True
        assert detail.get("would_block") is True

    def test_kill_switch_off(self):
        reason, _ = cmg.cost_move_verdict(
            False, True, notional=2000.0, taker_rate=TAKER,
            risk_usd=7.0, theta=0.25)
        assert reason is None

    def test_missing_inputs_abstain(self):
        reason, detail = cmg.cost_move_verdict(
            True, True, notional=None, taker_rate=TAKER,
            risk_usd=7.0, theta=0.25)
        assert reason is None
        assert detail.get("abstain") is True

    def test_tradfi_and_xyz_exempt(self):
        reason, _ = cmg.cost_move_verdict(
            True, True, notional=2000.0, taker_rate=TAKER,
            risk_usd=7.0, theta=0.25, asset_class="tradfi")
        assert reason is None
        reason, _ = cmg.cost_move_verdict(
            True, True, notional=2000.0, taker_rate=TAKER,
            risk_usd=7.0, theta=0.25, asset="xyz:NVDA")
        assert reason is None

    def test_sol_near_pass_at_theta(self):
        # Specimen SOL: stop_dist 0.438 on 86.378 ≈ 0.507%
        stop_pct = 0.438 / 86.378
        notional = 32.19 / stop_pct
        reason, detail = cmg.cost_move_verdict(
            True, True, notional=notional, taker_rate=TAKER,
            risk_usd=32.19, theta=0.25)
        assert reason is None
        assert detail["ratio"] < 0.25


class TestCostMoveExecutor:
    def test_blocks_tight_crypto_live(self, tmp_path):
        ex = _exec(tmp_path)
        # long: stop below entry; 0.35% stop; risk 7
        res = ex.place_bracket("BTC", "buy", 7.0, 100.0, 99.65,
                               coherence=5.0)
        assert not res.ok
        assert res.reason == cmg.COST_MOVE_BLOCK

    def test_allows_btc_wide_stop(self, tmp_path):
        ex = _exec(tmp_path)
        # ~1% stop, risk 7 (probe) — clears Θ=0.25 and hard-cap path
        res = ex.place_bracket("BTC", "sell", 7.0, 100_000.0, 101_000.0,
                               coherence=5.0)
        assert res.ok, res.reason

    def test_hard_cap_would_allow_but_theta_refuses(self, tmp_path):
        # Geometry with ratio ~0.257: hard_cap 0.30 would clear the
        # Decimal cost_frac gate; Θ=0.25 still refuses.
        ex = _exec(tmp_path)
        assert _ratio_for_stop_pct(0.0035) < 0.30
        assert _ratio_for_stop_pct(0.0035) >= 0.25
        res = ex.place_bracket("ETH", "buy", 7.0, 2000.0, 1993.0,
                               coherence=5.0)  # 0.35%
        assert not res.ok
        assert res.reason == cmg.COST_MOVE_BLOCK

    def test_shadow_allows_tight(self, tmp_path):
        ex = _exec(tmp_path, cost_move_gate_live=False)
        # Without live ceiling, hard_cap 0.30 lets 0.35% through
        res = ex.place_bracket("ETH", "buy", 7.0, 2000.0, 1993.0,
                               coherence=5.0)
        assert res.ok, res.reason

    def test_config_defaults_shadow(self):
        cfg = PropConfig(enabled=True)
        assert cfg.cost_move_gate_enabled is True
        assert cfg.cost_move_gate_live is False  # shadow until census
        assert cfg.cost_move_theta == 0.25
