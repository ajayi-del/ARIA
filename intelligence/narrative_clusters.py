"""Narrative-propagation evidence layer (S4 intelligence, department template).

Zero-I/O brain: all data injected (day-move dicts, clock callables); owns
only its originator-event state. Emits nothing — returns verdict objects
the caller logs. Kill switch: the splice simply never constructs/calls it.

Doctrine (Governor calibration): a symbol moving >=15% in a day is the
ORIGINATOR of its narrative cluster; the move propagates to cluster nodes
with decay day0 1.00 / day1 0.55 / day2 0.35 / day3 0.20 / >3 0.0. Enter
Day 1-2 nodes LONG only; skip after Day 3; a node already moved >8% is too
late; exit all nodes if the originator retraces >50% of its move.
"""
from dataclasses import dataclass, field
from typing import Callable, Optional

DEFAULT_CLUSTERS = {
    "DeFi_GOVERNANCE": ["UNI-USD", "CRV-USD", "SNX-USD", "BAL-USD", "AAVE-USD", "ENA-USD"],
    "L2_SHADOW":       ["OP-USD", "ARB-USD"],
    "COSMOS":          ["SEI-USD", "TIA-USD", "ATOM-USD", "PYTH-USD"],
    "AI":              ["TAO-USD", "FET-USD", "RENDER-USD"],
    "MEME":            ["BONK-USD", "WIF-USD", "FARTCOIN-USD", "DOGE-USD", "1000PEPE-USD"],
    "RWA":             ["ONDO-USD"],
}

DEFAULT_DECAY_TABLE = [1.00, 0.55, 0.35, 0.20]  # index = propagation day; >3 -> 0.0

_ORIGINATOR_MIN_MOVE_PCT = 15.0
_LATE_NODE_MOVE_PCT = 8.0
_RETRACE_EXIT_FRAC = 0.50
_EXPIRY_S = 7 * 86400.0
_DAY_S = 86400.0


def cluster_of(symbol: str, clusters: Optional[dict] = None) -> Optional[str]:
    """Cluster name for a symbol (ARIA "-USD" form); None if unclustered."""
    if not symbol:
        return None
    for name, members in (clusters or DEFAULT_CLUSTERS).items():
        if symbol in members:
            return name
    return None


@dataclass
class OriginatorEvent:
    symbol: str
    cluster: str
    move_pct: float
    detected_ts: float


def detect_originator(symbol_day_moves: Optional[dict],
                      min_move_pct: float = _ORIGINATOR_MIN_MOVE_PCT,
                      clusters: Optional[dict] = None,
                      detected_ts: float = 0.0) -> list:
    """Pure: symbols whose |day move| >= min_move_pct become originator events.

    Edge: exactly 15.0% qualifies (>=). Unknown cluster symbols are skipped —
    a narrative cannot propagate without a registry entry.
    """
    events = []
    if not symbol_day_moves:
        return events
    for sym, move in symbol_day_moves.items():
        try:
            m = float(move)
        except (TypeError, ValueError):
            continue
        if abs(m) < min_move_pct:
            continue
        cl = cluster_of(sym, clusters)
        if cl is None:
            continue
        events.append(OriginatorEvent(symbol=sym, cluster=cl,
                                      move_pct=m, detected_ts=detected_ts))
    return events


def propagation_day(originator_ts: float, now_ts: float) -> int:
    """UTC-day index of the propagation: 0 = originator's own day, 1, 2, 3+.

    Day boundaries are UTC midnights, not 24h rollovers: 23:59 detection and
    00:01 read = day 1. Negative/None input fails open to day 0.
    """
    try:
        o = int(originator_ts // _DAY_S)
        n = int(now_ts // _DAY_S)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, n - o)


def propagation_decay(day: int, table: Optional[list] = None) -> float:
    """Decay weight for a propagation day; days beyond the table -> 0.0."""
    t = table if table is not None else DEFAULT_DECAY_TABLE
    try:
        d = int(day)
    except (TypeError, ValueError):
        return 0.0
    if d < 0 or d >= len(t):
        return 0.0
    return float(t[d])


def propagation_nodes(cluster: str, moved_symbols, max_moved_pct: float = _LATE_NODE_MOVE_PCT,
                      clusters: Optional[dict] = None,
                      exclude: Optional[set] = None) -> list:
    """Late-entry filter: cluster members that have NOT already moved >8%.

    moved_symbols may be a set of already-moved symbols, or a dict of
    symbol -> day_move_pct (filtered internally; |move| > max_moved_pct
    counts as moved — exactly 8.0% is still an eligible node).
    """
    members = list((clusters or DEFAULT_CLUSTERS).get(cluster) or [])
    ex = set(exclude or ())
    if isinstance(moved_symbols, dict):
        moved = set()
        for sym, mv in moved_symbols.items():
            try:
                if abs(float(mv)) > max_moved_pct:
                    moved.add(sym)
            except (TypeError, ValueError):
                continue
    else:
        moved = set(moved_symbols or ())
    return [s for s in members if s not in moved and s not in ex]


def originator_retraced(originator_move_pct: float,
                        originator_current_from_peak_pct: float,
                        threshold: float = _RETRACE_EXIT_FRAC) -> bool:
    """Exit signal: originator gave back >50% of its move from peak.

    Edge: exactly 50% is NOT a retrace exit (strictly greater). Degenerate
    inputs (zero move, None, wrong types) fail open to False.
    """
    try:
        move = abs(float(originator_move_pct))
        back = abs(float(originator_current_from_peak_pct))
    except (TypeError, ValueError):
        return False
    if move <= 0.0:
        return False
    return back > threshold * move


class NarrativeTracker:
    """Stateful originator registry with 7-day expiry; clock injected.

    The tracker remembers the last day-move dict it was fed and uses it for
    the late-node filter in live_nodes(). All doctrine constants are
    constructor knobs with defaults (config wiring happens later).
    """

    def __init__(self,
                 now_fn: Optional[Callable[[], float]] = None,
                 clusters: Optional[dict] = None,
                 decay_table: Optional[list] = None,
                 min_move_pct: float = _ORIGINATOR_MIN_MOVE_PCT,
                 max_moved_pct: float = _LATE_NODE_MOVE_PCT,
                 expiry_s: float = _EXPIRY_S):
        self._now_fn = now_fn or (lambda: 0.0)
        self._clusters = clusters or DEFAULT_CLUSTERS
        self._decay_table = decay_table or DEFAULT_DECAY_TABLE
        self._min_move_pct = min_move_pct
        self._max_moved_pct = max_moved_pct
        self._expiry_s = expiry_s
        self._events = []          # list[OriginatorEvent], unexpired
        self._last_moves = {}

    def _now(self, now_ts: Optional[float]) -> float:
        if now_ts is not None:
            return now_ts
        try:
            return float(self._now_fn())
        except (TypeError, ValueError):
            return 0.0

    def _prune(self, now: float) -> None:
        self._events = [e for e in self._events
                        if now - e.detected_ts <= self._expiry_s]

    def on_day_moves(self, symbol_day_moves: Optional[dict],
                     now_ts: Optional[float] = None) -> list:
        """Feed the day's moves; returns only NEW originator events.

        A symbol with an unexpired originator event does not re-fire (one
        active narrative per symbol). Fail-open: None/empty -> [].
        """
        now = self._now(now_ts)
        self._prune(now)
        if symbol_day_moves:
            self._last_moves = dict(symbol_day_moves)
        live = {e.symbol for e in self._events}
        fresh = []
        for ev in detect_originator(symbol_day_moves, self._min_move_pct,
                                    self._clusters, detected_ts=now):
            if ev.symbol in live:
                continue
            self._events.append(ev)
            live.add(ev.symbol)
            fresh.append(ev)
        return fresh

    def live_nodes(self, now_ts: Optional[float] = None) -> list:
        """Verdict objects: every propagation node still open, with day/decay.

        Originators are excluded from their own node list; nodes already
        moved >max_moved_pct are filtered (late-entry doctrine). Day >3
        nodes carry decay 0.0 — the caller skips them (doctrine: skip after
        Day 3); they stay visible for telemetry until expiry.
        """
        now = self._now(now_ts)
        self._prune(now)
        out = []
        for ev in self._events:
            day = propagation_day(ev.detected_ts, now)
            decay = propagation_decay(day, self._decay_table)
            nodes = propagation_nodes(ev.cluster, self._last_moves,
                                      self._max_moved_pct, self._clusters,
                                      exclude={ev.symbol})
            for sym in nodes:
                out.append({"symbol": sym, "cluster": ev.cluster,
                            "originator": ev.symbol, "day": day,
                            "decay": decay})
        return out

    def originator_events(self, now_ts: Optional[float] = None) -> list:
        """Unexpired originator events (read-only copy for telemetry)."""
        now = self._now(now_ts)
        self._prune(now)
        return list(self._events)
