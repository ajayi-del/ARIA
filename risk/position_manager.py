"""
Position Manager

Tracks all open positions across all symbols.
Single source of truth for position state.
"""

import structlog
import time
from typing import List, Dict, Optional
from execution.schemas import Position

logger = structlog.get_logger(__name__)


class PositionManager:
    """
    Tracks all open positions across all symbols.
    Single source of truth for position state.
    """
    
    def __init__(self):
        self._positions: Dict[str, List[Position]] = {}
    
    def get_all(self) -> list:
        """
        Alias for compatibility. 
        Returns flattened list of all open positions across all symbols.
        """
        existing = getattr(self, '_positions', {})
        if isinstance(existing, dict):
            # Flatten dict of lists
            result = []
            for symbol_positions in existing.values():
                if isinstance(symbol_positions, list):
                    result.extend(symbol_positions)
                else:
                    result.append(symbol_positions)
            return result
        elif isinstance(existing, list):
            return existing
        return []

    def add(self, position: Position) -> None:
        """Add a new position to tracking"""
        if position.symbol not in self._positions:
            self._positions[position.symbol] = []
        
        self._positions[position.symbol].append(position)
    
    def get(self, symbol: str) -> List[Position]:
        """Returns all open positions for symbol"""
        return self._positions.get(symbol, [])
    
    def count(self, symbol: str) -> int:
        """
        Returns number of open positions.
        Hard limit: if count >= 2 → block new entry.
        """
        return len(self._positions.get(symbol, []))
    
    def can_pyramid(self, symbol: str) -> bool:
        """
        Returns True only if:
        count(symbol) == 1 AND
        get(symbol)[0].tp1_hit == True
        """
        positions = self.get(symbol)
        if len(positions) != 1:
            return False
        
        return positions[0].tp1_hit
    
    def mark_tp1_hit(self, symbol: str, position_idx: int = 0) -> Optional[float]:
        """
        Sets tp1_hit = True and returns the NEW stop price (Golden Stop).
        Golden Stop = Entry + (TP1 - Entry) * 0.5
        """
        positions = self.get(symbol)
        if position_idx < len(positions):
            pos = positions[position_idx]
            pos.tp1_hit = True
            pos.tp1_hit_at_ms = int(time.time() * 1000)

            # Calculate Golden Stop
            if pos.side == "long":
                new_stop = pos.entry_price + (pos.tp1_price - pos.entry_price) * 0.5
            else:
                new_stop = pos.entry_price - (pos.entry_price - pos.tp1_price) * 0.5
                
            pos.stop_price = new_stop
            pos.stop_moved = True
            pos.golden_stop_used = True
            logger.info("stop_to_golden", symbol=symbol,
                        entry=pos.entry_price, tp1=pos.tp1_price, new_stop=round(new_stop, 6))
            return new_stop
        return None

    def mark_tp2_hit(self, symbol: str, position_idx: int = 0) -> Optional[float]:
        """
        Sets tp2_hit = True and moves stop to TP1 price.
        """
        positions = self.get(symbol)
        if position_idx < len(positions):
            pos = positions[position_idx]
            pos.tp2_hit = True
            
            # Move stop to TP1 level
            new_stop = pos.tp1_price
            pos.stop_price = new_stop
            pos.tp1_level_stop_used = True
            logger.info("stop_to_tp1_level", symbol=symbol, tp1=pos.tp1_price)
            return new_stop
        return None
    
    def close(self, symbol: str, position_idx: int) -> None:
        """Removes position from tracking"""
        positions = self.get(symbol)
        if position_idx < len(positions):
            position = positions.pop(position_idx)
            if not positions:  # No more positions for this symbol
                del self._positions[symbol]
            
            pnl = self._calculate_pnl(position)
            logger.info("position_closed", symbol=symbol, side=position.side, pnl=round(pnl, 2))
    
    def liq_distance_pct(self, symbol: str, current_price: float) -> float:
        """
        Returns % distance from current price to nearest liquidation price.
        Used for health monitoring.
        """
        positions = self.get(symbol)
        if not positions:
            return 0.0
        
        # Find closest liquidation price
        min_distance = float('inf')
        for position in positions:
            distance = abs(current_price - position.liq_price) / current_price
            min_distance = min(min_distance, distance)
        
        return min_distance * 100  # Convert to percentage
    
    def net_exposure(self) -> Dict[str, float]:
        """
        Returns net USD exposure per symbol.
        Long = positive, short = negative.
        """
        exposure = {}
        for symbol, positions in self._positions.items():
            net_exposure = 0.0
            for position in positions:
                notional = position.size * position.entry_price
                if position.side == "long":
                    net_exposure += notional
                else:
                    net_exposure -= notional
            
            exposure[symbol] = net_exposure
        
        return exposure
    
    def _calculate_pnl(self, position: Position) -> float:
        """Calculate unrealized P&L for position (placeholder)"""
        # This would need current market price
        return 0.0
