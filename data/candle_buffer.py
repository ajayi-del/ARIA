from collections import deque
from dataclasses import dataclass

@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int

class CandleBuffer:
    def __init__(self, symbol: str, interval: str, maxlen: int = 200):
        self.symbol = symbol
        self.interval = interval
        self.maxlen = maxlen
        self.candles = deque(maxlen=maxlen)

    def add(self, candle: Candle) -> None:
        self.candles.append(candle)

    def latest(self, n: int = 1) -> list[Candle]:
        if len(self.candles) < n:
            raise ValueError(f"Buffer has less than {n} candles.")
        # Return last n candles directly
        return list(self.candles)[-n:]

    def closes(self, n: int) -> list[float]:
        return [c.close for c in self.latest(n)]

    def highs(self, n: int) -> list[float]:
        return [c.high for c in self.latest(n)]

    def lows(self, n: int) -> list[float]:
        return [c.low for c in self.latest(n)]

    def volumes(self, n: int) -> list[float]:
        return [c.volume for c in self.latest(n)]

    def is_ready(self, min_candles: int = 20) -> bool:
        return len(self.candles) >= min_candles

    def count(self) -> int:
        return len(self.candles)
