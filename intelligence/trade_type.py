"""
intelligence/trade_type.py — Trade Type Tagger
ARIA Execution Alpha Patch — Component 2

Maps signal context to a trade archetype that drives:
  - Time stop duration  (cascade = 15 min; macro = 8 h; breakout = trail only)
  - ATR stop multiplier (cascade = tight 0.8×; mean reversion = wide 2.0×)
  - TP R:R structure    (cascade = [1.0, 1.5, 2.2]; momentum = [1.3, 2.5, 4.5])
  - Passive entry aggressiveness
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


class TradeType(Enum):
    CASCADE_AFTERMATH     = "cascade_aftermath"
    MOMENTUM_CONTINUATION = "momentum_cont"
    MEAN_REVERSION        = "mean_reversion"
    BREAKOUT              = "breakout"
    TRADFI_MACRO          = "tradfi_macro"


# Time stop per type in seconds. None = trail only, no clock limit.
TIME_STOP_SECONDS: dict[TradeType, Optional[int]] = {
    TradeType.CASCADE_AFTERMATH:     15 * 60,
    TradeType.MOMENTUM_CONTINUATION: 4 * 3600,
    TradeType.MEAN_REVERSION:        45 * 60,
    TradeType.BREAKOUT:              None,
    TradeType.TRADFI_MACRO:          8 * 3600,
}

_TRADFI_SYMBOLS: frozenset = frozenset({
    "XAUT-USD", "CL-USD", "COPPER-USD",
    "TSM-USD", "ORCL-USD", "NVDA-USD", "MSFT-USD",
    "AAPL-USD", "AMZN-USD", "GOOGL-USD", "META-USD", "TSLA-USD",
})


def tag_trade_type(
    symbol:                str,
    personality:           str,     # e.g. "AFTERMATH", "APEX", "COIL", "FLOW"
    cascade_zscore:        float,
    regime:                str,
    volatility_percentile: float = 0.5,  # 0–1: current ATR vs rolling history
) -> TradeType:
    """
    Priority: cascade > TradFi symbol > personality-based > default.
    """
    # Cascade aftermath always overrides everything
    if personality == "AFTERMATH" or cascade_zscore > 2.0:
        return TradeType.CASCADE_AFTERMATH

    # TradFi / macro symbols — slow regardless of personality
    if symbol in _TRADFI_SYMBOLS:
        return TradeType.TRADFI_MACRO

    # Personality-driven
    if personality == "APEX" and regime == "alt_season":
        return TradeType.MOMENTUM_CONTINUATION

    if personality == "COIL":
        return TradeType.MEAN_REVERSION

    if personality == "FLOW" and volatility_percentile > 0.80:
        return TradeType.BREAKOUT

    return TradeType.MOMENTUM_CONTINUATION
