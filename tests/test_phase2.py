import unittest
import time
from tests.helpers import make_test_candles, test_config, make_neutral_market_state, make_aligned_market_state
from core.structure_analyzer import StructureAnalyzer
from core.microstructure_analyzer import MicrostructureAnalyzer
from core.funding_analyzer import FundingAnalyzer
from intelligence.coherence import CoherenceEngine
from intelligence.stop_clusters import StopClusterMap

class TestATR(unittest.TestCase):

    def test_atr_calculation(self):
        analyzer = StructureAnalyzer()
        candles = make_test_candles(30, base_price=70000, volatility=200)
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        closes = [c.close for c in candles]
        atr = analyzer._calculate_atr(highs, lows, closes, period=14)
        self.assertGreater(atr, 0)
        self.assertLess(atr, 1000)

class TestRegimeClassifier(unittest.TestCase):

    def test_expansion_regime(self):
        analyzer = StructureAnalyzer()
        # Mock history to have a baseline
        analyzer.atr_history["BTC-USD"] = [200.0] * 20
        # Current ATR > 1.5 * baseline
        market_type = analyzer._determine_market_type(
            price_data=[70000.0]*20,
            volume_data=[1000.0]*20,
            atr=400.0,
            atr_vs_baseline=2.0
        )
        # Note: _determine_market_type also checks volume_trend for "expansion"
        # Since volume is flat, it might return "chop" or something else.
        # Let's adjust volume to have a positive trend.
        market_type = analyzer._determine_market_type(
            price_data=[70000.0]*20,
            volume_data=list(range(1000, 1020)),
            atr=400.0,
            atr_vs_baseline=2.0
        )
        self.assertEqual(market_type, "expansion")

    def test_compression_regime(self):
        analyzer = StructureAnalyzer()
        market_type = analyzer._determine_market_type(
            price_data=[70000.0]*20,
            volume_data=[1000.0]*20,
            atr=100.0,
            atr_vs_baseline=0.5
        )
        self.assertEqual(market_type, "compression")

class TestSweepDetector(unittest.TestCase):

    def test_no_sweep_normal_candle(self):
        analyzer = MicrostructureAnalyzer()
        # Mock analyze_microstructure call or internals
        # For simplicity, we test the logic via the main entry point
        sweep, _, _, _, _, _, _ = analyzer.analyze_microstructure(
            "BTC-USD",
            orderbook_data={"bids": [[70000, 1]], "asks": [[70001, 1]]},
            trade_data=[],
            mark_price=70000.5
        )
        self.assertEqual(sweep, "none")

class TestFundingClassifier(unittest.TestCase):

    def test_extreme_positive(self):
        analyzer = FundingAnalyzer()
        result = analyzer.analyze_funding("BTC-USD", 0.06, [], 70000, 70000)
        self.assertEqual(result, "extreme_positive")

    def test_neutral(self):
        analyzer = FundingAnalyzer()
        result = analyzer.analyze_funding("BTC-USD", 0.0005, [], 70000, 70000)
        self.assertEqual(result, "neutral")

class TestCoherenceScorer(unittest.TestCase):

    def test_no_micro_signal_returns_zero(self):
        engine = CoherenceEngine()
        analyzers_output = {
            "sweep": "none",
            "vpin": 0.2,
            "regime": "neutral",
            "market_type": "chop"
        }
        score, raw, components = engine.calculate_weighted_score("BTC-USD", analyzers_output)
        self.assertEqual(score, 0)
        self.assertEqual(raw, 0)

    def test_full_alignment_high_score(self):
        engine = CoherenceEngine(stop_clusters=StopClusterMap())
        # Mock stop cluster for validation
        engine.stop_clusters._clusters["BTC-USD"] = [
            # Dummy cluster
        ]
        # We need to mock validate_sweep to return True
        engine.stop_clusters.validate_sweep = lambda s, p, side: (True, 0.9)
        
        analyzers_output = {
            "sweep": "buy_side",
            "sweep_price": 70000,
            "sweep_side": "short_stops",
            "vpin": 0.8,
            "ssi_status": "strong_inflow",
            "regime": "risk_on",
            "market_type": "expansion",
            "funding_class": "extreme_negative"
        }
        score, raw, components = engine.calculate_weighted_score("BTC-USD", analyzers_output)
        self.assertGreaterEqual(score, 4)
        self.assertGreaterEqual(raw, 4)

if __name__ == "__main__":
    unittest.main()
