"""Wiring pins for the Arcus plane (main.py construction + venue partition +
band-clock splice + Fix C mark-store injection + config subaccount map).

Every pin keys on the inert-default contract: arcus_enabled=False /
arcus_feed_enabled=False reproduces the pre-module system bit-for-bit.
"""
import asyncio
import importlib
import unittest

import execution.venue as venue
from core.config import Settings
from data.arcus_feed import ArcusFeed
from intelligence.arcus_scalper import FlowWindows, ScalpRegistry
from intelligence.band_clock import BandClock
from intelligence.mfe_guardian import MfeTracker


class _FakeExecutor:
    def __init__(self):
        self.calls = []

    async def update_leverage_with_fallback(self, *a, **kw):
        self.calls.append((a, kw))
        return 5


class _FakeArcusExecutor:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    async def update_leverage(self, symbol="", leverage=0, **kw):
        self.calls.append({"symbol": symbol, "leverage": leverage})
        return self.ok


class _FakeMarkStore:
    def __init__(self):
        self.calls = []

    def update(self, mark_price, last_price, timestamp_ms):
        self.calls.append((mark_price, last_price, timestamp_ms))


def _fresh_venue():
    """Reload venue.py so module-level routing state is clean per test."""
    return importlib.reload(venue)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestVenuePartition(unittest.TestCase):
    def setUp(self):
        self.v = _fresh_venue()

    def tearDown(self):
        _fresh_venue()

    def test_disabled_bit_for_bit(self):
        """Kill-switch False: no arcus executor registered → every dispatch
        resolves exactly as the pre-module system."""
        sodex, aster = _FakeExecutor(), _FakeExecutor()
        self.v.register_executor("sodex", sodex)
        self.v.register_executor("aster", aster)
        sample = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "TAO-USD"]
        self.v.assign_symbols(["TAO-USD", "XRP-USD"], "aster")
        resolved = {s: self.v.venue_for(s) for s in sample}
        self.assertEqual(resolved, {"BTC-USD": "sodex", "ETH-USD": "sodex",
                                    "SOL-USD": "sodex", "XRP-USD": "aster",
                                    "TAO-USD": "aster"})
        self.assertNotIn("arcus", self.v.registered_venues())
        # NET-NEW guard with nothing assigned to arcus: aster-assigned
        # symbols are correctly withheld, every default-venue symbol passes
        # through — routing itself (asserted above) is untouched.
        self.assertEqual(self.v.arcus_route_candidates(sample),
                         ["BTC-USD", "ETH-USD", "SOL-USD"])
        # Leverage bridge unchanged for sodex + aster.
        self.assertEqual(_run(self.v.update_leverage("BTC-USD", 1, 8, 42)), 5)
        self.assertEqual(sodex.calls[-1][0], (1, 8, 42))      # positional
        self.assertEqual(_run(self.v.update_leverage("TAO-USD", 0, 8, 42)), 5)
        self.assertEqual(aster.calls[-1][1], {"symbol": "TAO-USD",
                                              "leverage": 8})  # kwarg

    def test_arcus_assets_route_to_arcus_when_enabled(self):
        sodex, arcus = _FakeExecutor(), _FakeArcusExecutor()
        self.v.register_executor("sodex", sodex)
        self.v.register_executor("arcus", arcus)
        route = self.v.arcus_route_candidates(["SPY-USD", "TSLA-USD"])
        self.v.assign_symbols(route, "arcus")
        self.assertEqual(self.v.venue_for("SPY-USD"), "arcus")
        self.assertEqual(self.v.venue_for("TSLA-USD"), "arcus")
        self.assertEqual(self.v.symbols_for("arcus"), ["SPY-USD", "TSLA-USD"])
        # Pre-existing partition untouched.
        self.assertEqual(self.v.venue_for("BTC-USD"), "sodex")
        self.assertIs(self.v.executor_for("SPY-USD"), arcus)

    def test_net_new_guard_never_migrates_assigned_symbols(self):
        """SoPoints doctrine: a symbol already on aster is never re-routed."""
        aster = _FakeExecutor()
        self.v.register_executor("aster", aster)
        self.v.assign_symbols(["TAO-USD"], "aster")
        cands = self.v.arcus_route_candidates(["TAO-USD", "SPY-USD"])
        self.assertEqual(cands, ["SPY-USD"])

    def test_update_leverage_arcus_branch(self):
        arcus = _FakeArcusExecutor(ok=True)
        self.v.register_executor("sodex", _FakeExecutor())
        self.v.register_executor("arcus", arcus)
        self.v.assign_symbols(["SPY-USD"], "arcus")
        out = _run(self.v.update_leverage("SPY-USD", 0, 12, 42))
        self.assertEqual(out, 12)
        self.assertEqual(arcus.calls[-1], {"symbol": "SPY-USD",
                                           "leverage": 12})
        # Fail-closed: venue refused → 0, never a phantom confirmation.
        arcus.ok = False
        self.assertEqual(_run(self.v.update_leverage("SPY-USD", 0, 12, 42)),
                         0)


class TestBandClockSplice(unittest.TestCase):
    """Mirrors main.py's _on_arcus_band_entry: feed listener →
    band_clock.ingest_entry; telemetry record exactly on streaming events."""

    def _wire(self):
        feed = ArcusFeed()
        clock = BandClock()
        telemetry = []

        def _on_arcus_band_entry(entry, is_snapshot):
            rec = clock.ingest_entry(entry, is_snapshot)
            if rec is not None:
                telemetry.append(rec)

        feed.add_band_event_listener(_on_arcus_band_entry)
        return feed, clock, telemetry

    _ENTRY = {"marketId": 65, "marketDisplayName": "SPY-USD",
              "isOutsideRth": True, "currentSettlementPrice": "672.11",
              "offHoursInitialMarginFraction": "0.05",
              "upperTradingBound": "675.00", "lowerTradingBound": "669.00",
              "boundEvent": None, "boundSide": None, "timestamp": 1}

    def test_snapshot_ingests_silently(self):
        feed, clock, telemetry = self._wire()
        feed._handle_market_attrs(
            {"isSnapshot": True, "entries": [dict(self._ENTRY)]}, True)
        self.assertIsNotNone(clock.entry("SPY-USD"))
        self.assertEqual(telemetry, [])               # nothing on snapshots
        self.assertEqual(clock.recent_events(), [])

    def test_streaming_event_lands_and_telemeters(self):
        feed, clock, telemetry = self._wire()
        feed._handle_market_attrs(
            {"isSnapshot": True, "entries": [dict(self._ENTRY)]}, True)
        upd = dict(self._ENTRY, boundEvent="tradingBoundExpanded",
                   boundSide="upper", timestamp=2)
        feed._handle_market_attrs({"isSnapshot": False, "entries": [upd]},
                                  False)
        self.assertEqual(len(telemetry), 1)
        rec = telemetry[0]
        self.assertEqual(rec["event"], "tradingBoundExpanded")
        self.assertEqual(rec["side"], "upper")
        self.assertEqual(rec["market"], "SPY-USD")
        self.assertIn("rung", rec)                    # rung field present
        self.assertEqual(clock.recent_events("SPY-USD"), [rec])
        # listener exceptions must never propagate into the feed handler
        feed2 = ArcusFeed()
        feed2.add_band_event_listener(
            lambda e, s: (_ for _ in ()).throw(RuntimeError("boom")))
        feed2._handle_market_attrs(
            {"isSnapshot": False, "entries": [upd]}, False)


class TestMarkStoreInjection(unittest.TestCase):
    """Fix C pin: every arcus-routed symbol gets a mark-store writer at
    construction, and the BBO mid writes through it end-to-end."""

    def test_every_routed_symbol_has_store_and_writes_through(self):
        arcus_symbols = ["SPY-USD", "TSLA-USD", "BTC-USD"]
        mark_price_stores = {s: _FakeMarkStore() for s in arcus_symbols}
        # The exact main.py comprehension.
        injected = {a: mark_price_stores[a] for a in arcus_symbols
                    if a in mark_price_stores}
        self.assertEqual(sorted(injected.keys()), sorted(arcus_symbols))
        feed = ArcusFeed(symbols=arcus_symbols, mark_price_stores=injected)
        feed._handle_bbo("SPY-USD", {
            "bestBid": {"price": "672.00", "size": "10"},
            "bestAsk": {"price": "672.20", "size": "4"},
            "timestamp": 1780410803123456})
        self.assertEqual(len(mark_price_stores["SPY-USD"].calls), 1)
        mark, last, ts_ms = mark_price_stores["SPY-USD"].calls[0]
        self.assertAlmostEqual(mark, 672.10)
        self.assertAlmostEqual(last, 672.10)
        self.assertEqual(ts_ms, 1780410803123)

    def test_missing_store_detected_by_wiring_idiom(self):
        """A routed symbol with no store must surface in the missing list —
        the MUBARAK-class silence the warning exists to catch."""
        arcus_symbols = ["SPY-USD", "GHOST-USD"]
        mark_price_stores = {"SPY-USD": _FakeMarkStore()}
        injected = {a: mark_price_stores[a] for a in arcus_symbols
                    if a in mark_price_stores}
        missing = [a for a in arcus_symbols if a not in mark_price_stores]
        self.assertEqual(missing, ["GHOST-USD"])
        self.assertEqual(sorted(injected.keys()), ["SPY-USD"])

    def test_signal_event_shared_object_is_set_by_trade_flow(self):
        ev = asyncio.Event()
        feed = ArcusFeed(symbols=["BTC-USD"], signal_event=ev)
        feed._handle_trades("BTC-USD", [
            {"price": "94500.0", "size": "0.5", "timestamp": 1}])
        self.assertTrue(ev.is_set())


class TestSubaccountMap(unittest.TestCase):
    def test_empty_default_inert(self):
        s = Settings()
        self.assertEqual(s.arcus_subaccount_map, "")
        self.assertEqual(s.arcus_subaccounts, {})

    def test_parse_four_sleeves(self):
        s = Settings(arcus_subaccount_map="SA0:rwa,SA1:crypto,SA2:carry,"
                                          "SA3:sandbox")
        self.assertEqual(s.arcus_subaccounts,
                         {"rwa": 0, "crypto": 1, "carry": 2, "sandbox": 3})

    def test_malformed_segments_skipped_fail_closed(self):
        s = Settings(arcus_subaccount_map="SA0:rwa,garbage,SAx:bad,"
                                          "SA2:, :nolabel,SA9:carry")
        self.assertEqual(s.arcus_subaccounts, {"rwa": 0, "carry": 9})


if __name__ == "__main__":
    unittest.main()
