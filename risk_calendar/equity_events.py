"""
risk_calendar/equity_events.py — earnings + index-rebalance event plane
(2026-09-26 Governor directive: E1 PEAD + E4 rebalance front-run data plane).

Composes with — never edits — the existing risk_calendar plane: the
CalendarEngine/EventStore holds BLOCKING macro events (FOMC/CPI/NFP) plus
MAG7 earnings. This module is the ADDITIVE equity-catalyst plane for the
symbols the equity-perp framework trades: NVDA, AMD, COIN, MSTR, HOOD
earnings, plus index-rebalance (index_add / index_delete) effective dates.

Department-template shape (docs/DEPARTMENT_TEMPLATE.md):
  - Seeds live in code (EARNINGS_EVENTS / REBALANCE_EVENTS) so the plane
    works with zero files on disk.
  - data/earnings_calendar.jsonl and data/rebalance_calendar.jsonl are
    additive OVERRIDE files — one JSON per line — so dates refresh without
    code edits. A newer override row supersedes the seed for its key
    (symbol, event_type, index_name) by ts_utc. One-bad-line doctrine: a
    malformed line kills one row, never the store.
  - STALE LAW: when the LATEST entry for a key is >10d in the past with no
    newer entry, the key is dark — upcoming_events/days_to_event/
    latest_event abstain (never trade a stale calendar).
  - Query functions are pure given injected override rows; the only I/O is
    the thin read_jsonl_overrides() wrapper (missing file = no overrides).
  - Kill switch: env EQUITY_EVENTS_CALENDAR_ENABLED default "true"; false =
    every query abstains.

EARNINGS SEED SOURCING (documented deviation): the five earnings datetimes
are pattern-based estimates (each name's historical reporting window for
the upcoming quarter), flagged confirmed=True so the plane is live from
birth, with source="seed_estimate". The Governor refreshes exact confirmed
dates by appending rows to data/earnings_calendar.jsonl — no code edit.
REBALANCE seeds are TEMPLATE rows (confirmed=False) requiring Governor
confirmation; unconfirmed rows are visible to latest_event but the
front-run brain abstains on them.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple


# ── Kill switch ───────────────────────────────────────────────────────────────

def equity_events_calendar_enabled() -> bool:
    """Module-level kill switch (env EQUITY_EVENTS_CALENDAR_ENABLED, default
    true). False = upcoming_events / days_to_event / latest_event abstain."""
    return os.environ.get("EQUITY_EVENTS_CALENDAR_ENABLED", "true").strip().lower() in (
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


def _parse_ts(raw) -> Optional[float]:
    """Epoch seconds (int/float) or ISO-8601 string -> epoch; None if bad."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        return v if v == v else None
    if isinstance(raw, str):
        try:
            s = raw.strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Event record ──────────────────────────────────────────────────────────────

EARNINGS_EVENT = "earnings"
INDEX_ADD = "index_add"
INDEX_DELETE = "index_delete"
EVENT_TYPES = frozenset({EARNINGS_EVENT, INDEX_ADD, INDEX_DELETE})

DAY_S = 86400.0
STALE_AFTER_S = 10.0 * DAY_S          # latest entry >10d past = dark/abstain

EARNINGS_JSONL_PATH = "data/earnings_calendar.jsonl"
REBALANCE_JSONL_PATH = "data/rebalance_calendar.jsonl"


@dataclass(frozen=True)
class Event:
    symbol: str
    event_type: str                     # earnings | index_add | index_delete
    ts_utc: float                       # epoch seconds (UTC)
    confirmed: bool = True
    source: str = "seed"
    note: str = ""
    index_name: str = ""                # rebalance rows only


# ── Seeds ─────────────────────────────────────────────────────────────────────
# Earnings datetimes are pattern-based estimates for the upcoming quarter
# (documented deviation — refresh exact confirmed dates via the JSONL
# override file, never a code edit). Times are after-close prints (~20:00-
# 21:00 UTC).

def _seed(symbol: str, ts_iso: str, note: str) -> Event:
    return Event(symbol=symbol, event_type=EARNINGS_EVENT,
                 ts_utc=_parse_ts(ts_iso), confirmed=True,
                 source="seed_estimate", note=note)


EARNINGS_EVENTS: Tuple[Event, ...] = (
    _seed("NVDA", "2026-11-19T21:00:00Z",
          "Q3 FY27 estimate — NVDA reports late Nov after close; refresh "
          "via data/earnings_calendar.jsonl when the company confirms."),
    _seed("AMD", "2026-11-04T21:00:00Z",
          "Q3 2026 estimate — AMD reports early Nov after close; refresh "
          "via data/earnings_calendar.jsonl when the company confirms."),
    _seed("COIN", "2026-11-06T20:00:00Z",
          "Q3 2026 estimate — COIN reports early Nov after close; refresh "
          "via data/earnings_calendar.jsonl when the company confirms."),
    _seed("MSTR", "2026-10-30T20:00:00Z",
          "Q3 2026 estimate — MSTR reports end-Oct after close; refresh "
          "via data/earnings_calendar.jsonl when the company confirms."),
    _seed("HOOD", "2026-11-05T21:00:00Z",
          "Q3 2026 estimate — HOOD reports early Nov after close; refresh "
          "via data/earnings_calendar.jsonl when the company confirms."),
)


def _tmpl(symbol: str, event_type: str, ts_iso: str, index_name: str) -> Event:
    return Event(symbol=symbol, event_type=event_type,
                 ts_utc=_parse_ts(ts_iso), confirmed=False,
                 source="template", index_name=index_name,
                 note="TEMPLATE — plausibility seed only; requires Governor "
                      "confirmation (flip confirmed=true via the JSONL "
                      "override) before the front-run brain may arm.")


# December quarterly index rebalance window (effective at the third-Friday
# close, 2026-12-18 ~21:00 UTC). TEMPLATE entries — see note above.
REBALANCE_EVENTS: Tuple[Event, ...] = (
    _tmpl("COIN", INDEX_ADD, "2026-12-18T21:00:00Z", "SP500"),
    _tmpl("HOOD", INDEX_ADD, "2026-12-18T21:00:00Z", "SP500"),
    _tmpl("MSTR", INDEX_DELETE, "2026-12-18T21:00:00Z", "NASDAQ100"),
)


# ── Override rows (pure validation + thin file reader) ───────────────────────

def parse_event_row(row) -> Optional[Event]:
    """Validate one override dict -> Event; None on any defect (never raises).

    Required: non-empty symbol, event_type in EVENT_TYPES, parseable ts_utc
    (epoch or ISO). confirmed defaults true; index_name defaults "".
    """
    try:
        if not isinstance(row, dict):
            return None
        sym = row.get("symbol")
        et = row.get("event_type")
        if not isinstance(sym, str) or not sym.strip():
            return None
        if et not in EVENT_TYPES:
            return None
        ts = _parse_ts(row.get("ts_utc") if et == EARNINGS_EVENT
                       else row.get("effective_ts_utc", row.get("ts_utc")))
        if ts is None:
            return None
        conf = row.get("confirmed", True)
        src = row.get("source", "override")
        note = row.get("note", "")
        idx = row.get("index_name", "")
        return Event(
            symbol=sym.strip().upper(), event_type=et, ts_utc=ts,
            confirmed=bool(conf),
            source=src if isinstance(src, str) else "override",
            note=note if isinstance(note, str) else "",
            index_name=idx if isinstance(idx, str) else "")
    except Exception:
        return None


def load_override_rows(rows) -> Tuple[List[Event], List[str]]:
    """Validate injected rows -> (events, errors); one-bad-line tolerant.
    Accepts dicts or Event instances (Events pass through)."""
    events, errors = [], []
    if not rows:
        return events, errors
    for i, row in enumerate(rows):
        if isinstance(row, Event):
            events.append(row)
            continue
        ev = parse_event_row(row)
        if ev is None:
            errors.append(f"row_{i}:bad_row")
        else:
            events.append(ev)
    return events, errors


def read_jsonl_overrides(path: str) -> Tuple[List[Event], List[str]]:
    """Thin I/O wrapper — the ONLY disk read in the module. Missing/unreadable
    file = no overrides (fail-open to seeds). One-bad-line doctrine: a
    malformed line kills one row, never the store."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            rows = []
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    rows.append(json.loads(line))
                except (ValueError, TypeError):
                    rows.append({"__malformed__": True})
    except (OSError, TypeError):
        return [], []
    return load_override_rows(rows)


# ── Event resolution ──────────────────────────────────────────────────────────

def all_events(override_rows=None) -> List[Event]:
    """Seeds + validated overrides. Overrides are additive; the per-key
    LATEST row (by ts_utc) is the effective one for staleness and queries."""
    events = list(EARNINGS_EVENTS) + list(REBALANCE_EVENTS)
    extra, _ = load_override_rows(override_rows)
    events.extend(extra)
    return events


def _key(ev: Event) -> Tuple[str, str, str]:
    return (ev.symbol, ev.event_type, ev.index_name or "")


def _latest_per_key(events: Iterable[Event]) -> Dict[Tuple[str, str, str], Event]:
    """Per (symbol, event_type, index_name) keep the max-ts row — a newer
    override supersedes a stale seed for staleness judgement."""
    latest: Dict[Tuple[str, str, str], Event] = {}
    for ev in events:
        k = _key(ev)
        cur = latest.get(k)
        if cur is None or ev.ts_utc >= cur.ts_utc:
            latest[k] = ev
    return latest


def _dark_keys(latest: Dict[Tuple[str, str, str], Event], now: float):
    """Keys whose LATEST entry is >STALE_AFTER_S in the past = dark."""
    return {k for k, ev in latest.items()
            if (now - ev.ts_utc) > STALE_AFTER_S}


# ── Query API (pure; override rows injected) ─────────────────────────────────

def upcoming_events(symbol, now, horizon_days,
                    override_rows=None) -> List[Event]:
    """Non-stale events for symbol with ts in (now, now + horizon_days],
    soonest first. Kill switch off / bad inputs = []."""
    if not equity_events_calendar_enabled():
        return []
    n, h = _f(now), _f(horizon_days)
    if n is None or h is None or h < 0 or not symbol:
        return []
    sym = str(symbol).strip().upper()
    events = all_events(override_rows)
    dark = _dark_keys(_latest_per_key(events), n)
    out = [ev for ev in events
           if ev.symbol == sym and _key(ev) not in dark
           and n < ev.ts_utc <= n + h * DAY_S]
    out.sort(key=lambda e: e.ts_utc)
    return out


def days_to_event(symbol, event_type, now,
                  override_rows=None) -> Optional[float]:
    """Days to the nearest upcoming non-stale event of event_type; None when
    none, dark, bad input, or kill switch off."""
    if not equity_events_calendar_enabled():
        return None
    n = _f(now)
    if n is None or not symbol or event_type not in EVENT_TYPES:
        return None
    sym = str(symbol).strip().upper()
    events = all_events(override_rows)
    dark = _dark_keys(_latest_per_key(events), n)
    future = [ev.ts_utc for ev in events
              if ev.symbol == sym and ev.event_type == event_type
              and _key(ev) not in dark and ev.ts_utc > n]
    if not future:
        return None
    return (min(future) - n) / DAY_S


def latest_event(symbol, event_type, now,
                 override_rows=None) -> Optional[Event]:
    """The effective (latest, non-stale) event of event_type for the symbol —
    past or future. This is the row the PEAD brain's splice reads for the
    earnings timestamp. None when dark / missing / kill switch off."""
    if not equity_events_calendar_enabled():
        return None
    n = _f(now)
    if n is None or not symbol or event_type not in EVENT_TYPES:
        return None
    sym = str(symbol).strip().upper()
    latest = _latest_per_key(all_events(override_rows))
    dark = _dark_keys(latest, n)
    best = None
    for k, ev in latest.items():
        if k in dark:
            continue
        if ev.symbol == sym and ev.event_type == event_type:
            if best is None or ev.ts_utc >= best.ts_utc:
                best = ev
    return best
