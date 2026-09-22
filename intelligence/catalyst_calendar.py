"""Catalyst calendar (S4 intelligence, department template) — parallel plane.

The existing risk_calendar engine (CalendarEngine + calendar.db) holds
BLOCKING macro events (FOMC/CPI/NFP: risk-off windows). This module is the
additive OPPORTUNITY plane: CATALYST events are datable narrative
originators (governance votes, token unlocks, protocol launches, exchange
listings, AI news) that the narrative-propagation layer can lean into.

Zero-I/O brain: rows injected (testable without disk), clock injected.
Production store: logs/catalyst_events.json — JSONL append-friendly with
one-bad-line doctrine (a malformed line kills one row, never the store).
This module never touches calendar.db or the blocking engine.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

CATALYST_EVENT_TYPES = frozenset({
    "governance_vote", "token_unlock", "protocol_launch",
    "exchange_listing", "ai_news",
})

_WINDOW_HOURS = 72.0


@dataclass
class CatalystEvent:
    symbol_or_cluster: str    # "UNI-USD" or a cluster name e.g. "AI"
    event_type: str
    ts_utc: float             # epoch seconds
    source: str = "unknown"
    confidence: float = 0.5   # 0.0-1.0
    note: str = ""


def _parse_ts(raw) -> Optional[float]:
    """Epoch seconds (int/float) or ISO-8601 string -> epoch; None if bad."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
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


def load_catalysts(rows) -> tuple:
    """Validate injected rows -> (events, errors); one-bad-line tolerant.

    Each row must be a dict with non-empty symbol_or_cluster and event_type
    strings and a parseable ts_utc. source defaults "unknown", confidence
    defaults 0.5 (clamped to [0,1]; unparseable -> default), note optional.
    Bad rows are skipped with a reason string — never an exception.
    """
    events, errors = [], []
    if not rows:
        return events, errors
    for i, row in enumerate(rows):
        try:
            if not isinstance(row, dict):
                raise ValueError("row_not_dict")
            soc = row.get("symbol_or_cluster")
            et = row.get("event_type")
            if not isinstance(soc, str) or not soc.strip():
                raise ValueError("bad_symbol_or_cluster")
            if not isinstance(et, str) or not et.strip():
                raise ValueError("bad_event_type")
            ts = _parse_ts(row.get("ts_utc"))
            if ts is None:
                raise ValueError("bad_ts_utc")
            try:
                conf = float(row.get("confidence", 0.5))
                conf = min(1.0, max(0.0, conf))
            except (TypeError, ValueError):
                conf = 0.5
            src = row.get("source", "unknown")
            note = row.get("note", "")
            events.append(CatalystEvent(
                symbol_or_cluster=soc.strip(), event_type=et.strip(),
                ts_utc=ts,
                source=src if isinstance(src, str) else "unknown",
                confidence=conf,
                note=note if isinstance(note, str) else ""))
        except Exception as exc:  # one-bad-line doctrine
            errors.append(f"row_{i}:{exc}")
    return events, errors


class CatalystCalendar:
    """Holds catalyst events; answers opportunity-window queries.

    The active window is symmetric around the event: ts ± window_hours —
    pre-event positioning and post-event propagation are both opportunity.
    Cluster-named events match any member symbol via the injected clusters
    map (defaults to narrative_clusters.DEFAULT_CLUSTERS).
    """

    def __init__(self, events=None,
                 now_fn: Optional[Callable[[], float]] = None,
                 clusters: Optional[dict] = None,
                 window_hours: float = _WINDOW_HOURS):
        self._events = list(events or [])
        self._now_fn = now_fn or (lambda: 0.0)
        if clusters is None:
            from intelligence.narrative_clusters import DEFAULT_CLUSTERS
            clusters = DEFAULT_CLUSTERS
        self._clusters = clusters
        self._window_hours = float(window_hours)

    def _now(self, now_ts: Optional[float]) -> float:
        if now_ts is not None:
            return now_ts
        try:
            return float(self._now_fn())
        except (TypeError, ValueError):
            return 0.0

    def active_catalysts(self, now_ts: Optional[float] = None,
                         window_hours: Optional[float] = None) -> list:
        """Events whose window [ts-w, ts+w] contains now (edges inclusive)."""
        now = self._now(now_ts)
        w = float(window_hours) if window_hours is not None else self._window_hours
        half = w * 3600.0
        return [e for e in self._events
                if e.ts_utc - half <= now <= e.ts_utc + half]

    def catalyst_for(self, symbol: str, now_ts: Optional[float] = None,
                     window_hours: Optional[float] = None) -> Optional[CatalystEvent]:
        """Best active catalyst covering the symbol (direct or via cluster).

        Ranking: highest confidence, tie -> soonest ts. Unknown/None symbol
        fails open to None.
        """
        if not symbol:
            return None
        matches = []
        for e in self.active_catalysts(now_ts, window_hours):
            if e.symbol_or_cluster == symbol:
                matches.append(e)
                continue
            members = self._clusters.get(e.symbol_or_cluster) or []
            if symbol in members:
                matches.append(e)
        if not matches:
            return None
        return max(matches, key=lambda e: (e.confidence, -e.ts_utc))

    def is_opportunity_window(self, symbol: str,
                              now_ts: Optional[float] = None,
                              window_hours: Optional[float] = None) -> bool:
        """True when any active catalyst covers the symbol."""
        return self.catalyst_for(symbol, now_ts, window_hours) is not None
