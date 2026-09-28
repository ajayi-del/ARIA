"""
intelligence/vwap_reversion.py — E3 VWAP mean-reversion brain (pure, zero-I/O).

Doctrine (2026-09-26 E3 build, Governor directive): fade stretched session-
VWAP deviations on SoDEX equity perps during core hours only. The session
VWAP (data/session_vwap.py, 13:30-20:00 UTC anchored) is the mean; when
price stretches >= the trigger away from it, enter the fade with a fixed
adverse stop, a reversion TP at 0.8x the trigger deviation, a hard time
stop, and an early exit when price crosses back through the VWAP (the
mean is reached — the trade's reason to exist is gone).

  1. ELIGIBLE — NVDA (primary), AMD, COIN, MSTR, HOOD. Base trigger 1.5%;
     NVDA trigger 2.0%; AMD trigger 1.5% with 0.75% wider stop/TP params
     (stop 2.75%, TP = 0.8 x trigger + 0.75%) per doctrine. Base stop 2.0%
     total adverse, TP = 0.8 x trigger deviation, max hold 3600s, min hold
     900s, leverage 5.
  2. CORE HOURS — HARD gate: 13:30-20:00 UTC. No entries outside; the
     boundary is exact (13:29:59 dead, 13:30:00 live; 19:59:59 live,
     20:00:00 dead).
  3. ONE AT A TIME — a symbol with an open E3 position produces no second
     signal. Daily governor ceiling 50 trades blocks.
  4. EXITS — stop first (defense), then TP, then the 3600s time stop, then
     the vwap_cross early exit (which the 900s min-hold guards against
     churn). Pure verdicts only; execution wiring is a later phase.

Fail-closed law: nothing in this module raises on bad input — every
function fails toward None / abstain with a named reason.
Kill switch: env VWAP_REVERSION_ENABLED default "true"; false = no signals.
"""
from __future__ import annotations

import datetime
import os
from dataclasses import dataclass
from typing import Optional, Tuple


# ── Kill switch ───────────────────────────────────────────────────────────────

def vwap_reversion_enabled() -> bool:
    """Module-level kill switch (env VWAP_REVERSION_ENABLED, default true).
    False = fade_signal returns None always."""
    return os.environ.get("VWAP_REVERSION_ENABLED", "true").strip().lower() in (
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


def base_name(symbol) -> str:
    """'NVDA-USD' -> 'NVDA' (symbols arrive venue-suffixed)."""
    try:
        s = str(symbol)
    except Exception:
        return ""
    if not s or s == "None":
        return ""
    return s.split("-")[0].strip().upper()


# ── Doctrine constants ────────────────────────────────────────────────────────

CORE_OPEN_S = 13 * 3600 + 30 * 60        # 13:30 UTC
CORE_CLOSE_S = 20 * 3600                 # 20:00 UTC
DAY_S = 86400.0

DAILY_TRADE_CEILING = 50                 # governor ceiling

_BASE = {"trigger_pct": 1.5, "stop_pct": 2.0, "tp_frac": 0.8,
         "tp_extra_pct": 0.0, "max_hold_s": 3600.0, "min_hold_s": 900.0,
         "leverage": 5}

ELIGIBLE = {
    # primary: wider trigger — NVDA's session vol needs the bigger stretch
    "NVDA": {**_BASE, "trigger_pct": 2.0},
    # doctrine: AMD trigger stays 1.5% but with 0.75% wider stop/TP params
    "AMD": {**_BASE, "stop_pct": 2.0 + 0.75, "tp_extra_pct": 0.75},
    "COIN": dict(_BASE),
    "MSTR": dict(_BASE),
    "HOOD": dict(_BASE),
}


# ── Core-hours gate ───────────────────────────────────────────────────────────

def _utc_seconds_of_day(now) -> Optional[float]:
    """Accept a datetime (naive treated as UTC) or epoch seconds."""
    if isinstance(now, datetime.datetime):
        return now.hour * 3600.0 + now.minute * 60.0 + now.second
    ts = _f(now)
    if ts is None:
        return None
    try:
        return float(ts % DAY_S)
    except (OverflowError, OSError, ValueError):
        return None


def core_hours_active(now) -> bool:
    """HARD gate: True strictly inside [13:30, 20:00) UTC. Unknown clock =>
    False (fail-closed — no entries outside the window, ever)."""
    sod = _utc_seconds_of_day(now)
    if sod is None:
        return False
    return CORE_OPEN_S <= sod < CORE_CLOSE_S


# ── Signal ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VwapSignal:
    symbol: str
    direction: str            # "long" | "short"
    deviation_pct: float      # signed deviation at entry
    entry: float
    stop: float
    tp: float
    max_hold_s: float
    leverage: int
    min_hold_s: float = 900.0


def fade_signal(symbol, price, deviation_pct, now, daily_trade_count,
                open_position,
                daily_trade_ceiling: int = DAILY_TRADE_CEILING
                ) -> Optional[VwapSignal]:
    """The E3 fade verdict. LONG when deviation <= -trigger (price stretched
    below the session mean), SHORT when >= +trigger. None when: kill switch
    off, symbol ineligible, outside core hours, |deviation| < trigger, the
    symbol already carries an open E3 position (one at a time), or the
    daily trade count reached the governor ceiling."""
    if not vwap_reversion_enabled():
        return None
    sym = base_name(symbol)
    params = ELIGIBLE.get(sym)
    if params is None:
        return None
    if not core_hours_active(now):
        return None
    if open_position:
        return None
    try:
        if daily_trade_count is not None and int(daily_trade_count) >= int(daily_trade_ceiling):
            return None
    except (TypeError, ValueError):
        return None
    p, dev = _f(price), _f(deviation_pct)
    if p is None or dev is None or p <= 0:
        return None
    trigger = params["trigger_pct"]
    if not (abs(dev) >= trigger):
        return None

    direction = "short" if dev > 0 else "long"
    stop_pct = params["stop_pct"]
    tp_pct = params["tp_frac"] * abs(dev) + params["tp_extra_pct"]
    if direction == "long":
        stop = p * (1.0 - stop_pct / 100.0)
        tp = p * (1.0 + tp_pct / 100.0)
    else:
        stop = p * (1.0 + stop_pct / 100.0)
        tp = p * (1.0 - tp_pct / 100.0)
    return VwapSignal(symbol=sym, direction=direction, deviation_pct=dev,
                      entry=p, stop=stop, tp=tp,
                      max_hold_s=params["max_hold_s"],
                      leverage=int(params["leverage"]),
                      min_hold_s=params["min_hold_s"])


# ── Exit verdicts ─────────────────────────────────────────────────────────────

def exit_verdict(signal, price, vwap_now, age_s
                 ) -> Optional[Tuple[str, str]]:
    """(verdict, reason) or None. Priority: stop (defense) > tp (reversion
    captured) > time_stop (max hold expired) > vwap_cross (mean reached —
    guarded by the min hold so a first-minutes cross cannot churn the
    trade). None while the trade has room."""
    if signal is None:
        return None
    p, age = _f(price), _f(age_s)
    if p is None or age is None:
        return None
    direction = str(signal.direction)
    stop, tp = _f(signal.stop), _f(signal.tp)
    max_hold = _f(signal.max_hold_s)
    min_hold = _f(signal.min_hold_s) or 0.0
    vw = _f(vwap_now)

    if direction == "long":
        if stop is not None and p <= stop:
            return ("stop", "stop_adverse")
        if tp is not None and p >= tp:
            return ("tp", "tp_reversion_captured")
        if max_hold is not None and age >= max_hold:
            return ("time_stop", "max_hold_expired")
        if vw is not None and age >= min_hold and p >= vw:
            return ("vwap_cross", "vwap_cross")
        return None
    if direction == "short":
        if stop is not None and p >= stop:
            return ("stop", "stop_adverse")
        if tp is not None and p <= tp:
            return ("tp", "tp_reversion_captured")
        if max_hold is not None and age >= max_hold:
            return ("time_stop", "max_hold_expired")
        if vw is not None and age >= min_hold and p <= vw:
            return ("vwap_cross", "vwap_cross")
        return None
    return None
