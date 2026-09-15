"""S2 BTC 4h dip-buy swing brain pins (Governor 2026-09-15, BYBIT-MAP)."""
import unittest

from intelligence.s2_btc_dipbuy import (
    fib_s1, evaluate_entry, evaluate_kill, bracket, wilder_atr,
    MIN_LS_RATIO, LS_BORDERLINE, OI_24H_MIN_PCT, IV_RANK_MAX,
    KILL_ETF_OUTFLOW_USD, KILL_OI_30D_PCT, STOP_ATR_MULT, TP1_ATR_MULT,
    TP2_ATR_MULT, TIME_STOP_H, FUNDING_PRINTS, REQUIRE_OPTIONS_LEGS,
)

GOOD_RATES = [0.0001, 0.0, 0.0002]


def _good_entry(**over):
    kw = dict(rates=GOOD_RATES, ls_ratio=1.5, close=100.0, s1=101.0,
              iv_rank=40.0, gex="positive", oi_chg_24h=1.0)
    kw.update(over)
    return evaluate_entry(**kw)


class TestFibS1(unittest.TestCase):
    def test_level_arithmetic(self):
        highs = [100.0] * 29 + [110.0]
        lows = [90.0] + [100.0] * 29
        s1 = fib_s1(highs, lows, lookback=30)
        self.assertAlmostEqual(s1, 110.0 - 0.236 * 20.0)

    def test_uses_last_lookback_only(self):
        # an ancient extreme beyond the window must NOT move the level
        highs = [500.0] + [100.0] * 30
        lows = [1.0] + [90.0] + [100.0] * 29
        s1 = fib_s1(highs, lows, lookback=30)
        self.assertAlmostEqual(s1, 100.0 - 0.236 * 10.0)

    def test_none_on_thin_series(self):
        self.assertIsNone(fib_s1([1.0] * 10, [1.0] * 10, lookback=30))
        self.assertIsNone(fib_s1([], []))
        self.assertIsNone(fib_s1([1.0] * 30, [1.0] * 30, lookback=0))


class TestEntryLegs(unittest.TestCase):
    def test_all_legs_pass(self):
        v = _good_entry()
        self.assertTrue(v["ok"])
        self.assertTrue(all(s == "pass" for s in v["legs"].values()))

    def test_each_required_leg_blocks_independently(self):
        self.assertFalse(_good_entry(rates=[0.01, -0.01, 0.02])["ok"])
        self.assertFalse(_good_entry(ls_ratio=1.0)["ok"])
        self.assertFalse(_good_entry(close=102.0)["ok"])   # above S1
        self.assertFalse(_good_entry(s1=None)["ok"])
        self.assertFalse(_good_entry(oi_chg_24h=-5.0)["ok"])

    def test_dark_required_legs_fail_closed(self):
        self.assertEqual(_good_entry(rates=[0.01])["legs"]["funding"], "dark")
        self.assertFalse(_good_entry(rates=[0.01])["ok"])
        self.assertEqual(_good_entry(ls_ratio=None)["legs"]["ls_ratio"], "dark")
        self.assertFalse(_good_entry(ls_ratio=None)["ok"])
        self.assertEqual(_good_entry(oi_chg_24h=None)["legs"]["oi_24h"], "dark")
        self.assertFalse(_good_entry(oi_chg_24h=None)["ok"])

    def test_options_dark_does_not_block(self):
        v = _good_entry(iv_rank=None, gex=None)
        self.assertEqual(v["legs"]["iv_rank"], "dark")
        self.assertEqual(v["legs"]["gex"], "dark")
        self.assertTrue(v["ok"])
        self.assertFalse(REQUIRE_OPTIONS_LEGS)

    def test_options_present_and_failing_blocks(self):
        v = _good_entry(iv_rank=60.0)
        self.assertEqual(v["legs"]["iv_rank"], "fail")
        self.assertFalse(v["ok"])
        v2 = _good_entry(gex="negative")
        self.assertEqual(v2["legs"]["gex"], "fail")
        self.assertFalse(v2["ok"])
        # boundary: exactly at the cap passes (IV rank < 55 is the bar)
        self.assertEqual(_good_entry(iv_rank=IV_RANK_MAX)["legs"]["iv_rank"], "fail")

    def test_borderline_bands(self):
        v = _good_entry(ls_ratio=MIN_LS_RATIO - LS_BORDERLINE / 2)
        self.assertEqual(v["legs"]["ls_ratio"], "borderline")
        self.assertFalse(v["ok"])
        v2 = _good_entry(ls_ratio=MIN_LS_RATIO)
        self.assertEqual(v2["legs"]["ls_ratio"], "pass")
        v3 = _good_entry(oi_chg_24h=OI_24H_MIN_PCT - 0.05)
        self.assertEqual(v3["legs"]["oi_24h"], "borderline")
        self.assertFalse(v3["ok"])
        v4 = _good_entry(oi_chg_24h=OI_24H_MIN_PCT - 0.5)
        self.assertEqual(v4["legs"]["oi_24h"], "fail")

    def test_gex_neutral_passes(self):
        self.assertEqual(_good_entry(gex="neutral")["legs"]["gex"], "pass")


class TestKill(unittest.TestCase):
    def test_funding_two_negative(self):
        self.assertEqual(evaluate_kill([-0.01, -0.02], None, None),
                         (True, "funding_2x_negative"))
        self.assertEqual(evaluate_kill([0.01, -0.02], None, None), (False, ""))
        self.assertEqual(evaluate_kill([-0.02], None, None), (False, ""))

    def test_etf_outflow(self):
        self.assertEqual(
            evaluate_kill([0.0], KILL_ETF_OUTFLOW_USD - 1.0, None),
            (True, "etf_outflow"))
        self.assertEqual(
            evaluate_kill([0.0], KILL_ETF_OUTFLOW_USD, None), (False, ""))

    def test_etf_none_is_no_opinion(self):
        self.assertEqual(evaluate_kill([0.0, 0.0], None, None), (False, ""))

    def test_oi_30d_collapse(self):
        self.assertEqual(evaluate_kill([0.0], None, KILL_OI_30D_PCT - 0.1),
                         (True, "oi_30d_collapse"))
        self.assertEqual(evaluate_kill([0.0], None, KILL_OI_30D_PCT),
                         (False, ""))
        self.assertEqual(evaluate_kill([0.0], None, None), (False, ""))


class TestBracketAndConstants(unittest.TestCase):
    def test_bracket_geometry(self):
        stop, tp1, tp2 = bracket(100.0, 10.0)
        self.assertAlmostEqual(stop, 100.0 - STOP_ATR_MULT * 10.0)
        self.assertAlmostEqual(tp1, 100.0 + TP1_ATR_MULT * 10.0)
        self.assertAlmostEqual(tp2, 100.0 + TP2_ATR_MULT * 10.0)
        self.assertLess(stop, 100.0)
        self.assertLess(tp1, tp2)

    def test_spec_constants_pinned(self):
        self.assertEqual(TP1_ATR_MULT, 3.40)
        self.assertEqual(TP2_ATR_MULT, 4.09)
        self.assertEqual(STOP_ATR_MULT, 0.73)
        self.assertEqual(TIME_STOP_H, 48)
        self.assertEqual(FUNDING_PRINTS, 3)

    def test_wilder_atr_reexported_single_source(self):
        from intelligence.s1_oi_pullback import wilder_atr as ref
        self.assertIs(wilder_atr, ref)


if __name__ == "__main__":
    unittest.main()
