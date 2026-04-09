import structlog
import numpy as np
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

logger = structlog.get_logger(__name__)

@dataclass
class RegimeMatrix:
    regime: str
    leading_category: str
    lagging_category: str
    category_scores: Dict[str, float]
    asset_scores: Dict[str, float]

class RelativeStrengthEngine:
    """
    v1.2 Relative Strength & Regime Classifier.
    Handles 7 assets grouped into 5 categories for holistic market regime detection.
    """
    
    def __init__(self, config):
        self.config = config
        self.categories = {
            "large_cap": ["BTC-USD", "ETH-USD"],
            "alt_l1": ["SOL-USD", "AVAX-USD"],
            "defi_infra": ["LINK-USD"],
            "cex_ecosystem": ["BNB-USD"],
            "commodity": ["XAUT-USD"],
            "index": ["USTECH100-USD"]
        }

    def compute_regime(self, candle_buffers: Dict[str, Any]) -> RegimeMatrix:
        """
        Computes the current market regime based on 24h performance across 6 categories.
        Expects candle_buffers providing 24h performance scores.
        """
        asset_scores = {}
        for asset in self.config.assets:
            # Calculate 24h return (current close vs 24h ago close)
            # Defaulting to 0 if not enough data
            score = 0.0
            if asset in candle_buffers:
                buf = candle_buffers[asset].get("15m") # Use 15m for 24h lookback
                if buf and len(buf.candles) >= 96: # 96 * 15m = 24h
                    current = buf.candles[-1].close
                    past = buf.candles[-96].close
                    score = (current - past) / past
            asset_scores[asset] = score

        # Group and average categorical scores
        cat_scores = {}
        for cat, assets in self.categories.items():
            valid_scores = [asset_scores[a] for a in assets if a in asset_scores]
            cat_scores[cat] = np.mean(valid_scores) if valid_scores else 0.0

        large_cap_avg = cat_scores.get("large_cap", 0.0)
        alt_l1_avg = cat_scores.get("alt_l1", 0.0)
        defi_avg = cat_scores.get("defi_infra", 0.0)
        cex_avg = cat_scores.get("cex_ecosystem", 0.0)
        commodity_avg = cat_scores.get("commodity", 0.0)
        ustech_perf = cat_scores.get("index", 0.0)

        # Extended Regime Logic
        regime = "confused"
        if (ustech_perf > large_cap_avg * 1.5 and ustech_perf > 0.005):
            regime = "tech_led"
        elif commodity_avg < 0.1 and large_cap_avg > 0.03: # Adjusted 0.3 to 0.03 for more sensitive 24h returns
            regime = "risk_on"
        elif commodity_avg > 0.03 and large_cap_avg < 0:
            regime = "risk_off"
        elif large_cap_avg > 0 and alt_l1_avg > large_cap_avg * 1.5:
            regime = "alt_season"
        elif large_cap_avg > 0 and alt_l1_avg < large_cap_avg * 0.5:
            regime = "btc_dominance"
        elif defi_avg < large_cap_avg * 0.5 and large_cap_avg > 0:
            regime = "defi_stress"
        elif cex_avg > large_cap_avg * 1.3 and large_cap_avg > 0:
            regime = "cex_flow"

        # Find leading and lagging categories
        leading = max(cat_scores, key=cat_scores.get)
        lagging = min(cat_scores, key=cat_scores.get)

        matrix = RegimeMatrix(
            regime=regime,
            leading_category=leading,
            lagging_category=lagging,
            category_scores=cat_scores,
            asset_scores=asset_scores
        )
        
        logger.info("regime_calculated", regime=regime, leading=leading, lagging=lagging)
        return matrix
