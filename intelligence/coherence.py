import structlog
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass

logger = structlog.get_logger(__name__)

class CoherenceEngine:
    """
    v1.2 Weighted Coherence Scoring.
    Distinguishes between Predictive (High weight) and Classification (Low weight) signals.
    """
    
    def __init__(self, stop_clusters=None):
        self.stop_clusters = stop_clusters

    def calculate_weighted_score(
        self,
        symbol: str,
        analyzers_output: Dict[str, Any]
    ) -> Tuple[float, int, Dict[str, float]]:
        """
        Computes both weighted_score (float) and raw_score (int).
        Returns (weighted_score, raw_score, component_scores)
        """
        components = {}
        raw_score = 0
        
        # --- 1. PREDICTIVE SIGNALS (Max: 4.0+) ---
        
        # Tier 4: Microstructure (Sweep + Cluster)
        sweep = analyzers_output.get("sweep", "none")
        sweep_price = analyzers_output.get("sweep_price", 0)
        sweep_side = analyzers_output.get("sweep_side", "none") # "long_stops" or "short_stops"
        
        micro_score = 0.0
        cluster_valid = False
        cluster_strength = 0.0
        
        if sweep != "none" and self.stop_clusters:
            # Validate against clusters
            cluster_valid, cluster_strength = self.stop_clusters.validate_sweep(
                symbol, sweep_price, sweep_side
            )
            
            if not cluster_valid:
                logger.info("sweep_rejected_no_cluster", symbol=symbol, price=sweep_price)
                sweep = "none" # Reject sweep
            else:
                micro_score = 1.0 # Base gate
                if cluster_strength > 0.8:
                    micro_score += 0.5
                    logger.info("strong_cluster_sweep", symbol=symbol, strength=cluster_strength)
                
                vpin = analyzers_output.get("vpin", 0.0)
                if vpin > 0.75:
                    micro_score += 0.5
                    
        components["microstructure"] = micro_score
        if micro_score >= 1.0: raw_score += 1

        # Tier 1: Institutional (SSI Inflows)
        ssi_status = analyzers_output.get("ssi_status", "neutral")
        ssi_score = 0.0
        if ssi_status == "strong_inflow": ssi_score = 1.5
        elif ssi_status == "inflow": ssi_score = 1.0
        elif ssi_status == "opposing": ssi_score = -0.5
        
        components["institutional"] = ssi_score
        if ssi_score >= 1.0: raw_score += 1
        
        # Tier 6: Ostium Lead (XAUT only)
        ostium_score = 0.0
        if symbol == "XAUT":
            ostium_lead = analyzers_output.get("ostium_oi_lead", False)
            if ostium_lead:
                ostium_score = 1.0
                cross_funding = analyzers_output.get("cross_venue_funding", "none")
                if "double_extreme" in cross_funding:
                    ostium_score += 0.5
            
        components["cross_venue"] = ostium_score
        if ostium_score >= 1.0: raw_score += 1

        # --- 2. CLASSIFICATION SIGNALS (Max: 2.25) ---
        
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
        if market_type in ["trend", "expansion"]: struct_score = 0.75
        elif market_type == "compression": struct_score = 0.4
        
        components["structure"] = struct_score
        if struct_score >= 0.75: raw_score += 1
        
        # Tier 5: Funding
        funding_class = analyzers_output.get("funding_class", "neutral")
        funding_score = 0.0
        if "extreme" in funding_class: funding_score = 0.75
        elif funding_class in ["positive", "negative"]: funding_score = 0.4
        
        components["funding"] = funding_score
        if funding_score >= 0.75: raw_score += 1
        
        weighted_score = sum(components.values())
        return weighted_score, raw_score, components

    def get_size_multiplier(self, weighted_score: float) -> float:
        """
        New score-to-size map:
        < 4.0: 0x
        4.0-4.9: 0.5x
        5.0-5.9: 0.75x
        6.0-6.9: 1.0x
        7.0+: 1.5x
        """
        if weighted_score < 4.0: return 0.0
        if weighted_score < 5.0: return 0.5
        if weighted_score < 6.0: return 0.75
        if weighted_score < 7.0: return 1.0
        return 1.5
