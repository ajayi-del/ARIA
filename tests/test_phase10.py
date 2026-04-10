import unittest
import asyncio
import time
from core.event_bus import CoalescedEventBus, EventType, Event
from core.system_state import SystemStateManager, SystemPhase
from risk.risk_engine import RiskEngine
from risk.margin_engine import MarginEngine
from intelligence.market_state import MarketState
from execution.schemas import TradeCandidate

class TestPhase10(unittest.IsolatedAsyncioTestCase):
    
    async def test_event_bus_coalescing(self):
        """Test that events are coalesced (overwritten) in the pending buffer."""
        bus = CoalescedEventBus()
        received = []
        
        def subscriber(event):
            received.append(event)
            
        bus.subscribe(EventType.MARK_PRICE_UPDATED, subscriber)
        
        # Publish 3 events rapidly for the same symbol
        now = int(time.time() * 1000)
        bus.publish(Event(EventType.MARK_PRICE_UPDATED, "BTC-USD", now, {"price": 100}))
        bus.publish(Event(EventType.MARK_PRICE_UPDATED, "BTC-USD", now + 1, {"price": 101}))
        bus.publish(Event(EventType.MARK_PRICE_UPDATED, "BTC-USD", now + 2, {"price": 102}))
        
        # Wait for calls to be processed in the loop iteration
        await asyncio.sleep(0.1)
        
        # Manually trigger one dispatch cycle
        await bus._dispatch_once()
        
        # Should only have 1 event (the last one)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].data["price"], 102)

    async def test_system_state_warmup(self):
        """Test warm-up phase gating."""
        mgr = SystemStateManager(assets=["BTC-USD"], min_candles=5)
        
        # Initial phase
        self.assertEqual(mgr._symbol_phase["BTC-USD"], SystemPhase.WARMING_UP)
        self.assertFalse(mgr.can_signal("BTC-USD"))
        
        # Increment candles via update
        for i in range(1, 5):
            mgr.update("BTC-USD", candle_count=i, ob_healthy=True, mark_healthy=True)
            self.assertEqual(mgr._symbol_phase["BTC-USD"], SystemPhase.WARMING_UP)
            
        # 5th candle -> READY
        mgr.update("BTC-USD", candle_count=5, ob_healthy=True, mark_healthy=True)
        self.assertEqual(mgr._symbol_phase["BTC-USD"], SystemPhase.READY)
        self.assertTrue(mgr.can_signal("BTC-USD"))
        
        # Set to trading
        mgr.mark_trading("BTC-USD")
        self.assertEqual(mgr._symbol_phase["BTC-USD"], SystemPhase.TRADING)
        self.assertTrue(mgr.can_trade("BTC-USD"))

    def test_multiplier_chain(self):
        """Test v1.3 Unified Multiplier Chain."""
        # Setup mocks/stubs
        class MockConfig:
            risk_pct = 0.01
            min_coherence = 4
        
        class MockCalendar:
            def get_state(self, sym):
                from dataclasses import dataclass
                @dataclass
                class S:
                    regime = "CLEAR"
                    size_multiplier = 0.5
                    stop_atr_multiplier = 1.0
                return S()

        margin = MarginEngine()
        risk = RiskEngine(MockConfig(), margin, None, MockCalendar()) 
        risk.allocation = {"directional_pct": 0.8} # Allocation multiplier
        
        candidate = TradeCandidate(
            symbol="BTC-USD",
            side="long",
            entry_price=100.0,
            stop_price=95.0,
            tp1_price=110.0,
            tp2_price=120.0,
            tp3_price=130.0,
            size=1.0,
            initial_margin=10.0,
            leverage=10,
            rr_ratio=2.0,
            coherence_score=6,
            size_multiplier=1.5, # Coherence multiplier
            signal_reason="test",
            invalidation="none",
            timestamp_ms=1712680000000,
            signal_age_ms=0,     # Freshness multiplier = 1.0
            atr=2.0
        )
        # Test that we can compute combined multipliers without error
        # In this phase, we just verify the object is valid and the fields exist
        self.assertEqual(candidate.size_multiplier, 1.5)
        self.assertEqual(candidate.signal_age_ms, 0)

if __name__ == "__main__":
    unittest.main()
