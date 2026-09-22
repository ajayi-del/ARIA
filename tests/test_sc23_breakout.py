"""Scalps 2+3 — shared breakout brain pins (register 2026-09-15)."""
import unittest

from intelligence.sc23_breakout import (
    ETH_CONFIG, SOL_CONFIG, IV_RANK_MAX, REQUIRE_OPTIONS_LEGS,
    ZONE_REENTRY_BARS,
    session_vwap, vwap_band_sigma, zone_price,
    evaluate_entry, evaluate_kill, bracket,
)

TAKER_RT = 7.60  # SoDEX live effective taker round-trip bp (min_viable_tp)
FEE = {"taker": TAKER_RT, "maker": 2.28, "spread_bp": 0.0}


def _hist_flat(n=20, vol=100.0):
    # flat closes at 100, highs 110, lows 100 -> tp = 103.333..., BB upper = 100,
    # VWAP band dominates the zone (zone = 310/3 exactly)
    return [(0.0, 110.0, 100.0, 100.0, vol) for _ in range(n)]


def _entry_bars(close=103.8, prior_close=100.0, n_hist=20, vol=100.0,
                sig_vol=250.0):
    bars = _hist_flat(n_hist, vol)
    bars[-1] = (0.0, 110.0, 100.0, prior_close, vol)
    bars.append((0.0, close, 99.0, close, sig_vol))
    return bars


class TestVwapAndZone(unittest.TestCase):
    def test_session_vwap_known_math(self):
        bars = [(0.0, 12.0, 8.0, 10.0, 100.0), (0.0, 22.0, 18.0, 20.0, 300.0)]
        self.assertAlmostEqual(session_vwap(bars), 17.5)
        self.assertAlmostEqual(vwap_band_sigma(bars), 18.75 ** 0.5)

    def test_session_vwap_dark(self):
        self.assertIsNone(session_vwap([]))
        self.assertIsNone(session_vwap([(0.0, 1.0, 1.0, 1.0, 0.0)]))
        self.assertIsNone(vwap_band_sigma([]))

    def test_zone_vwap_band_dominates(self):
        bars = _hist_flat()
        zone = zone_price([b[3] for b in bars], [b[1] for b in bars],
                          [b[2] for b in bars], [b[4] for b in bars])
        # BB upper = 100 (flat closes); VWAP + sigma = 310/3 + 0
        self.assertAlmostEqual(zone, 310.0 / 3.0, places=9)

    def test_zone_bb_upper_dominates(self):
        # steadily rising closes 100..119, unit vol: BB upper 121.03 > vwap+sigma
        closes = [100.0 + i for i in range(20)]
        zone = zone_price(closes, closes, closes, [1.0] * 20)
        self.assertAlmostEqual(zone, 109.5 + 2.0 * (33.25 ** 0.5), places=6)

    def test_zone_dark_on_thin_or_mismatch(self):
        self.assertIsNone(zone_price([1.0] * 19, [1.0] * 19, [1.0] * 19, [1.0] * 19))
        self.assertIsNone(zone_price([1.0] * 21, [1.0] * 20, [1.0] * 20, [1.0] * 20))


class TestEntry(unittest.TestCase):
    def _entry(self, config=ETH_CONFIG, bars=None, funding=0.0, iv_rank=None,
               oi=None, fee_state=FEE):
        return evaluate_entry(config, bars if bars is not None else _entry_bars(),
                              funding, iv_rank, oi, fee_state)

    def test_eth_full_pass_iv_dark_nonblocking(self):
        v = self._entry()
        self.assertTrue(v["ok"])
        self.assertEqual(v["legs"]["iv_rank"], "dark")
        self.assertFalse(REQUIRE_OPTIONS_LEGS)
        self.assertAlmostEqual(v["zone"], 310.0 / 3.0, places=9)

    def test_cross_detection(self):
        # prior already above the zone = no cross
        v = self._entry(bars=_entry_bars(close=103.8, prior_close=103.8))
        self.assertEqual(v["legs"]["zone_cross"], "fail")
        # close back below the zone = no cross
        self.assertEqual(self._entry(bars=_entry_bars(close=103.0))["legs"]["zone_cross"], "fail")
        # thin history = dark
        self.assertEqual(self._entry(bars=[(0.0, 1.0, 1.0, 1.0, 1.0)])["legs"]["zone_cross"], "dark")

    def test_within_band_chase_rejection(self):
        # zone = 103.333; close 103.8 = 45.2bp above: ETH 50bp passes
        self.assertEqual(self._entry(bars=_entry_bars(close=103.8))["legs"]["within_band"], "pass")
        v = self._entry(bars=_entry_bars(close=105.0))  # ~161bp above = chasing
        self.assertEqual(v["legs"]["within_band"], "fail")
        self.assertFalse(v["ok"])
        # SOL's 40bp band is tighter: the same 103.8 close (45.2bp) fails SOL
        self.assertEqual(self._entry(config=SOL_CONFIG, bars=_entry_bars(close=103.8),
                                     oi=0.0)["legs"]["within_band"], "fail")

    def test_volume_mult_config_difference(self):
        # prior vols 100, signal 190: ETH 2.0x fails, SOL 1.8x passes
        self.assertEqual(self._entry(bars=_entry_bars(sig_vol=190.0))["legs"]["volume"], "fail")
        self.assertEqual(self._entry(config=SOL_CONFIG, bars=_entry_bars(sig_vol=190.0),
                                     oi=0.0)["legs"]["volume"], "pass")
        # insufficient history = dark, blocks
        v = self._entry(bars=_entry_bars(n_hist=5))
        self.assertEqual(v["legs"]["volume"], "dark")
        self.assertFalse(v["ok"])

    def test_funding_dark_fail_closed(self):
        self.assertFalse(self._entry(funding=None)["ok"])
        self.assertEqual(self._entry(funding=-0.1)["legs"]["funding"], "fail")
        self.assertTrue(self._entry(funding=0.0)["ok"])

    def test_iv_leg_semantics(self):
        self.assertTrue(self._entry(iv_rank=None)["ok"])          # dark, non-blocking
        self.assertTrue(self._entry(iv_rank=64.9)["ok"])
        v = self._entry(iv_rank=IV_RANK_MAX)                      # 65 blocks
        self.assertEqual(v["legs"]["iv_rank"], "fail")
        self.assertFalse(v["ok"])

    def test_sol_oi_leg(self):
        # ETH: leg absent entirely
        self.assertNotIn("oi_24h", self._entry()["legs"])
        # SOL: dark fails closed; strict > -1.5 (close 103.7 = 35.5bp, inside
        # SOL's 40bp band so only the OI leg is under test)
        sol = dict(config=SOL_CONFIG, bars=_entry_bars(close=103.7))
        self.assertFalse(self._entry(oi=None, **sol)["ok"])
        self.assertEqual(self._entry(oi=None, **sol)["legs"]["oi_24h"], "dark")
        self.assertEqual(self._entry(oi=-1.5, **sol)["legs"]["oi_24h"], "fail")
        self.assertTrue(self._entry(oi=-1.49, **sol)["ok"])

    def test_fee_leg(self):
        # ETH: farthest TP = tp1 37bp; 7.60/37 = 20.5% passes
        v = self._entry()
        self.assertEqual(v["legs"]["fee_ratio"], "pass")
        self.assertAlmostEqual(v["fee_ratio"], TAKER_RT / 37.0)
        # SOL: farthest TP = tp2 62bp
        self.assertAlmostEqual(self._entry(config=SOL_CONFIG, oi=0.0)["fee_ratio"],
                               TAKER_RT / 62.0)
        # refusal above 0.30 and dark on missing state
        v2 = self._entry(fee_state={"taker": 12.0, "spread_bp": 0.0})
        self.assertEqual(v2["legs"]["fee_ratio"], "fail")  # 12/37 = 32.4%
        self.assertFalse(v2["ok"])
        self.assertEqual(self._entry(fee_state=None)["legs"]["fee_ratio"], "dark")
        # spread rides the fee state
        v3 = self._entry(fee_state={"taker": TAKER_RT, "spread_bp": 3.5})
        self.assertAlmostEqual(v3["fee_ratio"], (TAKER_RT + 3.5) / 37.0)

    def test_config_knobs_pinned(self):
        self.assertEqual((ETH_CONFIG["vol_mult"], ETH_CONFIG["within_bp"],
                          ETH_CONFIG["tp1_bp"], ETH_CONFIG["tp2_bp"],
                          ETH_CONFIG["stop_bp"], ETH_CONFIG["time_stop_min"],
                          ETH_CONFIG["oi_leg"]), (2.0, 50.0, 37.0, None, 24.0, 30, False))
        self.assertEqual((SOL_CONFIG["vol_mult"], SOL_CONFIG["within_bp"],
                          SOL_CONFIG["tp1_bp"], SOL_CONFIG["tp2_bp"],
                          SOL_CONFIG["stop_bp"], SOL_CONFIG["time_stop_min"],
                          SOL_CONFIG["oi_leg"], SOL_CONFIG["oi_min"]),
                         (1.8, 40.0, 42.0, 62.0, 21.0, 25, True, -1.5))


class TestKillAndBracket(unittest.TestCase):
    def test_volume_and_funding_kills(self):
        self.assertEqual(evaluate_kill(ETH_CONFIG, vol_ok_15=False),
                         (True, "entry_volume_recheck_fail"))
        self.assertEqual(evaluate_kill(ETH_CONFIG, funding_bp=-0.1),
                         (True, "funding_flip_negative"))
        self.assertEqual(evaluate_kill(ETH_CONFIG, funding_bp=None), (False, ""))
        self.assertEqual(evaluate_kill(ETH_CONFIG), (False, ""))

    def test_sol_deep_funding_precedence(self):
        # SOL: funding < -5bp outranks the generic flip (telemetry reason)
        self.assertEqual(evaluate_kill(SOL_CONFIG, funding_bp=-6.0),
                         (True, "funding_deep_negative"))
        self.assertEqual(evaluate_kill(SOL_CONFIG, funding_bp=-1.0),
                         (True, "funding_flip_negative"))
        # ETH has no deep-funding class — -6bp is still just a flip
        self.assertEqual(evaluate_kill(ETH_CONFIG, funding_bp=-6.0),
                         (True, "funding_flip_negative"))

    def test_zone_reentry_within_2_bars(self):
        zone = 103.0
        self.assertEqual(evaluate_kill(ETH_CONFIG, zone=zone, close_now=102.9, bars_held=1),
                         (True, "zone_reentry_within_2_bars"))
        self.assertEqual(evaluate_kill(ETH_CONFIG, zone=zone, close_now=102.9,
                                       bars_held=ZONE_REENTRY_BARS),
                         (True, "zone_reentry_within_2_bars"))
        self.assertEqual(evaluate_kill(ETH_CONFIG, zone=zone, close_now=102.9, bars_held=3),
                         (False, ""))
        self.assertEqual(evaluate_kill(ETH_CONFIG, zone=zone, close_now=103.1, bars_held=1),
                         (False, ""))
        self.assertEqual(evaluate_kill(ETH_CONFIG, zone=None, close_now=102.9, bars_held=1),
                         (False, ""))

    def test_sol_oi_second_day_kill(self):
        self.assertEqual(evaluate_kill(SOL_CONFIG, oi_chg_24h=-2.5),
                         (True, "oi_24h_unwind_day2"))
        self.assertEqual(evaluate_kill(SOL_CONFIG, oi_chg_24h=-1.9), (False, ""))
        # ETH: the OI kill class is absent entirely
        self.assertEqual(evaluate_kill(ETH_CONFIG, oi_chg_24h=-99.0), (False, ""))

    def test_bracket_arithmetic(self):
        stop, tp1, tp2 = bracket(ETH_CONFIG, 2500.0)
        self.assertAlmostEqual(stop, 2500.0 * (1 - 0.0024))
        self.assertAlmostEqual(tp1, 2500.0 * 1.0037)
        self.assertIsNone(tp2)
        stop, tp1, tp2 = bracket(SOL_CONFIG, 100.0)
        self.assertAlmostEqual(stop, 100.0 * (1 - 0.0021))
        self.assertAlmostEqual(tp1, 100.0 * 1.0042)
        self.assertAlmostEqual(tp2, 100.0 * 1.0062)


# ── S10 VWAP reanchor pins (Governor 2026-09-22, LIVE from birth) ──

from intelligence.sc23_breakout import (  # noqa: E402
    VWAP_REANCHOR_ENABLED, RA_SECOND_TOUCH_MULT,
    reanchor_size_mult, evaluate_reanchor_entry, evaluate_reanchor_kill,
    reanchor_bracket,
)

# Geometry: vwap 100.0, H1 101.0 (above vwap), L1 99.0, L2 99.4
# (higher low: 99.4 > 99.0*1.003 = 99.297), price 100.1 (within 0.2%
# above vwap: 100 <= 100.1 <= 100.2), up-leg vol 100, pullback vol 50
# (< 60), hour 12 UTC, first touch.
_VWAP, _H1, _L1, _L2, _PX = 100.0, 101.0, 99.0, 99.4, 100.1


def _ra(**kw):
    args = dict(vwap=_VWAP, h1=_H1, l1=_L1, l2=_L2, price=_PX,
                up_leg_volume=100.0, pullback_volume=50.0, hour_utc=12,
                touch_count=0)
    args.update(kw)
    return evaluate_reanchor_entry(**args)


class TestReanchorSizeMult(unittest.TestCase):
    def test_ladder(self):
        self.assertEqual(reanchor_size_mult(0), 1.0)
        self.assertEqual(reanchor_size_mult(1), RA_SECOND_TOUCH_MULT)
        self.assertEqual(reanchor_size_mult(2), 0.0)
        self.assertEqual(reanchor_size_mult(5), 0.0)

    def test_dark_on_none(self):
        self.assertIsNone(reanchor_size_mult(None))


class TestEvaluateReanchorEntry(unittest.TestCase):
    def test_full_pass_fires_long_live(self):
        v = _ra()
        self.assertTrue(v["ok"])
        self.assertEqual(v["direction"], "long")
        self.assertEqual(v["size_mult"], 1.0)
        self.assertFalse(v["shadow_only"])
        self.assertTrue(VWAP_REANCHOR_ENABLED)

    def test_second_touch_sizes_three_quarter(self):
        v = _ra(touch_count=1)
        self.assertTrue(v["ok"])
        self.assertEqual(v["size_mult"], 0.75)

    def test_third_touch_skipped(self):
        v = _ra(touch_count=2)
        self.assertFalse(v["ok"])
        self.assertEqual(v["legs"]["touch_budget"], "fail")
        self.assertEqual(v["size_mult"], 0.0)

    def test_each_leg_fail_and_dark(self):
        self.assertEqual(_ra(h1=100.0)["legs"]["h1_above_vwap"], "fail")
        self.assertEqual(_ra(h1=None)["legs"]["h1_above_vwap"], "dark")
        self.assertEqual(_ra(l2=99.29)["legs"]["higher_low"], "fail")
        self.assertEqual(_ra(l2=99.2971)["legs"]["higher_low"], "pass")
        self.assertEqual(_ra(l1=None)["legs"]["higher_low"], "dark")
        self.assertEqual(_ra(price=100.21)["legs"]["vwap_touch"], "fail")
        self.assertEqual(_ra(price=100.2)["legs"]["vwap_touch"], "pass")
        self.assertEqual(_ra(price=99.99)["legs"]["vwap_touch"], "fail")
        self.assertEqual(_ra(vwap=None)["legs"]["vwap_touch"], "dark")
        self.assertEqual(_ra(pullback_volume=60.0)["legs"]["volume_contract"],
                         "fail")
        self.assertEqual(_ra(pullback_volume=59.9)["legs"]["volume_contract"],
                         "pass")
        self.assertEqual(_ra(up_leg_volume=None)["legs"]["volume_contract"],
                         "dark")

    def test_window_guard(self):
        self.assertEqual(_ra(hour_utc=8)["legs"]["window"], "pass")
        self.assertEqual(_ra(hour_utc=15)["legs"]["window"], "pass")
        self.assertEqual(_ra(hour_utc=16)["legs"]["window"], "fail")
        self.assertEqual(_ra(hour_utc=7)["legs"]["window"], "fail")
        self.assertEqual(_ra(hour_utc=None)["legs"]["window"], "dark")
        v = _ra(low_volume_window=True)
        self.assertEqual(v["legs"]["window"], "fail")
        self.assertFalse(v["ok"])

    def test_disabled_never_fires(self):
        v = _ra(enabled=False)
        self.assertFalse(v["ok"])
        self.assertFalse(v["enabled"])


class TestEvaluateReanchorKill(unittest.TestCase):
    def test_structure_broken(self):
        self.assertEqual(evaluate_reanchor_kill(99.39, _VWAP, _L2),
                         (True, "structure_broken"))
        self.assertEqual(evaluate_reanchor_kill(99.4, _VWAP, _L2), (False, ""))

    def test_missed_reanchor(self):
        self.assertEqual(evaluate_reanchor_kill(101.01, _VWAP, _L2),
                         (True, "missed_reanchor"))
        self.assertEqual(evaluate_reanchor_kill(101.0, _VWAP, _L2), (False, ""))

    def test_fail_open_on_none(self):
        self.assertEqual(evaluate_reanchor_kill(None, _VWAP, _L2), (False, ""))
        self.assertEqual(evaluate_reanchor_kill(99.0, None, None), (False, ""))


class TestReanchorBracket(unittest.TestCase):
    def test_geometry(self):
        entry, stop, tp1, tp2 = reanchor_bracket(_VWAP, _H1, _L2)
        self.assertAlmostEqual(entry, 100.1)          # vwap + 0.1%
        self.assertAlmostEqual(stop, 99.4 * 0.997)    # L2 - 0.3%
        self.assertAlmostEqual(tp1, 101.0)            # H1
        self.assertAlmostEqual(tp2, 101.0 + (101.0 - 99.4) * 1.618)

    def test_long_ordering(self):
        entry, stop, tp1, tp2 = reanchor_bracket(_VWAP, _H1, _L2)
        self.assertLess(stop, entry)
        self.assertLess(entry, tp1)
        self.assertLess(tp1, tp2)


if __name__ == "__main__":
    unittest.main()
