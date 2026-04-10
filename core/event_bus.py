import asyncio
import structlog
import time
from enum import Enum, auto
from dataclasses import dataclass
from collections import defaultdict
from typing import Dict, List, Callable, Any, Optional, Tuple

logger = structlog.get_logger(__name__)

class EventType(Enum):
    ORDERBOOK_UPDATED = auto()
    MARK_PRICE_UPDATED = auto()
    CANDLE_CLOSED = auto()
    TRADE_FLOW_UPDATED = auto()
    SIGNAL_READY = auto()

@dataclass
class Event:
    event_type: EventType
    symbol: str
    timestamp_ms: int
    data: Dict[str, Any]

class CoalescedEventBus:
    """
    ARC 1.3 Architecture: Coalesced Event Bus.
    Ensures one pending slot per (event_type, symbol) pair.
    New events overwrite pending. Dispatch runs every 50ms.
    Eliminates burst accumulation risk.
    """
    def __init__(self):
        self._pending: Dict[Tuple[EventType, str], Event] = {}
        self._subscribers: Dict[EventType, List[Callable]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._pending_lock = asyncio.Lock() # For coalescing
        self._running = False
        
        logger.info("coalesced_event_bus_initialized", latency_ms=50)

    def subscribe(self, event_type: EventType, callback: Callable) -> None:
        """Register async or sync callback for event type."""
        self._subscribers[event_type].append(callback)
        logger.info("event_subscribed", event_type=event_type.name, callback=callback.__name__)

    def publish(self, event: Event) -> None:
        """
        Puts event on the pending dictionary (overwrites previous).
        Uses call_soon_threadsafe to ensure safety if called from background thread.
        """
        def _apply():
            key = (event.event_type, event.symbol)
            self._pending[key] = event

        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(_apply)
        except RuntimeError:
            # Fallback for synchronous contexts or tests without a running loop
            _apply()

    async def start(self) -> None:
        """Starts the dispatch loop as a background task."""
        if self._running:
            return
            
        self._running = True
        self._task = asyncio.create_task(self._dispatch_internal())
        logger.info("event_bus_started")

    async def _dispatch_internal(self) -> None:
        """Internal loop running forever at a 50ms cadence."""
        logger.info("event_bus_dispatch_loop_started")
        while self._running:
            try:
                # 50ms dispatch cadence
                await asyncio.sleep(0.05)
                
                # Snapshot and clear pending events atomically
                async with self._pending_lock:
                    if not self._pending:
                        continue
                    events = dict(self._pending)
                    self._pending.clear()
                
                # Dispatch batch
                for event in events.values():
                    subscribers = self._subscribers.get(event.event_type, [])
                    for callback in subscribers:
                        try:
                            if asyncio.iscoroutinefunction(callback):
                                await callback(event)
                            else:
                                callback(event)
                        except Exception as e:
                            logger.error("event_handler_error", 
                                         event_type=event.event_type.name, 
                                         symbol=event.symbol, 
                                         error=str(e))
                                         
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("event_bus_critical_error", error=str(e))
                await asyncio.sleep(0.1)

    async def _dispatch_once(self) -> None:
        """For unit testing: dispatches all currently pending events once."""
        async with self._pending_lock:
            events = dict(self._pending)
            self._pending.clear()
        
        for event in events.values():
            subscribers = self._subscribers.get(event.event_type, [])
            for callback in subscribers:
                if asyncio.iscoroutinefunction(callback):
                    await callback(event)
                else:
                    callback(event)

    async def stop(self):
        self._running = False
        if hasattr(self, '_task') and self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

# Singleton instance
event_bus = CoalescedEventBus()
