"""Unit tests for intelligence/shadow_journal.py — no I/O, no network."""
import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from intelligence.shadow_journal import ShadowJournal, _session_of, _skew


def _journal(tmpdir, marks=None, enabled=True):
    j = ShadowJournal()
    cfg = SimpleNamespace(shadow_journal_enabled=enabled, log_dir=tmpdir)
    j.wire(cfg, {}, marks or {}, {})
    return j


class TestRecording(unittest.TestCase):
    def test_rejection_creates_shadow(self):
        with tempfile.TemporaryDirectory() as td:
            j = _journal(td, {"OP-USD": SimpleNamespace(mark_price=0.42),
                              "BTC-USD": SimpleNamespace(mark_price=63000.0)})
            j.processor(None, "info", {"event": "signal_rejected_dispersion_gate",
                                       "symbol": "OP-USD", "direction": "long",
                                       "dispersion": 0.0055, "reason": "x",
                                       "coherence": 5.5, "threshold": 0.015})
            self.assertEqual(len(j._open), 1)
            rec = next(iter(j._open.values()))
            self.assertEqual(rec["gate"], "dispersion")
            self.assertEqual(rec["entry"], 0.42)
            self.assertEqual(rec["direction"], "long")
            self.assertEqual(rec["btc_price"], 63000.0)
            self.assertLess(rec["hyp_stop"], 0.42)   # long stop below entry

    def test_dedup_window(self):
        with tempfile.TemporaryDirectory() as td:
            j = _journal(td, {"OP-USD": SimpleNamespace(mark_price=0.42)})
            ev = {"event": "signal_rejected_c_tier", "symbol": "OP-USD",
                  "direction": "long", "coherence": 5.5}
            j.processor(None, "info", dict(ev))
            j.processor(None, "info", dict(ev))   # within 30min → dropped
            self.assertEqual(len(j._open), 1)

    def test_non_rejection_ignored_and_trade_tracked(self):
        with tempfile.TemporaryDirectory() as td:
            j = _journal(td, {})
            j.processor(None, "info", {"event": "signal_ready", "symbol": "X"})
            self.assertEqual(len(j._open), 0)
            j.processor(None, "info", {"event": "order_filled", "symbol": "X"})
            self.assertGreater(j._last_trade_ts, 0)

    def test_direction_none_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            j = _journal(td, {"X-USD": SimpleNamespace(mark_price=1.0)})
            j.processor(None, "info", {"event": "signal_stale_data",
                                       "symbol": "X-USD", "dir": "none"})
            self.assertEqual(len(j._open), 0)

    def test_processor_never_raises(self):
        j = ShadowJournal()   # unwired
        out = j.processor(None, "info", {"event": "coherence_tier_reject",
                                         "symbol": None})
        self.assertEqual(out["event"], "coherence_tier_reject")


class TestScoring(unittest.TestCase):
    def _rec(self, ts):
        return {"id": "x", "ts": ts, "symbol": "OP-USD", "direction": "long",
                "gate": "dispersion", "event": "e", "reason": "", "coherence": 5.0,
                "entry": 100.0, "hyp_stop": 98.0, "btc_price": 1.0,
                "session": "us", "regime": "", "gate_value": 0.0055,
                "gate_threshold": 0.015, "marks": {}, "mfe": 0.0, "mae": 0.0,
                "stopped": False, "scored": {}, "info_axis": None}

    def test_horizons_and_mfe(self):
        with tempfile.TemporaryDirectory() as td:
            t0 = time.time() - 25 * 3600
            j = _journal(td, {"OP-USD": SimpleNamespace(mark_price=103.0)})
            j._open["x"] = self._rec(t0)
            j._score_tick()
            self.assertNotIn("x", j._open)              # finalized at 24h
            rec = j._scored[0]
            self.assertEqual(rec["scored"]["1h"], 3.0)  # 100→103 long
            self.assertAlmostEqual(rec["mfe"], 0.03)
            self.assertTrue(rec["won_24h"])
            self.assertFalse(rec["stopped"])

    def test_stop_detection_kills_win(self):
        with tempfile.TemporaryDirectory() as td:
            t0 = time.time() - 25 * 3600
            j = _journal(td, {"OP-USD": SimpleNamespace(mark_price=97.0)})
            j._open["x"] = self._rec(t0)
            j._score_tick()
            rec = j._scored[0]
            self.assertTrue(rec["stopped"])             # 97 ≤ 98 stop
            self.assertFalse(rec["won_24h"])

    def test_persistence_quadrants(self):
        with tempfile.TemporaryDirectory() as td:
            t0 = time.time() - 25 * 3600
            j = _journal(td, {"OP-USD": SimpleNamespace(mark_price=99.0)})
            j._open["x"] = self._rec(t0)
            # gate value moved 209% of threshold within 30min → TRANSIENT
            j._gate_series["dispersion"] = [(t0 + 1800, 0.0055 + 2.09 * 0.015)]
            j._score_tick()
            rec = j._scored[0]
            self.assertEqual(rec["info_axis"], "TRANSIENT")
            # pnl 99 vs 100 = loss → refusal saved money on transient info → lucky
            self.assertEqual(rec["quadrant"], "lucky")


class TestAggregation(unittest.TestCase):
    def _scored_rec(self, ts, gate, pnl24, won4, quadrant="wise", sym="OP-USD"):
        return {"id": "x", "ts": ts, "symbol": sym, "direction": "long",
                "gate": gate, "event": "e", "reason": "", "coherence": 5.0,
                "entry": 100.0, "hyp_stop": 98.0, "btc_price": 1.0,
                "session": "us", "regime": "", "gate_value": None,
                "gate_threshold": None, "mfe": 0.0, "mae": 0.0, "stopped": False,
                "scored": {}, "info_axis": None, "pnl_4h": pnl24 / 2,
                "pnl_24h": pnl24, "won_4h": won4, "won_24h": won4,
                "quadrant": quadrant}

    def test_fnr_shrinkage_small_sample(self):
        with tempfile.TemporaryDirectory() as td:
            j = _journal(td, {})
            # 3/3 winners → raw FNR 1.0, but shrink k=20 pulls toward 0.5
            j._scored = [self._scored_rec(time.time(), "dispersion", 1.0, True)
                         for _ in range(3)]
            fnr = j._fnr(j._scored)
            self.assertAlmostEqual(fnr, (3 / 23) * 1.0 + (20 / 23) * 0.5, places=2)

    def test_aggregate_shapes(self):
        with tempfile.TemporaryDirectory() as td:
            j = _journal(td, {})
            now = time.time()
            j._scored = (
                [self._scored_rec(now - 86400, "dispersion", 1.0, True,
                                  "lucky") for _ in range(5)]
                + [self._scored_rec(now - 86400, "dispersion", -1.0, False,
                                    "wise") for _ in range(5)]
                + [self._scored_rec(now - 86400, "c_tier", -0.5, False)
                   for _ in range(8)]
            )
            j._trade_ts = [now - 10 * 3600, now - 2 * 3600]
            rep = j._aggregate()
            self.assertIn("dispersion", rep["q1_gate_fnr"])
            self.assertEqual(rep["q1_gate_fnr"]["dispersion"]["n"], 10)
            self.assertIn("gvr", rep["q5_gate_value_ratio"]["dispersion"])
            self.assertEqual(rep["q10_lucky_gates"]["dispersion"]["lucky"], 5)
            self.assertEqual(rep["q10_lucky_gates"]["dispersion"]["wise"], 5)
            self.assertTrue(rep["q10_lucky_gates"]["dispersion"]["luck_dominated"])
            md = j._render_md(rep)
            self.assertIn("Q10", md)
            self.assertIn("LUCK-DOMINATED", md)


class TestHelpers(unittest.TestCase):
    def test_session_buckets(self):
        import calendar
        self.assertEqual(_session_of(calendar.timegm((2026, 1, 1, 3, 0, 0))), "asia")
        self.assertEqual(_session_of(calendar.timegm((2026, 1, 1, 9, 0, 0))), "london")
        self.assertEqual(_session_of(calendar.timegm((2026, 1, 1, 15, 0, 0))), "us")
        self.assertEqual(_session_of(calendar.timegm((2026, 1, 1, 22, 0, 0))), "off_hours")

    def test_skew(self):
        self.assertGreater(_skew([1, 1, 1, 1, 1, 1, 1, 1, 1, 20]), 0.5)
        self.assertEqual(_skew([1.0] * 10), 0.0)


class TestRegistry(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            j = _journal(td, {"OP-USD": SimpleNamespace(mark_price=0.42)})
            j.processor(None, "info", {"event": "signal_rejected_c_tier",
                                       "symbol": "OP-USD", "direction": "short",
                                       "coherence": 5.0})
            j._save_registry()
            j2 = _journal(td, {})
            self.assertEqual(len(j2._open), 1)
            rec = next(iter(j2._open.values()))
            self.assertEqual(rec["symbol"], "OP-USD")


class TestScoredPersistence(unittest.TestCase):
    """Restart-amnesia regression (2026-08-17): _scored was memory-only —
    every restart wiped the verdict base, reports read '0 scored ghosts'
    with 1063 records opened. Verdicts now persist to shadow_scored.jsonl."""

    def _rec(self, ts, rid="x"):
        return {"id": rid, "ts": ts, "symbol": "OP-USD", "direction": "long",
                "gate": "dispersion", "event": "e", "reason": "", "coherence": 5.0,
                "entry": 100.0, "hyp_stop": 98.0, "btc_price": 1.0,
                "session": "us", "regime": "", "gate_value": 0.0055,
                "gate_threshold": 0.015, "marks": {}, "mfe": 0.0, "mae": 0.0,
                "stopped": False, "scored": {}, "info_axis": None}

    def test_finalize_appends_scored_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            j = _journal(td, {"OP-USD": SimpleNamespace(mark_price=103.0)})
            j._open["x"] = self._rec(time.time() - 25 * 3600)
            j._score_tick()
            with open(f"{td}/shadow_scored.jsonl") as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])
            self.assertEqual(rec["id"], "x")
            self.assertTrue(rec["won_24h"])

    def test_scored_survives_rewire(self):
        with tempfile.TemporaryDirectory() as td:
            j = _journal(td, {"OP-USD": SimpleNamespace(mark_price=103.0)})
            j._open["x"] = self._rec(time.time() - 25 * 3600)
            j._score_tick()
            j2 = _journal(td, {})          # the restart
            self.assertEqual(len(j2._scored), 1)
            self.assertEqual(j2._scored[0]["id"], "x")
            self.assertTrue(j2._scored[0]["won_24h"])

    def test_load_dedups_by_id_last_wins(self):
        with tempfile.TemporaryDirectory() as td:
            now = time.time()
            r1 = self._rec(now - 3600, "dup") | {"won_24h": False}
            r2 = self._rec(now - 3600, "dup") | {"won_24h": True}
            with open(f"{td}/shadow_scored.jsonl", "w") as f:
                f.write(json.dumps(r1) + "\n")
                f.write(json.dumps(r2) + "\n")
            j = _journal(td, {})
            self.assertEqual(len(j._scored), 1)
            self.assertTrue(j._scored[0]["won_24h"])

    def test_load_filters_beyond_35d(self):
        with tempfile.TemporaryDirectory() as td:
            old = self._rec(time.time() - 40 * 86400, "old")
            new = self._rec(time.time() - 86400, "new")
            with open(f"{td}/shadow_scored.jsonl", "w") as f:
                f.write(json.dumps(old) + "\n")
                f.write(json.dumps(new) + "\n")
                f.write("{not json\n")            # one bad line kills one record
            j = _journal(td, {})
            self.assertEqual([r["id"] for r in j._scored], ["new"])


if __name__ == "__main__":
    unittest.main()
