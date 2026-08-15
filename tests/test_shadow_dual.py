"""Unit tests for the shadow-dual dataset (2026-08-16) — no network.

Shadow-dual: BTC/ETH/SOL stay SoDEX-routed live; Aster supplies data only.
These tests pin the isolation invariants — if any of them break, live
routing may have flipped.
"""
import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace

from data.aster_feed import AsterFeed
from execution import venue
from intelligence.shadow_journal import ShadowJournal


class TestShadowFeed(unittest.TestCase):
    def test_bookticker_streams_only_for_shadow_symbols(self):
        f = AsterFeed(symbols=["HYPE-USD"],
                      shadow_symbols=["BTC-USD", "ETH-USD", "SOL-USD"])
        url = f._stream_url()
        self.assertIn("btcusdt@bookTicker", url)
        self.assertIn("ethusdt@bookTicker", url)
        self.assertIn("solusdt@bookTicker", url)
        self.assertIn("btcusdt@markPrice@1s", url)
        self.assertNotIn("hypeusdt@bookTicker", url)   # live syms: no book
        self.assertIn("hypeusdt@markPrice@1s", url)

    def test_shadow_symbol_overlapping_live_not_duplicated(self):
        f = AsterFeed(symbols=["BTC-USD"], shadow_symbols=["BTC-USD"])
        url = f._stream_url()
        self.assertEqual(url.count("btcusdt@markPrice@1s"), 1)
        self.assertNotIn("bookTicker", url)

    def test_book_ticker_handler_parses_binance_shape(self):
        f = AsterFeed(shadow_symbols=["BTC-USD"])
        f._handle_book_ticker({"u": 1, "s": "BTCUSDT", "b": "63000.5",
                               "B": "0.4", "a": "63001.5", "A": "0.2",
                               "E": 1724000000000})
        b = f.book["BTC-USD"]
        self.assertEqual(b["bid"], 63000.5)
        self.assertEqual(b["ask"], 63001.5)
        self.assertEqual(b["bid_qty"], 0.4)
        self.assertEqual(b["ask_qty"], 0.2)
        self.assertAlmostEqual(b["ts"], 1724000000.0, places=0)

    def test_book_ticker_handler_ts_fallback_and_garbage_safe(self):
        f = AsterFeed(shadow_symbols=["ETH-USD"])
        f._handle_book_ticker({"s": "ETHUSDT", "b": "1900", "a": "1901"})
        self.assertGreater(f.book["ETH-USD"]["ts"], 0)
        f._handle_book_ticker({"s": "", "b": "x"})       # no symbol → ignored
        f._handle_book_ticker({"s": "ETHUSDT", "b": "garbage"})
        self.assertEqual(f.book["ETH-USD"]["bid"], 1900.0)  # not clobbered


class TestRoutingIsolation(unittest.TestCase):
    def setUp(self):
        venue._executors.clear()
        venue._venue_by_symbol.clear()

    def tearDown(self):
        venue._executors.clear()
        venue._venue_by_symbol.clear()

    def test_shadow_assets_never_reroute(self):
        venue.register_executor("sodex", object())
        venue.register_executor("aster", object())
        venue.assign_symbols(["HYPE-USD", "ADA-USD"], "aster")
        for sym in ("BTC-USD", "ETH-USD", "SOL-USD"):
            self.assertEqual(venue.venue_for(sym), "sodex")


class TestVenueSnapshot(unittest.TestCase):
    def _journal(self, log_dir):
        j = ShadowJournal()
        j._config = SimpleNamespace(log_dir=log_dir, shadow_journal_enabled=True)
        j._wired = True
        return j

    def test_snapshot_appends_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            j = self._journal(d)
            j.record_venue_snapshot(
                "BTC-USD", "long", 63047.0,
                sodex_book={"bid": 63046.0, "ask": 63048.0, "spread_bps": 0.32},
                aster_mark={"mark_price": 63050.0, "funding_rate": 0.0001},
                aster_book={"bid": 63049.5, "ask": 63050.5},
                funding_sodex=0.0001, funding_aster=0.00012)
            with open(os.path.join(d, "venue_snapshots.jsonl")) as f:
                rec = json.loads(f.readline())
            self.assertEqual(rec["symbol"], "BTC-USD")
            self.assertEqual(rec["fill_price"], 63047.0)
            self.assertEqual(rec["aster_book"]["bid"], 63049.5)
            self.assertEqual(rec["funding_aster"], 0.00012)

    def test_snapshot_never_raises_on_garbage(self):
        j = ShadowJournal()   # no config at all — _path falls back
        j._config = SimpleNamespace(log_dir="/nonexistent-dir-x",
                                    shadow_journal_enabled=True)
        j.record_venue_snapshot("BTC-USD", "long", 1.0,
                                sodex_book=None, aster_mark=None)


class TestGateAccuracy(unittest.TestCase):
    def _row(self, gate, won24, won4, mfe=0.01, ts=None):
        return {
            "id": "x", "ts": ts or time.time(), "symbol": "BTC-USD",
            "direction": "long", "gate": gate, "event": "e", "reason": "",
            "coherence": 5.0, "entry": 100.0, "hyp_stop": 99.0,
            "btc_price": 100.0, "session": "us", "regime": "r",
            "market_energy": None, "day_type": "", "gate_value": None,
            "gate_threshold": None, "marks": {}, "mfe": mfe, "mae": 0.0,
            "stopped": False, "scored": {}, "info_axis": "PERSISTENT",
            "pnl_4h": 1.0, "pnl_24h": 1.0 if won24 else -1.0,
            "won_4h": won4, "won_24h": won24, "quadrant": "wise",
        }

    def test_gate_accuracy_counts_and_verdicts(self):
        j = ShadowJournal()
        j._scored = ([self._row("dispersion", False, False) for _ in range(9)]
                     + [self._row("dispersion", True, True)]
                     + [self._row("throttle", True, True) for _ in range(4)]
                     + [self._row("throttle", False, False) for _ in range(6)])
        rep = j._aggregate()
        ga = rep["gate_accuracy"]
        self.assertEqual(ga["dispersion"]["gated"], 10)
        self.assertEqual(ga["dispersion"]["would_profit"], 1)
        self.assertEqual(ga["dispersion"]["accuracy"], 0.9)
        self.assertEqual(ga["dispersion"]["verdict"], "strong")
        self.assertEqual(ga["throttle"]["accuracy"], 0.6)
        self.assertEqual(ga["throttle"]["verdict"], "too_tight")
        self.assertEqual(ga["_total"]["gated"], 20)
        self.assertEqual(ga["_total"]["would_profit"], 5)
        self.assertEqual(ga["_total"]["verdict"], "GATES CORRECT")

    def test_gate_accuracy_includes_4h_and_mfe(self):
        j = ShadowJournal()
        j._scored = [self._row("c_tier", True, False, mfe=0.03)]
        rep = j._aggregate()
        d = rep["gate_accuracy"]["c_tier"]
        self.assertEqual(d["would_profit_4h"], 0)
        self.assertEqual(d["would_profit"], 1)
        self.assertEqual(d["avg_mfe_pct"], 3.0)


if __name__ == "__main__":
    unittest.main()
