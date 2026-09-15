"""S3 ETH/BTC ratio-spread brain pins (Governor 2026-09-15, BYBIT-MAP)."""
import unittest

from intelligence.s3_ethbtc_spread import (
    ratio_vwap, fib_r1, evaluate_entry, evaluate_exit, evaluate_kill,
    ETH_OI_30D_MIN_PCT, BTC_OI_30D_MAX_PCT, MIN_LS_SPREAD,
    MIN_TOP_TRADER_RATIO, REQUIRE_TOP_TRADER, KILL_BTC_ETF_INFLOW_USD,
    SHADOW_ONLY,
)


def _good_entry(**over):
    kw = dict(eth_oi_30d=7.0, btc_oi_30d=-12.0, eth_ls_ratio=1.6,
              btc_ls_ratio=1.0, top_trader_ratio=1.8, ratio_now=0.050,
              vwap=0.051)
    kw.update(over)
    return evaluate_entry(**kw)


class TestRatioVwap(unittest.TestCase):
    def test_volume_weighted(self):
        v = ratio_vwap([0.05, 0.06], [1.0, 3.0])
        self.assertAlmostEqual(v, (0.05 * 1.0 + 0.06 * 3.0) / 4.0)

    def test_no_volume_falls_back_to_mean(self):
        self.assertAlmostEqual(ratio_vwap([0.05, 0.07]), 0.06)

    def test_zero_sum_volume_falls_back_to_mean(self):
        self.assertAlmostEqual(ratio_vwap([0.05, 0.07], [0.0, 0.0]), 0.06)

    def test_none_on_empty_or_mismatch(self):
        self.assertIsNone(ratio_vwap([]))
        self.assertIsNone(ratio_vwap([0.05, 0.06], [1.0]))


class TestFibR1(unittest.TestCase):
    def test_level_arithmetic(self):
        highs = [0.060] * 29 + [0.062]
        lows = [0.050] + [0.060] * 29
        r1 = fib_r1(highs, lows, lookback=30)
        self.assertAlmostEqual(r1, 0.050 + 0.236 * (0.062 - 0.050))

    def test_uses_last_lookback_only(self):
        highs = [9.0] + [0.062] * 30
        lows = [0.001] + [0.050] + [0.060] * 29
        r1 = fib_r1(highs, lows, lookback=30)
        self.assertAlmostEqual(r1, 0.050 + 0.236 * (0.062 - 0.050))

    def test_none_on_thin_series(self):
        self.assertIsNone(fib_r1([1.0] * 5, [1.0] * 5, lookback=30))
        self.assertIsNone(fib_r1([], []))


class TestEntryLegs(unittest.TestCase):
    def test_all_legs_pass(self):
        v = _good_entry()
        self.assertTrue(v["ok"])
        self.assertAlmostEqual(v["ls_spread"], 0.6)

    def test_each_required_leg_blocks_independently(self):
        self.assertFalse(_good_entry(eth_oi_30d=4.9)["ok"])
        self.assertFalse(_good_entry(btc_oi_30d=-9.9)["ok"])
        self.assertFalse(_good_entry(eth_ls_ratio=1.2, btc_ls_ratio=1.0)["ok"])
        self.assertFalse(_good_entry(ratio_now=0.052)["ok"])   # above vwap
        self.assertFalse(_good_entry(vwap=None)["ok"])
        self.assertFalse(_good_entry(ratio_now=None)["ok"])

    def test_dark_required_legs_fail_closed(self):
        self.assertEqual(_good_entry(eth_oi_30d=None)["legs"]["eth_oi_30d"], "dark")
        self.assertFalse(_good_entry(eth_oi_30d=None)["ok"])
        self.assertEqual(_good_entry(btc_oi_30d=None)["legs"]["btc_oi_30d"], "dark")
        self.assertEqual(_good_entry(eth_ls_ratio=None)["legs"]["ls_spread"], "dark")
        self.assertFalse(_good_entry(eth_ls_ratio=None)["ok"])
        self.assertEqual(_good_entry(vwap=None)["legs"]["ratio_vs_vwap"], "dark")

    def test_top_trader_dark_does_not_block(self):
        v = _good_entry(top_trader_ratio=None)
        self.assertEqual(v["legs"]["top_trader"], "dark")
        self.assertTrue(v["ok"])
        self.assertFalse(REQUIRE_TOP_TRADER)

    def test_top_trader_present_and_failing_blocks(self):
        v = _good_entry(top_trader_ratio=MIN_TOP_TRADER_RATIO - 0.01)
        self.assertEqual(v["legs"]["top_trader"], "fail")
        self.assertFalse(v["ok"])
        v2 = _good_entry(top_trader_ratio=MIN_TOP_TRADER_RATIO)
        self.assertEqual(v2["legs"]["top_trader"], "pass")

    def test_boundary_thresholds(self):
        self.assertEqual(
            _good_entry(eth_oi_30d=ETH_OI_30D_MIN_PCT)["legs"]["eth_oi_30d"],
            "pass")
        self.assertEqual(
            _good_entry(btc_oi_30d=BTC_OI_30D_MAX_PCT)["legs"]["btc_oi_30d"],
            "pass")
        self.assertEqual(
            _good_entry(eth_ls_ratio=1.0 + MIN_LS_SPREAD,
                        btc_ls_ratio=1.0)["legs"]["ls_spread"], "pass")

    def test_shadow_only_declared(self):
        self.assertTrue(SHADOW_ONLY)
        self.assertTrue(_good_entry()["shadow_only"])


class TestExitAndKill(unittest.TestCase):
    def test_fib_r1_exit(self):
        self.assertEqual(evaluate_exit(0.056, 0.055, [0.01], [0.01]),
                         (True, "fib_r1_reached"))
        self.assertEqual(evaluate_exit(0.054, 0.055, [0.01], [0.01]),
                         (False, ""))

    def test_funding_flip_exits(self):
        self.assertEqual(evaluate_exit(0.054, 0.055, [-0.01, -0.02], [0.01]),
                         (True, "eth_funding_2x_negative"))
        self.assertEqual(evaluate_exit(0.054, 0.055, [0.01], [-0.01, -0.02]),
                         (True, "btc_funding_2x_negative"))
        self.assertEqual(evaluate_exit(0.054, 0.055, [0.01, -0.02], [0.01]),
                         (False, ""))
        self.assertEqual(evaluate_exit(0.054, 0.055, [-0.02], [0.01]),
                         (False, ""))

    def test_dark_r1_does_not_block_funding_exit(self):
        self.assertEqual(evaluate_exit(None, None, [-0.01, -0.02], [0.01]),
                         (True, "eth_funding_2x_negative"))
        self.assertEqual(evaluate_exit(None, None, [0.01], [0.01]),
                         (False, ""))

    def test_kill_mega_inflow(self):
        self.assertEqual(evaluate_kill(KILL_BTC_ETF_INFLOW_USD + 1.0),
                         (True, "btc_etf_mega_inflow"))
        self.assertEqual(evaluate_kill(KILL_BTC_ETF_INFLOW_USD), (False, ""))
        self.assertEqual(evaluate_kill(None), (False, ""))
        self.assertEqual(evaluate_kill(-999_000_000.0), (False, ""))


if __name__ == "__main__":
    unittest.main()
