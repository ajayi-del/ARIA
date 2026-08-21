"""Aster swing class + pyramid add pins (2026-08-20, Stage 1+2).

- aster_swing_floor_price: VWAP breakeven with the buffer on the LOSS side —
  the anti-martingale invariant (the pyramided trade cannot turn red beyond
  the buffer).
- aster_swing_add_gate: the native-evidence pyramid gate matrix. Every
  refusal fail-closed; the pass case requires alignment + banked TP1 inside
  the window + the execution venue's own L4 not contradicting.
- config knobs exist with the operator-set defaults.
"""
from main import aster_swing_floor_price, aster_swing_add_gate
from core.config import Settings


# ── floor math ───────────────────────────────────────────────────────────────

def test_floor_long_vwap_with_buffer_below():
    # 1.0 @ 100 + 0.4 @ 110 → BE 102.857; long floor sits 0.4% BELOW BE.
    be = (1.0 * 100 + 0.4 * 110) / 1.4
    assert aster_swing_floor_price("long", 1.0, 100, 0.4, 110, 0.004) == be * (1 - 0.004)


def test_floor_short_vwap_with_buffer_above():
    be = (1.0 * 100 + 0.4 * 90) / 1.4
    assert aster_swing_floor_price("short", 1.0, 100, 0.4, 90, 0.004) == be * (1 + 0.004)


def test_floor_long_add_above_entry_raises_breakeven():
    # The add chases strength — the floor must land ABOVE the base entry.
    floor = aster_swing_floor_price("long", 1.0, 100, 0.4, 110, 0.004)
    assert 100 < floor < 110


def test_floor_garbage_returns_zero():
    assert aster_swing_floor_price("long", 0.0, 100, 0.4, 110) == 0.0
    assert aster_swing_floor_price("long", 1.0, 0.0, 0.4, 110) == 0.0
    assert aster_swing_floor_price("long", 1.0, 100, 0.4, 0.0) == 0.0


# ── gate matrix ──────────────────────────────────────────────────────────────

def _gate(**over):
    kw = dict(verdict="aligned", day_move_pct=3.0, max_day_move_pct=10.0,
              tp1_age_s=120.0, window_s=1800.0, imbalance=0.25,
              direction="long", spread_bps=8.0, spread_cap_bps=25.0,
              recovery_active=False, pyramided=False)
    kw.update(over)
    return aster_swing_add_gate(**kw)


def test_gate_pass_case():
    assert _gate() == (True, "ok")


def test_gate_short_symmetry():
    assert _gate(direction="short", imbalance=-0.25) == (True, "ok")


def test_gate_requires_alignment_not_just_not_counter():
    # "unknown" is the fail-safe default — the smaller commitment.
    assert _gate(verdict="unknown")[0] is False
    assert _gate(verdict="counter") == (False, "trend_verdict_counter")


def test_gate_one_add_ever():
    assert _gate(pyramided=True) == (False, "already_pyramided")


def test_gate_no_adds_in_recovery():
    # Post-loss-day tilt defense: pyramiding is an amplifier, never a rescue.
    assert _gate(recovery_active=True) == (False, "recovery_mode")


def test_gate_window_enforced():
    assert _gate(tp1_age_s=1801.0) == (False, "tp1_window_expired")
    assert _gate(tp1_age_s=1800.0)[0] is True


def test_gate_day_move_exhaustion():
    assert _gate(day_move_pct=10.5) == (False, "day_move_exhausted")
    assert _gate(day_move_pct=-10.5) == (False, "day_move_exhausted")
    assert _gate(day_move_pct=None)[0] is True   # no evidence → inert check


def test_gate_l4_must_not_contradict():
    assert _gate(direction="long", imbalance=-0.15) == (False, "l4_against")
    assert _gate(direction="short", imbalance=0.15) == (False, "l4_against")
    # Mild disagreement is noise, not contradiction.
    assert _gate(direction="long", imbalance=-0.05)[0] is True


def test_gate_l4_missing_fails_closed():
    assert _gate(imbalance=None)[1] == "no_l4"
    assert _gate(spread_bps=None)[1] == "no_l4"


def test_gate_spread_cap():
    assert _gate(spread_bps=26.0) == (False, "spread_too_wide")
    assert _gate(spread_bps=25.0)[0] is True


def test_gate_bad_direction():
    assert _gate(direction="flat", imbalance=0.0)[0] is False


# ── config knobs ─────────────────────────────────────────────────────────────

def test_swing_config_defaults():
    c = Settings()
    assert c.aster_swing_enabled is True
    assert c.aster_swing_pyramid_frac == 0.40
    assert c.aster_swing_pyramid_window_s == 1800.0
    assert c.aster_swing_max_day_move_pct == 8.0
    assert c.aster_swing_add_max_day_move_pct == 10.0
    assert c.aster_swing_l4_spread_cap_bps == 25.0
