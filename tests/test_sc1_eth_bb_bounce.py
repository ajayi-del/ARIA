"""Scalp 1 — ETH 5m BB-bounce brain pins (register 2026-09-15)."""
import unittest

from intelligence.sc1_eth_bb_bounce import (
    BB_WIDTH_KILL_MULT, FEE_RATIO_MAX, RUPTURE_FLOOR, SPREAD_MAX_BP,
    TIME_STOP_MIN, TP1_BP, TP2_BP, STOP_BP,
    bb_bands, vol_ma_ok, fee_ratio, fee_ratio_ok,
    evaluate_entry, evaluate_kill, bracket,
)

TAKER_RT = 7.60  # SoDEX live effective taker round-trip bp (min_viable_tp)
FEE = {"taker": TAKER_RT, "maker": 2.28}

# Known-math series: closes 10..29 -> mean 19.5, population var 33.25
CL = [float(x) for x in range(10, 30)]
MEAN = 19.5
SD = 33.25 ** 0.5
LOWER = MEAN - 2.0 * SD
UPPER = MEAN + 2.0 * SD


def _bar(o, c):
    return {"open": o, "high": max(o, c), "low": min(o, c), "close": c}


def _good_bands():
    # bands straddling a touch zone around 100
    return (100.0, 105.0, 110.0)


class TestBandsAndVolume(unittest.TestCase):
    def test_bb_bands_known_math(self):
        lower, mid, upper = bb_bands(CL)
        self.assertAlmostEqual(mid, 19.5, places=9)
        self.assertAlmostEqual(lower, LOWER, places=9)
        self.assertAlmostEqual(upper, UPPER, places=9)

    def test_bb_bands_none_on_thin(self):
        self.assertIsNone(bb_bands([1.0] * 19))
        self.assertIsNone(bb_bands([]))

    def test_bb_bands_constant_series(self):
        self.assertEqual(bb_bands([50.0] * 25), (50.0, 50.0, 50.0))

    def test_vol_ma_ok_pass_fail_dark(self):
        vols = [100.0] * 20 + [150.0]
        self.assertTrue(vol_ma_ok(vols, 1.2))
        self.assertFalse(vol_ma_ok(vols, 1.6))
        self.assertIsNone(vol_ma_ok([100.0] * 20, 1.2))  # n prints, no signal bar

    def test_vol_ma_excludes_signal_bar(self):
        # a huge signal bar must not lift its own benchmark
        vols = [100.0] * 20 + [10_000.0]
        self.assertTrue(vol_ma_ok(vols, 1.2))
        self.assertFalse(vol_ma_ok(vols, 101.0))


class TestFeeGate(unittest.TestCase):
    def test_fee_ratio_math_and_dark(self):
        self.assertAlmostEqual(fee_ratio(2.4, FEE, 59.0), 10.0 / 59.0)
        self.assertIsNone(fee_ratio(None, FEE, 59.0))
        self.assertIsNone(fee_ratio(2.4, None, 59.0))
        self.assertIsNone(fee_ratio(2.4, FEE, 0.0))
        self.assertIsNone(fee_ratio(2.4, {}, 59.0))

    def test_fee_ratio_boundary_exact(self):
        # 12.0 / 40.0 == 0.30 exactly in IEEE — acceptance AT the boundary
        self.assertTrue(fee_ratio_ok(0.0, {"taker": 12.0}, 40.0))
        self.assertFalse(fee_ratio_ok(0.0, {"taker": 12.1}, 40.0))
        self.assertIsNone(fee_ratio_ok(None, {"taker": 12.0}, 40.0))

    def test_fee_ratio_max_mirrors_doctrine(self):
        self.assertEqual(FEE_RATIO_MAX, 0.30)
        self.assertEqual(TIME_STOP_MIN, 25)
        self.assertEqual((TP1_BP, TP2_BP, STOP_BP), (30.0, 59.0, 14.0))


class TestEntryLegs(unittest.TestCase):
    def _entry(self, **kw):
        args = dict(bar=_bar(99.9, 100.0), prior_bar=_bar(104.0, 105.0),
                    bands=_good_bands(), prior_bands=_good_bands(),
                    vol_ok=True, funding=0.0001, spread_bp=2.0,
                    fee_state=FEE)
        args.update(kw)
        return evaluate_entry(**args)

    def test_all_legs_pass(self):
        v = self._entry()
        self.assertTrue(v["ok"])
        self.assertTrue(all(s == "pass" for s in v["legs"].values()))

    def test_rupture_band_boundary(self):
        # close AT the lower band = touch allowed
        self.assertTrue(self._entry(bar=_bar(99.9, 100.0))["ok"])
        # rupture floor is STRICT: close == lower * 0.9980 fails
        self.assertFalse(self._entry(bar=_bar(99.7, 99.8))["ok"])
        v = self._entry(bar=_bar(99.75, 99.8001))
        self.assertEqual(v["legs"]["bb_touch"], "pass")
        # above the band = no touch
        self.assertEqual(self._entry(bar=_bar(100.0, 100.5))["legs"]["bb_touch"], "fail")
        # deep rupture fails
        self.assertEqual(self._entry(bar=_bar(98.0, 99.0))["legs"]["bb_touch"], "fail")

    def test_prior_inside_strict(self):
        # prior close exactly ON a band fails (strict inside)
        self.assertEqual(self._entry(prior_bar=_bar(109.0, 110.0))["legs"]["prior_inside"], "fail")
        self.assertEqual(self._entry(prior_bar=_bar(100.0, 100.0))["legs"]["prior_inside"], "fail")
        self.assertEqual(self._entry(prior_bar=_bar(109.9, 109.99))["legs"]["prior_inside"], "pass")
        self.assertEqual(self._entry(prior_bands=None)["legs"]["prior_inside"], "dark")
        self.assertFalse(self._entry(prior_bands=None)["ok"])

    def test_dark_legs_fail_closed(self):
        self.assertFalse(self._entry(bands=None)["ok"])
        self.assertFalse(self._entry(vol_ok=None)["ok"])
        self.assertFalse(self._entry(funding=None)["ok"])
        self.assertFalse(self._entry(spread_bp=None)["ok"])
        self.assertFalse(self._entry(fee_state=None)["ok"])
        self.assertEqual(self._entry(vol_ok=None)["legs"]["volume"], "dark")
        self.assertEqual(self._entry(fee_state=None)["legs"]["fee_ratio"], "dark")

    def test_bullish_close_leg(self):
        v = self._entry(bar=_bar(100.0, 99.9))  # bearish close at the band
        self.assertEqual(v["legs"]["bullish_close"], "fail")
        self.assertFalse(v["ok"])
        self.assertEqual(self._entry(bar=_bar(99.9, 99.9))["legs"]["bullish_close"], "fail")

    def test_funding_and_spread_legs(self):
        self.assertEqual(self._entry(funding=-0.0001)["legs"]["funding"], "fail")
        self.assertEqual(self._entry(funding=0.0)["legs"]["funding"], "pass")
        self.assertEqual(self._entry(spread_bp=SPREAD_MAX_BP)["legs"]["spread"], "pass")
        self.assertEqual(self._entry(spread_bp=2.6)["legs"]["spread"], "fail")

    def test_volume_leg(self):
        self.assertFalse(self._entry(vol_ok=False)["ok"])

    def test_fee_leg_accept_refuse(self):
        # (spread + 7.60) / 59 <= 0.30  <=>  spread <= 10.1
        v = self._entry(spread_bp=10.1)
        self.assertEqual(v["legs"]["fee_ratio"], "pass")
        v2 = self._entry(spread_bp=10.1, fee_state={"taker": 10.1})
        self.assertEqual(v2["legs"]["fee_ratio"], "fail")
        self.assertFalse(v2["ok"])

    def test_fee_leg_custom_tp_boundary(self):
        # exact IEEE boundary via injected tp_bp: 12.0/40.0 == 0.30 passes
        v = self._entry(spread_bp=0.0, fee_state={"taker": 12.0}, tp_bp=40.0)
        self.assertEqual(v["legs"]["fee_ratio"], "pass")


class TestKillAndBracket(unittest.TestCase):
    def test_width_expansion_strict_3x(self):
        self.assertEqual(evaluate_kill(30.0, 10.0), (False, ""))  # exactly 3x: no kill
        self.assertEqual(evaluate_kill(30.01, 10.0), (True, "bb_width_expansion_3x"))
        self.assertEqual(evaluate_kill(None, 10.0), (False, ""))  # dark: fail-open
        self.assertEqual(evaluate_kill(40.0, 0.0), (False, ""))   # degenerate entry width
        self.assertEqual(BB_WIDTH_KILL_MULT, 3.0)

    def test_funding_flip_and_vol_recheck(self):
        self.assertEqual(evaluate_kill(funding_now=-0.0001), (True, "funding_flip_negative"))
        self.assertEqual(evaluate_kill(funding_now=0.0), (False, ""))
        self.assertEqual(evaluate_kill(vol_ok_15=False), (True, "entry_volume_recheck_fail"))
        self.assertEqual(evaluate_kill(vol_ok_15=None), (False, ""))
        self.assertEqual(evaluate_kill(), (False, ""))

    def test_kill_precedence(self):
        # width outranks funding outranks volume (spec order)
        self.assertEqual(evaluate_kill(40.0, 10.0, -1.0, False),
                         (True, "bb_width_expansion_3x"))
        self.assertEqual(evaluate_kill(1.0, 10.0, -1.0, False),
                         (True, "funding_flip_negative"))

    def test_bracket_arithmetic(self):
        stop, tp1, tp2 = bracket(2400.0)
        self.assertAlmostEqual(stop, 2400.0 * 0.9986)
        self.assertAlmostEqual(tp1, 2400.0 * 1.0030)
        self.assertAlmostEqual(tp2, 2400.0 * 1.0059)
        self.assertLess(stop, 2400.0)
        self.assertLess(tp1, tp2)


if __name__ == "__main__":
    unittest.main()
