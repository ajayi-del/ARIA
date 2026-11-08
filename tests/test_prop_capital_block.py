"""Pins for intelligence/prop_capital_block.py — §7 surplus emit (ATS-19).

Unlabeled omit = legacy aria_exec (fail-open). Capital standdowns emit
aria_capital_block; risk_block is never minted here. v1 does not wait on
KingdomView thesis_reserve (graph-synthesis.md; S4 later).
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from intelligence import prop_capital_block as pcb


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("PROP_BLOCK_CLASS", raising=False)
    monkeypatch.delenv("PROP_CAPITAL_SURPLUS", raising=False)


class TestBuildSignal:
    def test_long_geometry(self):
        rec = pcb.build_signal(
            symbol="BTC-USD", side="long", entry_ref=100.0, stop_price=99.0,
            coherence=5.5, block_class=pcb.BLOCK_CAPITAL,
            capital_reason="notional_floor", aria_venue="sodex", ts_ms=1)
        assert rec is not None
        assert rec["block_class"] == "aria_capital_block"
        assert rec["capital_reason"] == "notional_floor"
        assert rec["aria_venue"] == "sodex"
        assert rec["thesis_id"].startswith("BTC-USD:long:")
        assert rec["side"] == "long"

    def test_naked_stop_refused(self):
        assert pcb.build_signal(
            symbol="BTC-USD", side="long", entry_ref=100.0,
            stop_price=101.0) is None

    def test_buy_normalizes_to_long(self):
        rec = pcb.build_signal(
            symbol="ETH-USD", side="buy", entry_ref=3000.0,
            stop_price=2970.0, ts_ms=2)
        assert rec["side"] == "long"


class TestKillSwitches:
    def test_block_class_env_off(self, monkeypatch):
        monkeypatch.setenv("PROP_BLOCK_CLASS", "0")
        assert pcb.block_class_enabled() is False
        tagged = pcb.tag_exec_block_class({"id": "x"})
        assert "block_class" not in tagged

    def test_block_class_tags_exec(self):
        tagged = pcb.tag_exec_block_class({"id": "x", "symbol": "BTC-USD"})
        assert tagged["block_class"] == "aria_exec"

    def test_surplus_env_off_skips_emit(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROP_CAPITAL_SURPLUS", "0")
        path = str(tmp_path / "prop_outbox.jsonl")
        out = pcb.emit_capital_block(
            symbol="BTC-USD", side="long", entry_ref=100.0, stop_price=99.0,
            capital_reason="margin_budget", outbox_path=path)
        assert out is None
        assert not os.path.exists(path)

    def test_surplus_emit_writes_jsonl(self, tmp_path):
        path = str(tmp_path / "prop_outbox.jsonl")
        out = pcb.emit_capital_block(
            symbol="SOL-USD", side="short", entry_ref=150.0, stop_price=151.5,
            capital_reason="venue_equity_insufficient", aria_venue="aster",
            coherence=4.2, outbox_path=path, ts_ms=99)
        assert out is not None
        assert out["block_class"] == "aria_capital_block"
        rows = [json.loads(l) for l in open(path) if l.strip()]
        assert len(rows) == 1
        assert rows[0]["capital_reason"] == "venue_equity_insufficient"


class TestFromCandidate:
    def test_emits_from_candidate(self, tmp_path):
        cand = SimpleNamespace(
            symbol="LINK-USD", side="long", entry_price=20.0,
            stop_price=19.5, tp1_price=21.0, coherence_score=6.1)
        path = str(tmp_path / "o.jsonl")
        out = pcb.emit_from_candidate(
            cand, capital_reason="notional_floor", aria_venue="sodex",
            outbox_path=path)
        assert out["symbol"] == "LINK-USD"
        assert out["coherence"] == 6.1

    def test_degenerate_candidate_skips(self, tmp_path):
        cand = SimpleNamespace(
            symbol="LINK-USD", side="long", entry_price=0.0, stop_price=0.0)
        assert pcb.emit_from_candidate(
            cand, capital_reason="notional_floor",
            outbox_path=str(tmp_path / "o.jsonl")) is None
