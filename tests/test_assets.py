import unittest
from tests.helpers import test_config, make_test_candles, make_candle_buffers
from intelligence.relative_strength import RelativeStrengthEngine
from intelligence.stop_clusters import StopClusterMap

class TestAssetExpansion(unittest.TestCase):

    def test_all_7_assets_in_config(self):
        config = test_config()
        expected = {"BTC", "ETH", "SOL", "XAUT", "BNB", "LINK", "AVAX"}
        self.assertEqual(set(config.assets), expected)

    def test_asset_config_complete(self):
        # Import ASSET_CONFIG from config if it was implemented there
        from core.config import Settings
        config = Settings()
        asset_config = getattr(config, "ASSET_CONFIG", {})
        for asset in ["BTC", "ETH", "SOL", "XAUT", "BNB", "LINK", "AVAX"]:
            self.assertIn(asset, asset_config)
            self.assertIn("tick_size", asset_config[asset])
            self.assertIn("category", asset_config[asset])

    def test_regime_handles_7_assets(self):
        config = test_config()
        engine = RelativeStrengthEngine(config)
        
        # Populate buffers for all assets
        candle_buffers = {}
        for asset in config.assets:
            cb = make_candle_buffers(asset)
            # Add 100 15m candles
            for c in make_test_candles(100):
                cb["15m"].add(c)
            candle_buffers[asset] = cb
            
        matrix = engine.compute_regime(candle_buffers)
        self.assertIn(matrix.regime, [
            "risk_on", "risk_off", "alt_season",
            "btc_dominance", "defi_stress", "cex_flow", "confused"
        ])
        self.assertIn(matrix.leading_category, engine.categories.keys())

    def test_new_asset_round_numbers(self):
        clusters = StopClusterMap()
        # BNB price ~400. Round numbers every $5
        # 400, 405, 410...
        cluster_map = clusters.build_map(
            symbol="BNB",
            current_price=400.0,
            candles=make_test_candles(25, 400.0)
        )
        self.assertGreater(len(cluster_map), 0)
        bnb_increments = [c.price for c in cluster_map if c.source == "round_number"]
        # Check if 405 or 395 is there
        self.assertTrue(any(abs(p - 405.0) < 0.01 or abs(p - 395.0) < 0.01 for p in bnb_increments))

if __name__ == "__main__":
    unittest.main()
