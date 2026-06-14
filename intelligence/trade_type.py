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

# Crypto assets that can run breakout-style in trending regimes
_BREAKOUT_CANDIDATES: frozenset = frozenset({
    "BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD",
    "SPCX-USD", "ARB-USD", "OP-USD",
})

# Regimes that indicate trending / breakout conditions
_TRENDING_REGIMES: frozenset = frozenset({
    "risk_on", "alt_season", "tech_led", "mag7_led", "defi_active",
    "btc_dominance",
})


def tag_trade_type(
    symbol:                str,
    personality:           str,     # e.g. "AFTERMATH", "APEX", "COIL", "FLOW"
    cascade_zscore:        float,
    regime:                str,
    volatility_percentile: float = 0.5,  # 0–1: current ATR vs rolling history
) -> TradeType:
    """
    Priority: cascade > TradFi symbol > breakout conditions > personality-based > default.

    Bull market tuning:
      - BTC/ETH/SOL in a trending regime (risk_on/alt_season/tech_led) with elevated
        ATR (>70th percentile) → BREAKOUT [1.5R/3R/6R, no time stop, trail only]
      - APEX personality in alt_season/risk_on always → MOMENTUM_CONTINUATION
      - COIL personality → MEAN_REVERSION (counter-trend)
      - FLOW + vol > 80th → BREAKOUT (catch the move)
      - TradFi macro symbols → TRADFI_MACRO [1.2R/2R/3R, 8h stop]
      - Default crypto → MOMENTUM_CONTINUATION
    """
    # Cascade aftermath always overrides everything
    if personality == "AFTERMATH" or cascade_zscore > 2.0:
        return TradeType.CASCADE_AFTERMATH

    # TradFi / macro symbols — slow regardless of personality
    if symbol in _TRADFI_SYMBOLS:
        return TradeType.TRADFI_MACRO

    # ── Bull market breakout detection ────────────────────────────────────────
    # In trending regimes, major crypto assets with elevated ATR are breaking out.
    # Use wider TP targets and no time stop — let the trend carry the position.
    # Threshold: vol_percentile > 0.65 (not just 0.80) in trending regimes.
    # Rationale: bull market breakouts occur at moderate volatility before the
    # move becomes parabolic — waiting for 80th percentile catches the top 20%.
    _is_trending_regime = regime in _TRENDING_REGIMES
    _vol_breakout_threshold = 0.65 if _is_trending_regime else 0.80
    if symbol in _BREAKOUT_CANDIDATES and volatility_percentile > _vol_breakout_threshold:
        if personality in ("FLOW", "APEX") or _is_trending_regime:
            return TradeType.BREAKOUT

    # APEX in alt_season or risk_on → momentum continuation (run the trend)
    if personality == "APEX" and regime in ("alt_season", "risk_on", "tech_led", "btc_dominance"):
        return TradeType.MOMENTUM_CONTINUATION

    # Original personality-based routing
    if personality == "APEX" and regime == "alt_season":
        return TradeType.MOMENTUM_CONTINUATION

    if personality == "COIL":
        return TradeType.MEAN_REVERSION

    if personality == "FLOW" and volatility_percentile > 0.80:
        return TradeType.BREAKOUT

    return TradeType.MOMENTUM_CONTINUATION
