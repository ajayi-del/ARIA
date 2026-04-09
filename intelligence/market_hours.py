from datetime import datetime, time, timedelta
import pytz

class MarketHoursGate:
    """
    TradFi gold markets have trading hours.
    ARIA should not trade XAUT during TradFi market closures to avoid stale data.
    """
    
    def __init__(self):
        # Gold Market Trading Hours (UTC)
        # Opens Sunday 23:00 UTC
        # Monday-Thursday: Open 24h except 22:00-23:00 UTC (maintenance)
        # Closes Friday 22:00 UTC
        pass

    def is_gold_market_open(self, dt: datetime = None) -> bool:
        """
        Returns True if current UTC time is within gold trading hours.
        """
        if dt is None:
            dt = datetime.now(pytz.UTC)
        elif dt.tzinfo is None:
            dt = pytz.UTC.localize(dt)
            
        weekday = dt.weekday() # Monday=0, Sunday=6
        hour = dt.hour
        
        # Saturday: Always closed
        if weekday == 5:
            return False
            
        # Sunday: Opens at 23:00 UTC
        if weekday == 6:
            return hour >= 23
            
        # Friday: Closes at 22:00 UTC
        if weekday == 4:
            return hour < 22
            
        # Mon-Thu: Daily maintenance 22:00-23:00 UTC
        if hour == 22:
            return False
            
        return True

    def get_ustech_session(self, dt: datetime = None) -> str:
        """
        Returns USTECH session: "regular", "pre_market", "after_hours", "closed"
        Regular: 14:30-21:00 UTC
        Pre-market: 08:00-14:30 UTC
        After-hours: 21:00-00:00 UTC
        Closed: 00:00-08:00 UTC, + Sat, + Sun
        """
        if dt is None:
            dt = datetime.now(pytz.UTC)
        elif dt.tzinfo is None:
            dt = pytz.UTC.localize(dt)

        weekday = dt.weekday()
        if weekday >= 5: # Saturday or Sunday
            return "closed"

        hour = dt.hour
        minute = dt.minute
        time_decimal = hour + minute / 60.0

        if 14.5 <= time_decimal < 21.0:
            return "regular"
        if 8.0 <= time_decimal < 14.5:
            return "pre_market"
        if 21.0 <= time_decimal < 24.0:
            return "after_hours"
        
        return "closed"

    def get_next_open(self, dt: datetime = None) -> datetime:
        """
        Returns UTC datetime of next gold market open.
        """
        if dt is None:
            dt = datetime.now(pytz.UTC)
            
        # Simplistic next open logic (Sunday 23:00 or daily 23:00)
        if self.is_gold_market_open(dt):
            return dt # It's open
            
        # If Saturday or Friday night
        if dt.weekday() == 5 or (dt.weekday() == 4 and dt.hour >= 22):
            days_to_sunday = (6 - dt.weekday()) % 7
            next_sunday = dt.replace(hour=23, minute=0, second=0, microsecond=0) + timedelta(days=days_to_sunday)
            return next_sunday
            
        # If Sunday before 23:00
        if dt.weekday() == 6 and dt.hour < 23:
            return dt.replace(hour=23, minute=0, second=0, microsecond=0)
            
        # If daily maintenance (Mon-Thu)
        if dt.hour >= 22:
             return dt.replace(hour=23, minute=0, second=0, microsecond=0)
             
        return dt # Fallback

    def should_trade_symbol(self, symbol: str, dt: datetime = None) -> tuple[bool, str]:
        """
        Returns (ok, reason). Enforces GATE 0 for XAUT-USD and USTECH100-USD.
        """
        if symbol == "XAUT-USD":
            if not self.is_gold_market_open(dt):
                next_open = self.get_next_open(dt)
                return False, f"GOLD_MARKET_CLOSED: opens {next_open.strftime('%Y-%m-%d %H:%M')} UTC"
            return True, "market_open"
            
        if symbol == "USTECH100-USD":
            session = self.get_ustech_session(dt)
            if session == "closed":
                return False, "USTECH_MARKET_CLOSED"
            if session == "pre_market":
                return True, "pre_market_caution"
            if session == "after_hours":
                return True, "after_hours_caution"
            return True, "regular_session"
            
        return True, "24h_market"
