"""
tests/test_hyperliquid_feed.py — HL xyz: candle-plane feed pins.

Fixture rows below are the REAL 2026-11-07 candleSnapshot response shape for
xyz:NVDA (fields t,T,s,i,o,c,h,l,v,n; numbers as strings except t/T/n).
"""

import asyncio
import time
import unittest
from unittest.mock import MagicMock, patch

import data.hyperliquid_feed as hl
import data.tradfi_feed as tf
from data.candle_buffer import CandleBuffer
from data.sodex_feed import SoDEXFeed

NOW_MS = 1_794_103_800_000   # matches the forming bar's open_time below

# Real-shape rows: two closed bars + the still-forming bar (t == NOW_MS).
FIXTURE_ROWS = [
    {"t": NOW_MS - 120_000, "T": NOW_MS - 60_001, "s": "xyz:NVDA", "i": "1m",
     "o": "186.60", "c": "186.65", "h": "186.70", "l": "186.50",
     "v": "5.5", "n": 6},
    {"t": NOW_MS - 60_000, "T": NOW_MS - 1, "s": "xyz:NVDA", "i": "1m",
     "o": "186.65", "c": "186.64", "h": "186.65", "l": "186.60",
     "v": "7.065", "n": 8},
    {"t": NOW_MS, "T": NOW_MS + 59_999, "s": "xyz:NVDA", "i": "1m",
     "o": "186.64", "c": "186.66", "h": "186.66", "l": "186.64",
     "v": "13.339", "n": 4},
]


def _feed(symbols=("NVDA-USD",), with_buf=True):
    bufs = {s: {"1m": CandleBuffer(s, "1m")} for s in symbols} if with_buf else {}
    feed = hl.HyperliquidFeed(candle_buffers=bufs, mark_price_stores={},
                              symbols=list(symbols))
    return feed, bufs


class TestParser(unittest.TestCase):
    def test_parse_real_shape(self):
        candles = hl.parse_candle_rows(FIXTURE_ROWS)
        self.assertEqual(len(candles), 3)
        c = candles[1]
        self.assertEqual(c.open_time, NOW_MS - 60_000)
        self.assertEqual(c.close_time, NOW_MS)          # t + 60s
        self.assertAlmostEqual(c.open, 186.65)
        self.assertAlmostEqual(c.close, 186.64)
        self.assertAlmostEqual(c.volume, 7.065)

    def test_parse_skips_malformed_never_raises(self):
        bad = [None, "x", {}, {"t": "abc"}, {"t": 1, "o": "0", "c": "0",
               "h": "0", "l": "0"}]
        self.assertEqual(hl.parse_candle_rows(bad), [])
        self.assertEqual(hl.parse_candle_rows({"not": "a list"}), [])
        self.assertEqual(hl.parse_candle_rows(None), [])


class TestPollOne(unittest.TestCase):
    def setUp(self):
        hl._feed = None

    def tearDown(self):
        hl._feed = None

    def _run_poll(self, feed, rows):
        client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = rows
        client.post = MagicMock(return_value=resp)

        async def fake_fetch(*a, **k):
            return resp.json()
        return fake_fetch

    def test_forming_bar_written_closed_bar_published_once(self):
        feed, bufs = _feed()
        buf = bufs["NVDA-USD"]["1m"]
        with patch("data.hyperliquid_feed.event_bus") as bus, \
             patch("data.hyperliquid_feed.time") as t:
            t.time.return_value = NOW_MS / 1000.0 + 5   # mid forming bar
            feed._fetch_candles = self._run_poll(feed, FIXTURE_ROWS)
            asyncio.run(feed._poll_one(MagicMock(), "NVDA-USD"))
            # forming bar written in place
            self.assertEqual(buf.count(), 3)
            # exactly one CANDLE_CLOSED for the newest CLOSED bar only
            pubs = [c for c in bus.publish.call_args_list]
            self.assertEqual(len(pubs), 1)
            ev = pubs[0].args[0]
            self.assertEqual(ev.symbol, "NVDA-USD")
            self.assertEqual(ev.timestamp_ms, NOW_MS - 60_000)
            self.assertEqual(ev.data["close"], 186.64)
            self.assertTrue(ev.data["confirmed"])
            # second poll of the same rows: no republish
            asyncio.run(feed._poll_one(MagicMock(), "NVDA-USD"))
            self.assertEqual(bus.publish.call_count, 1)

    def test_marks_signal_plane_only_by_default(self):
        feed, bufs = _feed()
        store = MagicMock()
        feed.mark_price_stores["NVDA-USD"] = store
        with patch("data.hyperliquid_feed.event_bus"), \
             patch("data.hyperliquid_feed.time") as t, \
             patch.dict("os.environ", {"HL_MARKS_ENABLED": "false"}):
            t.time.return_value = NOW_MS / 1000.0 + 5
            feed._fetch_candles = self._run_poll(feed, FIXTURE_ROWS)
            asyncio.run(feed._poll_one(MagicMock(), "NVDA-USD"))
            # signal-plane mark recorded, canonical store untouched
            self.assertAlmostEqual(feed.marks["NVDA-USD"], 186.66)
            store.update.assert_not_called()
        with patch("data.hyperliquid_feed.event_bus"), \
             patch("data.hyperliquid_feed.time") as t, \
             patch.dict("os.environ", {"HL_MARKS_ENABLED": "true"}):
            t.time.return_value = NOW_MS / 1000.0 + 6
            feed._last_bar_ts["NVDA-USD"] = 0
            feed._fetch_candles = self._run_poll(feed, FIXTURE_ROWS)
            asyncio.run(feed._poll_one(MagicMock(), "NVDA-USD"))
            store.update.assert_called_once()

    def test_empty_snapshot_raises_for_caller_fail_closed(self):
        feed, _ = _feed()
        feed._fetch_candles = self._run_poll(feed, [])
        with self.assertRaises(RuntimeError):
            asyncio.run(feed._poll_one(MagicMock(), "NVDA-USD"))
        self.assertNotIn("NVDA-USD", feed._last_ok)


class TestOwnership(unittest.TestCase):
    def setUp(self):
        hl._feed = None
        tf.set_candle_yield([])
        tf._feed = None

    def tearDown(self):
        hl._feed = None
        tf.set_candle_yield([])
        tf._feed = None

    def test_hl_owns_health_gated(self):
        feed, _ = _feed()
        hl._feed = feed
        feed.enabled = True
        self.assertFalse(hl.hl_owns("NVDA-USD"))            # never polled
        feed._last_ok["NVDA-USD"] = time.time()
        self.assertTrue(hl.hl_owns("NVDA-USD"))
        feed._last_ok["NVDA-USD"] = time.time() - 9999      # stale
        self.assertFalse(hl.hl_owns("NVDA-USD"))
        feed._last_ok["NVDA-USD"] = time.time()
        feed.enabled = False                                 # verify failed
        self.assertFalse(hl.hl_owns("NVDA-USD"))
        hl._feed = None
        self.assertFalse(hl.hl_owns("NVDA-USD"))

    def test_tradfi_yields_when_hl_owns(self):
        feed, _ = _feed()
        hl._feed = feed
        feed.enabled = True
        feed._last_ok["NVDA-USD"] = time.time()
        tf._feed = tf.TradfiFeed(candle_buffers={}, mark_price_stores={})
        tf._feed._last_ok["NVDA-USD"] = time.time()
        self.assertFalse(tf.tradfi_owns("NVDA-USD"))
        # HL dark → tradfi ownership restored (no dark plane)
        feed._last_ok["NVDA-USD"] = 0.0
        self.assertTrue(tf.tradfi_owns("NVDA-USD"))

    def _sodex_feed(self, symbols):
        cfg = MagicMock()
        cfg.assets = list(symbols)
        cfg.aster_kline_assets = []
        cfg.sodex_kline_assets = []
        bufs = {s: {"1m": CandleBuffer(s, "1m")} for s in symbols}
        return (SoDEXFeed(config=cfg, mark_price_stores={}, orderbook_stores={},
                          candle_buffers=bufs, trade_flow_stores={}), bufs)

    def _candle_msg(self, symbol):
        return {"channel": "candle", "data": {
            "s": symbol, "t": NOW_MS - 60_000, "T": NOW_MS,
            "o": "69.0", "h": "69.2", "l": "68.9", "c": "69.1",
            "v": "0.05", "x": True,
        }}

    def test_sodex_ws_yields_when_hl_owns(self):
        feed, bufs = self._sodex_feed(["XAUT-USD"])
        hlf, _ = _feed(symbols=("XAUT-USD",))
        hl._feed = hlf
        hlf.enabled = True
        hlf._last_ok["XAUT-USD"] = time.time()
        with patch("data.sodex_feed.event_bus"):
            asyncio.run(feed._handle(self._candle_msg("XAUT-USD")))
        self.assertEqual(bufs["XAUT-USD"]["1m"].count(), 0)
        self.assertNotIn("XAUT-USD", feed._kline_owned_symbols())

    def test_sodex_ws_writes_when_hl_dark(self):
        # The dark-plane pin: HL unhealthy → SoDEX resumes writing.
        feed, bufs = self._sodex_feed(["XAUT-USD"])
        hlf, _ = _feed(symbols=("XAUT-USD",))
        hl._feed = hlf
        hlf.enabled = True
        hlf._last_ok["XAUT-USD"] = time.time() - 9999
        with patch("data.sodex_feed.event_bus"):
            asyncio.run(feed._handle(self._candle_msg("XAUT-USD")))
        self.assertEqual(bufs["XAUT-USD"]["1m"].count(), 1)
        self.assertIn("XAUT-USD", feed._kline_owned_symbols())


class TestBootAndKillSwitch(unittest.TestCase):
    def setUp(self):
        hl._feed = None

    def tearDown(self):
        hl._feed = None

    def test_boot_verify_shape_and_coin_membership(self):
        feed, _ = _feed(symbols=("XAUT-USD", "SMCI-USD"))
        payload = [
            {"universe": [{"name": "xyz:GOLD"}, {"name": "xyz:NVDA"}]},
            [{"markPx": "1"}, {"markPx": "2"}],
        ]
        client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = payload

        async def post(*a, **k):
            return resp
        client.post = post
        ok = asyncio.run(feed._boot_verify(client))
        self.assertTrue(ok)
        # SMCI has no xyz: listing on HL (2026-11-07 probe) → dropped
        self.assertEqual(feed.symbols, ["XAUT-USD"])

    def test_boot_verify_bad_shape_fails_closed(self):
        feed, _ = _feed()
        client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"error": "nope"}

        async def post(*a, **k):
            return resp
        client.post = post
        self.assertFalse(asyncio.run(feed._boot_verify(client)))

    def test_start_stands_down_on_verify_failure(self):
        feed, _ = _feed()
        async def boom(*a, **k):
            raise RuntimeError("network down")
        feed._boot_verify = boom
        asyncio.run(feed.start())
        self.assertFalse(feed.enabled)
        self.assertFalse(hl.hl_owns("NVDA-USD"))

    def test_config_defaults_and_list(self):
        from core.config import Settings
        cfg = Settings()
        self.assertTrue(cfg.hl_feed_enabled)   # safe: boot self-verify gates
        expected = ["TSM-USD", "USTECH100-USD", "LITE-USD", "SAMSUNG-USD",
                    "SKHX-USD", "UNITREE-USD", "DRAM-USD", "XAUT-USD",
                    "CL-USD", "COPPER-USD"]
        self.assertEqual(sorted(cfg.hl_kline_assets), sorted(expected))
        # every owned symbol has an xyz: listing in the canonical map AND
        # exists in the tradeable universe (candle buffers are per-assets)
        for s in cfg.hl_kline_assets:
            self.assertIn(s, hl.HL_SYMBOLS)
            self.assertTrue(hl.HL_SYMBOLS[s].startswith("xyz:"))
            self.assertIn(s, cfg.assets)
        # SMCI (no HL listing) and SILVER (universe-removed) stay out
        self.assertNotIn("SMCI-USD", cfg.hl_kline_assets)
        self.assertNotIn("SILVER-USD", cfg.hl_kline_assets)

    def test_env_kill_switch_off(self):
        import os
        from unittest.mock import patch as _p
        with _p.dict(os.environ, {"HL_FEED_ENABLED": "false"}):
            from core.config import Settings
            self.assertFalse(Settings().hl_feed_enabled)

    def test_symbol_map_covers_all_xyz_from_signal_tap(self):
        # The canonical map is signal_tap's; every xyz: entry must be present.
        from prop_harness.io.signal_tap import SYMBOL_MAP
        for aria, coin in SYMBOL_MAP.items():
            if coin.startswith("xyz:"):
                self.assertEqual(hl.HL_SYMBOLS.get(aria), coin)


if __name__ == "__main__":
    unittest.main()
