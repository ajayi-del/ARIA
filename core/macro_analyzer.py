import structlog
from typing import Literal, Dict, Any
from datetime import datetime, timedelta
from intelligence.market_state import MarketState

logger = structlog.get_logger(__name__)


class MacroAnalyzer:
    """Tier 1 - Macro bias analysis"""
    
    def __init__(self):
        self.macro_sources = [
            "economic_calendar",
            "news_sentiment", 
            "institutional_flow",
            "geopolitical_risk",
            "market_breadth"
        ]
        
    def analyze_macro_bias(
        self,
        symbol: str,
        economic_data: Dict[str, Any],
        news_sentiment: Dict[str, float],
        institutional_flow: Dict[str, float],
        geopolitical_risk: float,
        market_breadth: Dict[str, float]
    ) -> tuple[Literal["bullish", "bearish", "neutral"], str, float]:
        """
        Analyze macro bias from multiple sources
        
        Returns: (bias, source, confidence)
        """
        signals = []
        
        # Economic calendar analysis
        econ_bias = self._analyze_economic_data(economic_data)
        if econ_bias:
            signals.append(("economic_calendar", econ_bias[0], econ_bias[1]))
        
        # News sentiment analysis
        news_bias = self._analyze_news_sentiment(news_sentiment)
        if news_bias:
            signals.append(("news_sentiment", news_bias[0], news_bias[1]))
        
        # Institutional flow analysis
        flow_bias = self._analyze_institutional_flow(institutional_flow)
        if flow_bias:
            signals.append(("institutional_flow", flow_bias[0], flow_bias[1]))
        
        # Geopolitical risk
        risk_bias = self._analyze_geopolitical_risk(geopolitical_risk)
        if risk_bias:
            signals.append(("geopolitical_risk", risk_bias[0], risk_bias[1]))
        
        # Market breadth
        breadth_bias = self._analyze_market_breadth(market_breadth)
        if breadth_bias:
            signals.append(("market_breadth", breadth_bias[0], breadth_bias[1]))
        
        # Weight and combine signals
        if not signals:
            return "neutral", "no_data", 0.0
        
        # Find strongest signal
        # signals format: (source_name, bias, confidence)
        # return format expected by caller: (bias, source_name, confidence)
        strongest = max(signals, key=lambda x: x[2])
        return strongest[1], strongest[0], strongest[2]
    
    def _analyze_economic_data(self, data: Dict[str, Any]) -> tuple[Literal["bullish", "bearish", "neutral"], float] | None:
        """Analyze economic calendar data"""
        if not data:
            return None
            
        score = 0.0
        count = 0
        
        # Interest rates
        if "interest_rate_change" in data:
            change = data["interest_rate_change"]
            if change > 0:
                score -= 0.3  # Rate hike = bearish
            elif change < 0:
                score += 0.3  # Rate cut = bullish
            count += 1
        
        # Inflation data
        if "inflation_surprise" in data:
            surprise = data["inflation_surprise"]
            if surprise > 0.5:
                score -= 0.2  # Higher inflation = bearish
            elif surprise < -0.5:
                score += 0.2  # Lower inflation = bullish
            count += 1
        
        # GDP growth
        if "gdp_surprise" in data:
            surprise = data["gdp_surprise"]
            if surprise > 1.0:
                score += 0.3  # Strong GDP = bullish
            elif surprise < -1.0:
                score -= 0.3  # Weak GDP = bearish
            count += 1
        
        if count == 0:
            return None
        
        confidence = min(abs(score), 1.0)
        if score > 0.3:
            return "bullish", confidence
        elif score < -0.3:
            return "bearish", confidence
        else:
            return "neutral", confidence
    
    def _analyze_news_sentiment(self, sentiment: Dict[str, float]) -> tuple[Literal["bullish", "bearish", "neutral"], float] | None:
        """Analyze news sentiment data"""
        if not sentiment:
            return None
            
        total_score = 0.0
        total_weight = 0.0
        
        for source, score in sentiment.items():
            weight = 1.0  # Equal weighting for now
            total_score += score * weight
            total_weight += weight
        
        if total_weight == 0:
            return None
        
        avg_score = total_score / total_weight
        confidence = min(abs(avg_score), 1.0)
        
        if avg_score > 0.2:
            return "bullish", confidence
        elif avg_score < -0.2:
            return "bearish", confidence
        else:
            return "neutral", confidence
    
    def _analyze_institutional_flow(self, flow: Dict[str, float]) -> tuple[Literal["bullish", "bearish", "neutral"], float] | None:
        """Analyze institutional flow data"""
        if not flow:
            return None
            
        net_flow = flow.get("net_flow", 0.0)
        flow_strength = flow.get("strength", 0.0)
        
        confidence = min(abs(flow_strength), 1.0)
        
        if net_flow > 100_000_000:  # $100M+ inflow
            return "bullish", confidence
        elif net_flow < -100_000_000:  # $100M+ outflow
            return "bearish", confidence
        else:
            return "neutral", confidence
    
    def _analyze_geopolitical_risk(self, risk: float) -> tuple[Literal["bullish", "bearish", "neutral"], float] | None:
        """Analyze geopolitical risk (0-1 scale)"""
        if risk is None:
            return None
            
        # High geopolitical risk = bearish for risk assets
        confidence = risk
        
        if risk > 0.7:
            return "bearish", confidence
        elif risk < 0.3:
            return "bullish", confidence
        else:
            return "neutral", confidence
    
    def _analyze_market_breadth(self, breadth: Dict[str, float]) -> tuple[Literal["bullish", "bearish", "neutral"], float] | None:
        """Analyze market breadth indicators"""
        if not breadth:
            return None
            
        advance_decline = breadth.get("advance_decline_ratio", 1.0)
        new_highs_lows = breadth.get("new_highs_lows_ratio", 1.0)
        
        score = 0.0
        count = 0
        
        if advance_decline > 1.5:
            score += 0.4
        elif advance_decline < 0.67:
            score -= 0.4
        count += 1
        
        if new_highs_lows > 2.0:
            score += 0.3
        elif new_highs_lows < 0.5:
            score -= 0.3
        count += 1
        
        if count == 0:
            return None
        
        confidence = min(abs(score), 1.0)
        
        if score > 0.3:
            return "bullish", confidence
        elif score < -0.3:
            return "bearish", confidence
        else:
            return "neutral", confidence
