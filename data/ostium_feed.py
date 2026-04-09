import httpx
import time
import asyncio
import structlog
from dataclasses import dataclass
from typing import Dict, Optional, Any

logger = structlog.get_logger(__name__)

@dataclass
class OstiumSnapshot:
    gold_oi_usd: float
    gold_oi_change_1h: float  # percentage
    gold_long_pct: float
    gold_short_pct: float
    funding_rate: float
    timestamp_ms: int

class OstiumFeed:
    """
    Polls Ostium Gold Open Interest and Funding data via DefiLlama API.
    Used as an institutional lead signal for XAUT.
    """
    
    def __init__(self):
        self.url = "https://api.llama.fi/protocol/ostium"
        self.snapshot: Optional[OstiumSnapshot] = None
        self._last_update_ms: int = 0
        
    async def update(self) -> Optional[OstiumSnapshot]:
        """Polls API and updates internal snapshot. Never crashes on error."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(self.url)
                if response.status_code != 200:
                    logger.warning("ostium_api_error", status=response.status_code)
                    return self.snapshot
                
                data = response.json()
                
                # DefiLlama structure parsing (hypothetical mapping based on request)
                # In real scenario, we'd extract specific XAUT/Gold fields
                # Here we simulate data extraction from the protocol summary
                
                # Mock extraction logic based on the user's requirements
                current_oi = data.get("currentChainTvls", {}).get("Arbitrum", 0) # Use chain TVL as OI proxy if needed
                prev_oi = current_oi * 0.9 # Placeholder for change calculation
                oi_change = ((current_oi - prev_oi) / prev_oi * 100) if prev_oi != 0 else 0
                
                self.snapshot = OstiumSnapshot(
                    gold_oi_usd=current_oi,
                    gold_oi_change_1h=oi_change,
                    gold_long_pct=65.0, # Placeholder
                    gold_short_pct=35.0, # Placeholder
                    funding_rate=0.012, # Placeholder
                    timestamp_ms=int(time.time() * 1000)
                )
                self._last_update_ms = self.snapshot.timestamp_ms
                return self.snapshot
                
        except Exception as e:
            logger.error("ostium_feed_failure", error=str(e))
            return self.snapshot

    def compute_lead_signal(
        self,
        snapshot: OstiumSnapshot,
        sodex_xaut_oi: float,
        sodex_xaut_oi_prev: float
    ) -> Dict[str, Any]:
        """
        Detects if Ostium is leading a move not yet seen on SoDEX.
        """
        sodex_change = ((sodex_xaut_oi - sodex_xaut_oi_prev) / sodex_xaut_oi_prev * 100) if sodex_xaut_oi_prev != 0 else 0
        
        # Lead condition: Ostium moving > 20% while SoDEX < 5%
        lead_detected = (snapshot.gold_oi_change_1h > 20.0) and (abs(sodex_change) < 5.0)
        
        direction = "none"
        if snapshot.gold_long_pct > 60:
            direction = "bullish"
        elif snapshot.gold_long_pct < 40:
            direction = "bearish"
            
        strength = min(1.0, snapshot.gold_oi_change_1h / 50.0)
        
        return {
            "lead_detected": lead_detected,
            "direction": direction,
            "strength": strength,
            "lag_estimate_min": 15 if lead_detected else 0
        }

    def cross_venue_funding_signal(
        self,
        ostium_rate: float,
        sodex_xaut_rate: float
    ) -> str:
        """
        Divergence or convergence check for extreme funding.
        Rates are expected in decimal (e.g. 0.0001)
        """
        extreme_threshold = 0.0003 # 0.03%
        
        ostium_extreme_pos = ostium_rate > extreme_threshold
        sodex_extreme_pos = sodex_xaut_rate > extreme_threshold
        
        ostium_extreme_neg = ostium_rate < -extreme_threshold
        sodex_extreme_neg = sodex_xaut_rate < -extreme_threshold
        
        if ostium_extreme_pos and sodex_extreme_pos:
            return "double_extreme_short"
        if ostium_extreme_neg and sodex_extreme_neg:
            return "double_extreme_long"
            
        # Check for divergence (opposite signed extremes)
        if (ostium_extreme_pos and sodex_extreme_neg) or (ostium_extreme_neg and sodex_extreme_pos):
            return "funding_divergence"
            
        return "none"
