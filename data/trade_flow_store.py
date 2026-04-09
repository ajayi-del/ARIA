import time
from collections import deque
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
        self.last_update_ms: int | None = None

    def add(self, trade: Trade) -> None:
        self.trades.append(trade)
        self.last_update_ms = trade.timestamp_ms

    def buy_volume(self, window_ms: int = 60000) -> float:
        cutoff = int(time.time() * 1000) - window_ms
        return sum(t.size for t in self.trades if t.side == "buy" and t.timestamp_ms >= cutoff)

    def sell_volume(self, window_ms: int = 60000) -> float:
        cutoff = int(time.time() * 1000) - window_ms
        return sum(t.size for t in self.trades if t.side == "sell" and t.timestamp_ms >= cutoff)

    def delta(self, window_ms: int = 60000) -> float:
        return self.buy_volume(window_ms) - self.sell_volume(window_ms)

    def aggressor_ratio(self, window_ms: int = 60000) -> float:
        bv = self.buy_volume(window_ms)
        sv = self.sell_volume(window_ms)
        if bv + sv == 0:
            return 0.5
        return bv / (bv + sv)

    def latest_price(self) -> float | None:
        if not self.trades:
            return None
        return self.trades[-1].price

    def count(self, window_ms: int = 60000) -> int:
        cutoff = int(time.time() * 1000) - window_ms
        return sum(1 for t in self.trades if t.timestamp_ms >= cutoff)
