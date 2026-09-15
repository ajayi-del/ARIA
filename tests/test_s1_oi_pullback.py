"""S1 ETH 4h OI-pullback swing brain pins (Governor 2026-09-15, B5)."""
import unittest

from intelligence.s1_oi_pullback import (
    oi_nearest, oi_change_pct, bollinger_lower, wilder_atr, funding_leg_ok,
    evaluate_entry, evaluate_kill, bracket,
)


class TestOiLookup(unittest.TestCase):
    def test_nearest_row_mixed_resolution(self):
        rows = [(1000, 10.0), (2000, 20.0), (5000, 50.0)]
        self.assertEqual(oi_nearest(rows, 4200), (5000, 50.0))
        self.assertEqual(oi_nearest(rows, 1600), (2000, 20.0))
        self.assertIsNone(oi_nearest([], 1000))

    def test_change_pct_basic(self):
        rows = [(0, 100.0), (30 * 24 * 3600 * 1000, 106.0)]
        chg = oi_change_pct(rows, 30 * 24 * 3600 * 1000)
        self.assertAlmostEqual(chg, 6.0)

    def test_change_pct_none_on_thin_coverage(self):
        # then-row inside the NEWER half of the window = measured on air
        rows = [(20 * 24 * 3600 * 1000, 100.0), (30 * 24 * 3600 * 1000, 106.0)]
        self.assertIsNone(oi_change_pct(rows, 30 * 24 * 3600 * 1000))
        self.assertIsNone(oi_change_pct([], 1000))

    def test_change_pct_none_on_zero_base(self):
        rows = [(0, 0.0), (1000, 5.0)]
        self.assertIsNone(oi_change_pct(rows, 1000))


class TestIndicators(unittest.TestCase):
    def test_bollinger_lower(self):
        closes = [100.0] * 19 + [90.0]
        bl = bollinger_lower(closes)
        self.assertIsNotNone(bl)
        self.assertLess(bl, sum(closes) / 20)
        self.assertIsNone(bollinger_lower([1.0] * 5))

    def test_bollinger_constant_series_band_at_price(self):
        self.assertAlmostEqual(bollinger_lower([50.0] * 30), 50.0)

    def test_wilder_atr_matches_structure_analyzer(self):
        import random
        random.seed(7)
        closes = [100.0]
        for _ in range(60):
            closes.append(closes[-1] * (1 + random.uniform(-0.02, 0.02)))
        highs = [c * 1.005 for c in closes]
        lows = [c * 0.995 for c in closes]
        from core.structure_analyzer import StructureAnalyzer
        sa = StructureAnalyzer.__new__(StructureAnalyzer)
        ref = sa._calculate_atr(highs, lows, closes)
        got = wilder_atr(highs, lows, closes)
        self.assertAlmostEqual(got, ref, places=9)

    def test_wilder_atr_none_on_thin(self):
        self.assertIsNone(wilder_atr([1] * 5, [1] * 5, [1] * 5))

    def test_funding_leg(self):
        self.assertTrue(funding_leg_ok([0.01, 0.0, 0.02]))
        self.assertFalse(funding_leg_ok([0.01, -0.001, 0.02]))
        self.assertIsNone(funding_leg_ok([0.01]))
        self.assertIsNone(funding_leg_ok([]))


class TestVerdicts(unittest.TestCase):
    def test_entry_all_legs_required(self):
        v = evaluate_entry(6.0, 2.1, True, 100.0, 101.0)
        self.assertTrue(v["ok"])
        for bad_oi in (None, 3.0):
            self.assertFalse(evaluate_entry(bad_oi, 2.1, True, 100.0, 101.0)["ok"])
        self.assertFalse(evaluate_entry(6.0, 1.2, True, 100.0, 101.0)["ok"])
        self.assertFalse(evaluate_entry(6.0, None, True, 100.0, 101.0)["ok"])
        self.assertFalse(evaluate_entry(6.0, 2.1, False, 100.0, 101.0)["ok"])
        self.assertFalse(evaluate_entry(6.0, 2.1, None, 100.0, 101.0)["ok"])
        self.assertFalse(evaluate_entry(6.0, 2.1, True, 102.0, 101.0)["ok"])
        self.assertFalse(evaluate_entry(6.0, 2.1, True, 100.0, None)["ok"])

    def test_entry_options_dark_does_not_block_by_default(self):
        v = evaluate_entry(6.0, 2.1, True, 100.0, 101.0)
        self.assertEqual(v["legs"]["iv_rank"], "dark")
        self.assertEqual(v["legs"]["gex"], "dark")
        self.assertTrue(v["ok"])
        import intelligence.s1_oi_pullback as m
        old = m.REQUIRE_OPTIONS_LEGS
        try:
            m.REQUIRE_OPTIONS_LEGS = True
            self.assertFalse(evaluate_entry(6.0, 2.1, True, 100.0, 101.0)["ok"])
            self.assertTrue(evaluate_entry(6.0, 2.1, True, 100.0, 101.0,
                                           iv_rank=40.0, gex="neutral")["ok"])
            self.assertFalse(evaluate_entry(6.0, 2.1, True, 100.0, 101.0,
                                            iv_rank=75.0, gex="neutral")["ok"])
            self.assertFalse(evaluate_entry(6.0, 2.1, True, 100.0, 101.0,
                                            iv_rank=40.0, gex="negative")["ok"])
        finally:
            m.REQUIRE_OPTIONS_LEGS = old

    def test_named_whale_recorded_never_blocking(self):
        v = evaluate_entry(6.0, 2.1, True, 100.0, 101.0, named_whale_net=-5.0)
        self.assertTrue(v["ok"])
        self.assertFalse(v["aux_named_whale_long"])
        self.assertIsNone(evaluate_entry(6.0, 2.1, True, 100.0, 101.0)["aux_named_whale_long"])

    def test_entry_borderline_band(self):
        v = evaluate_entry(4.95, 2.1, True, 100.0, 101.0)
        self.assertEqual(v["legs"]["oi_30d"], "borderline")
        self.assertFalse(v["ok"])
        v2 = evaluate_entry(3.0, 2.1, True, 100.0, 101.0)
        self.assertEqual(v2["legs"]["oi_30d"], "fail")

    def test_kill_legs(self):
        self.assertEqual(evaluate_kill([-0.01, -0.02], 1.0), (True, "funding_2x_negative"))
        self.assertEqual(evaluate_kill([0.01, -0.02], -4.0), (True, "oi_24h_unwind"))
        self.assertEqual(evaluate_kill([0.01, -0.02], 1.0), (False, ""))
        self.assertEqual(evaluate_kill([], -4.0), (True, "oi_24h_unwind"))

    def test_kill_etf_leg(self):
        self.assertEqual(evaluate_kill([0.01], 1.0, -500_000_000.0), (True, "etf_outflow"))
        self.assertEqual(evaluate_kill([0.01], 1.0, -400_000_000.0), (False, ""))
        self.assertEqual(evaluate_kill([0.01], 1.0, None), (False, ""))

    def test_bracket_geometry(self):
        stop, tp1, tp2 = bracket(2400.0, 100.0)
        self.assertAlmostEqual(stop, 2400.0 - 79.0)
        self.assertAlmostEqual(tp1, 2400.0 * 1.039)
        self.assertAlmostEqual(tp2, 2400.0 * 1.048)
        self.assertLess(stop, 2400.0)
        self.assertLess(tp1, tp2)


if __name__ == "__main__":
    unittest.main()
