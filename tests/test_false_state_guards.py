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
from execution.sodex_client import parse_wallet_balance
from memory.adaptive_calibrator import AdaptiveCalibrator
from risk.drawdown_manager import DrawdownManager


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


class TestOpenBookWithdrawalDetection(unittest.TestCase):
    """2026-08-19: the flat-book-only withdrawal guard turned a real operator
    withdrawal (book open) into a phantom 3.63% DD → 28h recovery crouch.
    The open-book detector keys on wallet balance (wb — no uPnL/MAM) plus a
    close-event counter, and must be fail-closed in both directions."""

    def test_withdrawal_no_closes_flags(self):
        assert DrawdownManager.classify_external_flow(-21.5, 0) == "withdrawal"

    def test_deposit_no_closes_flags(self):
        assert DrawdownManager.classify_external_flow(50.0, 0) == "deposit"

    def test_close_in_window_disqualifies(self):
        # A realized loss with an open book must NEVER read as a withdrawal —
        # that would shift anchors down and erase real drawdown protection.
        assert DrawdownManager.classify_external_flow(-25.0, 1) is None
        assert DrawdownManager.classify_external_flow(40.0, 2) is None

    def test_funding_sized_noise_ignored(self):
        assert DrawdownManager.classify_external_flow(-1.99, 0) is None
        assert DrawdownManager.classify_external_flow(1.5, 0) is None

    def test_threshold_boundary(self):
        assert DrawdownManager.classify_external_flow(-2.0, 0) is None
        assert DrawdownManager.classify_external_flow(-2.01, 0) == "withdrawal"


class TestParseWalletBalance(unittest.TestCase):
    def test_sums_wb_across_entries(self):
        payload = {"code": 0, "data": {"balances": [
            {"wb": "500.25", "av": "510.0"},
            {"wb": "77.49"},
            {"av": "9.0"},          # no wb — skipped
        ]}}
        assert abs(parse_wallet_balance(payload) - 577.74) < 1e-9

    def test_rejects_bad_code(self):
        assert parse_wallet_balance({"code": 1, "data": {"balances": [{"wb": "100"}]}}) == 0.0

    def test_rejects_malformed(self):
        assert parse_wallet_balance({}) == 0.0
        assert parse_wallet_balance({"code": 0, "data": {"balances": [{"wb": "abc"}]}}) == 0.0
        assert parse_wallet_balance({"code": 0, "data": {"balances": "oops"}}) == 0.0
        assert parse_wallet_balance({"code": 0, "data": {"balances": [None, {"wb": "5"}]}}) == 5.0


if __name__ == "__main__":
    unittest.main()
