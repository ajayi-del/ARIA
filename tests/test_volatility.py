"""Pins for Yang-Zhang vol + Lo-MacKinlay variance ratio."""
import math
import random
import unittest
from types import SimpleNamespace

from intelligence.volatility import (
    yang_zhang_pct, variance_ratio, vr_class,
    VR_TREND_THRESHOLD, VR_MR_THRESHOLD,
)


def _c(o, h, l, c):
    return SimpleNamespace(open=o, high=h, low=l, close=c)


def _flat_candles(n=25, px=100.0, rng=0.005):
    out = []
    p = px
    for _ in range(n):
        c = p * (1 + rng)
        out.append(_c(p, max(p, c) * 1.001, min(p, c) * 0.999, c))
        p = c
    return out


class TestYangZhang(unittest.TestCase):
    def test_flat_series_has_small_vol(self):
        yz = yang_zhang_pct(_flat_candles())
        self.assertIsNotNone(yz)
        self.assertLess(yz, 0.01)

    def test_jump_series_has_larger_vol(self):
        candles = []
        p = 100.0
        random.seed(7)
        for _ in range(25):
            c = p * (1 + random.choice([-0.03, 0.03]))
            candles.append(_c(p, max(p, c) * 1.01, min(p, c) * 0.99, c))
            p = c
        yz = yang_zhang_pct(candles)
        self.assertGreater(yz, 0.02)

    def test_gap_overnight_component_priced(self):
        # open gaps of VARYING size with quiet intraday paths — the overnight
        # variance term must price the jump risk a TR-based measure misses
        candles = []
        p = 100.0
        random.seed(5)
        for _ in range(30):
            o = p * (1 + random.gauss(0, 0.02))
            c = o * (1 + random.gauss(0, 0.001))
            candles.append(_c(o, max(o, c) * 1.0005, min(o, c) * 0.9995, c))
            p = c
        yz = yang_zhang_pct(candles)
        self.assertIsNotNone(yz)
        self.assertGreater(yz, 0.01)

    def test_too_few_candles_none(self):
        self.assertIsNone(yang_zhang_pct(_flat_candles(n=10)))

    def test_garbage_none(self):
        self.assertIsNone(yang_zhang_pct([object()] * 30))
        self.assertIsNone(yang_zhang_pct([_c(0, -1, -2, -3)] * 30))


class TestVarianceRatio(unittest.TestCase):
    def test_trending_series_vr_above_1(self):
        # positively autocorrelated returns (momentum), not mere drift —
        # drift alone is VR ≈ 1 by construction
        random.seed(11)
        px, p, r = [], 100.0, 0.0
        for _ in range(120):
            r = 0.75 * r + random.gauss(0, 0.005)
            p *= 1 + r
            px.append(p)
        vr = variance_ratio(px, q=8)
        self.assertIsNotNone(vr)
        self.assertGreater(vr, 1.0)

    def test_mean_reverting_series_vr_below_1(self):
        px = [100.0 + (2.0 if i % 2 == 0 else -2.0) for i in range(120)]
        vr = variance_ratio(px, q=8)
        self.assertIsNotNone(vr)
        self.assertLess(vr, 1.0)

    def test_random_walk_vr_near_1(self):
        random.seed(3)
        px, p = [], 100.0
        for _ in range(120):
            p *= 1 + random.gauss(0, 0.01)
            px.append(p)
        vr = variance_ratio(px, q=8)
        self.assertIsNotNone(vr)
        self.assertGreater(vr, 0.6)
        self.assertLess(vr, 1.4)

    def test_thin_sample_none(self):
        self.assertIsNone(variance_ratio([1.0, 2.0, 3.0]))
        self.assertIsNone(variance_ratio([0.0, -1.0] * 60))

    def test_vr_class_thresholds(self):
        random.seed(19)
        px_t, p, r = [], 100.0, 0.0
        for _ in range(120):
            r = 0.8 * r + random.gauss(0, 0.004)
            p *= 1 + r
            px_t.append(p)
        cls, vr = vr_class(px_t)
        self.assertEqual(cls, "trend")
        self.assertGreaterEqual(vr, VR_TREND_THRESHOLD)
        px_mr = [100.0 + (2.0 if i % 2 == 0 else -2.0) for i in range(120)]
        cls2, vr2 = vr_class(px_mr)
        self.assertEqual(cls2, "mr")
        self.assertLessEqual(vr2, VR_MR_THRESHOLD)
        cls3, vr3 = vr_class([1.0, 2.0])
        self.assertEqual(cls3, "neutral")
        self.assertIsNone(vr3)


if __name__ == "__main__":
    unittest.main()
