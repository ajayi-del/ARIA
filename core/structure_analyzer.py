import structlog
from typing import Literal, Dict, Any, List
import numpy as np
from datetime import datetime, timedelta
from intelligence.market_state import MarketState

logger = structlog.get_logger(__name__)


class StructureAnalyzer:
    """Tier 3 - Market structure analysis (expansion, compression, trend, chop)"""
    
    def __init__(self):
        self.atr_history: Dict[str, List[float]] = {}
        self.price_history: Dict[str, List[float]] = {}
        
    def calculate_atr(
        self,
        candles: list,
        period: int = 14
    ) -> float:
        """Public interface for ATR computation from candle objects."""
        if not candles or len(candles) < period + 1:
            return 0.0
        
        highs = [float(c.high) for c in candles]
        lows = [float(c.low) for c in candles]
        closes = [float(c.close) for c in candles]
        
        return self._calculate_atr(highs, lows, closes, period)

    def calculate_baseline_atr(self, candles: List[Any], period: int = 20) -> float:
        """Calculates baseline ATR for ratio computation."""
        # Simple implementation: average of ATR over last n candles
        # In this phase we use a static baseline if history is short
        if not candles or len(candles) < period + 1:
            return 0.01
        
        # For simplicity in this Tier 3 adaptor:
        return self.calculate_atr(candles, period)

    def atr_ratio(self, current_atr: float, baseline_atr: float) -> float:
        """Computes volatility ratio."""
        if baseline_atr == 0:
            return 1.0
        return current_atr / baseline_atr

    def classify_regime(self, candles: List[Any], atr: float, ratio: float) -> str:
        """Classifies the market regime from state."""
        # Use prices and volume for more accurate classification
        prices = [float(c.close) for c in candles]
        volumes = [float(c.volume) for c in candles]
        highs = [float(c.high) for c in candles]
        lows = [float(c.low) for c in candles]
        
        market_type, _, _ = self.analyze_structure(
            symbol="", # symbol context is held in price history
            price_data=prices,
            volume_data=volumes,
            high_data=highs,
            low_data=lows
        )
        return market_type

    def analyze_structure(
        self,
        symbol: str,
        price_data: List[float],
        volume_data: List[float],
        high_data: List[float],
        low_data: List[float]
    ) -> tuple[Literal["expansion", "compression", "trend", "chop"], float, float]:
        """
        Analyze market structure
        
        Returns: (market_type, atr, atr_vs_baseline)
        """
        if len(price_data) < 20:
            return "chop", 0.01, 1.0
        
        # Calculate ATR
        atr = self._calculate_atr(high_data, low_data, price_data, period=14)
        
        # Calculate ATR vs baseline (20-period average)
        atr_vs_baseline = self._calculate_atr_vs_baseline(symbol, atr)
        
        # Determine market type
        market_type = self._determine_market_type(
            price_data, volume_data, atr, atr_vs_baseline
        )
        
        # Update history
        if symbol not in self.atr_history:
            self.atr_history[symbol] = []
        self.atr_history[symbol].append(atr)
        
        if symbol not in self.price_history:
            self.price_history[symbol] = []
        self.price_history[symbol].extend(price_data[-20:])
        
        # Keep history manageable
        if len(self.atr_history[symbol]) > 100:
            self.atr_history[symbol] = self.atr_history[symbol][-100:]
        if len(self.price_history[symbol]) > 200:
            self.price_history[symbol] = self.price_history[symbol][-200:]
        
        return market_type, atr, atr_vs_baseline
    
    def _calculate_atr(self, high_data: List[float], low_data: List[float], close_data: List[float], period: int = 14) -> float:
        """Calculate Average True Range using EWM(14) as specified."""
        if len(high_data) < period + 1 or len(low_data) < period + 1 or len(close_data) < period + 1:
            return 0.01

        true_ranges = []
        for i in range(1, len(close_data)):
            high = high_data[i]
            low = low_data[i]
            prev_close = close_data[i - 1]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)

        if not true_ranges:
            return 0.01

        # EWM with span=period, adjust=False (Wilder's smoothing = span=2*period-1 equivalent)
        alpha = 1.0 / period  # Wilder's smoothing factor
        atr = true_ranges[0]
        for tr in true_ranges[1:]:
            atr = alpha * tr + (1.0 - alpha) * atr
        return atr
    
    def _calculate_atr_vs_baseline(self, symbol: str, current_atr: float) -> float:
        """Calculate ATR ratio vs 20-period baseline"""
        if symbol not in self.atr_history or len(self.atr_history[symbol]) < 20:
            return 1.0
        
        recent_atrs = self.atr_history[symbol][-20:]
        baseline_atr = np.mean(recent_atrs)
        
        if baseline_atr == 0:
            return 1.0
        
        return current_atr / baseline_atr
    
    def _determine_market_type(
        self,
        price_data: List[float],
        volume_data: List[float],
        atr: float,
        atr_vs_baseline: float
    ) -> Literal["expansion", "compression", "trend", "chop"]:
        """Determine market structure type"""
        
        if len(price_data) < 20:
            return "chop"
        
        # Calculate price volatility
        returns = np.diff(price_data[-20:])
        if len(returns) == 0:
             return "chop"
        volatility = np.std(returns)
        
        # Volume analysis
        if len(volume_data) >= 20:
            recent_volume = np.mean(volume_data[-20:])
            volume_trend = np.polyfit(range(20), volume_data[-20:], 1)[0]
        else:
            recent_volume = 0
            volume_trend = 0
        
        # Trend analysis
        price_trend = self._calculate_trend_strength(price_data[-20:])
        
        # Logic for market type determination
        if atr_vs_baseline > 1.5:
            # High volatility expansion
            if volume_trend > 0:
                return "expansion"
            else:
                return "chop"
        elif atr_vs_baseline < 0.7:
            # Low volatility compression
            return "compression"
        else:
            # Normal volatility
            if abs(price_trend) > 0.02:  # 2% trend threshold
                return "trend"
            else:
                return "chop"
    
    def _calculate_trend_strength(self, prices: List[float]) -> float:
        """Calculate trend strength using linear regression"""
        if len(prices) < 10:
            return 0.0
        
        x = np.arange(len(prices))
        y = np.array(prices)
        
        if len(y) < 2:
            return 0.0

        # Linear regression
        coeffs = np.polyfit(x, y, 1)
        slope = coeffs[0]
        
        # Normalize by price level
        avg_price = np.mean(y)
        normalized_slope = slope / avg_price
        
        return normalized_slope
    
    def detect_breakout_potential(self, symbol: str) -> Dict[str, Any]:
        """Detect potential breakout scenarios"""
        if symbol not in self.price_history or len(self.price_history[symbol]) < 50:
            return {"potential": False, "reason": "insufficient_data"}
        
        prices = self.price_history[symbol][-50:]
        
        # Calculate recent price range
        recent_high = max(prices[-20:])
        recent_low = min(prices[-20:])
        price_range = recent_high - recent_low
        current_price = prices[-1]
        
        # Check if price is at range boundary
        at_boundary = False
        direction = None
        
        if abs(current_price - recent_high) < (price_range * 0.05):
            at_boundary = True
            direction = "upward"
        elif abs(current_price - recent_low) < (price_range * 0.05):
            at_boundary = True
            direction = "downward"
        
        # Volume confirmation (if available)
        volume_confirmation = True  # Placeholder
        
        # ATR expansion check
        atr_expanding = False
        if symbol in self.atr_history and len(self.atr_history[symbol]) >= 10:
            recent_atrs = self.atr_history[symbol][-10:]
            atr_trend = np.polyfit(range(10), recent_atrs, 1)[0]
            atr_expanding = atr_trend > 0
        
        potential = at_boundary and volume_confirmation and atr_expanding
        
        return {
            "potential": potential,
            "direction": direction,
            "at_boundary": at_boundary,
            "volume_confirmation": volume_confirmation,
            "atr_expanding": atr_expanding,
            "price_range_pct": (price_range / current_price) * 100
        }
