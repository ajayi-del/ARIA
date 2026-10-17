"""Earnings watch — zero-I/O brain for equity-perp earnings proximity scoring
(2026-10-17 Governor directive "STOCK EXECUTION ARCHITECTURE" — the MSFT
earnings template generalized to every equity-class perp).

The calendar plane ALREADY EXISTS (risk_calendar/equity_events.py — code
seeds + JSONL overrides, 10-day STALE LAW, EQUITY_EVENTS_CALENDAR_ENABLED).
This module does NOT rebuild it: the core scorer takes an injected
earnings_ts; the thin convenience `earnings_window_score_for` reads the
calendar's public accessor `latest_event` (which already enforces the
10-day STALE LAW and its own kill switch) and feeds the scorer.

SHADOW-FIRST doctrine: scores and emits, NEVER changes trading behavior.

Department template (docs/DEPARTMENT_TEMPLATE.md):
  - Zero I/O: no network, no files, no clocks of its own (now_ts injected).
  - One splice point (the caller emits `earnings_watch_window` with symbol,
    days_to_earnings, phase, score).
  - Kill switch EARNINGS_WATCH_ENABLED default "true"; false = abstain,
    indistinguishable from the module not existing.
  - Own telemetry namespace (EVENT_NAME below) + own test file.

Window doctrine: [T-5d, T+1d] around the earnings print. T-5d is where
implied-vol ramp and pre-positioning begin; T+1d keeps the post-print
drift window (PEAD first day) in scope without lingering on stale prints.
Phase is "pre" while now < T, "post" once the print time is reached.

Score = proximity_weight x expected_move_pct:
  pre:  w = (WINDOW_PRE_DAYS  - days_to) / WINDOW_PRE_DAYS   (1.0 at T,
        0.0 at T-5d)
  post: w = (WINDOW_POST_DAYS + days_to) / WINDOW_POST_DAYS  (1.0 at T,
        0.0 at T+1d)
expected_move_pct defaults to DEFAULT_EXPECTED_MOVE_PCT (4.0 — the house
neutral single-name earnings move) when the caller has no options-market
estimate; proximity alone still scores honestly.
"""
from __future__ import annotations

import os
from typing import Optional

EVENT_NAME = "earnings_watch_window"

DAY_S = 86400.0
WINDOW_PRE_DAYS = 5.0          # window opens at T-5d (IV ramp / positioning)
WINDOW_POST_DAYS = 1.0         # window closes at T+1d (PEAD first day)
STALE_AFTER_DAYS = 10.0        # the 10-day STALE LAW (equity_events parity)
DEFAULT_EXPECTED_MOVE_PCT = 4.0  # neutral single-name earnings move


def earnings_watch_enabled() -> bool:
    """Module-level kill switch (env EARNINGS_WATCH_ENABLED, default true).
    False = every score abstains (reason "disabled")."""
    return os.environ.get("EARNINGS_WATCH_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on")


def _f(x) -> Optional[float]:
    """Coerce to float; None on any garbage (fail-closed idiom)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def _abstain(symbol, reason, days_to=None, phase=None):
    return {
        "symbol": symbol,
        "days_to_earnings": days_to,
        "in_window": False,
        "phase": phase,
        "score": None,
        "abstain_reason": reason,
    }


def earnings_window_score(symbol, earnings_ts, now_ts,
                          expected_move_pct=None) -> dict:
    """Score earnings proximity x expected move. Pure, zero-I/O.

    Returns a dict with symbol / days_to_earnings / in_window / phase /
    score; abstains (score None + abstain_reason) when:
      - the module kill switch is off ("disabled"),
      - no earnings date is known ("no_earnings_date"),
      - the known date is >STALE_AFTER_DAYS in the past ("stale_calendar"),
      - inputs are garbage ("bad_input").
    Out-of-window (date known, fresh, but outside [T-5d, T+1d]) is NOT an
    abstention — in_window False with a real days_to_earnings so the
    shadow journal can watch the approach ramp.
    """
    sym = str(symbol).strip().upper() if symbol else ""
    if not earnings_watch_enabled():
        return _abstain(sym, "disabled")
    if not sym:
        return _abstain(sym, "bad_input")
    n = _f(now_ts)
    if n is None:
        return _abstain(sym, "bad_input")
    t = _f(earnings_ts)
    if t is None:
        return _abstain(sym, "no_earnings_date")
    if (n - t) > STALE_AFTER_DAYS * DAY_S:
        return _abstain(sym, "stale_calendar")

    days_to = (t - n) / DAY_S
    phase = "pre" if n < t else "post"
    in_window = -WINDOW_POST_DAYS <= days_to <= WINDOW_PRE_DAYS

    em = _f(expected_move_pct)
    if em is None or em < 0:
        em = DEFAULT_EXPECTED_MOVE_PCT

    if in_window:
        if phase == "pre":
            w = (WINDOW_PRE_DAYS - days_to) / WINDOW_PRE_DAYS
        else:
            w = (WINDOW_POST_DAYS + days_to) / WINDOW_POST_DAYS
        score = round(w * em, 4)
    else:
        score = 0.0

    return {
        "symbol": sym,
        "days_to_earnings": round(days_to, 6),
        "in_window": in_window,
        "phase": phase,
        "score": score,
        "abstain_reason": None,
    }


def earnings_window_score_for(symbol, now_ts, override_rows=None) -> dict:
    """Thin convenience: pull the symbol's effective earnings event from the
    EXISTING calendar plane (risk_calendar.equity_events.latest_event —
    10-day STALE LAW and EQUITY_EVENTS_CALENDAR_ENABLED enforced there) and
    score it. "-USD" perp suffix stripped for the calendar lookup. When the
    calendar has no live row for the symbol the score abstains
    "no_earnings_date" (never trade a dark calendar)."""
    sym = str(symbol).strip().upper() if symbol else ""
    cal_sym = sym[:-4] if sym.endswith("-USD") else sym
    try:
        from risk_calendar import equity_events
        ev = equity_events.latest_event(cal_sym, equity_events.EARNINGS_EVENT,
                                        now_ts, override_rows=override_rows)
    except Exception:
        ev = None
    t = ev.ts_utc if ev is not None else None
    out = earnings_window_score(sym, t, now_ts)
    out["calendar_source"] = ev.source if ev is not None else None
    out["calendar_confirmed"] = ev.confirmed if ev is not None else None
    return out
