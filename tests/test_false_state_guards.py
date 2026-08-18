"""False-state guards (2026-08-18): a failed venue poll must never read as
"zero balance" or "all positions closed" downstream, and DD-triggered
recovery must exit when the drawdown clears.

Root causes fixed:
- venue.all_positions/venue_balances swallowed exceptions → phantom
  exchange_close PnL + phantom 67% DD trough (12h book freeze).
- AdaptiveCalibrator recovery had no DD-cleared exit — a structural
  deadlock (recovery_mode_exited count was 0 in log history).
"""
import asyncio
import unittest

from execution import venue
from memory.adaptive_calibrator import AdaptiveCalibrator


class _ExecOK:
    def __init__(self, positions=None, balance=100.0):
        self._positions = positions or []
        self._balance = balance

    async def get_positions(self, address=""):
        return list(self._positions)

    async def get_account_balance(self, address=""):
        return self._balance


class _ExecDown:
    async def get_positions(self, address=""):
        raise ConnectionError("Cloudflare HTML")

    async def get_account_balance(self, address=""):
        raise ConnectionError("ConnectionTerminated")


class TestVenueFailureTracking(unittest.TestCase):
    def setUp(self):
        venue._executors.clear()
        venue._venue_by_symbol.clear()
        venue._positions_failures.clear()
        venue._balance_failures.clear()

    def tearDown(self):
        venue._executors.clear()
        venue._venue_by_symbol.clear()
        venue._positions_failures.clear()
        venue._balance_failures.clear()

    def test_all_positions_merges_good_venue_and_flags_failed(self):
        venue.register_executor("sodex", _ExecDown())
        venue.register_executor("aster", _ExecOK(positions=[{"symbol": "UNI-USD"}]))
        venue.assign_symbols(["UNI-USD"], "aster")
        merged = asyncio.run(venue.all_positions("addr"))
        assert merged == [{"symbol": "UNI-USD"}]
        assert venue.positions_failed_venues() == frozenset({"sodex"})

    def test_positions_failure_clears_on_recovery(self):
        venue.register_executor("sodex", _ExecDown())
        asyncio.run(venue.all_positions("addr"))
        assert "sodex" in venue.positions_failed_venues()
        venue.register_executor("sodex", _ExecOK(positions=[{"symbol": "BTC-USD"}]))
        merged = asyncio.run(venue.all_positions("addr"))
        assert merged == [{"symbol": "BTC-USD"}]
        assert venue.positions_failed_venues() == frozenset()

    def test_venue_balances_flags_failed_leg(self):
        venue.register_executor("sodex", _ExecDown())
        venue.register_executor("aster", _ExecOK(balance=203.0))
        venue.assign_symbols(["UNI-USD"], "aster")
        out = asyncio.run(venue.venue_balances("addr"))
        assert out["aster"] == 203.0 and out["sodex"] == 0.0
        assert venue.balance_failed_venues() == frozenset({"sodex"})

    def test_balance_failure_clears_on_recovery(self):
        venue.register_executor("sodex", _ExecDown())
        asyncio.run(venue.venue_balances("addr"))
        assert "sodex" in venue.balance_failed_venues()
        venue.register_executor("sodex", _ExecOK(balance=400.0))
        out = asyncio.run(venue.venue_balances("addr"))
        assert out["sodex"] == 400.0
        assert venue.balance_failed_venues() == frozenset()


class _Cfg:
    pass


class TestRecoveryDrawdownExit(unittest.TestCase):
    def test_dd_recovery_exits_when_dd_clears(self):
        cal = AdaptiveCalibrator(_Cfg())
        cal.update_drawdown(0.05)
        assert cal.is_in_recovery()
        cal.update_drawdown(0.02)     # below trigger, above exit — hysteresis holds
        assert cal.is_in_recovery()
        cal.update_drawdown(0.01)     # below exit band — DD over, recovery over
        assert not cal.is_in_recovery()

    def test_dd_recovery_hysteresis_no_flap(self):
        cal = AdaptiveCalibrator(_Cfg())
        cal.update_drawdown(0.05)
        cal.update_drawdown(0.014)    # exits
        assert not cal.is_in_recovery()
        cal.update_drawdown(0.02)     # below 3% trigger — must NOT re-activate
        assert not cal.is_in_recovery()
        cal.update_drawdown(0.031)    # above trigger — re-activates
        assert cal.is_in_recovery()

    def test_winrate_recovery_not_exited_by_dd_clear(self):
        cal = AdaptiveCalibrator(_Cfg())
        cal._recovery.activate("win_rate")
        cal.update_drawdown(0.0)
        assert cal.is_in_recovery()   # trade-based exits only


if __name__ == "__main__":
    unittest.main()
