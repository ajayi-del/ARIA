"""Equity overnight-gap measurement (2026-09-22, Governor equity-perp
framework: gap fade 1-3% / 3-7% / >7% playbook).

A SoDEX equity perp trades 24/7, so the "overnight gap" is measured
against a ROLLING anchor: the most recent 16:00 ET cash close. Before
16:00 ET the anchor is yesterday's close (the overnight gap); at 16:00
the anchor rolls and the same measurement becomes the after-hours
overreaction. One rule covers both fade windows the framework names.

Pure, zero-I/O: bars are injected, the clock is injected. Fail-open:
any missing/stale anchor data abstains with None.
"""

from __future__ import annotations

from intelligence.equity_session import _ET

_CLOSE_HOUR_ET = 16


def latest_anchor_ts(now_ts: float) -> float | None:
    """UTC epoch of the most recent 16:00 ET <= now_ts. None on error."""
    import datetime as _dt
    try:
        t = _dt.datetime.fromtimestamp(float(now_ts), tz=_dt.timezone.utc) \
            .astimezone(_ET)
        anchor = t.replace(hour=_CLOSE_HOUR_ET, minute=0, second=0,
                           microsecond=0)
        if anchor > t:
            anchor -= _dt.timedelta(days=1)
        return anchor.timestamp()
    except Exception:
        return None


def anchor_close(closes, anchor_ts: float,
                 max_age_s: float = 20 * 60) -> float | None:
    """Last close at or before anchor_ts. closes: iterable of (ts, price)
    ascending. max_age_s bounds how stale the anchor bar may be — on 15m
    perp klines the bar containing 16:00 ET opens 15:45, so 20 min admits
    exactly the honest bar and refuses feed gaps (a stale anchor prints
    phantom gaps — the fade playbook lives on the real close). None when
    no qualifying bar."""
    try:
        anchor_ts = float(anchor_ts)
    except (TypeError, ValueError):
        return None
    best = None
    for item in closes or []:
        try:
            ts, px = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if px <= 0 or ts > anchor_ts:
            continue
        if best is None or ts > best[0]:
            best = (ts, px)
    if best is None or anchor_ts - best[0] > max_age_s:
        return None
    return best[1]


def freshest_close(closes, now_ts: float,
                   max_age_s: float = 20 * 60) -> float | None:
    """Latest close whose bar is fresh relative to now_ts — the now-side
    staleness guard (a frozen feed must never mint a phantom gap). None
    when the newest bar is older than max_age_s or input is degenerate."""
    try:
        now_ts = float(now_ts)
    except (TypeError, ValueError):
        return None
    best = None
    for item in closes or []:
        try:
            ts, px = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if px <= 0:
            continue
        if best is None or ts > best[0]:
            best = (ts, px)
    if best is None or now_ts - best[0] > max_age_s:
        return None
    return best[1]


def gap_pct(now_price: float, anchor_px: float) -> float | None:
    """Signed gap % (now vs anchor close). None on degenerate input."""
    try:
        now_price, anchor_px = float(now_price), float(anchor_px)
        if anchor_px <= 0 or now_price <= 0:
            return None
        return (now_price / anchor_px - 1.0) * 100.0
    except (TypeError, ValueError):
        return None
