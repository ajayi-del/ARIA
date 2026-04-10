import time
import threading
from collections import deque
from core.event_bus import event_bus, Event, EventType
from dataclasses import dataclass
from typing import Literal

@dataclass
class Trade:
    timestamp_ms: int
    price: float
    size: float
    side: Literal["buy", "sell"]
    is_aggressor_buy: bool

class TradeFlowStore:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.trades = deque(maxlen=500)
        self.last_update_ms_val: int | None = None
        self._lock = threading.Lock()

    def add(self, trade: Trade) -> None:
        with self._lock:
            self.trades.append(trade)
            self.last_update_ms_val = trade.timestamp_ms
        
        # Publish update event
        event_bus.publish(Event(
            EventType.TRADE_FLOW_UPDATED,
            self.symbol,
            trade.timestamp_ms,
            {}
        ))

    def get_recent(self, n: int) -> list:
        """Thread-safe access to recent trades (raw objects)."""
        with self._lock:
            return list(self.trades)[-n:]

    def get_all(self) -> list:
        """Thread-safe access to all trades (raw objects)."""
        with self._lock:
            return list(self.trades)

    def last_update_ms(self) -> int:
        """Thread-safe access to latest timestamp."""
        with self._lock:
            if not self.trades:
                return 0
            return self.trades[-1].timestamp_ms

    def buy_volume(self, window_ms: int = 60000) -> float:
        """Deterministic buy volume calculation."""
        with self._lock:
            if not self.trades:
                return 0.0
            latest_ms = self.trades[-1].timestamp_ms
            cutoff = latest_ms - window_ms
            return sum(t.size for t in self.trades if t.side == "buy" and t.timestamp_ms >= cutoff)

    def sell_volume(self, window_ms: int = 60000) -> float:
        """Deterministic sell volume calculation."""
        with self._lock:
            if not self.trades:
                return 0.0
            latest_ms = self.trades[-1].timestamp_ms
            cutoff = latest_ms - window_ms
            return sum(t.size for t in self.trades if t.side == "sell" and t.timestamp_ms >= cutoff)

    def delta(self, window_ms: int = 60000) -> float:
        """Deterministic volume delta."""
        return self.buy_volume(window_ms) - self.sell_volume(window_ms)

    def aggressor_ratio(self, window_ms: int = 60000) -> float:
        """Deterministic aggressor ratio."""
        bv = self.buy_volume(window_ms)
        sv = self.sell_volume(window_ms)
        if bv + sv == 0:
            return 0.5
        return bv / (bv + sv)

    def latest_price(self) -> float | None:
        """Thread-safe access to latest price."""
        with self._lock:
            if not self.trades:
                return None
            return self.trades[-1].price

    def count(self, window_ms: int = 60000) -> int:
        """Deterministic trade count."""
        with self._lock:
            if not self.trades:
                return 0
            latest_ms = self.trades[-1].timestamp_ms
            cutoff = latest_ms - window_ms
            return sum(1 for t in self.trades if t.timestamp_ms >= cutoff)
