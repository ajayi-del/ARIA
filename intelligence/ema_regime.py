"""
intelligence/ema_regime.py — EMA-slope trend regime (Governor msg-186 P0,
regime-engine-v1 filing 2026-09-10).

The ORB day classifier is anti-tape (98.3% of rows read trend at 42.4%
accuracy) and gates ALL direction evidence behind its own verdict — OP took
4 longs into a -6.49% breakdown because day_type never read "trend", and
HYPE was shorted 40s after a LOCKED trend/UP classification because a stale
24h source conflicted the fresh locked read into "unknown".

This module is the always-on second plane: an EMA-slope trend read that
needs no opening range, no midnight anchor, and no 24h window. Pure,
zero-I/O — callers inject closes (+ optional ATR for vol-normalized
separation, matching the filing's vol-normalization confound handling).

Doctrine:
  - A trend exists when the fast EMA is separated from the slow EMA by
    >= min_sep_atr x ATR AND the fast EMA's slope over the lookback agrees
    with the separation direction. Flat/ambiguous = "none" (inert).
  - Fail-open: insufficient data, degenerate prices, or exception →
    "unknown"/"none" — the veto never fires on a guess.
"""

from __future__ import annotations

from typing import List, Optional, Tuple


def ema_series(closes: List[float], period: int) -> List[float]:
    """Standard EMA seeded on the first close. len(out) == len(closes)."""
    if not closes or period <= 0:
        return []
    k = 2.0 / (period + 1.0)
    out = [float(closes[0])]
    for c in closes[1:]:
        out.append(out[-1] + k * (float(c) - out[-1]))
    return out


def ema_slope_trend(
    closes: List[float],
    fast: int = 8,
    slow: int = 21,
    slope_lookback: int = 3,
    min_sep_atr: float = 0.15,
    atr: Optional[float] = None,
) -> Tuple[str, float]:
    """("long" | "short" | "none", separation_in_atr).

    direction "long":  ema_fast > ema_slow AND ema_fast rising over lookback
    direction "short": ema_fast < ema_slow AND ema_fast falling over lookback
    "none": flat, crossing, or slope disagreeing with separation.

    separation is |ema_fast - ema_slow| / atr (vol-normalized; raw price
    units when atr is None/<=0, in which case min_sep_atr is compared
    against the relative separation |fast-slow|/slow — scale-free).
    """
    n = len(closes)
    need = max(slow, fast) + max(1, slope_lookback) + 1
    if n < need:
        return "none", 0.0
    try:
        ef = ema_series(closes, fast)
        es = ema_series(closes, slow)
    except Exception:
        return "none", 0.0
    f_now, s_now = ef[-1], es[-1]
    if s_now <= 0:
        return "none", 0.0
    sep = f_now - s_now
    if atr is not None and atr > 0:
        sep_norm = abs(sep) / atr
    else:
        sep_norm = abs(sep) / s_now * 100.0  # percent scale fallback
    if sep_norm < min_sep_atr:
        return "none", round(sep_norm, 4)
    lb = max(1, slope_lookback)
    f_then = ef[-1 - lb]
    slope = f_now - f_then
    if sep > 0 and slope > 0:
        return "long", round(sep_norm, 4)
    if sep < 0 and slope < 0:
        return "short", round(sep_norm, 4)
    return "none", round(sep_norm, 4)


def ema_alignment_verdict(
    closes: List[float],
    signal_direction: str,
    fast: int = 8,
    slow: int = 21,
    slope_lookback: int = 3,
    min_sep_atr: float = 0.15,
    atr: Optional[float] = None,
) -> str:
    """"aligned" | "counter" | "unknown" — the trend-alignment filter read.

    "unknown" when the EMA plane has no directional opinion (inert — the
    legacy guard alone decides). "counter" only on a measured trend AGAINST
    the signal.
    """
    if signal_direction not in ("long", "short"):
        return "unknown"
    direction, _sep = ema_slope_trend(
        closes, fast=fast, slow=slow, slope_lookback=slope_lookback,
        min_sep_atr=min_sep_atr, atr=atr)
    if direction == "none":
        return "unknown"
    return "aligned" if signal_direction == direction else "counter"
