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
    # RE-ENCODED 2026-09-27 (Governor directive: "aster did not loose — aster
    # trades should not be undersized"; journal evidence: 7d Aster net
    # -$0.84 (n=393) vs SoDEX -$114.51 (n=250) — WR-reason recovery is
    # measured on the COMBINED book and the combined WR is SoDEX-driven, so
    # on the evidence it is venue-attributed, not strategy-attributed):
    # WR-reason recovery now skips aster-routed candidates. The kill switch
    # aster_wr_recovery_exempt_enabled=False preserves the 2026-08-27
    # "WR stays global" doctrine bit-for-bit (pinned below).
    assert aster_recovery_exempt("aster", "win_rate", True) is True
    assert aster_recovery_exempt("sodex", "win_rate", True) is False


def test_win_rate_exemption_kill_switch_is_legacy():
    # wr_enabled=False = 2026-08-27 doctrine bit-for-bit (WR binds aster).
    assert aster_recovery_exempt("aster", "win_rate", True, False) is False


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
