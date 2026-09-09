"""Pins for intelligence/plane_ledger.py (SCH-1/2/3, 2026-09-09).

Covers: would-veto math parity with skeptic.base_rate_veto, coherence/tide
vectors, quant-filter predicate replication, row shape, append doctrine
(kill-switch off = zero writes; one-bad-line = row lost, never the file),
and the TradeRecord join fields.
"""
import json
import os

import pytest

from intelligence import plane_ledger as pl
from intelligence.skeptic import base_rate_veto, VETO_MIN_N, VETO_WR_MARGIN
from memory.trade_db import TradeRecord


# ── base_rate_vector parity ───────────────────────────────────────────────────

@pytest.mark.parametrize("wr", [0.05, 0.10, 0.19, 0.20, 0.30, 0.50, 0.80])
@pytest.mark.parametrize("n", [0, 5, 9, 10, 30, 200])
@pytest.mark.parametrize("rr", [None, 0.0, 0.5, 1.0, 2.0, -1.0])
def test_base_rate_vector_parity_with_skeptic(wr, n, rr):
    v = pl.base_rate_vector(wr, n, rr)
    assert v["would_veto"] == base_rate_veto(wr, n, rr), (wr, n, rr)
    rr_f = float(rr or 0.0)
    breakeven = 1.0 / (1.0 + rr_f) if rr_f > 0 else 0.5
    assert v["threshold"] == pytest.approx(breakeven * VETO_WR_MARGIN, abs=1e-4)
    assert v["min_n"] == VETO_MIN_N


def test_base_rate_vector_unmeasurable_is_none_not_false():
    v = pl.base_rate_vector(None, None, 1.0)
    assert v["would_veto"] is None
    assert v["blended_wr"] is None
    assert v["n"] is None


def test_base_rate_vector_bad_rr_falls_back_conservative():
    v = pl.base_rate_vector(0.10, 50, "garbage")
    assert v["threshold"] == pytest.approx(0.5 * VETO_WR_MARGIN, abs=1e-4)


# ── coherence_vector ──────────────────────────────────────────────────────────

def test_coherence_vector_unmeasured():
    v = pl.coherence_vector(None, 8.0, 3.5)
    assert v["would_block"] is None
    assert v["asserted"] == 8.0
    assert v["measured"] is None


def test_coherence_vector_below_floor_blocks():
    assert pl.coherence_vector(2.8, 8.0, 3.5)["would_block"] is True


def test_coherence_vector_above_floor_passes():
    assert pl.coherence_vector(5.2, 8.0, 3.5)["would_block"] is False


# ── tide_vector ───────────────────────────────────────────────────────────────

def test_tide_vector_opposed_vetoes():
    v = pl.tide_vector(-500_000_000.0, 12.0, "opposed")
    assert v["would_veto"] is True


@pytest.mark.parametrize("verdict", ["aligned", "neutral"])
def test_tide_vector_non_opposed_never_vetoes(verdict):
    assert pl.tide_vector(500_000_000.0, 12.0, verdict)["would_veto"] is False


def test_tide_vector_dark_feed():
    v = pl.tide_vector(None, None, "neutral")
    assert v["flow_usd"] is None
    assert v["would_veto"] is False


# ── quant_filter_vector ───────────────────────────────────────────────────────

def _qf(**kw):
    base = dict(side="long", htf="neutral", regime="trending", coherence=5.0,
                vc_zscore=0.0, vc_direction="none", vc_phase="none",
                events_60s=100, quiet_s=0.0, is_tradfi=False,
                kant_accumulation=False, recovery_active=False)
    base.update(kw)
    return pl.quant_filter_vector(**base)


def test_qf_htf_opposed_blocks():
    assert _qf(side="short", htf="bullish")["htf_counter_trend"] is True
    assert _qf(side="long", htf="bearish")["htf_counter_trend"] is True


def test_qf_htf_aligned_never_blocks():
    assert _qf(side="long", htf="bullish")["htf_counter_trend"] is False
    assert _qf(side="short", htf="bearish")["htf_counter_trend"] is False


def test_qf_htf_elite_override_denied_in_recovery():
    assert _qf(side="short", htf="bullish", coherence=9.0)["htf_counter_trend"] is False
    assert _qf(side="short", htf="bullish", coherence=9.0,
               recovery_active=True)["htf_counter_trend"] is True


def test_qf_htf_confused_regime_soft():
    assert _qf(side="short", htf="bullish", regime="confused")["htf_counter_trend"] is False


def test_qf_htf_accumulation_probe_allowed_unless_recovery():
    assert _qf(side="short", htf="bullish",
               kant_accumulation=True)["htf_counter_trend"] is False
    assert _qf(side="short", htf="bullish", kant_accumulation=True,
               recovery_active=True)["htf_counter_trend"] is True


def test_qf_cascade_counter_direction():
    assert _qf(side="long", vc_zscore=3.0,
               vc_direction="bearish")["cascade_counter_direction"] is True
    assert _qf(side="short", vc_zscore=3.0,
               vc_direction="bearish")["cascade_counter_direction"] is False
    assert _qf(side="long", vc_zscore=1.5,
               vc_direction="bearish")["cascade_counter_direction"] is False
    assert _qf(side="long", vc_zscore=3.0,
               vc_direction="mixed")["cascade_counter_direction"] is False


def test_qf_cascade_expansion():
    assert _qf(vc_phase="expansion", vc_zscore=3.0)["cascade_expansion_unfillable"] is True
    assert _qf(vc_phase="expansion", vc_zscore=2.0)["cascade_expansion_unfillable"] is False
    assert _qf(vc_phase="exhaustion", vc_zscore=3.0)["cascade_expansion_unfillable"] is False


def test_qf_quiet_market():
    assert _qf(events_60s=10, quiet_s=2000.0)["quiet_market_pause"] is True
    assert _qf(events_60s=10, quiet_s=2000.0,
               is_tradfi=True)["quiet_market_pause"] is False
    assert _qf(events_60s=999, quiet_s=9999.0)["quiet_market_pause"] is False
    assert _qf(events_60s=10, quiet_s=900.0)["quiet_market_pause"] is False


def test_qf_optional_filters_none_when_unevaluated():
    v = _qf()
    assert v["dead_market_atr"] is None
    assert v["dispersion_gate"] is None
    assert v["dispersion_reason"] is None


def test_qf_dispersion_caller_evaluated():
    assert _qf(dispersion_ok=False, dispersion_reason="low_dispersion")["dispersion_gate"] is True
    assert _qf(dispersion_ok=True)["dispersion_gate"] is False


def test_qf_would_block_any_ignores_none_and_reason():
    assert _qf()["would_block_any"] is False
    assert _qf(side="short", htf="bullish")["would_block_any"] is True
    assert _qf(dispersion_ok=False)["would_block_any"] is True
    assert _qf(atr_too_small=True)["would_block_any"] is True


# ── build_row shape ───────────────────────────────────────────────────────────

def test_build_row_shape_filled():
    row = pl.build_row(
        ts_ms=1_700_000_000_000, symbol="BTC-USD", side="long",
        attempt_id="BTC-USD_1700000000000", plane="fastpath",
        strategy_tag="cascade_momentum", executor="cascade_momentum",
        entry_path_site="post_fill", gate_vector={"coherence": {"measured": 5.0}},
        sizing={"notional_usd": 250.0, "leverage": 8, "margin_usd": 31.25,
                "size_mults_applied": []},
        entry_id_uuid="uuid-1", trade_id="BTC-USD_1700000000000",
        filled=True, fill_ts_ms=1_700_000_000_000, reject_reason=None)
    assert row["schema"] == 1
    assert row["identity"]["attempt_id"] == "BTC-USD_1700000000000"
    assert row["plane"]["plane"] == "fastpath"
    assert row["gate_vector"]["coherence"]["measured"] == 5.0
    ok = row["outcome_key"]
    assert ok["filled"] is True and ok["reject_reason"] is None
    assert ok["trade_id"] == row["identity"]["attempt_id"]  # the join
    assert row["ts"].endswith("Z")


def test_build_row_shape_reject():
    row = pl.build_row(
        ts_ms=1_700_000_000_000, symbol="ETH-USD", side="short",
        attempt_id="ETH-USD_1700000000000", plane="gated",
        strategy_tag=None, executor="standard_path",
        entry_path_site="execution_decision", gate_vector=None, sizing=None,
        entry_id_uuid=None, trade_id=None, filled=False, fill_ts_ms=None,
        reject_reason="kant_structure")
    assert row["outcome_key"]["filled"] is False
    assert row["outcome_key"]["trade_id"] is None
    assert row["outcome_key"]["reject_reason"] == "kant_structure"


# ── append_row doctrine ───────────────────────────────────────────────────────

def test_append_row_writes_one_json_line(tmp_path, monkeypatch):
    monkeypatch.delenv("EXECUTION_PLANE_LEDGER_ENABLED", raising=False)
    p = str(tmp_path / "ledger.jsonl")
    row = {"schema": 1, "x": 1}
    assert pl.append_row(p, row) is True
    lines = open(p).read().strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0]) == row


def test_append_row_kill_switch_off_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("EXECUTION_PLANE_LEDGER_ENABLED", "false")
    p = str(tmp_path / "ledger.jsonl")
    assert pl.append_row(p, {"schema": 1}) is False
    assert not os.path.exists(p)


def test_append_row_bad_row_costs_row_not_file(tmp_path, monkeypatch):
    monkeypatch.delenv("EXECUTION_PLANE_LEDGER_ENABLED", raising=False)
    p = str(tmp_path / "ledger.jsonl")
    assert pl.append_row(p, {"good": 1}) is True
    before = open(p).read()

    class _Unserializable:
        __slots__ = ()
        def __repr__(self): return "<bad>"

    # default=str rescues arbitrary objects; force a genuine failure with a
    # cyclic dict instead.
    cyc = {}
    cyc["self"] = cyc
    assert pl.append_row(p, cyc) is False
    assert open(p).read() == before  # one-bad-line: file untouched


def test_append_row_missing_dir_returns_false_not_raise(tmp_path, monkeypatch):
    monkeypatch.delenv("EXECUTION_PLANE_LEDGER_ENABLED", raising=False)
    assert pl.append_row(str(tmp_path / "nope" / "x.jsonl"), {"a": 1}) is False


# ── kill-switch defaults ──────────────────────────────────────────────────────

def test_kill_switches_default_true(monkeypatch):
    for k in ("EXECUTION_PLANE_LEDGER_ENABLED", "FASTPATH_GATE_VECTOR_ENABLED",
              "FASTPATH_MEASURED_COHERENCE_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    assert pl.ledger_enabled() is True
    assert pl.gate_vector_enabled() is True
    assert pl.measured_coherence_enabled() is True


@pytest.mark.parametrize("val", ["false", "FALSE", "False"])
def test_kill_switches_false_forms(monkeypatch, val):
    monkeypatch.setenv("EXECUTION_PLANE_LEDGER_ENABLED", val)
    assert pl.ledger_enabled() is False


# ── TradeRecord join fields ───────────────────────────────────────────────────

def _record(**kw):
    base = dict(trade_id="BTC-USD_1", symbol="BTC-USD", side="long",
                timestamp_open_ms=1, timestamp_close_ms=2,
                coherence_score=5.0, tiers_fired=[], htf_regime="bull",
                session_name="us", session_mult=1.0, entry_price=100.0,
                exit_price=101.0, notional_usd=100.0, leverage=8,
                stop_price=99.0, tp1_price=102.0, atr=0.5, hold_seconds=60.0,
                directional_pnl=1.0, net_pnl=1.0, max_adverse_excursion=0.1,
                max_favourable_excursion=1.2, exit_reason="tp1")
    base.update(kw)
    return TradeRecord(**base)


def test_trade_record_join_fields_default_none():
    r = _record()
    assert r.entry_id_uuid is None
    assert r.entry_plane is None
    assert r.coherence_measured is None
    assert r.coherence_asserted is None
    assert r.coherence_source is None


def test_trade_record_join_fields_round_trip():
    from dataclasses import asdict
    r = _record(entry_id_uuid="uuid-9", entry_plane="fastpath",
                coherence_measured=4.2, coherence_asserted=8.0,
                coherence_source="measured")
    d = asdict(r)
    assert d["entry_id_uuid"] == "uuid-9"
    assert d["entry_plane"] == "fastpath"
    assert d["coherence_measured"] == 4.2
    assert d["coherence_source"] == "measured"
