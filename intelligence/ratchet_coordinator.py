"""intelligence/ratchet_coordinator.py — High-rung ROE network coordinator
(pure brain, zero I/O).

Governor doctrine (locked 2026-09-26, his words): "The moment the ratchet
senses maybe a 10% gain all other engines come to play. The ratchet can set
the next place for a short because it saw the price touched. We can test both
sides. The moment one side becomes profitable we can shift to the other side
and catch sudden movements. We also set weak stop losses when profitable and
price moves in our favour."

The existing intelligence/roe_ratchet.py ladder (3/6/9/15%) mechanically
raises the STOP. This module does NOT replace it — it subscribes to the SAME
per-position ROE tick (the _roe_ratchet_loop seam at main.py:15195) and emits
NETWORK INTENTS at the higher rungs, waking the other engines:

  RATCHET_TRIGGERS (ROE % on initial margin):
    10  → cross_side    — fund a counter-probe from the winner's open profit
    20  → propagation   — the move may propagate to the symbol's family
    30  → stop_to_entry — bank breakeven by geometry (tighten-only, caller's job)
    50  → pyramid       — add half the original margin into a proven move
    80  → weak_stop     — the weak-stop paradox: at 80% ROE a WIDE 50%-of-peak
                          giveback floor survives noise while banking the trend
    100 → respawn       — hand the cycle back to the campaign book

Monotonicity (pinned): each rung fires AT MOST ONCE per position. Arming keys
on the PEAK (a dip does not disarm), emission requires the CURRENT roe >=
rung - ratchet_emit_tolerance_roe (a rung crossed intrabar-then-collapsed does
not fire into weakness; it fires when the current roe recovers into the
tolerance band, then never again).

Department shape (docs/DEPARTMENT_TEMPLATE.md): zero-I/O brain. The caller
injects skip_fn (the existing skip-stack: treasury-managed, pyramid-owned,
Hugo-aligned, mark-scale-quarantined) and family_fn (propagation family
lookup). The brain emits Intent dataclasses; execution is the coordinator's
splice.

Kill switches (config getattr, every False state = module inert):
  ratchet_coordinator_enabled      False  — master gate; disabled returns []
                                     with ZERO state mutation
  ratchet_rungs                    "10,20,30,50,80,100" — comma list, parsed;
                                     unknown/malformed tokens dropped
  ratchet_cross_side_budget_frac   0.30   — fraction of open uPnL funding the
                                     counter-probe
  ratchet_emit_tolerance_roe       2.0    — emission band below the rung
  ratchet_min_price_move_pct       0.5    — leverage-blind guard (cross-review
                                     P1): at 15-38x a 10% ROE rung is a
                                     0.26-0.67% PRICE move — the fee-death
                                     scalp class the Governor's 2026-09-23
                                     fills proved fatal. A rung never fires
                                     while |mark/entry - 1| is below this.
                                     0.0 = legacy (ROE only).

Telemetry (emitted by the splice, per Intent):
  ratchet_rung_fired      rung, kind, symbol, side, position_id, peak_roe
  ratchet_intent_emitted  kind, symbol, side, position_id, payload summary
  ratchet_rung_suppressed kind, reason (skip_fn reason string |
                          degenerate_cross_side_budget)
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

import inspect

RATCHET_TRIGGERS = {
    10: "cross_side",
    20: "propagation",
    30: "stop_to_entry",
    50: "pyramid",
    80: "weak_stop",
    100: "respawn",
}

DEFAULT_RUNGS = "10,20,30,50,80,100"
MAX_TRACKED_POSITIONS = 10000   # bounded dict — FIFO eviction beyond this
PYRAMID_ADD_MARGIN_FRAC = 0.5   # 50: add half the ORIGINAL margin
WEAK_STOP_PEAK_FRAC = 0.50      # 80: floor = 50% of the PEAK roe


@dataclass(frozen=True)
class Intent:
    """One network event. The splice routes on kind; payload carries the
    rung-specific economics. `rung` rides every payload for telemetry."""
    kind: str
    symbol: str
    side: str
    position_id: object
    payload: dict


@dataclass
class _PosState:
    peak_roe: float = float("-inf")
    rungs_fired: Set[float] = field(default_factory=set)


def _opposite(side: str) -> Optional[str]:
    if side == "long":
        return "short"
    if side == "short":
        return "long"
    return None


def _parse_rungs(raw) -> List[float]:
    """Config rung list → sorted unique rungs present in RATCHET_TRIGGERS.
    Accepts the comma string or an iterable; malformed/unknown tokens are
    dropped (fail-closed: a typo shrinks the ladder, never grows it)."""
    if raw is None:
        raw = DEFAULT_RUNGS
    tokens = raw if isinstance(raw, (list, tuple, set)) else str(raw).split(",")
    out: List[float] = []
    for tok in tokens:
        try:
            rung = float(str(tok).strip())
        except (TypeError, ValueError):
            continue
        if rung in RATCHET_TRIGGERS and rung not in out:
            out.append(rung)
    return sorted(out)


class RatchetCoordinator:
    """Per-position rung state machine over the shared ROE tick.

    skip_fn(symbol) or skip_fn(symbol, side) -> Optional[str]: the injected
    skip-stack. Cross-review P1: the native loop's Hugo skip is SIDE-dependent
    (_hugo_sym_aligned(sym, side)), so a two-arg callable is detected via
    inspect at init and called with side; one-arg callables keep the legacy
    contract. A reason string suppresses ALL emission for that tick (peak
    tracking continues — the ratchet keeps score; it simply stays silent).
    Rungs are NOT marked fired while skipped, so they fire on the first
    unskipped eligible tick.

    family_fn(symbol) -> Optional[str]: propagation family lookup; its result
    (or None) rides the propagation payload verbatim.

    cross_side_budget_cap_usd: injected budget cap for the cross_side intent
    (None = uncapped).
    """

    def __init__(self,
                 skip_fn: Optional[Callable[..., Optional[str]]] = None,
                 family_fn: Optional[Callable[[str], Optional[str]]] = None,
                 cross_side_budget_cap_usd: Optional[float] = None,
                 max_positions: int = MAX_TRACKED_POSITIONS):
        self._skip_fn = skip_fn
        self._skip_takes_side = False
        if skip_fn is not None:
            try:
                n_pos = sum(
                    1 for p in inspect.signature(skip_fn).parameters.values()
                    if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
                self._skip_takes_side = n_pos >= 2
            except (TypeError, ValueError):
                self._skip_takes_side = False   # builtins/partials: legacy
        self._family_fn = family_fn
        self._cap = cross_side_budget_cap_usd
        self._max_positions = max(1, int(max_positions))
        self._state: "OrderedDict[object, _PosState]" = OrderedDict()

    # ── tick ────────────────────────────────────────────────────────────────

    def on_roe_tick(self, cfg, *, position_id, symbol, side, roe_pct,
                    peak_roe_pct, entry_price, mark_price,
                    initial_margin_usd, unrealized_pnl_usd,
                    now_ts) -> List[Intent]:
        """One ROE observation for one position → the rung Intents due NOW.

        Monotonic: a rung already in rungs_fired never re-emits, however the
        roe moves afterwards. Arming: effective peak >= rung. Emission:
        current roe >= rung - tolerance. Disabled or degenerate input: []
        with no state mutation (disabled) / no emission (degenerate).
        """
        if not getattr(cfg, "ratchet_coordinator_enabled", False):
            return []
        try:
            roe = float(roe_pct)
            peak_in = float(peak_roe_pct)
            entry = float(entry_price)
            mark = float(mark_price)
            margin = float(initial_margin_usd)
            upnl = float(unrealized_pnl_usd)
        except (TypeError, ValueError):
            return []
        if not position_id or side not in ("long", "short"):
            return []
        if entry <= 0 or mark <= 0:
            return []

        st = self._state.get(position_id)
        if st is None:
            st = _PosState()
            self._state[position_id] = st
            if len(self._state) > self._max_positions:
                self._state.popitem(last=False)   # FIFO eviction — bounded
        else:
            self._state.move_to_end(position_id)
        st.peak_roe = max(st.peak_roe, roe, peak_in)

        if self._skip_fn is not None:
            try:
                if self._skip_takes_side:
                    if self._skip_fn(symbol, side):
                        return []
                elif self._skip_fn(symbol):
                    return []
            except Exception:
                return []   # skip-stack failure = fail-closed silence

        rungs = _parse_rungs(getattr(cfg, "ratchet_rungs", DEFAULT_RUNGS))
        try:
            tol = float(getattr(cfg, "ratchet_emit_tolerance_roe", 2.0))
        except (TypeError, ValueError):
            tol = 2.0
        try:
            frac = float(getattr(cfg, "ratchet_cross_side_budget_frac", 0.30))
        except (TypeError, ValueError):
            frac = 0.30
        try:
            min_move = float(getattr(cfg, "ratchet_min_price_move_pct", 0.5))
        except (TypeError, ValueError):
            min_move = 0.5
        price_move_pct = abs(mark - entry) / entry * 100.0

        out: List[Intent] = []
        for rung in rungs:
            kind = RATCHET_TRIGGERS[rung]
            if rung in st.rungs_fired:
                continue
            if st.peak_roe < rung:
                continue            # not armed — peak never reached the rung
            if roe < rung - tol:
                continue            # armed but collapsed — do not fire weak
            if price_move_pct < min_move:
                continue            # leverage-blind guard — a high-leverage
                                    # rung on a sub-floor wiggle is the
                                    # fee-death class; NOT marked fired, it
                                    # fires when the move grows into it
            payload = self._payload(cfg, kind, rung, symbol, side,
                                    entry, mark, margin, upnl, frac,
                                    st.peak_roe)
            st.rungs_fired.add(rung)   # monotonic — even when payload is None
            if payload is None:
                continue
            out.append(Intent(kind=kind, symbol=symbol, side=side,
                              position_id=position_id, payload=payload))
        return out

    # ── payloads ────────────────────────────────────────────────────────────

    def _payload(self, cfg, kind: str, rung: float, symbol: str, side: str,
                 entry: float, mark: float, margin: float, upnl: float,
                 frac: float, peak: float) -> Optional[dict]:
        if kind == "cross_side":
            budget = frac * upnl
            if budget <= 0:
                return None            # degenerate — never fund a probe from
                                       # a winner that is not winning
            if self._cap is not None:
                budget = min(budget, float(self._cap))
                if budget <= 0:
                    return None
            return {"rung": rung,
                    "budget_usd": budget,
                    "counter_side": _opposite(side)}
        if kind == "propagation":
            family = None
            if self._family_fn is not None:
                try:
                    family = self._family_fn(symbol)
                except Exception:
                    family = None
            return {"rung": rung, "family": family}
        if kind == "stop_to_entry":
            return {"rung": rung, "new_stop": entry, "tighten_only": True}
        if kind == "pyramid":
            return {"rung": rung,
                    "add_margin_frac": PYRAMID_ADD_MARGIN_FRAC,
                    "add_margin_usd": PYRAMID_ADD_MARGIN_FRAC * margin}
        if kind == "weak_stop":
            return {"rung": rung,
                    "stop_roe_floor": WEAK_STOP_PEAK_FRAC * peak}
        if kind == "respawn":
            return {"rung": rung, "cycle": "campaign_respawn"}
        return None

    # ── lifecycle ───────────────────────────────────────────────────────────

    def seed_fired(self, position_id, *, peak_roe: float, rungs) -> None:
        """Boot recovery (cross-review P0): rungs_fired is memory-only — a
        restarted coordinator would re-fire pyramid/cross-side/weak-stop
        intents for an adopted position already past those rungs (duplicate
        pyramid adds = real money). The splice seeds every adopted position
        with the rungs its current ROE has already crossed."""
        try:
            peak = float(peak_roe)
        except (TypeError, ValueError):
            return
        st = self._state.get(position_id)
        if st is None:
            st = _PosState()
            self._state[position_id] = st
            if len(self._state) > self._max_positions:
                self._state.popitem(last=False)
        st.peak_roe = max(st.peak_roe, peak)
        for r in rungs or []:
            try:
                st.rungs_fired.add(float(r))
            except (TypeError, ValueError):
                continue

    def on_position_closed(self, position_id) -> None:
        """State cleanup. A recycled position_id starts with a clean ladder."""
        self._state.pop(position_id, None)

    # ── introspection (splice + tests) ──────────────────────────────────────

    def rungs_fired(self, position_id) -> Set[float]:
        st = self._state.get(position_id)
        return set(st.rungs_fired) if st else set()

    def tracked_positions(self) -> int:
        return len(self._state)
