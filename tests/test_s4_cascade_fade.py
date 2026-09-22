"""S4 ETH 4h cascade-fade brain pins (Governor 2026-09-15, SHADOW-ONLY)."""
import unittest

from intelligence.s4_cascade_fade import (
    ARM_WINDOW_H, FUNDING_FLOOR, KNIFE_BUFFER_PCT, SHADOW_ONLY,
    cascade_bar, knife_check, evaluate_entry, evaluate_kill, bracket,
)

HOUR_MS = 3600 * 1000
NOW = 1_800_000_000_000  # fixed deterministic clock


def _armed_score(age_h=1.0, score=5.0):
    return score, NOW - int(age_h * HOUR_MS)


class TestCascadeBar(unittest.TestCase):
    def test_boundaries_inclusive_fire(self):
        self.assertTrue(cascade_bar(-3.0, -2.0))
        self.assertTrue(cascade_bar(-5.2, -4.7))

    def test_inside_boundary_does_not_fire(self):
        self.assertFalse(cascade_bar(-2.99, -3.0))
        self.assertFalse(cascade_bar(-3.0, -1.99))
        self.assertFalse(cascade_bar(0.5, 0.5))

    def test_none_input_not_a_cascade(self):
        self.assertFalse(cascade_bar(None, -3.0))
        self.assertFalse(cascade_bar(-3.0, None))
        self.assertFalse(cascade_bar(None, None))


class TestKnifeCheck(unittest.TestCase):
    def test_dark_on_no_coverage(self):
        self.assertIsNone(knife_check(100.0, None))
        self.assertIsNone(knife_check(100.0, []))

    def test_dark_on_degenerate_trigger_low(self):
        self.assertIsNone(knife_check(0.0, [99.0, 99.5]))
        self.assertIsNone(knife_check(None, [99.0]))

    def test_pass_when_lows_hold_above_floor(self):
        # floor = 100 * (1 - 0.004) = 99.6; all lows above
        self.assertTrue(knife_check(100.0, [99.7, 99.8, 100.1]))

    def test_fail_when_low_beyond_buffer(self):
        # 99.5 < 99.6 floor -> knife still falling
        self.assertFalse(knife_check(100.0, [99.7, 99.5]))

    def test_buffer_boundary_exact_floor_passes(self):
        # "beyond" is strict: a low exactly at the floor still passes
        floor = 100.0 * (1.0 - KNIFE_BUFFER_PCT / 100.0)
        self.assertTrue(knife_check(100.0, [floor, 100.0]))
        self.assertFalse(knife_check(100.0, [floor - 1e-9]))

    def test_custom_buffer_pct(self):
        # 1% buffer on a 200 low -> floor 198.0
        self.assertTrue(knife_check(200.0, [198.0], buffer_pct=1.0))
        self.assertFalse(knife_check(200.0, [197.9], buffer_pct=1.0))


class TestEvaluateEntry(unittest.TestCase):
    def _good(self, **kw):
        args = dict(
            armed_score=5.0, armed_ts_ms=NOW - HOUR_MS, now_ms=NOW,
            oi_chg_1h=-4.0, close_chg_1h=-3.0,
            trigger_bar={"open": 3000.0, "low": 2880.0},
            recent_1m_lows=[2881.0, 2885.0, 2890.0],
            funding_latest=-0.0003,
        )
        args.update(kw)
        return evaluate_entry(**args)

    def test_all_legs_pass(self):
        v = self._good()
        self.assertTrue(v["ok"])
        self.assertEqual(v["legs"], {"armed": "pass", "trigger": "pass",
                                     "stabilization": "pass", "funding": "pass"})
        self.assertTrue(v["shadow_only"])
        self.assertTrue(SHADOW_ONLY)

    def test_armed_dark_on_none(self):
        v = self._good(armed_score=None)
        self.assertEqual(v["legs"]["armed"], "dark")
        self.assertFalse(v["ok"])
        v2 = self._good(armed_ts_ms=None)
        self.assertEqual(v2["legs"]["armed"], "dark")
        self.assertFalse(v2["ok"])

    def test_armed_fail_on_stale_score(self):
        # 7h old > 6h window -> fail (not dark: score exists, it expired)
        v = self._good(armed_ts_ms=NOW - 7 * HOUR_MS)
        self.assertEqual(v["legs"]["armed"], "fail")
        self.assertFalse(v["ok"])

    def test_armed_window_boundary_inclusive(self):
        # exactly 6h old still passes
        v = self._good(armed_ts_ms=NOW - ARM_WINDOW_H * HOUR_MS)
        self.assertEqual(v["legs"]["armed"], "pass")
        # one ms beyond fails
        v2 = self._good(armed_ts_ms=NOW - ARM_WINDOW_H * HOUR_MS - 1)
        self.assertEqual(v2["legs"]["armed"], "fail")

    def test_armed_fail_on_low_score(self):
        self.assertEqual(self._good(armed_score=3.99)["legs"]["armed"], "fail")
        self.assertEqual(self._good(armed_score=4.0)["legs"]["armed"], "pass")

    def test_trigger_dark_on_none(self):
        v = self._good(oi_chg_1h=None)
        self.assertEqual(v["legs"]["trigger"], "dark")
        self.assertFalse(v["ok"])

    def test_trigger_fail_on_weak_bar(self):
        self.assertEqual(self._good(close_chg_1h=-1.5)["legs"]["trigger"], "fail")

    def test_stabilization_dark_on_empty_lows(self):
        for lows in (None, []):
            v = self._good(recent_1m_lows=lows)
            self.assertEqual(v["legs"]["stabilization"], "dark")
            self.assertFalse(v["ok"])

    def test_stabilization_dark_on_missing_bar(self):
        self.assertEqual(self._good(trigger_bar=None)["legs"]["stabilization"],
                         "dark")
        self.assertEqual(self._good(trigger_bar={"open": 3000.0})["legs"]
                         ["stabilization"], "dark")

    def test_stabilization_fail_on_falling_knife(self):
        floor = 2880.0 * (1.0 - KNIFE_BUFFER_PCT / 100.0)  # 2868.48
        v = self._good(recent_1m_lows=[2881.0, floor - 0.5])
        self.assertEqual(v["legs"]["stabilization"], "fail")
        self.assertFalse(v["ok"])

    def test_funding_dark_and_fail_semantics(self):
        self.assertEqual(self._good(funding_latest=None)["legs"]["funding"], "dark")
        # at the floor exactly = freshly worse (entry needs strictly above)
        self.assertEqual(self._good(funding_latest=FUNDING_FLOOR)["legs"]
                         ["funding"], "fail")
        self.assertEqual(self._good(funding_latest=FUNDING_FLOOR + 1e-9)
                         ["legs"]["funding"], "pass")

    def test_tuple_trigger_bar_accepted(self):
        v = self._good(trigger_bar=(3000.0, 2880.0))
        self.assertTrue(v["ok"])
        self.assertEqual(v["trigger_low"], 2880.0)


class TestEvaluateKill(unittest.TestCase):
    def test_second_cascade_kills(self):
        self.assertEqual(evaluate_kill(True, None, None),
                         (True, "second_cascade"))

    def test_funding_floor_breach_kills(self):
        self.assertEqual(evaluate_kill(False, FUNDING_FLOOR, None),
                         (True, "funding_floor_breach"))
        self.assertEqual(evaluate_kill(False, -0.001, None),
                         (True, "funding_floor_breach"))

    def test_oi_deleveraging_kills(self):
        self.assertEqual(evaluate_kill(False, None, -7.0),
                         (True, "oi_24h_deleveraging"))
        self.assertEqual(evaluate_kill(False, None, -6.0), (False, ""))

    def test_none_is_no_opinion_fail_open(self):
        self.assertEqual(evaluate_kill(False, None, None), (False, ""))
        self.assertEqual(evaluate_kill(False, -0.0003, None), (False, ""))
        self.assertEqual(evaluate_kill(False, None, -2.0), (False, ""))

    def test_reason_priority_second_cascade_first(self):
        self.assertEqual(evaluate_kill(True, -0.001, -7.0)[1], "second_cascade")


class TestBracket(unittest.TestCase):
    def test_geometry_on_constructed_trigger_bar(self):
        # trigger bar: open 3000, low 2880; fade entry at 2900
        stop, tp1, tp2 = bracket(2900.0, 3000.0, 2880.0)
        self.assertAlmostEqual(stop, 2880.0 * 0.99)          # 2851.2
        self.assertAlmostEqual(tp1, 2900.0 + 0.5 * 120.0)    # 2960.0
        self.assertAlmostEqual(tp2, 3000.0)

    def test_long_ordering_stop_below_tp1_below_tp2(self):
        stop, tp1, tp2 = bracket(2900.0, 3000.0, 2880.0)
        self.assertLess(stop, tp1)
        self.assertLess(tp1, tp2)
        self.assertLess(stop, 2880.0)

    def test_custom_stop_buffer(self):
        stop, _, _ = bracket(100.0, 110.0, 95.0, stop_buffer_pct=2.0)
        self.assertAlmostEqual(stop, 95.0 * 0.98)


# ── S12 dead-cat bounce short pins (Governor 2026-09-22, LIVE from birth) ──

from intelligence.s4_cascade_fade import (  # noqa: E402
    DEAD_CAT_ENABLED,
    dc_recovery_frac, dc_rejection_shape, dc_volume_ok,
    evaluate_dead_cat_entry, evaluate_dead_cat_kill, dead_cat_bracket,
)

# Cascade: pre_cascade 100 -> trough 80 (drop 20). Recovery band = 94..98.
_TROUGH = 80.0
_PRE = 100.0
_PRIOR_VOLS = [10.0, 10.0, 10.0, 10.0, 10.0]


def _pass_bar(close=94.0, volume=20.0):
    # shooting star: range 90..110 (20); body top 97 <= 90+0.35*20=97
    # (inclusive); wick 110-97=13 > 0.6*20=12 (strict); bearish 94 <= 97.
    # close 94 sits at recovery 0.70 of the (80,100) cascade.
    return {"open": 97.0, "high": 110.0, "low": 90.0,
            "close": close, "volume": volume}


class TestDcRecoveryFrac(unittest.TestCase):
    def test_band_math(self):
        self.assertAlmostEqual(dc_recovery_frac(80.0, 100.0, 94.0), 0.70)
        self.assertAlmostEqual(dc_recovery_frac(80.0, 100.0, 98.0), 0.90)
        self.assertAlmostEqual(dc_recovery_frac(80.0, 100.0, 90.0), 0.50)

    def test_dark_on_missing_or_degenerate(self):
        self.assertIsNone(dc_recovery_frac(None, 100.0, 95.0))
        self.assertIsNone(dc_recovery_frac(80.0, None, 95.0))
        self.assertIsNone(dc_recovery_frac(80.0, 100.0, None))
        self.assertIsNone(dc_recovery_frac(100.0, 100.0, 100.0))  # drop 0
        self.assertIsNone(dc_recovery_frac(110.0, 100.0, 105.0))  # drop < 0


class TestDcRejectionShape(unittest.TestCase):
    def test_true_pass_shape(self):
        self.assertTrue(dc_rejection_shape(_pass_bar()))

    def test_body_top_boundary_inclusive(self):
        # body top exactly at low + 0.35*range still passes the body leg.
        self.assertTrue(dc_rejection_shape(_pass_bar()))  # 97 == 90+7

    def test_body_above_lower_third_fails(self):
        bar = {"open": 98.0, "high": 110.0, "low": 90.0, "close": 94.0,
               "volume": 20.0}  # body top 98 > 97
        self.assertFalse(dc_rejection_shape(bar))

    def test_wick_at_exactly_60pct_fails_strict(self):
        bar = {"open": 98.0, "high": 110.0, "low": 90.0, "close": 94.0,
               "volume": 20.0}  # wick 12 == 0.6*20, strict >
        self.assertFalse(dc_rejection_shape(bar))

    def test_bullish_body_fails(self):
        bar = {"open": 94.0, "high": 110.0, "low": 90.0, "close": 97.0,
               "volume": 20.0}  # close > open
        self.assertFalse(dc_rejection_shape(bar))

    def test_dark_on_missing_fields_and_zero_range(self):
        self.assertIsNone(dc_rejection_shape(None))
        self.assertIsNone(dc_rejection_shape({"open": 1.0}))
        self.assertIsNone(dc_rejection_shape(
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0,
             "volume": 1.0}))

    def test_tuple_form_accepted(self):
        self.assertTrue(dc_rejection_shape((97.0, 110.0, 90.0, 94.0, 20.0)))


class TestDcVolumeOk(unittest.TestCase):
    def test_pass_and_fail(self):
        self.assertTrue(dc_volume_ok(_pass_bar(volume=15.01), _PRIOR_VOLS))
        self.assertFalse(dc_volume_ok(_pass_bar(volume=15.0), _PRIOR_VOLS))

    def test_dark_on_thin_history_or_missing_volume(self):
        self.assertIsNone(dc_volume_ok(_pass_bar(), [10.0] * 4))
        self.assertIsNone(dc_volume_ok(_pass_bar(), None))
        bar = {"open": 97.0, "high": 110.0, "low": 90.0, "close": 94.0}
        self.assertIsNone(dc_volume_ok(bar, _PRIOR_VOLS))


class TestEvaluateDeadCatEntry(unittest.TestCase):
    def test_full_pass_fires_short_live(self):
        v = evaluate_dead_cat_entry(_TROUGH, _PRE, _pass_bar(), _PRIOR_VOLS)
        self.assertTrue(v["ok"])
        self.assertEqual(v["direction"], "short")
        self.assertFalse(v["shadow_only"])
        self.assertTrue(v["enabled"])
        self.assertEqual(v["legs"], {"recovery": "pass",
                                     "rejection_shape": "pass",
                                     "volume": "pass"})

    def test_recovery_band_boundaries_inclusive(self):
        # closes exactly at 94.0 (0.70) and 98.0 (0.90) of the (80,100)
        # cascade both arm the recovery leg (shape/volume not under test).
        for close in (94.0, 98.0):
            bar = {"open": 105.0, "high": 130.0, "low": 90.0, "close": close,
                   "volume": 20.0}
            v = evaluate_dead_cat_entry(_TROUGH, _PRE, bar, _PRIOR_VOLS)
            self.assertEqual(v["legs"]["recovery"], "pass")

    def test_below_band_fails_not_dark(self):
        bar = {"open": 97.0, "high": 110.0, "low": 90.0, "close": 93.9,
               "volume": 20.0}
        v = evaluate_dead_cat_entry(_TROUGH, _PRE, bar, _PRIOR_VOLS)
        self.assertEqual(v["legs"]["recovery"], "fail")
        self.assertFalse(v["ok"])

    def test_dark_recovery_blocks(self):
        v = evaluate_dead_cat_entry(None, _PRE, _pass_bar(), _PRIOR_VOLS)
        self.assertEqual(v["legs"]["recovery"], "dark")
        self.assertFalse(v["ok"])

    def test_disabled_never_fires(self):
        v = evaluate_dead_cat_entry(_TROUGH, _PRE, _pass_bar(), _PRIOR_VOLS,
                                    enabled=False)
        self.assertFalse(v["ok"])
        self.assertFalse(v["enabled"])
        self.assertTrue(DEAD_CAT_ENABLED)  # birth state is LIVE


class TestEvaluateDeadCatKill(unittest.TestCase):
    def test_full_retrace_kills(self):
        self.assertEqual(evaluate_dead_cat_kill(100.0, 100.0),
                         (True, "full_retrace"))
        self.assertEqual(evaluate_dead_cat_kill(101.0, 100.0)[0], True)

    def test_below_pre_cascade_no_kill(self):
        self.assertEqual(evaluate_dead_cat_kill(99.99, 100.0), (False, ""))

    def test_fail_open_on_none(self):
        self.assertEqual(evaluate_dead_cat_kill(None, 100.0), (False, ""))
        self.assertEqual(evaluate_dead_cat_kill(100.0, None), (False, ""))


class TestDeadCatBracket(unittest.TestCase):
    def test_geometry(self):
        stop, tp1, tp2, tp3 = dead_cat_bracket(94.5, 96.5, 80.0, 100.0)
        self.assertAlmostEqual(stop, 96.5 * 1.005)
        self.assertAlmostEqual(tp1, 80.0)
        self.assertAlmostEqual(tp2, 80.0 - 0.20 * 20.0)   # 76.0
        self.assertAlmostEqual(tp3, 100.0 * 0.70)          # 70.0

    def test_short_ordering(self):
        stop, tp1, tp2, tp3 = dead_cat_bracket(94.5, 96.5, 80.0, 100.0)
        self.assertGreater(stop, 94.5)
        self.assertGreater(94.5, tp1)
        self.assertGreater(tp1, tp2)
        self.assertGreater(tp2, tp3)


if __name__ == "__main__":
    unittest.main()
