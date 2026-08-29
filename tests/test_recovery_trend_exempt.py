"""Recovery trend-day exemption (2026-08-29): shadow evidence -912% net,
199/200 missed winners trend-aligned — aligned candidates participate at
the recovery size cap instead of refusal. Matrix pins on the pure helper."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.day_type_classifier import (  # noqa: E402
    recovery_trend_exempt, recovery_trend_exempt_enabled,
)


def test_aligned_exempts():
    assert recovery_trend_exempt("aligned", True) is True


def test_counter_fails_closed():
    assert recovery_trend_exempt("counter", True) is False


def test_unknown_fails_closed():
    assert recovery_trend_exempt("unknown", True) is False
    assert recovery_trend_exempt("", True) is False


def test_kill_switch_off_disables():
    assert recovery_trend_exempt("aligned", False) is False


def test_kill_switch_default_true():
    os.environ.pop("RECOVERY_TREND_DAY_EXEMPT_ENABLED", None)
    assert recovery_trend_exempt_enabled() is True


def test_kill_switch_env_false():
    os.environ["RECOVERY_TREND_DAY_EXEMPT_ENABLED"] = "false"
    try:
        assert recovery_trend_exempt_enabled() is False
    finally:
        os.environ.pop("RECOVERY_TREND_DAY_EXEMPT_ENABLED", None)
