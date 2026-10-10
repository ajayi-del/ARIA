"""
intelligence/fleet_events.py — fleet event plane (Three-Layer Network
Phase 1, Governor order 2026-10-10). SHADOW-ONLY from birth.

The fleet (anticipator conveyor + campaign book) is sacred: this module
never touches fleet behavior. It is the typed EVENT SPINE the parasite
scalper consumes — a frozen dataclass record plus a bounded in-memory
buffer with windowed snapshot/velocity reads. Pure, zero-I/O: the main.py
splice constructs FleetEvent records at its position_closed / fill handler
sites and pushes them in; brains pull snapshots out. Nothing here reads a
clock, a file, a socket, or config — `now` is always an argument.

Department shape (docs/DEPARTMENT_TEMPLATE.md):
  - Frozen dataclass: a FleetEvent is immutable once emitted (the journal
    doctrine applied to the event plane — an event is a fact, not a state).
  - Bounded buffer: deque(maxlen=50) — the plane holds recent fleet
    history, never the book. A restart loses it by design (the scalper's
    measurement cadence is minutes, not sessions).
  - Fail-closed: garbage rows are coerced or dropped at construction via
    make_event(); record() never raises.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

SCHEMA_VERSION = 1

# The only event types the plane carries. stop_hit / tp_hit / eviction are
# close-side facts; fill is the open-side fact. Anything else is a defect
# in the splice, not a new event class.
EVENT_TYPES = frozenset({"fill", "stop_hit", "tp_hit", "eviction"})

DEFAULT_BUFFER_MAXLEN = 50


@dataclass(frozen=True)
class FleetEvent:
    """One immutable fleet fact.

    event_type    "fill" | "stop_hit" | "tp_hit" | "eviction"
    symbol        venue symbol string, e.g. "BTC-USD"
    direction     "long" | "short"
    price         event price (fill mark / stop trigger / evict mark)
    timestamp     epoch seconds (UTC, injected by the splice)
    margin_usd    margin the fleet had committed (0.0 when unknown)
    leverage      position leverage (0.0 when unknown)
    fill_velocity fills-per-window the fleet was running at event time
                  (0.0 when the splice does not compute it — the buffer
                  recomputes windowed velocity from the event stream
                  itself, so this field is annotation, not authority).
    """
    event_type: str
    symbol: str
    direction: str
    price: float
    timestamp: float
    margin_usd: float = 0.0
    leverage: float = 0.0
    fill_velocity: float = 0.0


def _f(x) -> Optional[float]:
    """Coerce to float; None on any garbage (fail-closed idiom)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def make_event(event_type, symbol, direction, price, timestamp,
               margin_usd=0.0, leverage=0.0,
               fill_velocity=0.0) -> Optional[FleetEvent]:
    """Validated constructor. Returns None on degenerate input — the
    splice drops the row, never the loop (one-bad-line doctrine applied
    to the event plane)."""
    et = str(event_type or "").strip().lower()
    if et not in EVENT_TYPES:
        return None
    sym = str(symbol or "").strip()
    if not sym or sym == "None":
        return None
    side = str(direction or "").strip().lower()
    if side not in ("long", "short"):
        return None
    px = _f(price)
    ts = _f(timestamp)
    if px is None or px <= 0 or ts is None or ts <= 0:
        return None
    return FleetEvent(
        event_type=et,
        symbol=sym,
        direction=side,
        price=px,
        timestamp=ts,
        margin_usd=max(0.0, _f(margin_usd) or 0.0),
        leverage=max(0.0, _f(leverage) or 0.0),
        fill_velocity=max(0.0, _f(fill_velocity) or 0.0),
    )


class FleetEventBuffer:
    """Bounded recent-history plane. maxlen-bounded deque; all reads are
    window-filtered against an injected `now` — the buffer never reads a
    clock itself. Pure: no I/O, no config, no module-level state."""

    def __init__(self, maxlen: int = DEFAULT_BUFFER_MAXLEN) -> None:
        try:
            ml = int(maxlen)
        except (TypeError, ValueError):
            ml = DEFAULT_BUFFER_MAXLEN
        self._events: deque[FleetEvent] = deque(maxlen=max(1, ml))

    # ── write side (the splice) ────────────────────────────────────────

    def record(self, event: Optional[FleetEvent]) -> bool:
        """Push one event. Returns False (drop) on None/foreign input;
        never raises."""
        try:
            if not isinstance(event, FleetEvent):
                return False
            self._events.append(event)
            return True
        except Exception:
            return False

    def __len__(self) -> int:
        return len(self._events)

    # ── read side (the brains) ─────────────────────────────────────────

    def snapshot(self, window_s, now,
                 symbol: Optional[str] = None,
                 event_types: Optional[Sequence[str]] = None
                 ) -> List[FleetEvent]:
        """Events with timestamp in (now - window_s, now], oldest first.
        Optional symbol / event-type filters. Degenerate window/now → []
        (fail-closed: a dark window reads as an empty plane)."""
        win = _f(window_s)
        ts_now = _f(now)
        if win is None or win <= 0 or ts_now is None or ts_now <= 0:
            return []
        types = None
        if event_types is not None:
            types = {str(t).strip().lower() for t in event_types}
        sym = str(symbol).strip() if symbol else None
        cutoff = ts_now - win
        out: List[FleetEvent] = []
        try:
            for ev in self._events:
                if ev.timestamp <= cutoff or ev.timestamp > ts_now:
                    continue
                if sym is not None and ev.symbol != sym:
                    continue
                if types is not None and ev.event_type not in types:
                    continue
                out.append(ev)
        except Exception:
            return []
        return out

    def velocity(self, window_s, now,
                 symbol: Optional[str] = None,
                 event_types: Sequence[str] = ("fill",)) -> int:
        """Per-window event count — the fleet's fill cadence (or any
        event-class cadence via event_types). Counting lives here, not in
        the events themselves: fill_velocity on the record is annotation,
        this is authority."""
        return len(self.snapshot(window_s, now, symbol=symbol,
                                 event_types=event_types))

    def velocity_by_symbol(self, window_s, now,
                           event_types: Sequence[str] = ("fill",)
                           ) -> Dict[str, int]:
        """symbol → count over the window. Dense one-call read for the
        scalper's fleet-velocity scorer."""
        out: Dict[str, int] = {}
        for ev in self.snapshot(window_s, now, event_types=event_types):
            out[ev.symbol] = out.get(ev.symbol, 0) + 1
        return out

    def latest(self, symbol: Optional[str] = None,
               event_types: Optional[Sequence[str]] = None
               ) -> Optional[FleetEvent]:
        """Newest matching event or None. Never raises."""
        try:
            for ev in reversed(self._events):
                if symbol is not None and ev.symbol != symbol:
                    continue
                if (event_types is not None
                        and ev.event_type not in
                        {str(t).strip().lower() for t in event_types}):
                    continue
                return ev
        except Exception:
            return None
        return None
