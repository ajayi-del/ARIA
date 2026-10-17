"""Equity-open lead — zero-I/O brain for the USTECH100 09:30 ET lead over
crypto (2026-10-17 Governor directive "STOCK EXECUTION ARCHITECTURE" —
the USTECH100/US500 spine: the 09:30 ET cash open is when equity index
perp price discovery leads BTC/ETH by minutes).

SHADOW-FIRST doctrine: scores and emits, NEVER changes trading behavior.

Department template (docs/DEPARTMENT_TEMPLATE.md):
  - Zero I/O: no network, no files, deterministic ET clock via zoneinfo
    (America/New_York; DST handled by the tz database — ET vs UTC offset
    flips between UTC-4 (EDT) and UTC-5 (EST) at the US transitions).
  - One splice point (the caller emits `equity_open_lead_score` with
    ustech100_ret_pct, btc_ret_pct, divergence, leader).
  - Kill switch EQUITY_OPEN_LEAD_ENABLED default "true"; false = window
    closed and every score abstains — indistinguishable from the module
    not existing.
  - Own telemetry namespace (EVENT_NAME below) + own test file.

Window doctrine: 09:30-10:30 ET on WEEKDAYS only — the first hour of the
cash session is where index-perp discovery leads crypto; after 10:30 the
open auction is digested and the lead-lag signal is noise.

Noise doctrine: a sub-10bp index move over a 1-minute-sampled window is
microstructure noise, not leadership; a lead read needs at least
MIN_WINDOW_MINUTES of post-open tape. Leader requires a >5bp magnitude
edge between the two legs; otherwise the legs moved in lockstep
("none").
"""
from __future__ import annotations

import datetime as _dt
import os
from typing import Optional
from zoneinfo import ZoneInfo

EVENT_NAME = "equity_open_lead_score"

_ET = ZoneInfo("America/New_York")

OPEN_MIN_ET = 9 * 60 + 30       # 09:30 ET — cash open
CLOSE_MIN_ET = 10 * 60 + 30     # 10:30 ET — end of the lead window

NOISE_FLOOR_PCT = 0.10          # 10bp — below this a leg is noise, not signal
MIN_WINDOW_MINUTES = 5.0        # need >=5min of post-open tape for a read
DIVERGENCE_FLOOR_PCT = 0.05     # leader needs >5bp magnitude edge


def equity_open_lead_enabled() -> bool:
    """Module-level kill switch (env EQUITY_OPEN_LEAD_ENABLED, default true).
    False = is_equity_open_window always False, scores abstain ("disabled")."""
    return os.environ.get("EQUITY_OPEN_LEAD_ENABLED", "true").strip().lower() in (
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


def _et_clock(ts: float):
    """UTC epoch -> (weekday Mon=0..Sun=6, minute-of-day) on the ET clock;
    None on error. zoneinfo handles the EDT/EST transitions."""
    try:
        t = _dt.datetime.fromtimestamp(float(ts), tz=_dt.timezone.utc) \
            .astimezone(_ET)
        return t.weekday(), t.hour * 60 + t.minute
    except Exception:
        return None


def is_equity_open_window(now_ts) -> bool:
    """True only inside 09:30-10:30 America/New_York on weekdays (Mon-Fri).
    Half-open: [09:30, 10:30) — the 09:30 open bar counts, the 10:30 close
    does not. DST via zoneinfo (the UTC equivalent of 09:30 ET is 13:30 in
    EDT, 14:30 in EST — never hardcode it). Kill switch off = False."""
    if not equity_open_lead_enabled():
        return False
    ts = _f(now_ts)
    if ts is None:
        return False
    clock = _et_clock(ts)
    if clock is None:
        return False
    weekday, minute = clock
    if weekday >= 5:            # Saturday/Sunday ET — no cash open
        return False
    return OPEN_MIN_ET <= minute < CLOSE_MIN_ET


def lead_lag_score(ustech100_ret_pct, btc_ret_pct, window_minutes) -> dict:
    """Score the USTECH100-vs-BTC lead-lag divergence over a post-open
    window. Pure, zero-I/O; returns are signed pct over the SAME window.

    divergence = ustech100_ret_pct - btc_ret_pct (signed; positive = the
    equity index outran crypto over the window). leader is "equity" when
    |ustech100| exceeds |btc| by more than DIVERGENCE_FLOOR_PCT, "crypto"
    on the mirror, "none" when the legs moved together.

    Abstains (divergence/leader None + abstain_reason) when:
      - the module kill switch is off ("disabled"),
      - window_minutes < MIN_WINDOW_MINUTES ("window_too_small"),
      - BOTH |returns| sit below NOISE_FLOOR_PCT ("below_noise_floor"),
      - inputs are garbage ("bad_input").
    """
    out = {"ustech100_ret_pct": None, "btc_ret_pct": None,
           "divergence": None, "leader": None, "abstain_reason": None}
    if not equity_open_lead_enabled():
        out["abstain_reason"] = "disabled"
        return out
    u, b, w = _f(ustech100_ret_pct), _f(btc_ret_pct), _f(window_minutes)
    if u is None or b is None or w is None:
        out["abstain_reason"] = "bad_input"
        return out
    out["ustech100_ret_pct"] = u
    out["btc_ret_pct"] = b
    if w < MIN_WINDOW_MINUTES:
        out["abstain_reason"] = "window_too_small"
        return out
    if abs(u) < NOISE_FLOOR_PCT and abs(b) < NOISE_FLOOR_PCT:
        out["abstain_reason"] = "below_noise_floor"
        return out
    div = round(u - b, 6)
    out["divergence"] = div
    if abs(u) > abs(b) + DIVERGENCE_FLOOR_PCT:
        out["leader"] = "equity"
    elif abs(b) > abs(u) + DIVERGENCE_FLOOR_PCT:
        out["leader"] = "crypto"
    else:
        out["leader"] = "none"
    return out
