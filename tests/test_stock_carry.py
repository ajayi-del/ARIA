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


# ── Strategy C funding-flip + Strategy D tradfi-lead pins ──
# (Governor 2026-09-22: LIVE from birth — the new strategies carry their
# own ENABLED flags; the legacy SHADOW_ONLY above still binds A/B.)

from intelligence.stock_carry import (  # noqa: E402
    FUNDING_FLIP_ENABLED, FF_FUNDING_HIGH, FF_FUNDING_LOW, FF_DROP_MIN,
    TRADFI_LEAD_ENABLED, CL_COHERENCE_BONUS,
    funding_flip_verdict, funding_flip_limit, funding_flip_bracket,
    funding_flip_kill, coin_lead_verdict, xaut_risk_oracle,
    cl_coherence_modifier,
)

# 9 hourly prints: 0.0010 8h ago collapsing to 0.0001 now (drop 0.0009).
_FF_RATES = [0.0010, 0.0009, 0.0008, 0.0007, 0.0006, 0.0005,
             0.0004, 0.0002, 0.0001]


class TestFundingFlipVerdict(unittest.TestCase):
    def test_full_pass_fires_short_live(self):
        v = funding_flip_verdict(_FF_RATES, 0.4)
        self.assertTrue(v["ok"])
        self.assertEqual(v["direction"], "short")
        self.assertFalse(v["shadow_only"])
        self.assertTrue(v["enabled"])
        self.assertTrue(FUNDING_FLIP_ENABLED)
        self.assertEqual(v["legs"], {"high_8h_ago": "pass", "now_low": "pass",
                                     "drop": "pass", "price_flat": "pass"})

    def test_boundaries_inclusive(self):
        rates = [FF_FUNDING_HIGH] + [0.0005] * 7 + [FF_FUNDING_LOW]
        v = funding_flip_verdict(rates, 1.0)
        self.assertEqual(v["legs"]["high_8h_ago"], "pass")
        self.assertEqual(v["legs"]["now_low"], "pass")
        self.assertEqual(v["legs"]["drop"], "pass")   # 0.0006 == FF_DROP_MIN
        self.assertEqual(v["legs"]["price_flat"], "pass")
        self.assertTrue(v["ok"])

    def test_each_leg_fails(self):
        self.assertEqual(funding_flip_verdict([0.0007] * 9, 0.0)
                         ["legs"]["high_8h_ago"], "fail")
        hot_now = [0.0010] * 8 + [0.0003]
        self.assertEqual(funding_flip_verdict(hot_now, 0.0)
                         ["legs"]["now_low"], "fail")
        small_drop = [0.0007] * 8 + [0.0002]
        self.assertEqual(funding_flip_verdict(small_drop, 0.0)
                         ["legs"]["drop"], "fail")
        self.assertEqual(funding_flip_verdict(_FF_RATES, 1.01)
                         ["legs"]["price_flat"], "fail")
        self.assertEqual(funding_flip_verdict(_FF_RATES, -1.0)
                         ["legs"]["price_flat"], "pass")

    def test_dark_on_short_series_and_none_prints(self):
        v = funding_flip_verdict([0.001] * 8, 0.0)
        self.assertEqual(v["legs"]["high_8h_ago"], "dark")
        self.assertEqual(v["legs"]["now_low"], "dark")
        self.assertEqual(v["legs"]["drop"], "dark")
        self.assertFalse(v["ok"])
        none_ago = [None] + _FF_RATES[1:]
        self.assertEqual(funding_flip_verdict(none_ago, 0.0)
                         ["legs"]["high_8h_ago"], "dark")
        self.assertEqual(funding_flip_verdict(_FF_RATES, None)
                         ["legs"]["price_flat"], "dark")

    def test_disabled_never_fires(self):
        v = funding_flip_verdict(_FF_RATES, 0.4, enabled=False)
        self.assertFalse(v["ok"])
        self.assertFalse(v["enabled"])


class TestFundingFlipBracketAndKill(unittest.TestCase):
    def test_limit_is_marketable_short(self):
        self.assertAlmostEqual(funding_flip_limit(100.0), 100.0 * 0.9985)

    def test_bracket_geometry_and_ordering(self):
        stop, tp1, tp2, tp3 = funding_flip_bracket(100.0, 101.0, ma4h=95.0)
        self.assertAlmostEqual(stop, 101.0 * 1.003)
        self.assertAlmostEqual(tp1, 98.0)
        self.assertAlmostEqual(tp2, 96.0)
        self.assertAlmostEqual(tp3, 95.0)
        self.assertGreater(stop, 100.0)
        self.assertGreater(tp1, tp2)
        self.assertGreater(tp2, tp3)

    def test_tp3_none_when_ma_above_entry_or_missing(self):
        self.assertIsNone(funding_flip_bracket(100.0, 101.0, ma4h=105.0)[3])
        self.assertIsNone(funding_flip_bracket(100.0, 101.0, ma4h=None)[3])
        self.assertIsNone(funding_flip_bracket(100.0, 101.0, ma4h=0.0)[3])

    def test_kill_re_extreme_and_fail_open(self):
        self.assertEqual(funding_flip_kill(0.0008),
                         (True, "funding_re_extreme"))
        self.assertEqual(funding_flip_kill(0.0007), (False, ""))
        self.assertEqual(funding_flip_kill(None), (False, ""))


class TestCoinLeadVerdict(unittest.TestCase):
    def test_long_lead_fires(self):
        v = coin_lead_verdict(251.5, 250.0, 120.0)   # +0.6% dev
        self.assertTrue(v["ok"])
        self.assertEqual(v["direction"], "long")
        self.assertTrue(TRADFI_LEAD_ENABLED)

    def test_short_lead_fires(self):
        v = coin_lead_verdict(248.6, 250.0, 120.0)   # -0.56% dev
        self.assertTrue(v["ok"])
        self.assertEqual(v["direction"], "short")

    def test_boundary_and_staleness(self):
        self.assertTrue(coin_lead_verdict(251.25, 250.0, 600.0)["ok"])
        self.assertEqual(coin_lead_verdict(251.25, 250.0, 600.1)
                         ["legs"]["freshness"], "fail")
        self.assertEqual(coin_lead_verdict(251.0, 250.0, 10.0)
                         ["legs"]["deviation"], "fail")

    def test_dark_planes_abstain(self):
        self.assertEqual(coin_lead_verdict(None, 250.0, 10.0)
                         ["legs"]["deviation"], "dark")
        self.assertEqual(coin_lead_verdict(251.5, 250.0, None)
                         ["legs"]["freshness"], "dark")
        v = coin_lead_verdict(251.5, None, 10.0)
        self.assertFalse(v["ok"])
        self.assertIsNone(v["direction"])

    def test_disabled_never_votes(self):
        v = coin_lead_verdict(251.5, 250.0, 120.0, enabled=False)
        self.assertFalse(v["ok"])


class TestXautRiskOracle(unittest.TestCase):
    def test_gold_outperforming_is_risk_off(self):
        v = xaut_risk_oracle(0.8, -0.5, True)
        self.assertTrue(v["risk_off"])
        self.assertEqual(v["legs"]["outperformance"], "pass")

    def test_btc_outperforming_is_not(self):
        v = xaut_risk_oracle(-0.2, 1.5, True)
        self.assertFalse(v["risk_off"])

    def test_closed_market_and_dark_returns_abstain(self):
        self.assertIsNone(xaut_risk_oracle(0.8, -0.5, False)["risk_off"])
        self.assertIsNone(xaut_risk_oracle(0.8, -0.5, None)["risk_off"])
        self.assertIsNone(xaut_risk_oracle(None, -0.5, True)["risk_off"])


class TestClCoherenceModifier(unittest.TestCase):
    def test_signed_bonus(self):
        self.assertEqual(cl_coherence_modifier(0.5, "long"),
                         CL_COHERENCE_BONUS)
        self.assertEqual(cl_coherence_modifier(-0.5, "short"),
                         CL_COHERENCE_BONUS)
        self.assertEqual(cl_coherence_modifier(0.5, "short"),
                         -CL_COHERENCE_BONUS)
        self.assertEqual(cl_coherence_modifier(-0.5, "long"),
                         -CL_COHERENCE_BONUS)

    def test_immaterial_and_dark_pay_zero(self):
        self.assertEqual(cl_coherence_modifier(0.29, "long"), 0.0)
        self.assertEqual(cl_coherence_modifier(None, "long"), 0.0)
        self.assertEqual(cl_coherence_modifier(0.5, None), 0.0)
        self.assertEqual(cl_coherence_modifier(0.5, "flat"), 0.0)


if __name__ == "__main__":
    unittest.main()
