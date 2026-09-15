"""tests/test_liq_clusters.py — pins for the liquidation-cluster capture plane
(data/liq_clusters.py) and the pure math of the aftermath analyzer
(tools/liq_cluster_stats.py). Observer-class: the builder is zero-I/O with
injected timestamps; the only disk in these tests is tmp_path JSONL.
"""
import importlib.util
import json
import os

import pytest

from data.liq_clusters import (LiqClusterBuilder, append_cluster,
                               classify_cluster, read_clusters)

_TOOL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "tools", "liq_cluster_stats.py")
_spec = importlib.util.spec_from_file_location("liq_cluster_stats", _TOOL_PATH)
lcs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lcs)

T0 = 1_760_000_000_000   # arbitrary injected epoch ms


def _ingest(b, dt_s=0, symbol="BTC-USD", direction="bearish",
            notional=100_000.0, venue="valuechain"):
    return b.ingest(T0 + int(dt_s * 1000), symbol, direction, notional, venue)


# ── Tier boundaries (Bybit BTC-row table, Governor-stamped default) ──────────

class TestClassifyCluster:
    def test_small_below_500k(self):
        assert classify_cluster(499_999.0) == "small"

    def test_medium_at_500k(self):
        assert classify_cluster(500_000.0) == "medium"

    def test_medium_below_2m(self):
        assert classify_cluster(1_999_999.0) == "medium"

    def test_large_at_2m(self):
        assert classify_cluster(2_000_000.0) == "large"

    def test_large_below_5m(self):
        assert classify_cluster(4_999_999.0) == "large"

    def test_whale_at_5m(self):
        assert classify_cluster(5_000_000.0) == "whale"

    def test_whale_above_5m(self):
        assert classify_cluster(25_000_000.0) == "whale"

    def test_symbol_kind_reserved_ignored(self):
        assert classify_cluster(600_000.0, symbol_kind="alt") == "medium"

    def test_bad_input_never_raises(self):
        assert classify_cluster("garbage") == "small"
        assert classify_cluster(None) == "small"


# ── Cluster gap flush at 60s ─────────────────────────────────────────────────

class TestGapFlush:
    def test_event_at_t_plus_59s_same_cluster(self):
        b = LiqClusterBuilder()
        assert _ingest(b, 0) is None
        assert _ingest(b, 59) is None
        assert b.open_count() == 1
        rows = b.flush_all(T0 + 200_000)
        assert len(rows) == 1
        assert rows[0]["event_count"] == 2

    def test_event_at_t_plus_61s_flushes_new_cluster(self):
        b = LiqClusterBuilder()
        assert _ingest(b, 0) is None
        row = _ingest(b, 61)
        assert row is not None
        assert row["event_count"] == 1
        assert row["ts_start_ms"] == T0
        assert b.open_count() == 1   # the new cluster stays open

    def test_gap_exactly_60s_stays_open(self):
        b = LiqClusterBuilder()
        _ingest(b, 0)
        assert _ingest(b, 60) is None   # gap must EXCEED 60s to flush
        assert b.open_count() == 1


# ── Symbol + direction isolation ─────────────────────────────────────────────

class TestIsolation:
    def test_btc_long_cluster_does_not_absorb_btc_short(self):
        b = LiqClusterBuilder()
        _ingest(b, 0, direction="bearish")
        _ingest(b, 10, direction="bullish")
        assert b.open_count() == 2

    def test_btc_cluster_does_not_absorb_eth(self):
        b = LiqClusterBuilder()
        _ingest(b, 0, symbol="BTC-USD")
        _ingest(b, 10, symbol="ETH-USD")
        assert b.open_count() == 2

    def test_isolated_rows_flush_separately(self):
        b = LiqClusterBuilder()
        _ingest(b, 0, symbol="BTC-USD", direction="bearish", notional=100_000)
        _ingest(b, 5, symbol="BTC-USD", direction="bullish", notional=200_000)
        row = _ingest(b, 61, symbol="BTC-USD", direction="bearish",
                      notional=50_000)
        assert row is not None
        assert row["total_notional"] == 100_000.0   # only the bearish leg
        assert b.open_count() == 2


# ── flush_all drains stale only ──────────────────────────────────────────────

class TestFlushAll:
    def test_drains_stale_only(self):
        b = LiqClusterBuilder()
        _ingest(b, 0, symbol="BTC-USD")          # stale at now
        _ingest(b, 110, symbol="ETH-USD")        # fresh at now
        rows = b.flush_all(T0 + 121_000)         # BTC 121s old, ETH 11s old
        assert len(rows) == 1
        assert rows[0]["symbol"] == "BTC-USD"
        assert b.open_count() == 1

    def test_stale_boundary_120s_not_drained(self):
        b = LiqClusterBuilder()
        _ingest(b, 0)
        assert b.flush_all(T0 + 120_000) == []   # must EXCEED 120s
        assert b.open_count() == 1

    def test_flush_all_never_raises_on_bad_clock(self):
        b = LiqClusterBuilder()
        _ingest(b, 0)
        assert b.flush_all("garbage") == [] or isinstance(
            b.flush_all(T0 + 999_000), list)


# ── Builder returns row only on flush ────────────────────────────────────────

class TestRowOnlyOnFlush:
    def test_none_while_open(self):
        b = LiqClusterBuilder()
        for dt in (0, 10, 20, 30, 40, 50):
            assert _ingest(b, dt) is None

    def test_row_shape_on_flush(self):
        b = LiqClusterBuilder()
        _ingest(b, 0, notional=600_000, venue="bybit")
        _ingest(b, 30, notional=700_000, venue="aster")
        row = _ingest(b, 91, notional=10_000, venue="bybit")
        assert row is not None
        assert row["ts_start_ms"] == T0
        assert row["ts_end_ms"] == T0 + 30_000
        assert row["symbol"] == "BTC-USD"
        assert row["direction"] == "bearish"
        assert row["total_notional"] == 1_300_000.0
        assert row["tier"] == "medium"
        assert row["event_count"] == 2
        assert row["max_single_notional"] == 700_000.0


# ── venue_set accumulates distinct venues ────────────────────────────────────

class TestVenueSet:
    def test_distinct_venues_accumulate_sorted(self):
        b = LiqClusterBuilder()
        _ingest(b, 0, venue="valuechain")
        _ingest(b, 10, venue="bybit")
        _ingest(b, 20, venue="aster")
        _ingest(b, 30, venue="bybit")     # duplicate — set semantics
        row = _ingest(b, 91)
        assert row["venue_set"] == ["aster", "bybit", "valuechain"]

    def test_single_venue_set(self):
        b = LiqClusterBuilder()
        _ingest(b, 0, venue="bybit")
        _ingest(b, 10, venue="bybit")
        row = _ingest(b, 91)
        assert row["venue_set"] == ["bybit"]


# ── Non-positive notional / bad inputs ignored ───────────────────────────────

class TestInputValidation:
    def test_zero_notional_ignored(self):
        b = LiqClusterBuilder()
        assert _ingest(b, 0, notional=0.0) is None
        assert b.open_count() == 0

    def test_negative_notional_ignored(self):
        b = LiqClusterBuilder()
        assert _ingest(b, 0, notional=-50_000.0) is None
        assert b.open_count() == 0

    def test_non_positive_ts_ignored(self):
        b = LiqClusterBuilder()
        assert b.ingest(0, "BTC-USD", "bearish", 100_000.0, "bybit") is None
        assert b.ingest(-5, "BTC-USD", "bearish", 100_000.0, "bybit") is None
        assert b.open_count() == 0

    def test_empty_symbol_or_direction_ignored(self):
        b = LiqClusterBuilder()
        assert b.ingest(T0, "", "bearish", 100_000.0, "bybit") is None
        assert b.ingest(T0, "BTC-USD", "", 100_000.0, "bybit") is None
        assert b.open_count() == 0

    def test_never_raises_on_garbage(self):
        b = LiqClusterBuilder()
        assert b.ingest(None, None, None, None, None) is None
        assert b.open_count() == 0


# ── JSONL append + one-bad-line read ─────────────────────────────────────────

class TestJsonl:
    def test_append_then_read_roundtrip(self, tmp_path):
        p = str(tmp_path / "liq_clusters.jsonl")
        row = {"ts_start_ms": T0, "symbol": "BTC-USD", "tier": "medium"}
        assert append_cluster(p, row) is True
        rows = read_clusters(p)
        assert len(rows) == 1
        assert rows[0]["symbol"] == "BTC-USD"

    def test_append_is_append_only(self, tmp_path):
        p = str(tmp_path / "liq_clusters.jsonl")
        append_cluster(p, {"n": 1})
        append_cluster(p, {"n": 2})
        assert [r["n"] for r in read_clusters(p)] == [1, 2]

    def test_one_bad_line_read(self, tmp_path):
        p = tmp_path / "liq_clusters.jsonl"
        p.write_text('{"ok": 1}\nNOT JSON AT ALL\n{"ok": 2}\n{"broken": \n{"ok": 3}\n')
        rows = read_clusters(str(p))
        assert [r["ok"] for r in rows] == [1, 2, 3]

    def test_read_missing_file_empty(self, tmp_path):
        assert read_clusters(str(tmp_path / "nope.jsonl")) == []

    def test_append_never_raises_on_bad_path(self):
        assert append_cluster("/nonexistent-dir-xyz/deep/x.jsonl", {"a": 1}) is False


# ── Kill switch (LIQ_CLUSTER_ENABLED=false = module inert) ───────────────────

class TestKillSwitch:
    def test_ingest_inert_when_disabled(self, monkeypatch):
        monkeypatch.setenv("LIQ_CLUSTER_ENABLED", "false")
        b = LiqClusterBuilder()
        assert _ingest(b, 0) is None
        assert b.open_count() == 0

    def test_append_inert_when_disabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LIQ_CLUSTER_ENABLED", "false")
        p = str(tmp_path / "liq_clusters.jsonl")
        assert append_cluster(p, {"a": 1}) is False
        assert not os.path.exists(p)

    def test_enabled_default(self, monkeypatch):
        monkeypatch.delenv("LIQ_CLUSTER_ENABLED", raising=False)
        b = LiqClusterBuilder()
        _ingest(b, 0)
        assert b.open_count() == 1


# ── Analyzer pure math (tools/liq_cluster_stats.py) ──────────────────────────

class TestAnalyzerMath:
    def test_symbol_map(self):
        assert lcs.aria_to_bybit_symbol("SOL-USD") == "SOLUSDT"
        assert lcs.aria_to_bybit_symbol("BTC-USD") == "BTCUSDT"
        assert lcs.aria_to_bybit_symbol("1000PEPE-USD") == "1000PEPEUSDT"

    def test_symbol_map_unmappable(self):
        assert lcs.aria_to_bybit_symbol("SPY") is None
        assert lcs.aria_to_bybit_symbol("") is None
        assert lcs.aria_to_bybit_symbol(None) is None

    def test_signed_move_direction_semantics(self):
        # bearish (long liqs): continuation = price FALLS
        assert lcs.signed_move_pct("bearish", 100.0, 99.0) == pytest.approx(0.01)
        assert lcs.signed_move_pct("bearish", 100.0, 101.0) == pytest.approx(-0.01)
        # bullish (short liqs): continuation = price RISES
        assert lcs.signed_move_pct("bullish", 100.0, 100.5) == pytest.approx(0.005)
        assert lcs.signed_move_pct("bullish", 100.0, 99.5) == pytest.approx(-0.005)

    def test_signed_move_bad_inputs(self):
        assert lcs.signed_move_pct("bearish", 0.0, 100.0) is None
        assert lcs.signed_move_pct("sideways", 100.0, 101.0) is None

    def test_continuation_threshold(self):
        assert lcs.is_continuation(0.005) is True    # >=0.5% exactly
        assert lcs.is_continuation(0.0049) is False
        assert lcs.is_continuation(None) is False

    def test_crossover_thin_below_n5(self):
        per = {str(m): {"n": 3, "rate": 0.10} for m in (2, 4, 8, 15, 30, 45, 120)}
        assert lcs.crossover_minute(per) == "thin"

    def test_crossover_first_minute_below_half(self):
        per = {"2": {"n": 8, "rate": 0.62}, "4": {"n": 8, "rate": 0.55},
               "8": {"n": 8, "rate": 0.48}, "15": {"n": 8, "rate": 0.40},
               "30": {"n": 8, "rate": 0.30}, "45": {"n": 8, "rate": 0.30},
               "120": {"n": 8, "rate": 0.30}}
        assert lcs.crossover_minute(per) == 8

    def test_crossover_never_crosses(self):
        per = {str(m): {"n": 10, "rate": 0.75} for m in (2, 4, 8, 15, 30, 45, 120)}
        assert lcs.crossover_minute(per) is None

    def test_cluster_key_stable(self):
        rec = {"symbol": "BTC-USD", "direction": "bearish", "ts_start_ms": T0}
        assert lcs.cluster_key(rec) == f"BTC-USD|bearish|{T0}"
