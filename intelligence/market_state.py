import time
from typing import Literal, Optional, Any
from pydantic import BaseModel, Field, ConfigDict, computed_field
from datetime import datetime


class MarketState(BaseModel):
    """
    Unified immutable state of a symbol's microstructure and macro context.
    v1.3 Hardened: ConfigDict(frozen=True) to prevent mutation during risk checks.
    signal_age_ms is a computed_field — always-live, never stored, no mutation needed.
    """
    model_config = ConfigDict(
        frozen=True,
        json_encoders={datetime: lambda v: v.isoformat()}
    )

    # Identity
    symbol: str
    timestamp_ms: int
    mark_price: float = 0.0

    # Tracks when the signal was built — drives signal_age_ms computed property
    signal_created_at_ms: int = Field(
        default_factory=lambda: int(time.time() * 1000),
        description="Epoch ms when MarketState was constructed"
    )

    @computed_field  # type: ignore[misc]
    @property
    def signal_age_ms(self) -> int:
        """Always-live age of this signal in ms. No mutation — computed from creation time."""
        return max(0, int(time.time() * 1000) - self.signal_created_at_ms)
    
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
    sweep_price: float = 0.0
    sweep_index: Optional[int] = None
    cluster_validated: bool = False
    cluster_strength: float = 0.0
    reclaim: bool
    imbalance: float = Field(ge=-1.0, le=1.0, description="-1 to +1")
    vpin: float = 0.0
    vpin_hot: bool = False # v1.3 addition
    absorption: bool
    divergence_signal: Literal["bullish_reversion", "bearish_reversion", "none"]
    mark_local_spread_pct: float = Field(ge=0.0)
    
    # Tier 5 - Funding & OI
    funding_class: Literal["extreme_positive", "positive", "neutral", "negative", "extreme_negative"]
    oi_signal: Literal["BULLISH_EXPANSION", "BEARISH_EXPANSION", "SHORT_COVERING", "LONG_LIQUIDATION", "NEUTRAL"] = "NEUTRAL"
    oi_strength: float = 0.0
    
    # Tier 6 - MAG Signal / OI Momentum (SoDEX-native, no external dependencies)
    mag_active: bool
    mag_direction: Literal["bullish", "bearish", "none"]
    mag_lag_remaining_min: int = Field(ge=0)
    market_hours_gate: bool = True
    
    # Final score
    weighted_score: float = Field(ge=0.0, le=10.0, description="0-10 weighted scale")
    raw_score: int = Field(ge=0, le=7, description="0-7 tier count (Tier1-Tier6 + MAG)")
    coherence_score: float = Field(ge=0.0, le=10.0)
    
    # v1.3 Unified Multiplier Chain
    coherence_mult: float = 1.0
    freshness_mult: float = 1.0
    calendar_mult: float = 1.0
    allocation_mult: float = 1.0
    independence_discount: float = 1.0 # v1.3 Addition (Tier overlap penalty)
    
    # Quant Fix Metadata
    slippage_expected_usd: float = 0.0
    funding_cost_est_usd: float = 0.0
    
    size_multiplier: float = Field(ge=0.0, le=2.0, description="0.0-2.0")
    trade_direction: Literal["long", "short", "none"]
    invalidation_reason: Optional[str] = None

    def is_valid_signal(self) -> bool:
        """Check if this market state represents a valid trading signal"""
        return (
            self.trade_direction != "none" and
            self.invalidation_reason is None and
            self.coherence_score >= 1.0  # v2: was 3.0 — unreachable without Bybit data wired
        )

    def get_signal_strength(self) -> float:
        """Calculate overall signal strength (0.0-1.0)"""
        # score max is now potentially higher than 6 in v1.3 due to weighting
        base_strength = min(1.0, self.coherence_score / 6.0)
        confidence_multiplier = self.macro_confidence
        size_adjustment = self.size_multiplier
        
        return base_strength * confidence_multiplier * size_adjustment
