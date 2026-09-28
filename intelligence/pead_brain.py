"""
intelligence/pead_brain.py — E1: post-earnings-announcement drift (PEAD)
brain (pure, zero-I/O, 2026-09-26 Governor directive).

Doctrine (Governor's spec): after a CONFIRMED earnings print, the drift
follows the gap direction. Enter on the FIRST pullback that retraces
30-50% of the post-earnings gap; the structural stop sits beyond the BASE
CANDLE (the first 15m candle after the gap print); TP ladder 5/10/15-20%;
hold horizon days (3-10d max); leverage 3-5x. A pullback under 30% is not
yet an entry; past 50% the drift entry is MISSED — never chase. A retrace
beyond 100% (gap fully filled) kills the drift thesis outright.

Department-template shape (docs/DEPARTMENT_TEMPLATE.md): all market reads
arrive as arguments (earnings ts, gap pct via intelligence.equity_gap,
anchor/gap/current prices, base-candle extreme, injected clock); decisions
leave as frozen PeadSetup verdicts. main.py owns the I/O; the Kelly /
pipeline math is the coordinator's later splice — position_size_mult is
the 1.0 baseline hook.

Fail-closed law: nothing raises on bad input — every path fails toward
None / abstain. Kill switch: env PEAD_BRAIN_ENABLED default "true";
false = detect_setup returns None.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional, Tuple


# ── Kill switch ───────────────────────────────────────────────────────────────

def pead_brain_enabled() -> bool:
    """Module-level kill switch (env PEAD_BRAIN_ENABLED, default true).
    False = detect_setup returns None (no setups ever arm)."""
    return os.environ.get("PEAD_BRAIN_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on")


# ── Shared helpers ────────────────────────────────────────────────────────────

def _f(x) -> Optional[float]:
    """Coerce to float; None on any garbage (fail-closed idiom)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


# ── Doctrine constants ────────────────────────────────────────────────────────

MIN_GAP_PCT = 3.0                 # noise floor — smaller gaps are not drift
PULLBACK_LO = 0.30                # entry zone opens at 30% of gap retraced
PULLBACK_HI = 0.50                # ...and closes at 50% (no chase past it)
MAX_AGE_DAYS = 5.0                # arms only within 0-5d after the print
DEFAULT_MAX_HOLD_DAYS = 7.0       # inside the 3-10d doctrine band
DEFAULT_LEVERAGE_CAP = 4.0        # inside the 3-5x doctrine band
DEFAULT_TP_FRACS = (0.05, 0.10, 0.175)   # 5 / 10 / 15-20% ladder
DAY_S = 86400.0

LONG = "long"
SHORT = "short"


# ── Verdict dataclass ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PeadSetup:
    symbol: str
    direction: str                      # "long" | "short" (gap direction)
    gap_pct: float                      # signed post-earnings gap %
    pullback_pct: float                 # fraction of gap retraced at arming
    entry: float                        # zone-mid reference entry price
    structural_stop: float              # beyond the base candle extreme
    tp_ladder: Tuple[float, ...]        # price ladder 5/10/15-20%
    max_hold_s: float                   # hold-horizon cap in seconds
    leverage_cap: float                 # 3-5x doctrine band
    entry_zone: Tuple[float, float] = ()    # (lo, hi) trigger price band
    gap_fill_level: float = 0.0         # anchor price — the full-fill line
    armed_ts: float = 0.0               # clock at arming (max-hold anchor)


# ── Setup detection ───────────────────────────────────────────────────────────

def detect_setup(symbol, earnings_ts, gap_pct,
                 anchor_price, gap_price, current_price, base_candle_extreme,
                 now, confirmed=True, bars_since_gap=None,
                 min_gap_pct: float = MIN_GAP_PCT,
                 pullback_lo: float = PULLBACK_LO,
                 pullback_hi: float = PULLBACK_HI,
                 max_age_days: float = MAX_AGE_DAYS,
                 max_hold_days: float = DEFAULT_MAX_HOLD_DAYS,
                 leverage_cap: float = DEFAULT_LEVERAGE_CAP,
                 tp_fracs: Tuple[float, ...] = DEFAULT_TP_FRACS) -> Optional[PeadSetup]:
    """Arm a PEAD setup from injected reads; None on any failed leg.

    Legs (ALL must hold, fail-closed):
      - kill switch on, earnings CONFIRMED, print 0-5d in the past;
      - |gap_pct| >= min_gap_pct (noise floor, parameter);
      - gap_price side of anchor matches the sign of gap_pct (sanity);
      - measured pullback in [pullback_lo, pullback_hi] of the gap;
      - structural stop beyond the entry zone (non-degenerate bracket).
    bars_since_gap rides the signature for the splice plane (telemetry);
    the classification rules do not consume it — the 0-5d clock is the
    arming window, not a bar count.
    """
    if not pead_brain_enabled():
        return None
    if not confirmed:
        return None
    ets, gp, anchor, gap_px, cur, extreme, n = (
        _f(earnings_ts), _f(gap_pct), _f(anchor_price), _f(gap_price),
        _f(current_price), _f(base_candle_extreme), _f(now))
    if None in (ets, gp, anchor, gap_px, cur, extreme, n):
        return None
    if anchor <= 0 or gap_px <= 0 or cur <= 0 or extreme <= 0:
        return None
    age_s = n - ets
    if age_s < 0 or age_s > _f(max_age_days) * DAY_S:
        return None
    if abs(gp) < _f(min_gap_pct):
        return None
    lo_f, hi_f = _f(pullback_lo), _f(pullback_hi)
    mh_d, lev = _f(max_hold_days), _f(leverage_cap)
    if lo_f is None or hi_f is None or not (0.0 <= lo_f < hi_f <= 1.0):
        return None
    if mh_d is None or mh_d <= 0 or lev is None or lev <= 0:
        return None

    direction = LONG if gp > 0 else SHORT
    gap_size = (gap_px - anchor) if direction == LONG else (anchor - gap_px)
    if gap_size <= 0:
        return None                     # gap geometry contradicts gap_pct
    retrace = ((gap_px - cur) if direction == LONG
               else (cur - gap_px)) / gap_size
    if not (lo_f <= retrace <= hi_f):
        return None                     # <30% not yet; >50% missed, no chase

    # Entry zone: the [hi-retrace, lo-retrace] price band around the gap.
    if direction == LONG:
        zone_lo = gap_px - hi_f * gap_size
        zone_hi = gap_px - lo_f * gap_size
        stop = extreme
        if not (stop < zone_lo):
            return None                 # stop must sit beyond the zone
        entry = gap_px - 0.5 * (lo_f + hi_f) * gap_size
        tps = tuple(entry * (1.0 + t) for t in tp_fracs)
    else:
        zone_lo = gap_px + lo_f * gap_size
        zone_hi = gap_px + hi_f * gap_size
        stop = extreme
        if not (stop > zone_hi):
            return None
        entry = gap_px + 0.5 * (lo_f + hi_f) * gap_size
        tps = tuple(entry * (1.0 - t) for t in tp_fracs)

    return PeadSetup(
        symbol=str(symbol), direction=direction, gap_pct=gp,
        pullback_pct=retrace, entry=entry, structural_stop=stop,
        tp_ladder=tps, max_hold_s=mh_d * DAY_S, leverage_cap=lev,
        entry_zone=(zone_lo, zone_hi), gap_fill_level=anchor, armed_ts=n)


# ── Entry / invalidation / sizing ─────────────────────────────────────────────

def entry_triggered(setup: Optional[PeadSetup], current_price) -> bool:
    """Price has retraced INTO the 30-50% zone (first touch = entry live)."""
    if setup is None or not setup.entry_zone:
        return False
    p = _f(current_price)
    if p is None or p <= 0:
        return False
    lo, hi = setup.entry_zone
    return lo <= p <= hi


def invalidated(setup: Optional[PeadSetup], current_price, now) -> Optional[str]:
    """Named invalidation reason, None while the thesis lives. Priority:
    structural stop (bracket geometry) > gap fully filled (drift thesis
    dead — >100% retrace) > max hold exceeded (the drift has spent its
    half-life)."""
    if setup is None:
        return "no_setup"
    p, n = _f(current_price), _f(now)
    if p is not None:
        if setup.direction == LONG:
            if p <= setup.structural_stop:
                return "structural_stop"
            if setup.gap_fill_level > 0 and p <= setup.gap_fill_level:
                return "gap_filled"
        else:
            if p >= setup.structural_stop:
                return "structural_stop"
            if setup.gap_fill_level > 0 and p >= setup.gap_fill_level:
                return "gap_filled"
    if n is not None and setup.armed_ts > 0:
        if (n - setup.armed_ts) > setup.max_hold_s:
            return "max_hold"
    return None


def position_size_mult(setup: Optional[PeadSetup]) -> float:
    """1.0 baseline — the coordinator's splice owns Kelly / pipeline math.
    None setup = 0.0 (no size for a thesis that never armed)."""
    if setup is None:
        return 0.0
    return 1.0
