import structlog
from typing import Literal, Dict, Any, List, Optional
import numpy as np
from datetime import datetime, timedelta
from intelligence.market_state import MarketState

logger = structlog.get_logger(__name__)


class MAGAnalyzer:
    """Tier 6 - MAG (Market Algorithmic Generator) Signal Analysis"""
    
    def __init__(self):
        self.mag_signals: Dict[str, List[Dict[str, Any]]] = {}
        self.mag_lag_timers: Dict[str, int] = {}
        self.signal_history: Dict[str, List[Dict[str, Any]]] = {}
        
        # MAG signal parameters
        self.min_lag_time = 15  # Minimum 15 minutes between signals
        self.signal_confidence_threshold = 0.7
        self.signal_decay_rate = 0.95  # Signal strength decay per minute
        
    def analyze_mag_signal(
        self,
        symbol: str,
        market_state_data: Dict[str, Any],
        price_action: Dict[str, Any],
        volume_profile: Dict[str, Any]
    ) -> tuple[bool, Literal["bullish", "bearish", "none"], int]:
        """
        Analyze MAG signal
        
        Returns: (mag_active, mag_direction, mag_lag_remaining_min)
        """
        
        # Check if we're in lag period
        lag_remaining = self._check_lag_timer(symbol)
        if lag_remaining > 0:
            return False, "none", lag_remaining
        
        # Generate MAG signal
        signal = self._generate_mag_signal(symbol, market_state_data, price_action, volume_profile)
        
        if signal["active"]:
            # Start lag timer
            self._start_lag_timer(symbol)
            
            # Store signal
            self._store_signal(symbol, signal)
            
            return True, signal["direction"], 0
        else:
            return False, "none", lag_remaining
    
    def _check_lag_timer(self, symbol: str) -> int:
        """Check remaining lag time for symbol"""
        if symbol not in self.mag_lag_timers:
            return 0
        
        remaining = self.mag_lag_timers[symbol]
        
        # Update lag timer (called every minute)
        if remaining > 0:
            self.mag_lag_timers[symbol] = remaining - 1
        
        return max(0, remaining)
    
    def _start_lag_timer(self, symbol: str):
        """Start lag timer for symbol"""
        self.mag_lag_timers[symbol] = self.min_lag_time
    
    def _generate_mag_signal(
        self,
        symbol: str,
        market_state_data: Dict[str, Any],
        price_action: Dict[str, Any],
        volume_profile: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate MAG signal based on multiple factors"""
        
        # Initialize signal components
        bullish_score = 0.0
        bearish_score = 0.0
        
        # 1. Price momentum component
        momentum_signal = self._analyze_price_momentum(price_action)
        if momentum_signal["direction"] == "bullish":
            bullish_score += momentum_signal["strength"]
        elif momentum_signal["direction"] == "bearish":
            bearish_score += momentum_signal["strength"]
        
        # 2. Volume confirmation component
        volume_signal = self._analyze_volume_confirmation(volume_profile)
        if volume_signal["direction"] == "bullish":
            bullish_score += volume_signal["strength"]
        elif volume_signal["direction"] == "bearish":
            bearish_score += volume_signal["strength"]
        
        # 3. Market state coherence component
        coherence_signal = self._analyze_market_state_coherence(market_state_data)
        if coherence_signal["direction"] == "bullish":
            bullish_score += coherence_signal["strength"]
        elif coherence_signal["direction"] == "bearish":
            bearish_score += coherence_signal["strength"]
        
        # 4. Pattern recognition component
        pattern_signal = self._analyze_chart_patterns(price_action)
        if pattern_signal["direction"] == "bullish":
            bullish_score += pattern_signal["strength"]
        elif pattern_signal["direction"] == "bearish":
            bearish_score += pattern_signal["strength"]
        
        # 5. Support/resistance breach component
        sr_signal = self._analyze_support_resistance(price_action)
        if sr_signal["direction"] == "bullish":
            bullish_score += sr_signal["strength"]
        elif sr_signal["direction"] == "bearish":
            bearish_score += sr_signal["strength"]
        
        # Calculate final signal
        total_score = bullish_score - bearish_score
        max_possible_score = 5.0  # 5 components max 1.0 each
        
        signal_strength = abs(total_score) / max_possible_score
        
        # Determine if signal is active
        signal_active = signal_strength >= self.signal_confidence_threshold
        
        # Determine direction
        if total_score > 0:
            direction = "bullish"
        elif total_score < 0:
            direction = "bearish"
        else:
            direction = "none"
        
        return {
            "active": signal_active,
            "direction": direction,
            "strength": signal_strength,
            "bullish_score": bullish_score,
            "bearish_score": bearish_score,
            "components": {
                "momentum": momentum_signal,
                "volume": volume_signal,
                "coherence": coherence_signal,
                "pattern": pattern_signal,
                "support_resistance": sr_signal
            }
        }
    
    def _analyze_price_momentum(self, price_action: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze price momentum for MAG signal"""
        prices = price_action.get("prices", [])
        if len(prices) < 10:
            return {"direction": "none", "strength": 0.0}
        
        # Calculate momentum using multiple timeframes
        short_momentum = self._calculate_momentum(prices[-5:])
        medium_momentum = self._calculate_momentum(prices[-10:])
        long_momentum = self._calculate_momentum(prices[-20:]) if len(prices) >= 20 else 0
        
        # Weighted momentum score
        momentum_score = (short_momentum * 0.5 + medium_momentum * 0.3 + long_momentum * 0.2)
        
        strength = min(abs(momentum_score), 1.0)
        
        if momentum_score > 0.02:
            direction = "bullish"
        elif momentum_score < -0.02:
            direction = "bearish"
        else:
            direction = "none"
        
        return {"direction": direction, "strength": strength}
    
    def _analyze_volume_confirmation(self, volume_profile: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze volume confirmation for MAG signal"""
        recent_volume = volume_profile.get("recent_volume", 0)
        avg_volume = volume_profile.get("avg_volume", 1)
        volume_trend = volume_profile.get("volume_trend", 0)
        
        if avg_volume == 0:
            return {"direction": "none", "strength": 0.0}
        
        volume_ratio = recent_volume / avg_volume
        strength = min(volume_ratio / 3.0, 1.0)  # Cap at 3x average volume
        
        if volume_trend > 0.2 and volume_ratio > 1.5:
            direction = "bullish"
        elif volume_trend < -0.2 and volume_ratio > 1.5:
            direction = "bearish"
        else:
            direction = "none"
        
        return {"direction": direction, "strength": strength}
    
    def _analyze_market_state_coherence(self, market_state_data: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze market state coherence for MAG signal"""
        coherence_score = market_state_data.get("coherence_score", 0)
        trade_direction = market_state_data.get("trade_direction", "none")
        
        # Convert coherence score to strength
        strength = coherence_score / 6.0  # Normalize to 0-1
        
        return {"direction": trade_direction, "strength": strength}
    
    def _analyze_chart_patterns(self, price_action: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze chart patterns for MAG signal"""
        prices = price_action.get("prices", [])
        if len(prices) < 20:
            return {"direction": "none", "strength": 0.0}
        
        # Simple pattern detection (can be expanded)
        pattern_score = 0.0
        direction = "none"
        
        # Check for breakout pattern
        if len(prices) >= 20:
            recent_prices = prices[-10:]
            older_prices = prices[-20:-10]
            
            recent_volatility = np.std(recent_prices) / np.mean(recent_prices)
            older_volatility = np.std(older_prices) / np.mean(older_prices)
            
            # Breakout: recent price moves beyond recent range with higher volume
            if recent_volatility > older_volatility * 1.5:
                price_change = (recent_prices[-1] - recent_prices[0]) / recent_prices[0]
                if price_change > 0.02:
                    pattern_score = 0.8
                    direction = "bullish"
                elif price_change < -0.02:
                    pattern_score = 0.8
                    direction = "bearish"
        
        strength = min(pattern_score, 1.0)
        
        return {"direction": direction, "strength": strength}
    
    def _analyze_support_resistance(self, price_action: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze support/resistance levels for MAG signal"""
        current_price = price_action.get("current_price", 0)
        support_levels = price_action.get("support_levels", [])
        resistance_levels = price_action.get("resistance_levels", [])
        
        if current_price == 0:
            return {"direction": "none", "strength": 0.0}
        
        # Find nearest support and resistance
        nearest_support = None
        nearest_resistance = None
        
        if support_levels:
            nearest_support = min(support_levels, key=lambda x: abs(x - current_price))
        
        if resistance_levels:
            nearest_resistance = min(resistance_levels, key=lambda x: abs(x - current_price))
        
        # Check for breaches
        signal_strength = 0.0
        direction = "none"
        
        if nearest_support and current_price < nearest_support * 0.99:
            # Support breach
            signal_strength = (nearest_support - current_price) / nearest_support
            direction = "bearish"
        elif nearest_resistance and current_price > nearest_resistance * 1.01:
            # Resistance breach
            signal_strength = (current_price - nearest_resistance) / nearest_resistance
            direction = "bullish"
        
        strength = min(signal_strength * 10, 1.0)  # Scale and cap
        
        return {"direction": direction, "strength": strength}
    
    def _calculate_momentum(self, prices: List[float]) -> float:
        """Calculate price momentum for a given period"""
        if len(prices) < 2:
            return 0.0
        
        # Simple momentum calculation
        start_price = prices[0]
        end_price = prices[-1]
        
        return (end_price - start_price) / start_price
    
    def _store_signal(self, symbol: str, signal: Dict[str, Any]):
        """Store MAG signal in history"""
        if symbol not in self.signal_history:
            self.signal_history[symbol] = []
        
        self.signal_history[symbol].append({
            "timestamp": datetime.now().isoformat(),
            "signal": signal
        })
        
        # Keep only last 50 signals
        if len(self.signal_history[symbol]) > 50:
            self.signal_history[symbol] = self.signal_history[symbol][-50:]
    
    def get_signal_history(self, symbol: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent signal history for symbol"""
        if symbol not in self.signal_history:
            return []
        
        return self.signal_history[symbol][-limit:]
    
    def get_signal_performance(self, symbol: str) -> Dict[str, Any]:
        """Analyze MAG signal performance"""
        if symbol not in self.signal_history or len(self.signal_history[symbol]) < 5:
            return {"error": "Insufficient signal history"}
        
        signals = self.signal_history[symbol]
        
        # Calculate success rate (placeholder - would need actual price data)
        total_signals = len(signals)
        successful_signals = sum(1 for s in signals if s["signal"]["strength"] > 0.8)
        
        success_rate = successful_signals / total_signals if total_signals > 0 else 0
        
        # Average signal strength
        avg_strength = np.mean([s["signal"]["strength"] for s in signals])
        
        return {
            "total_signals": total_signals,
            "success_rate": success_rate,
            "avg_strength": avg_strength,
            "last_signal": signals[-1] if signals else None
        }
