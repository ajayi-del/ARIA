"""Pins: live-USD peak lock (Governor 2026-11-15 giveback repair).

The birth-R exit stack (trail / MFE sharpen / runner bank) is quantity-
blind: mfe_peak_r is favorable-move ÷ birth stop distance, converted to
USD via birth/residual risk. tap_reload-grown runners (turbo LTC
10.9→152.17 qty) showed mfe_peak_r 0.688 — below the trail rungs and the
$30 sharpen trigger — while the REAL venue peak was ~$218. Only
breakeven armed; $72 of +$81 open was designed to be given back.

live_peak_lock_verdict ratchets the peak on the VENUE uPnL plane
(quantity-aware by construction) and locks rung-fractions of the real
peak off the CURRENT venue quantity. Tighten-only. Default OFF =
pre-module bit-for-bit (no state writes, no tighten).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from prop_harness.brains import eval_profile as ep
from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor


def _bracket(**over) -> dict:
    b = {
        "semantic_id": "s1",
        "asset": "BTC",
        "side": "buy",
        "entry_ref": "100",
        "stop_price": "92",       # birth stop — stop_dist 8
        "current_stop": "92",
        "risk_usd": 14.0,
        "opened_ts": 1_700_000_000.0,
    }
    b.update(over)
    return b


class TestRatchet:
    def test_raise_only(self):
        b = _bracket()
        assert ep.ratchet_mfe_peak_usd_live(b, 40.0) == 40.0
        assert ep.ratchet_mfe_peak_usd_live(b, 55.5) == 55.5
        assert ep.ratchet_mfe_peak_usd_live(b, 12.0) == 55.5  # never drops
        assert ep.ratchet_mfe_peak_usd_live(b, -7.0) == 55.5

    def test_unparseable_abstains(self):
        b = _bracket()
        assert ep.ratchet_mfe_peak_usd_live(b, None) is None
        assert ep.ratchet_mfe_peak_usd_live(b, "junk") is None
        assert ep.ratchet_mfe_peak_usd_live(b, float("nan")) is None
        assert "mfe_peak_usd_live" not in b

    def test_disabled_verdict_writes_no_state(self):
        b = _bracket()
        assert ep.live_peak_lock_verdict(
            "evaluation", b, 110.0, 1.5, 200.0, enabled=False) is None
        assert "mfe_peak_usd_live" not in b  # kill OFF = bit-for-bit


class TestVerdict:
    def test_standard_profile_none(self):
        b = _bracket()
        assert ep.live_peak_lock_verdict(
            "standard", b, 110.0, 1.5, 200.0, rungs_r=()) is None

    def test_below_first_rung_none(self):
        b = _bracket()
        assert ep.live_peak_lock_verdict(
            "evaluation", b, 110.0, 1.5, 29.99, rungs_r=()) is None

    def test_first_rung_locks_half(self):
        b = _bracket()
        # peak = 40 → floor max(20, 0.5×40) = 20; qty 1.5 → +13.33...
        t = ep.live_peak_lock_verdict(
            "evaluation", b, 130.0, 1.5, 40.0, rungs_r=())
        assert t == pytest.approx(100.0 + 20.0 / 1.5)

    def test_rung_ladder(self):
        b = _bracket()
        t = ep.live_peak_lock_verdict(
            "evaluation", b, 200.0, 1.5, 80.0, rungs_r=())
        assert t == pytest.approx(100.0 + 56.0 / 1.5)   # 0.70 × 80
        b2 = _bracket()
        t2 = ep.live_peak_lock_verdict(
            "evaluation", b2, 300.0, 1.5, 160.0, rungs_r=())
        assert t2 == pytest.approx(100.0 + 128.0 / 1.5)  # 0.80 × 160

    def test_min_floor_binds(self):
        b = _bracket()
        # peak 35 → frac floor 17.5 < min 20 → floor 20
        t = ep.live_peak_lock_verdict(
            "evaluation", b, 130.0, 2.0, 35.0, rungs_r=())
        assert t == pytest.approx(110.0)

    def test_skips_when_floor_through_mark(self):
        b = _bracket()
        # ratchet the peak high, then uPnL collapses under the floor
        ep.ratchet_mfe_peak_usd_live(b, 218.0)
        assert ep.live_peak_lock_verdict(
            "evaluation", b, 105.0, 152.17, 81.0, rungs_r=()) is None

    def test_tighten_only(self):
        # incumbent stop 190 already tighter than the 185.3 rung target
        b = _bracket(current_stop="190")
        assert ep.live_peak_lock_verdict(
            "evaluation", b, 200.0, 1.5, 160.0, rungs_r=()) is None
        # looser incumbent (140) IS tightened
        b2 = _bracket(current_stop="140")
        assert ep.live_peak_lock_verdict(
            "evaluation", b2, 200.0, 1.5, 160.0, rungs_r=()) is not None

    def test_sell_mirror(self):
        b = _bracket(side="sell", current_stop="108", stop_price="108")
        t = ep.live_peak_lock_verdict(
            "evaluation", b, 90.0, 1.5, 40.0, rungs_r=())
        assert t == pytest.approx(100.0 - 20.0 / 1.5)
        # tighten-only mirrored: an already-lower stop is never raised
        b2 = _bracket(side="sell", current_stop="80", stop_price="108")
        assert ep.live_peak_lock_verdict(
            "evaluation", b2, 90.0, 1.5, 40.0, rungs_r=()) is None

    def test_bad_geometry_none(self):
        assert ep.live_peak_lock_verdict(
            "evaluation", _bracket(), 110.0, 0, 100.0, rungs_r=()) is None
        assert ep.live_peak_lock_verdict(
            "evaluation", _bracket(entry_ref=None), 110.0, 1.5, 100.0,
            rungs_r=()) is None
        assert ep.live_peak_lock_verdict(
            "evaluation", _bracket(), 110.0, 1.5, None, rungs_r=()) is None

    def test_ltc_reload_grown_class(self):
        """The turbo wound: birth risk $14 / stop_dist 1.244, qty reloaded
        10.9 → 152.17. Legacy peak math: 1.15R × $14 ≈ $16 < $30 trigger;
        trail rung-1 breakeven+fees ≈ $45.20 → ~$9 locked off +$218 peak.
        Live lock at the peak tick: 0.80 × 218 = $174.4 → stop 46.39."""
        b = _bracket(entry_ref="45.244", stop_price="44.0",
                     current_stop="45.184", risk_usd=14.0)
        peak_mark = 45.244 + 218.0 / 152.17   # ≈ 46.6765
        t = ep.live_peak_lock_verdict(
            "evaluation", b, peak_mark, Decimal("152.17"), 218.0,
            rungs_r=())
        assert t == pytest.approx(45.244 + 174.4 / 152.17)  # ≈ 46.3901
        assert t > 45.184  # far tighter than the breakeven lock
        # giveback tick: floor now through the mark → skip; the 46.39
        # stop already placed is what protects the $174.
        assert ep.live_peak_lock_verdict(
            "evaluation", b, 45.776, Decimal("152.17"), 81.0,
            rungs_r=()) is None


class TestRRungsAwaken:
    """ATS-201: Mid tickets arm on R-rungs when peak$ < $30 USD floor."""

    def test_mid_ticket_arms_on_r_path(self):
        # risk $8, stop_dist 8 → mark +8 = 1.0R; peak$=8 < $30 USD rung
        b = _bracket(risk_usd=8.0, current_stop="92")
        detail = {}
        t = ep.live_peak_lock_verdict(
            "evaluation", b, 108.0, 1.0, 8.0,  # uPnL=$8 << $30
            rungs_r=((0.5, 0.50), (1.0, 0.75)),
            min_floor_usd=5.0,
            detail_out=detail)
        assert t is not None
        assert detail["path"] == "r"
        assert detail["lock_frac"] == 0.75
        # floor = 0.75 * 1.0R * $8 = $6 → stop 106
        assert t == pytest.approx(100.0 + 6.0 / 1.0)

    def test_below_half_r_none(self):
        b = _bracket(risk_usd=8.0)
        # mark 103 → 0.375R < 0.5
        assert ep.live_peak_lock_verdict(
            "evaluation", b, 103.0, 1.0, 3.0,
            rungs_r=((0.5, 0.50), (1.0, 0.75)),
            min_floor_usd=5.0) is None

    def test_rungs_r_empty_usd_only(self):
        b = _bracket(risk_usd=8.0)
        # Would arm on R-path, but rungs_r empty → USD-only → below $30
        assert ep.live_peak_lock_verdict(
            "evaluation", b, 108.0, 1.0, 8.0,
            rungs_r=(), min_floor_usd=5.0) is None

    def test_upnl_dark_mark_fallback(self):
        b = _bracket(risk_usd=10.0)
        detail = {}
        t = ep.live_peak_lock_verdict(
            "evaluation", b, 108.0, 1.0, None,  # uPnL dark
            rungs_r=((0.5, 0.50), (1.0, 0.75)),
            min_floor_usd=5.0, detail_out=detail)
        assert t is not None
        assert detail["path"] == "r"


class _LockClient:
    """Minimal venue: accepts the resize stop, reports no open orders."""

    account_id = "acct1"

    def __init__(self):
        self.placed = []

    def place_orders(self, body):
        self.placed.append(body)
        return [{"orderId": "sl%d" % len(self.placed)}]

    def get_open_orders(self, base=None):
        return []


def _exec(tmp_path: Path, client, **over) -> ProprExecutor:
    knobs = dict(
        enabled=True, state_root=tmp_path, profile="evaluation",
        asset_class="all", crypto_comove_enabled=False,
        family_cap_enabled=False, post_win_respend_enabled=False,
        cost_move_gate_enabled=False, max_positions=7,
        slot_reserve_enabled=False, eval_crypto_min_risk_enabled=False,
        crypto_min_coh_fire_enabled=False,
        eval_margin_cap_frac=0.99,
        eval_trend_budget_usd=5_000.0,
    )
    knobs.update(over)
    cfg = PropConfig(**knobs)
    (tmp_path / "journal").mkdir(exist_ok=True)
    dd = DrawdownBrain(tmp_path / "dd.json", floor_line=1000.0,
                       floor_usd=150.0)
    dd.update(5000.0)
    budget = BudgetBrain(tmp_path / "budget.json", hard_zero=30.0,
                         divisor=10.0, floor_budget=40.0)
    budget.roll(10_000.0)
    return ProprExecutor(cfg, client, budget, dd)


def _events(tmp_path: Path) -> list[dict]:
    out = []
    with open(tmp_path / "journal" / "events.jsonl") as fh:
        for line in fh:
            out.append(json.loads(line))
    return out


class TestExecutor:
    def test_default_off_bit_for_bit(self, tmp_path):
        client = _LockClient()
        ex = _exec(tmp_path, client)  # live_peak_lock_enabled default False
        b = _bracket()
        pos = {"quantity": "1.5", "positionId": "p1",
               "unrealizedPnl": "218.0"}
        assert ex.lock_live_peak(b, pos, 300.0) is False
        assert client.placed == []
        assert "mfe_peak_usd_live" not in b

    def test_locks_and_journals(self, tmp_path):
        client = _LockClient()
        ex = _exec(tmp_path, client, live_peak_lock_enabled=True)
        b = _bracket(entry_ref="45.244", stop_price="44.0",
                     current_stop="45.184", risk_usd=14.0)
        pos = {"quantity": "152.17", "positionId": "p1",
               "unrealizedPnl": "218.0"}
        mark = 45.244 + 218.0 / 152.17
        assert ex.lock_live_peak(b, pos, mark) is True
        assert len(client.placed) == 1
        order = client.placed[0]["orders"][0]
        assert order["type"] == "stop_market"
        assert order["reduceOnly"] is True
        assert order["side"] == "sell"
        assert order["quantity"] == "152.17"
        assert float(order["triggerPrice"]) == pytest.approx(
            45.244 + 174.4 / 152.17)
        assert b["current_stop"] == order["triggerPrice"]
        assert b["be_armed"] is True      # stop locks through entry
        assert b["mfe_floor_armed"] is True
        evs = _events(tmp_path)
        locked = [e for e in evs if e["type"] == "live_peak_locked"][0]
        assert locked["mfe_peak_usd_live"] == 218.0
        assert locked["live_upnl"] == "218.0"
        assert locked["previous_stop"] == "45.184"

    def test_tighten_only_across_ticks(self, tmp_path):
        client = _LockClient()
        ex = _exec(tmp_path, client, live_peak_lock_enabled=True)
        b = _bracket()
        pos = {"quantity": "1.5", "positionId": "p1",
               "unrealizedPnl": "40.0"}
        assert ex.lock_live_peak(b, pos, 130.0) is True
        first = b["current_stop"]
        # price dips: floor through mark → skip, no new order
        pos["unrealizedPnl"] = "10.0"
        assert ex.lock_live_peak(b, pos, 106.0) is False
        assert len(client.placed) == 1
        assert b["current_stop"] == first

    def test_dust_qty_silent(self, tmp_path):
        client = _LockClient()
        ex = _exec(tmp_path, client, live_peak_lock_enabled=True)
        b = _bracket()
        pos = {"quantity": "0.0000001", "positionId": "p1",
               "unrealizedPnl": "500.0"}
        assert ex.lock_live_peak(b, pos, 300.0) is False
        assert client.placed == []


class TestConfigParse:
    def test_defaults_are_legacy(self, monkeypatch, tmp_path):
        for k in ("PROP_LIVE_PEAK_LOCK", "PROP_LIVE_PEAK_LOCK_MIN_FLOOR_USD",
                  "PROP_LIVE_PEAK_LOCK_RUNGS_R"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        cfg = PropConfig.from_env()
        assert cfg.live_peak_lock_enabled is False
        assert cfg.live_peak_lock_min_floor_usd == 20.0
        assert cfg.live_peak_lock_rungs_r == "0.5:0.5,1.0:0.75"

    def test_env_binds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        monkeypatch.setenv("PROP_LIVE_PEAK_LOCK", "1")
        monkeypatch.setenv("PROP_LIVE_PEAK_LOCK_MIN_FLOOR_USD", "25")
        monkeypatch.setenv("PROP_LIVE_PEAK_LOCK_RUNGS_R", "0.5:0.6,1.0:0.8")
        cfg = PropConfig.from_env()
        assert cfg.live_peak_lock_enabled is True
        assert cfg.live_peak_lock_min_floor_usd == 25.0
        assert cfg.live_peak_lock_rungs_r == "0.5:0.6,1.0:0.8"
