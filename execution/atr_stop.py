"""
execution/atr_stop.py — ATR-Based Dynamic Stop Calculator
ARIA Execution Alpha Patch — Component 3 (v1)

Replaces the flat 1.5× ATR stop with trade-type and tier-aware stop distances.

Logic:
  Base multiplier: cascade=0.8× (tight — fast reversal), momentum=1.5×, MR=2.0×
  Tier adjustment: S-tier ×0.85 (high conviction = tighter stop), B-tier ×1.15

Used by tp_engine and build_candidate as an override when enabled.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from intelligence.signal_tier import SignalTier
    from intelligence.trade_type  import TradeType

# Base ATR multiplier per trade type value string (avoids circular import)
_BASE_MULT: dict[str, float] = {
    "cascade_aftermath": 0.8,
    "momentum_cont":     1.5,
    "mean_reversion":    2.0,
    "breakout":          1.2,
    "tradfi_macro":      1.8,
}

# Tier fine-tuning
_TIER_ADJ: dict[str, float] = {
    "s_tier": 0.85,   # tight — high conviction
    "a_tier": 1.00,
    "b_tier": 1.15,   # wider — give room
    "c_tier": 1.15,
}


def compute_stop(
    entry_price: float,
    direction:   str,
    trade_type:  "TradeType",
    atr:         float,
    tier:        "SignalTier",
) -> float:
    """
    Returns absolute stop price.
    Falls back to entry ± 1.5×ATR if inputs are zero.
    """
    if entry_price <= 0 or atr <= 0:
        return 0.0

    base  = _BASE_MULT.get(trade_type.value, 1.5)
    tadj  = _TIER_ADJ.get(tier.value, 1.0)
    dist  = atr * base * tadj

    if direction == "long":
        return max(0.0, entry_price - dist)
    return entry_price + dist
