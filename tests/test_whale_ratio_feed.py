"""Pins for data/whale_ratio_feed.py — Bybit account-ratio data plane.

HTTP is mocked by injecting a fake fetch callable into WhaleRatioFeed —
NO live network in tests.
"""
import json
import os

import pytest

from data.whale_ratio_feed import (
    RatioReading,
    WhaleRatioFeed,
    classify_skew,
    long_short_ratio,
    ratio_percentile,
)


def _payload(rows):
    return {"retCode": 0, "result": {"list": rows}}


def _row(symbol, buy, sell, ts_ms):
    return {"symbol": symbol, "buyRatio": str(buy), "sellRatio": str(sell),
            "timestamp": str(ts_ms)}


NOW_MS = 1_790_400_000_000  # fixed clock for all tests


def _feed(tmp_path, fetch_fn, symbols=("BTCUSDT",), now_s=NOW_MS / 1000.0):
    return WhaleRatioFeed(symbols=symbols, log_dir=str(tmp_path),
                          fetch_fn=fetch_fn, time_fn=lambda: now_s)


# ---------------------------------------------------------------- ratio math

class TestRatioMath:
    def test_basic(self):
        assert long_short_ratio(0.57, 0.43) == pytest.approx(0.57 / 0.43)

    def test_sell_zero_returns_none(self):
        assert long_short_ratio(1.0, 0.0) is None

    def test_negative_and_garbage_abstain(self):
        assert long_short_ratio(-0.5, 0.5) is None
        assert long_short_ratio("x", 0.5) is None
        assert long_short_ratio(None, 0.5) is None


# ---------------------------------------------------------------- percentile

class TestPercentile:
    def test_known_distribution(self):
        hist = [1.0, 1.1, 1.2, 1.3, 1.4]
        # current 1.4 = top of window → 4 below + half a tie = 90th
        assert ratio_percentile(hist, 1.4) == 90.0
        # current 1.0 = bottom → 10th
        assert ratio_percentile(hist, 1.0) == 10.0
        # current above everything → 100th
        assert ratio_percentile(hist, 9.9) == 100.0

    def test_thin_history_abstains(self):
        assert ratio_percentile([1.0, 1.1], 1.05) is None
        assert ratio_percentile([], 1.05) is None
        assert ratio_percentile(None, 1.05) is None

    def test_degenerate_inputs_abstain(self):
        assert ratio_percentile([1.0] * 10, 0.0) is None
        assert ratio_percentile([1.0] * 10, "x") is None


# ---------------------------------------------------------------- skew bands

class TestClassifySkew:
    def test_percentile_primary(self):
        assert classify_skew(1.01, 95.0) == "strong_long"
        assert classify_skew(1.01, 75.0) == "long"
        assert classify_skew(1.01, 50.0) == "neutral"
        assert classify_skew(1.01, 25.0) == "short"
        assert classify_skew(1.01, 5.0) == "strong_short"

    def test_percentile_outranks_absolute(self):
        # absolute bands would say strong_long, but percentile says neutral
        assert classify_skew(1.40, 50.0) == "neutral"

    def test_absolute_fallback(self):
        assert classify_skew(1.35, None) == "strong_long"
        assert classify_skew(1.20, None) == "long"
        assert classify_skew(1.05, None) == "neutral"
        assert classify_skew(0.80, None) == "short"
        assert classify_skew(0.70, None) == "strong_short"

    def test_none_ratio_neutral(self):
        assert classify_skew(None, 99.0) == "neutral"
        assert classify_skew(None, None) == "neutral"


# ---------------------------------------------------------------- fetcher

class TestFetcher:
    def test_happy_path_reading_and_history(self, tmp_path):
        rows = [_row("BTCUSDT", 0.50 + i * 0.005, 0.50 - i * 0.005,
                     NOW_MS - (23 - i) * 3600_000) for i in range(24)]
        feed = _feed(tmp_path, lambda sym: _payload(rows))
        assert feed.run() is True
        r = feed.get_reading("BTCUSDT")
        assert isinstance(r, RatioReading)
        assert r.symbol == "BTCUSDT"
        assert r.ratio == pytest.approx(0.615 / 0.385)
        assert r.long_share == pytest.approx(0.615)
        assert r.ts_ms == NOW_MS
        assert r.age_s == pytest.approx(0.0)
        # 24 hourly points in history → percentile available
        assert feed.get_percentile("BTCUSDT") == 100.0  # newest is the max

    def test_empty_list_is_dark_never_raises(self, tmp_path):
        feed = _feed(tmp_path, lambda sym: {"retCode": 0, "result": {"list": []}},
                     symbols=("FETUSDT",))
        assert feed.run() is False          # nothing updated
        assert feed.get_reading("FETUSDT") is None
        assert feed.get_percentile("FETUSDT") is None
        assert feed.is_dark("FETUSDT") is True

    def test_retcode_nonzero_isolated(self, tmp_path):
        def fetch(sym):
            if sym == "BADUSDT":
                return {"retCode": 10001, "retMsg": "symbol not supported"}
            return _payload([_row(sym, 0.6, 0.4, NOW_MS)])
        feed = _feed(tmp_path, fetch, symbols=("BADUSDT", "ETHUSDT"))
        assert feed.run() is True
        assert feed.get_reading("BADUSDT") is None
        assert feed.get_reading("ETHUSDT") is not None

    def test_fetch_exception_isolated(self, tmp_path):
        def fetch(sym):
            if sym == "XUSDT":
                raise RuntimeError("boom")
            return _payload([_row(sym, 0.6, 0.4, NOW_MS)])
        feed = _feed(tmp_path, fetch, symbols=("XUSDT", "SOLUSDT"))
        assert feed.run() is True
        assert feed.get_reading("XUSDT") is None
        assert feed.get_reading("SOLUSDT") is not None

    def test_staleness_abstain(self, tmp_path):
        old_ms = NOW_MS - 5 * 3600_000   # 5h old > 4h staleness
        feed = _feed(tmp_path, lambda sym: _payload([_row("BTCUSDT", 0.6, 0.4, old_ms)]))
        feed.run()
        assert feed.get_reading("BTCUSDT") is None
        assert feed.get_percentile("BTCUSDT") is None

    def test_atomic_cache_write_and_reload(self, tmp_path):
        feed = _feed(tmp_path, lambda sym: _payload([_row("BTCUSDT", 0.6, 0.4, NOW_MS)]))
        feed.run()
        cache = tmp_path / "whale_ratio.json"
        assert cache.exists()
        assert not (tmp_path / "whale_ratio.json.tmp").exists()  # tmp replaced
        data = json.loads(cache.read_text())
        assert data["BTCUSDT"]["ratio"] == pytest.approx(1.5)
        # a second feed instance reads the persisted cache
        feed2 = _feed(tmp_path, lambda sym: (_ for _ in ()).throw(AssertionError("no fetch")))
        assert feed2.get_reading("BTCUSDT").ratio == pytest.approx(1.5)

    def test_history_append_and_one_bad_line_read(self, tmp_path):
        feed = _feed(tmp_path, lambda sym: _payload([_row("BTCUSDT", 0.6, 0.4, NOW_MS)]))
        feed.run()
        hist = tmp_path / "whale_ratio_history.jsonl"
        lines = hist.read_text().strip().split("\n")
        assert len(lines) == 1
        assert json.loads(lines[0])["symbol"] == "BTCUSDT"
        # corrupt one line + add a second good line; reload skips only the bad one
        with open(hist, "a") as f:
            f.write("THIS IS NOT JSON\n")
            f.write(json.dumps({"symbol": "BTCUSDT", "ratio": 1.2, "ts": 0}) + "\n")
        feed2 = _feed(tmp_path, lambda sym: _payload([_row("BTCUSDT", 0.6, 0.4, NOW_MS)]))
        feed2.run()  # adds a third point → ≥5 not needed; just verify no crash + load
        feed3 = _feed(tmp_path, lambda sym: _payload([]))
        assert len(feed3._history["BTCUSDT"]) == 3  # 2 good lines + 1 from feed2.run

    def test_kill_switch_inert(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WHALE_RATIO_FEED_ENABLED", "false")
        called = []
        feed = _feed(tmp_path, lambda sym: called.append(sym) or _payload([]))
        assert feed.run() is False
        assert called == []
        assert not (tmp_path / "whale_ratio.json").exists()

    def test_injected_callable_symbol_list(self, tmp_path):
        feed = WhaleRatioFeed(symbols=lambda: ("ETHUSDT", "SOLUSDT"),
                              log_dir=str(tmp_path),
                              fetch_fn=lambda sym: _payload([_row(sym, 0.6, 0.4, NOW_MS)]),
                              time_fn=lambda: NOW_MS / 1000.0)
        assert feed.run() is True
        assert feed.get_reading("ETHUSDT") is not None
        assert feed.get_reading("SOLUSDT") is not None

    def test_long_share_guard(self, tmp_path):
        # buy+sell = 0 → long_share None, ratio still from long_short_ratio (None → dark)
        feed = _feed(tmp_path, lambda sym: _payload([_row("BTCUSDT", 0.0, 0.0, NOW_MS)]))
        assert feed.run() is False  # unparseable → dark
        assert feed.get_reading("BTCUSDT") is None
