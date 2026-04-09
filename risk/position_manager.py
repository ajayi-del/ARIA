"""
Position Manager

Tracks all open positions across all symbols.
Single source of truth for position state.
"""

from typing import List, Dict
from execution.schemas import Position


class PositionManager:
    """
    Tracks all open positions across all symbols.
    Single source of truth for position state.
    """
    
    def __init__(self):
        self._positions: Dict[str, List[Position]] = {}
    
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
    
    def mark_tp1_hit(self, symbol: str, position_idx: int = 0) -> None:
        """Sets tp1_hit = True"""
        positions = self.get(symbol)
        if position_idx < len(positions):
            positions[position_idx].tp1_hit = True
    
    def close(self, symbol: str, position_idx: int) -> None:
        """Removes position from tracking"""
        positions = self.get(symbol)
        if position_idx < len(positions):
            position = positions.pop(position_idx)
            if not positions:  # No more positions for this symbol
                del self._positions[symbol]
            
            # Log close with P&L
            pnl = self._calculate_pnl(position)
            print(f"Position closed: {symbol} {position.side} P&L: {pnl:.2f}")
    
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
