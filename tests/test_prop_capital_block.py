"""Pins for intelligence/prop_capital_block.py — §7 surplus + tradfi fascia.

Unlabeled omit = legacy aria_exec (fail-open). Capital standdowns emit
aria_capital_block; risk_block is never minted here. v1 does not wait on
KingdomView thesis_reserve (graph-synthesis.md; S4 later).

Fascia (2026-11-08): emit_exec_signal (anticipator approve → aria_exec) and
emit_arcus_notional_block (Arcus dust → notional_floor capital_block).
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
    monkeypatch.delenv("PROP_OUTBOX_ENABLED", raising=False)
    monkeypatch.delenv("PROP_OUTBOX_FAST_CYCLE_EMIT", raising=False)
    monkeypatch.delenv("PROP_OUTBOX_ARCUS_NOTIONAL_EMIT", raising=False)
    monkeypatch.delenv("PROP_OUTBOX_MAP_HYGIENE", raising=False)
    monkeypatch.delenv("PROP_SURPLUS_COHERENCE_UNITS", raising=False)


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


class TestFasciaExecEmit:
    def test_fast_cycle_emit_writes_aria_exec(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROP_OUTBOX_MAP_HYGIENE", "false")
        path = str(tmp_path / "prop_outbox.jsonl")
        out = pcb.emit_exec_signal(
            symbol="XAUT-USD", side="long", entry_ref=2650.0,
            stop_price=2620.0, tp1_price=2700.0, coherence=3.4,
            aria_venue="sodex", outbox_path=path, ts_ms=42)
        assert out is not None
        assert out["block_class"] == "aria_exec"
        assert out["src"] == "fast_cycle_approved"
        assert out["symbol"] == "XAUT-USD"
        rows = [json.loads(l) for l in open(path) if l.strip()]
        assert len(rows) == 1
        assert rows[0]["stop_price"] == 2620.0

    def test_fast_cycle_kill_switch_off(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROP_OUTBOX_FAST_CYCLE_EMIT", "false")
        monkeypatch.setenv("PROP_OUTBOX_MAP_HYGIENE", "false")
        path = str(tmp_path / "prop_outbox.jsonl")
        assert pcb.emit_exec_signal(
            symbol="XAUT-USD", side="long", entry_ref=2650.0,
            stop_price=2620.0, outbox_path=path) is None
        assert not os.path.exists(path)

    def test_fast_cycle_master_outbox_off(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROP_OUTBOX_ENABLED", "false")
        monkeypatch.setenv("PROP_OUTBOX_MAP_HYGIENE", "false")
        path = str(tmp_path / "prop_outbox.jsonl")
        assert pcb.emit_exec_signal(
            symbol="CL-USD", side="short", entry_ref=70.0,
            stop_price=71.0, outbox_path=path) is None

    def test_fast_cycle_map_hygiene_refuses_unmapped(self, monkeypatch, tmp_path):
        # Hygiene ON (default); invent a symbol absent from SYMBOL_MAP.
        path = str(tmp_path / "prop_outbox.jsonl")
        out = pcb.emit_exec_signal(
            symbol="NOTAMAPPED-USD", side="long", entry_ref=10.0,
            stop_price=9.5, outbox_path=path)
        assert out is None
        assert not os.path.exists(path)

    def test_fast_cycle_mapped_tradfi_passes(self, tmp_path):
        path = str(tmp_path / "prop_outbox.jsonl")
        out = pcb.emit_exec_signal(
            symbol="USTECH100-USD", side="short", entry_ref=27000.0,
            stop_price=27200.0, outbox_path=path, ts_ms=7)
        assert out is not None
        assert out["symbol"] == "USTECH100-USD"
        assert out["block_class"] == "aria_exec"


class TestSurplusCoherenceUnits:
    """Option B — live-signals-cybernetic-audit §2.2 / P0.

    OrderSpec.strength is 0..1; prop eval risk_for_signal wants 0–10.
    Prefer ARIA coherence; else strength*10 (never omit when strength set).
    """

    def test_kill_default_on(self):
        assert pcb.surplus_coherence_units_enabled() is True
        assert pcb.surplus_coherence_units_enabled(
            SimpleNamespace(prop_surplus_coherence_units_enabled=True)) is True

    def test_kill_env_off(self, monkeypatch):
        monkeypatch.setenv("PROP_SURPLUS_COHERENCE_UNITS", "0")
        assert pcb.surplus_coherence_units_enabled() is False

    def test_prefer_aria_coherence(self):
        assert pcb.resolve_surplus_coherence(
            aria_coherence=8.4, strength=0.8) == 8.4

    def test_strength_times_ten_fallback(self):
        # Documented: missing aria coh → strength*10, never omit.
        assert pcb.resolve_surplus_coherence(strength=0.8) == 8.0
        assert pcb.resolve_surplus_coherence(strength=0.6) == 6.0
        assert pcb.resolve_surplus_coherence(strength=1.0) == 10.0

    def test_clamps_to_zero_ten(self):
        assert pcb.resolve_surplus_coherence(aria_coherence=12.0) == 10.0
        assert pcb.resolve_surplus_coherence(aria_coherence=-1.0) == 0.0
        assert pcb.resolve_surplus_coherence(strength=1.5) == 10.0

    def test_no_strength_no_aria_returns_none(self):
        # xpr CounterProbeSpec has no strength — omit when aria dark.
        assert pcb.resolve_surplus_coherence() is None

    def test_legacy_kill_off_passes_strength_raw(self, monkeypatch):
        monkeypatch.setenv("PROP_SURPLUS_COHERENCE_UNITS", "false")
        # Legacy ant: strength stamped as coherence (0..1 mis-scale).
        assert pcb.resolve_surplus_coherence(
            aria_coherence=8.4, strength=0.8) == 0.8
        # Legacy xpr: omit when no strength.
        assert pcb.resolve_surplus_coherence(aria_coherence=8.4) is None

    def test_emit_capital_block_uses_resolved_coh(self, tmp_path):
        path = str(tmp_path / "prop_outbox.jsonl")
        coh = pcb.resolve_surplus_coherence(strength=0.8)
        out = pcb.emit_capital_block(
            symbol="SOL-USD", side="short", entry_ref=150.0,
            stop_price=151.5, capital_reason="margin_preflight",
            coherence=coh, outbox_path=path, ts_ms=7)
        assert out is not None
        assert out["coherence"] == 8.0
        rows = [json.loads(l) for l in open(path) if l.strip()]
        assert rows[0]["coherence"] == 8.0


class TestFasciaArcusNotional:
    def test_arcus_notional_emit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROP_OUTBOX_MAP_HYGIENE", "false")
        path = str(tmp_path / "prop_outbox.jsonl")
        out = pcb.emit_arcus_notional_block(
            symbol="TSLA-USD", side="long", entry_ref=250.0,
            stop_price=248.0, outbox_path=path, ts_ms=11)
        assert out is not None
        assert out["block_class"] == "aria_capital_block"
        assert out["capital_reason"] == "notional_floor"
        assert out["aria_venue"] == "arcus"
        assert out["src"] == "arcus_below_min_notional"

    def test_arcus_notional_kill_switch_off(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROP_OUTBOX_ARCUS_NOTIONAL_EMIT", "0")
        monkeypatch.setenv("PROP_OUTBOX_MAP_HYGIENE", "false")
        path = str(tmp_path / "prop_outbox.jsonl")
        assert pcb.emit_arcus_notional_block(
            symbol="NVDA-USD", side="long", entry_ref=140.0,
            stop_price=138.0, outbox_path=path) is None

    def test_arcus_notional_respects_surplus_kill(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROP_CAPITAL_SURPLUS", "false")
        monkeypatch.setenv("PROP_OUTBOX_MAP_HYGIENE", "false")
        path = str(tmp_path / "prop_outbox.jsonl")
        assert pcb.emit_arcus_notional_block(
            symbol="TSLA-USD", side="long", entry_ref=250.0,
            stop_price=248.0, outbox_path=path) is None
