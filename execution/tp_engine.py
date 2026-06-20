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
# Large-cap crypto: deep liquidity, can sustain 1.2x wider TP targets in bull market.
# SOL/BNB added: both are Tier-1 liquid perps on SoDEX with clean breakout structure.
_LARGE_CAP_SYMS = frozenset({"BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD"})
# Equity index: slightly more compressed than large-cap crypto (more mean-reverting)
_EQUITY_INDEX_SYMS = frozenset({"SPCX-USD", "USTECH100-USD"})

# Short scale: in a trending bull market, shorts are against the flow.
# Standard: 0.80x. Bull market (risk_on / alt_season): 0.70x for tighter short TPs.
# Logic: if caught short in a bull market, take profit fast — don't overstay.
_SHORT_SCALE      = 0.80   # default short compression
_SHORT_SCALE_BULL = 0.70   # bull market short compression (applied by caller context) 

# Round-trip fee budget (taker+taker). Used to set minimum TP floor:
# A TP inside this boundary is net-negative even when it fills.
# Default: 0.08% RT (0.04% each leg). Live fee from fee_engine overrides.
_DEFAULT_RT_FEE_PCT = 0.0008   # 0.08% round trip
_MIN_TP1_FEE_MULT   = 1.5      # TP1 must be at least 1.5× the fee distance above entry
                                # i.e. net profit ≥ 0.5× fee_dist after paying fees
_RR_BASE: dict[str, list[float]] = {
    "cascade_aftermath":   [1.0, 1.5, 2.2],
    "momentum_cont":       [1.3, 2.5, 4.5],
    "mean_reversion":      [0.8, 1.4, 2.0],
    "breakout":            [1.5, 3.0, 6.0],
    "tradfi_macro":        [1.2, 2.0, 3.0],
}



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
    risk_distance: Optional[float] = None,
    fee_pct:     float = _DEFAULT_RT_FEE_PCT,  # round-trip fee from fee_engine
) -> Dict:
    """
    Returns:
      tp1, tp2, tp3            — absolute prices (fee-adjusted: never below fee floor)
      partial1/2/3_pct         — allocation fractions (sum=1.0)
      risk_distance            — stop distance used
      rr_targets               — actual R multiples after adjustments
      fee_floor_applied        — True if TPs were bumped for fee viability
    """
    if entry <= 0 or atr <= 0:
        return {
            "tp1": 0.0, "tp2": 0.0, "tp3": 0.0,
            "partial1_pct": 0.50, "partial2_pct": 0.30, "partial3_pct": 0.20,
            "risk_distance": 0.0, "rr_targets": [],
        }

    # ── Risk distance ──────────────────────────────────────────────────────────
    if risk_distance is not None and risk_distance > 0:
        risk_dist = risk_distance
    elif calibration and calibration.get("sample", 0) >= 5:
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

    # Asset class scaling — bull market tuning:
    # Large cap crypto (BTC/ETH/SOL/BNB): 1.2x wider TPs (run the trend)
    # Meme coins: 0.75x tighter TPs (high vol, mean-reverts fast)
    # Equity index (SPCX/USTECH100): 1.1x moderate extension
    if symbol in _MEME_SYMS:
        rr = [r * 0.75 for r in rr]
    elif symbol in _LARGE_CAP_SYMS:
        rr = [r * 1.20 for r in rr]
    elif symbol in _EQUITY_INDEX_SYMS:
        rr = [r * 1.10 for r in rr]

    # ── Partials ───────────────────────────────────────────────────────────────
    partials = _TIER_PARTIALS.get(tier.value, [0.40, 0.35, 0.25])

    if direction == "long":
        tps = [entry + risk_dist * r for r in rr]
    else:
        tps = [entry - risk_dist * r for r in rr]

    # ── Fee floor enforcement ──────────────────────────────────────────────────
    # Minimum TP1 must clear round-trip fees + minimum net profit.
    # fee_dist = entry × fee_pct (the cost of entering AND exiting)
    # floor = entry ± (fee_dist × _MIN_TP1_FEE_MULT)
    # If TP1 is inside the floor, bump ALL TPs outward proportionally.
    # This prevents ARIA from targeting 0.05% moves when fees are 0.08%.
    fee_floor_applied = False
    fee_dist = entry * fee_pct
    if direction == "long":
        _tp1_floor = entry + fee_dist * _MIN_TP1_FEE_MULT
        if tps[0] < _tp1_floor:
            _bump = _tp1_floor - tps[0]  # distance to shift all TPs outward
            tps = [t + _bump for t in tps]
            fee_floor_applied = True
    else:
        _tp1_floor = entry - fee_dist * _MIN_TP1_FEE_MULT
        if tps[0] > _tp1_floor:
            _bump = tps[0] - _tp1_floor
            tps = [t - _bump for t in tps]
            fee_floor_applied = True

    return {
        "tp1":              tps[0],
        "tp2":              tps[1],
        "tp3":              tps[2],
        "partial1_pct":     partials[0],
        "partial2_pct":     partials[1],
        "partial3_pct":     partials[2],
        "risk_distance":    risk_dist,
        "rr_targets":       rr,
        "fee_floor_applied": fee_floor_applied,
    }
