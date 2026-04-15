import json
import os
import time
import structlog
from dataclasses import dataclass, asdict
from typing import Dict, List, Any, Optional

logger = structlog.get_logger(__name__)

@dataclass
class FundingRecord:
    symbol: str
    rate: float  # hourly %
    timestamp_ms: int
    source: str  # "live" or "derived"

class FundingHistory:
    """Tracks funding rates over time per asset and persists to disk.

    v2: Also stores Bybit funding rates separately to enable cross-venue
    spread calculation (Tier 7 coherence signal).
    """

    def __init__(self, storage_path: str = "logs/funding_history.json"):
        self.storage_path = storage_path
        self._history: Dict[str, List[FundingRecord]] = {}
        self._bybit_rates: Dict[str, float] = {}   # symbol → latest Bybit 8h rate
        self.max_records = 168  # 7 days * 24 hours

    def add(self, symbol: str, rate: float, source: str) -> None:
        """Appends a new record and prunes old ones."""
        if symbol not in self._history:
            self._history[symbol] = []
        
        record = FundingRecord(
            symbol=symbol,
            rate=rate,
            timestamp_ms=int(time.time() * 1000),
            source=source
        )
        
        self._history[symbol].append(record)
        
        # Keep last 168 records
        if len(self._history[symbol]) > self.max_records:
            self._history[symbol] = self._history[symbol][-self.max_records:]
        
        self.save()

    def get_rates(self, symbol: str, n: int = 24) -> List[float]:
        """Returns last n rates for symbol."""
        records = self._history.get(symbol, [])
        return [r.rate for r in records[-n:]]

    def avg(self, symbol: str, hours: int = 24) -> float:
        """Returns average rate over last n hours."""
        rates = self.get_rates(symbol, hours)
        if not rates:
            return 0.0
        return sum(rates) / len(rates)

    def avg_7d(self, symbol: str) -> float:
        """Returns 7-day average rate."""
        return self.avg(symbol, 168)

    def is_extreme(self, symbol: str, threshold_pct: float = 0.05) -> bool:
        """Returns True if latest rate exceeds threshold in either direction."""
        rates = self.get_rates(symbol, 1)
        if not rates:
            return False
        return abs(rates[0]) >= threshold_pct

    def carry_score(self, symbol: str) -> float:
        """
        Score from -3.0 to +3.0 based on latest rate.

        Prefers the Bybit 8h funding rate (stored by bybit_feed via add_bybit_rate)
        because SoDEX perp rates are often near-zero and don't reflect market sentiment.
        Falls back to SoDEX rate if Bybit rate is unavailable.

        Thresholds are calibrated for Bybit 8h rates (normal range ±0.0001–0.001).
        """
        # Use Bybit rate when available — it's the market-consensus funding signal.
        bybit_rate = self._bybit_rates.get(symbol)
        if bybit_rate is not None:
            return self._score_rate(bybit_rate)

        rates = self.get_rates(symbol, 1)
        if not rates:
            return 0.0
        return self._score_rate(rates[0])

    def carry_score_from_rate(self, rate: float) -> float:
        """Stateless version of carry_score: score a rate without needing history."""
        return self.carry_score.__func__(self, "_synthetic_") if False else self._score_rate(rate)

    def _score_rate(self, rate: float) -> float:
        """Shared scoring logic used by carry_score() and carry_score_from_rate()."""
        if rate >= 0.001:   return 3.0
        if rate >= 0.0005:  return 2.0
        if rate >= 0.0003:  return 1.0
        if rate >= 0.0001:  return 0.5
        if rate > -0.0001:  return 0.0
        if rate > -0.0003:  return -0.5
        if rate > -0.0005:  return -1.0
        if rate > -0.001:   return -2.0
        return -3.0

    # ── Cross-venue (Bybit) API ─────────────────────────────────────────────────

    def add_bybit_rate(self, symbol: str, rate: float) -> None:
        """Store latest Bybit 8h funding rate for cross-venue spread calculation."""
        self._bybit_rates[symbol] = float(rate)

    def get_latest_bybit_rate(self, symbol: str) -> Optional[float]:
        """Returns latest Bybit 8h funding rate or None if not yet received."""
        return self._bybit_rates.get(symbol)

    def get_cross_venue_spread(self, symbol: str) -> Optional[float]:
        """
        Returns bybit_rate - sodex_rate as decimal (positive = Bybit more bullish).
        Returns None if either rate is unavailable.
        """
        bybit = self._bybit_rates.get(symbol)
        if bybit is None:
            return None
        sodex_rates = self.get_rates(symbol, 1)
        if not sodex_rates:
            return None
        return bybit - sodex_rates[-1]

    def save(self) -> None:
        """Persists history to JSON."""
        try:
            os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
            data = {sym: [asdict(r) for r in recs] for sym, recs in self._history.items()}
            with open(self.storage_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error("funding_history_save_error", error=str(e))

    def load(self) -> None:
        """Loads history from JSON."""
        if not os.path.exists(self.storage_path):
            return
        
        try:
            with open(self.storage_path, 'r') as f:
                data = json.load(f)
                for symbol, records in data.items():
                    self._history[symbol] = [FundingRecord(**r) for r in records]
        except Exception as e:
            logger.error("funding_history_load_error", error=str(e))
