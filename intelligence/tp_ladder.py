"""TP ladder — personality-aware targets and manual-trader structure (2026-08-25).

The generic bracket ladder set TP1 at 1.0–1.5R for every trade while FLOW
(rr_min 2.0), SCOUT (2.5) and APEX (2.0) declare higher minimums in
intelligence/personality.py. The machine was amputating its own right tail at
birth: FLOW's avg win measured ≈1.5R against a 2.0R doctrine (payoff 0.56,
expectancy −$0.20/trade over 66 scored trades).

Design doctrine:
  Freeman-Shor (Art of Execution) — payoff repair, not entry repair: never
      take profit below your own minimum R. The floor is the single
      highest-leverage change; at the same win rate, payoff 0.56 → ≥1.2
      flips expectancy positive.
  Van Tharp — R-multiples are the unit of account; the ladder is re-rung
      upward (TP2 ≥ TP1+1R, TP3 ≥ TP2+1R) so a floored TP1 never inverts
      the ladder.
  Raschke / manual-trader doctrine — traders draw lines: swing highs and
      lows ARE the targets. When the nearest structural level sits at or
      just beyond the personality floor, target the line (better fill
      odds, real liquidity) instead of an arithmetic rung.

Pure logic only — candles and prices are injected, nothing here touches I/O.
"""

from __future__ import annotations

import os

# Snap band: a structure level may replace the floored TP1 when its own R
# sits in [rr_min, rr_min + _SNAP_BAND_R] — "the first line beyond my floor".
_SNAP_BAND_R = 1.5
# Buffer pulled back from the line so the target fills BEFORE the wall of
# resting orders at the level, expressed as a fraction of one risk unit.
_STRUCTURE_BUFFER_R = 0.10
# rr_min values above this are doctrine markers (SHIELD 99), not targets.
_MAX_SANE_RR_MIN = 5.0


def personality_tp_floor_enabled() -> bool:
    return os.getenv("PERSONALITY_TP_FLOOR_ENABLED", "true").lower() == "true"


def structure_snap_enabled() -> bool:
    return os.getenv("STRUCTURE_TP_SNAP_ENABLED", "true").lower() == "true"


def _r_of(price: float, entry: float, risk_dist: float, side: str) -> float:
    if side == "long":
        return (price - entry) / risk_dist
    return (entry - price) / risk_dist


def _price_at_r(r: float, entry: float, risk_dist: float, side: str) -> float:
    if side == "long":
        return entry + risk_dist * r
    return entry - risk_dist * r


def floor_ladder_to_rr_min(entry: float, stop: float, side: str,
                           tp1: float, tp2: float, tp3: float,
                           rr_min: float) -> tuple[float, float, float] | None:
    """Floor TP1 at the personality's rr_min and re-rung the ladder upward.

    Returns the new (tp1, tp2, tp3) when TP1 was below rr_min, else None
    (caller leaves the candidate untouched — bit-for-bit legacy path).
    Never lowers any rung: TP2/TP3 only move when they would otherwise sit
    below the new TP1 (+1R spacing).
    """
    risk_dist = abs(entry - stop)
    if risk_dist <= 0 or rr_min <= 0.0 or rr_min > _MAX_SANE_RR_MIN:
        return None
    tp1_r = _r_of(tp1, entry, risk_dist, side)
    if tp1_r >= rr_min - 1e-9:
        return None
    tp2_r = _r_of(tp2, entry, risk_dist, side)
    tp3_r = _r_of(tp3, entry, risk_dist, side)
    new1 = rr_min
    new2 = max(tp2_r, new1 + 1.0)
    new3 = max(tp3_r, new2 + 1.0)
    return (_price_at_r(new1, entry, risk_dist, side),
            _price_at_r(new2, entry, risk_dist, side),
            _price_at_r(new3, entry, risk_dist, side))


def swing_levels(candles, side: str, entry: float,
                 left_right: int = 3) -> list[float]:
    """Manual-trader lines: swing highs (long targets) / swing lows (short
    targets) strictly beyond entry, nearest first.

    A swing high at index i requires high[i] to be the maximum of the
    left_right bars on both sides — the same shape a trader's eye circles.
    Only levels on the TARGET side of entry are returned (a long never
    aims at a line below price).
    """
    n = len(candles)
    if n < 2 * left_right + 1:
        return []
    levels: list[float] = []
    for i in range(left_right, n - left_right):
        window = candles[i - left_right: i + left_right + 1]
        if side == "long":
            px = candles[i].high
            if px > entry and all(px >= c.high for c in window):
                levels.append(px)
        else:
            px = candles[i].low
            if px < entry and all(px <= c.low for c in window):
                levels.append(px)
    if side == "long":
        levels.sort()
    else:
        levels.sort(reverse=True)
    return levels


def structure_target(entry: float, stop: float, side: str, rr_min: float,
                     candles) -> float | None:
    """The line a manual trader would aim at: nearest swing level whose R
    sits in [rr_min, rr_min + band], pulled back a small buffer so the
    target fills before the level's resting orders. None when no level
    qualifies (caller keeps the arithmetic floor)."""
    risk_dist = abs(entry - stop)
    if risk_dist <= 0 or rr_min <= 0.0 or rr_min > _MAX_SANE_RR_MIN:
        return None
    for px in swing_levels(candles, side, entry):
        level_r = _r_of(px, entry, risk_dist, side)
        if level_r < rr_min - 1e-9:
            continue
        if level_r > rr_min + _SNAP_BAND_R:
            break
        target_r = level_r - _STRUCTURE_BUFFER_R
        if target_r < rr_min:
            target_r = rr_min
        return _price_at_r(target_r, entry, risk_dist, side)
    return None
