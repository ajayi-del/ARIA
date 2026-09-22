"""Equity session regime v2 (2026-09-22, Governor equity-perp framework +
position-state review: "AH is not one regime — it is three").

The SoDEX equity perps trade 24/7 but the underlying price discovery does
not. The after-hours window is not one information environment:

  PRE_MARKET   04:00-09:30 ET — gap positioning; slight edge (0.90)
  CORE_HOURS   09:30-16:00 ET — deep market, info disadvantage (0.75)
  AH_OPEN      16:00-20:00 ET, book flat — the perp IS the price
               discovery (structural edge — full size 1.0)
  AH_HOLD      16:00-20:00 ET, book carrying — thin hours degrade
               execution; new entries cut (0.60)
  AH_EVENT     16:00-20:00 ET, live catalyst on the symbol — spread
               explodes 3-5x; only the catalyst trade itself belongs
               here and even it is cut (0.40)
  OVERNIGHT    20:00-04:00 ET — minimal new entries (0.50)

Position-state-aware: book_open (any live position) and event (an
active catalyst on the candidate) are injected by the caller — the
module stays pure and deterministic. Deterministic ET clock (zoneinfo
handles DST); zero-I/O; fail-closed to CORE on error.

Kill switch: config.equity_session_sizing_enabled=False = pre-module
sizing bit-for-bit (multiplier never computed).
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

PRE_MARKET = "PRE_MARKET"
CORE_HOURS = "CORE_HOURS"
AH_OPEN = "AH_OPEN"
AH_HOLD = "AH_HOLD"
AH_EVENT = "AH_EVENT"
OVERNIGHT = "OVERNIGHT"

_PRE_OPEN_MIN = 4 * 60          # 04:00 ET
_CORE_OPEN_MIN = 9 * 60 + 30    # 09:30 ET
_CORE_CLOSE_MIN = 16 * 60       # 16:00 ET
_AH_CLOSE_MIN = 20 * 60         # 20:00 ET

DEFAULT_MULTS = {
    PRE_MARKET: 0.90,
    CORE_HOURS: 0.75,           # information disadvantage — reduced size
    AH_OPEN: 1.0,               # structural edge — the perp is the price
    AH_HOLD: 0.60,              # thin hours, book already carrying
    AH_EVENT: 0.40,             # live catalyst — spread explosion
    OVERNIGHT: 0.50,            # minimal new entries
}


def et_minute_of_day(ts: float) -> int | None:
    """UTC epoch -> minute-of-day on the ET clock (0..1439). None on error."""
    import datetime as _dt
    try:
        t = _dt.datetime.fromtimestamp(float(ts), tz=_dt.timezone.utc) \
            .astimezone(_ET)
        return t.hour * 60 + t.minute
    except Exception:
        return None


def regime(ts: float, book_open: bool = False, event: bool = False) -> str:
    """UTC epoch + injected position state -> session regime. Never raises."""
    m = et_minute_of_day(ts)
    if m is None:
        return CORE_HOURS          # fail-closed to the cautious regime
    if _PRE_OPEN_MIN <= m < _CORE_OPEN_MIN:
        return PRE_MARKET
    if _CORE_OPEN_MIN <= m < _CORE_CLOSE_MIN:
        return CORE_HOURS
    if _CORE_CLOSE_MIN <= m < _AH_CLOSE_MIN:
        if event:
            return AH_EVENT
        return AH_HOLD if book_open else AH_OPEN
    return OVERNIGHT


def size_mult(ts: float, mults: dict | None = None,
              book_open: bool = False, event: bool = False) -> float:
    """Bounded session multiplier; unknown regime reads as CORE (0.75)."""
    table = dict(DEFAULT_MULTS)
    if mults:
        table.update(mults)
    return float(table.get(regime(ts, book_open=book_open, event=event),
                           table[CORE_HOURS]))
