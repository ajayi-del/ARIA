"""Pins for intelligence/roe_ratchet.py — the peak-ROE mechanical stop
ratchet (2026-09-04 operator directive: track the peak ROE and chase it
mechanically; a trade that hits a threshold has been PROVED, its stop
ratchets even mid-trade; give back less, rotate capital faster)."""
import math

from intelligence.roe_ratchet import (
    BE_BUFFER_PCT, BE_RUNG_PCT, EARLY_ARM_RUNG_PCT,
    HIGH_LOCK_FRAC, HIGH_RUNG_PCT,
    MID_LOCK_FRAC, MID_RUNG_PCT, RUNNER_LOCK_FRAC, RUNNER_RUNG_PCT,
    early_arm_breakeven_stop, early_arm_telemetry_due,
    merge_early_arm_target, ratchet_target_stop, roe_pct,
)


# ── roe_pct ──────────────────────────────────────────────────────────────────

def test_roe_pct_long_and_short_symmetric():
    # +1% price move at 10x = +10% ROE either direction.
    assert roe_pct("long", 100.0, 101.0, 10.0) == 10.0
    assert roe_pct("short", 100.0, 99.0, 10.0) == 10.0


def test_roe_pct_negative_when_underwater():
    assert roe_pct("long", 100.0, 99.5, 10.0) == -5.0
    assert roe_pct("short", 100.0, 100.5, 10.0) == -5.0


def test_roe_pct_degenerate_inputs():
    assert roe_pct("long", 0.0, 101.0, 10.0) is None
    assert roe_pct("long", 100.0, -1.0, 10.0) is None
    assert roe_pct("long", 100.0, 101.0, 0.0) is None
    assert roe_pct("long", None, 101.0, 10.0) is None
    assert roe_pct("long", "x", 101.0, 10.0) is None


# ── ladder rungs ─────────────────────────────────────────────────────────────

def test_below_first_rung_no_stop():
    assert ratchet_target_stop("long", 100.0, 101.0, 2.99, 10.0) is None
    assert ratchet_target_stop("short", 100.0, 99.0, 0.0, 10.0) is None


def test_breakeven_rung_buffer_beyond_entry():
    # ≥3% peak → stop = breakeven + 0.15% price buffer (fees + noise).
    s = ratchet_target_stop("long", 100.0, 101.0, BE_RUNG_PCT, 10.0)
    assert math.isclose(s, 100.0 * (1 + BE_BUFFER_PCT / 100.0), rel_tol=1e-12)
    s = ratchet_target_stop("short", 100.0, 99.0, BE_RUNG_PCT, 10.0)
    assert math.isclose(s, 100.0 * (1 - BE_BUFFER_PCT / 100.0), rel_tol=1e-12)


def test_mid_rung_locks_45pct_of_peak():
    # peak 6% ROE at 10x → lock 2.7% ROE = 0.27% price move.
    s = ratchet_target_stop("long", 100.0, 101.0, MID_RUNG_PCT, 10.0)
    locked_move = MID_RUNG_PCT * MID_LOCK_FRAC / (10.0 * 100.0)
    assert math.isclose(s, 100.0 * (1 + locked_move), rel_tol=1e-12)


def test_high_rung_locks_60pct_of_peak_operator_example():
    # The operator's example: 9% ROE → stop increases automatically.
    s = ratchet_target_stop("long", 100.0, 101.5, HIGH_RUNG_PCT, 10.0)
    locked_move = HIGH_RUNG_PCT * HIGH_LOCK_FRAC / (10.0 * 100.0)
    assert math.isclose(s, 100.0 * (1 + locked_move), rel_tol=1e-12)
    assert s > 100.0 * (1 + BE_BUFFER_PCT / 100.0)  # beats the BE rung


def test_runner_rung_trails_with_peak():
    # ≥15% → 70% lock; as the peak grows the locked level grows (30% giveback).
    s15 = ratchet_target_stop("long", 100.0, 103.0, RUNNER_RUNG_PCT, 10.0)
    s20 = ratchet_target_stop("long", 100.0, 103.5, 20.0, 10.0)
    locked15 = RUNNER_RUNG_PCT * RUNNER_LOCK_FRAC / (10.0 * 100.0)
    assert math.isclose(s15, 100.0 * (1 + locked15), rel_tol=1e-12)
    assert s20 > s15


def test_short_side_ladder_mirrors():
    s = ratchet_target_stop("short", 100.0, 98.5, HIGH_RUNG_PCT, 10.0)
    locked_move = HIGH_RUNG_PCT * HIGH_LOCK_FRAC / (10.0 * 100.0)
    assert math.isclose(s, 100.0 * (1 - locked_move), rel_tol=1e-12)


# ── mark-crossed → None (software-stop guardian owns the exit) ───────────────

def test_mark_crossed_returns_none():
    # A stop computed at/above the live mark (long) is already crossed.
    assert ratchet_target_stop("long", 100.0, 100.05, HIGH_RUNG_PCT, 10.0) is None
    assert ratchet_target_stop("short", 100.0, 99.95, HIGH_RUNG_PCT, 10.0) is None


def test_degenerate_inputs_return_none():
    assert ratchet_target_stop("long", 0.0, 101.0, 9.0, 10.0) is None
    assert ratchet_target_stop("long", 100.0, 101.0, 9.0, 0.0) is None
    assert ratchet_target_stop("flat", 100.0, 101.0, 9.0, 10.0) is None
    assert ratchet_target_stop("long", 100.0, 101.0, None, 10.0) is None


def test_leverage_scales_price_move_inversely():
    # Same locked ROE fraction → smaller price move at higher leverage.
    s8 = ratchet_target_stop("long", 100.0, 102.0, HIGH_RUNG_PCT, 8.0)
    s10 = ratchet_target_stop("long", 100.0, 102.0, HIGH_RUNG_PCT, 10.0)
    assert s8 > s10  # 8x locks further from entry in price terms


# ── config knobs ─────────────────────────────────────────────────────────────

def test_config_knobs_exist_with_directive_defaults():
    from core.config import Settings
    c = Settings()
    assert c.roe_ratchet_be_rung_pct == 3.0
    assert c.roe_ratchet_be_buffer_pct == 0.15
    assert c.roe_ratchet_min_stop_dist_atr == 1.0   # D11 — CLOSED to tuning
                                                    # until shadow gate n≥30


# ── D11 Fix A: ATR floor (CEO spec 2026-09-06) ──────────────────────────────
# The ladder is ROE-denominated; the market is volatility-denominated. The
# floor re-denominates the stop: never closer than min_stop_dist_atr × ATR.

def test_pin_a1_unit_invariant_grid():
    # Every non-None stop sits at least min_stop_dist_atr × ATR from the mark.
    for side, mark in (("long", 101.0), ("short", 99.0)):
        for lev in (1, 3, 5, 7, 10, 20):
            for peak in (3, 6, 9, 15, 30, 60):
                for frac in (0.001, 0.005, 0.01, 0.02, 0.05):
                    atr = 100.0 * frac
                    s = ratchet_target_stop(side, 100.0, mark, peak, lev,
                                            atr=atr, min_stop_dist_atr=1.0)
                    if s is None:
                        continue
                    assert abs(mark - s) >= 1.0 * atr - 1e-12, (
                        side, lev, peak, frac, s)


def test_pin_a2_legacy_bit_for_bit_golden():
    # atr=None (the kill-switch path) reproduces the pre-D11 function exactly.
    # Golden generated from the pre-change function, 576 cells.
    import json
    import os
    golden_path = os.path.join(os.path.dirname(__file__),
                               "roe_ratchet_legacy_golden.json")
    golden = json.load(open(golden_path))
    assert len(golden) == 576
    for key, expected in golden.items():
        side, lev, peak, mark = key.split("|")
        got = ratchet_target_stop(side, 100.0, float(mark), float(peak),
                                  float(lev), atr=None)
        if expected is None:
            assert got is None, key
        else:
            assert got is not None and abs(got - expected) < 1e-12, key


def test_pin_a3_arb_case():
    # The 2026-09-05 ARB long: the ratchet locked +1.53% price at 0.31× ATR
    # and the winner was cut before the move. The floor hands it back to the
    # ATR trail's geometry.
    legacy = ratchet_target_stop("long", 0.17369, 0.17635, 12.75, 5.0,
                                 atr=None)
    floored = ratchet_target_stop("long", 0.17369, 0.17635, 12.75, 5.0,
                                  atr=0.00332)
    assert legacy is not None and floored is not None
    assert floored < legacy
    assert floored <= 0.17635 - 0.00332 + 1e-12


def test_pin_a4_tighten_only_preserved_by_caller():
    # Caller contract (main.py _roe_ratchet_loop): a floored target WORSE than
    # the live stop is rejected — the stop never moves backwards. The caller
    # lives in a closure; this pins the arithmetic it implements.
    live_stop = 0.1740
    floored = ratchet_target_stop("long", 0.17369, 0.17635, 12.75, 5.0,
                                  atr=0.00332)
    assert floored is not None and floored < live_stop
    improve = floored - live_stop          # long: improvement = target − live
    assert improve <= 0                    # → caller continues, stop unchanged
    # ...and a floor target BETTER than the live stop still tightens.
    live_stop2 = 0.1700
    assert floored - live_stop2 > 0


# ── T1a (2026-09-08): early breakeven+buffer arm at +0.3% peak ROE ───────────
# 91.6% of September software_stop closes went positive first; only 7 trades
# ever reached the 3% rung, so the ladder never armed on the round-trip
# cohort. ROE_RATCHET_EARLY_ARM_ENABLED (env, default FALSE) gates the arm;
# the would-have-fired telemetry is ALWAYS on (3-window shadow proof).

def test_t1a_below_early_rung_no_stop():
    assert early_arm_breakeven_stop("long", 100.0, 101.0, 0.29) is None
    assert early_arm_breakeven_stop("short", 100.0, 99.0, 0.0) is None
    assert early_arm_breakeven_stop("long", 100.0, 101.0, -5.0) is None


def test_t1a_arms_at_0_3pct_breakeven_plus_buffer():
    # Mirrors the >=3% breakeven rung mechanics exactly: entry ± BE buffer.
    s = early_arm_breakeven_stop("long", 100.0, 100.5, EARLY_ARM_RUNG_PCT)
    assert math.isclose(s, 100.0 * (1 + BE_BUFFER_PCT / 100.0), rel_tol=1e-12)
    s = early_arm_breakeven_stop("short", 100.0, 99.5, EARLY_ARM_RUNG_PCT)
    assert math.isclose(s, 100.0 * (1 - BE_BUFFER_PCT / 100.0), rel_tol=1e-12)


def test_t1a_degenerate_inputs_return_none():
    assert early_arm_breakeven_stop("long", 0.0, 101.0, 0.5) is None
    assert early_arm_breakeven_stop("long", 100.0, -1.0, 0.5) is None
    assert early_arm_breakeven_stop("flat", 100.0, 101.0, 0.5) is None
    assert early_arm_breakeven_stop("long", 100.0, 101.0, None) is None


def test_t1a_mark_crossed_returns_none():
    # Entry 100, mark dipped to 99.9 but peak still ≥0.3 — the BE+buffer stop
    # is already crossed; the software-stop guardian owns the exit.
    assert early_arm_breakeven_stop("long", 100.0, 99.9, 0.5) is None
    assert early_arm_breakeven_stop("short", 100.0, 100.1, 0.5) is None


def test_t1a_atr_floor_applies_like_d11_rungs():
    # Wide mark with a big ATR: the floor pushes the stop off breakeven.
    s = early_arm_breakeven_stop("long", 100.0, 104.0, 0.5, atr=2.0,
                                 min_stop_dist_atr=1.0)
    assert s is not None and s <= 104.0 - 2.0 + 1e-12


def test_t1a_merge_knob_off_is_ladder_bit_for_bit():
    ladder = ratchet_target_stop("long", 100.0, 101.0, HIGH_RUNG_PCT, 10.0)
    early = early_arm_breakeven_stop("long", 100.0, 101.0, HIGH_RUNG_PCT)
    assert merge_early_arm_target("long", ladder, early, False) == ladder
    assert merge_early_arm_target("long", None, early, False) is None
    assert merge_early_arm_target("short", ladder, early, False) == ladder
    # early_target None → ladder untouched regardless of the knob.
    assert merge_early_arm_target("long", ladder, None, True) == ladder
    assert merge_early_arm_target("long", None, None, True) is None


def test_t1a_merge_knob_on_picks_tighter_rung():
    early = early_arm_breakeven_stop("long", 100.0, 101.0, 0.5)
    # Ladder has no target below 3% → early arm supplies the stop.
    assert ratchet_target_stop("long", 100.0, 101.0, 0.5, 10.0) is None
    assert merge_early_arm_target("long", None, early, True) == early
    # Both present → the tighter (higher for long, lower for short) wins.
    ladder = ratchet_target_stop("long", 100.0, 101.0, MID_RUNG_PCT, 10.0)
    assert ladder is not None and ladder > early   # 45% lock beats BE+buffer
    assert merge_early_arm_target("long", ladder, early, True) == ladder
    early_s = early_arm_breakeven_stop("short", 100.0, 99.0, 0.5)
    ladder_s = ratchet_target_stop("short", 100.0, 99.0, MID_RUNG_PCT, 10.0)
    assert ladder_s is not None and ladder_s < early_s
    assert merge_early_arm_target("short", ladder_s, early_s, True) == ladder_s


def test_t1a_telemetry_fires_regardless_of_knob():
    # early_arm_telemetry_due takes no knob argument — it cannot be gated.
    assert early_arm_telemetry_due(None, 1000, 0.31) is True
    assert early_arm_telemetry_due(None, 1000, EARLY_ARM_RUNG_PCT) is True


def test_t1a_telemetry_once_per_position_keyed_opened_at_ms():
    # Same position (same opened_at_ms) never re-fires; a new position on the
    # same symbol re-arms — the deploy-#37 registry idiom.
    assert early_arm_telemetry_due(1000, 1000, 1.5) is False
    assert early_arm_telemetry_due(1000, 2000, 1.5) is True
    assert early_arm_telemetry_due(None, 1000, 0.29) is False   # below rung
    assert early_arm_telemetry_due(None, 1000, None) is False   # degenerate


def test_t1a_loop_splice_knob_off_bit_for_bit_source_pin():
    # The main.py splice merges via merge_early_arm_target with the env read
    # defaulting FALSE — pin the wiring contract in source.
    import inspect
    import main as _m
    src = inspect.getsource(_m)
    assert '"ROE_RATCHET_EARLY_ARM_ENABLED", "false"' in src
    assert "merge_early_arm_target(_pos.side, _target," in src
    assert "roe_ratchet_early_arm_would_have_fired" in src
