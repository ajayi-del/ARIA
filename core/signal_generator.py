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
        self._tier_weight_overrides: Dict[str, float] = {}
        self._last_components: Dict[str, Dict[str, float]] = {}

    def set_tier_weight_overrides(self, weights: Dict[str, float]) -> None:
        """Called by feedback engine each cycle to update adaptive tier weights."""
        self._tier_weight_overrides = weights

    def generate_market_state(
        self,
        symbol: str,
        market_data: Dict[str, Any],
        market_hours_ok: bool = True
    ) -> MarketState:
        """
        Generate complete MarketState by running all analyzers
        """
        timestamp_ms = int(datetime.now().timestamp() * 1000)

        # ── Tier 1: Macro Analysis ──
        macro_bias, macro_source, macro_confidence = self.macro_analyzer.analyze_macro_bias(
            symbol,
            market_data.get("economic_data", {}),
            market_data.get("news_sentiment", {}),
            market_data.get("institutional_flow", {}),
            market_data.get("geopolitical_risk", 0.0),
            market_data.get("market_breadth", {})
        )
        # Fallback: derive macro from candle price momentum when no external data
        if macro_source == "no_data":
            momentum = market_data.get("_momentum_pct", 0.0)
            if momentum > 0.001:          # Up 0.1%+ → bullish bias (lowered from 0.2%)
                macro_bias, macro_source, macro_confidence = "bullish", "price_momentum", 0.4
            elif momentum < -0.001:       # Down 0.1%+ → bearish bias (lowered from 0.2%)
                macro_bias, macro_source, macro_confidence = "bearish", "price_momentum", 0.4

        # ── Tier 2: Regime Analysis ──
        regime, leading_asset, lagging_asset = self.regime_analyzer.analyze_regime(
            symbol,
            market_data.get("asset_returns", {}),
            market_data.get("volatility_data", {}),
            {symbol: market_data.get("volume_data", [])}
        )
        # Fallback: single-asset regime from momentum when correlations are empty
        if regime == "rotational":
            momentum = market_data.get("_momentum_pct", 0.0)
            if momentum > 0.0015:         # Sustained upward move → risk_on (lowered from 0.3%)
                regime = "risk_on"
                leading_asset = symbol
                lagging_asset = symbol
            elif momentum < -0.0015:      # Sustained downward move → risk_off (lowered from 0.3%)
                regime = "risk_off"
                leading_asset = symbol
                lagging_asset = symbol

        # ── Tier 3: Structure Analysis ──
        # Use pre-computed Tier 3 from interpreter (50 candles, warmed ATR baseline)
        # when available — avoids re-computing on 20 candles with a fresh StructureAnalyzer.
        if market_data.get("_t3_atr"):
            market_type = market_data["_t3_market_type"]
            atr = market_data["_t3_atr"]
            atr_vs_baseline = market_data["_t3_atr_vs_baseline"]
        else:
            market_type, atr, atr_vs_baseline = self.structure_analyzer.analyze_structure(
                symbol,
                market_data.get("price_data", []),
                market_data.get("volume_data", []),
                market_data.get("high_data", []),
                market_data.get("low_data", [])
            )

        # ── Tier 4: Microstructure Analysis ──
        # Use pre-computed Tier 4 from interpreter (swing-based sweep + live VPIN)
        # when available — avoids calling analyze_microstructure() which uses the old
        # _detect_sweep() method (trade-data patterns) instead of the fixed candle-based one.
        if "_t4_sweep" in market_data:
            sweep = market_data["_t4_sweep"]
            sweep_index = market_data.get("_t4_sweep_index", 0)
            imbalance = market_data.get("_t4_imbalance", 0.0)
            absorption = market_data.get("_t4_absorption", False)
            divergence_signal = market_data.get("_t4_divergence", "none")
            mark_local_spread_pct = 0.0
            reclaim = False
        else:
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
        
        # Derive SSI status from OI signal rather than leaving it hardcoded "none"
        # OI expansion + price direction = institutional flow confirmation
        _oi_label = oi_signal.label
        if _oi_label == "BULLISH_EXPANSION":
            ssi_status = "strong_inflow"
        elif _oi_label == "SHORT_COVERING":
            ssi_status = "inflow"
        else:
            ssi_status = "none"

        # VPIN: use injected value from interpreter (computed on raw trade objects)
        # when available — data_processor trade_data is list-of-dicts, not Trade objects
        _injected_vpin = market_data.get("_t4_vpin")
        if _injected_vpin is not None:
            class _VPINResult:
                vpin = _injected_vpin
                is_hot = _injected_vpin >= 0.60  # v2: threshold 0.70→0.60 (Bybit calibration)
            _vpin_result = _VPINResult()
        else:
            _vpin_result = self.vpin_calculator.compute(symbol, market_data.get("trade_data", []))

        # --- v2 Weighted Scoring — Bybit intelligence wired ─────────────────────
        volume_surge     = market_data.get("_t3_volume_surge", 1.0)
        candle_conviction = market_data.get("_t3_candle_conviction", 0.0)

        analyzers_output = {
            "sweep": sweep,
            "sweep_price": market_data.get("mark_price", 0),
            "sweep_side": "long_stops" if sweep == "sell_side" else "short_stops" if sweep == "buy_side" else "none",
            "ssi_status": ssi_status,
            "regime": regime,
            "market_type": market_type,
            "funding_class": funding_class,
            "oi_signal": _oi_label,
            "vpin": _vpin_result.vpin,
            "vpin_hot": _vpin_result.is_hot,
            # v2: volume/conviction/imbalance for richer microstructure scoring
            "volume_surge": volume_surge,
            "candle_conviction": candle_conviction,
            "imbalance": imbalance,
            # Tier 6: liquidation engine score (injected by interpreter from on-chain events)
            # BUG FIX: was missing from analyzers_output so coherence engine always got 0.0
            "tier6_liq_score": market_data.get("tier6_liq_score", 0.0),
        }
        
        weighted_score, raw_score, components = self.coherence_engine.calculate_weighted_score(
            symbol,
            analyzers_output,
            tier_weight_overrides=self._tier_weight_overrides or None,
        )
        # Cache tier scores for feedback engine (keyed by symbol)
        self._last_components[symbol] = {
            k: v for k, v in components.items() if k != "independence_discount"
        }
        
        # Determine trade direction — multi-tier fallback chain
        trade_direction = "none"

        # Primary: MAG lead signal (when active with sufficient score)
        if mag_active and weighted_score >= 3.0:
            if mag_direction == "bullish":
                trade_direction = "long"
            elif mag_direction == "bearish":
                trade_direction = "short"

        # Fallback 1: Liquidity sweep — micro signal, no macro gate.
        # A sweep IS the reversal signal. Macro/regime adjustment happens in the
        # Enhancement Layer (HTF size penalty). Gating sweeps by macro permanently
        # blocks counter-trend longs in any sustained downtrend.
        if trade_direction == "none" and sweep != "none":
            if sweep == "buy_side":    # buyers absorbed sell-side liquidity → bullish
                trade_direction = "long"
            elif sweep == "sell_side": # sellers absorbed buy-side liquidity → bearish
                trade_direction = "short"

        # Fallback 2: Pure macro + regime alignment with active structure
        if trade_direction == "none" and market_type in ("trend", "expansion") and weighted_score >= 1.0:
            if macro_bias == "bullish" and regime == "risk_on":
                trade_direction = "long"
            elif macro_bias == "bearish" and regime == "risk_off":
                trade_direction = "short"
            # OI expansion as tie-breaker when macro is neutral
            elif _oi_label == "BULLISH_EXPANSION" and regime == "risk_on":
                trade_direction = "long"
            elif _oi_label in ("BEARISH_EXPANSION", "LONG_LIQUIDATION") and regime == "risk_off":
                trade_direction = "short"

        # Fallback 3: Score-driven direction for ambiguous/rotational regimes.
        # Fires when score >= 3.0 and direction is still undetermined — regime constraint
        # relaxed because a 3+ score means multiple independent tiers agree.
        # Uses macro_bias as primary then OI label as secondary.
        if trade_direction == "none" and weighted_score >= 3.0:
            if macro_bias in ("bullish", "very_bullish"):
                trade_direction = "long"
            elif macro_bias in ("bearish", "very_bearish"):
                trade_direction = "short"
            elif _oi_label == "BULLISH_EXPANSION":
                trade_direction = "long"
            elif _oi_label in ("BEARISH_EXPANSION", "LONG_LIQUIDATION"):
                trade_direction = "short"

        # Fallback 4: OB imbalance tiebreaker for high-score signals where macro/OI are
        # both silent (common in rotational/confused regimes). Requires score >= 3.5 to
        # limit to genuinely strong setups. Imbalance >= ±0.25 = clear bid/ask skew.
        if trade_direction == "none" and weighted_score >= 3.5:
            if imbalance >= 0.25:
                trade_direction = "long"
            elif imbalance <= -0.25:
                trade_direction = "short"

        # Fallback 5: Regime-structural direction for neutral macro + meaningful score.
        # When macro_bias="neutral" but regime is clearly directional (risk_on / risk_off),
        # the regime reading IS the direction signal — market structure dominates.
        # Threshold lowered from 3.5 → 3.0: SoDEX thin market means scores rarely exceed
        # 3.5 without external catalyst. 3.0+ with clear regime is actionable.
        # Weekend conv_mult=1.4 at score≥3.0 → $280×0.75=$210 ≥ $200 floor (passes).
        if trade_direction == "none" and weighted_score >= 3.0:
            if regime == "risk_on":
                trade_direction = "long"
            elif regime == "risk_off":
                trade_direction = "short"
            elif weighted_score >= 4.0:
                # High score with rotational/confused regime: use OB imbalance at lower bar
                if imbalance >= 0.15:
                    trade_direction = "long"
                elif imbalance <= -0.15:
                    trade_direction = "short"

        # Fallback 6 — Funding-rate tiebreaker for rotational/neutral markets.
        # Extreme crowding is its own signal: fade it (applies even if F5 didn't fire).
        if trade_direction == "none" and weighted_score >= 3.0:
            if regime in ("rotational", "confused") and macro_bias == "neutral":
                if "extreme_positive" in funding_class:
                    trade_direction = "short"
                elif "extreme_negative" in funding_class:
                    trade_direction = "long"

        # ── Conviction Accelerators — funding + liquidation as post-direction boosters ──
        # Funding rates and liquidation events are ACCELERATORS: they confirm and amplify
        # an already-decided trade direction, never penalise or block.
        # Architecture:  direction decided by regime/structure/micro → accelerators boost size.
        # This matches the user's intent: "they are like tools for AI agents" = additive signal.
        #
        # Funding accelerator: when Bybit funding aligns with direction
        #   short + positive funding (longs overpaying) → crowd validation → +boost
        #   long  + negative funding (shorts overpaying) → crowd validation → +boost
        # Liquidation accelerator: on-chain liq events (from interpreter's liq_engine)
        #   cascade in direction of trade → momentum confirmation → +boost
        _conviction_boost = 0.0
        if trade_direction in ("long", "short"):
            _funding_aligns = (
                (trade_direction == "short" and "positive" in funding_class) or
                (trade_direction == "long"  and "negative" in funding_class)
            )
            if _funding_aligns:
                # Aligned funding: extreme=+15%, moderate=+7%
                _conviction_boost += 0.15 if "extreme" in funding_class else 0.07

            # Liquidation accelerator: tier6_liq_score in analyzers_output
            _t6 = float(analyzers_output.get("tier6_liq_score", 0.0))
            if _t6 >= 0.75:
                # Proportional boost: max score (1.5) → +10%
                _conviction_boost += min(_t6 / 1.5 * 0.10, 0.10)

        # Cap total accelerator at +25% to prevent runaway sizing
        _conviction_boost = min(_conviction_boost, 0.25)

        size_multiplier = self.coherence_engine.get_size_multiplier(weighted_score)
        # Apply conviction accelerator (funding + liq alignment boost) — additive only
        if _conviction_boost > 0:
            size_multiplier = min(size_multiplier * (1.0 + _conviction_boost), 1.5)

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
            market_hours_gate=market_hours_ok,
            weighted_score=weighted_score,
            raw_score=raw_score,
            coherence_score=weighted_score, # Mapping directly to weighted float in v1.3
            independence_discount=components.get("independence_discount", 1.0),
            mark_price=market_data.get("mark_price", 0.0),
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
