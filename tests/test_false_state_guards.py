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


class TestNettedExternalFlow(unittest.TestCase):
    """2026-08-27: the any-close veto permanently missed withdrawals sharing a
    poll window with any close (the wb anchor advanced every poll regardless).
    Netting the exact realized pnl isolates the external leg; a pure-loss
    window nets to ~0 and stays fail-closed."""

    def test_withdrawal_hidden_behind_close_is_caught(self):
        # wb −53 with a −3 realized close in window: old code vetoed (None,
        # forever); netted sees the −50 external leg
        assert DrawdownManager.classify_external_flow_netted(-53.0, -3.0) == "withdrawal"

    def test_pure_loss_window_classifies_as_nothing(self):
        # −3 wb move fully explained by the −3 close — NOT a withdrawal
        assert DrawdownManager.classify_external_flow_netted(-3.0, -3.0) is None

    def test_deposit_behind_close_is_caught(self):
        assert DrawdownManager.classify_external_flow_netted(47.0, -3.0) == "deposit"

    def test_win_plus_small_withdrawal_nets_correctly(self):
        # +12 win, −15 withdrawal → wb −3; netted: −3 − 12 = −15 → withdrawal
        assert DrawdownManager.classify_external_flow_netted(-3.0, 12.0) == "withdrawal"

    def test_residual_under_threshold_ignored(self):
        assert DrawdownManager.classify_external_flow_netted(-2.5, -1.0) is None
        assert DrawdownManager.classify_external_flow(-2.01, 0) == "withdrawal"


class TestCrossplaneFlowAgreement(unittest.TestCase):
    """2026-09-21 (deposit-arm-upnl-guard-0921): open-book external-flow arms
    must only stand when the EQUITY plane agrees with the realized-netted wb
    delta. Phantom classes (realized-posting race, manual closes on untracked
    positions, MAM/internal-transfer endpoint glitches) move wb alone.
    Fail-closed: disagreement -> veto, anchors untouched.
    Kill switch OPENBOOK_FLOW_CROSSCHECK_ENABLED defaults to "true" (unset =
    guard ACTIVE); false = legacy pre-guard behavior bit-for-bit."""

    def test_real_deposit_both_planes_agrees(self):
        # +200 wb_net, +205 equity_net: a real inflow moves both planes
        assert DrawdownManager.crossplane_flow_agrees(200.0, 205.0) is True

    def test_phantom_glitch_wb_only_vetoed(self):
        # The 05:31 episode: wb +200 while the equity plane was flat (749.87)
        assert DrawdownManager.crossplane_flow_agrees(200.0, 0.0) is False

    def test_posting_race_vetoed(self):
        # 09:14 episode: close posted +3.78 to wb exchange-side before
        # _record_close netted it; equity plane barely moved in the window
        assert DrawdownManager.crossplane_flow_agrees(3.78, 0.10) is False

    def test_posting_race_mirror_vetoed(self):
        # 09:13 mirror: -3.75 wb swing, equity -0.05
        assert DrawdownManager.crossplane_flow_agrees(-3.75, -0.05) is False

    def test_real_withdrawal_agrees(self):
        assert DrawdownManager.crossplane_flow_agrees(-50.0, -48.0) is True

    def test_tolerance_boundary_abs(self):
        # diff 2.0 == tol_abs -> agree; diff 3.1 > max(2.0, 2.5)=2.5 -> veto
        assert DrawdownManager.crossplane_flow_agrees(10.0, 8.0) is True
        assert DrawdownManager.crossplane_flow_agrees(10.0, 6.9) is False

    def test_tolerance_boundary_frac(self):
        # tol_frac*|wb_net| = 25: diff 24 <= 25 -> agree; diff 24.1 > 25? no:
        # 100-75.9 = 24.1 <= 25 -> agree; use 74.9 -> diff 25.1 > 25 -> veto
        assert DrawdownManager.crossplane_flow_agrees(100.0, 76.0) is True
        assert DrawdownManager.crossplane_flow_agrees(100.0, 74.9) is False

    def test_zero_wb_net_small_equity_noise_agrees(self):
        # wb_net 0: abs diff = |equity_net|; covered by tol_abs when <= 2
        assert DrawdownManager.crossplane_flow_agrees(0.0, 1.5) is True

    def test_zero_wb_net_large_equity_swing_vetoed(self):
        # wb flat but equity moved > tol_abs — planes disagree, veto
        assert DrawdownManager.crossplane_flow_agrees(0.0, 5.0) is False


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

    def test_live_total_schema_20260915(self):
        """Verbatim live mainnet shape probed 2026-09-15: entries serve
        `total` (coin quantity), NOT `wb`. The wb-only read returned 0.0
        forever and the open-book withdrawal detector was dead since birth —
        the 06:54Z $20 operator withdrawal booked as phantom -28.6% DD."""
        payload = {"code": 0, "timestamp": 1789458209878, "data": {
            "blockTime": 1789458209747, "blockHeight": 230069990,
            "balances": [
                {"id": 0, "coin": "vUSDC", "total": "58.614370653586892582",
                 "collateral": "0", "marginRatio": "1", "price": "1"},
                {"id": 4, "coin": "WSOSO", "total": "0.0000001451976",
                 "collateral": "0.0000001451976", "marginRatio": "0.5",
                 "price": "0.2941323495596013"},
            ]}}
        assert abs(parse_wallet_balance(payload) - 58.614370653586892) < 1e-9

    def test_total_fallback_excludes_non_usd(self):
        # WSOSO-only wallet (no USD leg): non-USD totals must NOT enter at
        # coin-unit scale — 0.0 = no-data, not a phantom balance.
        payload = {"code": 0, "data": {"balances": [
            {"id": 4, "coin": "WSOSO", "total": "1000.0"}]}}
        assert parse_wallet_balance(payload) == 0.0

    def test_wb_preferred_over_total(self):
        # Legacy semantics bit-for-bit: wb present wins; USD total ignored.
        payload = {"code": 0, "data": {"balances": [
            {"id": 0, "coin": "vUSDC", "wb": "100.0", "total": "999.0"}]}}
        assert parse_wallet_balance(payload) == 100.0

    def test_usd_coin_alias_variants(self):
        for coin in ("USDC", "USD1", "vusdc"):
            payload = {"code": 0, "data": {"balances": [
                {"id": 7, "coin": coin, "total": "42.5"}]}}
            assert parse_wallet_balance(payload) == 42.5, coin

    def test_bad_total_fails_closed(self):
        assert parse_wallet_balance({"code": 0, "data": {"balances": [
            {"id": 0, "coin": "vUSDC", "total": "abc"}]}}) == 0.0


class TestAnchorAlreadyReflectsFlow(unittest.TestCase):
    """2026-09-21 idempotency leg (the true 05:31 +200 mechanism): a REAL
    inflow double-counted because the peak anchor was already pre-booked to
    the post-flow balance (prev_peak 749.87 == post_balance 749.90 -> arm
    shifted to 949.87 -> phantom 21% DD). State-based skip: peak ~= post-flow
    balance within max($1, 0.5%) means the flow is already inside the anchors.
    Narrow: fresh deposits and deep-DD deposits fail the test and adjust as
    before. Same kill switch as the cross-plane leg."""

    def test_episode_a_double_count_skipped(self):
        # prev_peak 749.87 vs post_balance 749.90 -> already reflected
        assert DrawdownManager.anchor_already_reflects_flow(749.87, 749.90) is True

    def test_episode_a_withdrawal_side_skipped(self):
        # symmetric: a hand-booked withdrawal (peak already lowered to balance)
        assert DrawdownManager.anchor_already_reflects_flow(570.13, 570.00) is True

    def test_fresh_deposit_not_skipped(self):
        # prev_peak 568 vs post_balance 749.90: money NOT in the anchor -> adjust
        assert DrawdownManager.anchor_already_reflects_flow(568.0, 749.90) is False

    def test_real_withdrawal_not_skipped(self):
        # the 09-19 -200: prev_peak 770.13 vs post_balance 570.13 -> adjust
        assert DrawdownManager.anchor_already_reflects_flow(770.13, 570.13) is False

    def test_deep_dd_deposit_doctrine_preserved(self):
        # prev_peak 800 vs post_balance 600: gap 200 > 3.0 -> adjust (unchanged)
        assert DrawdownManager.anchor_already_reflects_flow(800.0, 600.0) is False

    def test_zero_inputs_fail_closed(self):
        assert DrawdownManager.anchor_already_reflects_flow(0.0, 100.0) is False
        assert DrawdownManager.anchor_already_reflects_flow(100.0, 0.0) is False


if __name__ == "__main__":
    unittest.main()
