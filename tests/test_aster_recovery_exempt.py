"""Venue-decoupled recovery: DD-reason exempt on Aster, WR-reason global."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.venue import (  # noqa: E402
    aster_recovery_exempt, aster_recovery_exempt_enabled,
)


def test_aster_drawdown_is_exempt():
    assert aster_recovery_exempt("aster", "drawdown", True) is True


def test_sodex_drawdown_not_exempt():
    assert aster_recovery_exempt("sodex", "drawdown", True) is False


def test_bybit_drawdown_not_exempt():
    assert aster_recovery_exempt("bybit", "drawdown", True) is False


def test_win_rate_reason_applies_on_every_venue():
    # WR is strategy evidence, not a venue balance — never exempt
    assert aster_recovery_exempt("aster", "win_rate", True) is False
    assert aster_recovery_exempt("sodex", "win_rate", True) is False


def test_empty_reason_not_exempt():
    assert aster_recovery_exempt("aster", "", True) is False


def test_kill_switch_off_disables_exemption():
    assert aster_recovery_exempt("aster", "drawdown", False) is False


def test_kill_switch_default_true():
    os.environ.pop("ASTER_RECOVERY_EXEMPT_ENABLED", None)
    assert aster_recovery_exempt_enabled() is True


def test_kill_switch_env_false():
    os.environ["ASTER_RECOVERY_EXEMPT_ENABLED"] = "false"
    try:
        assert aster_recovery_exempt_enabled() is False
    finally:
        os.environ.pop("ASTER_RECOVERY_EXEMPT_ENABLED", None)
