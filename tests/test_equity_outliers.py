"""Behavioral tests for intelligence/equity_outliers.py (2026-10-17
Governor directive, STOCK EXECUTION ARCHITECTURE spec).

House style: the brains are pure (zero I/O) — tests exercise verdicts
directly. Textual wiring pins assert the env kill switch and the four
event names exist in the module source (the splice is orchestrator-owned
and applied separately, so no main.py pins here).

Per detector: normal case fires, below-threshold doesn't, insufficient-
data abstains, stale-data abstains (OI detectors), no-earnings-date
abstains (pre-earnings), boundary at threshold.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence import equity_outliers as eo  # noqa: E402

_NOW = 1_800_000_000.0           # epoch seconds
_MS = 1000.0


def _series(deltas, start=100.0, newest_ts_ms=None):
    """Build an oldest-first (ts_ms, oi) series from per-day deltas."""
    newest_ts_ms = newest_ts_ms if newest_ts_ms is not None else _NOW * _MS
    n = len(deltas) + 1
    oi = start
    pts = []
    for i in range(n):
        ts = newest_ts_ms - (n - 1 - i) * 86400.0 * _MS
        pts.append((ts, oi))
        if i < len(deltas):
            oi += deltas[i]
    return pts


# ── 1. OI velocity ────────────────────────────────────────────────────────────

class TestOIVelocity:
    def test_fires_on_acceleration(self):
        # Baseline deltas pstdev 1.0 (0/2 alternating), newest delta 4 → z=3.
        v = eo.oi_velocity("AMD-USD", _series([0, 2, 0, 2, 0, 2, 4]), _NOW)
        assert v.fired is True and v.abstain_reason == ""
        assert v.event == "equity_outlier_oi_velocity"
        assert v.score == 3.0 and v.detail["side"] == "expansion"

    def test_below_threshold_does_not_fire(self):
        # newest delta 2.5 → z=1.5 < 2.0
        v = eo.oi_velocity("AMD-USD", _series([0, 2, 0, 2, 0, 2, 2.5]), _NOW)
        assert v.fired is False and v.score == 1.5 and v.abstain_reason == ""

    def test_contraction_side(self):
        v = eo.oi_velocity("AMD-USD", _series([0, 2, 0, 2, 0, 2, -2]), _NOW)
        assert v.fired is True and v.score == -3.0
        assert v.detail["side"] == "contraction"

    def test_insufficient_points_abstains(self):
        v = eo.oi_velocity("AMD-USD", _series([0, 2, 0, 2, 0]), _NOW)  # 6 pts
        assert v.fired is False and v.score is None
        assert v.abstain_reason == "insufficient_points"

    def test_stale_abstains(self):
        stale_newest = (_NOW - 49 * 3600.0) * _MS
        v = eo.oi_velocity("AMD-USD",
                           _series([0, 2, 0, 2, 0, 2, 4], newest_ts_ms=stale_newest),
                           _NOW)
        assert v.fired is False and v.abstain_reason == "stale"

    def test_degenerate_baseline_abstains(self):
        v = eo.oi_velocity("AMD-USD", _series([1, 1, 1, 1, 1, 1, 5]), _NOW)
        assert v.fired is False and v.abstain_reason == "degenerate_baseline"

    def test_boundary_at_threshold_fires(self):
        # newest delta 3 → z = (3-1)/1 = 2.0 exactly → fires (>= doctrine)
        v = eo.oi_velocity("AMD-USD", _series([0, 2, 0, 2, 0, 2, 3]), _NOW)
        assert v.score == 2.0 and v.fired is True

    def test_garbage_points_self_heal(self):
        pts = _series([0, 2, 0, 2, 0, 2, 4])
        pts.insert(2, ("bad", None))
        pts.insert(3, (123, -5.0))  # negative OI dropped
        v = eo.oi_velocity("AMD-USD", pts, _NOW)
        assert v.fired is True and v.score == 3.0


# ── 2. Rotation residual ──────────────────────────────────────────────────────

class TestRotationResidual:
    # Window in return scale: [-0.01, +0.01] × 5 → mean 0, pstdev 0.01.
    _W = [-0.01, 0.01] * 5

    def test_fires_on_idiosyncratic_move(self):
        v = eo.rotation_residual("NVDA-USD", 0.035, 0.005, self._W)  # resid 0.03
        assert v.fired is True and v.score == 3.0  # z = 0.03/0.01
        assert v.event == "equity_outlier_rotation_residual"

    def test_below_threshold_does_not_fire(self):
        v = eo.rotation_residual("NVDA-USD", 0.015, 0.0, self._W)  # z = 1.5
        assert v.fired is False and v.score == 1.5

    def test_insufficient_window_abstains(self):
        v = eo.rotation_residual("NVDA-USD", 0.05, 0.0, [-0.01, 0.01] * 4)
        assert v.fired is False and v.score is None
        assert v.abstain_reason == "insufficient_window"

    def test_degenerate_window_abstains(self):
        v = eo.rotation_residual("NVDA-USD", 0.05, 0.0, [0.005] * 12)
        assert v.fired is False and v.abstain_reason == "degenerate_window"

    def test_bad_input_abstains(self):
        v = eo.rotation_residual("NVDA-USD", None, 0.01, self._W)
        assert v.fired is False and v.abstain_reason == "bad_input"

    def test_boundary_at_threshold_fires(self):
        v = eo.rotation_residual("NVDA-USD", 0.02, 0.0, self._W)  # z = 2.0
        assert v.score == 2.0 and v.fired is True

    def test_window_garbage_filtered(self):
        window = list(self._W) + [None, "x"]
        v = eo.rotation_residual("NVDA-USD", 0.03, 0.0, window)
        assert v.fired is True and v.detail["window"] == 10


# ── 3. Funding divergence ─────────────────────────────────────────────────────

class TestFundingDivergence:
    def test_fires_on_divergence(self):
        # symbol 25 bps vs cohort median 0 → rich crowded long
        v = eo.funding_divergence("TSLA-USD", 0.0025, 0.0)
        assert v.fired is True and v.score == 25.0
        assert v.detail["side"] == "rich"
        assert v.event == "equity_outlier_funding_divergence"

    def test_below_threshold_does_not_fire(self):
        v = eo.funding_divergence("TSLA-USD", 0.0009, 0.0)  # 9 bps
        assert v.fired is False and v.score == 9.0

    def test_cheap_side(self):
        v = eo.funding_divergence("TSLA-USD", -0.0020, 0.0005)  # -25 bps
        assert v.fired is True and v.score == -25.0
        assert v.detail["side"] == "cheap"

    def test_bad_input_abstains(self):
        v = eo.funding_divergence("TSLA-USD", None, 0.0001)
        assert v.fired is False and v.score is None
        assert v.abstain_reason == "bad_input"

    def test_boundary_at_threshold_fires(self):
        v = eo.funding_divergence("TSLA-USD", 0.0010, 0.0)  # exactly 10 bps
        assert v.fired is True and v.score >= 10.0


# ── 4. Pre-earnings OI build ──────────────────────────────────────────────────

class TestPreEarningsOIBuild:
    _ETS = _NOW + 3 * 86400.0      # earnings in 3 days (inside 5d window)

    def test_fires_on_build(self):
        # window-start 100 → latest 115 = ratio 1.15 ≥ 1.10
        v = eo.pre_earnings_oi_build(
            "NVDA-USD", _series([5, 5, 0, 5], start=100.0), self._ETS, _NOW)
        assert v.fired is True and v.score == 1.15
        assert v.detail["days_to_earnings"] == 3.0
        assert v.event == "equity_outlier_pre_earnings_oi"

    def test_below_threshold_does_not_fire(self):
        v = eo.pre_earnings_oi_build(
            "NVDA-USD", _series([2, 2, 0, 2], start=100.0), self._ETS, _NOW)
        assert v.fired is False and v.score == 1.06

    def test_no_earnings_date_abstains(self):
        v = eo.pre_earnings_oi_build(
            "NVDA-USD", _series([5, 5, 0, 5], start=100.0), None, _NOW)
        assert v.fired is False and v.score is None
        assert v.abstain_reason == "no_earnings_date"

    def test_outside_window_abstains(self):
        far = _NOW + 6 * 86400.0   # 6 days out > 5-day window
        v = eo.pre_earnings_oi_build(
            "NVDA-USD", _series([5, 5, 0, 5], start=100.0), far, _NOW)
        assert v.fired is False and v.abstain_reason == "outside_window"

    def test_past_earnings_abstains(self):
        past = _NOW - 3600.0
        v = eo.pre_earnings_oi_build(
            "NVDA-USD", _series([5, 5, 0, 5], start=100.0), past, _NOW)
        assert v.fired is False and v.abstain_reason == "outside_window"

    def test_insufficient_points_abstains(self):
        v = eo.pre_earnings_oi_build(
            "NVDA-USD", _series([5, 5, 0], start=100.0), self._ETS, _NOW)
        assert v.fired is False and v.abstain_reason == "insufficient_points"

    def test_stale_abstains(self):
        stale_newest = (_NOW - 49 * 3600.0) * _MS
        v = eo.pre_earnings_oi_build(
            "NVDA-USD",
            _series([5, 5, 0, 5], start=100.0, newest_ts_ms=stale_newest),
            self._ETS, _NOW)
        assert v.fired is False and v.abstain_reason == "stale"

    def test_degenerate_base_abstains(self):
        v = eo.pre_earnings_oi_build(
            "NVDA-USD", _series([5, 5, 0, 5], start=0.0), self._ETS, _NOW)
        assert v.fired is False and v.abstain_reason == "degenerate_base"

    def test_boundary_at_threshold_fires(self):
        # 100 → 110 = ratio exactly 1.10 → fires (>= doctrine)
        v = eo.pre_earnings_oi_build(
            "NVDA-USD", _series([5, 5, 0, 0], start=100.0), self._ETS, _NOW)
        assert v.score == 1.1 and v.fired is True

    def test_boundary_days_to_earnings_in_window(self):
        # exactly 5.0 days out = inside the window (<= doctrine)
        ets = _NOW + 5 * 86400.0
        v = eo.pre_earnings_oi_build(
            "NVDA-USD", _series([5, 5, 0, 5], start=100.0), ets, _NOW)
        assert v.abstain_reason == "" and v.detail["days_to_earnings"] == 5.0


# ── Textual wiring pins ───────────────────────────────────────────────────────

_SRC = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "intelligence", "equity_outliers.py")


def _src():
    with open(_SRC) as fh:
        return fh.read()


class TestWiringPins:
    def test_env_kill_switch_present(self):
        assert "EQUITY_OUTLIER_ENABLED" in _src()

    def test_event_names_present(self):
        src = _src()
        for name in ("equity_outlier_oi_velocity",
                     "equity_outlier_rotation_residual",
                     "equity_outlier_funding_divergence",
                     "equity_outlier_pre_earnings_oi"):
            assert name in src

    def test_kill_switch_helper_default_true(self):
        os.environ.pop("EQUITY_OUTLIER_ENABLED", None)
        assert eo.equity_outlier_enabled() is True

    def test_kill_switch_helper_false(self):
        old = os.environ.get("EQUITY_OUTLIER_ENABLED")
        os.environ["EQUITY_OUTLIER_ENABLED"] = "false"
        try:
            assert eo.equity_outlier_enabled() is False
        finally:
            if old is None:
                os.environ.pop("EQUITY_OUTLIER_ENABLED", None)
            else:
                os.environ["EQUITY_OUTLIER_ENABLED"] = old

    def test_thresholds_documented_at_module_top(self):
        for const in ("OI_VELOCITY_Z_THRESHOLD",
                      "ROTATION_RESIDUAL_Z_THRESHOLD",
                      "FUNDING_DIVERGENCE_BPS_THRESHOLD",
                      "PRE_EARNINGS_BUILD_RATIO_THRESHOLD"):
            assert const in _src()
