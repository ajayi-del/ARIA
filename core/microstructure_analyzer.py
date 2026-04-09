import structlog
from typing import Literal, Dict, Any, List, Optional
import numpy as np
from datetime import datetime, timedelta
from intelligence.market_state import MarketState

logger = structlog.get_logger(__name__)


class MicrostructureAnalyzer:
    """Tier 4 - Market microstructure analysis"""
    
    def __init__(self):
        self.sweep_history: Dict[str, List[Dict[str, Any]]] = {}
        self.imbalance_history: Dict[str, List[float]] = {}
        self.absorption_levels: Dict[str, List[float]] = {}
        self.price_history: Dict[str, List[float]] = {}
        
    def analyze_microstructure(
        self,
        symbol: str,
        orderbook_data: Dict[str, Any],
        trade_data: List[Dict[str, Any]],
        mark_price: float
    ) -> tuple[
        Literal["buy_side", "sell_side", "none"],
        Optional[int],
        bool,
        float,
        bool,
        Literal["bullish_reversion", "bearish_reversion", "none"],
        float
    ]:
        """
        Analyze market microstructure
        
        Returns: (sweep, sweep_index, reclaim, imbalance, absorption, divergence_signal, mark_local_spread_pct)
        """
        
        # 1. Sweep detection
        sweep, sweep_index = self._detect_sweep(symbol, orderbook_data, trade_data)
        
        # 2. Reclaim detection
        reclaim = self._detect_reclaim(symbol, orderbook_data, mark_price)
        
        # 3. Imbalance calculation
        imbalance = self._calculate_orderbook_imbalance(orderbook_data)
        
        # 4. Absorption detection
        absorption = self._detect_absorption(symbol, orderbook_data, trade_data)
        
        # 5. Divergence signal
        divergence_signal = self._detect_divergence(symbol, orderbook_data, mark_price)
        
        # 6. Mark-local spread percentage
        mark_local_spread_pct = self._calculate_mark_local_spread(orderbook_data, mark_price)
        
        return sweep, sweep_index, reclaim, imbalance, absorption, divergence_signal, mark_local_spread_pct
    
    def _detect_sweep(
        self,
        symbol: str,
        orderbook_data: Dict[str, Any],
        trade_data: List[Dict[str, Any]]
    ) -> tuple[Literal["buy_side", "sell_side", "none"], Optional[int]]:
        """Detect liquidity sweeps"""
        
        if not orderbook_data or "bids" not in orderbook_data or "asks" not in orderbook_data:
            return "none", None
        
        bids = orderbook_data["bids"]
        asks = orderbook_data["asks"]
        
        if not bids or not asks:
            return "none", None
        
        # Get key levels
        best_bid = bids[0][0] if bids else 0
        best_ask = asks[0][0] if asks else 0
        mid_price = (best_bid + best_ask) / 2
        
        # Look for sweep patterns in recent trades
        if not trade_data or len(trade_data) < 5:
            return "none", None
        
        recent_trades = trade_data[-10:]  # Last 10 trades
        
        # Check for buy-side sweep (price dipped below bids then recovered)
        buy_sweep = False
        sell_sweep = False
        sweep_index = None
        
        for i, trade in enumerate(recent_trades):
            trade_price = trade.get("price", 0)
            trade_size = trade.get("size", 0)
            
            # Buy-side sweep: large trades below best bid, then price recovers
            if trade_price < best_bid and trade_size > np.mean([t.get("size", 0) for t in recent_trades]) * 2:
                if i < len(recent_trades) - 1:
                    next_trade = recent_trades[i + 1]
                    if next_trade.get("price", 0) > mid_price:
                        buy_sweep = True
                        sweep_index = len(recent_trades) - i - 1
                        break
            
            # Sell-side sweep: large trades above best ask, then price recovers
            if trade_price > best_ask and trade_size > np.mean([t.get("size", 0) for t in recent_trades]) * 2:
                if i < len(recent_trades) - 1:
                    next_trade = recent_trades[i + 1]
                    if next_trade.get("price", 0) < mid_price:
                        sell_sweep = True
                        sweep_index = len(recent_trades) - i - 1
                        break
        
        if buy_sweep:
            return "buy_side", sweep_index
        elif sell_sweep:
            return "sell_side", sweep_index
        else:
            return "none", None
    
    def _detect_reclaim(self, symbol: str, orderbook_data: Dict[str, Any], mark_price: float) -> bool:
        """Detect price reclaim of key levels"""
        
        if not orderbook_data or "bids" not in orderbook_data or "asks" not in orderbook_data:
            return False
        
        bids = orderbook_data["bids"]
        asks = orderbook_data["asks"]
        
        if not bids or not asks:
            return False
        
        # Get significant levels
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        
        # Check if mark price has reclaimed a level after breaking it
        if symbol not in self.price_history or len(self.price_history[symbol]) < 10:
            return False
        
        recent_prices = self.price_history[symbol][-10:]
        min_price = min(recent_prices)
        max_price = max(recent_prices)
        
        # Reclaim logic: price broke level then came back
        if mark_price > best_bid and min_price < best_bid:
            return True  # Reclaimed bid level
        elif mark_price < best_ask and max_price > best_ask:
            return True  # Reclaimed ask level
        
        return False
    
    def _calculate_orderbook_imbalance(self, orderbook_data: Dict[str, Any]) -> float:
        """Calculate orderbook imbalance (-1 to +1)"""
        
        if not orderbook_data or "bids" not in orderbook_data or "asks" not in orderbook_data:
            return 0.0
        
        bids = orderbook_data["bids"]
        asks = orderbook_data["asks"]
        
        if not bids or not asks:
            return 0.0
        
        # Calculate bid and ask volumes (top 5 levels)
        bid_volume = sum(bid[1] for bid in bids[:5])
        ask_volume = sum(ask[1] for ask in asks[:5])
        
        total_volume = bid_volume + ask_volume
        if total_volume == 0:
            return 0.0
        
        # Imbalance: positive = more bids, negative = more asks
        imbalance = (bid_volume - ask_volume) / total_volume
        
        return max(-1.0, min(1.0, imbalance))
    
    def _detect_absorption(
        self,
        symbol: str,
        orderbook_data: Dict[str, Any],
        trade_data: List[Dict[str, Any]]
    ) -> bool:
        """Detect absorption of large orders"""
        
        if not trade_data or len(trade_data) < 10:
            return False
        
        # Look for large trades that don't move price significantly
        recent_trades = trade_data[-20:]
        
        large_trades = [t for t in recent_trades if t.get("size", 0) > np.mean([t.get("size", 0) for t in recent_trades]) * 3]
        
        if not large_trades:
            return False
        
        # Check if price moved less than expected for these large trades
        price_impact = 0.0
        for trade in large_trades:
            trade_price = trade.get("price", 0)
            expected_move = trade.get("size", 0) * 0.001  # Expected 0.1% move per unit size
            
            # Compare with actual price movement
            if len(recent_trades) > 1:
                prev_price = recent_trades[recent_trades.index(trade) - 1].get("price", trade_price)
                actual_move = abs(trade_price - prev_price)
                price_impact += actual_move / max(expected_move, 0.0001)
        
        avg_impact = price_impact / len(large_trades) if large_trades else 0
        
        # Low impact indicates absorption
        return avg_impact < 0.5
    
    def _detect_divergence(
        self,
        symbol: str,
        orderbook_data: Dict[str, Any],
        mark_price: float
    ) -> Literal["bullish_reversion", "bearish_reversion", "none"]:
        """Detect price-microstructure divergence signals"""
        
        if not orderbook_data or "bids" not in orderbook_data or "asks" not in orderbook_data:
            return "none"
        
        bids = orderbook_data["bids"]
        asks = orderbook_data["asks"]
        
        if not bids or not asks:
            return "none"
        
        # Calculate orderbook skew
        bid_volume = sum(bid[1] for bid in bids[:5])
        ask_volume = sum(ask[1] for ask in asks[:5])
        skew = (bid_volume - ask_volume) / (bid_volume + ask_volume)
        
        # Get recent price trend
        if symbol not in self.price_history or len(self.price_history[symbol]) < 10:
            return "none"
        
        recent_prices = self.price_history[symbol][-10:]
        price_trend = (recent_prices[-1] - recent_prices[0]) / recent_prices[0]
        
        # Bullish divergence: price down but orderbook skewed bullish
        if price_trend < -0.01 and skew > 0.3:
            return "bullish_reversion"
        
        # Bearish divergence: price up but orderbook skewed bearish
        elif price_trend > 0.01 and skew < -0.3:
            return "bearish_reversion"
        
        return "none"
    
    def _calculate_mark_local_spread(self, orderbook_data: Dict[str, Any], mark_price: float) -> float:
        """Calculate mark-local spread as percentage"""
        
        if not orderbook_data or "bids" not in orderbook_data or "asks" not in orderbook_data:
            return 0.0
        
        bids = orderbook_data["bids"]
        asks = orderbook_data["asks"]
        
        if not bids or not asks:
            return 0.0
        
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        mid_price = (best_bid + best_ask) / 2
        
        # Calculate how far mark price is from local mid price
        spread = abs(mark_price - mid_price) / mid_price
        
        return spread * 100  # Convert to percentage
