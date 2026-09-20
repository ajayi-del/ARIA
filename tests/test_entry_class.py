"""B3 pins (2026-09-19): entry_class tagging.

Pins: the Position schema field (default None, additive), the TradeRecord
field, and the threading through main._build_trade_record.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from execution.schemas import Position  # noqa: E402
from memory.trade_db import TradeRecord  # noqa: E402


def _position(**kw):
    base = dict(
        symbol="SOL-USD", side="long", entry_price=100.0, size=1.0,
        stop_price=99.0, tp1_price=101.0, tp2_price=102.0, tp3_price=103.0,
        liq_price=90.0, initial_margin=20.0, leverage=5,
        opened_at_ms=1_000_000,
    )
    base.update(kw)
    return Position(**base)


class TestSchemaFields:
    def test_position_entry_class_defaults_none(self):
        assert _position().entry_class is None

    def test_position_entry_class_accepts_values(self):
        for klass in ("cascade", "restart", "salvo", "normal"):
            assert _position(entry_class=klass).entry_class == klass

    def test_trade_record_entry_class_defaults_none(self):
        rec = main._build_trade_record(_position(), 100.5, "exchange_close", 0.5)
        assert isinstance(rec, TradeRecord)
        assert rec.entry_class is None


class TestBuildTradeRecordThreading:
    def test_cascade_class_threaded(self):
        rec = main._build_trade_record(
            _position(entry_class="cascade"), 100.5, "exchange_close", 0.5)
        assert rec.entry_class == "cascade"

    def test_restart_class_threaded(self):
        rec = main._build_trade_record(
            _position(entry_class="restart"), 100.5, "exchange_close", 0.5)
        assert rec.entry_class == "restart"

    def test_unstamped_position_records_none(self):
        # A position built before the stamp existed (attr missing entirely)
        # must not raise — getattr fallback records None.
        pos = SimpleNamespace(
            symbol="SOL-USD", side="long", entry_price=100.0, size=1.0,
            opened_at_ms=1_000_000)
        rec = main._build_trade_record(pos, 100.5, "exchange_close", 0.5)
        assert rec.entry_class is None
