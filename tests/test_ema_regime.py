"""regime-engine-v1 pins (Governor msg-186 P0/P1, filing regime-engine-v1).

Wounds pinned:
  HYPE 09-10 — shorted 40s after LOCKED trend/UP: a stale change_24h
    conflicted the fresh locked ORB read and the fail-open abstained the veto.
    locked_orb_wins: the locked ORB votes alone.
  OP 09-10 — 4 longs into a -6.49% breakdown: day_type != "trend" gated
    away ALL direction evidence. strong_move_mult: an extreme same-day move
    votes regardless of the ORB class.
  anti-tape classifier (98.3% trend rows at 42.4% accuracy): the EMA-slope
    plane needs no opening range and no midnight anchor.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.day_type_classifier import trend_direction_guard  # noqa: E402
from intelligence.ema_regime import (  # noqa: E402
    ema_series, ema_slope_trend, ema_alignment_verdict)


def _ramp(start, step, n):
    return [start + i * step for i in range(n)]


class TestEmaMath:
    def test_series_seed_and_length(self):
        out = ema_series([10.0, 12.0, 14.0], 3)
        assert len(out) == 3 and out[0] == 10.0
        # k = 2/(3+1) = 0.5 → second value = 10 + 0.5*(12-10) = 11
        assert abs(out[1] - 11.0) < 1e-9

    def test_empty_and_bad_period(self):
        assert ema_series([], 8) == []
        assert ema_series([1.0, 2.0], 0) == []


class TestEmaSlopeTrend:
    def test_uptrend_reads_long(self):
        d, sep = ema_slope_trend(_ramp(100.0, 1.0, 40), atr=2.0)
        assert d == "long" and sep >= 0.15

    def test_downtrend_reads_short(self):
        d, _ = ema_slope_trend(_ramp(200.0, -1.0, 40), atr=2.0)
        assert d == "short"

    def test_flat_reads_none(self):
        d, sep = ema_slope_trend([100.0] * 40, atr=2.0)
        assert d == "none" and sep == 0.0

    def test_insufficient_data_none(self):
        assert ema_slope_trend(_ramp(100.0, 1.0, 10))[0] == "none"

    def test_slope_disagreement_none(self):
        # fast above slow but falling sharply over the lookback → none
        closes = _ramp(100.0, 1.0, 36) + [134.0, 132.0, 129.0, 125.0]
        d, _ = ema_slope_trend(closes, atr=2.0, slope_lookback=3)
        assert d == "none"

    def test_separation_below_floor_none(self):
        d, _ = ema_slope_trend(_ramp(100.0, 0.01, 40), atr=5.0)
        assert d == "none"

    def test_no_atr_relative_fallback(self):
        # relative separation fallback still reads a strong ramp
        d, _ = ema_slope_trend(_ramp(100.0, 1.0, 40), atr=None,
                               min_sep_atr=0.05)
        assert d == "long"


class TestEmaAlignmentVerdict:
    def test_aligned_and_counter(self):
        up = _ramp(100.0, 1.0, 40)
        assert ema_alignment_verdict(up, "long", atr=2.0) == "aligned"
        assert ema_alignment_verdict(up, "short", atr=2.0) == "counter"

    def test_unknown_on_flat_and_bad_direction(self):
        assert ema_alignment_verdict([100.0] * 40, "long", atr=2.0) == "unknown"
        assert ema_alignment_verdict(_ramp(100.0, 1.0, 40), "sideways",
                                     atr=2.0) == "unknown"


class TestLockedOrbWins:
    """HYPE pin: locked trend/UP + stale negative 24h — the veto must bind."""

    def test_locked_orb_outranks_stale_24h(self):
        # legacy: conflict → unknown (fail-open abstain — the HYPE wound)
        assert trend_direction_guard(
            "trend", "up", -6.0, "short") == "unknown"
        # repaired: locked ORB votes alone → short is counter
        assert trend_direction_guard(
            "trend", "up", -6.0, "short",
            locked=True, locked_orb_wins=True) == "counter"
        assert trend_direction_guard(
            "trend", "up", -6.0, "long",
            locked=True, locked_orb_wins=True) == "aligned"

    def test_unlocked_keeps_conflict_fail_open(self):
        # not locked → legacy conflict abstain even with the knob on
        assert trend_direction_guard(
            "trend", "up", -6.0, "short",
            locked=False, locked_orb_wins=True) == "unknown"

    def test_locked_no_breakout_direction_still_legacy(self):
        assert trend_direction_guard(
            "trend", "", -6.0, "short",
            locked=True, locked_orb_wins=True) == "aligned"  # 24h source alone


class TestStrongMoveOverride:
    """OP pin: -6.49% day move with day_type=range — longs must read counter."""

    def test_extreme_move_votes_through_non_trend_class(self):
        assert trend_direction_guard(
            "range", "", None, "long",
            day_move_pct=-6.49, day_move_threshold=3.0,
            strong_move_mult=2.0) == "counter"
        assert trend_direction_guard(
            "range", "", None, "short",
            day_move_pct=-6.49, day_move_threshold=3.0,
            strong_move_mult=2.0) == "aligned"

    def test_below_strong_mult_stays_inert(self):
        assert trend_direction_guard(
            "range", "", None, "long",
            day_move_pct=-4.0, day_move_threshold=3.0,
            strong_move_mult=2.0) == "unknown"

    def test_mult_zero_is_legacy(self):
        assert trend_direction_guard(
            "range", "", None, "long",
            day_move_pct=-9.0, day_move_threshold=3.0,
            strong_move_mult=0.0) == "unknown"


class TestLegacyBitForBit:
    def test_defaults_unchanged(self):
        # All pre-existing call shapes behave identically (new params default off)
        assert trend_direction_guard("trend", "up", 9.0, "long") == "aligned"
        assert trend_direction_guard("trend", "up", -6.0, "short") == "unknown"
        assert trend_direction_guard("chop", "up", 9.0, "long") == "unknown"
        assert trend_direction_guard("trend", "", None, "long",
                                     day_move_pct=-4.0,
                                     day_move_threshold=3.0) == "counter"
