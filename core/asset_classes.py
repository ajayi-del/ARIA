"""
core/asset_classes.py — Single source of truth for asset class metadata.

Asset classes:
  crypto       — 24/7 perpetuals (BTC, ETH, SOL, etc.)
  commodity    — commodity perpetuals (XAUT, CL, COPPER) — CME electronic hours
  equity       — individual equity perpetuals (TSM, ORCL, NVDA) — US market hours
  equity_index — index perpetuals (USTECH100) — extended hours

This module is imported by:
  intelligence/personality.py  — personality availability + ATR thresholds
  intelligence/coherence.py    — tier weight multipliers per terrain
  intelligence/market_hours.py — session gating
  main.py                      — ATR filter, personality wiring
"""

from typing import Dict, List

# ── Symbol → Asset Class ──────────────────────────────────────────────────────

ASSET_CLASS: Dict[str, str] = {
    # Crypto — 24/7, liquidation cascades, funding rates
    "BTC-USD":       "crypto",
    "ETH-USD":       "crypto",
    "SOL-USD":       "crypto",
    "BNB-USD":       "crypto",
    "LINK-USD":      "crypto",
    "AVAX-USD":      "crypto",
    "SUI-USD":       "crypto",
    "ARB-USD":       "crypto",
    "OP-USD":        "crypto",
    "NEAR-USD":      "crypto",
    "MNT-USD":       "crypto",
    "1000PEPE-USD":  "crypto",
    "AAVE-USD":      "crypto",
    "UNI-USD":       "crypto",
    "DOGE-USD":      "crypto",
    "1000BONK-USD":  "crypto",
    "1000SHIB-USD":  "crypto",
    "WIF-USD":       "crypto",
    "ENA-USD":       "crypto",
    "HYPE-USD":      "crypto",
    "TAO-USD":       "crypto",
    "XRP-USD":       "crypto",
    "TRUMP-USD":     "crypto",
    "BASED-USD":     "crypto",
    "ADA-USD":       "crypto",
    "LTC-USD":       "crypto",
    "BCH-USD":       "crypto",
    # Commodity — CME electronic: Sun 23:00 – Fri 22:00 UTC, 22–23 daily maintenance
    # IMPORTANT: Verify CL-USD and COPPER-USD on SoDEX API before trading:
    #   curl "https://mainnet-gw.sodex.dev/api/v1/perps/markets/symbols" | grep -i "CL\|copper"
    "XAUT-USD":      "commodity",
    "CL-USD":        "commodity",
    "COPPER-USD":    "commodity",
    # Equity — US market hours: Mon–Fri 14:30–21:00 UTC regular
    # IMPORTANT: Verify all equity symbols on SoDEX API before trading:
    #   curl "https://mainnet-gw.sodex.dev/api/v1/perps/markets/symbols" | grep -i "TSM\|ORCL\|NVDA\|AAPL\|TSLA"
    "TSM-USD":       "equity",
    "ORCL-USD":      "equity",
    "NVDA-USD":      "equity",
    "AAPL-USD":      "equity",
    "TSLA-USD":      "equity",
    "MSFT-USD":      "equity",
    "GOOGL-USD":     "equity",
    "AMZN-USD":      "equity",
    "META-USD":      "equity",
    # Equity Index — extended hours (pre-market 08:00, regular 14:30–21:00, after-hours)
    "USTECH100-USD": "equity_index",
}


def get_asset_class(symbol: str) -> str:
    """Return asset class string for a symbol. Defaults to 'crypto'."""
    return ASSET_CLASS.get(symbol, "crypto")


# ── Asset-Class Coherence Tier Weights ───────────────────────────────────────
# Multipliers applied to each coherence tier score during calculation.
# Keys match component names in CoherenceEngine.calculate_weighted_score().
#
# Principle: same personality, different terrain expression.
# Crypto: microstructure (sweep, VPIN, OB imbalance) is the primary signal.
# Commodity: macro and geopolitical lead signal are primary; micro is noise.
# Equity: earnings/macro dominant; OB microstructure is noise on SoDEX perps.

ASSET_CLASS_TIERS: Dict[str, Dict[str, float]] = {
    "crypto": {
        "institutional":     0.75,
        "oi_momentum":       1.00,
        "regime":            0.50,
        "structure":         1.00,
        "funding":           0.50,
        "microstructure":    1.50,   # Primary signal — sweep, VPIN, imbalance
        "liquidation":       1.00,
        "mag7_macro":        0.75,
        "cross_venue":       1.00,
        "cascade_aftermath": 1.00,
        "flow_confirmation": 1.00,
    },
    "commodity": {
        "institutional":     1.25,   # Macro SSI critical for commodity moves
        "oi_momentum":       1.00,
        "regime":            0.75,
        "structure":         1.00,
        "funding":           0.00,   # No perp funding on commodity spot
        "microstructure":    0.50,   # Sweep detection less meaningful on commodity OBs
        "liquidation":       0.75,   # Less reliable for commodity cascades
        "mag7_macro":        1.50,   # Geopolitical / macro lead signal primary
        "cross_venue":       0.75,
        "cascade_aftermath": 1.00,
        "flow_confirmation": 0.75,
    },
    "equity": {
        "institutional":     1.50,   # Earnings / institutional flow critical
        "oi_momentum":       0.75,
        "regime":            1.00,
        "structure":         0.75,
        "funding":           0.00,   # No perp funding for stocks
        "microstructure":    0.25,   # Equity OB thin on SoDEX; noise-dominant
        "liquidation":       0.50,   # Rare for individual equity SoDEX perps
        "mag7_macro":        2.00,   # MAG7 lag signal primary for tech stocks
        "cross_venue":       0.50,
        "cascade_aftermath": 0.75,
        "flow_confirmation": 1.00,
    },
    "equity_index": {
        "institutional":     1.25,
        "oi_momentum":       1.00,
        "regime":            1.25,
        "structure":         1.00,
        "funding":           0.00,
        "microstructure":    0.50,
        "liquidation":       0.75,
        "mag7_macro":        1.75,   # Index IS the macro signal
        "cross_venue":       0.75,
        "cascade_aftermath": 1.00,
        "flow_confirmation": 1.00,
    },
}


def get_tier_weights(symbol: str) -> Dict[str, float]:
    """Return coherence tier weight multipliers for a symbol's asset class."""
    asset_class = get_asset_class(symbol)
    return ASSET_CLASS_TIERS.get(asset_class, ASSET_CLASS_TIERS["crypto"])


# ── ATR Ratio Thresholds for COIL Detection ───────────────────────────────────
# If atr_vs_baseline < threshold, market is in COIL regime.
# atr_vs_baseline = current_atr / 20-bar_avg_atr (self-calibrating per symbol)
# A ratio of 0.80 means: current ATR is 80% of the symbol's own baseline.

ASSET_CLASS_ATR_THRESHOLDS: Dict[str, float] = {
    "crypto":       0.80,   # Crypto volatile; threshold at 80% of baseline
    "commodity":    0.70,   # Commodities more range-bound; lower threshold
    "equity":       0.75,
    "equity_index": 0.80,
}


# ── Personality Availability by Asset Class ───────────────────────────────────
# Which personalities are available for each asset class.
# APEX: crypto only (requires liquidation cascades from ValueChain RPC)
# AFTERMATH: crypto + commodity (index can flash-crash; stocks cannot)
# FLOW/SCOUT/COIL/SHIELD: universal

PERSONALITY_AVAILABILITY: Dict[str, List[str]] = {
    "crypto":       ["SHIELD", "AFTERMATH", "APEX", "COIL", "FLOW", "SCOUT"],
    "commodity":    ["SHIELD", "AFTERMATH", "FLOW", "SCOUT", "COIL"],
    # SOVEREIGN: equity only — requires staked MAG7 index as structural anchor.
    # Equity_index (USTECH100-USD) is itself the index; no component divergence possible.
    "equity":       ["SHIELD", "SOVEREIGN", "FLOW", "SCOUT", "COIL"],
    "equity_index": ["SHIELD", "AFTERMATH", "FLOW", "SCOUT", "COIL"],
}

# MAG7 component symbols eligible for SOVEREIGN trading
MAG7_EQUITY_SYMBOLS = frozenset({
    "NVDA-USD", "MSFT-USD", "AAPL-USD", "AMZN-USD",
    "GOOGL-USD", "META-USD", "TSLA-USD",
})
