import unittest
import asyncio
from tests.helpers import test_config, make_test_candidate
from risk.margin_engine import MarginEngine
from risk.risk_engine import RiskEngine
from risk.position_manager import PositionManager
from execution.paper_client import PaperClient
from execution.nonce_manager import NonceManager

class TestMarginEngine(unittest.TestCase):

    def test_liquidation_price_long(self):
        engine = MarginEngine()
        # BTC, entry=70000, leverage=3, side=long
        liq = engine.compute_liquidation_price(
            symbol="BTC-USD",
            entry_price=70000,
            side=1, # long
            leverage=3,
            size=0.01
        )
        self.assertLess(liq, 70000)
        self.assertGreater(liq, 40000)

    def test_stop_is_safe_passes(self):
        engine = MarginEngine()
        safe, reason = engine.stop_is_safe(
            entry_price=70000,
            stop_price=69000,
            side=1,
            leverage=3,
            symbol="BTC-USD",
            size=0.01,
            atr_ratio=1.0
        )
        self.assertTrue(safe)

    def test_stop_too_close_to_liq_fails(self):
        engine = MarginEngine()
        # High leverage, stop very far (beyond liq)
        safe, reason = engine.stop_is_safe(
            entry_price=70000,
            stop_price=40000,
            side=1,
            leverage=25,
            symbol="BTC-USD",
            size=1.0,
            atr_ratio=1.0
        )
        self.assertFalse(safe)

class TestRiskEngine(unittest.TestCase):

    def test_gate4_coherence_blocks(self):
        config = test_config()
        config.live_min_coherence = 4
        engine = RiskEngine(config, MarginEngine(), PositionManager(), None, None, None, None)
        candidate = make_test_candidate("BTC-USD")
        candidate.coherence_score = 2
        approved, reason = engine.validate(candidate, 1000)
        self.assertFalse(approved)
        self.assertIn("COHERENCE", reason)

    def test_gate5_rr_blocks(self):
        config = test_config()
        engine = RiskEngine(config, MarginEngine(), PositionManager(), None, None, None, None)
        candidate = make_test_candidate("BTC-USD")
        candidate.rr_ratio = 1.0 # Min RR is usually 2.0
        approved, reason = engine.validate(candidate, 1000)
        self.assertFalse(approved)
        self.assertIn("RR", reason)

class TestNonceManager(unittest.TestCase):

    def test_nonces_are_unique(self):
        mgr = NonceManager(api_key="test_key")
        nonces = [mgr.next_nonce() for _ in range(100)]
        self.assertEqual(len(set(nonces)), 100)

class TestPaperClient(unittest.TestCase):

    def test_place_order_returns_fill(self):
        client = PaperClient(test_config())
        loop = asyncio.get_event_loop()
        result = loop.run_until_complete(client.place_order({
            "orders": [{
                "symbol": "BTC-USD",
                "side": 1,
                "price": 70000.0,
                "quantity": 0.01,
                "type": 2 # Limit
            }]
        }))
        self.assertIsNotNone(result.order_id)
        self.assertEqual(result.status, "filled")

    def test_balance_decreases_after_order(self):
        config = test_config()
        client = PaperClient(config)
        loop = asyncio.get_event_loop()
        initial = loop.run_until_complete(client.get_account_balance("paper"))
        
        loop.run_until_complete(client.place_order({
            "orders": [{
                "symbol": "BTC-USD",
                "side": 1,
                "price": 70000.0,
                "quantity": 0.01,
                "type": 2 # Limit
            }]
        }))
        
        after = loop.run_until_complete(client.get_account_balance("paper"))
        self.assertLess(after, initial)

if __name__ == "__main__":
    unittest.main()
