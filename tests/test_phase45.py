import unittest
from tests.helpers import test_config
from funding.history import FundingHistory
from funding.radar import FundingRadar

# SoDEX funding rates are raw decimals (e.g. 0.0000125 = 0.00125% hourly).
# These tests use realistic values against the calibrated thresholds.

class TestFundingHistory(unittest.TestCase):

    def test_carry_score_extreme_positive(self):
        history = FundingHistory()
        # 0.002 = 0.2% hourly — extreme positive; shorts pay longs
        history.add("BTC-USD", 0.002, "derived")
        score = history.carry_score("BTC-USD")
        self.assertEqual(score, 3.0)

    def test_carry_score_neutral(self):
        history = FundingHistory()
        # 0.00005 = 0.005% hourly — inside neutral band (< 0.0001)
        history.add("BTC-USD", 0.00005, "derived")
        score = history.carry_score("BTC-USD")
        self.assertEqual(score, 0.0)

    def test_avg_calculation(self):
        history = FundingHistory()
        for rate in [0.01, 0.02, 0.03]:
            history.add("BTC-USD", rate, "derived")
        avg = history.avg("BTC-USD", hours=3)
        self.assertAlmostEqual(avg, 0.02, places=3)

class TestFundingRadar(unittest.TestCase):

    def test_arb_signal_fires_at_threshold(self):
        history = FundingHistory()
        # Two consecutive high rates — score reaches 3.0 (>= arb threshold of 2.5)
        history.add("BTC-USD", 0.002, "derived")
        history.add("BTC-USD", 0.002, "derived")
        radar = FundingRadar(
            config=test_config(),
            trade_flow_stores={},
            history=history
        )
        snap = radar.build_snapshot("BTC-USD")
        self.assertTrue(snap.arb_signal)
        # Positive funding -> Shorts pay Longs -> Arb is to go Short Perps (collect funding)
        self.assertEqual(snap.direction, "short_arb")

    def test_no_arb_neutral_funding(self):
        history = FundingHistory()
        # 0.00005 = 0.005% hourly — neutral, no arb signal
        history.add("BTC-USD", 0.00005, "derived")
        radar = FundingRadar(
            config=test_config(),
            trade_flow_stores={},
            history=history
        )
        snap = radar.build_snapshot("BTC-USD")
        self.assertFalse(snap.arb_signal)

if __name__ == "__main__":
    unittest.main()
