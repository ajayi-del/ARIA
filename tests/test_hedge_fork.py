"""Market-fork budget doctrine pins (Governor 2026-09-20).

"The same movement we suffer profits on the other end — we are forking the
market." Three modes on one spine: GREEN (profit-lock / tp1-approach), RED
(pain-harvest), FORK-RIDE (flush rider). Every hedge is funded ONLY by a
budget the primary's own stop geometry defines. These pins lock the budget
math, the ATR-scaled stop geometry, the harvest/re-arm loop, and the
kill-switch contract (budget_sizing_enabled=False = legacy bit-for-bit).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Settings  # noqa: E402
from intelligence.hedge_manager import (  # noqa: E402
    BookCtx, HedgeKnobs, HedgeManager, LongCtx)

SYM = "BTC-USD"
BASE = 1000.0


def _long(symbol=SYM, entry=BASE, qty=1.0, lev=5.0, mark=BASE,
          opened_at_ms=1_000_000, age_s=100.0, stop=990.0, tp1=0.0):
    return LongCtx(symbol=symbol, side="long", entry_price=entry, qty=qty,
                   leverage=lev, mark=mark, opened_at_ms=opened_at_ms,
                   age_s=age_s, stop_price=stop, tp1_price=tp1)


def _ctx(now=10_000.0, longs=None, hedge_mark=BASE, enabled=True, **kw):
    longs = _longs if (_longs := longs) is not None else [_long()]
    marks = {l.symbol: l.mark for l in longs}
    hedge_marks = ({l.symbol: hedge_mark for l in longs}
                   if hedge_mark else {})
    d = dict(now=now, longs=longs, marks=marks, hedge_marks=hedge_marks,
             enabled=enabled, hedge_free_equity=10_000.0)
    d.update(kw)
    return BookCtx(**d)


def _mgr(**mkw):
    mkw.setdefault("budget_sizing_enabled", True)
    mkw.setdefault("short_floor_usd", 10_000.0)   # S_max non-binding fixture
    return HedgeManager(HedgeKnobs(**mkw))


def _kinds(actions):
    return [a.kind for a in actions]


def _events(actions):
    return [a.data.get("name") for a in actions if a.kind == "event"]


def _one(actions, kind):
    hits = [a for a in actions if a.kind == kind]
    assert len(hits) == 1, f"expected one {kind}, got {_kinds(actions)}"
    return hits[0]


def _ev(actions, name):
    hits = [a for a in actions
            if a.kind == "event" and a.data.get("name") == name]
    assert len(hits) == 1, f"expected one {name} event"
    return hits[0].data["fields"]


def _fill(m, ctx_now=10_001.0, fill=BASE, qty=0.35, hedge_mark=BASE,
          longs=None):
    """Drive the armed plan to FILLED through exchange truth."""
    plan = m.plans()[0]
    m.on_order_placed(plan.plan_id, "ord-1")
    return m.evaluate(_ctx(now=ctx_now, longs=longs, hedge_mark=hedge_mark,
                           hedge_positions={SYM: {"qty": qty, "entry": fill}}))


# ── GREEN budget math ────────────────────────────────────────────────────────

class TestGreenBudget:
    def test_budget_math_and_notional(self):
        # stop 990 < entry 1000 → locked floor 0 (the ratchet has not moved
        # the stop above entry yet); open = (1010−1000)×1 = 10 → budget
        # 0.7×10 = 7; notional = 7 / 0.02 = 350 → qty 0.35 at mark 1000.
        m = _mgr()
        acts = m.evaluate(_ctx(longs=[_long(mark=1010.0)]))
        place = _one(acts, "place_limit_short")
        assert place.data["qty"] == pytest.approx(0.35)
        assert place.data["leverage"] == 15   # fat fixture free equity
        ev = _ev(acts, "hedge_budget_armed")
        assert ev["mode"] == "green"
        assert ev["trigger"] == "rung_1.0"
        assert ev["budget_total"] == pytest.approx(7.0)
        assert ev["stop_frac"] == pytest.approx(0.02)

    def test_locked_floor_after_ratchet(self):
        # Post-pyramid geometry: stop ratcheted to 1005 (above entry) →
        # locked floor (1005−1000)×1 = 5 + 0.7×(1010−1005) = 8.5. The
        # pyramid's breakeven floor MANUFACTURES hedge budget.
        m = _mgr()
        acts = m.evaluate(_ctx(longs=[_long(mark=1010.0, stop=1005.0)]))
        ev = _ev(acts, "hedge_budget_armed")
        assert ev["budget_total"] == pytest.approx(8.5)

    def test_no_stop_standdown(self):
        m = _mgr()
        acts = m.evaluate(_ctx(longs=[_long(mark=1010.0, stop=0.0)]))
        assert "place_limit_short" not in _kinds(acts)
        assert "hedge_budget_standdown" in _events(acts)

    def test_thesis_broken_never_arms(self):
        # Orchestration law: thesis_intact False blocks NEW arms absolutely —
        # the fork only RIDES a hedge already on.
        m = _mgr()
        acts = m.evaluate(_ctx(longs=[_long(mark=1010.0)],
                               thesis_intact={SYM: False}))
        assert acts == []

    def test_budget_exhausted_standdown(self):
        # Micro budget: floor 0 + 0.7×0.02 = 0.014 → notional 0.7 < $6 min.
        # lev=300 lifts ROE (0.6%) clearly past the first rung (0.30).
        m = _mgr()
        acts = m.evaluate(_ctx(longs=[_long(mark=1000.02, stop=999.98,
                                            lev=300.0)]))
        assert "place_limit_short" not in _kinds(acts)
        assert "hedge_budget_standdown" in _events(acts)


# ── TP1-approach trigger (pre-pyramid top-catcher) ──────────────────────────

class TestTp1Approach:
    """lev 0.5 keeps ROE (0.23%) below the first rung (0.30) so ONLY the
    tp1_approach trigger can arm: the fork shorts the retrace the pyramid
    add is about to suffer through."""

    def test_arms_at_90pct_of_tp1_distance(self):
        m = _mgr()
        acts = m.evaluate(_ctx(longs=[_long(mark=1004.6, tp1=1005.0,
                                            lev=0.5)]))
        ev = _ev(acts, "hedge_budget_armed")
        assert ev["trigger"].startswith("tp1_approach_")
        # budget = floor 0 + 0.7 × 4.6 = 3.22
        assert ev["budget_total"] == pytest.approx(3.22)

    def test_below_threshold_no_arm(self):
        m = _mgr()
        acts = m.evaluate(_ctx(longs=[_long(mark=1004.4, tp1=1005.0,
                                            lev=0.5)]))
        assert acts == []

    def test_unknown_tp1_abstains(self):
        m = _mgr()
        acts = m.evaluate(_ctx(longs=[_long(mark=1004.6, tp1=0.0, lev=0.5)]))
        assert acts == []

    def test_frac_zero_disables(self):
        m = _mgr(tp1_approach_frac=0.0)
        acts = m.evaluate(_ctx(longs=[_long(mark=1004.6, tp1=1005.0,
                                            lev=0.5)]))
        assert acts == []


# ── ATR-scaled stop (vol-honest fork geometry) ──────────────────────────────

class TestAtrScaledStop:
    def test_atr_widens_stop_and_shrinks_notional(self):
        # ATR 30 = 3% of mark → stop_frac 0.03 (floor 0.02 loses); the same
        # 7 budget buys notional 233.33, not 350 — constant risk, vol-honest
        # geometry (P1b doctrine, hedge leg).
        m = _mgr()
        acts = m.evaluate(_ctx(longs=[_long(mark=1010.0)],
                               atr_abs={SYM: 30.0}))
        plan = m.plans()[0]
        assert plan.stop_frac == pytest.approx(0.03)
        place = _one(acts, "place_limit_short")
        assert place.data["qty"] == pytest.approx(7.0 / 0.03 / 1000.0)

    def test_atr_below_floor_binds_floor(self):
        # Quiet market: ATR 10 = 1% < 2% floor → pre-ATR geometry bit-for-bit.
        m = _mgr()
        m.evaluate(_ctx(longs=[_long(mark=1010.0)], atr_abs={SYM: 10.0}))
        assert m.plans()[0].stop_frac == pytest.approx(0.02)

    def test_violent_market_derives_lower_leverage(self):
        # ATR 60 = 6% stop → isolated clearance 1.25×0.06+0.01 = 0.085 →
        # int(1/0.085) = 11x. The 15x cap is a QUIET-market privilege; the
        # derivation deleverages the fork when the tape gets violent.
        m = _mgr()
        acts = m.evaluate(_ctx(longs=[_long(mark=1010.0)],
                               atr_abs={SYM: 60.0}, hedge_free_equity=0.0))
        place = _one(acts, "place_limit_short")
        assert place.data["leverage"] == 11

    def test_fill_geometry_scales_with_stop(self):
        # Fill at 1000 with stop_frac 0.03: catastrophic 1030, TP at 0.75R —
        # 0.015 × (0.03/0.02) = 0.0225 → 977.5. R-multiple is vol-invariant.
        m = _mgr()
        m.evaluate(_ctx(longs=[_long(mark=1010.0)], atr_abs={SYM: 30.0}))
        qty = 7.0 / 0.03 / 1000.0
        acts = _fill(m, qty=qty)
        plan = m.plans()[0]
        assert plan.catastrophic_stop == pytest.approx(1030.0)
        assert plan.tp_price == pytest.approx(977.5)
        assert plan.trail_dist_abs == 0.0
        prot = _one(acts, "set_protection")
        assert prot.data["tp_price"] == pytest.approx(977.5)

    def test_fill_geometry_floor_case(self):
        # No ATR: legacy doctrine numbers — stop ×1.02, TP ×0.985, no trail.
        m = _mgr()
        m.evaluate(_ctx(longs=[_long(mark=1010.0)]))
        _fill(m)
        plan = m.plans()[0]
        assert plan.catastrophic_stop == pytest.approx(1020.0)
        assert plan.tp_price == pytest.approx(985.0)
        assert plan.trail_dist_abs == 0.0


# ── RED pain-harvest ─────────────────────────────────────────────────────────

class TestPainHarvest:
    def test_arms_at_halfway_to_stop(self):
        # pain_frac = (1000−990)/(1000−980) = 0.5 → budget 0.7×20 = 14 →
        # notional 700 → qty 0.7. "Every time price approaches our stop we
        # hedge — the same movement we suffer profits on the other end."
        m = _mgr()
        acts = m.evaluate(_ctx(longs=[_long(mark=990.0, stop=980.0)]))
        ev = _ev(acts, "hedge_budget_armed")
        assert ev["mode"] == "pain"
        assert ev["trigger"] == "pain_0.50"
        assert ev["budget_total"] == pytest.approx(14.0)
        assert _one(acts, "place_limit_short").data["qty"] == pytest.approx(0.7)

    def test_not_triggered_above_threshold(self):
        m = _mgr()
        acts = m.evaluate(_ctx(longs=[_long(mark=995.0, stop=980.0)]))
        assert acts == []

    def test_max_ratio_caps_notional(self):
        m = _mgr(pain_max_ratio=0.5)
        acts = m.evaluate(_ctx(longs=[_long(mark=990.0, stop=980.0)]))
        # min(700, 0.5 × 1.0 × 990) = 495 → qty 0.5
        assert _one(acts, "place_limit_short").data["qty"] == pytest.approx(0.5)

    def test_pain_mode_disabled(self):
        m = _mgr(pain_mode_enabled=False)
        acts = m.evaluate(_ctx(longs=[_long(mark=990.0, stop=980.0)]))
        assert acts == []

    def test_thesis_broken_never_arms_pain(self):
        m = _mgr()
        acts = m.evaluate(_ctx(longs=[_long(mark=990.0, stop=980.0)],
                               thesis_intact={SYM: False}))
        assert acts == []


# ── Harvest / re-arm loop ────────────────────────────────────────────────────

class TestHarvestLoop:
    def _covered_once(self, m, now, cover_mark):
        """Exchange-side cover: the hedge leg vanishes from the snapshot."""
        return m.evaluate(_ctx(now=now, longs=[_long(mark=1010.0)],
                               hedge_mark=cover_mark, hedge_positions={}))

    def test_tp_cover_harvest_and_cooloff(self):
        m = _mgr()
        m.evaluate(_ctx(longs=[_long(mark=1010.0)]))
        plan = m.plans()[0]
        _fill(m)
        m.on_tp_placed(plan.plan_id, "tp-1")
        acts = self._covered_once(m, 10_002.0, 985.0)   # TP-side cover
        assert "hedge_harvest_covered" in _events(acts)
        assert _one(acts, "cancel_order").data["order_id"] == "tp-1"
        assert m._harvest_count[f"{SYM}-1000000"] == 1
        assert m._budget_spent[f"{SYM}-1000000"] == pytest.approx(0.0)
        # Re-arm cooloff binds…
        assert m.evaluate(_ctx(now=10_500.0, longs=[_long(mark=1010.0)],
                               hedge_positions={})) == []
        # …and lifts after rearm_cooloff_s.
        acts = m.evaluate(_ctx(now=10_903.0, longs=[_long(mark=1010.0)],
                               hedge_positions={}))
        assert "hedge_budget_armed" in _events(acts)

    def test_stop_cover_spends_the_budget(self):
        # THE doctrine pin: one full stop-out spends the entire remaining
        # budget (notional = remaining/stop_frac ⇒ spend = remaining). This
        # is why the ATR-scaled stop exists — a wicked stop is a dead fork.
        m = _mgr()
        m.evaluate(_ctx(longs=[_long(mark=1010.0)]))
        _fill(m)
        acts = self._covered_once(m, 10_002.0, 1020.0)  # stop-side cover
        assert "hedge_harvest_covered" in _events(acts)
        # spend = 0.02 × 1000 × 0.35 = 7.0 = the whole budget
        assert m._budget_spent[f"{SYM}-1000000"] == pytest.approx(7.0)
        acts = m.evaluate(_ctx(now=10_903.0, longs=[_long(mark=1010.0)],
                               hedge_positions={}))
        assert "place_limit_short" not in _kinds(acts)
        assert "hedge_budget_standdown" in _events(acts)

    def test_harvest_cap_standdown(self):
        m = _mgr()
        now = 10_000.0
        for _ in range(4):
            m.evaluate(_ctx(now=now, longs=[_long(mark=1010.0)],
                            hedge_positions={}))
            _fill(m, ctx_now=now + 1)
            self._covered_once(m, now + 2, 985.0)
            now += 1000.0   # past each 900s re-arm cooloff
        assert m._harvest_count[f"{SYM}-1000000"] == 4
        acts = m.evaluate(_ctx(now=now, longs=[_long(mark=1010.0)],
                               hedge_positions={}))
        assert "place_limit_short" not in _kinds(acts)
        assert "hedge_budget_standdown" in _events(acts)


# ── FORK-RIDE (thesis break → ride the flush) ───────────────────────────────

class TestForkRide:
    def _riding_manager(self):
        m = _mgr()
        m.evaluate(_ctx(longs=[_long(mark=1010.0)]))
        plan = m.plans()[0]
        _fill(m)
        m.on_tp_placed(plan.plan_id, "tp-1")
        return m, plan

    def test_ride_arms_on_thesis_break(self):
        m, plan = self._riding_manager()
        acts = m.evaluate(_ctx(now=10_010.0, longs=[_long(mark=1010.0)],
                               thesis_intact={SYM: False},
                               hedge_positions={SYM: {"qty": 0.35,
                                                      "entry": BASE}}))
        assert plan.riding is True
        # Breakeven stop — "the fork cannot lose."
        assert plan.catastrophic_stop == pytest.approx(plan.entry_price)
        assert _one(acts, "cancel_order").data["order_id"] == "tp-1"
        assert "hedge_fork_ride_armed" in _events(acts)
        prot = _one(acts, "set_protection")
        assert prot.data["tp_price"] == 0.0

    def test_no_ride_while_thesis_intact(self):
        m, plan = self._riding_manager()
        acts = m.evaluate(_ctx(now=10_010.0, longs=[_long(mark=1010.0)],
                               hedge_positions={SYM: {"qty": 0.35,
                                                      "entry": BASE}}))
        assert plan.riding is False
        assert "hedge_fork_ride_armed" not in _events(acts)

    def test_giveback_cover(self):
        m, plan = self._riding_manager()
        pos = {SYM: {"qty": 0.35, "entry": BASE}}
        m.evaluate(_ctx(now=10_010.0, longs=[_long(mark=1010.0)],
                        thesis_intact={SYM: False}, hedge_positions=pos))
        # Flush: mark 960 → peak short profit 4%.
        m.evaluate(_ctx(now=10_020.0, longs=[_long(mark=1004.0)],
                        hedge_mark=960.0, thesis_intact={SYM: False},
                        hedge_positions=pos))
        # Retrace to 972: short profit 2.8%; giveback 1.2% ≥ 0.30 × 4% → cover.
        acts = m.evaluate(_ctx(now=10_030.0, longs=[_long(mark=1004.0)],
                               hedge_mark=972.0, thesis_intact={SYM: False},
                               hedge_positions=pos))
        close = _one(acts, "close_short_market")
        assert close.data["reason"] == "fork_ride_giveback"
        assert "hedge_fork_ride_cover" in _events(acts)

    def test_hold_expiry_force_cover(self):
        # FM-4: a 48h-old budget hedge is force-covered — funding bleed is
        # bounded by clock, not hope.
        m, plan = self._riding_manager()
        pos = {SYM: {"qty": 0.35, "entry": BASE}}
        acts = m.evaluate(_ctx(now=10_000.0 + 172_801.0,
                               longs=[_long(mark=1010.0)],
                               hedge_positions=pos))
        close = _one(acts, "close_short_market")
        assert close.data["reason"] == "max_hold_expired"
        assert "hedge_hold_expired" in _events(acts)


# ── Kill-switch contract ─────────────────────────────────────────────────────

class TestLegacyOff:
    def test_budget_off_is_legacy_bit_for_bit(self):
        # budget_sizing_enabled=False: rung-ratio sizing, no budget events,
        # no pain eval, tp1_price/stop_price on the long are inert.
        m = HedgeManager(HedgeKnobs(short_floor_usd=10_000.0))
        acts = m.evaluate(_ctx(longs=[_long(mark=1010.0, tp1=1005.0)]))
        assert "hedge_plan_armed" in _events(acts)
        assert "hedge_budget_armed" not in _events(acts)
        place = _one(acts, "place_limit_short")
        assert place.data["qty"] == pytest.approx(0.15 * 1.0)  # rung ratio

    def test_budget_off_no_pain_eval(self):
        m = HedgeManager(HedgeKnobs(short_floor_usd=10_000.0))
        acts = m.evaluate(_ctx(longs=[_long(mark=990.0, stop=980.0)]))
        assert acts == []


# ── Config pins ──────────────────────────────────────────────────────────────

class TestForkConfig:
    def test_live_arming(self):
        s = Settings()
        assert s.hedge_budget_sizing_enabled is True
        assert s.hedge_budget_stop_pct == 0.02
        assert s.hedge_budget_tp_pct == 0.015
        assert s.hedge_budget_haircut == 0.7
        assert s.hedge_pain_mode_enabled is True
        assert s.hedge_pain_trigger_frac == 0.5
        assert s.hedge_rearm_cooloff_s == 900.0
        assert s.hedge_max_harvests_per_primary == 4
        assert s.hedge_fork_ride_enabled is True
        assert s.hedge_fork_ride_giveback_frac == 0.30
        assert s.hedge_max_hold_s == 172800.0

    def test_vol_and_tp1_knobs(self):
        s = Settings()
        assert s.hedge_budget_stop_atr_mult == 1.0
        assert s.hedge_tp1_approach_frac == 0.9
