"""
MAG7SSI Tier 1 Signal — ARIA v1.8

Tracks USTECH100-USD (Nasdaq 100) mark price as a native SoDEX macro
leading indicator. Nasdaq 100 is ~50% Mag7 weighted, making it the best
single-instrument proxy for MAG7 tech sentiment.

Lead-lag relationship:
  Tech selling (USTECH100 falling) → risk-off → crypto pressure (2-4h lag)
  Tech buying  (USTECH100 rising)  → risk-on  → crypto support

Signal output:
  direction: "bullish" | "bearish" | "neutral"
  strength:  0.0 – 1.5 (direction-neutral magnitude)
  tier1_score(direction): +strength if aligned, -0.5×strength if opposing

Integration:
  Called from interpreter._build_and_publish() when USTECH100 candle closes.
  Injects processed["mag7_direction"] and processed["mag7_strength"].
  CoherenceEngine reads mag7_strength as the "mag7_macro" tier.
  Enhancement Layer applies direction bonus/penalty post-coherence.
"""

import time
import structlog
from collections import deque
from typing import Tuple

log = structlog.get_logger(__name__)


class MAG7SSISignal:
    """
    USTECH100-USD mark price as MAG7 tech macro regime indicator.

    Uses 3-candle vs 10-candle short-MA/long-MA crossover.
    Stale after 2 hours without update (market closed or feed gap).
    """

    _MIN_CANDLES = 10   # Need 10 candles for long MA
    _STALE_S = 7200     # 2 hours

    def __init__(self):
        self._prices: deque = deque(maxlen=20)
        self.direction: str = "neutral"
        self.strength: float = 0.0
        self._last_update: float = 0.0
        self._candle_count: int = 0

    def update(self, mark_price: float, timestamp_ms: int) -> None:
        """Feed a new USTECH100 candle close. Called on every 1m candle."""
        if mark_price <= 0:
            return
        self._prices.append(mark_price)
        self._candle_count += 1
        self._last_update = time.time()
        self._compute_direction()

    def _compute_direction(self) -> None:
        """3-MA vs 10-MA crossover → direction + strength."""
        prices = list(self._prices)
        if len(prices) < self._MIN_CANDLES:
            self.direction = "neutral"
            self.strength = 0.0
            return

        short_ma = sum(prices[-3:]) / 3
        long_ma  = sum(prices[-10:]) / 10

        if long_ma == 0:
            return

        diff_pct = (short_ma - long_ma) / long_ma

        # 0.3% threshold — calibrated for USTECH100 (moves ~0.5-1% per hour normally)
        if diff_pct < -0.003:
            self.direction = "bearish"
            self.strength  = min(1.5, abs(diff_pct) * 333)  # 0.3% → 1.0 raw
        elif diff_pct > 0.003:
            self.direction = "bullish"
            self.strength  = min(1.5, diff_pct * 333)
        else:
            self.direction = "neutral"
            self.strength  = 0.0

        log.debug(
            "mag7_computed",
            direction=self.direction,
            strength=round(self.strength, 3),
            diff_pct=round(diff_pct * 100, 4),
            candles=len(prices),
        )

    def get_tier1_score(self, trade_direction: str) -> float:
        """
        Direction-aware Tier 1 score.

        Aligned (MAG7 bearish + short, or MAG7 bullish + long): +strength
        Opposing (MAG7 bearish + long, or MAG7 bullish + short): -0.5×strength
        Neutral: 0.0
        """
        if self.is_stale() or self.direction == "neutral":
            return 0.0

        if self.direction == "bearish":
            return self.strength if trade_direction == "short" else -self.strength * 0.5
        if self.direction == "bullish":
            return self.strength if trade_direction == "long" else -self.strength * 0.5
        return 0.0

    def is_stale(self) -> bool:
        """Signal older than 2 hours is stale (market closed or feed gap)."""
        return (time.time() - self._last_update) > self._stale_s if self._last_update > 0 else True

    @property
    def _stale_s(self):
        return self._STALE_S

    def status(self) -> dict:
        return {
            "direction":  self.direction,
            "strength":   round(self.strength, 3),
            "candles":    self._candle_count,
            "stale":      self.is_stale(),
            "last_update_s": round(time.time() - self._last_update) if self._last_update else None,
        }
