"""Pins for intelligence/tp1_signal.py — TP1-as-signal brain (2026-10-24
Governor axiom stack: a confirmed TP1 is a signal, not an end — funding gate,
retrace targets, pyramid ladder, scalp trigger, landmark record, lifecycle
state machine; pure, fail-closed, zero-I/O)."""
import math

from intelligence.tp1_signal import (
    FUNDING_CLOSE_THRESH, FUNDING_PYRAMID_THRESH, LOWER_LIMIT_DIPS,
    LOWER_LIMIT_SIZES, MAX_TOTAL_MULT, PYRAMID_LADDER, TP1_STATES,
    Tp1Track, funding_verdict, landmark_record, lower_limit_ladder,
    next_state, pyramid_leg_notional, retrace_targets, scalp_bracket,
    scalp_trigger_verdict,
)

_EPS = 1e-7


# ── Axiom 1: funding gate ────────────────────────────────────────────────────

def test_long_pyramid_below_threshold():
    assert funding_verdict("long", -2e-5) == "pyramid"
    assert funding_verdict("long", -FUNDING_PYRAMID_THRESH - _EPS) == "pyramid"


def test_long_pyramid_boundary_inclusive_trail():
    # -1e-5 exactly is inside the trail_exit band (threshold strict).
    assert funding_verdict("long", -FUNDING_PYRAMID_THRESH) == "trail_exit"
    assert funding_verdict("long", -FUNDING_PYRAMID_THRESH + _EPS) == "trail_exit"


def test_long_close_rest_above_threshold():
    assert funding_verdict("long", 5e-5) == "close_rest"
    assert funding_verdict("long", FUNDING_CLOSE_THRESH + _EPS) == "close_rest"


def test_long_close_rest_boundary_inclusive_trail():
    assert funding_verdict("long", FUNDING_CLOSE_THRESH) == "trail_exit"
    assert funding_verdict("long", FUNDING_CLOSE_THRESH - _EPS) == "trail_exit"


def test_long_middle_band_trail_exit():
    assert funding_verdict("long", 0.0) == "trail_exit"
    assert funding_verdict("long", 1e-5) == "trail_exit"


def test_short_mirrored_pyramid():
    assert funding_verdict("short", 2e-5) == "pyramid"
    assert funding_verdict("short", FUNDING_PYRAMID_THRESH + _EPS) == "pyramid"
    assert funding_verdict("short", FUNDING_PYRAMID_THRESH) == "trail_exit"


def test_short_mirrored_close_rest():
    assert funding_verdict("short", -5e-5) == "close_rest"
    assert funding_verdict("short", -FUNDING_CLOSE_THRESH - _EPS) == "close_rest"
    assert funding_verdict("short", -FUNDING_CLOSE_THRESH) == "trail_exit"


def test_funding_none_fails_closed_to_middle():
    assert funding_verdict("long", None) == "trail_exit"
    assert funding_verdict("short", None) == "trail_exit"


def test_funding_gate_degenerate_side_and_value():
    assert funding_verdict("flat", -1.0) == "trail_exit"
    assert funding_verdict("long", "junk") == "trail_exit"


# ── Axiom 2: retracement targets ─────────────────────────────────────────────

def test_retrace_long_math():
    l1, l2 = retrace_targets(100.0, 110.0, "long", 0.01)
    # move=10; L1 = 110 − 0.38×10 = 106.2; L2 = 100×1.005 = 100.5
    assert math.isclose(l1, 106.2, rel_tol=1e-12)
    assert math.isclose(l2, 100.5, rel_tol=1e-12)


def test_retrace_short_mirrored():
    l1, l2 = retrace_targets(100.0, 90.0, "short", 0.01)
    # L1 = 90 + 0.38×10 = 93.8; L2 = 100×0.995 = 99.5
    assert math.isclose(l1, 93.8, rel_tol=1e-12)
    assert math.isclose(l2, 99.5, rel_tol=1e-12)


def test_retrace_tick_rounding():
    # tick 0.5: L1 raw 106.2 → 212.4 ticks → 212 → 106.0; L2 100.5 on-grid.
    l1, l2 = retrace_targets(100.0, 110.0, "long", 0.5)
    assert l1 == 106.0
    assert l2 == 100.5


def test_retrace_degenerate_inputs():
    assert retrace_targets(0.0, 110.0, "long", 0.01) is None       # entry <= 0
    assert retrace_targets(100.0, 90.0, "long", 0.01) is None      # tp1 wrong side
    assert retrace_targets(100.0, 110.0, "short", 0.01) is None    # tp1 wrong side
    assert retrace_targets(100.0, 110.0, "long", 0.0) is None      # tick <= 0
    assert retrace_targets(100.0, 110.0, "flat", 0.01) is None     # bad side
    assert retrace_targets(None, 110.0, "long", 0.01) is None


# ── Axiom 3: pyramid sizing ladder + Tp1Track ────────────────────────────────

def test_pyramid_leg_sizes_first_two_legs():
    assert pyramid_leg_notional(1000.0, 0) == 500.0    # 0.5x
    assert pyramid_leg_notional(1000.0, 1) == 250.0    # 0.25x


def test_pyramid_cumulative_cap_binds_third_leg():
    # 1.0 + 0.5 + 0.25 = 1.75 = max_total_mult → zero headroom for leg 2.
    assert pyramid_leg_notional(1000.0, 2) == 0.0


def test_pyramid_beyond_ladder_returns_zero():
    assert pyramid_leg_notional(1000.0, 3) == 0.0
    assert pyramid_leg_notional(1000.0, 99) == 0.0


def test_pyramid_degenerate_inputs():
    assert pyramid_leg_notional(0.0, 0) == 0.0
    assert pyramid_leg_notional(-5.0, 0) == 0.0
    assert pyramid_leg_notional(1000.0, -1) == 0.0
    assert pyramid_leg_notional("x", 0) == 0.0


def test_pyramid_looser_cap_admits_third_leg():
    # Cap 2.0 restores the full default ladder: leg 2 = 0.25x again.
    assert pyramid_leg_notional(1000.0, 2, max_total_mult=2.0) == 250.0


def test_tp1_track_cumulative_mult():
    tr = Tp1Track(symbol="SOL-USD", side="long", entry=100.0,
                  tp1_price=110.0, fired_ts=1_000.0, orig_qty=10.0)
    assert tr.cumulative_mult() == 1.0                      # no legs yet
    tr.legs_filled.append((106.2, 5.0, 1_100.0))            # 531.0
    tr.legs_filled.append((100.5, 2.5, 1_200.0))            # 251.25
    # 1.0 + (531.0 + 251.25) / (100×10) = 1.78225
    assert math.isclose(tr.cumulative_mult(), 1.78225, rel_tol=1e-12)


def test_tp1_track_cumulative_mult_fail_safe():
    tr = Tp1Track(symbol="X", side="long", entry=100.0,
                  tp1_price=110.0, fired_ts=0.0, orig_qty=0.0)
    tr.legs_filled.append((105.0, 1.0, 1.0))
    assert tr.cumulative_mult() == 1.0                      # orig_qty unknown


# ── Axiom 4: scalp trigger verdict matrix ────────────────────────────────────

def test_scalp_fires_when_all_conditions_met():
    fire, reason = scalp_trigger_verdict(0.02, True, "pyramid", False)
    assert fire is True and reason == "fire"


def test_scalp_retrace_at_minimum_fires():
    fire, _ = scalp_trigger_verdict(0.015, True, "pyramid", False)
    assert fire is True


def test_scalp_skip_funding_not_pyramid():
    assert scalp_trigger_verdict(0.02, True, "trail_exit", False) == \
        (False, "funding_not_pyramid")
    assert scalp_trigger_verdict(0.02, True, "close_rest", False) == \
        (False, "funding_not_pyramid")


def test_scalp_skip_retrace_too_shallow():
    assert scalp_trigger_verdict(0.014, True, "pyramid", False) == \
        (False, "retrace_too_shallow")
    assert scalp_trigger_verdict(None, True, "pyramid", False) == \
        (False, "retrace_too_shallow")


def test_scalp_skip_accelerating():
    assert scalp_trigger_verdict(0.02, False, "pyramid", False) == \
        (False, "retrace_accelerating")


def test_scalp_skip_already_active():
    assert scalp_trigger_verdict(0.02, True, "pyramid", True) == \
        (False, "scalp_active")


# ── Axiom 5: scalp geometry ──────────────────────────────────────────────────

def test_scalp_bracket_long():
    tp, stop = scalp_bracket(100.0, "long", tick=0.01)
    assert math.isclose(tp, 100.5, rel_tol=1e-12)
    assert math.isclose(stop, 99.6, rel_tol=1e-12)


def test_scalp_bracket_short_mirrored():
    tp, stop = scalp_bracket(100.0, "short", tick=0.01)
    assert math.isclose(tp, 99.5, rel_tol=1e-12)
    assert math.isclose(stop, 100.4, rel_tol=1e-12)


def test_scalp_bracket_tick_rounding():
    # tp raw 100.53015 → 100.55; stop raw 99.62988 → 99.65 on tick 0.05.
    tp, stop = scalp_bracket(100.03, "long", tick=0.05)
    assert tp == 100.55
    assert stop == 99.65


def test_scalp_bracket_degenerate():
    assert scalp_bracket(0.0, "long", tick=0.01) is None
    assert scalp_bracket(100.0, "long", tick=0.0) is None
    assert scalp_bracket(100.0, "flat", tick=0.01) is None


# ── Axiom 6: lower-limit entry ladder (STAGED) ───────────────────────────────

def test_lower_limit_ladder_long_shape_and_staged():
    rows = lower_limit_ladder(100.0, "long", 0.01)
    assert [r["level"] for r in rows] == [1, 2, 3]
    assert [r["price"] for r in rows] == [99.0, 97.5, 96.0]
    assert [r["size_frac"] for r in rows] == list(LOWER_LIMIT_SIZES)
    assert all(r["stage"] == "staged" for r in rows)


def test_lower_limit_ladder_short_above_price():
    rows = lower_limit_ladder(100.0, "short", 0.01)
    assert [r["price"] for r in rows] == [101.0, 102.5, 104.0]
    assert all(r["stage"] == "staged" for r in rows)
    assert len(rows) == len(LOWER_LIMIT_DIPS)


def test_lower_limit_ladder_degenerate():
    assert lower_limit_ladder(0.0, "long", 0.01) == []
    assert lower_limit_ladder(100.0, "long", 0.0) == []
    assert lower_limit_ladder(100.0, "flat", 0.01) == []


# ── Axiom 7: landmark record ─────────────────────────────────────────────────

def test_landmark_record_shape():
    rec = landmark_record("SOL-USD", "long", 110.0, 1_234_567.0,
                          100.0, "momentum_cont")
    assert rec == {"ts": 1_234_567.0, "symbol": "SOL-USD", "side": "long",
                   "tp1_price": 110.0, "entry_price": 100.0,
                   "move_pct": 0.1, "trade_class": "momentum_cont"}


def test_landmark_record_degenerate_entry():
    rec = landmark_record("X", "short", 90.0, 0.0, 0.0, "scalp")
    assert rec["move_pct"] is None
    assert rec["tp1_price"] == 90.0


# ── Axiom 8: state machine ───────────────────────────────────────────────────

def _track(state: str) -> Tp1Track:
    return Tp1Track(symbol="SOL-USD", side="long", entry=100.0,
                    tp1_price=110.0, fired_ts=1_000.0, state=state)


def test_state_machine_happy_path():
    s, ok = next_state(_track("IDLE"), "tp1_fired")
    assert (s, ok) == ("TP1_FIRED", True)
    s, ok = next_state(_track("TP1_FIRED"), "l1_placed")
    assert (s, ok) == ("L1_PENDING", True)
    s, ok = next_state(_track("L1_PENDING"), "l1_filled")
    assert (s, ok) == ("L1_ACTIVE", True)
    s, ok = next_state(_track("L1_ACTIVE"), "l2_filled")
    assert (s, ok) == ("EXHAUSTED", True)


def test_state_machine_ttl_expired_from_l1_pending():
    assert next_state(_track("L1_PENDING"), "ttl_expired") == ("EXHAUSTED", True)


def test_state_machine_verdict_close_rest_from_tp1_fired():
    assert next_state(_track("TP1_FIRED"), "verdict_close_rest") == ("CLOSED", True)


def test_state_machine_carrier_closed_terminal():
    assert next_state(_track("L1_ACTIVE"), "carrier_closed") == ("CLOSED", True)
    assert next_state(_track("L1_PENDING"), "carrier_closed") == ("CLOSED", True)


def test_state_machine_illegal_transitions_unchanged():
    assert next_state(_track("IDLE"), "l1_filled") == ("IDLE", False)
    assert next_state(_track("TP1_FIRED"), "l1_filled") == ("TP1_FIRED", False)
    assert next_state(_track("CLOSED"), "tp1_fired") == ("CLOSED", False)
    assert next_state(_track("EXHAUSTED"), "l1_placed") == ("EXHAUSTED", False)
    assert next_state(_track("L1_ACTIVE"), "not_an_event") == ("L1_ACTIVE", False)


def test_state_machine_states_enum_covers_terminals():
    assert set(TP1_STATES) == {"IDLE", "TP1_FIRED", "L1_PENDING", "L1_ACTIVE",
                               "EXHAUSTED", "CLOSED"}


# ── Knob identity (the ladder defaults the orchestrator mirrors) ─────────────

def test_default_knob_values_match_axioms():
    assert FUNDING_PYRAMID_THRESH == 1e-5
    assert FUNDING_CLOSE_THRESH == 3e-5
    assert PYRAMID_LADDER == (0.5, 0.25, 0.25)
    assert MAX_TOTAL_MULT == 1.75
