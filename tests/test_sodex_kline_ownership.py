"""
tests/test_sodex_kline_ownership.py — SILVER/COPPER SoDEX candle ownership.

2026-08-24: Yahoo futures 1m lags ~10 min structurally → the interpreter's
90s staleness guard vetoed every SILVER/COPPER signal (~60-70/hour, all day).
XAUT/CL solved the same defect with Aster klines (2026-08-18); SILVER/COPPER
have no Aster listing, so SoDEX kline_1m owns their candles. Pins the
ownership matrix so no combination of yield/health can darken the plane or
double-write a buffer.
"""

import os
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


class TestFundingPush(unittest.TestCase):
    """Funding-push axiom (subscribe-don't-poll): the markPrice WS push
    already carries the rate field `r`; the feed delivers it in-process on
    CHANGE only, exception-wrapped, kill-switch gated."""

    def _make_feed(self, symbols, cb=None):
        cfg = MagicMock()
        cfg.assets = list(symbols)
        cfg.aster_kline_assets = []
        cfg.sodex_kline_assets = []
        feed = SoDEXFeed(config=cfg, mark_price_stores={}, orderbook_stores={},
                         candle_buffers={}, trade_flow_stores={},
                         funding_push_callback=cb)
        return feed

    def _mp_msg(self, symbol, rate, event_ms):
        return {"channel": "markPrice", "data": {
            "s": symbol, "p": "100.0", "i": "100.0",
            "r": str(rate), "E": event_ms,
        }}

    def _handle(self, feed, msg):
        import asyncio
        with patch("data.sodex_feed.event_bus"):
            asyncio.run(feed._handle(msg))

    def test_push_fires_on_change_only(self):
        calls = []
        feed = self._make_feed(["BTC-USD"], cb=lambda *a: calls.append(a))
        t0 = 1_757_498_400_000
        self._handle(feed, self._mp_msg("BTC-USD", 0.0001, t0))
        self._handle(feed, self._mp_msg("BTC-USD", 0.0001, t0 + 1000))
        self._handle(feed, self._mp_msg("BTC-USD", 0.0001 + 5e-10, t0 + 2000))
        self._handle(feed, self._mp_msg("BTC-USD", 0.0002, t0 + 3000))

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], ("BTC-USD", 0.0001, t0 / 1000.0))
        self.assertEqual(calls[1][0:2], ("BTC-USD", 0.0002))
        self.assertAlmostEqual(calls[1][2], (t0 + 3000) / 1000.0)

    def test_kill_switch_off_silent(self):
        calls = []
        feed = self._make_feed(["BTC-USD"], cb=lambda *a: calls.append(a))
        t0 = 1_757_498_400_000
        with patch.dict(os.environ, {"FUNDING_PUSH_ENABLED": "false"}):
            self._handle(feed, self._mp_msg("BTC-USD", 0.0001, t0))
            self._handle(feed, self._mp_msg("BTC-USD", 0.0002, t0 + 1000))
        self.assertEqual(calls, [])
        self.assertEqual(feed._funding_pushed, {})

    def test_callback_exception_swallowed_feed_survives(self):
        def boom(*a):
            raise RuntimeError("consumer defect")
        feed = self._make_feed(["BTC-USD"], cb=boom)
        t0 = 1_757_498_400_000
        with patch("data.sodex_feed.logger") as log:
            self._handle(feed, self._mp_msg("BTC-USD", 0.0001, t0))
            warning_events = [c.args[0] for c in log.warning.call_args_list]
        self.assertIn("funding_push_callback_error", warning_events)
        # The change was still recorded — the rate does not re-fire.
        self.assertEqual(feed._funding_pushed["BTC-USD"], 0.0001)

    def test_callback_error_warning_throttled_300s(self):
        def boom(*a):
            raise RuntimeError("x")
        feed = self._make_feed(["BTC-USD"], cb=boom)
        t0 = 1_757_498_400_000
        with patch("data.sodex_feed.logger") as log:
            self._handle(feed, self._mp_msg("BTC-USD", 0.0001, t0))
            self._handle(feed, self._mp_msg("BTC-USD", 0.0002, t0 + 1000))
            warning_events = [c.args[0] for c in log.warning.call_args_list]
        self.assertEqual(warning_events.count("funding_push_callback_error"), 1)

    def test_no_callback_no_crash(self):
        feed = self._make_feed(["BTC-USD"])
        self._handle(feed, self._mp_msg("BTC-USD", 0.0001, 1_757_498_400_000))
        self.assertEqual(feed._funding_pushed, {})

    def test_setter_injection(self):
        feed = self._make_feed(["BTC-USD"])
        calls = []
        feed.set_funding_push_callback(lambda *a: calls.append(a))
        t0 = 1_757_498_400_000
        self._handle(feed, self._mp_msg("BTC-USD", -0.0003, t0))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], -0.0003)


class TestConfigWiring(unittest.TestCase):
    _KLINE_ASSETS = [
        # Re-encoded 2026-10-10 (Governor order: SILVER retired — 30d 0/9,
        # −$39.18, binomial p≈0.02): SILVER struck from the universe and the
        # kline-ownership list; COPPER/CL KEEP per the census verdicts.
        "COPPER-USD",
        "TSM-USD", "ORCL-USD", "NVDA-USD", "MSFT-USD", "AAPL-USD",
        "AMZN-USD", "GOOGL-USD", "META-USD", "TSLA-USD",
        "USTECH100-USD", "SPCX-USD",
        "HOOD-USD", "LITE-USD", "SMCI-USD",
        "SAMSUNG-USD", "SKHX-USD", "UNITREE-USD",
        "COIN-USD", "CRCL-USD",
        # Re-encoded 2026-09-21 (Governor directive: every SoDEX-tradable alt
        # leaves Aster): XAUT/CL migrated back to SoDEX routing; their candles
        # move with them (ex-aster_kline_assets) — same 24/7 perp plane.
        "XAUT-USD", "CL-USD",
        # Re-encoded 2026-09-22 (Governor paste of the SoDEX markets page):
        # AMD/DRAM kline-owned from birth — the same 24/7 perp plane.
        "AMD-USD", "DRAM-USD",
        # Re-encoded 2026-09-26 (Governor directive: register MSTR-USD as a
        # fully tradeable equity perp): kline-owned from birth — same wound
        # class as the 09-11/09-22 adds.
        "MSTR-USD",
    ]

    def test_sodex_kline_assets_registered(self):
        # Re-encoded 2026-09-11 (Governor directive "migrate tradfi to sedex
        # klines immediately"): the 11 equity/index perps join SILVER/COPPER.
        # Overnight census: Yahoo dies at US close -> signal_stale_data all
        # night (ORCL x3,193/7d); the perp's own 24/7 kline is the only
        # honest candle plane for these names.
        # Re-encoded again same session ("add a few more coins"): the 6
        # expansion adds are kline-owned from birth; COIN/CRCL were in-
        # universe but dark (no seed/ownership — identical wound).
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
