"""Tests for intelligence/hurst.py + tools/hurst_census.py.

Synthetic series with KNOWN persistence classes — seeded, deterministic,
no network. The census parser is tested against a fixture payload built
from the documented Bybit V5 kline shape.
"""
import importlib.util
import math
import os
import random

import pytest

from intelligence.hurst import classify, hurst_dfa, hurst_rs

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools", "hurst_census.py")
_spec = importlib.util.spec_from_file_location("hurst_census", _PATH)
census = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(census)


# ── Synthetic generators (stdlib, seeded) ────────────────────────────────────

def _fbm(n: int, hurst: float, seed: int) -> list:
    """Fractional-Brownian-ish path via random midpoint displacement.
    Level-j noise scales as 2^(-jH); deterministic under the seed."""
    rnd = random.Random(seed)
    k = max(1, math.ceil(math.log2(max(2, n - 1))))
    m = 2 ** k + 1
    x = [0.0] * m
    x[-1] = rnd.gauss(0.0, 1.0)
    step = m - 1
    scale = 1.0
    while step > 1:
        half = step // 2
        for i in range(half, m - 1, step):
            x[i] = 0.5 * (x[i - half] + x[i + half]) + scale * rnd.gauss(0.0, 1.0)
        scale *= 2.0 ** (-hurst)
        step = half
    return x[:n]


def _ou(n: int, theta: float, sigma: float, seed: int) -> list:
    """Ornstein-Uhlenbeck path: dx = -theta*x*dt + sigma*dW (dt=1)."""
    rnd = random.Random(seed)
    x = [0.0]
    for _ in range(n - 1):
        x.append(x[-1] - theta * x[-1] + sigma * rnd.gauss(0.0, 1.0))
    return x


def _gbm(n: int, sigma: float, seed: int) -> list:
    """Pure random walk in log space (geometric Brownian motion)."""
    rnd = random.Random(seed)
    x = [0.0]
    for _ in range(n - 1):
        x.append(x[-1] + sigma * rnd.gauss(0.0, 1.0))
    return x


def _closes(path: list) -> list:
    """Map a zero-mean path to positive pseudo-prices."""
    return [100.0 * math.exp(0.01 * v) for v in path]


N = 2048


# ── Estimator recovery on known classes ──────────────────────────────────────

class TestEstimatorRecovery:
    def test_persistent_series_classifies_trendable(self):
        closes = _closes(_fbm(N, hurst=0.8, seed=42))
        h = hurst_rs(closes)
        h2 = hurst_dfa(closes)
        assert h is not None and h > 0.55
        assert h2 is not None and h2 > 0.55
        assert classify(h, h2)["class"] == "trendable"

    def test_ou_series_classifies_mean_reverting(self):
        closes = _closes(_ou(N, theta=0.9, sigma=1.0, seed=7))
        h = hurst_rs(closes)
        assert h is not None and h < 0.45
        assert classify(h, hurst_dfa(closes))["class"] == "mean_reverting"

    def test_antipersistent_fbm_classifies_mean_reverting(self):
        closes = _closes(_fbm(N, hurst=0.25, seed=11))
        h = hurst_rs(closes)
        assert h is not None and h < 0.45
        assert classify(h, hurst_dfa(closes))["class"] == "mean_reverting"

    def test_gbm_walk_classifies_random_walk(self):
        closes = _closes(_gbm(N, sigma=1.0, seed=99))
        h = hurst_rs(closes)
        assert h is not None and 0.40 < h < 0.60
        verdict = classify(h, hurst_dfa(closes))
        assert verdict["class"] in ("random_walk", "unknown")  # never a fake class

    def test_newest_tail_drives_the_estimate(self):
        # REGIME doctrine: the estimator measures the CURRENT window. A strong
        # deterministic trend confined to the NEWEST 23% of the series must
        # outscore the identical trend confined to the OLDEST 23% — under
        # head-anchored chunking the newest tail fell OUT of the largest
        # window (h_rs read 0.02 newest vs 0.83 oldest on this exact fixture).
        def closes_from_returns(rs):
            px = [100.0]
            for r in rs:
                px.append(px[-1] * math.exp(r))
            return px

        n = 999
        noise = [0.001 * math.sin(i * 1.7) for i in range(n)]
        trend = [0.004 + 0.0008 * math.sin(i * 2.3) for i in range(n)]
        newest_trend = closes_from_returns(noise[:768] + trend[768:])
        oldest_trend = closes_from_returns(trend[:231] + noise[231:])
        h_new = hurst_rs(newest_trend)
        h_old = hurst_rs(oldest_trend)
        assert h_new is not None and h_old is not None
        assert h_new > h_old
        assert h_new > 0.55      # the current regime reads persistent
        assert hurst_dfa(newest_trend) > hurst_dfa(oldest_trend)


# ── Degenerate inputs ────────────────────────────────────────────────────────

class TestDegenerate:
    def test_short_series_returns_none(self):
        assert hurst_rs([100.0, 101.0, 100.5]) is None
        assert hurst_dfa([100.0 + i for i in range(150)]) is None

    def test_zero_variance_returns_none(self):
        flat = [100.0] * 2000
        assert hurst_rs(flat) is None
        assert hurst_dfa(flat) is None

    def test_bad_prices_return_none(self):
        assert hurst_rs([100.0] * 500 + [0.0] * 500) is None
        assert hurst_rs(["abc"] * 500) is None
        assert hurst_rs(None) is None

    def test_classify_none_is_unknown(self):
        assert classify(None)["class"] == "unknown"
        assert classify(None)["confidence"] == 0.0


# ── Classifier discipline (Aronson: state uncertainty, never fake precision) ─

class TestClassifier:
    def test_clear_bands(self):
        assert classify(0.70)["class"] == "trendable"
        assert classify(0.30)["class"] == "mean_reverting"
        assert classify(0.50)["class"] == "random_walk"

    def test_estimator_disagreement_widens_to_unknown(self):
        # R/S says trendable, DFA says anti-persistent: disagreement 0.17
        # widens the dead zone past the primary reading.
        assert classify(0.60, h2=0.43)["class"] == "unknown"
        # Small disagreement does not widen.
        assert classify(0.62, h2=0.59)["class"] == "trendable"

    def test_ci_straddle_is_unknown(self):
        # The doctrine's own example: 0.53 ± 0.06 is unknown, not trendable.
        assert classify(0.53, ci=(0.47, 0.59))["class"] == "unknown"
        assert classify(0.62, ci=(0.57, 0.67))["class"] == "trendable"

    def test_wide_ci_is_unknown(self):
        assert classify(0.62, ci=(0.40, 0.65))["class"] == "unknown"

    def test_confidence_bounded(self):
        for h in (0.3, 0.5, 0.7):
            c = classify(h)["confidence"]
            assert 0.0 <= c <= 1.0


# ── Census parser (fixture payload, documented Bybit shape, no network) ──────

def _kline_payload(n: int = 1000) -> dict:
    rnd = random.Random(5)
    px = 60000.0
    rows = []
    t0 = 1757900000000
    for i in range(n):
        px *= 1.0 + 0.001 * rnd.gauss(0.0, 1.0)
        rows.append([str(t0 + i * 900000), f"{px:.1f}", f"{px * 1.001:.1f}",
                     f"{px * 0.999:.1f}", f"{px:.1f}", "12.5", "750000.0"])
    rows.reverse()  # API serves newest-first
    return {"retCode": 0, "retMsg": "OK",
            "result": {"category": "linear", "symbol": "BTCUSDT", "list": rows},
            "retExtInfo": {}, "time": t0 + n * 900000}


class TestCensusParser:
    def test_parse_documented_shape_oldest_first(self):
        payload = _kline_payload(50)
        closes = census.parse_klines(payload)
        assert len(closes) == 50
        assert all(c > 0 for c in closes)
        raw = payload["result"]["list"]
        assert closes[0] == float(raw[-1][4])   # reversed to oldest-first
        assert closes[-1] == float(raw[0][4])

    def test_parse_rejects_contract_deviations(self):
        with pytest.raises(ValueError):
            census.parse_klines({"retCode": 10001, "retMsg": "symbol not found",
                                 "result": {"list": []}})
        with pytest.raises(ValueError):
            census.parse_klines({"retCode": 0, "result": {"list": []}})
        with pytest.raises(ValueError):
            census.parse_klines({"retCode": 0, "result": {"list": [["1", "2"]]}})

    def test_census_row_end_to_end(self):
        closes = census.parse_klines(_kline_payload(1000))
        row = census.census_row("BTC-USD", closes)
        assert row["error"] is None
        assert row["bars"] == 1000
        assert row["h_rs"] is not None
        assert row["class"] in ("trendable", "mean_reverting",
                                "random_walk", "unknown")

    def test_bybit_symbol_map(self):
        assert census.bybit_symbol("BTC-USD") == "BTCUSDT"
        assert census.bybit_symbol("1000PEPE-USD") == "1000PEPEUSDT"
