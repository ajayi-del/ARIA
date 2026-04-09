import asyncio
import time
from dataclasses import dataclass
from typing import Dict, Any, Optional
import structlog
from core.config import Settings
from data.trade_flow_store import TradeFlowStore
from funding.history import FundingHistory

logger = structlog.get_logger(__name__)

@dataclass
class FundingSnapshot:
    symbol: str
    rate: float  # hourly %
    rate_24h_avg: float
    rate_7d_avg: float
    carry_score: float  # -3 to +3
    arb_signal: bool
    direction: str  # "long_arb", "short_arb", "none"
    timestamp_ms: int

class FundingRadar:
    """Live funding rate tracker for all assets."""
    
    def __init__(
        self, 
        config: Settings, 
        trade_flow_stores: Dict[str, TradeFlowStore],
        history: FundingHistory
    ):
        self.config = config
        self.trade_flow_stores = trade_flow_stores
        self.history = history
        self.last_update_ms = 0
        self._snapshots: Dict[str, FundingSnapshot] = {}

    async def update_all(self) -> Dict[str, FundingSnapshot]:
        """Updates funding rates for all assets."""
        now_ms = int(time.time() * 1000)
        
        for symbol in self.config.assets:
            try:
                if self.config.mode == "paper":
                    rate = self.derive_from_flow(symbol)
                    source = "derived"
                else:
                    rate = await self.fetch_from_sodex(symbol)
                    source = "live"
                
                self.history.add(symbol, rate, source)
                self._snapshots[symbol] = self.build_snapshot(symbol)
                
            except Exception as e:
                logger.error("funding_radar_update_error", symbol=symbol, error=str(e))
        
        self.last_update_ms = now_ms
        return self._snapshots

    def derive_from_flow(self, symbol: str) -> float:
        """Derives synthetic funding rate from trade flow aggressor ratio."""
        if symbol not in self.trade_flow_stores:
            return 0.0
        
        # Look back 60s
        aggressor = self.trade_flow_stores[symbol].aggressor_ratio(60000)
        
        # Map 0.0-1.0 -> -0.10 to +0.10
        # 0.5 (neutral) -> 0.0
        # 1.0 (all buys) -> +0.10
        # 0.0 (all sells) -> -0.10
        synthetic_rate = (aggressor - 0.5) * 0.2
        return synthetic_rate

    async def fetch_from_sodex(self, symbol: str) -> float:
        """Fetches hourly funding rate from SoDEX (placeholder for actual API)."""
        # Placeholder logic: return a small random-looking rate or fallback to flow
        # In a real implementation: GET /fundingRate?symbol={symbol}
        await asyncio.sleep(0.1)  # Simulate network latency
        return self.derive_from_flow(symbol)

    def build_snapshot(self, symbol: str) -> FundingSnapshot:
        """Builds a FundingSnapshot for the symbol."""
        score = self.history.carry_score(symbol)
        rates = self.history.get_rates(symbol, 1)
        current_rate = rates[0] if rates else 0.0
        
        arb_signal = abs(score) >= 2.5
        
        if score >= 2.5:
            direction = "short_arb"
        elif score <= -2.5:
            direction = "long_arb"
        else:
            direction = "none"
            
        return FundingSnapshot(
            symbol=symbol,
            rate=current_rate,
            rate_24h_avg=self.history.avg(symbol, 24),
            rate_7d_avg=self.history.avg_7d(symbol),
            carry_score=score,
            arb_signal=arb_signal,
            direction=direction,
            timestamp_ms=int(time.time() * 1000)
        )

    def get_best_opportunity(self) -> Optional[FundingSnapshot]:
        """Returns the snapshot with the highest abs(carry_score) if arb_signal is True."""
        best_opp = None
        max_score = 0.0
        
        for snap in self._snapshots.values():
            if snap.arb_signal:
                if abs(snap.carry_score) > max_score:
                    max_score = abs(snap.carry_score)
                    best_opp = snap
                    
        return best_opp

    def should_update(self) -> bool:
        """Returns True if it's been more than an hour since the last update."""
        return (time.time() * 1000 - self.last_update_ms) >= 3600000
