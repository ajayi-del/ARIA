import structlog
from typing import Literal, Dict, Any, List
import numpy as np
from datetime import datetime, timedelta
from intelligence.market_state import MarketState

logger = structlog.get_logger(__name__)


class FundingAnalyzer:
    """Tier 5 - Funding rate analysis"""
    
    def __init__(self):
        self.funding_history: Dict[str, List[Dict[str, Any]]] = {}
        self.funding_thresholds = {
            "extreme_positive": 0.01,   # > 1%
            "positive": 0.001,           # 0.1% - 1%
            "neutral": -0.001,           # -0.1% - 0.1%
            "negative": -0.01,           # -1% - -0.1%
            "extreme_negative": -0.01    # < -1%
        }
        
    def analyze_funding(
        self,
        symbol: str,
        current_funding_rate: float,
        funding_history: List[float],
        mark_price: float,
        index_price: float
    ) -> Literal["extreme_positive", "positive", "neutral", "negative", "extreme_negative"]:
        """
        Analyze funding rate and classify
        
        Returns: funding_class
        """
        
        # Store funding data
        self._update_funding_history(symbol, current_funding_rate, mark_price, index_price)
        
        # Classify funding rate
        funding_class = self._classify_funding_rate(current_funding_rate)
        
        return funding_class
    
    def _update_funding_history(self, symbol: str, funding_rate: float, mark_price: float, index_price: float):
        """Update funding rate history"""
        if symbol not in self.funding_history:
            self.funding_history[symbol] = []
        
        self.funding_history[symbol].append({
            "timestamp": datetime.now().isoformat(),
            "funding_rate": funding_rate,
            "mark_price": mark_price,
            "index_price": index_price,
            "premium": (mark_price - index_price) / index_price * 100  # Premium percentage
        })
        
        # Keep only last 100 entries
        if len(self.funding_history[symbol]) > 100:
            self.funding_history[symbol] = self.funding_history[symbol][-100:]
    
    def _classify_funding_rate(self, funding_rate: float) -> Literal["extreme_positive", "positive", "neutral", "negative", "extreme_negative"]:
        """Classify funding rate into categories"""
        
        if funding_rate > self.funding_thresholds["extreme_positive"]:
            return "extreme_positive"
        elif funding_rate > self.funding_thresholds["positive"]:
            return "positive"
        elif funding_rate > self.funding_thresholds["neutral"]:
            return "neutral"
        elif funding_rate > self.funding_thresholds["negative"]:
            return "negative"
        else:
            return "extreme_negative"
    
    def get_funding_trend(self, symbol: str) -> Dict[str, Any]:
        """Analyze funding rate trend"""
        if symbol not in self.funding_history or len(self.funding_history[symbol]) < 10:
            return {"trend": "insufficient_data", "strength": 0.0}
        
        recent_data = self.funding_history[symbol][-20:]
        funding_rates = [d["funding_rate"] for d in recent_data]
        
        # Calculate trend using linear regression
        x = np.arange(len(funding_rates))
        y = np.array(funding_rates)
        
        # Simple trend calculation
        recent_avg = np.mean(funding_rates[-5:])
        older_avg = np.mean(funding_rates[-10:-5]) if len(funding_rates) >= 10 else np.mean(funding_rates[:5])
        
        trend_strength = (recent_avg - older_avg) / max(abs(older_avg), 0.0001)
        
        if trend_strength > 0.1:
            trend = "rising"
        elif trend_strength < -0.1:
            trend = "falling"
        else:
            trend = "stable"
        
        return {
            "trend": trend,
            "strength": abs(trend_strength),
            "current_rate": funding_rates[-1],
            "recent_avg": recent_avg,
            "older_avg": older_avg
        }
    
    def detect_funding_extremes(self, symbol: str) -> Dict[str, Any]:
        """Detect funding rate extremes and potential reversals"""
        if symbol not in self.funding_history or len(self.funding_history[symbol]) < 20:
            return {"extreme": False, "type": None, "reversal_signal": False}
        
        recent_data = self.funding_history[symbol][-20:]
        funding_rates = [d["funding_rate"] for d in recent_data]
        current_rate = funding_rates[-1]
        
        # Check for extreme levels
        is_extreme_positive = current_rate > self.funding_thresholds["extreme_positive"]
        is_extreme_negative = current_rate < self.funding_thresholds["extreme_negative"]
        
        extreme_type = None
        if is_extreme_positive:
            extreme_type = "positive"
        elif is_extreme_negative:
            extreme_type = "negative"
        
        # Check for potential reversal (funding starting to normalize)
        reversal_signal = False
        if extreme_type and len(funding_rates) >= 10:
            # Check if funding has been extreme but is now moving toward neutral
            recent_trend = np.polyfit(range(10), funding_rates[-10:], 1)[0]
            
            if extreme_type == "positive" and recent_trend < -0.0001:
                reversal_signal = True
            elif extreme_type == "negative" and recent_trend > 0.0001:
                reversal_signal = True
        
        return {
            "extreme": extreme_type is not None,
            "type": extreme_type,
            "reversal_signal": reversal_signal,
            "current_rate": current_rate,
            "threshold": self.funding_thresholds.get(f"extreme_{extreme_type}" if extreme_type else "neutral", 0)
        }
    
    def calculate_funding_arbitrage_opportunity(
        self,
        symbol: str,
        mark_price: float,
        index_price: float,
        funding_rate: float
    ) -> Dict[str, Any]:
        """Calculate potential funding arbitrage opportunity"""
        
        # Calculate premium/discount
        premium = (mark_price - index_price) / index_price
        annualized_funding = funding_rate * 3 * 365  # Assuming 3x funding, 365 days
        
        # Estimate carry cost/benefit
        carry_cost = annualized_funding - premium * 100
        
        # Opportunity score (0-100)
        opportunity_score = 0
        if abs(carry_cost) > 5:  # More than 5% annualized
            opportunity_score = min(100, abs(carry_cost) * 10)
        
        # Direction
        if carry_cost > 0:
            direction = "short_perp"  # Cost to hold long, benefit to hold short
        else:
            direction = "long_perp"   # Benefit to hold long, cost to hold short
        
        return {
            "opportunity_score": opportunity_score,
            "direction": direction,
            "carry_cost": carry_cost,
            "annualized_funding": annualized_funding,
            "premium_pct": premium * 100,
            "mark_price": mark_price,
            "index_price": index_price
        }
    
    def get_funding_summary(self, symbol: str) -> Dict[str, Any]:
        """Get comprehensive funding summary"""
        if symbol not in self.funding_history:
            return {"error": "No funding data available"}
        
        data = self.funding_history[symbol]
        if not data:
            return {"error": "No funding data available"}
        
        latest = data[-1]
        current_rate = latest["funding_rate"]
        current_premium = latest["premium"]
        
        # Statistics
        all_rates = [d["funding_rate"] for d in data]
        all_premiums = [d["premium"] for d in data]
        
        return {
            "current_rate": current_rate,
            "current_class": self._classify_funding_rate(current_rate),
            "current_premium": current_premium,
            "avg_rate": np.mean(all_rates),
            "max_rate": max(all_rates),
            "min_rate": min(all_rates),
            "avg_premium": np.mean(all_premiums),
            "trend": self.get_funding_trend(symbol),
            "extremes": self.detect_funding_extremes(symbol),
            "data_points": len(data)
        }
