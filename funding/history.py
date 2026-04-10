import json
import os
import time
from dataclasses import dataclass, asdict
from typing import Dict, List, Any

@dataclass
class FundingRecord:
    symbol: str
    rate: float  # hourly %
    timestamp_ms: int
    source: str  # "live" or "derived"

class FundingHistory:
    """Tracks funding rates over time per asset and persists to disk."""
    
    def __init__(self, storage_path: str = "logs/funding_history.json"):
        self.storage_path = storage_path
        self._history: Dict[str, List[FundingRecord]] = {}
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
        Score from -3.0 to +3.0 based on latest rate and trends.
        Logic:
          rate > 0.05: +3.0
          rate > 0.03: +2.0
          rate > 0.01: +1.0
          -0.01 < rate < 0.01: 0.0
          rate < -0.01: -1.0
          rate < -0.03: -2.0
          rate < -0.05: -3.0
        """
        rates = self.get_rates(symbol, 1)
        if not rates:
            return 0.0
        
        rate = rates[0]
        
        if rate >= 0.0002: return 3.0  # 0.02%
        if rate >= 0.0001: return 2.0  # 0.01%
        if rate >= 0.00005: return 1.5 # 0.005%
        if rate >= 0.00002: return 1.0 # 0.002%
        if rate > -0.00002: return 0.0
        if rate > -0.00005: return -1.0
        if rate > -0.0001: return -1.5
        if rate > -0.0002: return -2.0
        return -3.0

    def save(self) -> None:
        """Persists history to JSON."""
        try:
            os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
            data = {sym: [asdict(r) for r in recs] for sym, recs in self._history.items()}
            with open(self.storage_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Error saving funding history: {e}")

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
            print(f"Error loading funding history: {e}")
