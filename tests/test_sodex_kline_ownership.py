"""
tests/test_sodex_kline_ownership.py — SILVER/COPPER SoDEX candle ownership.

2026-08-24: Yahoo futures 1m lags ~10 min structurally → the interpreter's
90s staleness guard vetoed every SILVER/COPPER signal (~60-70/hour, all day).
XAUT/CL solved the same defect with Aster klines (2026-08-18); SILVER/COPPER
have no Aster listing, so SoDEX kline_1m owns their candles. Pins the
ownership matrix so no combination of yield/health can darken the plane or
double-write a buffer.
"""

import time
import unittest
from unittest.mock import MagicMock, patch

import data.tradfi_feed as tf
from data.candle_buffer import CandleBuffer
from data.sodex_feed import SoDEXFeed, SODEX_SUPPORTED


def _feed_with_healthy(symbols):
    """TradfiFeed whose healthy() reports True for the given symbols."""
    feed = tf.TradfiFeed(candle_buffers={}, mark_price_stores={})
    for s in symbols:
        feed._last_ok[s] = time.time()
    return feed


class TestTradfiOwnsSemantics(unittest.TestCase):
    def setUp(self):
        tf.set_candle_yield([])

    def tearDown(self):
        tf.set_candle_yield([])
        tf._feed = None

    def test_healthy_unyielded_owns(self):
        tf._feed = _feed_with_healthy(["AAPL-USD"])
        self.assertTrue(tf.tradfi_owns("AAPL-USD"))

    def test_healthy_yielded_does_not_own(self):
        # The dark-plane pin: yielded must NOT claim ownership — otherwise
        # sodex_feed yields too and nobody writes candles.
        tf._feed = _feed_with_healthy(["SILVER-USD"])
        tf.set_candle_yield(["SILVER-USD"])
        self.assertFalse(tf.tradfi_owns("SILVER-USD"))
        # ...but health reporting is unaffected (basis guard keeps working)
        self.assertTrue(tf.tradfi_health("SILVER-USD"))

    def test_no_feed_never_owns(self):
        tf._feed = None
        self.assertFalse(tf.tradfi_owns("AAPL-USD"))


class TestSoDEXCandleGate(unittest.TestCase):
    def setUp(self):
        tf.set_candle_yield([])
        tf._feed = None

    def tearDown(self):
        tf.set_candle_yield([])
        tf._feed = None

    def _make_feed(self, symbols):
        cfg = MagicMock()
        cfg.assets = list(symbols)
        cfg.aster_kline_assets = ["XAUT-USD", "CL-USD"]
        cfg.sodex_kline_assets = ["SILVER-USD", "COPPER-USD"]
        bufs = {s: {"1m": CandleBuffer(s, "1m")} for s in symbols}
        feed = SoDEXFeed(config=cfg, mark_price_stores={}, orderbook_stores={},
                         candle_buffers=bufs, trade_flow_stores={})
        return feed, bufs

    def _candle_msg(self, symbol):
        now_ms = int(time.time() * 1000)
        return {"channel": "candle", "data": {
            "s": symbol, "t": now_ms - 60_000, "T": now_ms,
            "o": "69.0", "h": "69.2", "l": "68.9", "c": "69.1",
            "v": "0.05", "x": True,
        }}

    def test_sodex_kline_asset_publishes(self):
        # SILVER is yielded from tradfi and NOT aster-owned → SoDEX writes.
        feed, bufs = self._make_feed(["SILVER-USD"])
        tf._feed = _feed_with_healthy(["SILVER-USD"])
        tf.set_candle_yield(["SILVER-USD"])
        import asyncio
        with patch("data.sodex_feed.event_bus"):
            asyncio.run(
                feed._handle(self._candle_msg("SILVER-USD")))
        self.assertEqual(bufs["SILVER-USD"]["1m"].count(), 1)

    def test_aster_kline_asset_still_yields(self):
        # XAUT is yielded from tradfi AND aster-owned → SoDEX must NOT write
        # (AsterFeed owns the buffer; writing would double-write).
        feed, bufs = self._make_feed(["XAUT-USD"])
        tf._feed = _feed_with_healthy(["XAUT-USD"])
        tf.set_candle_yield(["XAUT-USD"])
        import asyncio
        with patch("data.sodex_feed.event_bus"):
            asyncio.run(
                feed._handle(self._candle_msg("XAUT-USD")))
        self.assertEqual(bufs["XAUT-USD"]["1m"].count(), 0)

    def test_tradfi_written_symbol_still_yields(self):
        # AAPL: tradfi healthy and unyielded → tradfi owns → SoDEX yields.
        feed, bufs = self._make_feed(["AAPL-USD"])
        tf._feed = _feed_with_healthy(["AAPL-USD"])
        import asyncio
        with patch("data.sodex_feed.event_bus"):
            asyncio.run(
                feed._handle(self._candle_msg("AAPL-USD")))
        self.assertEqual(bufs["AAPL-USD"]["1m"].count(), 0)

    def test_tradfi_unhealthy_falls_back_to_sodex(self):
        # Pre-existing fail-operational behavior: tradfi dark → SoDEX writes.
        feed, bufs = self._make_feed(["AAPL-USD"])
        tf._feed = None   # feed down
        import asyncio
        with patch("data.sodex_feed.event_bus"):
            asyncio.run(
                feed._handle(self._candle_msg("AAPL-USD")))
        self.assertEqual(bufs["AAPL-USD"]["1m"].count(), 1)


class TestConfigWiring(unittest.TestCase):
    _KLINE_ASSETS = [
        "SILVER-USD", "COPPER-USD",
        "TSM-USD", "ORCL-USD", "NVDA-USD", "MSFT-USD", "AAPL-USD",
        "AMZN-USD", "GOOGL-USD", "META-USD", "TSLA-USD",
        "USTECH100-USD", "SPCX-USD",
    ]

    def test_sodex_kline_assets_registered(self):
        # Re-encoded 2026-09-11 (Governor directive "migrate tradfi to sedex
        # klines immediately"): the 11 equity/index perps join SILVER/COPPER.
        # Overnight census: Yahoo dies at US close -> signal_stale_data all
        # night (ORCL x3,193/7d); the perp's own 24/7 kline is the only
        # honest candle plane for these names.
        from core.config import Settings
        cfg = Settings()
        self.assertEqual(sorted(cfg.sodex_kline_assets),
                         sorted(self._KLINE_ASSETS))

    def test_sodex_kline_assets_seed_supported(self):
        # fetch_historical seeds only SODEX_SUPPORTED symbols — every owned
        # symbol must be a member or boots cold-start ATR for hours.
        for sym in self._KLINE_ASSETS:
            self.assertIn(sym, SODEX_SUPPORTED)


if __name__ == "__main__":
    unittest.main()
