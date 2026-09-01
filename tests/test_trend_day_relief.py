"""Trend-day coherence relief + c_tier bypass (2026-09-01, watchdog proposal
coherence-floor-trend-day-conditional, operator-shipped pre-US-open).

The gates earn ~86% accuracy on range days but amputated the trend-day right
tail (coherence_floor x trend n=244 +992.8% 7d missed; c_tier x trend n=114
+423.1%). Relief is for ALIGNED candidates on locked trend days only, never
in recovery, floor relieved not waived (clamp >= 2.5)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.kant_gate import KantGate, COHERENCE_MINIMUM  # noqa: E402
from execution.execution_guardian import ExecutionGuardian  # noqa: E402


def _kant():
    return KantGate()


class TestKantCoherenceMinimumOverride:
    def test_default_is_legacy(self):
        v = _kant().check("BTC-USD", "long", coherence=2.9, rr_ratio=3.5,
                          balance=500.0, regime_state=None)
        assert v.allowed is False and v.log_event == "coherence_tier_reject"
        assert f"coherence_below_{COHERENCE_MINIMUM}_" in v.reason

    def test_override_admits_relieved_band(self):
        # 2.7 coherence: rejected at the 3.0 floor, admitted at relieved 2.5
        v = _kant().check("BTC-USD", "long", coherence=2.7, rr_ratio=3.5,
                          balance=500.0, regime_state=None,
                          coherence_minimum=2.5)
        assert v.allowed is True

    def test_override_never_waives_floor(self):
        v = _kant().check("BTC-USD", "long", coherence=2.4, rr_ratio=3.5,
                          balance=500.0, regime_state=None,
                          coherence_minimum=2.5)
        assert v.allowed is False and "coherence_below_2.5_" in v.reason

    def test_guardian_passthrough(self):
        g = ExecutionGuardian()
        v = g.check("BTC-USD", "long", coherence=2.7, rr_ratio=3.5,
                    balance=500.0, regime_state=None, coherence_minimum=2.5)
        assert v.allowed is True
        v2 = g.check("BTC-USD", "long", coherence=2.7, rr_ratio=3.5,
                     balance=500.0, regime_state=None)
        assert v2.allowed is False


class TestConfigDefaults:
    def test_knobs(self):
        from core.config import Settings
        s = Settings()
        assert s.trend_day_coherence_relief_enabled is True
        assert s.trend_day_coherence_relief == 0.5
        assert s.trend_day_c_tier_bypass_enabled is True


class TestMainWiring:
    @staticmethod
    def _src():
        return open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "main.py")).read()

    def test_both_guardian_sites_relieved(self):
        src = self._src()
        assert src.count("coherence_minimum=_trend_day_kant_floor(symbol, _sig_dir)") == 2

    def test_c_tier_bypass_before_rejection(self):
        src = self._src()
        i_bypass = src.index("c_tier_trend_day_bypass")
        i_reject = src.index('logger.info("signal_rejected_c_tier"')
        assert i_bypass < i_reject
        assert "trend_day_c_tier_bypass_enabled" in src
        assert "_trend_day_offense_ok(symbol, _sig_dir)" in src

    def test_helpers_and_telemetry(self):
        src = self._src()
        assert "def _trend_day_offense_ok" in src
        assert "def _trend_day_kant_floor" in src
        assert "trend_day_coherence_relief" in src
        # Recovery suppresses; verdict must be aligned (fail-closed)
        assert "_recovery_params_for(symbol)" in src
        assert '_trend_day_verdict(symbol, direction) == "aligned"' in src
        # Floor relieved, never waived
        assert "max(2.5, _KCM" in src
