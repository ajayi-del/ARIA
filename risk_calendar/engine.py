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

try:
    from intelligence.market_hours import MarketHoursGate as _MHGate
    _mhgate = _MHGate()
except ImportError:
    _mhgate = None

# Assets that receive WEEKEND_CLOSE / WEEKEND_REOPEN calendar events.
# Individual equity stocks (NVDA, AAPL, etc.) are excluded — MarketHoursGate
# handles their hard session gate already and adding WEEKEND_CLOSE causes a
# spurious BLOCK within 2h of Friday 21:00 UTC while the market is still open.
# Only index products and commodities where the calendar event carries unique
# information (different close time, different multiplier curve) belong here.
_WEEKEND_AFFECTED = frozenset({
    "XAUT-USD", "SILVER-USD",
    "USTECH100-USD", "US500-USD",
})


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

    async def init(self):
        """Initializes the database and seeds events asynchronously."""
        await self.event_store.init_db()
        await self.event_store.seed_events()

    async def get_state(
        self,
        symbol: str,
        now_utc: datetime = None,
        upcoming: Optional[CalendarEvent] = None,
        last_past: Optional[CalendarEvent] = None
    ) -> CalendarState:
        """
        Returns the current calendar risk state for a symbol.
        Allows passing pre-fetched events for performance.
        Now asynchronous due to aiosqlite integration.
        """
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)

        # Step 0 — Hard gate: if the market is currently closed for this asset,
        # return BLOCK immediately. This is the authoritative source for session
        # closures (equity weekends, gold Fri 22:00–Sun 23:00 UTC, etc.).
        # The calendar event layer handles pre-event risk windows; this layer
        # handles actual market closures so the display shows BLOCK on weekends
        # without the calendar event approach firing 2h early during trading hours.
        if _mhgate is not None:
            _tradeable, _session_reason = _mhgate.should_trade_symbol(symbol, now_utc)
            if not _tradeable:
                return CalendarState(
                    symbol=symbol,
                    regime="BLOCK",
                    hours_to_event=None,
                    hours_since_event=None,
                    nearest_event_type="MARKET_CLOSED",
                    nearest_event_name=_session_reason.replace("_", " ").title(),
                    nearest_event_time=None,
                    size_multiplier=0.0,
                    stop_atr_multiplier=1.0,
                    reason=_session_reason,
                )

        # Step 1 — Get nearest upcoming event if not provided.
        # For crypto-only assets (not XAUT/USTECH100/stocks), WEEKEND events are
        # irrelevant — crypto trades 24/7. Filter them out so they don't produce
        # confusing "weekend close in 3.7d" noise for BTC, ETH, etc.
        if upcoming is None:
            if symbol not in _WEEKEND_AFFECTED:
                # Exclude weekend structural events for 24/7 crypto assets
                upcoming = await self.event_store.get_nearest(
                    now_utc=now_utc,
                    exclude_types=["WEEKEND_CLOSE", "WEEKEND_REOPEN"],
                )
            else:
                upcoming = await self.event_store.get_nearest(now_utc=now_utc)
            
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
            last_past = await self.event_store.get_last_past(hours_back=2, now_utc=now_utc)
            
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

        # Build human-readable reason — avoid "WEEKEND_CLOSE" noise when event is days away.
        # Operators see this in the terminal; far-future weekend events are not actionable.
        _event_label = upcoming.event_type.replace("_", " ").lower()
        if hours_to_event > 48:
            _reason = f"clear — next: {_event_label} in {hours_to_event/24:.1f}d"
        elif hours_to_event > 24:
            _reason = f"watch:{upcoming.event_type}_in_{hours_to_event:.0f}h"
        else:
            _reason = f"{regime}:{upcoming.event_type}_in_{hours_to_event:.1f}h"

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
            reason=_reason,
        )

    async def get_states_all(
        self,
        symbols: List[str]
    ) -> Dict[str, CalendarState]:
        """
        Returns calendar states for all symbols in two DB round-trips.

        Pre-fetches two event variants to avoid N×DB round-trips per symbol:
          - upcoming_all      → for weekend-affected assets (XAUT, equities)
          - upcoming_no_wknd  → for 24/7 crypto (strips WEEKEND noise)

        Without this fix, all symbols receive the same upcoming event from a
        single get_nearest() call — which may be WEEKEND_CLOSE, causing crypto
        assets to show "weekend close in 3.5d" even on a Monday.
        """
        now_utc = datetime.now(timezone.utc)
        # Two variants — one per asset class. Only two DB calls regardless of
        # how many symbols are in the universe.
        upcoming_all = await self.event_store.get_nearest(now_utc=now_utc)
        upcoming_no_wknd = await self.event_store.get_nearest(
            now_utc=now_utc,
            exclude_types=["WEEKEND_CLOSE", "WEEKEND_REOPEN"],
        )
        last_past = await self.event_store.get_last_past(hours_back=2, now_utc=now_utc)

        result: Dict[str, CalendarState] = {}
        for s in symbols:
            # Weekend-affected assets see WEEKEND events; 24/7 crypto does not.
            up = upcoming_all if s in _WEEKEND_AFFECTED else upcoming_no_wknd
            result[s] = await self.get_state(s, now_utc, up, last_past)
        return result
