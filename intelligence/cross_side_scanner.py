"""intelligence/cross_side_scanner.py — Counter-probe armory (pure brain,
zero I/O).

Governor doctrine (locked 2026-09-26): "The ratchet can set the next place
for a short because it saw the price touched. We can test both sides. The
moment one side becomes profitable we can shift to the other side and catch
sudden movements."

This module is BOTH the consumer of the RatchetCoordinator's "cross_side"
intents AND a standing scanner: given the book's winning positions plus
injected structure hints (stop-cluster levels), it produces CounterProbeSpec
dataclasses for the anticipator fleet — a small, fast-cycle probe on the
OPPOSITE side of a proven winner, funded ONLY by the winner's open profit.

Doctrine (Livermore probe doctrine, Taleb convexity, Aronson bounded DoF):
  - The probe's premium is fixed and tiny: budget = cross_side_budget_frac x
    winner uPnL, capped at cross_side_budget_cap_usd ($8) — a fast-cycle
    margin, never principal. When frac x uPnL falls below
    cross_side_budget_floor_usd ($2) the probe STANDS DOWN (cross-review
    P1: the floor used to pull the missing dollars from principal, funding
    a $2 probe against $0.80 of actual open profit).
  - SAME-SYMBOL PROBE INTELLIGENCE (Governor ruling 2026-09-26, overriding
    the cross-review's refusal recommendation): "same symbol probe in
    different directions at different price levels are two different
    trades." On SoDEX one-way netting a same-symbol counter fill nets
    against the winner — and that netting IS the mechanic, not a defect:
    the probe rests at a DIFFERENT level than the winner's entry, so its
    fill (a) banks winner profit AT the cluster level the ratchet saw
    touched, releasing the margin, and (b) any excess beyond the winner's
    qty flips the book net counter — "the profitable side funds the flip."
    Budget is sized from a fraction of uPnL, so the probe trims rather
    than cancels the winner. cross_side_same_symbol_enabled defaults True.
  - Entry is a LEVEL, not a market chase: the nearest injected stop-cluster
    on the counter side within cross_side_level_band_pct (2%) of the mark.
    No level in band → None. No fake placement.
  - Geometry is asymmetric: a tight stop 0.4% beyond the level and a take
    profit caged at >= 3x the risk — the probe is a convex ticket on the
    sudden reversal the doctrine expects.
  - One live counter-probe per (symbol, counter_side): the dedup registry.
    The splice releases the key when the probe closes.

Department shape (docs/DEPARTMENT_TEMPLATE.md): zero-I/O brain. Levels and
marks arrive injected; decisions leave as specs. main.py owns the I/O and
the order placement.

Kill switches (config getattr, False = module inert, no state mutation):
  cross_side_scanner_enabled    False — master gate
  cross_side_same_symbol_enabled True — Governor ruling 2026-09-26: same-
                                symbol probes at different levels are two
                                trades (bank-at-level + flip), the netting
                                is the mechanic. False = refuse same-symbol
                                probes (splice routes to a family sibling
                                via probe_symbol).
  cross_side_budget_frac        0.30
  cross_side_budget_floor_usd   2.0   — standdown floor, never a top-up
  cross_side_budget_cap_usd     8.0
  cross_side_level_band_pct     2.0

Telemetry (emitted by the splice):
  cross_side_probe_armed      symbol, side, budget_usd, entry, stop, tp,
                              source (intent|scan), winner_position_id
  cross_side_probe_standdown  symbol, side, reason (released|expired|
                              registry_evicted)
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

SCAN_ROE_RUNG = 10.0          # standing-scan arming rung (house ROE %)
STOP_BEYOND_PCT = 0.4         # tight stop this % beyond the level
TP_RR = 3.0                   # take-profit cage: >= 3x the stop risk
MAX_LIVE_PROBES = 1000        # bounded registry — FIFO eviction beyond this


@dataclass(frozen=True)
class CounterProbeSpec:
    """A counter-probe order plan. The splice executes; the brain never
    touches a venue."""
    symbol: str
    side: str                    # the COUNTER side (probe direction)
    budget_usd: float
    entry_price: float           # the stop-cluster level
    stop_price: float            # STOP_BEYOND_PCT beyond the level
    take_profit_price: float     # TP_RR x risk from the level
    rr: float
    source: str                  # "intent" | "scan"
    winner_position_id: object = None
    created_ts: Optional[float] = None


def _opposite(side: str) -> Optional[str]:
    if side == "long":
        return "short"
    if side == "short":
        return "long"
    return None


def _level_price(level) -> Optional[float]:
    """Injected levels arrive as floats or dicts carrying a price."""
    try:
        if isinstance(level, dict):
            level = level.get("price")
        px = float(level)
    except (TypeError, ValueError):
        return None
    return px if px > 0 else None


class CrossSideScanner:
    """Counter-probe factory + live-probe dedup registry."""

    def __init__(self, max_live: int = MAX_LIVE_PROBES):
        self._max_live = max(1, int(max_live))
        self._live: "OrderedDict[Tuple[str, str], CounterProbeSpec]" = \
            OrderedDict()

    # ── intent consumer ─────────────────────────────────────────────────────

    def on_cross_side_intent(self, cfg, *, intent, winner: dict,
                             levels: list) -> Optional[CounterProbeSpec]:
        """Consume a RatchetCoordinator cross_side intent → a probe spec.

        None when: disabled, wrong intent kind, winner uPnL <= 0 (never fund
        a probe from a loser), no level in band, or a live probe already
        holds the (symbol, counter_side) slot. Armed probes are REGISTERED —
        the standing scan dedups against them.
        """
        if not getattr(cfg, "cross_side_scanner_enabled", False):
            return None
        if getattr(intent, "kind", None) != "cross_side":
            return None
        if not isinstance(winner, dict):
            return None
        try:
            symbol = winner["symbol"]
            side = winner["side"]
            mark = float(winner["mark"])
            upnl = float(winner["unrealized_pnl"])
        except (KeyError, TypeError, ValueError):
            return None
        pid = getattr(intent, "position_id", None)
        spec = self._build(cfg, symbol=symbol, winner_side=side, mark=mark,
                           upnl=upnl, levels=levels, source="intent",
                           winner_position_id=pid, created_ts=None)
        if spec is None:
            return None
        return self._register(spec)

    # ── standing scan ───────────────────────────────────────────────────────

    def scan(self, cfg, *, positions: list, marks: dict,
             levels_by_symbol: dict, now_ts) -> List[CounterProbeSpec]:
        """The standing pass: every winning position with house ROE >=
        SCAN_ROE_RUNG and no live counter-probe gets one. Dedup is per
        (symbol, counter_side). Disabled → [] with no registry mutation.
        """
        if not getattr(cfg, "cross_side_scanner_enabled", False):
            return []
        out: List[CounterProbeSpec] = []
        for pos in positions or []:
            if not isinstance(pos, dict):
                continue
            try:
                symbol = pos["symbol"]
                side = pos["side"]
                upnl = float(pos.get("unrealized_pnl", 0.0))
            except (KeyError, TypeError, ValueError):
                continue
            try:
                margin = float(pos.get("initial_margin", 0.0) or 0.0)
            except (TypeError, ValueError):
                margin = 0.0
            roe = None
            if margin > 0:
                roe = upnl / margin * 100.0
            else:
                try:
                    roe = float(pos.get("roe_pct"))
                except (TypeError, ValueError):
                    roe = None
            if roe is None or roe < SCAN_ROE_RUNG:
                continue
            mark = None
            try:
                if marks and marks.get(symbol) is not None:
                    mark = float(marks[symbol])
                elif pos.get("mark") is not None:
                    mark = float(pos["mark"])
            except (TypeError, ValueError):
                mark = None
            if mark is None or mark <= 0:
                continue
            levels = (levels_by_symbol or {}).get(symbol) or []
            spec = self._build(cfg, symbol=symbol, winner_side=side,
                               mark=mark, upnl=upnl, levels=levels,
                               source="scan",
                               winner_position_id=pos.get("position_id"),
                               created_ts=now_ts)
            if spec is None:
                continue
            registered = self._register(spec)
            if registered is not None:
                out.append(registered)
        return out

    # ── registry ────────────────────────────────────────────────────────────

    def release(self, symbol: str, counter_side: str) -> bool:
        """Standdown: the probe closed/expired; free the slot. False when no
        live probe held the key."""
        return self._live.pop((symbol, counter_side), None) is not None

    def live_probes(self) -> int:
        return len(self._live)

    def is_live(self, symbol: str, counter_side: str) -> bool:
        return (symbol, counter_side) in self._live

    # ── internals ───────────────────────────────────────────────────────────

    def _register(self, spec: CounterProbeSpec) -> Optional[CounterProbeSpec]:
        key = (spec.symbol, spec.side)
        if key in self._live:
            return None                     # dedup — one probe per slot
        self._live[key] = spec
        if len(self._live) > self._max_live:
            self._live.popitem(last=False)  # bounded — FIFO eviction
        return spec

    def _build(self, cfg, *, symbol: str, winner_side: str, mark: float,
               upnl: float, levels: list, source: str,
               winner_position_id, created_ts,
               probe_symbol: Optional[str] = None) -> Optional[CounterProbeSpec]:
        if winner_side not in ("long", "short"):
            return None
        if not symbol or mark <= 0 or upnl <= 0:
            return None                     # never fund a probe from a loser
        counter = _opposite(winner_side)
        target = probe_symbol or symbol
        if (target == symbol
                and not getattr(cfg, "cross_side_same_symbol_enabled", True)):
            return None                     # sibling-routing mode only;
                                            # default embraces the netting
                                            # (Governor ruling 2026-09-26)

        try:
            frac = float(getattr(cfg, "cross_side_budget_frac", 0.30))
            floor = float(getattr(cfg, "cross_side_budget_floor_usd", 2.0))
            cap = float(getattr(cfg, "cross_side_budget_cap_usd", 8.0))
            band = float(getattr(cfg, "cross_side_level_band_pct", 2.0))
        except (TypeError, ValueError):
            return None
        budget = min(frac * upnl, cap)
        if budget < floor:
            return None                     # never top up from principal

        # Entry = nearest stop-cluster ON THE COUNTER SIDE within the band:
        # a short probe sells at a level at-or-above the mark (resistance the
        # winner's run just touched); a long probe buys at-or-below it.
        best = None
        for raw in levels or []:
            px = _level_price(raw)
            if px is None:
                continue
            if counter == "short" and px < mark:
                continue
            if counter == "long" and px > mark:
                continue
            if abs(px - mark) / mark > band / 100.0:
                continue
            if best is None or abs(px - mark) < abs(best - mark):
                best = px
        if best is None:
            return None                     # no level in band — no fake
                                            # placement

        if counter == "short":
            stop = best * (1.0 + STOP_BEYOND_PCT / 100.0)
            risk = stop - best
            tp = best - TP_RR * risk
        else:
            stop = best * (1.0 - STOP_BEYOND_PCT / 100.0)
            risk = best - stop
            tp = best + TP_RR * risk

        return CounterProbeSpec(
            symbol=target, side=counter, budget_usd=budget,
            entry_price=best, stop_price=stop, take_profit_price=tp,
            rr=TP_RR, source=source,
            winner_position_id=winner_position_id, created_ts=created_ts)
