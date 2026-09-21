"""Fix 2 pins (2026-09-20): aster leverage-set gap + boot cache seed.

Pins the entry-path leverage-set guard (aster-routed symbols — SoDEX
symbol_id 0 — join the call set only while aster_leverage_set_enabled; the
SEI 20x defect), the fail-closed-but-never-raises aster call wrapper, and
the boot cache seed (positionRisk is the only exchange read path and covers
open-position symbols only).
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


class _FakeVenue:
    def __init__(self, venue_name, result=5, raises=False):
        self._venue_name = venue_name
        if raises:
            self.update_leverage = AsyncMock(
                side_effect=RuntimeError("exchange rejected"))
        else:
            self.update_leverage = AsyncMock(return_value=result)

    def venue_for(self, symbol):
        return self._venue_name


class TestLeverageSetGuard:
    def test_aster_routed_symbol_gets_update_leverage(self):
        assert main._leverage_set_guard(0, "aster", True, 7) is True

    def test_kill_switch_false_aster_never_called(self):
        assert main._leverage_set_guard(0, "aster", False, 7) is False

    def test_bybit_path_unchanged(self):
        assert main._leverage_set_guard(0, "bybit", True, 7) is True
        assert main._leverage_set_guard(0, "bybit", False, 7) is True

    def test_sodex_path_unchanged(self):
        assert main._leverage_set_guard(12, "sodex", True, 7) is True
        assert main._leverage_set_guard(12, "sodex", False, 7) is True
        assert main._leverage_set_guard(0, "sodex", True, 7) is False

    def test_no_account_id_never_calls(self):
        assert main._leverage_set_guard(0, "aster", True, 0) is False
        assert main._leverage_set_guard(12, "sodex", True, 0) is False


class TestAsterEntryLeverageSet:
    def test_real_exchange_call_returns_actual(self):
        v = _FakeVenue("aster", result=5)
        actual, failed = asyncio.run(
            main._aster_entry_leverage_set(v, "SEI-USD", 0, 5, 7))
        assert (actual, failed) == (5, False)
        v.update_leverage.assert_awaited_once_with("SEI-USD", 0, 5, 7)

    def test_exchange_rejection_fails_without_raising(self):
        # leverage-set failure must NOT abort the entry — flagged, not raised
        v = _FakeVenue("aster", raises=True)
        actual, failed = asyncio.run(
            main._aster_entry_leverage_set(v, "SEI-USD", 0, 5, 7))
        assert (actual, failed) == (0, True)

    def test_fallback_chain_exhausted_fails_without_raising(self):
        v = _FakeVenue("aster", result=0)
        actual, failed = asyncio.run(
            main._aster_entry_leverage_set(v, "SEI-USD", 0, 5, 7))
        assert (actual, failed) == (0, True)

    def test_fallback_leverage_passes_through(self):
        v = _FakeVenue("aster", result=3)
        actual, failed = asyncio.run(
            main._aster_entry_leverage_set(v, "SEI-USD", 0, 5, 7))
        assert (actual, failed) == (3, False)


class TestAsterLeverageCacheSeed:
    def test_seeds_open_position_symbol_and_flags_mismatch(self):
        cache = {}
        positions = [{"symbol": "SEI-USD", "leverage": "20"},
                     {"symbol": "BTC-USD", "leverage": "5"}]
        seeded, mismatches = main._aster_leverage_cache_seed(
            cache, positions, ["SEI-USD"], lambda s: 5)
        assert seeded == 1
        assert cache == {"SEI-USD": 20}
        assert mismatches == [("SEI-USD", 20, 5)]

    def test_match_is_not_a_mismatch(self):
        cache = {}
        seeded, mismatches = main._aster_leverage_cache_seed(
            cache, [{"symbol": "SEI-USD", "leverage": 5}], ["SEI-USD"],
            lambda s: 5)
        assert seeded == 1 and mismatches == []

    def test_degenerate_rows_skipped(self):
        cache = {}
        seeded, mismatches = main._aster_leverage_cache_seed(
            cache,
            [{"symbol": "SEI-USD", "leverage": 0},
             {"symbol": "X-USD", "leverage": "bad"},
             {"coin": "Y-USD", "leverage": None}],
            ["SEI-USD", "X-USD", "Y-USD"], lambda s: 5)
        assert seeded == 0 and cache == {} and mismatches == []

    def test_wiring_event_names_present(self):
        import inspect
        src = inspect.getsource(main)
        assert "leverage_cache_seeded" in src
        assert "leverage_cache_unseeded" in src
        assert "leverage_mismatch" in src
        assert "leverage_set_failed" in src
