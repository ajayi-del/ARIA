"""Pins for the Conviction Review brain — v1 reproduction + the five book rules."""
import unittest

from intelligence.conviction_review import (
    PositionSnapshot, abandonment_verdict, adverse_pct, noise_band_pct,
    atr_pct_from_candles, BASE_GRACE_S, MIN_ADVERSE_PCT,
)

NOW = 1_000_000.0


def _snap(age=4000.0, side="long", upnl=-2.0, entry=100.0, size=1.0,
          im=40.0, trade_type="", atr_pct=None):
    return PositionSnapshot(symbol="X-USD", side=side, upnl=upnl, entry=entry,
                            size=size, age_s=age, initial_margin=im,
                            trade_type=trade_type, atr_pct=atr_pct)


class TestV1KillSwitch(unittest.TestCase):
    """v2_enabled=False must reproduce the old loop bit-for-bit."""

    def test_old_bleeding_abandons(self):
        v = abandonment_verdict(_snap(age=4000, upnl=-1.0, im=40.0),
                                now=NOW, v2_enabled=False)
        self.assertTrue(v.abandon)
        self.assertEqual(v.reason, "v1_abandon")

    def test_young_holds(self):
        v = abandonment_verdict(_snap(age=1700, upnl=-1.0, im=40.0),
                                now=NOW, v2_enabled=False)
        self.assertFalse(v.abandon)
        self.assertEqual(v.reason, "v1_hold_young")

    def test_roe_band_holds(self):
        # ROE -1.0% > -2.0% gate → hold regardless of age
        v = abandonment_verdict(_snap(age=9999, upnl=-0.4, im=40.0),
                                now=NOW, v2_enabled=False)
        self.assertFalse(v.abandon)
        self.assertEqual(v.reason, "v1_hold_roe_band")

    def test_winner_holds_documents_dead_branch(self):
        # v1's 60-min winner grace was unreachable; winners hold via the ROE gate
        v = abandonment_verdict(_snap(age=9999, upnl=+5.0, im=40.0),
                                now=NOW, v2_enabled=False)
        self.assertFalse(v.abandon)


class TestNoiseBand(unittest.TestCase):
    def test_flat_floor_reproduces_v1(self):
        # adverse 0.4% exactly at the floor → NOT bleeding (strictly below band)
        s = _snap(upnl=-0.399, entry=100.0, size=1.0)   # 0.399% adverse
        v = abandonment_verdict(s, now=NOW)
        self.assertEqual(v.reason, "hold_noise_band")

    def test_bleeding_passes_band(self):
        s = _snap(upnl=-0.5)                            # 0.5% adverse > 0.4%
        v = abandonment_verdict(s, now=NOW)
        self.assertNotEqual(v.reason, "hold_noise_band")

    def test_atr_widens_band(self):
        # ATR 2% × 0.5 = 1.0% band: 0.6% adverse is noise for this alt
        s = _snap(upnl=-0.6, atr_pct=0.02)
        v = abandonment_verdict(s, now=NOW)
        self.assertEqual(v.reason, "hold_noise_band")
        # but 1.2% adverse bleeds even on this alt
        s2 = _snap(upnl=-1.2, atr_pct=0.02)
        v2 = abandonment_verdict(s2, now=NOW)
        self.assertNotEqual(v2.reason, "hold_noise_band")

    def test_none_atr_falls_back_to_flat(self):
        self.assertEqual(noise_band_pct(_snap(atr_pct=None), 0.5), MIN_ADVERSE_PCT)

    def test_winner_never_bleeds(self):
        self.assertEqual(adverse_pct(_snap(upnl=+3.0)), 0.0)


class TestRegimeConditionalGrace(unittest.TestCase):
    """Lo — and the exact SOL 22:31 case from the 2026-08-22 audit."""

    def _sol(self, age=2163.0):
        # SOL aftermath long: entry 93.80, mark 93.42, size 2.18 → upnl -0.83
        return _snap(age=age, entry=93.80, size=2.18, upnl=-0.8284, im=40.0)

    def test_sol_case_unknown_verdict_still_abandons(self):
        v = abandonment_verdict(self._sol(), now=NOW, trend_verdict="unknown")
        self.assertTrue(v.abandon)
        self.assertEqual(v.reason, "signal_absent")

    def test_sol_case_aligned_verdict_holds(self):
        v = abandonment_verdict(self._sol(), now=NOW, trend_verdict="aligned")
        self.assertFalse(v.abandon)
        self.assertEqual(v.reason, "hold_aligned_grace")

    def test_aligned_grace_expires(self):
        v = abandonment_verdict(self._sol(age=7300.0), now=NOW, trend_verdict="aligned")
        self.assertTrue(v.abandon)

    def test_counter_verdict_keeps_base_grace(self):
        v = abandonment_verdict(self._sol(), now=NOW, trend_verdict="counter")
        self.assertTrue(v.abandon)
        self.assertEqual(v.grace_s, BASE_GRACE_S)

    def test_young_holds_regardless_of_verdict(self):
        v = abandonment_verdict(self._sol(age=900.0), now=NOW, trend_verdict="unknown")
        self.assertEqual(v.reason, "hold_young")


class TestThesisSignals(unittest.TestCase):
    """Raschke — support and inversion, direction-aware."""

    def test_same_dir_signal_reconfirms(self):
        v = abandonment_verdict(_snap(), now=NOW, last_same_dir_ts=NOW - 600.0)
        self.assertFalse(v.abandon)
        self.assertEqual(v.reason, "hold_signal_reconfirmed")

    def test_stale_same_dir_signal_does_not_reconfirm(self):
        v = abandonment_verdict(_snap(), now=NOW, last_same_dir_ts=NOW - 5000.0)
        self.assertTrue(v.abandon)

    def test_opposite_dir_signal_is_not_support(self):
        v = abandonment_verdict(_snap(), now=NOW, last_opp_dir_ts=NOW - 100.0)
        self.assertTrue(v.abandon)   # inversion needs counter verdict too

    def test_inversion_fires_early(self):
        v = abandonment_verdict(_snap(age=1000.0), now=NOW,
                                trend_verdict="counter",
                                last_opp_dir_ts=NOW - 300.0)
        self.assertTrue(v.abandon)
        self.assertEqual(v.reason, "thesis_inversion")

    def test_inversion_needs_fresh_opp_signal(self):
        v = abandonment_verdict(_snap(age=1000.0), now=NOW,
                                trend_verdict="counter",
                                last_opp_dir_ts=NOW - 2000.0)
        self.assertFalse(v.abandon)   # falls through to hold_young

    def test_inversion_needs_counter_verdict(self):
        v = abandonment_verdict(_snap(age=1000.0), now=NOW,
                                trend_verdict="unknown",
                                last_opp_dir_ts=NOW - 300.0)
        self.assertFalse(v.abandon)

    def test_inversion_never_kills_fresh_position(self):
        v = abandonment_verdict(_snap(age=800.0), now=NOW,
                                trend_verdict="counter",
                                last_opp_dir_ts=NOW - 100.0)
        self.assertFalse(v.abandon)

    def test_inversion_kill_switch(self):
        v = abandonment_verdict(_snap(age=1000.0), now=NOW,
                                trend_verdict="counter",
                                last_opp_dir_ts=NOW - 300.0,
                                inversion_enabled=False)
        self.assertFalse(v.abandon)

    def test_reconfirm_beats_inversion(self):
        # chop: both directions signaled recently — support wins
        v = abandonment_verdict(_snap(age=3000.0), now=NOW,
                                trend_verdict="counter",
                                last_same_dir_ts=NOW - 400.0,
                                last_opp_dir_ts=NOW - 300.0)
        self.assertEqual(v.reason, "hold_signal_reconfirmed")


class TestSwingExemption(unittest.TestCase):
    def test_aster_swing_exempt_at_any_age(self):
        v = abandonment_verdict(_snap(age=99999.0, trade_type="aster_swing"),
                                now=NOW)
        self.assertFalse(v.abandon)
        self.assertEqual(v.reason, "swing_class_exempt")

    def test_other_classes_not_exempt(self):
        v = abandonment_verdict(_snap(age=99999.0, trade_type="breakout"),
                                now=NOW)
        self.assertTrue(v.abandon)


class TestAtrHelper(unittest.TestCase):
    def test_known_series(self):
        class C:
            def __init__(self, h, l, c):
                self.high, self.low, self.close = h, l, c
        # 15 flat-range candles: high-low = 1.0, closes = 100 → ATR% = 1%
        candles = [C(100.5, 99.5, 100.0) for _ in range(15)]
        self.assertAlmostEqual(atr_pct_from_candles(candles), 0.01, places=4)

    def test_too_few_candles_returns_none(self):
        class C:
            high, low, close = 1.0, 1.0, 1.0
        self.assertIsNone(atr_pct_from_candles([C()] * 5))

    def test_garbage_returns_none(self):
        self.assertIsNone(atr_pct_from_candles([object()] * 20))


if __name__ == "__main__":
    unittest.main()
