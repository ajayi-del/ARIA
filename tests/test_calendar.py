import unittest
from datetime import datetime, timezone, timedelta
from risk_calendar.multipliers import (
    time_decay_multiplier,
    post_event_multiplier,
    asset_calendar_multiplier,
    stop_atr_multiplier
)
from risk_calendar.engine import CalendarEngine
from risk_calendar.events import CalendarEvent

class TestCalendarMath(unittest.TestCase):
    def test_time_decay_steps(self):
        # BLOCK (<2h)
        self.assertEqual(time_decay_multiplier(1.0), 0.0)
        self.assertEqual(time_decay_multiplier(1.9), 0.0)
        
        # Severe Caution (2-6h)
        self.assertEqual(time_decay_multiplier(3.0), 0.25)
        
        # Caution (6-12h)
        self.assertEqual(time_decay_multiplier(8.0), 0.50)
        
        # Mild Caution (12-24h)
        self.assertEqual(time_decay_multiplier(18.0), 0.75)
        
        # Clear (>24h)
        self.assertEqual(time_decay_multiplier(30.0), 1.0)

    def test_post_event_recovery(self):
        self.assertEqual(post_event_multiplier(0.25), 0.25)
        self.assertEqual(post_event_multiplier(0.75), 0.50)
        self.assertEqual(post_event_multiplier(1.5), 0.75)
        self.assertEqual(post_event_multiplier(3.0), 1.0)

    def test_asset_multiplier_scaling(self):
        # FOMC XAUT (impact 1.0) at 50% decay
        # reduction = (1.0 - 0.5) * 1.0 = 0.5. 1.0 - 0.5 = 0.5
        self.assertAlmostEqual(asset_calendar_multiplier(0.5, "FOMC", "XAUT-USD"), 0.5)
        
        # FOMC LINK (impact 0.6) at 50% decay
        # reduction = (1.0 - 0.5) * 0.6 = 0.3. 1.0 - 0.3 = 0.7
        self.assertAlmostEqual(asset_calendar_multiplier(0.5, "FOMC", "LINK-USD"), 0.7)
        
        # MAG7 NVDA Earnings (impact 1.0) for USTECH100-USD
        self.assertAlmostEqual(asset_calendar_multiplier(0.5, "EARNINGS_MAG7", "USTECH100-USD"), 0.5)

    def test_stop_widening(self):
        # FOMC XAUT (impact 1.0) at 8h
        # 1.0 + (1.0 * 1.0) = 2.0
        self.assertEqual(stop_atr_multiplier(8.0, "FOMC", "XAUT-USD"), 2.0)
        
        # FOMC LINK (impact 0.6) at 18h
        # 1.0 + (0.5 * 0.6) = 1.3
        self.assertAlmostEqual(stop_atr_multiplier(18.0, "FOMC", "LINK-USD"), 1.3)

class TestCalendarEngine(unittest.TestCase):
    def setUp(self):
        # Use in-memory DB for tests
        self.engine = CalendarEngine(":memory:")
        # Clear seeded events to have predictable test state
        with self.engine.event_store.conn:
            self.engine.event_store.conn.execute("DELETE FROM events")

    def test_engine_state_clear(self):
        # Manual add clear event far in future
        far_future = datetime.now(timezone.utc) + timedelta(days=10)
        self.engine.event_store.add_event(CalendarEvent(
            "FOMC", "Test Event", far_future, "HIGH", "desc", "test"
        ))
        
        state = self.engine.get_state("BTC-USD")
        self.assertEqual(state.regime, "CLEAR")
        self.assertEqual(state.size_multiplier, 1.0)

    def test_engine_state_block(self):
        near_future = datetime.now(timezone.utc) + timedelta(hours=1)
        self.engine.event_store.add_event(CalendarEvent(
            "FOMC", "Test Near", near_future, "HIGH", "desc", "test"
        ))
        
        state = self.engine.get_state("BTC-USD")
        self.assertEqual(state.regime, "BLOCK")
        self.assertEqual(state.size_multiplier, 0.0)

    def test_engine_ustech_scaling(self):
        # EARNINGS_MAG7 impact is 1.0 for USTECH but 0.4 for BTC
        near_future = datetime.now(timezone.utc) + timedelta(hours=8)
        self.engine.event_store.add_event(CalendarEvent(
            "EARNINGS_MAG7", "NVDA", near_future, "HIGH", "desc", "test"
        ))
        
        # Base TD for 8h is 0.5
        # USTECH: reduction = (1.0-0.5)*1.0 = 0.5 -> 0.5
        state_tech = self.engine.get_state("USTECH100-USD")
        self.assertAlmostEqual(state_tech.size_multiplier, 0.5)
        
        # BTC: reduction = (1.0-0.5)*0.4 = 0.2 -> 0.8
        state_btc = self.engine.get_state("BTC-USD")
        self.assertAlmostEqual(state_btc.size_multiplier, 0.8)

if __name__ == "__main__":
    unittest.main()
