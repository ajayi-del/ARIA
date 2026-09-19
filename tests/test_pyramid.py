"""tests/test_pyramid.py — P1 brain pins (Governor 2026-09-18 reason registry).

Guard-stack ordering is load-bearing and pinned as such: tp1_not_confirmed
delegates FIRST (main.py:624 parent gate), trigger early-exits, warmup
deferrals, kill switches before coherence, account guards last and INERT
when their plane does not exist (None). Floor math pins against the REAL
main.aster_swing_floor_price (single source of truth, injected).
"""
from types import SimpleNamespace

import pytest

from intelligence.pyramid import (
    COHERENCE_COLLAPSE, MAX_ADD_RETRACE_FRAC, PAUSE_PHASES, PHASE_BASE_FILLED,
    PHASE_BUILDING, PHASE_LEG_PENDING, PHASE_PYRAMIDED, PHASE_UNWINDING,
    WARMUP_MIN_FRAC, AddVerdict, LegPlan, PyramidTrack, add_verdict,
    breakeven_floor, klass_for, leg_plans, leg_qty, owns_stop, pause_exits,
    pre_add_floor, registrable, reentry_verdict, retrace_frac_of_peak,
    trigger_price, unwind_verdict,
)
from main import aster_swing_floor_price

FLOOR = aster_swing_floor_price


def _cfg(**over):
    base = dict(
        pyramid_max_add_attempts=2,
        pyramid_trigger_mode="step",
        pyramid_funding_extreme_pct=0.10,
        pyramid_rv_rank_kill=90.0,
        pyramid_oi_delta_kill_pct=-2.0,
        pyramid_add_coherence_min=5.0,
        pyramid_max_concurrent=2,
        pyramid_max_exposure_pct=0.30,
        pyramid_be_buffer_pct=0.004,
        pyramid_scalp_leg_weights="0.5,0.5",
        pyramid_swing_leg_weights="0.405,0.25,0.20,0.145",
        pyramid_scalp_trigger_atr=0.3,
        pyramid_swing_trigger_atr=1.0,
        pyramid_reentry_window_s=86400,
        pyramid_reentry_retrace_min=0.30,
        pyramid_reentry_retrace_max=0.65,
        pyramid_reentry_coherence_min=6.5,
    )
    base.update(over)
    return SimpleNamespace(**base)


CFG = _cfg()
SCALP = leg_plans(CFG)["scalp"]      # (0.5, 0.5), step 0.3
SWING = leg_plans(CFG)["swing"]      # (0.405, .25, .20, .145), step 1.0


def _track(klass="swing", side="long", legs_done=0, vwap=100.0, atr=1.0,
           peak=0.0, base_qty=10.0, base_entry=100.0, phase=PHASE_LEG_PENDING,
           **over):
    return PyramidTrack(
        symbol="UNI-USD", side=side, klass=klass, base_qty=base_qty,
        base_entry=base_entry, current_vwap=vwap,
        current_qty=base_qty, legs_done=legs_done, atr_at_reg=atr,
        phase=phase, registered_at=1000.0,
        peak_move_atr=peak, **over)


def _green(klass="swing", side="long", **over):
    """All pillars healthy; mark above the leg-1 trigger (vwap 100, atr 1)."""
    kw = dict(mark=101.5, funding_rate=0.0001, rv_rank_now=50.0,
              oi_delta_pct=0.5, tp1_cleared=True, floor_fn=FLOOR, cfg=CFG)
    if klass == "swing":
        kw.update(trend_verdict="aligned", l4_imbalance=0.3,
                  l4_spread_bps=5.0)
    else:
        kw.update(coherence=7.0, trend_verdict="aligned")
    if side == "short":
        kw["mark"] = 98.5
        if klass == "swing":
            kw["l4_imbalance"] = -0.3
    kw.update(over)
    return kw


# ── leg_plans / klass_for / registrable ──────────────────────────────────────

def test_leg_plans_defaults():
    plans = leg_plans(CFG)
    assert plans["scalp"].weights == (0.5, 0.5)
    assert plans["scalp"].triggers_atr == (0.3,)
    assert plans["swing"].weights == (0.405, 0.25, 0.20, 0.145)
    assert plans["swing"].triggers_atr == (1.0, 1.0, 1.0)
    assert plans["swing"].be_buffer_pct == pytest.approx(0.004)


@pytest.mark.parametrize("raw", ["garbage", "1.0", "0.5,0", "0.5,,", ""])
def test_leg_plans_malformed_drops_class(raw):
    cfg = _cfg(pyramid_scalp_leg_weights=raw,
               pyramid_swing_leg_weights=raw)
    assert leg_plans(cfg) == {}


def test_klass_for():
    assert klass_for("aster_swing") == "swing"
    assert klass_for("breakout") == "swing"
    assert klass_for("s1_oi_pullback") == "swing"
    assert klass_for("cascade_momentum") == "scalp"
    assert klass_for("") == "scalp"


def test_registrable_mutex():
    assert registrable("standard") is True
    assert registrable("standard", is_campaign=True) is False
    assert registrable("whale_probe") is False
    assert registrable("standard", strategy_tag="explosive") is False


# ── leg_qty (FIX-A clamp adaptation: normalize to ACTUAL base) ───────────────

def test_leg_qty_normalizes_to_actual_base():
    assert leg_qty(SCALP, 1, 10.0) == pytest.approx(10.0)
    assert leg_qty(SWING, 1, 10.0) == pytest.approx(10.0 * 0.25 / 0.405)
    assert leg_qty(SWING, 2, 10.0) == pytest.approx(10.0 * 0.20 / 0.405)
    assert leg_qty(SWING, 3, 10.0) == pytest.approx(10.0 * 0.145 / 0.405)


def test_leg_qty_degenerate():
    assert leg_qty(SWING, 0, 10.0) == 0.0
    assert leg_qty(SWING, 4, 10.0) == 0.0
    assert leg_qty(SWING, 1, 0.0) == 0.0


# ── trigger ladder ───────────────────────────────────────────────────────────

def test_trigger_step_mode_anchors_on_current_vwap():
    t = _track(vwap=100.0)
    assert trigger_price(t, SWING, 1, "step") == pytest.approx(101.0)
    t2 = _track(vwap=105.0, legs_done=1)
    assert trigger_price(t2, SWING, 2, "step") == pytest.approx(106.0)


def test_trigger_cumulative_mode_anchors_on_base():
    t = _track(vwap=105.0, legs_done=1)
    assert trigger_price(t, SWING, 2, "cumulative") == pytest.approx(102.0)


def test_trigger_short_mirror():
    t = _track(side="short", vwap=100.0)
    assert trigger_price(t, SWING, 1, "step") == pytest.approx(99.0)


def test_trigger_degenerate_abstains():
    assert trigger_price(_track(atr=0.0), SWING, 1) is None
    assert trigger_price(_track(), SWING, 0) is None
    assert trigger_price(_track(), SWING, 4) is None
    assert trigger_price(_track(vwap=0.0), SWING, 1) is None


# ── floors ───────────────────────────────────────────────────────────────────

def test_pre_add_floor_buffer_side():
    assert pre_add_floor("long", 100.0, 0.004) == pytest.approx(99.6)
    assert pre_add_floor("short", 100.0, 0.004) == pytest.approx(100.4)
    assert pre_add_floor("long", 0.0, 0.004) == 0.0


def test_breakeven_floor_delegates_to_real_main_helper():
    got = breakeven_floor("long", 10.0, 100.0, 6.0, 102.0, 0.004, FLOOR)
    want = aster_swing_floor_price("long", 10.0, 100.0, 6.0, 102.0, 0.004)
    assert got == pytest.approx(want)
    comb = (10.0 * 100.0 + 6.0 * 102.0) / 16.0
    assert got == pytest.approx(comb * 0.996)
    # Tighten-only invariant: floor sits BELOW combined breakeven (long).
    assert got < comb


# ── retrace_frac_of_peak ─────────────────────────────────────────────────────

def test_retrace_frac_math_and_abstain():
    t = _track(peak=2.0)                    # peak px = 102
    assert retrace_frac_of_peak(t, 101.0) == pytest.approx(0.5)
    assert retrace_frac_of_peak(t, 100.5) == pytest.approx(0.75)
    ts = _track(side="short", peak=2.0)     # peak px = 98
    assert retrace_frac_of_peak(ts, 99.0) == pytest.approx(0.5)
    assert retrace_frac_of_peak(_track(peak=0.0), 101.0) is None
    assert retrace_frac_of_peak(t, None) is None
    assert retrace_frac_of_peak(t, -1) is None
    assert retrace_frac_of_peak(_track(atr=0.0, peak=2.0), 101.0) is None


# ── add_verdict: ordering pins (Governor registry) ───────────────────────────

def test_already_complete_first():
    t = _track(legs_done=3)
    v = add_verdict(t, SWING, **_green())
    assert (v.allowed, v.reason) == (False, "already_complete")


def test_max_attempts():
    t = _track(add_attempts=2)
    v = add_verdict(t, SWING, **_green())
    assert v.reason == "max_attempts"


def test_tp1_parent_gate_delegates_first():
    # Falsy injection blocks; None falls back to track.tp1_cleared.
    assert add_verdict(_track(), SWING,
                       **_green(tp1_cleared=False)).reason == "tp1_not_confirmed"
    kw = _green(tp1_cleared=None)
    assert add_verdict(_track(), SWING, **kw).reason == "tp1_not_confirmed"
    assert add_verdict(_track(tp1_cleared=True), SWING, **kw).allowed is True
    # TP1 outranks every later guard — kill-switch inputs still denied.
    kw2 = _green(tp1_cleared=False, funding_rate=None)
    assert add_verdict(_track(), SWING, **kw2).reason == "tp1_not_confirmed"


def test_mark_and_atr_unknown():
    assert add_verdict(_track(), SWING, **_green(mark=None)).reason == "mark_unknown"
    assert add_verdict(_track(), SWING, **_green(mark=0)).reason == "mark_unknown"
    assert add_verdict(_track(atr=0.0), SWING, **_green()).reason == "atr_unknown"


def test_trigger_not_hit_long_and_short():
    v = add_verdict(_track(), SWING, **_green(mark=100.5))
    assert (v.allowed, v.reason) == (False, "trigger_not_hit")
    assert v.trigger_px == pytest.approx(101.0)
    vs = add_verdict(_track(side="short"), SWING, **_green(side="short", mark=99.5))
    assert vs.reason == "trigger_not_hit"
    assert vs.trigger_px == pytest.approx(99.0)


def test_warmup_boundary():
    # Trigger NOT hit + warming → in_warmup_window
    v = add_verdict(_track(), SWING, **_green(mark=100.5, warmup_frac=0.79))
    assert v.reason == "in_warmup_window"
    # Trigger HIT + warming → deferred, queued not fired
    v2 = add_verdict(_track(), SWING, **_green(warmup_frac=0.79))
    assert v2.reason == "add_deferred_warmup"
    # 0.80 boundary: ladder fires the moment the ruler is honest
    v3 = add_verdict(_track(), SWING,
                     **_green(warmup_frac=WARMUP_MIN_FRAC))
    assert v3.allowed is True
    v4 = add_verdict(_track(), SWING,
                     **_green(mark=100.5, warmup_frac=WARMUP_MIN_FRAC))
    assert v4.reason == "trigger_not_hit"


def test_kill_switches_before_coherence():
    assert add_verdict(_track(), SWING,
                       **_green(funding_rate=None)).reason == "funding_unknown"
    assert add_verdict(_track(), SWING,
                       **_green(funding_rate=0.0011)).reason == "funding_extreme"
    assert add_verdict(_track(), SWING,
                       **_green(funding_rate=-0.0011)).reason == "funding_extreme"
    assert add_verdict(_track(), SWING,
                       **_green(rv_rank_now=None)).reason == "rv_rank_unknown"
    assert add_verdict(_track(), SWING,
                       **_green(rv_rank_now=90.0)).reason == "rv_rank_extreme"
    assert add_verdict(_track(), SWING,
                       **_green(oi_delta_pct=None)).reason == "oi_delta_unknown"
    assert add_verdict(_track(), SWING,
                       **_green(oi_delta_pct=-2.01)).reason == "oi_delta_flush"
    assert add_verdict(_track(), SWING,
                       **_green(oi_delta_pct=-2.0)).allowed is True
    # Kill switch outranks coherence compute (swing needs no coherence).
    assert add_verdict(_track(), SWING,
                       **_green(funding_rate=None,
                                coherence=None)).reason == "funding_unknown"


def test_recovery_mode():
    assert add_verdict(_track(), SWING,
                       **_green(recovery_active=True)).reason == "recovery_mode"


def test_swing_native_evidence_gates():
    assert add_verdict(_track(), SWING,
                       **_green(trend_verdict="neutral")).reason == "trend_verdict_neutral"
    assert add_verdict(_track(), SWING,
                       **_green(l4_imbalance=None)).reason == "no_l4"
    assert add_verdict(_track(), SWING,
                       **_green(l4_spread_bps=None)).reason == "no_l4"
    assert add_verdict(_track(), SWING,
                       **_green(l4_spread_bps=26.0)).reason == "spread_too_wide"
    assert add_verdict(_track(), SWING,
                       **_green(l4_imbalance=-0.11)).reason == "l4_against"
    assert add_verdict(_track(side="short"), SWING,
                       **_green(side="short", l4_imbalance=0.11)).reason == "l4_against"
    # Scalp class never requires L4.
    assert add_verdict(_track(klass="scalp"), SCALP, **_green("scalp")).allowed


def test_standard_class_coherence_and_regime():
    t = _track(klass="scalp")
    assert add_verdict(t, SCALP,
                       **_green("scalp", coherence=None)).reason == "coherence_unavailable"
    assert add_verdict(t, SCALP,
                       **_green("scalp", coherence=4.99)).reason == "coherence_below_threshold"
    assert add_verdict(t, SCALP,
                       **_green("scalp", coherence=5.0)).allowed is True
    assert add_verdict(t, SCALP,
                       **_green("scalp", trend_verdict="counter")).reason == "regime_changed"
    assert add_verdict(t, SCALP,
                       **_green("scalp", trend_verdict="unknown")).reason == "regime_unknown"
    # regime_ok=False binds both classes
    assert add_verdict(_track(), SWING,
                       **_green(regime_ok=False)).reason == "regime_changed"


def test_retrace_too_deep_at_half_of_peak():
    # peak 102 (peak_move_atr=2, atr=1); vwap 99.5 → trigger 100.5; mark at
    # trigger → frac 0.75 > 0.50 ⇒ dead. Exactly 0.50 survives.
    t = _track(vwap=99.5, peak=2.0)
    assert add_verdict(t, SWING, **_green(mark=100.5)).reason == "retrace_too_deep"
    t2 = _track(vwap=100.0, peak=2.0)
    assert add_verdict(t2, SWING, **_green(mark=101.0)).allowed is True


def test_etf_tide_opposed():
    assert add_verdict(_track(), SWING,
                       **_green(etf_tide="opposed")).reason == "etf_tide_opposed"


def test_account_guards():
    assert add_verdict(_track(), SWING,
                       **_green(concurrent_pyramids=2)).reason == "concurrency_cap"
    assert add_verdict(_track(), SWING,
                       **_green(pyramid_margin_frac=0.30)).reason == "exposure_cap"
    assert add_verdict(_track(), SWING,
                       **_green(cluster_frac=0.15)).reason == "cluster_cap_hit"
    assert add_verdict(_track(), SWING,
                       **_green(account_risk_frac=0.02)).reason == "account_risk_budget_full"
    assert add_verdict(_track(), SWING,
                       **_green(projected_gross_frac=0.50)).reason == "leverage_cap_hit"
    assert add_verdict(_track(), SWING,
                       **_green(mark_scale_ok=False)).reason == "mark_scale_quarantined"


def test_account_guards_inert_when_plane_absent():
    v = add_verdict(_track(), SWING,
                    **_green(cluster_frac=None, account_risk_frac=None,
                             projected_gross_frac=None))
    assert v.allowed is True


def test_add_approved_leg_naming_and_payload():
    v = add_verdict(_track(legs_done=0), SWING, **_green())
    assert v.allowed and v.reason == "add_approved_leg2"
    assert v.leg_idx == 1
    assert v.qty == pytest.approx(10.0 * 0.25 / 0.405)
    assert v.floor_price == pytest.approx(100.0 * 0.996)
    assert v.trigger_px == pytest.approx(101.0)
    v3 = add_verdict(_track(legs_done=1, vwap=101.0), SWING,
                     **_green(mark=102.5))
    assert v3.reason == "add_approved_leg3"
    v4 = add_verdict(_track(legs_done=2, vwap=102.0), SWING,
                     **_green(mark=103.5))
    assert v4.reason == "add_approved_leg4"


def test_qty_degenerate():
    assert add_verdict(_track(base_qty=0.0), SWING,
                       **_green()).reason == "qty_degenerate"


# ── pause / owns_stop truth tables ───────────────────────────────────────────

@pytest.mark.parametrize("phase,paused", [
    (PHASE_BASE_FILLED, True), (PHASE_LEG_PENDING, True),
    (PHASE_BUILDING, True), (PHASE_PYRAMIDED, False),
    (PHASE_UNWINDING, False),
])
def test_pause_exits_phases(phase, paused):
    t = _track(phase=phase)
    assert pause_exits(t, enabled=True, shadow=False) is paused
    assert owns_stop(t, enabled=True, shadow=False) is paused
    assert (phase in PAUSE_PHASES) is paused


def test_pause_exits_switches():
    t = _track(phase=PHASE_BUILDING)
    assert pause_exits(t, enabled=True, shadow=True) is False   # shadow NEVER
    assert pause_exits(t, enabled=False, shadow=False) is False
    assert pause_exits(None, enabled=True, shadow=False) is False


# ── unwind_verdict ───────────────────────────────────────────────────────────

def test_unwind_kill_switches_hard_exit():
    t = _track()
    assert unwind_verdict(t, SWING, funding_rate=0.002,
                          cfg=CFG).mode == "HARD_EXIT"
    assert unwind_verdict(t, SWING, rv_rank_now=90.0,
                          cfg=CFG).reason == "rv_rank_extreme"
    assert unwind_verdict(t, SWING, oi_delta_pct=-2.5,
                          cfg=CFG).reason == "oi_delta_flush"


def test_unwind_thesis_damage_scale_out():
    t = _track()
    v = unwind_verdict(t, SWING, coherence=COHERENCE_COLLAPSE - 0.1, cfg=CFG)
    assert (v.mode, v.reason) == ("SCALE_OUT", "coherence_collapse")
    v2 = unwind_verdict(t, SWING, trend_verdict="counter", cfg=CFG)
    assert (v2.mode, v2.reason) == ("SCALE_OUT", "trend_flip")


def test_unwind_complete_trails_and_pillar_null_holds():
    done = _track(legs_done=3)
    assert unwind_verdict(done, SWING, cfg=CFG).mode == "TRAIL"
    t = _track()
    v = unwind_verdict(t, SWING, cfg=CFG)  # all pillars None
    assert (v.mode, v.reason) == ("", "hold")


# ── reentry_verdict boundaries ───────────────────────────────────────────────

def _closed():
    return _track(peak=2.0, closed_at=1000.0, phase=PHASE_UNWINDING)


def test_reentry_not_closed_and_window():
    assert reentry_verdict(_track(), mark=101.4, now=1100.0,
                           cfg=CFG).reason == "not_closed"
    v = reentry_verdict(_closed(), mark=101.4, now=1000.0 + 86401, cfg=CFG)
    assert v.reason == "window_expired"
    v2 = reentry_verdict(_closed(), mark=101.3, now=1000.0 + 86400,
                         coherence=7.0, cfg=CFG)
    assert v2.eligible is True


def test_reentry_retrace_boundaries():
    # peak px 102; frac = (102 - mark) / 2 — clearly outside/inside the band
    assert reentry_verdict(_closed(), mark=101.42, now=1100.0,
                           coherence=7.0, cfg=CFG).reason == "retrace_shallow"   # 0.29
    assert reentry_verdict(_closed(), mark=101.3, now=1100.0,
                           coherence=7.0, cfg=CFG).eligible is True              # 0.35
    assert reentry_verdict(_closed(), mark=100.8, now=1100.0,
                           coherence=7.0, cfg=CFG).eligible is True              # 0.60
    assert reentry_verdict(_closed(), mark=100.68, now=1100.0,
                           coherence=7.0, cfg=CFG).reason == "retrace_deep"      # 0.66


def test_reentry_retrace_edges_inclusive():
    # Binary-exact edges: knobs at 0.25/0.75, marks landing frac exactly on
    # them — the edge itself is INSIDE the candidate band.
    cfg = _cfg(pyramid_reentry_retrace_min=0.25, pyramid_reentry_retrace_max=0.75)
    assert reentry_verdict(_closed(), mark=101.5, now=1100.0,
                           coherence=7.0, cfg=cfg).eligible is True             # 0.25
    assert reentry_verdict(_closed(), mark=100.5, now=1100.0,
                           coherence=7.0, cfg=cfg).eligible is True             # 0.75


def test_reentry_coherence_boundaries():
    assert reentry_verdict(_closed(), mark=101.3, now=1100.0,
                           coherence=None, cfg=CFG).reason == "coherence_unavailable"
    assert reentry_verdict(_closed(), mark=101.3, now=1100.0,
                           coherence=6.49,
                           cfg=CFG).reason == "coherence_below_threshold"
    assert reentry_verdict(_closed(), mark=101.3, now=1100.0,
                           coherence=6.5, cfg=CFG).eligible is True


def test_reentry_no_move_abstains():
    assert reentry_verdict(_track(peak=0.0, closed_at=1000.0), mark=100.0,
                           now=1100.0, cfg=CFG).reason == "no_move"


# ── Boot rebuild (restart orphan seam, 2026-09-19) ──────────────────────────
# _PYRAMID_STATE is memory-only; startup-sync-adopted positions ran the exit
# stack with zero pyramid coverage. The rebuild registers the track in the
# TERMINAL state (staircase complete at birth): adds never fire, kill-switch
# HARD_EXIT still covers, no SCALE_OUT, exit stack unpaused.

from intelligence.pyramid import boot_rebuild_track
from main import _pyramid_boot_rebuild_eligible, _pyramid_phase_allows_adds


def _rebuilt(klass="scalp", side="long", **over):
    kw = dict(base_qty=10.0, base_entry=100.0, atr=1.0,
              plan=(SCALP if klass == "scalp" else SWING), now=1000.0)
    kw.update(over)
    return boot_rebuild_track("UNI-USD", side, klass, **kw)


def test_boot_rebuild_track_terminal_state():
    tr = _rebuilt()
    assert tr is not None
    assert tr.phase == PHASE_PYRAMIDED            # complete-state value
    assert tr.legs_done == len(SCALP.weights) - 1  # staircase full at birth
    assert tr.rebuilt_boot is True
    assert tr.current_vwap == tr.base_entry
    assert tr.current_qty == tr.base_qty
    assert tr.closed_at == 0.0  # close-path bookkeeping fires normally


def test_boot_rebuild_track_degenerate_abstains():
    assert _rebuilt(base_qty=0.0) is None
    assert _rebuilt(base_entry=0.0) is None
    assert _rebuilt(atr=0.0) is None
    assert _rebuilt(atr=None) is None
    assert _rebuilt(plan=None) is None
    assert _rebuilt(base_qty="junk") is None


def test_rebuilt_track_adds_never_fire():
    tr = _rebuilt()
    # 1. The loop's phase guard skips the add stack entirely — zero
    #    pyramid_add_blocked noise (the track never reaches the guard stack).
    assert _pyramid_phase_allows_adds(tr.phase) is False
    # 2. Belt-and-suspenders: even if driven directly with all-green pillars,
    #    the brain refuses — a rebuilt track can never pyramid an
    #    already-pyramided position.
    v = add_verdict(tr, SCALP, **_green(klass="scalp"))
    assert v.allowed is False
    assert v.reason == "already_complete"


def test_rebuilt_track_kill_switch_hard_exit_covers():
    tr = _rebuilt()
    for kw, reason in (
            (dict(funding_rate=0.50), "funding_extreme"),
            (dict(rv_rank_now=95.0), "rv_rank_extreme"),
            (dict(oi_delta_pct=-5.0), "oi_delta_flush")):
        uw = unwind_verdict(tr, SCALP, cfg=CFG, **kw)
        assert uw.mode == "HARD_EXIT"
        assert uw.reason == reason


def test_rebuilt_track_never_scales_out():
    tr = _rebuilt()
    # Thesis damage on a rebuilt track hands the runner back to the trail
    # stack (TRAIL), never a 50% scale-out — leg geometry is unknowable.
    uw = unwind_verdict(tr, SCALP, coherence=1.0, cfg=CFG)
    assert uw.mode == "TRAIL" and uw.reason == "complete"
    uw = unwind_verdict(tr, SCALP, trend_verdict="counter", cfg=CFG)
    assert uw.mode == "TRAIL" and uw.reason == "complete"


def test_non_rebuilt_complete_track_still_scales_out_legacy():
    # Contrast pin: the suppression keys on rebuilt_boot ONLY — a fresh-path
    # track that reached PYRAMIDED keeps legacy thesis-damage SCALE_OUT.
    tr = _track(legs_done=len(SWING.weights) - 1, phase=PHASE_PYRAMIDED)
    assert tr.rebuilt_boot is False
    uw = unwind_verdict(tr, SWING, coherence=1.0, cfg=CFG)
    assert uw.mode == "SCALE_OUT" and uw.reason == "coherence_collapse"
    uw = unwind_verdict(tr, SWING, trend_verdict="counter", cfg=CFG)
    assert uw.mode == "SCALE_OUT" and uw.reason == "trend_flip"


def test_rebuilt_track_not_paused_not_stop_owned():
    # The exit stack treats a rebuilt track as non-building: trail /
    # roe_ratchet / software_tp / time_stop / coherence_decay /
    # conviction_review / profit_cap / treasury all key on these predicates.
    tr = _rebuilt()
    assert tr.phase not in PAUSE_PHASES
    assert pause_exits(tr, enabled=True, shadow=False) is False
    assert owns_stop(tr, enabled=True, shadow=False) is False
    # Contrast: a building track pauses (legacy semantics untouched).
    building = _track(phase=PHASE_BUILDING)
    assert pause_exits(building, enabled=True, shadow=False) is True


def test_boot_rebuild_eligible_gate():
    tracks = {}
    assert _pyramid_boot_rebuild_eligible(
        "UNI-USD", size=10.0, entry_price=5.0, venue_name="aster",
        tracks=tracks, enabled=True) is True
    # Knob False = legacy bit-for-bit: no registration.
    assert _pyramid_boot_rebuild_eligible(
        "UNI-USD", size=10.0, entry_price=5.0, venue_name="aster",
        tracks=tracks, enabled=False) is False
    # Idempotent: a symbol adopted twice in one boot = one track.
    tracks["UNI-USD"] = object()
    assert _pyramid_boot_rebuild_eligible(
        "UNI-USD", size=10.0, entry_price=5.0, venue_name="aster",
        tracks=tracks, enabled=True) is False


def test_boot_rebuild_dust_never_gets_track():
    # Aster close min $1 / SoDEX $10 (mirrors _has_actionable_position).
    assert _pyramid_boot_rebuild_eligible(
        "SUI-USD", size=0.1, entry_price=3.0, venue_name="aster",
        tracks={}, enabled=True) is False            # $0.30 < $1
    assert _pyramid_boot_rebuild_eligible(
        "BTC-USD", size=0.00005, entry_price=100000.0, venue_name="sodex",
        tracks={}, enabled=True) is False            # $5 < $10
    assert _pyramid_boot_rebuild_eligible(
        "BTC-USD", size=0.00011, entry_price=100000.0, venue_name="sodex",
        tracks={}, enabled=True) is True             # $11 >= $10
    # Degenerate inputs fail closed.
    assert _pyramid_boot_rebuild_eligible(
        "", size=1.0, entry_price=100.0, venue_name="aster",
        tracks={}, enabled=True) is False
    assert _pyramid_boot_rebuild_eligible(
        "UNI-USD", size="junk", entry_price=100.0, venue_name="aster",
        tracks={}, enabled=True) is False
