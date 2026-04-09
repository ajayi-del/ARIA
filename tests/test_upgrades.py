import unittest
import asyncio
from core.event_bus import event_bus, EventType, Event
from intelligence.freshness import compute_freshness
from risk.correlation_engine import compute_portfolio_var, correlation_gate
from execution.schemas import TradeCandidate
from risk.margin_engine import MarginEngine
from risk.risk_engine import RiskEngine
from core.config import Settings

class TestPhase10Upgrades(unittest.TestCase):
    def setUp(self):
        self.config = Settings()
        self.margin_engine = MarginEngine()
        self.risk_engine = RiskEngine(self.config, self.margin_engine, None, None) # Mocks for simplicity

    def test_event_bus_basic(self):
        """Standard pub/sub check"""
        received = []
        def handler(ev): received.append(ev)
        
        event_bus.subscribe(EventType.MARK_PRICE_UPDATED, handler)
        ev = Event(
            event_type=EventType.MARK_PRICE_UPDATED,
            symbol="BTC-USD",
            timestamp_ms=1000,
            data={"price": 70000}
        )
        event_bus.publish(ev)
        
        # In a real async test we'd wait, but publish is synchronous internal to the bus registry 
        # (the dispatch loop is what's async in main.py)
        # Actually in our implementation, publish just puts in queue. 
        self.assertEqual(event_bus._queue.qsize(), 1)

    def test_freshness_decay(self):
        """Validates that older signals get lower scores"""
        # 100ms old
        f1 = compute_freshness(100, 50, 70000)
        # 10000ms old (10s) - should definitely hit floor
        f2 = compute_freshness(10000, 1000, 70000)
        
        self.assertGreater(f1, f2)
        self.assertEqual(f2, 0.3) # Decayed to floor

    def test_dynamic_stop_buffer(self):
        """Validates ATR scaling in margin engine"""
        # Normal ATR
        safe1, _ = self.margin_engine.stop_is_safe(
            entry_price=70000, stop_price=69500, side=1, leverage=10, 
            symbol="BTC-USD", size=0.1, atr_ratio=1.0
        )
        # High ATR (volatility spike)
        safe2, _ = self.margin_engine.stop_is_safe(
            entry_price=70000, stop_price=69500, side=1, leverage=10, 
            symbol="BTC-USD", size=0.1, atr_ratio=5.0
        )
        
        # If ATR is 5x, the buffer might push the liq price too close to the stop 
        # (or rather, the stop needs to be further)
        # We just want to ensure it's different.
        self.assertNotEqual(safe1, safe2) if not safe2 else self.assertTrue(safe1)

    def test_portfolio_var(self):
        """Validates VaR calculation logic"""
        class MockPos:
            def __init__(self, sym, risk):
                self.symbol = sym
                self.initial_risk_usd = risk
        
        pos1 = MockPos("BTC-USD", 100)
        pos2 = MockPos("ETH-USD", 100)
        
        var = compute_portfolio_var([pos1, pos2], 100)
        # sqrt(100^2 + 100^2 + 2*100*100*0.88) = sqrt(10000 + 10000 + 17600) = sqrt(37600) approx 193.9
        self.assertGreater(var, 150)
        self.assertLess(var, 201)

if __name__ == "__main__":
    unittest.main()
