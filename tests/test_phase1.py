import unittest
import time
from data.orderbook_store import OrderbookStore
from data.mark_price_store import MarkPriceStore
from data.candle_buffer import CandleBuffer, Candle
from data.trade_flow_store import TradeFlowStore, Trade

class TestOrderbookStore(unittest.TestCase):

    def test_update_and_retrieve(self):
        store = OrderbookStore("BTC")
        bids = [(70000.0, 1.5), (69999.0, 2.0)]
        asks = [(70001.0, 1.0), (70002.0, 3.0)]
        store.update(bids, asks, 1000)
        self.assertEqual(store.bids, bids)
        self.assertEqual(store.asks, asks)

    def test_top_of_book(self):
        store = OrderbookStore("BTC")
        store.update(
            [(70000.0, 1.0)],
            [(70001.0, 1.0)],
            1000
        )
        best_bid, best_ask, spread = store.top_of_book()
        self.assertEqual(best_bid, 70000.0)
        self.assertEqual(best_ask, 70001.0)
        self.assertAlmostEqual(spread, 1.0)

    def test_imbalance_bid_dominant(self):
        store = OrderbookStore("BTC")
        # 5 levels of bids=10.0, asks=1.0
        bids = [(70000.0 - i, 10.0) for i in range(5)]
        asks = [(70001.0 + i, 1.0) for i in range(5)]
        store.update(bids, asks, 1000)
        imb = store.imbalance(depth=5)
        # 50 total bid vol, 5 total ask vol. Imbalance = (50-5)/(50+5) = 45/55 = 0.818
        self.assertGreater(imb, 0.5)

    def test_staleness_gate(self):
        store = OrderbookStore("BTC")
        store.update([], [], int(time.time() * 1000) - 1000)
        self.assertFalse(store.is_healthy(max_age_ms=500))

class TestMarkPriceStore(unittest.TestCase):

    def test_divergence_calculation(self):
        store = MarkPriceStore("BTC")
        # last_price=70000, mark_price=70100. Divergence = (70100-70000)/70000 = 100/70000 = 0.001428... approx 0.143%
        store.update(70100.0, 70000.0, int(time.time() * 1000))
        data = store.get()
        self.assertAlmostEqual(data["divergence_pct"], 0.143, places=3)

    def test_is_diverging(self):
        store = MarkPriceStore("BTC")
        store.update(70100.0, 70000.0, int(time.time() * 1000))
        self.assertTrue(store.is_diverging(0.10))
        self.assertFalse(store.is_diverging(0.20))

class TestCandleBuffer(unittest.TestCase):

    def test_add_and_retrieve(self):
        buf = CandleBuffer("BTC", "1m", 200)
        candle = Candle(
            open_time=1000,
            open=70000, high=70100,
            low=69900, close=70050,
            volume=100, close_time=1000 + 59999
        )
        buf.add(candle)
        self.assertEqual(buf.count(), 1)
        self.assertEqual(buf.latest(1)[0].close, 70050)

    def test_is_ready(self):
        buf = CandleBuffer("BTC", "1m", 200)
        self.assertFalse(buf.is_ready(20))
        for i in range(20):
            buf.add(Candle(
                open_time=i*60000,
                open=100, high=101, low=99,
                close=100, volume=10,
                close_time=i*60000 + 59999
            ))
        self.assertTrue(buf.is_ready(20))

class TestTradeFlowStore(unittest.TestCase):

    def test_buy_sell_delta(self):
        store = TradeFlowStore("BTC")
        now = int(time.time() * 1000)
        # Add 10 BTC buy
        store.add(Trade(
            timestamp_ms=now,
            price=70000,
            size=10.0,
            side="buy",
            is_aggressor_buy=True
        ))
        # Add 4 BTC sell
        store.add(Trade(
            timestamp_ms=now + 10,
            price=70050,
            size=4.0,
            side="sell",
            is_aggressor_buy=False
        ))
        self.assertEqual(store.delta(60000), 6.0)
        self.assertAlmostEqual(store.aggressor_ratio(60000), 0.714, places=3)

if __name__ == "__main__":
    unittest.main()
