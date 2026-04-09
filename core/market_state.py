from typing import Literal, Optional
from pydantic import BaseModel, Field
from datetime import datetime


class MarketState(BaseModel):
    # Identity
    symbol: str
    timestamp_ms: int
    
    # Tier 1 - Macro
    macro_bias: Literal["bullish", "bearish", "neutral"]
    macro_source: str
    macro_confidence: float = Field(ge=0.0, le=1.0)
    
    # Tier 2 - Regime
    regime: Literal["risk_on", "risk_off", "rotational", "confused"]
    leading_asset: str
    lagging_asset: str
    
    # Tier 3 - Structure
    market_type: Literal["expansion", "compression", "trend", "chop"]
    atr: float = Field(ge=0.0)
    atr_vs_baseline: float = Field(description="ratio vs 20-bar avg")
    
    # Tier 4 - Microstructure
    sweep: Literal["buy_side", "sell_side", "none"]
    sweep_index: Optional[int] = None
    reclaim: bool
    imbalance: float = Field(ge=-1.0, le=1.0, description="-1 to +1")
    absorption: bool
    divergence_signal: Literal["bullish_reversion", "bearish_reversion", "none"]
    mark_local_spread_pct: float = Field(ge=0.0)
    
    # Tier 5 - Funding
    funding_class: Literal["extreme_positive", "positive", "neutral", "negative", "extreme_negative"]
    
    # Tier 6 - MAG Signal
    mag_active: bool
    mag_direction: Literal["bullish", "bearish", "none"]
    mag_lag_remaining_min: int = Field(ge=0)
    
    # Final score
    coherence_score: int = Field(ge=0, le=6, description="0-6")
    size_multiplier: float = Field(ge=0.0, le=1.5, description="0.0-1.5")
    trade_direction: Literal["long", "short", "none"]
    invalidation_reason: Optional[str] = None

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }

    def is_valid_signal(self) -> bool:
        """Check if this market state represents a valid trading signal"""
        return (
            self.trade_direction != "none" and
            self.invalidation_reason is None and
            self.coherence_score >= 4
        )

    def get_signal_strength(self) -> float:
        """Calculate overall signal strength (0.0-1.0)"""
        base_strength = self.coherence_score / 6.0
        confidence_multiplier = self.macro_confidence
        size_adjustment = self.size_multiplier
        
        return base_strength * confidence_multiplier * size_adjustment
