import unittest
from tests.helpers import test_config
from funding.history import FundingHistory
from funding.radar import FundingRadar

class TestFundingHistory(unittest.TestCase):

    def test_carry_score_extreme_positive(self):
        history = FundingHistory()
        # Mocking add
        history.add("BTC", 0.06, "derived")
        score = history.carry_score("BTC")
        # Extreme positive funding (shorts pay longs) -> score should be high
        self.assertEqual(score, 3.0)

    def test_carry_score_neutral(self):
        history = FundingHistory()
        history.add("BTC", 0.005, "derived")
        score = history.carry_score("BTC")
        self.assertEqual(score, 0.0)

    def test_avg_calculation(self):
        history = FundingHistory()
        for rate in [0.01, 0.02, 0.03]:
            history.add("BTC", rate, "derived")
        avg = history.avg("BTC", hours=3)
        self.assertAlmostEqual(avg, 0.02, places=3)

class TestFundingRadar(unittest.TestCase):

    def test_arb_signal_fires_at_threshold(self):
        history = FundingHistory()
        # Two consecutive high funding rates
        history.add("BTC", 0.06, "derived")
        history.add("BTC", 0.06, "derived")
        radar = FundingRadar(
            config=test_config(),
            trade_flow_stores={},
            history=history
        )
        snap = radar.build_snapshot("BTC")
        self.assertTrue(snap.arb_signal)
        # Positive funding -> Shorts pay Longs -> Arb is to go Short Perps (collect funding)
        self.assertEqual(snap.direction, "short_arb")

    def test_no_arb_neutral_funding(self):
        history = FundingHistory()
        history.add("BTC", 0.005, "derived")
        radar = FundingRadar(
            config=test_config(),
            trade_flow_stores={},
            history=history
        )
        snap = radar.build_snapshot("BTC")
        self.assertFalse(snap.arb_signal)

if __name__ == "__main__":
    unittest.main()
