"""
intelligence/rebalance_frontrun.py — E4: index-rebalancing front-run brain
(pure, zero-I/O, 2026-09-26 Governor directive).

Doctrine (Governor's spec): passive index flows are datable, directionally
certain demand. Front-run them:
  - index_add    -> LONG, entry window opens 10d before the effective date
                    (the 5-10d band is the ramp);
  - index_delete -> SHORT, entry window opens 5d before (the 3-5d band is
                    the ramp).
  - strength scales 0.5 at the outer window edge -> 1.0 at the inner edge
    (clamped at 1.0 inside the inner edge — the front-run is at full
    conviction closest to the print);
  - the signal EXPIRES at the effective timestamp — once the rebalance
    prints, the front-run is over.

Unconfirmed events (the TEMPLATE seeds pending Governor confirmation)
abstain — never trade a hypothetical rebalance.

Department-template shape (docs/DEPARTMENT_TEMPLATE.md): the event arrives
as an argument (risk_calendar.equity_events.Event), the clock is injected,
decisions leave as frozen RebalanceSignal verdicts. main.py owns the I/O;
the sizing splice is a later phase.

Fail-closed law: nothing raises on bad input. Kill switch: env
REBALANCE_FRONTRUN_ENABLED default "true"; false = frontrun_signal None.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from risk_calendar.equity_events import INDEX_ADD, INDEX_DELETE


# ── Kill switch ───────────────────────────────────────────────────────────────

def rebalance_frontrun_enabled() -> bool:
    """Module-level kill switch (env REBALANCE_FRONTRUN_ENABLED, default
    true). False = frontrun_signal returns None (no signals ever arm)."""
    return os.environ.get("REBALANCE_FRONTRUN_ENABLED", "true").strip().lower() in (
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

ADD_OUTER_D = 10.0                # index_add window opens 10d pre-effective
ADD_INNER_D = 5.0                 # ...full strength from 5d in
DEL_OUTER_D = 5.0                 # index_delete window opens 5d pre-effective
DEL_INNER_D = 3.0                 # ...full strength from 3d in
STRENGTH_OUTER = 0.5
STRENGTH_INNER = 1.0
DAY_S = 86400.0

LONG = "long"
SHORT = "short"


# ── Verdict dataclass ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RebalanceSignal:
    symbol: str
    direction: str                  # "long" (index_add) | "short" (index_delete)
    effective_ts: float             # epoch seconds of the rebalance print
    days_to_effective: float
    strength: float                 # 0.5 outer edge -> 1.0 at/inside inner edge


# ── Signal logic ──────────────────────────────────────────────────────────────

def _strength(days_to_eff: float, outer: float, inner: float) -> float:
    """0.5 at the outer window edge ramping linearly to 1.0 at the inner
    edge; clamped 1.0 inside (the signal lives until the effective print)."""
    if days_to_eff >= outer:
        return STRENGTH_OUTER
    if days_to_eff <= inner:
        return STRENGTH_INNER
    frac = (outer - days_to_eff) / (outer - inner)
    return STRENGTH_OUTER + frac * (STRENGTH_INNER - STRENGTH_OUTER)


def frontrun_signal(symbol, event, now) -> Optional[RebalanceSignal]:
    """Arm a front-run signal from an injected equity_events.Event.

    index_add -> LONG armed from 10d out; index_delete -> SHORT armed from
    5d out; both live until the effective timestamp. None when: kill switch
    off, event missing/malformed, UNCONFIRMED (template seeds abstain),
    effective date passed, outside the window, or bad inputs.
    """
    if not rebalance_frontrun_enabled():
        return None
    if event is None:
        return None
    et = getattr(event, "event_type", None)
    ts = _f(getattr(event, "ts_utc", None))
    confirmed = getattr(event, "confirmed", False)
    n = _f(now)
    if ts is None or n is None or not confirmed:
        return None
    d = (ts - n) / DAY_S
    if d <= 0:
        return None                         # the rebalance already printed
    if et == INDEX_ADD:
        if d > ADD_OUTER_D:
            return None
        return RebalanceSignal(
            symbol=str(symbol), direction=LONG, effective_ts=ts,
            days_to_effective=d,
            strength=_strength(d, ADD_OUTER_D, ADD_INNER_D))
    if et == INDEX_DELETE:
        if d > DEL_OUTER_D:
            return None
        return RebalanceSignal(
            symbol=str(symbol), direction=SHORT, effective_ts=ts,
            days_to_effective=d,
            strength=_strength(d, DEL_OUTER_D, DEL_INNER_D))
    return None                             # unknown event type — abstain


def signal_expired(signal: Optional[RebalanceSignal], now) -> bool:
    """The front-run is over once the rebalance prints: expired when now
    reaches the effective timestamp. Missing clock = expired (fail-closed —
    a signal of unknown age is never traded)."""
    if signal is None:
        return True
    n = _f(now)
    if n is None:
        return True
    return n >= signal.effective_ts
