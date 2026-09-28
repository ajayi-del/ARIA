"""Pins for data/oi_history_feed.py — Bybit daily open-interest plane.

HTTP is mocked by injecting a fake fetch callable into OIHistoryFeed —
NO live network in tests.
"""
import json
import os

import pytest

from data.oi_history_feed import OIHistoryFeed, oi_growth, parse_series


def _payload(rows):
    return {"retCode": 0, "result": {"list": rows}}


def _row(oi, ts_ms):
    return {"openInterest": str(oi), "timestamp": str(ts_ms)}


NOW_MS = 1_790_400_000_000          # fixed clock for all tests
DAY_MS = 86_400_000


def _feed(tmp_path, fetch_fn, symbols=("BTCUSDT",), now_s=NOW_MS / 1000.0):
    return OIHistoryFeed(symbols=symbols, log_dir=str(tmp_path),
                         fetch_fn=fetch_fn, time_fn=lambda: now_s)


def _fresh_rows(base=100.0, step=1.0, n=30):
    """REVERSE-chronological rows (newest first) as Bybit serves them;
    newest bar is 12h old (well inside the 48h staleness window)."""
    rows = []
    for i in range(n):
        ts = NOW_MS - 12 * 3600_000 - i * DAY_MS
        rows.append(_row(base + (n - 1 - i) * step, ts))
    return rows


# ---------------------------------------------------------------- pure brains

class TestParseSeries:
    def test_reverse_chronological_normalized(self):
        rows = [_row(130, 3000), _row(120, 2000), _row(100, 1000)]
        assert parse_series(rows) == [(1000, 100.0), (2000, 120.0), (3000, 130.0)]

    def test_bad_rows_dropped_individually(self):
        rows = [_row(100, 1000), {"openInterest": "x", "timestamp": "2000"},
                _row(110, 3000), None, _row(-5, 4000)]
        assert parse_series(rows) == [(1000, 100.0), (3000, 110.0)]

    def test_empty_and_none(self):
        assert parse_series([]) == []
        assert parse_series(None) == []


class TestOIGrowth:
    def test_known_growth(self):
        assert oi_growth([100.0, 110.0]) == pytest.approx(10.0)
        assert oi_growth([100.0, 100.0, 125.0]) == pytest.approx(25.0)
        assert oi_growth([200.0, 100.0]) == pytest.approx(-50.0)

    def test_oldest_zero_guard(self):
        assert oi_growth([0.0, 100.0]) is None

    def test_thin_window_abstains(self):
        assert oi_growth([100.0]) is None
        assert oi_growth([]) is None
        assert oi_growth(None) is None

    def test_degenerate_values_abstain(self):
        assert oi_growth([100.0, "x", 110.0]) == pytest.approx(10.0)  # bad dropped
        assert oi_growth([-1.0, 100.0]) is None                        # oldest invalid


# ---------------------------------------------------------------- fetcher

class TestFeed:
    def test_run_stores_normalized_series_and_growth(self, tmp_path):
        feed = _feed(tmp_path, lambda s: _payload(_fresh_rows()))
        assert feed.run() is True
        series = feed.get_series("BTCUSDT")
        assert series is not None
        assert series == sorted(series)                    # oldest-first
        # base 100 → 100 + 29 = 129 over the window
        assert feed.get_growth("BTCUSDT") == pytest.approx(29.0)

    def test_empty_list_is_dark_never_fatal(self, tmp_path):
        feed = _feed(tmp_path, lambda s: _payload([]))
        assert feed.run() is False
        assert feed.is_dark("BTCUSDT") is True
        assert feed.get_growth("BTCUSDT") is None

    def test_retcode_error_isolated(self, tmp_path):
        def fetch(sym):
            if sym == "ETHUSDT":
                return {"retCode": 10001, "retMsg": "bad symbol"}
            return _payload(_fresh_rows())
        feed = _feed(tmp_path, fetch, symbols=("BTCUSDT", "ETHUSDT"))
        assert feed.run() is True
        assert feed.get_growth("BTCUSDT") is not None
        assert feed.get_growth("ETHUSDT") is None

    def test_fetch_exception_isolated(self, tmp_path):
        def fetch(sym):
            if sym == "ETHUSDT":
                raise RuntimeError("boom")
            return _payload(_fresh_rows())
        feed = _feed(tmp_path, fetch, symbols=("BTCUSDT", "ETHUSDT"))
        assert feed.run() is True
        assert feed.get_growth("BTCUSDT") is not None
        assert feed.get_growth("ETHUSDT") is None

    def test_staleness_abstains(self, tmp_path):
        stale = [_row(100 + i, NOW_MS - 49 * 3600_000 - (29 - i) * DAY_MS)
                 for i in range(30)]
        feed = _feed(tmp_path, lambda s: _payload(stale))
        assert feed.run() is True
        assert feed.get_series("BTCUSDT") is None           # newest bar >48h old
        assert feed.get_growth("BTCUSDT") is None

    def test_kill_switch_inert(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OI_HISTORY_FEED_ENABLED", "false")
        feed = _feed(tmp_path, lambda s: _payload(_fresh_rows()))
        assert feed.run() is False
        assert feed.get_growth("BTCUSDT") is None
        assert not os.path.exists(os.path.join(str(tmp_path), "oi_history.json"))

    def test_atomic_cache_and_history_written(self, tmp_path):
        feed = _feed(tmp_path, lambda s: _payload(_fresh_rows()))
        assert feed.run() is True
        with open(os.path.join(str(tmp_path), "oi_history.json")) as f:
            cache = json.load(f)
        assert "BTCUSDT" in cache and len(cache["BTCUSDT"]["series"]) == 30
        with open(os.path.join(str(tmp_path), "oi_history.jsonl")) as f:
            rows = [json.loads(l) for l in f if l.strip()]
        assert len(rows) == 1 and rows[0]["growth_pct"] == pytest.approx(29.0)

    def test_cache_roundtrip_new_instance(self, tmp_path):
        _feed(tmp_path, lambda s: _payload(_fresh_rows())).run()
        # a fresh instance with a FAILING fetch still serves the cache
        def boom(s):
            raise AssertionError("network must not be touched")
        feed2 = _feed(tmp_path, boom)
        assert feed2.get_growth("BTCUSDT") == pytest.approx(29.0)

    def test_one_bad_line_history_read(self, tmp_path):
        path = os.path.join(str(tmp_path), "oi_history.jsonl")
        with open(path, "w") as f:
            f.write('{"symbol":"BTCUSDT","growth_pct":3.5}\n')
            f.write("not json at all\n")
            f.write('{"symbol":"ETHUSDT","growth_pct":"garbage"}\n')
        feed = _feed(tmp_path, lambda s: _payload([]))
        assert feed.get_growth_history("BTCUSDT") == [3.5]
        assert feed.get_growth_history("ETHUSDT") == []

    def test_symbols_callable_failure_isolated(self, tmp_path):
        def bad():
            raise RuntimeError("no universe")
        feed = OIHistoryFeed(symbols=bad, log_dir=str(tmp_path),
                             fetch_fn=lambda s: _payload(_fresh_rows()),
                             time_fn=lambda: NOW_MS / 1000.0)
        assert feed.run() is False
