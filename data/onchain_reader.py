"""
On-chain / Funding Data Reader
v1.3 Hardened: Price-confirming Open Interest (OI) signals.
"""

import structlog
from typing import Dict, Any, Optional
from dataclasses import dataclass

logger = structlog.get_logger(__name__)

@dataclass
class OISignal:
    symbol: str
    oi_change_pct: float
    price_change_pct: float
    label: str  # e.g., "BULLISH_EXPANSION", "BEARISH_EXPANSION", etc.
    strength: float

class OnchainReader:
    """
    Handles higher-latency on-chain data like Open Interest (OI).
    Injects price-directional confirmation into OI signals.
    """
    
    def compute_oi_signal(
        self,
        symbol: str,
        current_oi: float,
        previous_oi: float,
        current_price: float,
        previous_price: float
    ) -> OISignal:
        """
        Calculates OI signal with price confirmation.
        Ensures OI expansion is correctly attributed to long/short builds.
        """
        if previous_oi == 0 or previous_price == 0:
            return OISignal(symbol, 0.0, 0.0, "NEUTRAL", 0.0)
            
        oi_change = (current_oi - previous_oi) / previous_oi
        price_change = (current_price - previous_price) / previous_price
        
        # Thresholds for "Significant" change
        OI_THRESHOLD = 0.005  # 0.5% OI change
        
        label = "NEUTRAL"
        strength = 0.0
        
        if abs(oi_change) >= OI_THRESHOLD:
            if oi_change > 0:
                if price_change > 0:
                    label = "BULLISH_EXPANSION" # Longs entering
                else:
                    label = "BEARISH_EXPANSION" # Shorts entering
            else:
                if price_change > 0:
                    label = "SHORT_COVERING"    # Shorts exiting
                else:
                    label = "LONG_LIQUIDATION"  # Longs exiting
            
            # Strength is a function of both OI intensity and Price confirmation
            strength = min(1.0, abs(oi_change) * 10.0)
            
        return OISignal(
            symbol=symbol,
            oi_change_pct=oi_change,
            price_change_pct=price_change,
            label=label,
            strength=strength
        )
