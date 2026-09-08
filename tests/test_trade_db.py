"""Trade DB attribution fields (2026-09-08 observability, T3a).

trade_db.jsonl rows lacked venue / mark_at_exit / trigger_event /
ratchet_state / treasury_state — exit attribution (which subsystem owned
each close) was unmeasurable. The fields are additive-only: old rows
without them stay valid, missing values land as None, writes never raise.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.trade_db import TradeDatabase, TradeRecord  # noqa: E402
from main import _build_trade_record  # noqa: E402


def _record(**kw):
    base = dict(
        trade_id="SOL-USD_1000", symbol="SOL-USD", side="long",
        timestamp_open_ms=1000, timestamp_close_ms=2000,
        coherence_score=6.0, tiers_fired=[], htf_regime="up",
        session_name="us", session_mult=1.0,
        entry_price=100.0, exit_price=101.0, notional_usd=500.0,
        leverage=5, stop_price=99.0, tp1_price=102.0, atr=0.5,
        hold_seconds=60.0, directional_pnl=5.0, net_pnl=4.5,
        max_adverse_excursion=0.1, max_favourable_excursion=1.2,
        exit_reason="software_tp",
    )
    base.update(kw)
    return TradeRecord(**base)


class TestAttributionFields(unittest.TestCase):
    def test_record_carries_five_new_fields(self):
        rec = _record(venue="aster", mark_at_exit=101.25,
                      trigger_event="software_tp",
                      ratchet_state={"stop": 100.4, "peak_roe": 6.7},
                      treasury_state=True)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "trade_db.jsonl"
            with patch("memory.trade_db.DB_PATH", path):
                db = TradeDatabase()
                db.record(rec)
                with open(path) as f:
                    row = json.loads(f.readline())
        self.assertEqual(row["venue"], "aster")
        self.assertEqual(row["mark_at_exit"], 101.25)
        self.assertEqual(row["trigger_event"], "software_tp")
        self.assertEqual(row["ratchet_state"], {"stop": 100.4, "peak_roe": 6.7})
        self.assertIs(row["treasury_state"], True)

    def test_defaults_are_null_never_raise(self):
        rec = _record()     # no attribution kwargs
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "trade_db.jsonl"
            with patch("memory.trade_db.DB_PATH", path):
                db = TradeDatabase()
                db.record(rec)
                with open(path) as f:
                    row = json.loads(f.readline())
        for k in ("venue", "mark_at_exit", "trigger_event",
                  "ratchet_state", "treasury_state"):
            self.assertIn(k, row)                # key present on new rows
            self.assertIsNone(row[k])            # missing value = null

    def test_old_rows_without_fields_stay_valid(self):
        # A pre-2026-09-08 row (no attribution keys) must load and stat cleanly.
        import dataclasses
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "trade_db.jsonl"
            legacy = dataclasses.asdict(_record())
            # asdict-style legacy row: strip the new keys entirely
            for k in ("venue", "mark_at_exit", "trigger_event",
                      "ratchet_state", "treasury_state"):
                legacy.pop(k, None)
            with open(path, "w") as f:
                f.write(json.dumps(legacy) + "\n")
            with patch("memory.trade_db.DB_PATH", path):
                db = TradeDatabase()
                rows = db.get_all()
                stats = db.get_stats()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "SOL-USD")
        self.assertNotIn("venue", rows[0])       # absence tolerated
        self.assertEqual(stats["total"], 1)


class TestBuildTradeRecordAttribution(unittest.TestCase):
    def _pos(self):
        return SimpleNamespace(
            entry_price=100.0, size=5.0, side="long", opened_at_ms=1000,
            symbol="SOL-USD", entry_coherence=6.0, tiers_fired=[],
            entry_htf="up", entry_session="us", entry_session_mult=1.0,
            leverage=5, stop_price=99.0, tp1_price=102.0, atr=0.5,
            max_adverse_excursion=0.1, max_favourable_excursion=1.2,
        )

    def test_builder_passes_attribution_kwargs(self):
        rec = _build_trade_record(
            self._pos(), exit_price=101.0, exit_reason="software_stop",
            net_pnl=-1.5, venue="sodex", mark_at_exit=99.4,
            trigger_event="software_stop",
            ratchet_state={"stop": 100.4, "peak_roe": 6.7},
            treasury_state=False)
        self.assertEqual(rec.venue, "sodex")
        self.assertEqual(rec.mark_at_exit, 99.4)
        self.assertEqual(rec.trigger_event, "software_stop")
        self.assertEqual(rec.ratchet_state, {"stop": 100.4, "peak_roe": 6.7})
        self.assertIs(rec.treasury_state, False)
        # Legacy fields untouched
        self.assertEqual(rec.exit_reason, "software_stop")
        self.assertEqual(rec.symbol, "SOL-USD")

    def test_builder_legacy_call_signature_bit_for_bit(self):
        # The pre-2026-09-08 call shape (4 args) still works, fields null.
        rec = _build_trade_record(self._pos(), exit_price=101.0,
                                  exit_reason="time_stop", net_pnl=0.5)
        self.assertEqual(rec.exit_reason, "time_stop")
        for k in ("venue", "mark_at_exit", "trigger_event",
                  "ratchet_state", "treasury_state"):
            self.assertIsNone(getattr(rec, k))


if __name__ == "__main__":
    unittest.main()
