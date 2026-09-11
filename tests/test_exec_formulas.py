"""tests/test_exec_formulas.py — pins for the canon execution-formula
measurement plane (intelligence/exec_formulas.py). Measurement only; the
module is zero-I/O and fail-open (None, never raise).
"""
import math
import os
import random

import pytest

from intelligence.exec_formulas import (
    amihud_illiq,
    avellaneda_stoikov_reservation,
    corwin_schultz_spread,
    estimate_symbol,
    kyle_lambda,
    realized_skew,
)


def _trend_series(n=120, start=100.0, drift=0.1, vol=1000.0, seed=7):
    """Trending up series with volume — signed flow and dPrice agree."""
    rng = random.Random(seed)
    closes, volumes, opens = [], [], []
    px = start
    for _ in range(n):
        o = px
        px = px + drift + rng.uniform(-0.02, 0.02)
        opens.append(o)
        closes.append(px)
        volumes.append(vol + rng.uniform(-50, 50))
    return closes, volumes, opens


class TestKyleLambda:
    def test_trending_series_positive_lambda(self):
        closes, volumes, opens = _trend_series()
        lam = kyle_lambda(closes, volumes, opens=opens)
        assert lam is not None
        assert lam > 0          # up-drift on up-signed flow = positive impact

    def test_constant_price_series_none(self):
        closes = [100.0] * 80
        volumes = [1000.0] * 80
        # zero variance in dPrice AND signed flow collapses to zero
        assert kyle_lambda(closes, volumes) is None

    def test_insufficient_bars_none(self):
        closes, volumes, _ = _trend_series(n=20)
        assert kyle_lambda(closes, volumes) is None

    def test_zero_volume_none(self):
        closes, _, _ = _trend_series()
        assert kyle_lambda(closes, [0.0] * len(closes)) is None


class TestAmihud:
    def test_constant_price_zero_or_none(self):
        closes = [100.0] * 80
        volumes = [1000.0] * 80
        out = amihud_illiq(closes, volumes)
        # |ret| = 0 every bar -> mean 0.0 is a valid, documented read
        assert out == 0.0

    def test_moving_series_positive(self):
        closes, volumes, _ = _trend_series()
        out = amihud_illiq(closes, volumes)
        assert out is not None and out > 0

    def test_insufficient_bars_none(self):
        closes, volumes, _ = _trend_series(n=10)
        assert amihud_illiq(closes, volumes) is None


class TestCorwinSchultz:
    def test_known_synthetic_path(self):
        # Deterministic path: H/L = 1.002 per bar with 2-bar range 1.003.
        # Hand-computed CS spread for this geometry below (1e-6 tolerance).
        highs, lows = [], []
        px = 100.0
        for i in range(20):
            lows.append(px)
            highs.append(px * 1.002)
            px *= 1.001
        out = corwin_schultz_spread(highs, lows)
        # beta = 2*ln(1.002)^2 ; gamma = ln(1.003)^2 (2-bar range 1.001*1.002)
        beta = 2.0 * math.log(1.002) ** 2
        gamma = math.log(1.001 * 1.002) ** 2
        denom = 3.0 - 2.0 * math.sqrt(2.0)
        alpha = max((math.sqrt(2 * beta) - math.sqrt(beta)) / denom
                    - math.sqrt(gamma / denom), 0.0)
        expected = 2.0 * (math.exp(alpha) - 1.0) / (1.0 + math.exp(alpha))
        assert out is not None
        assert abs(out - expected) < 1e-6

    def test_spread_is_fraction(self):
        rng = random.Random(3)
        highs, lows = [], []
        px = 50.0
        for _ in range(20):
            h = px * (1 + rng.uniform(0.001, 0.004))
            l = px * (1 - rng.uniform(0.001, 0.004))
            highs.append(h)
            lows.append(l)
            px *= 1 + rng.uniform(-0.002, 0.002)
        out = corwin_schultz_spread(highs, lows)
        assert out is not None
        assert 0.0 <= out < 0.05      # sane fractional-spread band

    def test_degenerate_none(self):
        assert corwin_schultz_spread([100.0], [99.0]) is None
        assert corwin_schultz_spread([0.0] * 20, [0.0] * 20) is None


class TestRealizedSkew:
    def test_symmetric_series_near_zero(self):
        rng = random.Random(11)
        px = 100.0
        closes = []
        for _ in range(120):
            px *= 1 + rng.gauss(0.0, 0.001)
            closes.append(px)
        out = realized_skew(closes)
        assert out is not None
        assert abs(out) < 0.5

    def test_crash_shaped_negative(self):
        rng = random.Random(13)
        px = 100.0
        closes = []
        for i in range(120):
            if i % 20 == 19:
                px *= 0.97            # periodic -3% crash bar
            else:
                px *= 1 + rng.gauss(0.0002, 0.0005)
            closes.append(px)
        out = realized_skew(closes)
        assert out is not None
        assert out < 0

    def test_constant_series_none(self):
        assert realized_skew([100.0] * 80) is None

    def test_insufficient_none(self):
        assert realized_skew([100.0, 100.1, 100.2]) is None


class TestAvellanedaStoikov:
    def test_long_inventory_pushes_below_mid(self):
        r = avellaneda_stoikov_reservation(100.0, 500.0, 0.01,
                                           max_inventory_usd=1000.0)
        assert r < 100.0

    def test_short_inventory_pushes_above_mid(self):
        r = avellaneda_stoikov_reservation(100.0, -500.0, 0.01,
                                           max_inventory_usd=1000.0)
        assert r > 100.0

    def test_zero_inventory_is_mid(self):
        assert avellaneda_stoikov_reservation(100.0, 0.0, 0.01) == 100.0

    def test_degenerate_returns_mid(self):
        assert avellaneda_stoikov_reservation(0.0, 500.0, 0.01) == 0.0
        assert avellaneda_stoikov_reservation(100.0, 500.0, 0.0) == 100.0

    def test_inventory_ratio_clamped(self):
        r = avellaneda_stoikov_reservation(100.0, 1e9, 0.01,
                                           gamma=0.1, horizon_s=60.0,
                                           max_inventory_usd=1000.0)
        assert abs(r - (100.0 - 1.0 * 0.1 * 0.01 ** 2 * 60.0)) < 1e-12


class TestEstimateSymbol:
    def test_shape_dict_bars(self):
        rng = random.Random(17)
        px = 100.0
        bars = []
        for _ in range(120):
            o = px
            px *= 1 + rng.gauss(0.0003, 0.001)
            bars.append({"open": o, "high": max(o, px) * 1.001,
                         "low": min(o, px) * 0.999, "close": px,
                         "volume": 1000.0})
        out = estimate_symbol(bars)
        assert set(out.keys()) == {"kyle_lambda", "amihud_illiq",
                                   "cs_spread", "realized_skew"}
        assert out["cs_spread"] is not None
        assert out["realized_skew"] is not None

    def test_shape_object_bars(self):
        class Bar:
            def __init__(self, o, h, l, c, v):
                self.open, self.high, self.low, self.close, self.volume = o, h, l, c, v
        bars = [Bar(100, 100.5, 99.5, 100.1, 1000.0) for _ in range(120)]
        out = estimate_symbol(bars)
        assert set(out.keys()) == {"kyle_lambda", "amihud_illiq",
                                   "cs_spread", "realized_skew"}

    def test_empty_bars_all_none(self):
        out = estimate_symbol([])
        assert all(v is None for v in out.values())


class TestWiring:
    def test_loop_registered_and_knobs_present(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "main.py")).read()
        assert "_supervise(_exec_formulas_loop" in src
        assert "exec_formulas_published" in src
        assert "exec_formulas_loop_error" in src
        cfg = open(os.path.join(root, "core", "config.py")).read()
        for knob in ("exec_formulas_enabled", "exec_formulas_window",
                     "exec_formulas_publish_s"):
            assert knob in cfg
