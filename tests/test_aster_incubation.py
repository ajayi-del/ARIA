"""Unit tests for the Aster incubation path — no I/O, no network.

Incubation = expansion symbols live in config.assets with no SoDEX ID:
fetch_symbol_ids() must NOT prune them (aster exemption), and the shadow
journal must score their fully-approved signals under gate "no_venue".
"""
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from intelligence.shadow_journal import REJECTION_EVENTS, ShadowJournal


class _Resp:
    status_code = 200

    def json(self):
        return {"code": 0, "data": [{"name": "BTC-USD", "id": 1}]}


class _FakeAsyncClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        return _Resp()


class TestSymbolIdExemption(unittest.TestCase):
    def test_aster_symbols_survive_pruning(self):
        import main as m
        cfg = SimpleNamespace(
            assets=["BTC-USD", "VELVET-USD", "GHOST-USD"],
            core_assets=["BTC-USD"],
            bybit_assets=[],
            aster_assets=["VELVET-USD"],
        )
        client = SimpleNamespace(base_url="https://example.invalid/api/v1")
        with patch("httpx.AsyncClient", _FakeAsyncClient):
            asyncio.run(m.fetch_symbol_ids(client, cfg, m.logger))
        # VELVET-USD has no SoDEX ID and no fallback entry, but the aster
        # exemption keeps it in the universe (execution routes via venue.py).
        self.assertIn("VELVET-USD", cfg.assets)
        self.assertNotIn("VELVET-USD", m.SYMBOL_IDS)
        # GHOST-USD is in neither venue list — pruned as before.
        self.assertNotIn("GHOST-USD", cfg.assets)
        self.assertEqual(m.SYMBOL_IDS.get("BTC-USD"), 1)


class TestNoVenueGate(unittest.TestCase):
    def test_mapping_exists(self):
        self.assertEqual(REJECTION_EVENTS["order_blocked_no_symbol_id"],
                         "no_venue")

    def test_blocked_order_records_no_venue_shadow(self):
        with tempfile.TemporaryDirectory() as td:
            j = ShadowJournal()
            cfg = SimpleNamespace(shadow_journal_enabled=True, log_dir=td)
            j.wire(cfg, {}, {"VELVET-USD": SimpleNamespace(mark_price=0.21),
                             "BTC-USD": SimpleNamespace(mark_price=63000.0)},
                   {})
            j.processor(None, "info", {
                "event": "order_blocked_no_symbol_id",
                "symbol": "VELVET-USD", "direction": "long",
                "reason": "no SoDEX symbol ID"})
            self.assertEqual(len(j._open), 1)
            rec = next(iter(j._open.values()))
            self.assertEqual(rec["gate"], "no_venue")
            self.assertEqual(rec["symbol"], "VELVET-USD")
            self.assertEqual(rec["direction"], "long")
            self.assertEqual(rec["entry"], 0.21)

    def test_blocked_order_without_direction_skipped(self):
        # Pre-incubation log shape (no direction field) must not record.
        with tempfile.TemporaryDirectory() as td:
            j = ShadowJournal()
            cfg = SimpleNamespace(shadow_journal_enabled=True, log_dir=td)
            j.wire(cfg, {}, {"VELVET-USD": SimpleNamespace(mark_price=0.21)},
                   {})
            j.processor(None, "info", {
                "event": "order_blocked_no_symbol_id",
                "symbol": "VELVET-USD",
                "action": "signal dropped — no SoDEX symbol ID"})
            self.assertEqual(len(j._open), 0)


if __name__ == "__main__":
    unittest.main()
