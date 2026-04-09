import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Dict, List, Optional, Any

from .events import EventStore, CalendarEvent
from .multipliers import (
    time_decay_multiplier,
    post_event_multiplier,
    asset_calendar_multiplier,
    stop_atr_multiplier
)

@dataclass
class CalendarState:
    symbol:              str
    regime:              str  # "CLEAR", "CAUTION", "BLOCK"
    hours_to_event:      Optional[float]
    hours_since_event:   Optional[float]
    nearest_event_type:  Optional[str]
    nearest_event_name:  Optional[str]
    nearest_event_time:  Optional[datetime]
    size_multiplier:     float  # 0.0-1.0
    stop_atr_multiplier: float  # 1.0-2.0
    reason:              str

class CalendarEngine:
    """Orchestrates event store and multiplier calculations."""
    
    def __init__(self, db_path: str = "logs/calendar.db"):
        self.event_store = EventStore(db_path)
        self.event_store.init_db()
        self.event_store.seed_events()

    def get_state(
        self,
        symbol: str,
        now_utc: datetime = None,
        upcoming: Optional[CalendarEvent] = None,
        last_past: Optional[CalendarEvent] = None
    ) -> CalendarState:
        """
        Returns the current calendar risk state for a symbol.
        Allows passing pre-fetched events for performance.
        """
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
            
        # Step 1 — Get nearest upcoming event if not provided
        if upcoming is None:
            upcoming = self.event_store.get_nearest(now_utc=now_utc)
            
        if upcoming is None:
            return CalendarState(
                symbol=symbol,
                regime="CLEAR",
                hours_to_event=None,
                hours_since_event=None,
                nearest_event_type=None,
                nearest_event_name=None,
                nearest_event_time=None,
                size_multiplier=1.0,
                stop_atr_multiplier=1.0,
                reason="no_events_scheduled"
            )

        # Step 2 — Compute time delta to next event
        delta = upcoming.event_time - now_utc
        hours_to_event = delta.total_seconds() / 3600.0

        # Step 3 — Check if we are post-event (last event fired recently)
        if last_past is None:
            # hours_back=2 is the max settlement period we track
            last_past = self.event_store.get_last_past(hours_back=2, now_utc=now_utc)
            
        if last_past is not None:
            past_delta = now_utc - last_past.event_time
            hours_since = past_delta.total_seconds() / 3600.0
            
            if hours_since < 2.0:
                post_mult = post_event_multiplier(hours_since)
                asset_mult = asset_calendar_multiplier(
                    post_mult,
                    last_past.event_type,
                    symbol
                )
                # Note: Stop multiplier doesn't widen after the event (volatility usually settles)
                # It only widens pre-event to avoid getting stopped on the spike.
                stop_mult = 1.0 
                
                regime = "CAUTION" if asset_mult > 0 else "BLOCK"
                if asset_mult >= 1.0:
                    regime = "CLEAR"
                
                return CalendarState(
                    symbol=symbol,
                    regime=regime,
                    hours_to_event=hours_to_event,
                    hours_since_event=hours_since,
                    nearest_event_type=upcoming.event_type,
                    nearest_event_name=upcoming.name,
                    nearest_event_time=upcoming.event_time,
                    size_multiplier=asset_mult,
                    stop_atr_multiplier=stop_mult,
                    reason=f"post_event_recovery:{last_past.event_type}"
                )

        # Step 4 — Pre-event logic
        base_mult = time_decay_multiplier(hours_to_event)
        
        if base_mult == 0.0:
            return CalendarState(
                symbol=symbol,
                regime="BLOCK",
                hours_to_event=hours_to_event,
                hours_since_event=None,
                nearest_event_type=upcoming.event_type,
                nearest_event_name=upcoming.name,
                nearest_event_time=upcoming.event_time,
                size_multiplier=0.0,
                stop_atr_multiplier=1.0,
                reason=f"BLOCK:{upcoming.event_type}_in_{hours_to_event:.1f}h"
            )
        
        asset_mult = asset_calendar_multiplier(
            base_mult,
            upcoming.event_type,
            symbol
        )
        stop_mult = stop_atr_multiplier(
            hours_to_event,
            upcoming.event_type,
            symbol
        )
        
        regime = "CAUTION" if asset_mult < 1.0 else "CLEAR"
        
        return CalendarState(
            symbol=symbol,
            regime=regime,
            hours_to_event=hours_to_event,
            hours_since_event=None,
            nearest_event_type=upcoming.event_type,
            nearest_event_name=upcoming.name,
            nearest_event_time=upcoming.event_time,
            size_multiplier=asset_mult,
            stop_atr_multiplier=stop_mult,
            reason=f"{regime}:{upcoming.event_type}_in_{hours_to_event:.1f}h"
        )

    def get_states_all(
        self,
        symbols: List[str]
    ) -> Dict[str, CalendarState]:
        """Returns calendar states for all requested symbols with efficient batch pre-fetch."""
        now_utc = datetime.now(timezone.utc)
        upcoming = self.event_store.get_nearest(now_utc=now_utc)
        last_past = self.event_store.get_last_past(hours_back=2, now_utc=now_utc)
        
        return {
            s: self.get_state(s, now_utc, upcoming, last_past)
            for s in symbols
        }
