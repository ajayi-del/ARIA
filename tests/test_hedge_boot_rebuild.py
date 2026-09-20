"""tests/test_hedge_boot_rebuild.py — pins for the boot orphan-rebuild brain.

Context (2026-09-19): the HedgeManager registry is memory-only; a hedge
short filled pre-restart (WLD 42.9 @ 0.4341 on the Bybit hedge account) is
orphaned at boot — the bot has no plan for it and cannot cover it when the
primary closes. match_hedge_orphans sorts live hedge legs at boot;
HedgeManager.adopt_plan_at_boot rebuilds a PROTECTED, coverable plan.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.hedge_manager import (  # noqa: E402
    BookCtx, HedgeManager, LongCtx, STATE_COVERED, STATE_CLOSING,
    STATE_PROTECTED, match_hedge_orphans)

SYM = "WLD-USD"
ORPHAN = {"symbol": SYM, "qty": 42.9, "entry_price": 0.4341, "leverage": 5}


def _long(symbol=SYM, opened_at_ms=555_000):
    return LongCtx(symbol=symbol, side="long", entry_price=0.40, qty=42.9,
                   leverage=5.0, mark=0.40, opened_at_ms=opened_at_ms,
                   age_s=10.0)


def _ctx(now, longs, hedge_positions=None):
    return BookCtx(now=now, longs=longs,
                   marks={l.symbol: l.mark for l in longs},
                   hedge_marks={l.symbol: l.mark for l in longs},
                   hedge_positions=hedge_positions, enabled=True)


def _kinds(actions):
    return [a.kind for a in actions]


# ── match_hedge_orphans ──────────────────────────────────────────────────────

class TestMatchHedgeOrphans:
    def test_primary_open_and_unknown_adopts(self):
        out = match_hedge_orphans([ORPHAN], {SYM, "ETH-USD"}, set())
        assert out == {"adopt": [ORPHAN], "unmatched": []}

    def test_no_primary_and_unknown_is_unmatched(self):
        out = match_hedge_orphans([ORPHAN], {"ETH-USD"}, set())
        assert out == {"adopt": [], "unmatched": [ORPHAN]}

    def test_known_symbol_omitted(self):
        out = match_hedge_orphans([ORPHAN], {SYM}, {SYM})
        assert out == {"adopt": [], "unmatched": []}
        # known trumps even a missing primary
        out = match_hedge_orphans([ORPHAN], set(), {SYM})
        assert out == {"adopt": [], "unmatched": []}

    def test_mixed_book(self):
        legs = [ORPHAN,
                {"symbol": "ETH-USD", "qty": 1.0, "entry_price": 4000.0,
                 "leverage": 5},
                {"symbol": "BTC-USD", "qty": 0.01, "entry_price": 100000.0,
                 "leverage": 5}]
        out = match_hedge_orphans(legs, {SYM}, {"BTC-USD"})
        assert [p["symbol"] for p in out["adopt"]] == [SYM]
        assert [p["symbol"] for p in out["unmatched"]] == ["ETH-USD"]

    def test_empty_inputs(self):
        assert match_hedge_orphans([], set(), set()) == {
            "adopt": [], "unmatched": []}


# ── adopt_plan_at_boot ───────────────────────────────────────────────────────

class TestAdoptPlanAtBoot:
    def test_registers_protected_plan(self):
        m = HedgeManager()
        plan = m.adopt_plan_at_boot(SYM, 42.9, 0.4341, 5)
        assert plan.plan_id.startswith(f"boot-{SYM}-")
        assert plan.state == STATE_PROTECTED
        assert plan.qty == pytest.approx(42.9)
        assert plan.entry_price == pytest.approx(0.4341)
        assert plan.leverage == 5
        assert plan.trail_dist_abs == pytest.approx(
            m.knobs.trail_pct * 0.4341)
        assert plan.catastrophic_stop == pytest.approx(
            0.4341 * (1.0 + m.knobs.catastrophic_stop_pct))
        assert plan in m.plans()

    def test_primary_closed_cover_flow_sees_adopted_plan(self):
        """Primary gone from the book → the existing unwind path covers the
        adopted leg at market."""
        m = HedgeManager()
        plan = m.adopt_plan_at_boot(SYM, 42.9, 0.4341, 5)
        acts = m.evaluate(_ctx(now=1_000.0, longs=[]))
        close = [a for a in acts if a.kind == "close_short_market"]
        assert len(close) == 1
        assert close[0].data["qty"] == pytest.approx(42.9)
        assert close[0].data["reason"] == "primary_closed"
        assert plan.state == STATE_CLOSING

    def test_identity_binds_on_first_sight(self):
        """With the primary present the adopted plan must NOT unwind; it
        binds the live long's opened_at identity."""
        m = HedgeManager()
        plan = m.adopt_plan_at_boot(SYM, 42.9, 0.4341, 5)
        acts = m.evaluate(_ctx(
            now=1_000.0, longs=[_long()],
            hedge_positions={SYM: {"qty": 42.9, "entry": 0.4341}}))
        assert "close_short_market" not in _kinds(acts)
        assert plan.primary_opened_at == 555_000
        assert plan.state == STATE_PROTECTED
        # ...and once the primary later closes, the cover flow fires.
        acts = m.evaluate(_ctx(now=2_000.0, longs=[]))
        assert "close_short_market" in _kinds(acts)

    def test_exchange_cover_detected(self):
        """Leg vanished exchange-side (trail/catastrophic stop) → covered."""
        m = HedgeManager()
        plan = m.adopt_plan_at_boot(SYM, 42.9, 0.4341, 5)
        acts = m.evaluate(_ctx(now=1_000.0, longs=[_long()],
                               hedge_positions={}))
        assert plan.state == STATE_COVERED
        names = [a.data.get("name") for a in acts if a.kind == "event"]
        assert "hedge_leg_covered" in names
