"""Unit tests for data/arcus_feed.py — handler-level, no network."""
import asyncio
import unittest

from data.arcus_feed import ArcusFeed


class _FakeMarkStore:
    def __init__(self):
        self.calls = []

    def update(self, mark_price, last_price, timestamp_ms):
        self.calls.append((mark_price, last_price, timestamp_ms))


class _FakeObStore:
    def __init__(self):
        self.bids = []
        self.asks = []
        self.calls = []

    def update_l4_diff(self, bid_diffs, ask_diffs, timestamp_ms):
        self.calls.append((list(bid_diffs), list(ask_diffs), timestamp_ms))
        bid_map = dict(self.bids)
        for p, q in bid_diffs:
            if q > 0:
                bid_map[p] = q
            else:
                bid_map.pop(p, None)
        ask_map = dict(self.asks)
        for p, q in ask_diffs:
            if q > 0:
                ask_map[p] = q
            else:
                ask_map.pop(p, None)
        self.bids = sorted(bid_map.items(), key=lambda x: -x[0])
        self.asks = sorted(ask_map.items())


def _feed(**kw):
    return ArcusFeed(**kw)


class TestSubscribeFrames(unittest.TestCase):
    def test_frames_cover_channels(self):
        f = _feed(symbols=["BTC-USD", "SPY-USD"],
                  orderbook_stores={"BTC-USD": _FakeObStore()},
                  ob_symbols=["BTC-USD"])
        frames = f._subscribe_frames()
        chans = [(fr["channel"], fr.get("id")) for fr in frames]
        self.assertIn(("marketAttributes", None), chans)
        self.assertIn(("bbo", "BTC-USD"), chans)
        self.assertIn(("trades", "BTC-USD"), chans)
        self.assertIn(("predictedFunding", "BTC-USD"), chans)
        self.assertIn(("l2OrderbookUpdates", "BTC-USD"), chans)
        self.assertIn(("bbo", "SPY-USD"), chans)
        # BTC is in symbols already — no duplicate bbo frame for the ob leg
        self.assertEqual(chans.count(("bbo", "BTC-USD")), 1)

    def test_ob_only_symbol_gets_bbo(self):
        f = _feed(orderbook_stores={"ETH-USD": _FakeObStore()},
                  ob_symbols=["ETH-USD"])
        chans = [(fr["channel"], fr.get("id")) for fr in
                 f._subscribe_frames()]
        self.assertIn(("bbo", "ETH-USD"), chans)
        self.assertIn(("l2OrderbookUpdates", "ETH-USD"), chans)


class TestBbo(unittest.TestCase):
    def test_mid_writes_mark_store(self):
        ms = _FakeMarkStore()
        f = _feed(symbols=["BTC-USD"],
                  mark_price_stores={"BTC-USD": ms})
        f._handle_bbo("BTC-USD", {
            "bestBid": {"price": "94500.00", "size": "1.5"},
            "bestAsk": {"price": "94501.00", "size": "0.8"},
            "timestamp": 1780410803123456})
        self.assertEqual(len(ms.calls), 1)
        mark, last, ts_ms = ms.calls[0]
        self.assertAlmostEqual(mark, 94500.5)
        self.assertEqual(ts_ms, 1780410803123)
        self.assertAlmostEqual(f.get_mark_price("BTC-USD"), 94500.5)

    def test_null_side_ignored(self):
        ms = _FakeMarkStore()
        f = _feed(symbols=["BTC-USD"], mark_price_stores={"BTC-USD": ms})
        f._handle_bbo("BTC-USD", {"bestBid": None,
                                  "bestAsk": {"price": "1", "size": "1"},
                                  "timestamp": 1})
        self.assertEqual(ms.calls, [])
        self.assertEqual(f.get_mark_price("BTC-USD"), 0.0)


class TestTrades(unittest.TestCase):
    def test_direction_inferred_and_signal_event_set(self):
        f = _feed(symbols=["BTC-USD"])
        f._handle_bbo("BTC-USD", {
            "bestBid": {"price": "100.0", "size": "1"},
            "bestAsk": {"price": "101.0", "size": "1"},
            "timestamp": 1})
        f.book["BTC-USD"]["ts"] = asyncio.get_event_loop().time() and \
            __import__("time").time()
        ev = asyncio.Event()
        f._signal_event = ev
        seen = []
        f.add_trade_listener(lambda *a: seen.append(a))
        f._handle_trades("BTC-USD", [
            {"price": "101.5", "size": "0.5", "timestamp": 1712345679000000},
            {"price": "99.5", "size": "0.25", "timestamp": 1712345679000001},
            {"price": "100.5", "size": "0.1", "timestamp": 1712345679000002},
        ])
        self.assertTrue(ev.is_set())
        self.assertEqual([s[4] for s in seen], [True, False, None])
        self.assertAlmostEqual(seen[0][1], 101.5)
        self.assertAlmostEqual(seen[0][2], 0.5)

    def test_stale_book_direction_none(self):
        f = _feed(symbols=["BTC-USD"])
        f._handle_bbo("BTC-USD", {
            "bestBid": {"price": "100.0", "size": "1"},
            "bestAsk": {"price": "101.0", "size": "1"},
            "timestamp": 1})
        f.book["BTC-USD"]["ts"] = 0.0
        seen = []
        f.add_trade_listener(lambda *a: seen.append(a))
        f._handle_trades("BTC-USD", [
            {"price": "101.5", "size": "0.5", "timestamp": 1}])
        self.assertIsNone(seen[0][4])

    def test_bad_fills_skipped(self):
        f = _feed(symbols=["BTC-USD"])
        seen = []
        f.add_trade_listener(lambda *a: seen.append(a))
        f._handle_trades("BTC-USD", [{"price": "0", "size": "1"},
                                     {"price": "x"}, "garbage", {}])
        self.assertEqual(seen, [])


class TestPredictedFunding(unittest.TestCase):
    def test_rate_tracked(self):
        f = _feed(symbols=["BTC-USD"])
        f._handle_predicted_funding({"market": "BTC-USD",
                                     "rate1h": "0.0000125"})
        self.assertAlmostEqual(f.predicted_funding["BTC-USD"], 0.0000125)
        f._handle_predicted_funding({"market": "BTC-USD",
                                     "rate1h": "0.0000131"})
        self.assertAlmostEqual(f.predicted_funding["BTC-USD"], 0.0000131)


class TestMarketAttributes(unittest.TestCase):
    def test_snapshot_replace_and_listener(self):
        f = _feed()
        seen = []
        f.add_band_event_listener(lambda e, snap: seen.append((e, snap)))
        entry = {"marketId": 65, "marketDisplayName": "SPY-USD",
                 "isOutsideRth": True, "currentSettlementPrice": "672.11",
                 "upperTradingBound": "675.00", "lowerTradingBound": "669.00",
                 "boundEvent": None, "boundSide": None}
        f._handle_market_attrs({"isSnapshot": True, "entries": [entry]}, True)
        self.assertEqual(f.market_attrs[65]["upperTradingBound"], "675.00")
        self.assertEqual(seen[0][1], True)
        # streaming update replaces wholesale (docs: null never omitted)
        upd = dict(entry, upperTradingBound="678.00",
                   boundEvent="tradingBoundExpanded", boundSide="upper")
        f._handle_market_attrs({"isSnapshot": False, "entries": [upd]}, False)
        self.assertEqual(f.market_attrs[65]["upperTradingBound"], "678.00")
        self.assertEqual(seen[1][1], False)
        self.assertEqual(seen[1][0]["boundEvent"], "tradingBoundExpanded")
        self.assertEqual(f.get_band_state("SPY-USD")["boundSide"], "upper")
        self.assertIsNone(f.get_band_state("DOGE-USD"))


class TestOrderbook(unittest.TestCase):
    def test_snapshot_seeds_then_diffs_apply(self):
        st = _FakeObStore()
        f = _feed(orderbook_stores={"BTC-USD": st}, ob_symbols=["BTC-USD"])
        f._handle_ob_snapshot("BTC-USD", {
            "bids": [["94499.0", "1.0"], ["94498.0", "2.0"]],
            "asks": [["94503.0", "1.2"]],
            "lastSequenceId": 1040, "timestamp": 1712345679000000})
        self.assertEqual(st.bids[0], (94499.0, 1.0))
        self.assertTrue(f._ob_seeded["BTC-USD"])
        # boundary gap after snapshot is expected — no resubscribe
        f._handle_ob_delta("BTC-USD", {"bids": [["94499.0", "0"]],
                                       "asks": [["94503.0", "1.5"]],
                                       "lastSequenceId": 1043})
        self.assertTrue(f._ob_seeded["BTC-USD"])
        self.assertEqual(st.bids, [(94498.0, 2.0)])   # size "0" removed
        self.assertEqual(st.asks, [(94503.0, 1.5)])

    def test_mid_stream_gap_triggers_resubscribe(self):
        st = _FakeObStore()
        f = _feed(orderbook_stores={"BTC-USD": st}, ob_symbols=["BTC-USD"])
        f._handle_ob_snapshot("BTC-USD", {
            "bids": [["100.0", "1.0"]], "asks": [["101.0", "1.0"]],
            "lastSequenceId": 10})
        f._handle_ob_delta("BTC-USD", {"bids": [["100.0", "2.0"]],
                                       "asks": [], "lastSequenceId": 11})
        self.assertEqual(st.bids, [(100.0, 2.0)])      # contiguous applied
        f._handle_ob_delta("BTC-USD", {"bids": [["100.0", "3.0"]],
                                       "asks": [], "lastSequenceId": 15})
        self.assertFalse(f._ob_seeded["BTC-USD"])      # gap → unseeded
        self.assertEqual(st.bids, [(100.0, 2.0)])      # gap delta NOT applied
        f._handle_ob_delta("BTC-USD", {"bids": [["100.0", "9.0"]],
                                       "asks": [], "lastSequenceId": 16})
        self.assertEqual(st.bids, [(100.0, 2.0)])      # ignored until snapshot

    def test_delta_before_snapshot_ignored(self):
        st = _FakeObStore()
        f = _feed(orderbook_stores={"BTC-USD": st}, ob_symbols=["BTC-USD"])
        f._handle_ob_delta("BTC-USD", {"bids": [["100.0", "1.0"]],
                                       "asks": [], "lastSequenceId": 1})
        self.assertEqual(st.calls, [])


class TestHealth(unittest.TestCase):
    def test_health_shape(self):
        f = _feed(symbols=["BTC-USD"])
        h = f.health_check()
        self.assertEqual(h["feed"], "arcus_public")
        self.assertEqual(h["status"], "stopped")
        self.assertEqual(h["symbols"], 1)


if __name__ == "__main__":
    unittest.main()
