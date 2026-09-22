"""Equity session regime (2026-09-22, Governor equity-perp framework).

The SoDEX equity perps trade 24/7 but the underlying price discovery does
not: US cash 09:30-16:00 ET is the deep market (ARIA is at an information
disadvantage — reduce new-entry size), pre-market and after-hours are the
thin windows where the perp is the ONLY real-time price (the structural
edge — full size). Deterministic ET clock (zoneinfo handles DST); pure,
zero-I/O, fail-open.

Regimes (Governor framework):
  PRE_MARKET   04:00-09:30 ET — position for the gap; perp leads
  CORE_HOURS   09:30-16:00 ET — arb band tight; reduce new entries
  AFTER_HOURS  16:00-04:00 ET — fade overreactions; perp leads

Kill switch: config.equity_session_sizing_enabled=False = pre-module
sizing bit-for-bit (multiplier never computed).
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

PRE_MARKET = "PRE_MARKET"
CORE_HOURS = "CORE_HOURS"
AFTER_HOURS = "AFTER_HOURS"

_PRE_OPEN_MIN = 4 * 60          # 04:00 ET
_CORE_OPEN_MIN = 9 * 60 + 30    # 09:30 ET
_CORE_CLOSE_MIN = 16 * 60       # 16:00 ET

DEFAULT_MULTS = {
    PRE_MARKET: 1.0,
    CORE_HOURS: 0.75,           # information disadvantage — reduced size
    AFTER_HOURS: 1.0,
}


def regime(ts: float) -> str:
    """UTC epoch -> session regime on the ET clock. Never raises."""
    import datetime as _dt
    try:
        t = _dt.datetime.fromtimestamp(float(ts), tz=_dt.timezone.utc) \
            .astimezone(_ET)
        m = t.hour * 60 + t.minute
        if _PRE_OPEN_MIN <= m < _CORE_OPEN_MIN:
            return PRE_MARKET
        if _CORE_OPEN_MIN <= m < _CORE_CLOSE_MIN:
            return CORE_HOURS
        return AFTER_HOURS
    except Exception:
        return CORE_HOURS          # fail-closed to the cautious regime


def et_minute_of_day(ts: float) -> int | None:
    """UTC epoch -> minute-of-day on the ET clock (0..1439). None on error."""
    import datetime as _dt
    try:
        t = _dt.datetime.fromtimestamp(float(ts), tz=_dt.timezone.utc) \
            .astimezone(_ET)
        return t.hour * 60 + t.minute
    except Exception:
        return None


def size_mult(ts: float, mults: dict | None = None) -> float:
    """Bounded session multiplier; unknown regime reads as CORE (0.75)."""
    table = dict(DEFAULT_MULTS)
    if mults:
        table.update(mults)
    return float(table.get(regime(ts), table[CORE_HOURS]))
