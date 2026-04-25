"""
risk/regime_sizing.py — Regime-Aware Position Sizing
ARIA Execution Alpha Patch — Component 6

Scales position size by how well the trade direction aligns with the active regime.
Complements the existing RegimeMultiplierEngine (Kent structure) — this layer adds
direction × asset-class granularity.

Example:
  alt_season + alt_long  = 1.30×  (tailwind — ride the trend)
  alt_season + btc_long  = 0.80×  (BTC underperforms in alt season)
  btc_dominance + alt_long = 0.50×  (heavy headwind)
  risk_off + any_long    = 0.40×  (tighten all longs)
"""
from __future__ import annotations

from typing import Dict, Tuple
import structlog

log = structlog.get_logger(__name__)

# (regime, direction_key) → raw size multiplier
# direction_key: "{asset_class}_{direction}" | "any_{direction}"
_REGIME_MULT: Dict[Tuple[str, str], float] = {
    ("alt_season",        "alt_long"):    1.30,
    ("alt_season",        "alt_short"):   0.60,
    ("alt_season",        "btc_long"):    0.80,
    ("alt_season",        "meme_long"):   1.10,
    ("btc_dominance",     "btc_long"):    1.20,
    ("btc_dominance",     "alt_long"):    0.50,
    ("btc_dominance",     "btc_short"):   0.70,
    ("btc_dominance",     "meme_long"):   0.60,
    ("risk_off",          "any_long"):    0.40,
    ("risk_off",          "any_short"):   1.20,
    ("risk_on",           "any_long"):    1.15,
    ("risk_on",           "any_short"):   0.80,
    ("rotational",        "any_long"):    1.00,
    ("rotational",        "any_short"):   1.00,
    ("transitioning",     "any_long"):    0.70,
    ("transitioning",     "any_short"):   0.70,
    ("btc_consolidation", "any_long"):    0.85,
    ("btc_consolidation", "any_short"):   0.85,
    ("cex_flow",          "btc_long"):    1.10,
    ("cex_flow",          "alt_long"):    1.05,
}

_BTC_SYMS  = frozenset({"BTC-USD"})
_ALT_SYMS  = frozenset({
    "ETH-USD", "SOL-USD", "AVAX-USD", "NEAR-USD", "LINK-USD",
    "SUI-USD", "ARB-USD", "OP-USD", "BNB-USD", "MNT-USD", "XRP-USD",
})
_MEME_SYMS = frozenset({"1000PEPE-USD", "TRUMP-USD", "BASED-USD"})


def _asset_class(symbol: str) -> str:
    if symbol in _BTC_SYMS:  return "btc"
    if symbol in _ALT_SYMS:  return "alt"
    if symbol in _MEME_SYMS: return "meme"
    return "any"


def regime_size_mult(
    regime:            str,
    regime_confidence: float,
    symbol:            str,
    direction:         str,
) -> float:
    """
    Returns multiplier in [0.40, 1.30].
    Scales linearly with regime_confidence:
      confidence=0.0 → mult=1.0 (no adjustment)
      confidence=1.0 → full raw mult from table
    """
    if not regime or regime in ("unknown", "confused", ""):
        return 1.0

    asset_cls     = _asset_class(symbol)
    direction_key = f"{asset_cls}_{direction}"

    raw = (
        _REGIME_MULT.get((regime, direction_key))
        or _REGIME_MULT.get((regime, f"any_{direction}"))
        or 1.0
    )

    conf = min(max(regime_confidence, 0.0), 1.0)
    if raw > 1.0:
        adjusted = 1.0 + (raw - 1.0) * conf
    elif raw < 1.0:
        adjusted = 1.0 - (1.0 - raw) * conf
    else:
        adjusted = 1.0

    return round(adjusted, 4)
