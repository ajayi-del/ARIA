"""Stock carry brain pins (register Stocks 1 & 2, B4 plane 2026-09-15)."""
import unittest

from intelligence.stock_carry import (
    FUNDING_ZERO_EPS, FUNDING_NORMAL_EPS, BASIS_Z_MIN, KILL_SPREAD_BP,
    SHADOW_ONLY, funding_zero_streak, first_positive_after_streak,
    basis_episode_run, evaluate_orcl_entry, evaluate_meta_entry,
    evaluate_exit,
)


def _orcl_ok_kwargs():
    return dict(
        vol_now=300.0, vol_baseline=100.0,       # exactly 3x
        range_now=30.0, range_baseline=10.0,     # exactly 3x
        closes=[100.0] * 20 + [100.5],           # strictly > 20-bar high
        basis_zs=[0.2, 1.1, 1.3, 1.5],           # trailing run of 3
        funding_rates=[0.0, 0.0, 0.0, 0.0, 0.0],  # streak 5
    )


class TestFundingZeroStreak(unittest.TestCase):
    def test_trailing_count(self):
        self.assertEqual(funding_zero_streak([0.01, 0.0, 0.0, 0.0]), 3)
        self.assertEqual(funding_zero_streak([0.0, 0.0, 1e-4]), 0)
        self.assertEqual(funding_zero_streak([]), 0)

    def test_eps_boundary_strict(self):
        # |r| == eps is NOT zero (strict <)
        self.assertEqual(funding_zero_streak([FUNDING_ZERO_EPS]), 0)
        self.assertEqual(funding_zero_streak([-FUNDING_ZERO_EPS]), 0)
        self.assertEqual(funding_zero_streak([FUNDING_ZERO_EPS * 0.999]), 1)

    def test_none_breaks_streak(self):
        self.assertEqual(funding_zero_streak([0.0, 0.0, None, 0.0]), 1)
        self.assertEqual(funding_zero_streak([None]), 0)
        self.assertEqual(funding_zero_streak([0.0, None]), 0)


class TestFirstPositiveAfterStreak(unittest.TestCase):
    def test_fires_on_first_positive_after_7_zeros(self):
        self.assertEqual(first_positive_after_streak([0.0] * 7 + [1e-4]), 7)

    def test_no_fire_on_streak_6(self):
        self.assertIsNone(first_positive_after_streak([0.0] * 6 + [1e-4]))

    def test_none_interrupts_the_streak(self):
        # 6 zeros, a dark print, 1 zero, then positive -> window has a None
        rates = [0.0] * 6 + [None, 0.0, 1e-4]
        self.assertIsNone(first_positive_after_streak(rates))

    def test_positive_must_exceed_eps(self):
        # r == eps is not a positive print (strict >)
        self.assertIsNone(first_positive_after_streak([0.0] * 7 + [FUNDING_ZERO_EPS]))

    def test_first_qualifying_trigger_wins(self):
        rates = [0.0] * 7 + [1e-4] + [0.0] * 7 + [2e-4]
        self.assertEqual(first_positive_after_streak(rates), 7)

    def test_min_streak_parametrized(self):
        self.assertEqual(
            first_positive_after_streak([0.0] * 5 + [2e-5], min_streak=5), 5)


class TestBasisEpisodeRun(unittest.TestCase):
    def test_b4_strict_significance(self):
        # z == BASIS_Z_MIN exactly is NOT significant (B4 strict > stamp)
        self.assertEqual(basis_episode_run([BASIS_Z_MIN, BASIS_Z_MIN, BASIS_Z_MIN]), 0)
        self.assertEqual(basis_episode_run([0.1, 1.01, 1.5, 2.0]), 3)

    def test_none_breaks_run(self):
        self.assertEqual(basis_episode_run([1.5, None, 1.5, 1.5]), 2)
        self.assertEqual(basis_episode_run([1.5, 1.5, None]), 0)


class TestOrclEntry(unittest.TestCase):
    def test_all_legs_pass(self):
        v = evaluate_orcl_entry(**_orcl_ok_kwargs())
        self.assertTrue(v["ok"])
        self.assertTrue(all(s == "pass" for s in v["legs"].values()))
        self.assertTrue(v["shadow_only"])

    def test_breakout_boundary_equal_does_not_fire(self):
        kw = _orcl_ok_kwargs()
        kw["closes"] = [100.0] * 20 + [100.0]   # equal to 20-bar high
        v = evaluate_orcl_entry(**kw)
        self.assertEqual(v["legs"]["breakout"], "fail")
        self.assertFalse(v["ok"])
        kw["closes"] = [100.0] * 20 + [100.0001]  # strictly greater fires
        self.assertTrue(evaluate_orcl_entry(**kw)["ok"])

    def test_dark_legs_fail_closed(self):
        kw = _orcl_ok_kwargs()
        kw["vol_now"] = None
        v = evaluate_orcl_entry(**kw)
        self.assertEqual(v["legs"]["volume_surge"], "dark")
        self.assertFalse(v["ok"])
        kw = _orcl_ok_kwargs()
        kw["range_baseline"] = 0.0
        self.assertEqual(evaluate_orcl_entry(**kw)["legs"]["range_surge"], "dark")
        kw = _orcl_ok_kwargs()
        kw["closes"] = [100.0] * 10               # insufficient bars
        self.assertEqual(evaluate_orcl_entry(**kw)["legs"]["breakout"], "dark")
        kw = _orcl_ok_kwargs()
        kw["funding_rates"] = [0.0] * 4 + [None]  # newest print dark
        self.assertEqual(
            evaluate_orcl_entry(**kw)["legs"]["funding_zero_streak"], "dark")

    def test_failing_legs(self):
        kw = _orcl_ok_kwargs()
        kw["vol_now"] = 299.9                     # below 3x
        self.assertFalse(evaluate_orcl_entry(**kw)["ok"])
        kw = _orcl_ok_kwargs()
        kw["basis_zs"] = [1.2, 1.3]               # run of 2 < 3
        self.assertEqual(evaluate_orcl_entry(**kw)["legs"]["basis_episode"], "fail")
        kw = _orcl_ok_kwargs()
        kw["funding_rates"] = [0.0] * 4           # streak 4 < 5
        self.assertFalse(evaluate_orcl_entry(**kw)["ok"])


class TestMetaEntry(unittest.TestCase):
    def test_fires_on_trigger_bar(self):
        v = evaluate_meta_entry([0.0] * 7 + [1e-4])
        self.assertTrue(v["ok"])
        self.assertEqual(v["trigger_index"], 7)

    def test_stale_trigger_never_refires(self):
        v = evaluate_meta_entry([0.0] * 7 + [1e-4, 0.0])
        self.assertFalse(v["ok"])
        self.assertEqual(v["trigger_index"], 7)   # known but not newest

    def test_streak_6_no_fire(self):
        self.assertFalse(evaluate_meta_entry([0.0] * 6 + [1e-4])["ok"])

    def test_none_interruption_no_fire(self):
        rates = [0.0] * 6 + [None, 0.0, 1e-4]
        self.assertFalse(evaluate_meta_entry(rates)["ok"])


class TestExits(unittest.TestCase):
    def test_orcl_funding_returns_zero_exits(self):
        self.assertEqual(evaluate_exit("orcl", [1e-4, 0.0]),
                         (True, "funding_returned_zero"))

    def test_orcl_funding_flips_negative_exits(self):
        self.assertEqual(evaluate_exit("orcl", [-1e-4]),
                         (True, "funding_flipped_negative"))

    def test_orcl_kills(self):
        self.assertEqual(evaluate_exit("orcl", [1e-4], basis_z=-0.1),
                         (True, "basis_z_flipped_negative"))
        self.assertEqual(evaluate_exit("orcl", [1e-4], spread_bp=8.0),
                         (True, "spread_widened"))

    def test_spread_none_is_no_opinion_and_boundary(self):
        self.assertEqual(evaluate_exit("orcl", [1e-4], spread_bp=None),
                         (False, ""))
        # exactly KILL_SPREAD_BP is NOT a kill (strict >)
        self.assertEqual(evaluate_exit("orcl", [1e-4], spread_bp=KILL_SPREAD_BP),
                         (False, ""))
        self.assertEqual(evaluate_exit("orcl", [1e-4]), (False, ""))

    def test_orcl_none_funding_no_opinion(self):
        self.assertEqual(evaluate_exit("orcl", [1e-4, None]), (False, ""))

    def test_meta_zero_2x_exit(self):
        self.assertEqual(evaluate_exit("meta", [1e-4, 0.0, 0.0]),
                         (True, "funding_zero_2x"))
        self.assertEqual(evaluate_exit("meta", [1e-4, 0.0]), (False, ""))

    def test_meta_regime_normalized_kill(self):
        self.assertEqual(
            evaluate_exit("meta", [FUNDING_NORMAL_EPS, -6e-5, 7e-5]),
            (True, "regime_normalized"))
        # boundary: |r| == normal_eps counts (>=)
        self.assertEqual(
            evaluate_exit("meta", [FUNDING_NORMAL_EPS] * 3),
            (True, "regime_normalized"))
        # None breaks the normalization run
        self.assertEqual(
            evaluate_exit("meta", [6e-5, None, 6e-5, 6e-5]), (False, ""))

    def test_meta_spread_kill_and_unknown_strategy(self):
        self.assertEqual(evaluate_exit("meta", [1e-4], spread_bp=7.5),
                         (True, "spread_widened"))
        with self.assertRaises(ValueError):
            evaluate_exit("tsm", [0.0])

    def test_shadow_only_declared(self):
        self.assertTrue(SHADOW_ONLY)


if __name__ == "__main__":
    unittest.main()
