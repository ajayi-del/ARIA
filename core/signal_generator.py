import structlog
from typing import Dict, Any, List, Optional
import numpy as np
from datetime import datetime
from intelligence.market_state import MarketState
from core.macro_analyzer import MacroAnalyzer
from core.regime_analyzer import RegimeAnalyzer
from core.structure_analyzer import StructureAnalyzer
from core.microstructure_analyzer import MicrostructureAnalyzer
from core.funding_analyzer import FundingAnalyzer
from core.mag_analyzer import MAGAnalyzer
from intelligence.coherence import CoherenceEngine
from intelligence.vpin import VPINCalculator
from data.onchain_reader import OnchainReader

logger = structlog.get_logger(__name__)


class SignalGenerator:
    """Main signal generation engine that combines all analyzers"""
    
    def __init__(self, stop_clusters=None):
        self.macro_analyzer = MacroAnalyzer()
        self.regime_analyzer = RegimeAnalyzer()
        self.structure_analyzer = StructureAnalyzer()
        self.microstructure_analyzer = MicrostructureAnalyzer()
        self.funding_analyzer = FundingAnalyzer()
        self.mag_analyzer = MAGAnalyzer()
        self.coherence_engine = CoherenceEngine(stop_clusters=stop_clusters)
        self.vpin_calculator = VPINCalculator(window_size=50)
        self.onchain_reader = OnchainReader()
        
        self.signal_history: List[MarketState] = []
        
    def generate_market_state(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        ostium_data: Dict[str, Any] = None,
        market_hours_ok: bool = True
    ) -> MarketState:
        """
        Generate complete MarketState by running all analyzers
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
            {symbol: market_data.get("volume_data", [])}  # Convert list to dict for RegimeAnalyzer
        )
        
        # Tier 3 - Structure Analysis
        market_type, atr, atr_vs_baseline = self.structure_analyzer.analyze_structure(
            symbol,
            market_data.get("price_data", []),
            market_data.get("volume_data", []),  # Already a list for this symbol
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
        
        # Tier 5 - Funding & OI Analysis
        funding_class = self.funding_analyzer.analyze_funding(
            symbol,
            market_data.get("funding_rate", 0.0),
            market_data.get("funding_history", []),
            market_data.get("mark_price", 0),
            market_data.get("index_price", 0)
        )
        oi_signal = self.onchain_reader.compute_oi_signal(
            symbol,
            market_data.get("open_interest", 0.0),
            market_data.get("prev_open_interest", 0.0),
            market_data.get("mark_price", 0.0),
            market_data.get("prev_mark_price", 0.0)
        )
        
        # Tier 6 - MAG Analysis
        mag_active, mag_direction, mag_lag_remaining_min = self.mag_analyzer.analyze_mag_signal(
            symbol,
            market_data,
            market_data.get("price_action", {}),
            market_data.get("volume_profile", {})
        )
        
        # --- v1.2 Weighted Scoring ---
        analyzers_output = {
            "sweep": sweep,
            "sweep_price": market_data.get("mark_price", 0),
            "sweep_side": "long_stops" if sweep == "sell_side" else "short_stops" if sweep == "buy_side" else "none",
            "ssi_status": "none", # Placeholder for manual override if needed
            "ostium_oi_lead": ostium_data.get("lead_detected", False) if ostium_data else False,
            "cross_venue_funding": ostium_data.get("cross_funding", "none") if ostium_data else "none",
            "regime": regime,
            "market_type": market_type,
            "funding_class": funding_class,
            "oi_signal": oi_signal.label,
            "vpin": self.vpin_calculator.compute(symbol, market_data.get("trade_data", [])).vpin,
            "vpin_hot": self.vpin_calculator.compute(symbol, market_data.get("trade_data", [])).is_hot
        }
        
        weighted_score, raw_score, components = self.coherence_engine.calculate_weighted_score(
            symbol, analyzers_output
        )
        
        # Determine trade direction
        trade_direction = "none"
        if mag_active:
            if mag_direction == "bullish" and weighted_score >= 4.0:
                trade_direction = "long"
            elif mag_direction == "bearish" and weighted_score >= 4.0:
                trade_direction = "short"
        
        size_multiplier = self.coherence_engine.get_size_multiplier(weighted_score)
        
        # Cluster validation results for logging
        cluster_valid = False
        cluster_strength = 0.0
        if sweep != "none" and self.coherence_engine.stop_clusters:
            cluster_valid, cluster_strength = self.coherence_engine.stop_clusters.validate_sweep(
                symbol, analyzers_output["sweep_price"], analyzers_output["sweep_side"]
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
            sweep_price=analyzers_output["sweep_price"],
            sweep_index=sweep_index,
            cluster_validated=cluster_valid,
            cluster_strength=cluster_strength,
            reclaim=reclaim,
            imbalance=imbalance,
            vpin=analyzers_output["vpin"],
            vpin_hot=analyzers_output["vpin_hot"],
            absorption=absorption,
            divergence_signal=divergence_signal,
            mark_local_spread_pct=mark_local_spread_pct,
            funding_class=funding_class,
            oi_signal=oi_signal.label,
            oi_strength=oi_signal.strength,
            mag_active=mag_active,
            mag_direction=mag_direction,
            mag_lag_remaining_min=mag_lag_remaining_min,
            ostium_lead_active=analyzers_output["ostium_oi_lead"],
            ostium_lead_dir=ostium_data.get("direction", "none") if ostium_data else "none",
            cross_venue_funding=analyzers_output["cross_venue_funding"],
            market_hours_gate=market_hours_ok,
            weighted_score=weighted_score,
            raw_score=raw_score,
            coherence_score=weighted_score, # Mapping directly to weighted float in v1.3
            independence_discount=components.get("independence_discount", 1.0),
            size_multiplier=size_multiplier,
            trade_direction=trade_direction,
            invalidation_reason=None if trade_direction != "none" else "Insufficient weighted coherence or no MAG active"
        )
        
        # Store in history
        self.signal_history.append(market_state)
        if len(self.signal_history) > 1000:
            self.signal_history = self.signal_history[-1000:]
        
        return market_state
    
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
