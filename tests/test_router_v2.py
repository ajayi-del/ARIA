"""Unit tests for Router v2 shadow scorer + storm-mode conviction rebalance."""
import unittest

from execution.router_v2 import RouterV2, _NEVER
from intelligence.conviction_engine import compute_conviction


class TestRouterV2(unittest.TestCase):
    def setUp(self):
        self.r = RouterV2()

    def test_cheaper_venue_wins_on_fees_alone(self):
        # SoDEX taker 4.0bps vs Aster taker 4.0bps, Aster maker 0 — same fee,
        # same funding, fresh feeds → tie broken only by fee diff.
        s_sodex = self.r.score_venue(fee_bps=4.0, funding_rate=0.0,
                                     direction="long", feed_age_s=1.0)
        s_aster = self.r.score_venue(fee_bps=0.0, funding_rate=0.0,
                                     direction="long", feed_age_s=1.0)
        self.assertGreater(s_aster, s_sodex)

    def test_funding_carry_sign_flips_with_direction(self):
        # Positive funding: longs pay, shorts receive.
        long_s = self.r.score_venue(fee_bps=0.0, funding_rate=0.001,
                                    direction="long", feed_age_s=1.0)
        short_s = self.r.score_venue(fee_bps=0.0, funding_rate=0.001,
                                     direction="short", feed_age_s=1.0)
        self.assertEqual(long_s, -10.0)    # pays 10bps/8h
        self.assertEqual(short_s, 10.0)    # receives 10bps/8h

    def test_stale_feed_taxed_and_capped(self):
        fresh = self.r.score_venue(fee_bps=0.0, funding_rate=0.0,
                                   direction="long", feed_age_s=1.0)
        stale = self.r.score_venue(fee_bps=0.0, funding_rate=0.0,
                                   direction="long", feed_age_s=65.0)
        dead = self.r.score_venue(fee_bps=0.0, funding_rate=0.0,
                                  direction="long", feed_age_s=None)
        self.assertEqual(fresh, 0.0)
        self.assertEqual(stale, -20.0)     # (65-5)*0.5 capped at 20
        self.assertEqual(dead, -20.0)

    def test_halted_or_unlisted_never_chosen(self):
        self.assertEqual(self.r.score_venue(fee_bps=0.0, funding_rate=0.0,
                                            direction="long", feed_age_s=1.0,
                                            halted=True), _NEVER)
        self.assertEqual(self.r.score_venue(fee_bps=0.0, funding_rate=0.0,
                                            direction="long", feed_age_s=1.0,
                                            listed=False), _NEVER)

    def test_compare_reports_divergence_and_delta(self):
        v = self.r.compare("BTC-USD", "long", "sodex", {
            "sodex": {"fee_bps": 4.0, "funding_rate": 0.0005,
                      "direction": "long", "feed_age_s": 1.0},
            "aster": {"fee_bps": 0.0, "funding_rate": -0.0005,
                      "direction": "long", "feed_age_s": 1.0},
        })
        self.assertEqual(v["v2_choice"], "aster")
        self.assertTrue(v["diverges"])
        self.assertGreater(v["delta_bps"], 0.0)

    def test_compare_static_when_static_better(self):
        v = self.r.compare("BTC-USD", "short", "sodex", {
            "sodex": {"fee_bps": 0.0, "funding_rate": 0.0,
                      "direction": "short", "feed_age_s": 1.0},
            "aster": {"fee_bps": 4.0, "funding_rate": -0.001,
                      "direction": "short", "feed_age_s": 1.0},
        })
        self.assertEqual(v["v2_choice"], "sodex")
        self.assertFalse(v["diverges"])


class TestStormRebalance(unittest.TestCase):
    def test_calm_weights_unchanged(self):
        # No market_energy → legacy behavior bit-for-bit
        a = compute_conviction(coherence=6.0, cascade_active=True,
                               cascade_zscore=4.0)
        b = compute_conviction(coherence=6.0, cascade_active=True,
                               cascade_zscore=4.0, market_energy=30.0)
        self.assertEqual(a, b)

    def test_storm_amplifies_cascade_priority(self):
        calm = compute_conviction(coherence=6.0, cascade_active=True,
                                  cascade_zscore=4.0, market_energy=30.0)
        storm = compute_conviction(coherence=6.0, cascade_active=True,
                                   cascade_zscore=4.0, market_energy=85.0)
        # z=4 → cascade_score 1.0: storm swaps 0.10 of coh weight to cascade.
        # coh 6/9=0.667 loses 0.10×0.667=0.0667; cascade gains 0.10×1.0=0.10
        self.assertAlmostEqual(storm - calm, 0.10 - 0.10 * (6.0 / 9.0),
                               places=2)
        self.assertGreater(storm, calm)

    def test_storm_without_cascade_lowers_conviction(self):
        calm = compute_conviction(coherence=6.0, cascade_active=False,
                                  market_energy=30.0)
        storm = compute_conviction(coherence=6.0, cascade_active=False,
                                   market_energy=85.0)
        self.assertLess(storm, calm)   # no cascade to catch the moved weight


if __name__ == "__main__":
    unittest.main()
