import structlog
from typing import Dict, Any, List, Optional
import numpy as np
from datetime import datetime
from core.market_state import MarketState
from core.macro_analyzer import MacroAnalyzer
from core.regime_analyzer import RegimeAnalyzer
from core.structure_analyzer import StructureAnalyzer
from core.microstructure_analyzer import MicrostructureAnalyzer
from core.funding_analyzer import FundingAnalyzer
from core.mag_analyzer import MAGAnalyzer

logger = structlog.get_logger(__name__)


class SignalGenerator:
    """Main signal generation engine that combines all analyzers"""
    
    def __init__(self):
        self.macro_analyzer = MacroAnalyzer()
        self.regime_analyzer = RegimeAnalyzer()
        self.structure_analyzer = StructureAnalyzer()
        self.microstructure_analyzer = MicrostructureAnalyzer()
        self.funding_analyzer = FundingAnalyzer()
        self.mag_analyzer = MAGAnalyzer()
        
        self.signal_history: List[MarketState] = []
        
    def generate_market_state(
        self,
        symbol: str,
        market_data: Dict[str, Any]
    ) -> MarketState:
        """
        Generate complete MarketState by running all analyzers
        
        Args:
            symbol: Trading symbol
            market_data: Dictionary containing all market data
            
        Returns:
            MarketState object with complete analysis
        """
        timestamp_ms = int(datetime.now().timestamp() * 1000)
        
        # Tier 1 - Macro Analysis
        macro_bias, macro_source, macro_confidence = self.macro_analyzer.analyze_macro_bias(
            symbol,
            market_data.get("economic_data", {}),
            market_data.get("news_sentiment", {}),
            market_data.get("institutional_flow", {}),
            market_data.get("geopolitical_risk", 0.0),
            market_data.get("market_breadth", {})
        )
        
        # Tier 2 - Regime Analysis
        regime, leading_asset, lagging_asset = self.regime_analyzer.analyze_regime(
            symbol,
            market_data.get("asset_returns", {}),
            market_data.get("volatility_data", {}),
            market_data.get("volume_data", {})
        )
        
        # Tier 3 - Structure Analysis
        market_type, atr, atr_vs_baseline = self.structure_analyzer.analyze_structure(
            symbol,
            market_data.get("price_data", []),
            market_data.get("volume_data", {}).get(symbol, []),
            market_data.get("high_data", []),
            market_data.get("low_data", [])
        )
        
        # Tier 4 - Microstructure Analysis
        (sweep, sweep_index, reclaim, imbalance, absorption, 
         divergence_signal, mark_local_spread_pct) = self.microstructure_analyzer.analyze_microstructure(
            symbol,
            market_data.get("orderbook_data", {}),
            market_data.get("trade_data", []),
            market_data.get("mark_price", 0)
        )
        
        # Tier 5 - Funding Analysis
        funding_class = self.funding_analyzer.analyze_funding(
            symbol,
            market_data.get("funding_rate", 0.0),
            market_data.get("funding_history", []),
            market_data.get("mark_price", 0),
            market_data.get("index_price", 0)
        )
        
        # Tier 6 - MAG Analysis
        mag_active, mag_direction, mag_lag_remaining_min = self.mag_analyzer.analyze_mag_signal(
            symbol,
            market_data,  # Pass full market data for MAG analysis
            market_data.get("price_action", {}),
            market_data.get("volume_profile", {})
        )
        
        # Calculate final coherence score and trade direction
        coherence_score, size_multiplier, trade_direction, invalidation_reason = self._calculate_final_signal(
            macro_bias,
            macro_confidence,
            regime,
            market_type,
            sweep,
            mag_active,
            mag_direction,
            funding_class,
            divergence_signal
        )
        
        # Create MarketState object
        market_state = MarketState(
            symbol=symbol,
            timestamp_ms=timestamp_ms,
            macro_bias=macro_bias,
            macro_source=macro_source,
            macro_confidence=macro_confidence,
            regime=regime,
            leading_asset=leading_asset,
            lagging_asset=lagging_asset,
            market_type=market_type,
            atr=atr,
            atr_vs_baseline=atr_vs_baseline,
            sweep=sweep,
            sweep_index=sweep_index,
            reclaim=reclaim,
            imbalance=imbalance,
            absorption=absorption,
            divergence_signal=divergence_signal,
            mark_local_spread_pct=mark_local_spread_pct,
            funding_class=funding_class,
            mag_active=mag_active,
            mag_direction=mag_direction,
            mag_lag_remaining_min=mag_lag_remaining_min,
            coherence_score=coherence_score,
            size_multiplier=size_multiplier,
            trade_direction=trade_direction,
            invalidation_reason=invalidation_reason
        )
        
        # Store in history
        self.signal_history.append(market_state)
        if len(self.signal_history) > 1000:
            self.signal_history = self.signal_history[-1000:]
        
        return market_state
    
    def _calculate_final_signal(
        self,
        macro_bias: str,
        macro_confidence: float,
        regime: str,
        market_type: str,
        sweep: str,
        mag_active: bool,
        mag_direction: str,
        funding_class: str,
        divergence_signal: str
    ) -> tuple[int, float, str, Optional[str]]:
        """
        Calculate final coherence score, size multiplier, and trade direction
        
        Returns: (coherence_score, size_multiplier, trade_direction, invalidation_reason)
        """
        
        coherence_score = 0
        invalidation_reason = None
        trade_direction = "none"
        
        # Coherence scoring - each tier contributes max 1 point
        
        # Tier 1 - Macro (1 point)
        if macro_bias != "neutral" and macro_confidence > 0.6:
            coherence_score += 1
        
        # Tier 2 - Regime (1 point)
        if regime in ["risk_on", "risk_off"]:
            coherence_score += 1
        elif regime == "rotational":
            coherence_score += 0.5
        
        # Tier 3 - Structure (1 point)
        if market_type in ["trend", "expansion"]:
            coherence_score += 1
        elif market_type == "compression":
            coherence_score += 0.5
        
        # Tier 4 - Microstructure (1 point)
        if sweep != "none":
            coherence_score += 1
        elif divergence_signal != "none":
            coherence_score += 0.5
        
        # Tier 5 - Funding (1 point)
        if funding_class in ["positive", "negative"]:
            coherence_score += 0.5
        elif funding_class in ["extreme_positive", "extreme_negative"]:
            coherence_score += 1
        
        # Tier 6 - MAG (1 point)
        if mag_active:
            coherence_score += 1
        
        # Determine trade direction
        bullish_signals = 0
        bearish_signals = 0
        
        # Count directional signals
        if macro_bias == "bullish":
            bullish_signals += 1
        elif macro_bias == "bearish":
            bearish_signals += 1
        
        if regime == "risk_on":
            bullish_signals += 1
        elif regime == "risk_off":
            bearish_signals += 1
        
        if sweep == "buy_side":
            bullish_signals += 1
        elif sweep == "sell_side":
            bearish_signals += 1
        
        if divergence_signal == "bullish_reversion":
            bullish_signals += 1
        elif divergence_signal == "bearish_reversion":
            bearish_signals += 1
        
        if funding_class in ["positive", "extreme_positive"]:
            bullish_signals += 1
        elif funding_class in ["negative", "extreme_negative"]:
            bearish_signals += 1
        
        if mag_direction == "bullish":
            bullish_signals += 1
        elif mag_direction == "bearish":
            bearish_signals += 1
        
        # Final direction decision
        if bullish_signals > bearish_signals and coherence_score >= 4:
            trade_direction = "long"
        elif bearish_signals > bullish_signals and coherence_score >= 4:
            trade_direction = "short"
        else:
            trade_direction = "none"
            if coherence_score < 4:
                invalidation_reason = f"Low coherence score: {coherence_score}/6"
            else:
                invalidation_reason = "Conflicting directional signals"
        
        # Calculate size multiplier based on coherence and confidence
        base_multiplier = coherence_score / 6.0
        confidence_adjustment = macro_confidence * 0.2
        
        size_multiplier = min(1.5, base_multiplier + confidence_adjustment)
        
        # Apply size reduction for conflicting signals
        if abs(bullish_signals - bearish_signals) <= 1:
            size_multiplier *= 0.7
        
        return coherence_score, size_multiplier, trade_direction, invalidation_reason
    
    def get_signal_summary(self, symbol: str = None) -> Dict[str, Any]:
        """Get summary of recent signals"""
        recent_signals = self.signal_history[-20:] if symbol is None else [
            s for s in self.signal_history if s.symbol == symbol
        ][-20:]
        
        if not recent_signals:
            return {"message": "No signals available"}
        
        # Calculate statistics
        total_signals = len(recent_signals)
        long_signals = sum(1 for s in recent_signals if s.trade_direction == "long")
        short_signals = sum(1 for s in recent_signals if s.trade_direction == "short")
        none_signals = sum(1 for s in recent_signals if s.trade_direction == "none")
        
        avg_coherence = np.mean([s.coherence_score for s in recent_signals])
        avg_size_multiplier = np.mean([s.size_multiplier for s in recent_signals])
        
        # Most recent signal
        latest_signal = recent_signals[-1]
        
        return {
            "total_signals": total_signals,
            "long_signals": long_signals,
            "short_signals": short_signals,
            "none_signals": none_signals,
            "long_signal_pct": (long_signals / total_signals) * 100 if total_signals > 0 else 0,
            "short_signal_pct": (short_signals / total_signals) * 100 if total_signals > 0 else 0,
            "avg_coherence": avg_coherence,
            "avg_size_multiplier": avg_size_multiplier,
            "latest_signal": {
                "symbol": latest_signal.symbol,
                "direction": latest_signal.trade_direction,
                "coherence": latest_signal.coherence_score,
                "size_multiplier": latest_signal.size_multiplier,
                "timestamp": latest_signal.timestamp_ms
            }
        }
    
    def get_performance_metrics(self) -> Dict[str, Any]:
        """Get performance metrics for the signal generator"""
        if not self.signal_history:
            return {"message": "No signal history available"}
        
        # Signal quality metrics
        valid_signals = [s for s in self.signal_history if s.is_valid_signal()]
        
        return {
            "total_signals_generated": len(self.signal_history),
            "valid_signals": len(valid_signals),
            "signal_validity_rate": len(valid_signals) / len(self.signal_history) * 100,
            "avg_coherence_score": np.mean([s.coherence_score for s in self.signal_history]),
            "avg_size_multiplier": np.mean([s.size_multiplier for s in self.signal_history]),
            "signal_distribution": {
                "long": sum(1 for s in self.signal_history if s.trade_direction == "long"),
                "short": sum(1 for s in self.signal_history if s.trade_direction == "short"),
                "none": sum(1 for s in self.signal_history if s.trade_direction == "none")
            }
        }
