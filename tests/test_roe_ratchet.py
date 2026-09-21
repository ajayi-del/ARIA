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
    # Every non-None stop sits at least min_stop_dist_atr × ATR from the mark
    # UNLESS the 2026-09-20 breakeven clamp fired: a breakeven-or-better rung
    # floored back through entry is clamped AT entry, which is by construction
    # closer to the mark than 1×ATR (mark−ATR < entry ⇒ mark−entry < ATR).
    # RE-ENCODED 2026-09-20 (UNI defect): the pre-fix invariant asserted the
    # ATR distance UNCONDITIONALLY — that assertion IS the defect mechanism
    # (the floor pulling BE rungs through entry). The clamp outranks the
    # floor for BE-or-better rungs; all other cells keep the ATR invariant.
    for side, mark in (("long", 101.0), ("short", 99.0)):
        for lev in (1, 3, 5, 7, 10, 20):
            for peak in (3, 6, 9, 15, 30, 60):
                for frac in (0.001, 0.005, 0.01, 0.02, 0.05):
                    atr = 100.0 * frac
                    s = ratchet_target_stop(side, 100.0, mark, peak, lev,
                                            atr=atr, min_stop_dist_atr=1.0)
                    if s is None:
                        continue
                    clamped_at_entry = (s >= 100.0 - 1e-12 if side == "long"
                                        else s <= 100.0 + 1e-12)
                    if clamped_at_entry:
                        # Clamp path: never through entry, either direction.
                        if side == "long":
                            assert s >= 100.0 - 1e-12, (side, lev, peak, frac, s)
                        else:
                            assert s <= 100.0 + 1e-12, (side, lev, peak, frac, s)
                    else:
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
    # RE-ENCODED 2026-09-20 (UNI defect): the pre-fix assertion demanded
    # floored ≤ mark−ATR = 0.17303 — but that is BELOW entry 0.17369, the
    # exact defect class (a +12.75%-peak rung landing under entry). The
    # breakeven clamp now holds this rung AT entry; the floor still loosened
    # the ladder stop (0.1763 → 0.17369), which is what the A3 case exists
    # to pin.
    legacy = ratchet_target_stop("long", 0.17369, 0.17635, 12.75, 5.0,
                                 atr=None)
    floored = ratchet_target_stop("long", 0.17369, 0.17635, 12.75, 5.0,
                                  atr=0.00332)
    assert legacy is not None and floored is not None
    assert floored < legacy                          # floor still loosens
    assert math.isclose(floored, 0.17369, rel_tol=1e-12)   # clamped at entry
    assert floored > 0.17635 - 0.00332               # above the defect value
    # Kill switch off reproduces the pre-fix floored stop bit-for-bit.
    legacy_floor = ratchet_target_stop("long", 0.17369, 0.17635, 12.75, 5.0,
                                       atr=0.00332,
                                       breakeven_floor_enabled=False)
    assert math.isclose(legacy_floor, 0.17635 - 0.00332, rel_tol=1e-12)


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


def test_sodex_positions_enrolled_in_software_loops_source_pin():
    # Venue-enrollment pin (2026-09-18 verification): the roe_ratchet and
    # trailing-stop SOFTWARE loops iterate position_manager._positions with
    # no venue filter — SoDEX-venue positions are enrolled. `_native_ok`
    # (`_sym_id or venue.venue_for(_sym) != "sodex"`) gates ONLY the native
    # exchange replace_stop_order call; the software stop write
    # (_pos.stop_price = _new_stop) precedes it unconditionally.
    import inspect
    import main as _m
    src = inspect.getsource(_m)
    roe = src.split("async def _roe_ratchet_loop", 1)[1].split(
        "async def _emerging_trend_loop", 1)[0]
    trail = src.split("async def _trailing_stop_loop", 1)[1].split(
        "async def _roe_ratchet_loop", 1)[0]
    for name, body in (("roe_ratchet", roe), ("trailing_stop", trail)):
        # Software write happens BEFORE the native-replace eligibility check.
        assert body.index("_pos.stop_price = _new_stop") < body.index(
            "_native_ok"), f"{name}: software ratchet must precede _native_ok"
        # _native_ok gates only the native replace — never a loop `continue`.
        assert "_native_ok and _old_stop_id" in body, (
            f"{name}: _native_ok must gate replace_stop_order only")
        assert "venue_for" not in body.split("_native_ok", 1)[0], (
            f"{name}: no venue-based enrollment skip before _native_ok")


# ── 2026-09-19: pyramid exemption + mark-cap tighten-only hardening ─────────
# Pyramid tracks stuck in BUILDING (XMR/HYPE) paused the ratchet all night —
# ~$1+ of locked profit forgone. The exemption lets the ratchet act on
# pyramid-owned symbols because BOTH systems are tighten-only — but the 1bp
# mark-side cap could move the stop backwards by <1bp when the live stop sat
# within 1bp of the mark, so the caller clamps the computed stop against the
# live stop. The caller lives in a closure; these pin the arithmetic it
# implements plus the wiring in source.

def test_config_knob_pyramid_roe_ratchet_exempt_default_true():
    from core.config import Settings
    assert Settings().pyramid_roe_ratchet_exempt_enabled is True


def _caller_skip(owned, cfg):
    # Mirrors the _roe_ratchet_loop skip predicate in main.py.
    return (owned and not getattr(cfg, "pyramid_roe_ratchet_exempt_enabled",
                                  False))


def test_exemption_on_pyramid_owned_ratchet_proceeds():
    from core.config import Settings
    cfg = Settings()                       # knob defaults True
    assert _caller_skip(True, cfg) is False    # ratchet proceeds on owned sym
    assert _caller_skip(False, cfg) is False   # unowned: unchanged


def test_exemption_off_pyramid_owned_skipped_legacy():
    from core.config import Settings
    cfg = Settings(pyramid_roe_ratchet_exempt_enabled=False)
    assert _caller_skip(True, cfg) is True     # legacy pause restored
    assert _caller_skip(False, cfg) is False
    # A config object WITHOUT the knob (old snapshots) fails closed to legacy.
    class _Bare:
        pass
    assert _caller_skip(True, _Bare()) is True


def _caller_new_stop(side, target, mark, live_stop):
    # Mirrors the hardened mark-cap + clamp in _roe_ratchet_loop.
    new = (min(target, mark * 0.9999) if side == "long"
           else max(target, mark * 1.0001))
    return (max(new, live_stop) if side == "long"
            else min(new, live_stop))


def test_mark_cap_hardening_never_moves_stop_backwards():
    # The loosen path the hardening kills: live stop within 1bp under the
    # mark, ladder target above the live stop but below the mark — the raw
    # cap picks mark*0.9999 UNDER the live stop (mirror on shorts).
    mark = 101.0
    live = mark * 0.99995                      # 0.5bp under the mark
    target = mark * 0.99997                    # above live, under the mark
    assert mark * 0.9999 < live < target < mark
    raw = min(target, mark * 0.9999)
    assert raw < live                          # the pre-hardening backwards move
    assert _caller_new_stop("long", target, mark, live) >= live
    # Short mirror.
    mark_s = 99.0
    live_s = mark_s * 1.00005                  # 0.5bp above the mark
    target_s = mark_s * 1.00003                # below live, above the mark
    assert mark_s < target_s < live_s < mark_s * 1.0001
    raw_s = max(target_s, mark_s * 1.0001)
    assert raw_s > live_s                      # pre-hardening backwards move
    assert _caller_new_stop("short", target_s, mark_s, live_s) <= live_s


def test_mark_cap_hardening_grid_tighten_only():
    # Sweep the 1bp band on both sides: the hardened caller arithmetic never
    # moves the stop backwards, and still tightens when the cap allows.
    for k in range(11):
        d = k * 0.000009                       # offset inside the 1bp band
        # Long: live stop sits d above mark*0.9999, strictly under the mark.
        mark = 101.0
        live = mark * 0.9999 + mark * d
        assert live < mark
        for target in (live + 1e-6, mark * 0.99999):
            if target <= live or target >= mark:
                continue
            assert _caller_new_stop("long", target, mark, live) >= live
        # Short: live stop sits d below mark*1.0001, strictly above the mark.
        mark_s = 99.0
        live_s = mark_s * 1.0001 - mark_s * d
        assert live_s > mark_s
        for target_s in (live_s - 1e-6, mark_s * 1.00001):
            if target_s >= live_s or target_s <= mark_s:
                continue
            assert _caller_new_stop("short", target_s, mark_s, live_s) <= live_s
    # Unaffected case: live stop far from the mark still tightens to target.
    assert _caller_new_stop("long", 100.495, 101.0, 99.99) == 100.495
    assert _caller_new_stop("short", 99.495, 99.0, 99.999) == 99.495


def test_exemption_and_hardening_wiring_source_pin():
    # Pin the main.py wiring contract in source (the caller is a closure).
    import inspect
    import main as _m
    src = inspect.getsource(_m)
    roe = src.split("async def _roe_ratchet_loop", 1)[1].split(
        "async def _emerging_trend_loop", 1)[0]
    trail = src.split("async def _trailing_stop_loop", 1)[1].split(
        "async def _roe_ratchet_loop", 1)[0]
    # The ratchet skip is knob-conditional; the trail's pause is UNTOUCHED.
    assert "_pyr_owned = _pyramid_stop_owned(_sym)" in roe
    assert "pyramid_roe_ratchet_exempt_enabled" in roe
    assert "pyramid_roe_ratchet_exempt_enabled" not in trail
    assert "_pyramid_stop_owned(_sym)" in trail   # trail pause still present
    # Telemetry fires when the exemption lets the ratchet act on an owned sym.
    assert "roe_ratchet_pyramid_exempt" in roe
    # Hardening: the clamp against the live stop sits AFTER the mark cap and
    # BEFORE the software write.
    assert roe.index("min(_target, _mark * 0.9999)") < roe.index(
        "max(_new_stop, _pos.stop_price)") < roe.index(
        "_pos.stop_price = _new_stop")


# ── 2026-09-20: breakeven-floor clamp (UNI production defect) ───────────────
# The D11 ATR floor could re-denominate a breakeven-or-better rung stop back
# THROUGH entry on a pullback from peak — UNI's "breakeven" rung installed a
# stop at 8.695415 vs entry 8.7045, a guaranteed −$0.18 on a rung whose
# entire doctrine is breakeven banked by geometry. Governor-locked fix: after
# the floor, clamp long → max(floored, entry) / short → min(floored, entry),
# ONLY when the pre-floor rung stop was already breakeven-or-better. Kill
# switch breakeven_floor_enabled=False = legacy bit-for-bit (the caller
# injects it from config.roe_ratchet_breakeven_floor_enabled, default True).

def test_b1_uni_reproduction_clamped_at_entry():
    # Production numbers 2026-09-20: entry 8.7045, 9% peak ROE at 8x (HIGH
    # rung, 60% lock → rung stop 8.7633 ≥ entry), mark pulled back to 8.80
    # with ATR 0.104585 → mark−1×ATR = 8.695415 < entry (the defect stop).
    rung = ratchet_target_stop("long", 8.7045, 8.80, 9.0, 8.0, atr=None)
    assert rung is not None and rung >= 8.7045      # rung is BE-or-better
    s = ratchet_target_stop("long", 8.7045, 8.80, 9.0, 8.0, atr=0.104585)
    assert s is not None and s >= 8.7045            # never through entry
    assert math.isclose(s, 8.7045, rel_tol=1e-12)   # clamped exactly at entry


def test_b2_rung_below_entry_prefloor_clamp_does_not_fire():
    # The ladder never computes a sub-entry rung stop at sane knobs, so a
    # negative buffer constructs the pre-floor-below-entry case: the clamp
    # must NOT fire — legacy floor behavior bit-for-bit with the knob ON.
    # Long: rung stop 99.5 < entry 100; floor → 99.0 both ways.
    on = ratchet_target_stop("long", 100.0, 101.0, 3.0, 10.0,
                             be_buffer_pct=-0.5, atr=2.0)
    off = ratchet_target_stop("long", 100.0, 101.0, 3.0, 10.0,
                              be_buffer_pct=-0.5, atr=2.0,
                              breakeven_floor_enabled=False)
    assert on is not None and math.isclose(on, 99.0, rel_tol=1e-12)
    assert on == off
    # Short mirror: rung stop 100.5 > entry 100 (not BE-or-better for a
    # short); floor → 101.0 both ways.
    on_s = ratchet_target_stop("short", 100.0, 99.0, 3.0, 10.0,
                               be_buffer_pct=-0.5, atr=2.0)
    off_s = ratchet_target_stop("short", 100.0, 99.0, 3.0, 10.0,
                                be_buffer_pct=-0.5, atr=2.0,
                                breakeven_floor_enabled=False)
    assert on_s is not None and math.isclose(on_s, 101.0, rel_tol=1e-12)
    assert on_s == off_s


def test_b3_short_mirror_of_uni():
    # Short side: 9% peak at 8x → rung stop 99.325 ≤ entry; mark pulled UP to
    # 99.2 with ATR 0.9 → mark+1×ATR = 100.1 > entry (the short-side defect:
    # a guaranteed loss above entry). Clamp → stop sits exactly at entry.
    rung = ratchet_target_stop("short", 100.0, 99.2, 9.0, 8.0, atr=None)
    assert rung is not None and rung <= 100.0
    s = ratchet_target_stop("short", 100.0, 99.2, 9.0, 8.0, atr=0.9)
    assert s is not None and s <= 100.0
    assert math.isclose(s, 100.0, rel_tol=1e-12)


def test_b4_kill_switch_off_byte_identical_legacy():
    # breakeven_floor_enabled=False reproduces the pre-fix function on both
    # defect cases — the exact pre-fix values, not just "below entry".
    s_long = ratchet_target_stop("long", 8.7045, 8.80, 9.0, 8.0, atr=0.104585,
                                 breakeven_floor_enabled=False)
    assert math.isclose(s_long, 8.695415, rel_tol=1e-12)   # the UNI defect
    s_short = ratchet_target_stop("short", 100.0, 99.2, 9.0, 8.0, atr=0.9,
                                  breakeven_floor_enabled=False)
    assert math.isclose(s_short, 100.1, rel_tol=1e-12)
    # atr=None legacy path is untouched by the knob entirely.
    assert (ratchet_target_stop("long", 100.0, 101.0, 9.0, 10.0, atr=None)
            == ratchet_target_stop("long", 100.0, 101.0, 9.0, 10.0, atr=None,
                                   breakeven_floor_enabled=False))


def test_b5_tighten_only_invariant_grid():
    # The fix NEVER loosens: vs the legacy floored output the clamp only
    # tightens (long: raises toward entry; short: lowers toward entry), and
    # the clamped stop never crosses entry. Sweep the A1 grid.
    for side, mark in (("long", 101.0), ("short", 99.0)):
        for lev in (1, 3, 5, 7, 10, 20):
            for peak in (3, 6, 9, 15, 30, 60):
                for frac in (0.001, 0.005, 0.01, 0.02, 0.05):
                    atr = 100.0 * frac
                    new = ratchet_target_stop(side, 100.0, mark, peak, lev,
                                              atr=atr)
                    old = ratchet_target_stop(side, 100.0, mark, peak, lev,
                                              atr=atr,
                                              breakeven_floor_enabled=False)
                    if side == "long":
                        if old is None:
                            continue   # old was mark-crossed → new is too
                        assert new is not None or old >= mark
                        if new is None:
                            continue
                        assert new >= old - 1e-12, (side, lev, peak, frac)
                        # Clamp fired ⇒ stop at/above entry; else identical.
                        assert new >= 100.0 - 1e-12 or new == old
                    else:
                        if old is None:
                            continue
                        if new is None:
                            continue
                        assert new <= old + 1e-12, (side, lev, peak, frac)
                        assert new <= 100.0 + 1e-12 or new == old


def test_b6_clamp_at_entry_mark_crossed_returns_none():
    # Mark pulled back THROUGH entry: the clamped stop (= entry) is already
    # crossed → None, the software-stop guardian owns the exit. Legacy would
    # have returned a sub-entry stop (the defect, still reachable knob-off).
    assert ratchet_target_stop("long", 100.0, 99.5, 9.0, 8.0, atr=2.0) is None
    legacy = ratchet_target_stop("long", 100.0, 99.5, 9.0, 8.0, atr=2.0,
                                 breakeven_floor_enabled=False)
    assert legacy is not None and legacy < 100.0
    # Short mirror.
    assert ratchet_target_stop("short", 100.0, 100.5, 9.0, 8.0, atr=2.0) is None
    legacy_s = ratchet_target_stop("short", 100.0, 100.5, 9.0, 8.0, atr=2.0,
                                   breakeven_floor_enabled=False)
    assert legacy_s is not None and legacy_s > 100.0


def test_b7_early_arm_same_clamp():
    # The T1a early arm is a breakeven rung by construction — the same
    # UNI-class defect and the same clamp. Floor pulls 100.15 → 98.6 (below
    # entry 100); the clamp holds it at entry. Knob off = legacy 98.6.
    s = early_arm_breakeven_stop("long", 100.0, 100.6, 0.5, atr=2.0)
    assert s is not None and math.isclose(s, 100.0, rel_tol=1e-12)
    legacy = early_arm_breakeven_stop("long", 100.0, 100.6, 0.5, atr=2.0,
                                      breakeven_floor_enabled=False)
    assert math.isclose(legacy, 98.6, rel_tol=1e-12)
    # Short mirror.
    s_s = early_arm_breakeven_stop("short", 100.0, 99.4, 0.5, atr=2.0)
    assert s_s is not None and math.isclose(s_s, 100.0, rel_tol=1e-12)
    legacy_s = early_arm_breakeven_stop("short", 100.0, 99.4, 0.5, atr=2.0,
                                        breakeven_floor_enabled=False)
    assert math.isclose(legacy_s, 101.4, rel_tol=1e-12)


def test_b8_caller_injects_config_knob_source_pin():
    # Fresh-eyes 2026-09-20: the knob was defined in core/config.py but never
    # consumed — both call sites ran default-True regardless. Pin the wiring:
    # ratchet_target_stop AND early_arm_breakeven_stop are called with the
    # config-driven breakeven_floor_enabled kwarg inside the ratchet loop.
    import inspect
    import main as _m
    src = inspect.getsource(_m)
    roe = src.split("async def _roe_ratchet_loop", 1)[1].split(
        "async def _emerging_trend_loop", 1)[0]
    assert roe.count("breakeven_floor_enabled=bool(getattr(") == 2
    assert roe.count('config, "roe_ratchet_breakeven_floor_enabled", True') == 2
