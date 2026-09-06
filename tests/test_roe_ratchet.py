"""Pins for intelligence/roe_ratchet.py — the peak-ROE mechanical stop
ratchet (2026-09-04 operator directive: track the peak ROE and chase it
mechanically; a trade that hits a threshold has been PROVED, its stop
ratchets even mid-trade; give back less, rotate capital faster)."""
import math

from intelligence.roe_ratchet import (
    BE_BUFFER_PCT, BE_RUNG_PCT, HIGH_LOCK_FRAC, HIGH_RUNG_PCT,
    MID_LOCK_FRAC, MID_RUNG_PCT, RUNNER_LOCK_FRAC, RUNNER_RUNG_PCT,
    ratchet_target_stop, roe_pct,
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
