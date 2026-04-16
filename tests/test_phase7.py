import unittest
from datetime import datetime, timezone
from tests.helpers import make_test_candles, mock_position, make_aligned_market_state
from intelligence.stop_clusters import StopClusterMap
from intelligence.market_hours import MarketHoursGate
from risk.position_manager import PositionManager
from intelligence.coherence import CoherenceEngine

class TestStopClusters(unittest.TestCase):

    def test_round_number_cluster_found(self):
        clusters = StopClusterMap()
        # BTC, price=70000. Round numbers every 500.
        cluster_map = clusters.build_map(
            symbol="BTC-USD",
            current_price=70000.0,
            candles=make_test_candles(25, base_price=70000)
        )
        btc_rounds = [c for c in cluster_map if c.source == "round_number"]
        self.assertGreater(btc_rounds[0].price, 60000)
        self.assertGreater(len(btc_rounds), 0)

    def test_sweep_rejected_without_cluster(self):
        clusters = StopClusterMap()
        # No clusters initialized
        valid, strength = clusters.validate_sweep(
            symbol="BTC-USD",
            sweep_price=70123.45,
            sweep_side="short_stops"
        )
        self.assertFalse(valid)

    def test_sweep_validated_at_round_number(self):
        clusters = StopClusterMap()
        # Initialize clusters
        clusters.build_map("BTC-USD", 70000.0, make_test_candles(25, 70000))
        # 70500 is a BTC round number (increment 500)
        valid, strength = clusters.validate_sweep(
            symbol="BTC-USD",
            sweep_price=70500.0,
            sweep_side="short_stops"
        )
        self.assertTrue(valid)

class TestMarketHours(unittest.TestCase):

    def test_gold_blocked_saturday(self):
        gate = MarketHoursGate()
        # Saturday, April 11, 2026
        saturday = datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc)
        ok, reason = gate.should_trade_symbol("XAUT-USD", saturday)
        self.assertFalse(ok)
        self.assertIn("COMMODITY_MARKET_CLOSED", reason)

    def test_crypto_always_open(self):
        gate = MarketHoursGate()
        saturday = datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc)
        ok, reason = gate.should_trade_symbol("BTC-USD", saturday)
        self.assertTrue(ok)

    def test_gold_open_weekday(self):
        gate = MarketHoursGate()
        # Wednesday, April 8, 2026
        wednesday = datetime(2026, 4, 8, 14, 0, tzinfo=timezone.utc)
        ok, reason = gate.should_trade_symbol("XAUT-USD", wednesday)
        self.assertTrue(ok)

class TestGoldenStop(unittest.TestCase):

    def test_golden_stop_long(self):
        pm = PositionManager()
        pos = mock_position(
            side="long",
            entry_price=70000,
            tp1_price=70600
        )
        pm.add(pos)
        new_stop = pm.mark_tp1_hit("BTC-USD", 0)
        # Entry + 50% of distance to TP1 = 70000 + (600 * 0.5) = 70300
        expected = 70300.0
        self.assertAlmostEqual(new_stop, expected)
        self.assertEqual(pos.stop_price, expected)
        self.assertTrue(pos.tp1_hit)

    def test_golden_stop_short(self):
        pm = PositionManager()
        pos = mock_position(
            side="short",
            entry_price=70000,
            tp1_price=69400
        )
        pm.add(pos)
        new_stop = pm.mark_tp1_hit("BTC-USD", 0)
        # Entry - 50% of distance to TP1 = 70000 - (600 * 0.5) = 69700
        expected = 69700.0
        self.assertAlmostEqual(new_stop, expected)
        self.assertEqual(pos.stop_price, expected)
        self.assertTrue(pos.tp1_hit)

class TestWeightedCoherence(unittest.TestCase):

    def test_strong_micro_scores_higher(self):
        engine = CoherenceEngine(stop_clusters=StopClusterMap())
        # Mock sweep validation
        engine.stop_clusters.validate_sweep = lambda s, p, side: (True, 0.9)
        
        # Scenario 1: High VPIN
        output1 = {
            "sweep": "buy_side", "sweep_price": 70000, "sweep_side": "short_stops",
            "vpin": 0.85, "ssi_status": "none", "regime": "neutral", "market_type": "chop", "funding_class": "neutral"
        }
        score1, _, _ = engine.calculate_weighted_score("BTC-USD", output1)
        
        # Scenario 2: Low VPIN
        output2 = output1.copy()
        output2["vpin"] = 0.45
        score2, _, _ = engine.calculate_weighted_score("BTC-USD", output2)
        
        # High VPIN should have a 0.5 weight advantage in microstructure tier
        self.assertGreater(score1, score2)

if __name__ == "__main__":
    unittest.main()
