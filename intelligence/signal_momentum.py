"""
Signal Momentum Tracker — ARIA v1.7

Tracks consecutive confirmed signals in the same direction per symbol.
Consecutive agreement = sustained pressure, not noise.
"""
from collections import defaultdict, deque
from dataclasses import dataclass
import time
import structlog

log = structlog.get_logger(__name__)


@dataclass
class _SignalRecord:
    direction: str
    coherence: float
    timestamp_ms: int
    confirmed: bool


class SignalMomentumTracker:
    """
    Bonus table (consecutive confirmed signals same direction):
      1  → +0.0
      2  → +0.2
      3  → +0.4
      4+ → +0.6 (cap)
    Signals expire after EXPIRY_MS (5 min).
    """

    EXPIRY_MS = 300_000

    BONUS_TABLE = {0: 0.0, 1: 0.0, 2: 0.2, 3: 0.4}
    MAX_BONUS = 0.6

    def __init__(self):
        self._history: dict = defaultdict(lambda: deque(maxlen=10))

    def record(self, symbol: str, direction: str, coherence: float, confirmed: bool) -> None:
        self._history[symbol].append(
            _SignalRecord(direction=direction, coherence=coherence,
                          timestamp_ms=int(time.time() * 1000), confirmed=confirmed)
        )

    def get_momentum_bonus(self, symbol: str, direction: str) -> float:
        now_ms = int(time.time() * 1000)
        cutoff = now_ms - self.EXPIRY_MS
        recent = [s for s in self._history[symbol] if s.timestamp_ms > cutoff and s.confirmed]
        if not recent:
            return 0.0
        consecutive = 0
        for s in reversed(recent):
            if s.direction == direction:
                consecutive += 1
            else:
                break
        bonus = self.BONUS_TABLE.get(min(consecutive, 3), self.MAX_BONUS)
        if bonus > 0:
            log.debug("momentum_bonus", symbol=symbol, direction=direction,
                      consecutive=consecutive, bonus=bonus)
        return bonus

    def get_streak(self, symbol: str) -> tuple:
        """Returns (streak_length, direction)."""
        records = list(self._history.get(symbol, []))
        if not records:
            return 0, "none"
        last_dir = records[-1].direction
        streak = sum(1 for s in reversed(records) if s.direction == last_dir)
        # break on first mismatch
        count = 0
        for s in reversed(records):
            if s.direction == last_dir:
                count += 1
            else:
                break
        return count, last_dir
