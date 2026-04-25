"""
execution/tp_engine.py — Asymmetric Take Profit Engine
ARIA Execution Alpha Patch — Component 4 (v1) + Component 6 (v2)

Computes TP1/TP2/TP3 and partial allocation sizes based on:
  - Trade type  (cascade = tight 1.0/1.5/2.2×; momentum = wide 1.3/2.5/4.5×)
  - Signal tier (S = bigger runner; B = take most off at TP1)
  - Asset class (meme = 0.75×; large cap = 1.20×)
  - Calibration (uses p90_mae_pct × optimal_mult for risk_dist if ≥5 samples)

Short targets are compressed by 0.80× (bearish moves are sharper but reverse faster).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from intelligence.signal_tier import SignalTier
    from intelligence.trade_type  import TradeType

_MEME_SYMS      = frozenset({"BASED-USD", "TRUMP-USD", "1000PEPE-USD"})
_LARGE_CAP_SYMS = frozenset({"BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD"})

# Base R:R targets (risk units) per trade type — long direction
_RR_BASE: dict[str, list[float]] = {
    "cascade_aftermath":   [1.0, 1.5, 2.2],
    "momentum_cont":       [1.3, 2.5, 4.5],
    "mean_reversion":      [0.8, 1.4, 2.0],
    "breakout":            [1.5, 3.0, 6.0],
    "tradfi_macro":        [1.2, 2.0, 3.0],
}

_SHORT_SCALE = 0.80   # shorts compressed — snap back faster

# [tp1_pct, tp2_pct, tp3_pct] per tier
_TIER_PARTIALS: dict[str, list[float]] = {
    "s_tier": [0.25, 0.35, 0.40],   # bigger runner allocation
    "a_tier": [0.40, 0.35, 0.25],
    "b_tier": [0.50, 0.30, 0.20],   # take majority off early
    "c_tier": [0.60, 0.30, 0.10],
}


def compute_tps(
    entry:       float,
    direction:   str,
    trade_type:  "TradeType",
    tier:        "SignalTier",
    atr:         float,
    symbol:      str,
    calibration: Optional[Dict] = None,   # per-symbol {p90_mae_pct, optimal_mult, sample}
) -> Dict:
    """
    Returns:
      tp1, tp2, tp3            — absolute prices
      partial1/2/3_pct         — allocation fractions (sum=1.0)
      risk_distance            — stop distance used
      rr_targets               — actual R multiples after adjustments
    """
    if entry <= 0 or atr <= 0:
        return {
            "tp1": 0.0, "tp2": 0.0, "tp3": 0.0,
            "partial1_pct": 0.50, "partial2_pct": 0.30, "partial3_pct": 0.20,
            "risk_distance": 0.0, "rr_targets": [],
        }

    # ── Risk distance ──────────────────────────────────────────────────────────
    if calibration and calibration.get("sample", 0) >= 5:
        p90  = float(calibration.get("p90_mae_pct", 0.0) or 0.0)
        mult = float(calibration.get("optimal_mult", 1.5) or 1.5)
        if p90 > 0:
            risk_dist = entry * (p90 * mult / 100.0)
        else:
            risk_dist = atr * 1.5
    else:
        risk_dist = atr * 1.5

    # ── R:R targets ────────────────────────────────────────────────────────────
    rr = list(_RR_BASE.get(trade_type.value, [1.2, 2.0, 3.0]))

    if direction == "short":
        rr = [r * _SHORT_SCALE for r in rr]

    # Asset class scaling
    if symbol in _MEME_SYMS:
        rr = [r * 0.75 for r in rr]
    elif symbol in _LARGE_CAP_SYMS:
        rr = [r * 1.20 for r in rr]

    # ── Partials ───────────────────────────────────────────────────────────────
    partials = _TIER_PARTIALS.get(tier.value, [0.40, 0.35, 0.25])

    if direction == "long":
        tps = [entry + risk_dist * r for r in rr]
    else:
        tps = [entry - risk_dist * r for r in rr]

    return {
        "tp1":          tps[0],
        "tp2":          tps[1],
        "tp3":          tps[2],
        "partial1_pct": partials[0],
        "partial2_pct": partials[1],
        "partial3_pct": partials[2],
        "risk_distance": risk_dist,
        "rr_targets":   rr,
    }
