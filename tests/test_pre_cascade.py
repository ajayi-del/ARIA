"""PRE-CASCADE regime-score brain pins (Governor 2026-09-15 directive)."""
import unittest

from intelligence.pre_cascade import (
    DARK_STREAK, THRESHOLD_SCORE, OI_WINDOW_MS,
    funding_neg_streak, oi_expanding, score_symbol, verdict,
)

H = 3600 * 1000
NOW = 6 * H


def _oi_rows(delta_pct):
    return [(0, 100.0), (NOW, 100.0 * (1 + delta_pct / 100.0))]


class TestFundingNegStreak(unittest.TestCase):
    def test_streak_zero_on_positive_latest(self):
        self.assertEqual(funding_neg_streak([-0.01, 0.005]), 0)

    def test_streak_one(self):
        self.assertEqual(funding_neg_streak([0.01, -0.002]), 1)

    def test_streak_two(self):
        self.assertEqual(funding_neg_streak([0.01, -0.001, -0.002]), 2)

    def test_streak_three_and_more(self):
        self.assertEqual(funding_neg_streak([-0.001, -0.002, -0.003]), 3)
        self.assertEqual(
            funding_neg_streak([-0.001, -0.002, -0.003, -0.004, -0.005]), 5)

    def test_exactly_zero_rate_is_not_negative(self):
        self.assertEqual(funding_neg_streak([-0.001, 0.0]), 0)
        self.assertEqual(funding_neg_streak([0.0, -0.001]), 1)

    def test_none_is_dark_sentinel(self):
        self.assertEqual(funding_neg_streak(None), DARK_STREAK)
        self.assertEqual(DARK_STREAK, -1)

    def test_empty_is_dark_sentinel(self):
        self.assertEqual(funding_neg_streak([]), DARK_STREAK)


class TestOiExpanding(unittest.TestCase):
    def test_pass_on_expansion(self):
        self.assertTrue(oi_expanding(_oi_rows(2.5), NOW))

    def test_fail_on_contraction_or_flat(self):
        self.assertFalse(oi_expanding(_oi_rows(-1.0), NOW))
        self.assertFalse(oi_expanding(_oi_rows(0.0), NOW))

    def test_dark_on_thin_coverage(self):
        # then-row sits in the NEWER half of the 6h window = measured on air
        thin = [(4 * H, 100.0), (NOW, 110.0)]
        self.assertIsNone(oi_expanding(thin, NOW))
        self.assertIsNone(oi_expanding([], NOW))

    def test_window_param_respected(self):
        rows = [(0, 100.0), (2 * H, 105.0)]
        self.assertTrue(oi_expanding(rows, 2 * H, window_ms=2 * H))
        self.assertIsNone(oi_expanding(rows, 2 * H, window_ms=OI_WINDOW_MS))


class TestScoreSymbol(unittest.TestCase):
    def test_all_legs_pass_scores_4_threshold_met(self):
        d = score_symbol([-0.001, -0.002, -0.003], _oi_rows(3.0), 2.5, NOW)
        self.assertEqual(d["score"], 4)
        self.assertTrue(d["threshold_met"])
        self.assertEqual(d["legs"], {"funding_neg": "pass",
                                     "funding_neg_2prior": "pass",
                                     "oi_expanding": "pass",
                                     "whale_ls": "pass"})
        self.assertEqual(THRESHOLD_SCORE, 4)

    def test_dark_funding_leg_makes_score_none(self):
        d = score_symbol(None, _oi_rows(3.0), 2.5, NOW)
        self.assertIsNone(d["score"])
        self.assertIsNone(d["threshold_met"])
        self.assertEqual(d["legs"]["funding_neg"], "dark")
        self.assertEqual(d["legs"]["funding_neg_2prior"], "dark")

    def test_dark_oi_leg_makes_score_none(self):
        d = score_symbol([-0.001, -0.002, -0.003], [], 2.5, NOW)
        self.assertIsNone(d["score"])
        self.assertIsNone(d["threshold_met"])
        self.assertEqual(d["legs"]["oi_expanding"], "dark")

    def test_dark_whale_leg_makes_score_none(self):
        d = score_symbol([-0.001, -0.002, -0.003], _oi_rows(3.0), None, NOW)
        self.assertIsNone(d["score"])
        self.assertIsNone(d["threshold_met"])
        self.assertEqual(d["legs"]["whale_ls"], "dark")

    def test_dark_leg_never_counted_even_with_three_measured_passes(self):
        # 3/3 measured passes is NOT a 3/4 — a missing leg is missing
        # information, fail-closed.
        d = score_symbol([-0.001, -0.002, -0.003], _oi_rows(3.0), None, NOW)
        self.assertIsNone(d["score"])
        self.assertNotEqual(d["score"], 3)

    def test_ls_ratio_exactly_1_8_fails_strictly_greater(self):
        d = score_symbol([-0.001, -0.002, -0.003], _oi_rows(3.0), 1.8, NOW)
        self.assertEqual(d["legs"]["whale_ls"], "fail")
        self.assertEqual(d["score"], 3)
        self.assertFalse(d["threshold_met"])
        self.assertEqual(d["ls_ratio"], 1.8)

    def test_partial_score_counts_measured_passes(self):
        d = score_symbol([0.01, -0.005], _oi_rows(3.0), 2.5, NOW)
        # funding_neg pass (streak 1), 2prior fail, oi pass, whale pass
        self.assertEqual(d["score"], 3)
        self.assertEqual(d["legs"]["funding_neg_2prior"], "fail")


class TestVerdict(unittest.TestCase):
    def _score(self, streak_rates, oi_rows, ls):
        return score_symbol(streak_rates, oi_rows, ls, NOW)

    def test_armed_at_threshold(self):
        d = self._score([-0.001, -0.002, -0.003], _oi_rows(3.0), 2.5)
        self.assertEqual(verdict(d), "pre_cascade_armed")

    def test_watch_at_three(self):
        d = self._score([0.01, -0.005], _oi_rows(3.0), 2.5)
        self.assertEqual(d["score"], 3)
        self.assertEqual(verdict(d), "watch")

    def test_calm_zero_to_two(self):
        for ls in (1.5,):
            d = self._score([0.01, 0.02], _oi_rows(-2.0), ls)
            self.assertEqual(d["score"], 0)
            self.assertEqual(verdict(d), "calm")
        d2 = self._score([-0.001, -0.002], _oi_rows(-1.0), 1.5)
        self.assertEqual(d2["score"], 1)
        self.assertEqual(verdict(d2), "calm")

    def test_dark_on_none_score(self):
        d = self._score(None, _oi_rows(3.0), 2.5)
        self.assertEqual(verdict(d), "dark")
        self.assertEqual(verdict({"score": None, "threshold_met": None,
                                  "legs": {}, "ls_ratio": None}), "dark")


if __name__ == "__main__":
    unittest.main()
