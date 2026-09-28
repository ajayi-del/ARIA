"""tests/test_vwap_reversion.py — pins for the E3 VWAP mean-reversion brain.

Pinned: the Governor's worked examples (+1.6% COIN -> SHORT at stop
entry x 1.02 / tp at 0.8 x 1.6% reversion; -2.1% NVDA -> LONG through its
2.0% trigger; +/-1.6% NVDA -> None below the trigger), the exact core-hours
boundaries (13:29:59 vs 13:30:00, 19:59:59 vs 20:00:00), one-position-per-
symbol, the 50/day governor ceiling, every exit verdict (stop / tp /
time_stop at 3600s / vwap_cross with the 900s min-hold guard), and the
kill switch.
"""
from __future__ import annotations

import datetime

import pytest

from intelligence.vwap_reversion import (
    DAILY_TRADE_CEILING,
    ELIGIBLE,
    VwapSignal,
    core_hours_active,
    exit_verdict,
    fade_signal,
    vwap_reversion_enabled,
)


def _dt(hour: int, minute: int = 0, second: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 9, 26, hour, minute, second)


IN_CORE = _dt(14, 0)


class TestCoreHours:
    def test_boundaries(self):
        assert core_hours_active(_dt(13, 29, 59)) is False
        assert core_hours_active(_dt(13, 30, 0)) is True
        assert core_hours_active(_dt(19, 59, 59)) is True
        assert core_hours_active(_dt(20, 0, 0)) is False

    def test_epoch_input(self):
        ts = datetime.datetime(2026, 9, 26, 14, 0,
                               tzinfo=datetime.timezone.utc).timestamp()
        assert core_hours_active(ts) is True

    def test_garbage_fails_closed(self):
        assert core_hours_active(None) is False
        assert core_hours_active("x") is False


class TestFadeSignal:
    def test_coin_short_worked_example(self):
        # +1.6% deviation on COIN in core hours -> SHORT
        sig = fade_signal("COIN", 250.0, 1.6, IN_CORE, 0, False)
        assert sig is not None
        assert sig.symbol == "COIN"
        assert sig.direction == "short"
        assert sig.entry == 250.0
        assert sig.stop == pytest.approx(250.0 * 1.02)          # 255.0
        assert sig.tp == pytest.approx(250.0 * (1 - 0.0128))    # 246.8
        assert sig.max_hold_s == 3600.0
        assert sig.leverage == 5

    def test_nvda_long_past_its_trigger(self):
        # -2.1% on NVDA clears the 2.0% trigger -> LONG
        sig = fade_signal("NVDA", 100.0, -2.1, IN_CORE, 0, False)
        assert sig is not None
        assert sig.direction == "long"
        assert sig.stop == pytest.approx(100.0 * 0.98)
        assert sig.tp == pytest.approx(100.0 * (1 + 0.8 * 2.1 / 100))

    def test_nvda_below_trigger_abstains(self):
        # +/-1.6% on NVDA is below its 2.0% trigger -> None
        assert fade_signal("NVDA", 100.0, 1.6, IN_CORE, 0, False) is None
        assert fade_signal("NVDA", 100.0, -1.6, IN_CORE, 0, False) is None

    def test_outside_core_hours_abstains(self):
        assert fade_signal("COIN", 250.0, 1.6, _dt(13, 29, 59), 0, False) is None
        assert fade_signal("COIN", 250.0, 1.6, _dt(13, 30, 0), 0, False) is not None
        assert fade_signal("COIN", 250.0, 1.6, _dt(19, 59, 59), 0, False) is not None
        assert fade_signal("COIN", 250.0, 1.6, _dt(20, 0, 0), 0, False) is None

    def test_open_position_blocks_second_signal(self):
        assert fade_signal("COIN", 250.0, 1.6, IN_CORE, 0, True) is None

    def test_daily_ceiling(self):
        assert fade_signal("COIN", 250.0, 1.6, IN_CORE,
                           DAILY_TRADE_CEILING, False) is None
        assert fade_signal("COIN", 250.0, 1.6, IN_CORE,
                           DAILY_TRADE_CEILING - 1, False) is not None

    def test_ineligible_symbol_abstains(self):
        assert fade_signal("TSLA", 100.0, 3.0, IN_CORE, 0, False) is None

    def test_venue_suffix_normalizes(self):
        sig = fade_signal("COIN-USD", 250.0, 1.6, IN_CORE, 0, False)
        assert sig is not None and sig.symbol == "COIN"

    def test_amd_wider_band(self):
        # AMD doctrine: 1.5% trigger, stop widened +0.75% (2.75), TP +0.75%
        sig = fade_signal("AMD", 100.0, -1.6, IN_CORE, 0, False)
        assert sig is not None and sig.direction == "long"
        assert sig.stop == pytest.approx(100.0 * (1 - 0.0275))
        assert sig.tp == pytest.approx(100.0 * (1 + (0.8 * 1.6 + 0.75) / 100))

    def test_kill_switch(self, monkeypatch):
        monkeypatch.setenv("VWAP_REVERSION_ENABLED", "false")
        assert vwap_reversion_enabled() is False
        assert fade_signal("COIN", 250.0, 1.6, IN_CORE, 0, False) is None


class TestExitVerdict:
    def _coin_short(self) -> VwapSignal:
        return fade_signal("COIN", 250.0, 1.6, IN_CORE, 0, False)

    def _nvda_long(self) -> VwapSignal:
        return fade_signal("NVDA", 100.0, -2.1, IN_CORE, 0, False)

    def test_short_tp(self):
        sig = self._coin_short()
        assert exit_verdict(sig, sig.tp - 0.01, 248.0, 1200) == (
            "tp", "tp_reversion_captured")

    def test_short_stop(self):
        sig = self._coin_short()
        assert exit_verdict(sig, sig.stop + 0.01, 252.0, 1200) == (
            "stop", "stop_adverse")

    def test_long_tp_and_stop(self):
        sig = self._nvda_long()
        assert exit_verdict(sig, sig.tp + 0.01, 100.5, 1200) == (
            "tp", "tp_reversion_captured")
        assert exit_verdict(sig, sig.stop - 0.01, 99.0, 1200) == (
            "stop", "stop_adverse")

    def test_time_stop_at_max_hold(self):
        sig = self._coin_short()
        # price parked mid-range, vwap far enough that no cross fires
        assert exit_verdict(sig, 249.0, 246.0, 3600) == (
            "time_stop", "max_hold_expired")
        assert exit_verdict(sig, 249.0, 246.0, 3599) is None

    def test_vwap_cross_fires_after_min_hold(self):
        sig = self._nvda_long()
        # entry 100 long; price back at/above vwap, below tp, past min hold
        assert exit_verdict(sig, 100.6, 100.5, 1000) == (
            "vwap_cross", "vwap_cross")

    def test_vwap_cross_blocked_inside_min_hold(self):
        sig = self._nvda_long()
        assert exit_verdict(sig, 100.6, 100.5, 899) is None

    def test_short_vwap_cross(self):
        sig = self._coin_short()
        # price fell back to vwap but above tp... construct: vwap moved
        # toward price so the cross fires before tp
        assert exit_verdict(sig, 248.5, 248.6, 1000) == (
            "vwap_cross", "vwap_cross")

    def test_no_verdict_mid_trade(self):
        sig = self._nvda_long()
        assert exit_verdict(sig, 100.2, 101.0, 500) is None

    def test_garbage_fails_closed(self):
        sig = self._nvda_long()
        assert exit_verdict(sig, None, 100.5, 1000) is None
        assert exit_verdict(None, 100.0, 100.5, 1000) is None
