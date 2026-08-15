"""Unit tests for the Watcher (Mode 1) + Dreamer (Mode 7) — no I/O, no network."""
import math
import tempfile
import time
import unittest
from collections import deque
from types import SimpleNamespace

from intelligence.watcher import Watcher
from intelligence.explosive_scanner import ExplosiveScanner
from intelligence.day_type_classifier import DayTypeClassifier, DayType, DayTypeState
from intelligence.shadow_journal import ShadowJournal
from intelligence.liq_phase_engine import liq_phase_engine, _EventRecord


class _Buf:
    def __init__(self, candles):
        self._candles = candles

    def latest(self, n):
        return self._candles[-n:]


def _candles(closes, vols):
    return [SimpleNamespace(close=c, volume=v, high=c * 1.001, low=c * 0.999)
            for c, v in zip(closes, vols)]


def _quiet_buffers():
    # Old candles volatile, recent candles dead — compression in progress.
    closes, vols = [], []
    px = 100.0
    for i in range(120):
        amp = 0.02 if i < 80 else 0.002
        px *= (1 + amp * math.sin(i))
        closes.append(px)
        vols.append(1000.0 if i < 80 else 200.0)
    return {"AAA-USD": {"1m": _Buf(_candles(closes, vols))},
            "BBB-USD": {"1m": _Buf(_candles(closes, vols))}}


def _storm_buffers():
    closes, vols = [], []
    px = 100.0
    for i in range(120):
        amp = 0.002 if i < 80 else 0.03
        px *= (1 + amp * (1 if i % 2 else -1))
        closes.append(px)
        vols.append(200.0 if i < 80 else 3000.0)
    return {"AAA-USD": {"1m": _Buf(_candles(closes, vols))},
            "BBB-USD": {"1m": _Buf(_candles(closes, vols))}}


def _classifier_with(day_type):
    clf = DayTypeClassifier()
    for sym in ("AAA-USD", "BBB-USD"):
        clf._state[sym] = DayTypeState(day_type=day_type)
    return clf


class TestWatcher(unittest.TestCase):
    def setUp(self):
        self._saved_events = dict(liq_phase_engine._events)
        liq_phase_engine._events.clear()

    def tearDown(self):
        liq_phase_engine._events.clear()
        liq_phase_engine._events.update(self._saved_events)

    def _liqs(self, n, now):
        dq = deque(maxlen=500)
        for i in range(n):
            dq.append(_EventRecord(timestamp=now - i * 30,
                                   notional_usd=100_000.0,
                                   direction="bearish", venue="bybit"))
        liq_phase_engine._events[""] = dq

    def test_quiet_market_low_energy(self):
        w = Watcher()
        now = time.time()
        snap = w.compute(_quiet_buffers(), {}, _classifier_with(DayType.CHOP),
                         now=now)
        self.assertIsNotNone(snap["energy"])
        self.assertLess(snap["energy"], 25.0)

    def test_storm_market_high_energy(self):
        w = Watcher()
        now = time.time()
        self._liqs(14, now)
        tickers = {f"S{i}-USD": {"funding_rate": 0.001 * (i - 3),
                                 "open_interest": 0.0}
                   for i in range(7)}
        snap = w.compute(_storm_buffers(), tickers,
                         _classifier_with(DayType.TREND), now=now)
        self.assertGreater(snap["energy"], 50.0)
        self.assertEqual(snap["components"]["liq"], 100.0)

    def test_chop_fraction_damps_energy(self):
        now = time.time()
        self._liqs(14, now)
        w1, w2 = Watcher(), Watcher()
        tickers = {f"S{i}-USD": {"funding_rate": 0.001 * (i - 3)}
                   for i in range(7)}
        bufs = _storm_buffers()
        trend = w1.compute(bufs, tickers, _classifier_with(DayType.TREND), now=now)
        chop = w2.compute(bufs, tickers, _classifier_with(DayType.CHOP), now=now)
        self.assertAlmostEqual(chop["raw_energy"], trend["raw_energy"], places=1)
        self.assertLess(chop["energy"], trend["energy"])
        self.assertAlmostEqual(chop["energy"], chop["raw_energy"] * 0.6, places=1)

    def test_no_data_energy_none(self):
        w = Watcher()
        snap = w.compute({}, {}, None)
        self.assertIsNone(snap["energy"])

    def test_oi_change_pct(self):
        w = Watcher()
        now = time.time()
        w._oi_hist["ACE-USD"] = deque([(now - 3700, 100.0), (now, 106.0)])
        self.assertAlmostEqual(w.oi_change_pct("ACE-USD", 3600.0, now=now), 6.0)
        self.assertIsNone(w.oi_change_pct("NOPE-USD", 3600.0, now=now))


class TestDreamer(unittest.TestCase):
    def _loaded_buffers(self):
        # 100 volatile candles, last 20 tight (compression), last 15 huge volume.
        closes, vols = [], []
        px = 2.0
        for i in range(120):
            amp = 0.02 if i < 95 else 0.0005
            px *= (1 + amp * math.sin(i * 1.7))
            closes.append(px)
            vols.append(5000.0 if i >= 105 else 100.0)
        return {"ACE-USD": {"1m": _Buf(_candles(closes, vols))}}

    def test_three_precursors_emit_long_squeeze(self):
        w = Watcher()
        now = time.time()
        w._oi_hist["ACE-USD"] = deque([(now - 3700, 100.0), (now, 106.0)])
        tickers = {"ACE-USD": {"funding_rate": -0.0008, "open_interest": 106.0}}
        cands = ExplosiveScanner().scan(["ACE-USD"], self._loaded_buffers(),
                                        tickers, w, now=now)
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c["symbol"], "ACE-USD")
        self.assertEqual(c["direction"], "long")   # shorts crowded → squeeze up
        self.assertGreaterEqual(c["score"], 3)
        self.assertIn("compression", c["precursors"])
        self.assertIn("funding_extreme", c["precursors"])
        self.assertIn("volume_breakout", c["precursors"])

    def test_calm_symbol_silent(self):
        w = Watcher()
        now = time.time()
        tickers = {"ACE-USD": {"funding_rate": 0.0001, "open_interest": 100.0}}
        cands = ExplosiveScanner().scan(["ACE-USD"], _quiet_buffers(),
                                        tickers, w, now=now)
        self.assertEqual(cands, [])

    def test_cooldown_blocks_repeat(self):
        w = Watcher()
        now = time.time()
        w._oi_hist["ACE-USD"] = deque([(now - 3700, 100.0), (now, 106.0)])
        tickers = {"ACE-USD": {"funding_rate": -0.0008, "open_interest": 106.0}}
        s = ExplosiveScanner()
        bufs = self._loaded_buffers()
        first = s.scan(["ACE-USD"], bufs, tickers, w, now=now)
        second = s.scan(["ACE-USD"], bufs, tickers, w, now=now + 60)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])


class TestHistorianFields(unittest.TestCase):
    def test_record_candidate_with_context(self):
        with tempfile.TemporaryDirectory() as td:
            j = ShadowJournal()
            cfg = SimpleNamespace(shadow_journal_enabled=True, log_dir=td)
            j.wire(cfg, {},
                   {"ACE-USD": SimpleNamespace(mark_price=2.0),
                    "BTC-USD": SimpleNamespace(mark_price=63000.0)},
                   {},
                   context_fn=lambda sym: {"market_energy": 22.5,
                                           "day_type": "chop"})
            j.record_candidate("ACE-USD", "long", "explosive", 3,
                               details="bb_pctl=0.10 funding=-0.0008")
            self.assertEqual(len(j._open), 1)
            rec = next(iter(j._open.values()))
            self.assertEqual(rec["gate"], "explosive_s3")
            self.assertEqual(rec["event"], "explosive_candidate")
            self.assertEqual(rec["market_energy"], 22.5)
            self.assertEqual(rec["day_type"], "chop")
            self.assertEqual(rec["entry"], 2.0)
            self.assertLess(rec["hyp_stop"], 2.0)

    def test_record_candidate_dedup(self):
        with tempfile.TemporaryDirectory() as td:
            j = ShadowJournal()
            cfg = SimpleNamespace(shadow_journal_enabled=True, log_dir=td)
            j.wire(cfg, {}, {"ACE-USD": SimpleNamespace(mark_price=2.0)}, {})
            j.record_candidate("ACE-USD", "long", "explosive", 3)
            j.record_candidate("ACE-USD", "long", "explosive", 3)
            self.assertEqual(len(j._open), 1)

    def test_rejection_records_carry_context(self):
        with tempfile.TemporaryDirectory() as td:
            j = ShadowJournal()
            cfg = SimpleNamespace(shadow_journal_enabled=True, log_dir=td)
            j.wire(cfg, {}, {"OP-USD": SimpleNamespace(mark_price=0.42)},
                   {},
                   context_fn=lambda sym: {"market_energy": 8.0,
                                           "day_type": "range"})
            j.processor(None, "info", {"event": "signal_rejected_c_tier",
                                       "symbol": "OP-USD", "direction": "long",
                                       "coherence": 5.5})
            rec = next(iter(j._open.values()))
            self.assertEqual(rec["market_energy"], 8.0)
            self.assertEqual(rec["day_type"], "range")


if __name__ == "__main__":
    unittest.main()
