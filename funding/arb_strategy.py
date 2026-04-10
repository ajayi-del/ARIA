import time
import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional
import structlog
from core.config import Settings
from risk.position_manager import PositionManager
from funding.radar import FundingRadar, FundingSnapshot
from funding.history import FundingHistory
from execution.schemas import TradeCandidate, Position

logger = structlog.get_logger(__name__)

@dataclass
class ArbPosition:
    symbol: str
    direction: str  # "long_arb" (long perp + short spot) or "short_arb" (short perp + long spot)
    spot_size: float
    perp_size: float
    entry_rate: float
    target_exit_rate: float = 0.01
    max_hold_hours: int = 72
    opened_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    funding_collected: float = 0.0
    perp_entry_price: float = 0.0
    spot_entry_price: float = 0.0
    status: str = "open"
    
    # UI/Telemetry fields
    current_pnl: float = 0.0
    long_venue: str = "spot"
    short_venue: str = "perps"
    entry_spread_pct: float = 0.0
    size: float = 0.0

class FundingArbStrategy:
    """Manages delta-neutral funding arbitrage positions."""
    
    def __init__(
        self,
        config: Settings,
        client: Any, 
        position_manager: PositionManager,
        radar: FundingRadar,
        history: FundingHistory
    ):
        self.config = config
        self.client = client
        self.position_manager = position_manager
        self.radar = radar
        self.history = history
        self._open_arbs: Dict[str, ArbPosition] = {}

    async def evaluate(self) -> Optional[ArbPosition]:
        """Checks for new arb opportunities."""
        opp = self.radar.get_best_opportunity()
        if not opp:
            return None
            
        # Already have an arb position for this symbol
        if opp.symbol in self._open_arbs:
            return None
            
        # 2. Capital check - Use dynamic allocation from RiskEngine if available
        balance = await self.client.get_account_balance(self.config.account_id or "paper")
        arb_capital = getattr(self, 'current_allocation', balance * self.config.arb_capital_pct)
        
        if arb_capital < 100:  # Minimum capital threshold
            return None
            
        # Build candidate
        # For simplicity, we assume spot and perp prices are close (mark price)
        # In a real environment, we'd fetch both book depths.
        rate = opp.rate
        
        # Calculate size based on arb_capital
        # net exposure is zero, so we buy $X spot and sell $X perp (or vice versa)
        # For 1x leverage, size = capital / price
        trade_flow = self.radar.trade_flow_stores.get(opp.symbol)
        price = trade_flow.latest_price() if trade_flow else 0.0
        
        if price <= 0:
            return None
            
        size = arb_capital / price
        
        candidate = ArbPosition(
            symbol=opp.symbol,
            direction=opp.direction,
            spot_size=size,
            perp_size=size,
            entry_rate=rate,
            perp_entry_price=price,
            spot_entry_price=price,
            opened_at_ms=int(time.time() * 1000)
        )
        
        return candidate

    async def open_arb(self, candidate: ArbPosition) -> bool:
        """Opens simultaneous spot and perp positions - v1.3.FIXED."""
        symbol = candidate.symbol
        logger.info("opening_arb_started", symbol=symbol, direction=candidate.direction, version="v1.3.FIXED")
        
        try:
            # 1. Ensure 1x leverage (arb yield comes from funding, not delta)
            # Placeholder: actual client call to set leverage
            
            # 2. Market orders for speed (v1.3 target < 500ms gap)
            start_time = time.time()
            success = False
            if candidate.direction == "short_arb":
                # Short Perp (side 2) + Long Spot (side 1)
                perp_task = self.client.place_order({
                    "symbol": symbol,
                    "orders": [{"symbol": symbol, "side": 2, "size": candidate.perp_size, "type": 1, "instrument": "perp"}]
                })
                spot_task = self.client.place_order({
                    "symbol": symbol, 
                    "orders": [{"symbol": symbol, "side": 1, "size": candidate.spot_size, "type": 1, "instrument": "spot"}]
                })
            else:
                # Long Perp (side 1) + Short Spot (side 2)
                perp_task = self.client.place_order({
                    "symbol": symbol,
                    "orders": [{"symbol": symbol, "side": 1, "size": candidate.perp_size, "type": 1, "instrument": "perp"}]
                })
                spot_task = self.client.place_order({
                    "symbol": symbol,
                    "orders": [{"symbol": symbol, "side": 2, "size": candidate.spot_size, "type": 1, "instrument": "spot"}]
                })
            
            results = await asyncio.gather(perp_task, spot_task, return_exceptions=True)
            end_time = time.time()
            gap_ms = (end_time - start_time) * 1000
            
            # 3. Check for failures
            failed = False
            for res in results:
                if isinstance(res, Exception) or (hasattr(res, "success") and not res.success):
                    failed = True
                    break
            
            if failed:
                logger.error("arb_leg_failed", symbol=symbol, results=results)
                # 4. Attempt to close any opened leg
                return False
            
            if gap_ms > 500:
                logger.warning("arb_leg_gap_exceeded", symbol=symbol, gap_ms=gap_ms)
            
            # Populate UI telemetry
            if candidate.direction == "short_arb":
                candidate.long_venue = "Bybit Spot"
                candidate.short_venue = "Bybit Perp"
            else:
                candidate.long_venue = "Bybit Perp"
                candidate.short_venue = "Bybit Spot"
            candidate.entry_spread_pct = 0.02 # Estimated for v1.3
            
            self._open_arbs[symbol] = candidate
            logger.info("arb_opened", symbol=symbol, direction=candidate.direction, size=candidate.perp_size, gap_ms=gap_ms)
            return True
            
        except Exception as e:
            logger.error("open_arb_exception", symbol=symbol, error=str(e))
            return False

    async def monitor_arbs(self, current_snapshots: Dict[str, FundingSnapshot]) -> None:
        """Monitors open arbs for exit conditions and tracks funding collected."""
        now_ms = int(time.time() * 1000)
        
        for symbol, arb in list(self._open_arbs.items()):
            if symbol not in current_snapshots:
                continue
                
            snap = current_snapshots[symbol]
            
            # 1. Update funding collected (hourly estimation for this phase)
            # hours_passed = (now_ms - arb.opened_at_ms) / 3600000
            # For this loop, we just apply the hourly rate whenever update_all is called (hourly)
            # In update_all interval:
            if self.radar.should_update():
               collected = arb.perp_size * arb.perp_entry_price * (abs(snap.rate) / 100) # hourly %
               arb.funding_collected += collected
            
            # 2. Check Exits
            # EXIT 1: Rate normalized
            if abs(snap.rate) < arb.target_exit_rate:
                await self.close_arb(symbol, "rate_normalized")
                continue
                
            # EXIT 2: Max hold time (72h)
            hours_passed = (now_ms - arb.opened_at_ms) / 3600000
            if hours_passed >= arb.max_hold_hours:
                await self.close_arb(symbol, "time_exit")
                continue
                
            # EXIT 3: Rate flipped against us
            if arb.direction == "short_arb" and snap.rate < -0.02:
                await self.close_arb(symbol, "rate_flipped")
                continue
            if arb.direction == "long_arb" and snap.rate > 0.02:
                await self.close_arb(symbol, "rate_flipped")
                continue

    async def close_arb(self, symbol: str, reason: str) -> None:
        """Closes both legs of the arbitrage position."""
        if symbol not in self._open_arbs:
            return
            
        arb = self._open_arbs[symbol]
        logger.info("closing_arb_started", symbol=symbol, reason=reason)
        
        try:
            # Market orders to close both legs
            if arb.direction == "short_arb":
                # Long Perp (side 1) + Short Spot (side 2)
                perp_task = self.client.place_order({
                    "symbol": symbol,
                    "orders": [{"symbol": symbol, "side": 1, "size": arb.perp_size, "type": 1, "instrument": "perp"}]
                })
                spot_task = self.client.place_order({
                    "symbol": symbol,
                    "orders": [{"symbol": symbol, "side": 2, "size": arb.spot_size, "type": 1, "instrument": "spot"}]
                })
            else:
                # Short Perp (side 2) + Long Spot (side 1)
                perp_task = self.client.place_order({
                    "symbol": symbol,
                    "orders": [{"symbol": symbol, "side": 2, "size": arb.perp_size, "type": 1, "instrument": "perp"}]
                })
                spot_task = self.client.place_order({
                    "symbol": symbol,
                    "orders": [{"symbol": symbol, "side": 1, "size": arb.spot_size, "type": 1, "instrument": "spot"}]
                })
                
            await asyncio.gather(perp_task, spot_task)
            
            hold_time = (time.time() * 1000 - arb.opened_at_ms) / 3600000
            logger.info("arb_closed", symbol=symbol, reason=reason, funding_collected=arb.funding_collected, hold_time_hours=hold_time)
            
            del self._open_arbs[symbol]
            
        except Exception as e:
            logger.error("close_arb_exception", symbol=symbol, error=str(e))

    def get_open_arbs(self) -> List[ArbPosition]:
        return list(self._open_arbs.values())

    def get_arb_summary(self) -> Dict[str, Any]:
        total_collected = sum(arb.funding_collected for arb in self._open_arbs.values())
        return {
            "active_arbs": len(self._open_arbs),
            "total_collected": total_collected,
            "open_symbols": list(self._open_arbs.keys())
        }
