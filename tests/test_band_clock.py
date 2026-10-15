"""Unit tests for intelligence/band_clock.py — pure brain, no I/O."""
import unittest

from intelligence.band_clock import (BandClock, RUNGS, anchor_reversion_verdict,
                                     band_rung, dist_to_bounds_pct,
                                     expansion_imminent, session_bridge_verdict)

# INTC-USD off-hours: anchor 150.25, imf 1%, band 0.5 rung → ±0.751
_OFF = {"marketId": 12, "marketDisplayName": "INTC-USD", "isOutsideRth": True,
        "offHoursInitialMarginFraction": "0.01",
        "currentSettlementPrice": "150.25",
        "upperTradingBound": "151.0013", "lowerTradingBound": "149.4987",
        "nextUpperTradingBound": "151.75", "nextLowerTradingBound": "148.75",
        "isUpperInExpansionZone": False, "isLowerInExpansionZone": False,
        "boundEvent": None, "boundSide": None, "timestamp": 1_760_000_000_000_000}


class TestRung(unittest.TestCase):
    def test_rung_estimates(self):
        self.assertEqual(band_rung(_OFF), 0.5)
        widened = dict(_OFF, upperTradingBound="151.7538",
                       lowerTradingBound="148.7462")   # ±1% = rung 1.0
        self.assertEqual(band_rung(widened), 1.0)
        wide4 = dict(_OFF, upperTradingBound="156.26",
                     lowerTradingBound="144.24")        # ±4% ≈ rung 4
        self.assertEqual(band_rung(wide4), 4.0)

    def test_rung_none_on_incomplete(self):
        self.assertIsNone(band_rung({"currentSettlementPrice": None}))
        self.assertIsNone(band_rung(dict(_OFF, upperTradingBound=None)))
        self.assertIsNone(band_rung(dict(_OFF, currentSettlementPrice="0")))


class TestDistances(unittest.TestCase):
    def test_dist(self):
        d = dist_to_bounds_pct(_OFF, 150.25)
        self.assertAlmostEqual(d["upper_pct"], 0.5, places=2)
        self.assertAlmostEqual(d["lower_pct"], 0.5, places=2)

    def test_null_bounds(self):
        d = dist_to_bounds_pct(dict(_OFF, upperTradingBound=None), 150.0)
        self.assertIsNone(d["upper_pct"])
        self.assertIsNotNone(d["lower_pct"])


class TestExpansionImminent(unittest.TestCase):
    def test_clock_within_window(self):
        e = dict(_OFF, isUpperInExpansionZone=True,
                 upperExpectedExpansionAt=1_000_300)
        r = expansion_imminent(e, now_s=1_000_000, within_s=600)
        self.assertTrue(r["upper"])
        self.assertFalse(r["lower"])

    def test_not_imminent_when_far_or_exited(self):
        e = dict(_OFF, isUpperInExpansionZone=True,
                 upperExpectedExpansionAt=1_001_000)
        self.assertFalse(expansion_imminent(e, 1_000_000, 600)["upper"])
        e2 = dict(e, isUpperInExpansionZone=False)
        self.assertFalse(expansion_imminent(e2, 1_000_300, 600)["upper"])

    def test_past_clock_not_imminent(self):
        e = dict(_OFF, isLowerInExpansionZone=True,
                 lowerExpectedExpansionAt=999_000)
        self.assertFalse(expansion_imminent(e, 1_000_000, 3600)["lower"])


class TestAnchorReversion(unittest.TestCase):
    def test_arms_short_at_upper_edge(self):
        # mark pinned at the upper bound; anchor 0.5% below
        v = anchor_reversion_verdict(_OFF, 151.00, now_s=1.0)
        self.assertTrue(v["armed"])
        self.assertEqual(v["direction"], "short")
        self.assertAlmostEqual(v["anchor"], 150.25)
        self.assertEqual(v["rung"], 0.5)

    def test_arms_long_at_lower_edge(self):
        v = anchor_reversion_verdict(_OFF, 149.50, now_s=1.0)
        self.assertTrue(v["armed"])
        self.assertEqual(v["direction"], "long")

    def test_mid_band_refused(self):
        v = anchor_reversion_verdict(_OFF, 150.25, now_s=1.0)
        self.assertFalse(v["armed"])
        self.assertEqual(v["reason"], "mid_band")

    def test_in_rth_refused(self):
        v = anchor_reversion_verdict(dict(_OFF, isOutsideRth=False),
                                     151.00, now_s=1.0)
        self.assertFalse(v["armed"])
        self.assertEqual(v["reason"], "in_rth_or_unknown")

    def test_no_anchor_fail_closed(self):
        e = dict(_OFF, currentSettlementPrice=None)
        v = anchor_reversion_verdict(e, 151.00, now_s=1.0)
        self.assertFalse(v["armed"])
        self.assertEqual(v["reason"], "no_anchor")

    def test_min_room_enforced(self):
        tight = dict(_OFF, currentSettlementPrice="150.999")
        v = anchor_reversion_verdict(tight, 151.00, now_s=1.0)
        self.assertFalse(v["armed"])


class TestSessionBridge(unittest.TestCase):
    def test_gap_direction(self):
        v = session_bridge_verdict(_OFF, 151.50)
        self.assertTrue(v["valid"])
        self.assertEqual(v["direction"], "long")
        self.assertAlmostEqual(v["gap_pct"], 0.8319, places=3)
        v2 = session_bridge_verdict(_OFF, 149.00)
        self.assertEqual(v2["direction"], "short")

    def test_invalid_without_anchor(self):
        v = session_bridge_verdict(dict(_OFF, currentSettlementPrice=None),
                                   150.0)
        self.assertFalse(v["valid"])


class TestBandClockTracker(unittest.TestCase):
    def test_ingest_and_verdict(self):
        bc = BandClock()
        self.assertIsNone(bc.ingest_entry(dict(_OFF), is_snapshot=True))
        self.assertEqual(bc.verdict("INTC-USD", 151.00, 1.0)["rung"], 0.5)
        self.assertFalse(bc.verdict("DOGE-USD", 1.0, 1.0)["tracked"])

    def test_streaming_events_ring_and_rate(self):
        bc = BandClock()
        e1 = dict(_OFF, boundEvent="tradingBoundExpanded", boundSide="upper",
                  timestamp=2_000_000_000_000)
        rec = bc.ingest_entry(e1, is_snapshot=False)
        self.assertEqual(rec["event"], "tradingBoundExpanded")
        e2 = dict(_OFF, boundEvent="expansionZoneEntered", boundSide="lower",
                  timestamp=2_100_000_000_000)
        bc.ingest_entry(e2, is_snapshot=False)
        self.assertEqual(len(bc.recent_events("INTC-USD")), 2)
        rate = bc.expansion_rate("INTC-USD", window_us=200_000_000_000,
                                 now_us=2_100_000_000_000)
        self.assertEqual(rate, 1)

    def test_snapshot_events_not_recorded(self):
        bc = BandClock()
        bc.ingest_entry(dict(_OFF, boundEvent="exitRth"), is_snapshot=True)
        self.assertEqual(bc.recent_events(), [])

    def test_event_cap(self):
        bc = BandClock()
        bc._event_cap = 10
        for i in range(25):
            bc.ingest_entry(dict(_OFF, boundEvent="expansionZoneEntered",
                                 timestamp=1_000 + i), is_snapshot=False)
        self.assertEqual(len(bc.recent_events(limit=100)), 10)


if __name__ == "__main__":
    unittest.main()
