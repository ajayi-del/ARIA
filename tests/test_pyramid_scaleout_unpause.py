"""Pyramid scale-out un-pause + track qty-sync pins (2026-09-19).

Live incident (verified from production logs 2026-09-19): UNI, ETH and ARB
pyramid tracks SCALE_OUT'ed (thesis-damage 50% partial close) and then sat
in PAUSE_PHASES FOREVER — the branch set unwind_mode="SCALE_OUT" but never
advanced the phase, so pause_exits/owns_stop never released and the normal
exit stack (trail / software_tp / time_stop / coherence_decay /
conviction_review / profit_cap) stayed paused on the remaining position
all night. Companion desync (UNI): classify_size_sync re-anchored the
Position to the exchange size (6.0) but the PyramidTrack kept believing
its stale remaining-qty (5.5) — future leg/unwind math read a ghost size.

FIX A — _pyramid_scale_out_unpause_phase: after a successful scale-out the
phase advances to UNWINDING. UNWINDING (1) is not in PAUSE_PHASES so the
exit stack resumes, (2) fails _pyramid_phase_allows_adds so the unwinding
pyramid can never start adding again, (3) leaves the track registered so
the final-close pyramid_closed journaling still fires.

FIX B — _pyramid_track_qty_sync: a size sync that adopts a new exchange
size for a symbol with a pyramid track re-anchors the track's current_qty
(base_qty — the registration anchor for leg ratios — is never touched).
"""
from intelligence.pyramid import (
    PAUSE_PHASES, PHASE_BASE_FILLED, PHASE_BUILDING, PHASE_LEG_PENDING,
    PHASE_PYRAMIDED, PHASE_UNWINDING, PyramidTrack, owns_stop, pause_exits,
    unwind_verdict, leg_plans,
)
from types import SimpleNamespace

from main import (
    _pyramid_phase_allows_adds,
    _pyramid_scale_out_unpause_phase,
    _pyramid_track_qty_sync,
)


def _track(phase=PHASE_BUILDING, qty=5.5, unwind_mode="SCALE_OUT"):
    return PyramidTrack(
        symbol="UNI-USD", side="long", klass="swing", base_qty=10.0,
        base_entry=100.0, current_vwap=101.0, current_qty=qty,
        legs_done=1, atr_at_reg=1.0, phase=phase, registered_at=1000.0,
        unwind_mode=unwind_mode)


# ── FIX A: phase advance releases the exit-stack pause ───────────────────────

def test_scale_out_unpause_advances_to_unwinding():
    for phase in (PHASE_BASE_FILLED, PHASE_LEG_PENDING, PHASE_BUILDING):
        tr = _track(phase=phase)
        assert _pyramid_scale_out_unpause_phase(tr) == PHASE_UNWINDING


def test_unpaused_track_releases_pause_exits_and_owns_stop():
    # The incident itself: BUILDING track paused the whole exit stack.
    tr = _track(phase=PHASE_BUILDING)
    assert pause_exits(tr, enabled=True, shadow=False) is True
    assert owns_stop(tr, enabled=True, shadow=False) is True
    tr.phase = _pyramid_scale_out_unpause_phase(tr)
    assert pause_exits(tr, enabled=True, shadow=False) is False
    assert owns_stop(tr, enabled=True, shadow=False) is False


def test_unwinding_is_not_a_pause_phase():
    # Registry pin: the chosen phase must live outside PAUSE_PHASES.
    assert PHASE_UNWINDING not in PAUSE_PHASES


def test_unpause_is_noop_when_already_out_of_pause_set():
    # A track that already advanced (TRAIL handback / prior un-pause) is
    # never re-written — idempotent, no double telemetry.
    for phase in (PHASE_PYRAMIDED, PHASE_UNWINDING):
        tr = _track(phase=phase)
        assert _pyramid_scale_out_unpause_phase(tr) is None
        assert tr.phase == phase


def test_unpause_kill_switch_off_is_legacy():
    # False = pre-fix behavior bit-for-bit: the phase is never touched.
    tr = _track(phase=PHASE_BUILDING)
    assert _pyramid_scale_out_unpause_phase(tr, enabled=False) is None
    assert tr.phase == PHASE_BUILDING
    assert pause_exits(tr, enabled=True, shadow=False) is True


def test_unpause_none_track_is_noop():
    assert _pyramid_scale_out_unpause_phase(None) is None


# ── FIX A check 2: the advanced track can never re-enter add-leg logic ───────

def test_phase_allows_adds_only_in_build_phases():
    # Pins the exact set the main() pyramid loop skipped pre-extraction —
    # bit-for-bit: PYRAMIDED + UNWINDING continue past the guard.
    assert _pyramid_phase_allows_adds(PHASE_BASE_FILLED) is True
    assert _pyramid_phase_allows_adds(PHASE_LEG_PENDING) is True
    assert _pyramid_phase_allows_adds(PHASE_BUILDING) is True
    assert _pyramid_phase_allows_adds(PHASE_PYRAMIDED) is False
    assert _pyramid_phase_allows_adds(PHASE_UNWINDING) is False


def test_unpaused_track_fails_the_add_leg_guard():
    # The semantic that matters: after the un-pause the loop's add-verdict
    # branch is unreachable for this track (an unwinding pyramid never
    # starts adding again).
    tr = _track(phase=PHASE_LEG_PENDING)
    tr.phase = _pyramid_scale_out_unpause_phase(tr)
    assert _pyramid_phase_allows_adds(tr.phase) is False


# ── FIX A check 3: close journaling path still fires for an advanced track ──

def test_unpaused_track_stays_registered_and_kill_switches_still_fire():
    # The pyramid_closed path keys on position absence while the track sits
    # in _PYRAMID_STATE["tracks"] — the un-pause mutates ONLY the phase, so
    # the track object (and its registry membership) is untouched, and the
    # unwind brain keeps guarding the remainder (HARD_EXIT kill switches
    # still reachable at UNWINDING).
    tr = _track(phase=PHASE_BUILDING)
    new_phase = _pyramid_scale_out_unpause_phase(tr)
    tr.phase = new_phase
    assert tr.closed_at == 0.0            # close bookkeeping untouched
    assert tr.unwind_mode == "SCALE_OUT"  # unwind identity preserved
    cfg = SimpleNamespace(pyramid_funding_extreme_pct=0.10,
                          pyramid_rv_rank_kill=90.0,
                          pyramid_oi_delta_kill_pct=-2.0)
    plan = leg_plans(cfg)["swing"]
    uw = unwind_verdict(tr, plan, funding_rate=0.50, cfg=cfg)
    assert uw.mode == "HARD_EXIT"         # funding kill switch still binds


# ── FIX B: track qty sync on size-sync adoption ──────────────────────────────

def test_track_qty_sync_patches_current_qty_and_reports_delta():
    # The UNI desync itself: track believed 5.5, exchange book said 6.0.
    tr = _track(qty=5.5)
    out = _pyramid_track_qty_sync(tr, 6.0)
    assert out == (5.5, 6.0)
    assert tr.current_qty == 6.0


def test_track_qty_sync_never_touches_base_qty():
    # base_qty is the registration anchor that normalizes leg ratios —
    # re-anchoring it would silently re-size every future leg.
    tr = _track(qty=5.5)
    _pyramid_track_qty_sync(tr, 6.0)
    assert tr.base_qty == 10.0


def test_track_qty_sync_without_track_is_noop():
    # Size sync on a symbol with no pyramid track — pure no-op, no event.
    assert _pyramid_track_qty_sync(None, 6.0) is None


def test_track_qty_sync_kill_switch_off_is_legacy():
    tr = _track(qty=5.5)
    assert _pyramid_track_qty_sync(tr, 6.0, enabled=False) is None
    assert tr.current_qty == 5.5


def test_track_qty_sync_unchanged_is_silent():
    # Sync fired because the Position diverged but the track already agreed
    # with the book — no belief change, no telemetry noise.
    tr = _track(qty=6.0)
    assert _pyramid_track_qty_sync(tr, 6.0) is None
    assert tr.current_qty == 6.0


def test_track_qty_sync_degenerate_input_is_noop():
    tr = _track(qty=5.5)
    assert _pyramid_track_qty_sync(tr, 0.0) is None
    assert _pyramid_track_qty_sync(tr, -1.0) is None
    assert _pyramid_track_qty_sync(tr, "junk") is None
    assert tr.current_qty == 5.5
