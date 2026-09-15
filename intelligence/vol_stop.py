"""Vol-stop cybernetics — ATR-scaled stop floors and R:R-scaled TP floors
(2026-09-15, Governor order after the September exit census).

The census proved the exit class is the leak: stops at ~0.41% fire inside
1-sigma of 4h noise (0.80-1.16%) and 91.6% of stopped trades went green
first. The fix is structural, applied once at bracket creation and FROZEN:
the stop distance may never be tighter than multiplier x ATR(14, 4h), and
TP1 may never sit closer than 2.5x the final stop distance (breakeven WR at
2.5:1 is 28.6% — that is the design point). Existing trailing logic still
governs post-entry tightening; this module only ever WIDENS the geometry,
never tightens it (fail-closed floor, fail-open data).

Multiplier ladder keys on realized-vol rank (Parkinson, data/klines_4h) —
the Polemarch-stamped proxy for options IV rank, which ARIA does not have:
  vol_rank < 30        -> 1.5
  30 <= vol_rank <= 60 -> 2.0
  vol_rank > 60        -> 2.5
  vol_rank None        -> 2.0 (thin data = normal regime)

Zero-I/O brain: candles and prices are injected, nothing here touches the
network, disk, or clock. Never raises — abstain (None) is the fail-open.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Multiplier ladder boundaries (Governor-stamped, CLOSED to tuning until the
# vol_stop shadow gate has n>=30).
_MULT_LOW = 1.5
_MULT_NORMAL = 2.0
_MULT_HIGH = 2.5
_RANK_LOW = 30.0
_RANK_HIGH = 60.0


@dataclass(frozen=True)
class VolFloorResult:
    floored_stop: float
    floored_tp1: float
    stop_dist_pct: float    # final stop distance as a fraction of entry
    atr_pct: float          # ATR(period) as a fraction of entry
    multiplier: float
    floored: bool           # True when either leg moved


def wilder_atr(candles, period: int = 14) -> Optional[float]:
    """Wilder ATR: first ATR = SMA of the first `period` true ranges, then
    ATR = (prior x (period-1) + TR) / period. Needs >= period+1 bars (each TR
    consumes the previous close), else None. Degenerate bars -> None."""
    if candles is None or period <= 0 or len(candles) < period + 1:
        return None
    trs = []
    prev_close = None
    for c in candles:
        try:
            h, l, cl = float(c.high), float(c.low), float(c.close)
        except Exception:
            return None
        if h < l or h <= 0 or l <= 0 or cl <= 0:
            return None
        if prev_close is not None:
            trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
        prev_close = cl
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def atr_multiplier(vol_rank: Optional[float]) -> float:
    """The 1.5 / 2.0 / 2.5 ladder. None (thin data) = normal regime."""
    if vol_rank is None:
        return _MULT_NORMAL
    if vol_rank < _RANK_LOW:
        return _MULT_LOW
    if vol_rank <= _RANK_HIGH:
        return _MULT_NORMAL
    return _MULT_HIGH


def abstain_reason(entry: float, side: str, stop: float, tp1: float,
                   candles, period: int = 14) -> Optional[str]:
    """Why apply_vol_floors would abstain, or None when it would engage.
    Telemetry aid for the splice layer — same predicates, same order."""
    if not candles:
        return "no_bars"
    if len(candles) < period + 1:
        return "atr_thin"
    return _degenerate_reason(entry, side, stop, tp1, candles, period)


def _degenerate_reason(entry: float, side: str, stop: float, tp1: float,
                       candles, period: int) -> Optional[str]:
    if entry <= 0 or stop <= 0 or tp1 <= 0 or side not in ("long", "short"):
        return "degenerate"
    if side == "long":
        if stop >= entry or tp1 <= entry:
            return "degenerate"
    else:
        if stop <= entry or tp1 >= entry:
            return "degenerate"
    atr = wilder_atr(candles, period)
    if atr is None or atr <= 0:
        return "degenerate"
    # Scale-poisoned plane guard (SPCX-class rebase mid-window): an ATR
    # reading above 25% of entry is a plane defect, not volatility — abstain
    # rather than floor a stop through zero.
    if atr / entry > 0.25:
        return "degenerate"
    return None


def apply_vol_floors(entry: float, side: str, stop: float, tp1: float,
                     candles, vol_rank: Optional[float],
                     period: int = 14,
                     tp_rr: float = 2.5,
                     tp2: Optional[float] = None) -> Optional[VolFloorResult]:
    """Floor the stop at multiplier x ATR/entry and TP1 at tp_rr x the final
    stop distance. Returns None to abstain (legacy bit-for-bit at the call
    site); floored=False with originals when the existing geometry already
    satisfies both floors. Only ever moves the stop AWAY from entry and TP1
    FURTHER from entry. tp2 (when given) caps the TP1 raise: a floored TP1
    that would leapfrog TP2 keeps the original TP1 (stop floor still
    applies)."""
    if abstain_reason(entry, side, stop, tp1, candles, period) is not None:
        return None
    atr = wilder_atr(candles, period)
    mult = atr_multiplier(vol_rank)
    min_stop_dist = mult * atr
    stop_dist = abs(entry - stop)
    new_stop = stop
    if stop_dist < min_stop_dist - 1e-12:
        new_stop = entry - min_stop_dist if side == "long" else entry + min_stop_dist
        if new_stop <= 0:   # belt: atr sanity above makes this unreachable
            return None
        stop_dist = min_stop_dist
    min_tp_dist = tp_rr * stop_dist
    tp_dist = abs(tp1 - entry)
    new_tp1 = tp1
    if tp_dist < min_tp_dist - 1e-12:
        new_tp1 = entry + min_tp_dist if side == "long" else entry - min_tp_dist
        if tp2 is not None and tp2 > 0:
            if (side == "long" and new_tp1 >= tp2) or \
               (side == "short" and new_tp1 <= tp2):
                new_tp1 = tp1
    return VolFloorResult(
        floored_stop=new_stop,
        floored_tp1=new_tp1,
        stop_dist_pct=stop_dist / entry,
        atr_pct=atr / entry,
        multiplier=mult,
        floored=(new_stop != stop or new_tp1 != tp1),
    )
