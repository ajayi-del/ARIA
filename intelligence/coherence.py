import structlog
import numpy as np
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass

logger = structlog.get_logger(__name__)

# v1.3 Tier Correlation Matrix (Approximation of redundancy)
TIER_CORRELATIONS = {
    ("institutional", "regime"): 0.65,
    ("microstructure", "structure"): 0.45,
    ("regime", "structure"): 0.55,
    ("institutional", "oi_momentum"): 0.50,  # OI expansion often confirms institutional flow
    ("regime", "oi_momentum"): 0.35,
}

class CoherenceEngine:
    """
    v1.3 Weighted Coherence Scoring with Independence Discount.
    Protects against double-counting by penalizing overlapping signals.
    """
    
    def __init__(self, stop_clusters=None):
        self.stop_clusters = stop_clusters

    def calculate_weighted_score(
        self,
        symbol: str,
        analyzers_output: Dict[str, Any],
        freshness: float = 1.0
    ) -> Tuple[float, int, Dict[str, float]]:
        """
        Computes weighted score with Tier Independence Discount.
        Returns (weighted_score, raw_score, component_scores)
        """
        components = {}
        raw_score = 0
        
        # --- 1. PREDICTIVE SIGNALS ---
        
        # Tier 4: Microstructure (Sweep + Cluster + VPIN)
        sweep = analyzers_output.get("sweep", "none")
        sweep_price = analyzers_output.get("sweep_price", 0)
        sweep_side = analyzers_output.get("sweep_side", "none")
        
        micro_score = 0.0
        vpin_hot = analyzers_output.get("vpin_hot", False)
        vpin_val = analyzers_output.get("vpin", 0.0)

        if sweep != "none":
            # Sweep alone is worth 0.5 even without cluster validation
            micro_score = 0.5
            if self.stop_clusters:
                cluster_valid, cluster_strength = self.stop_clusters.validate_sweep(
                    symbol, sweep_price, sweep_side
                )
                if cluster_valid:
                    micro_score = 1.0
                    if cluster_strength > 0.8:
                        micro_score += 0.5
            # VPIN amplification: toxic flow confirms the sweep
            if vpin_hot or vpin_val > 0.70:
                micro_score += 0.5
        elif vpin_hot or vpin_val > 0.70:
            # VPIN alone (no sweep): partial micro score — order flow imbalance is informative
            micro_score = 0.5

        components["microstructure"] = micro_score
        if micro_score >= 1.0: raw_score += 1

        # Tier 1: Institutional (SSI/OI Confirmation)
        ssi_status = analyzers_output.get("ssi_status", "neutral")
        oi_label = analyzers_output.get("oi_signal", "NEUTRAL")
        
        ssi_score = 0.0
        if ssi_status == "strong_inflow": ssi_score = 1.0
        elif ssi_status == "inflow": ssi_score = 0.5
        
        # OI Confirmation (v1.3)
        if "EXPANSION" in oi_label:
            ssi_score += 0.5
        
        components["institutional"] = ssi_score
        if ssi_score >= 1.0: raw_score += 1
        
        # Tier 6: OI momentum signal (on-chain, SoDEX-native)
        # Uses oi_signal label from onchain_reader — no external dependency
        oi_label = analyzers_output.get("oi_signal", "NEUTRAL")
        oi_score = 0.0
        if oi_label in ("BULLISH_EXPANSION", "BEARISH_EXPANSION"):
            oi_score = 1.0
        elif oi_label in ("SHORT_COVERING", "LONG_LIQUIDATION"):
            oi_score = 0.5

        components["oi_momentum"] = oi_score
        if oi_score >= 1.0: raw_score += 1

        # --- 2. CLASSIFICATION SIGNALS ---
        
        # Tier 2: Regime
        regime = analyzers_output.get("regime", "neutral")
        regime_score = 0.0
        if regime in ["risk_on", "risk_off"]: regime_score = 0.75
        elif regime == "rotational": regime_score = 0.4
        
        components["regime"] = regime_score
        if regime_score >= 0.75: raw_score += 1
        
        # Tier 3: Structure
        market_type = analyzers_output.get("market_type", "chop")
        struct_score = 0.0
        if market_type in ["trend", "expansion"]: struct_score = 1.0
        elif market_type == "compression": struct_score = 0.5
        
        components["structure"] = struct_score
        if struct_score >= 0.75: raw_score += 1
        
        # Tier 5: Funding
        funding_class = analyzers_output.get("funding_class", "neutral")
        funding_score = 0.0
        if "extreme" in funding_class: funding_score = 0.75
        elif funding_class in ["positive", "negative"]: funding_score = 0.4
        
        components["funding"] = funding_score
        if funding_score >= 0.75: raw_score += 1
        
        # --- 3. INDEPENDENCE DISCOUNT (v1.3) ---
        base_weighted_score = sum(components.values())
        independence_factor = self._calculate_independence_factor(components)
        
        weighted_score = base_weighted_score * independence_factor

        # NOTE: No blanket Tier 4 penalty — on thin/new chains (SoDEX mainnet),
        # VPIN is near-0.5 and sweeps are rare. A hard gate here would make
        # coherence 3.0 unreachable without microstructure. Tier 4 score of 0
        # already reduces total naturally.

        # Apply freshness decay
        if freshness < 1.0:
            weighted_score *= freshness
            
        components["independence_discount"] = independence_factor
            
        return weighted_score, raw_score, components

    def _calculate_independence_factor(self, components: Dict[str, float]) -> float:
        """
        Calculates a multiplier (0.7-1.0) based on signal overlap.
        More redundant signals reduce the independence factor.
        """
        active_tiers = [k for k, v in components.items() if v > 0]
        if len(active_tiers) <= 1:
            return 1.0
            
        total_redundancy = 0.0
        matches = 0
        
        for k1, k2 in TIER_CORRELATIONS:
            if k1 in active_tiers and k2 in active_tiers:
                total_redundancy += TIER_CORRELATIONS[(k1, k2)]
                matches += 1
                
        if matches == 0:
            return 1.0
            
        # Max discount is 30% (factor 0.7)
        avg_redundancy = total_redundancy / matches
        discount = min(0.30, avg_redundancy * 0.4) 
        
        return 1.0 - discount

    def get_size_multiplier(self, weighted_score: float) -> float:
        # Non-zero at 2.0+ so risk_engine never computes 0-notional trade
        if weighted_score < 2.0:  return 0.0   # Too weak — no trade
        if weighted_score < 3.0:  return 0.25  # Minimal position, 1/4 size
        if weighted_score < 4.0:  return 0.5   # Half size
        if weighted_score < 5.0:  return 0.75
        if weighted_score < 6.0:  return 1.0
        if weighted_score < 7.0:  return 1.25
        return 1.5
