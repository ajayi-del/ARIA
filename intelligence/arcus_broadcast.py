"""Arcus → prop_outbox signal broadcaster (observer-class, read-and-broadcast).

Governor directive 2026-11-08: "aria should also broadcast stock signals from
arcus. they executed well on arcus so better to poll arcus and trades pass
through signal layer."

What this is: a READ-ONLY plane. The poller asks the Arcus venue client for
positions + open orders (both are public address-parametrized GETs — no
signing, no mutation surface) and, on detecting a NEW position, appends ONE
record to signals/prop_outbox.jsonl in the exact M4 outbox contract shape
(main.py:15115). The prop harness (prop_harness/io/signal_tap.py) pulls that
file with a 300s TTL — the 20s default poll cadence keeps fills fresh.

What this is NOT: it never touches trade execution, gates, or sizing. It
never cancels, places, or modifies anything. An Arcus API failure skips the
cycle (throttled warning), never crashes the loop.

Kill-switch layering (BOTH must be true for any record to be written; both
default FALSE → pre-bridge bit-for-bit, zero API calls when off):
  ARCUS_BROADCAST_ENABLED — this plane (the new signal flow; Governor flips
                            it on deliberately).
  PROP_OUTBOX_ENABLED     — the master gate for the outbox file (also gates
                            the execution_decision writer at main.py:15121).

Optional narrowing: ARCUS_BROADCAST_SYMBOLS="NVDA-USD,TSLA-USD" (comma list)
restricts emissions; "ALL" or unset = the poller's configured symbol filter
(the main.py splice passes the harness-mappable subset of arcus_assets).
ARCUS_BROADCAST_POLL_S (default 20), ARCUS_BROADCAST_STOP_PCT (default 0.5).

Stop-geometry decision (DOCUMENTED per the build order): the venue's
positionTpsl legs are visible in /v1/openOrders as reduce-only orders
carrying stopPrice triggers — infer_tpsl() reads the NATIVE SL trigger (and
TP) from those orders, classified by side and by which side of the entry the
trigger sits (the client deliberately does not expose orderType/tpslType, so
price-relation classification is the robust path). When no native stop is
found, we SYNTHESIZE a conservative flat stop at entry ∓ stop_pct% (default
0.5% — the scalp regime's stop floor is 0.30%, so 0.5% sits just outside the
noise band; deliberately wider-is-safer: the harness sizes by stop distance,
so a too-wide stop UNDERsizes the mirror, never oversizes it). A record is
NEVER emitted without a valid adverse-side stop — the naked-position class
dies at the source, matching the M4 writer's doctrine.

Dedup identity: arcus positions carry no stable venue-side position id in the
parsed client shape, so identity is (symbol, side) presence-transition keyed:
a position broadcasts ONCE on its absent→present transition with
arcus_position_id = f"arcus_{symbol}_{side}_{first_seen_ms}"; disappearance
clears the key so the NEXT position on that symbol+side broadcasts fresh.
Pyramid adds change averageEntryPrice but NOT the key — one position = one
signal (the harness mirrors the position, not each add). State persists to
logs/arcus_broadcast_state.json (atomic tmp+replace) so restarts never
re-broadcast live positions.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)

COHERENCE_SENTINEL = 5.0        # provenance marker; src carries the truth
SRC = "arcus_fill"
DEFAULT_POLL_S = 20.0
DEFAULT_STOP_PCT = 0.5          # % of entry; see module docstring
_WARN_THROTTLE_S = 60.0


# ── Env gates ────────────────────────────────────────────────────────────────

def broadcast_enabled() -> bool:
    """This plane's kill switch. Default FALSE (new signal flow)."""
    return os.environ.get("ARCUS_BROADCAST_ENABLED", "false").strip().lower() == "true"


def outbox_enabled() -> bool:
    """Master gate for the outbox FILE (shared with the M4 writer)."""
    return os.environ.get("PROP_OUTBOX_ENABLED", "false").strip().lower() == "true"


def env_symbol_filter() -> Optional[frozenset]:
    """ARCUS_BROADCAST_SYMBOLS: comma list narrows, "ALL"/unset = no override
    (None → the caller's configured filter stands)."""
    raw = os.environ.get("ARCUS_BROADCAST_SYMBOLS", "").strip()
    if not raw or raw.upper() == "ALL":
        return None
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


# ── Pure helpers ─────────────────────────────────────────────────────────────

def position_key(pos: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """(symbol, side) identity for presence-transition dedup."""
    sym = str(pos.get("symbol") or "").strip()
    side = str(pos.get("side") or "").strip().lower()
    if not sym or side not in ("long", "short"):
        return None
    return (sym, side)


def infer_tpsl(position: Dict[str, Any],
               open_orders: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    """Read the NATIVE positionTpsl legs out of the open-orders snapshot.

    Returns (stop_price, tp_price), either leg None when absent. The legs are
    reduce-only orders on the CLOSE side; the client's place_position_tpsl
    writes SL trigger via stopPrice (protective bound in price, ≤5% away) and
    TP as a LIMIT whose price == stopPrice. Classification is by trigger
    relation to entry (adverse side = stop, favorable side = TP) — robust to
    the venue not exposing orderType/tpslType through the parsed shape.
    Tightest stop / nearest TP win when multiple candidates exist.
    """
    entry = float(position.get("entry") or position.get("avgEntryPrice") or 0.0)
    side = str(position.get("side") or "").lower()
    sym = str(position.get("symbol") or "")
    if entry <= 0 or side not in ("long", "short"):
        return None, None
    close_side = "SELL" if side == "long" else "BUY"
    stops: List[float] = []
    tps: List[float] = []
    for o in open_orders or []:
        try:
            if str(o.get("symbol") or "") != sym:
                continue
            if not bool(o.get("reduceOnly")):
                continue
            if str(o.get("side") or "").upper() != close_side:
                continue
            trig = float(o.get("stopPrice") or 0.0)
            if trig <= 0:
                trig = float(o.get("price") or 0.0)
            if trig <= 0:
                continue
            if side == "long":
                (stops if trig < entry else tps).append(trig)
            else:
                (stops if trig > entry else tps).append(trig)
        except (TypeError, ValueError, AttributeError):
            continue
    stop = (max(stops) if side == "long" else min(stops)) if stops else None
    tp = (min(tps) if side == "long" else max(tps)) if tps else None
    return stop, tp


def synthesize_stop(side: str, entry: float, stop_pct: float) -> Optional[float]:
    """Conservative flat fallback: entry ∓ stop_pct%. Wider-is-safer doctrine
    (harness sizes by stop distance → too-wide UNDERsizes the mirror)."""
    if entry <= 0 or stop_pct <= 0 or side not in ("long", "short"):
        return None
    frac = float(stop_pct) / 100.0
    return entry * (1.0 - frac) if side == "long" else entry * (1.0 + frac)


def build_signal_record(symbol: str, side: str, entry_ref: float,
                        stop_price: float, tp1_price: Optional[float],
                        ts_ms: int, position_id: str) -> Optional[Dict[str, Any]]:
    """The M4 outbox contract (+ arcus_position_id extension). Returns None —
    never emit — when the geometry is degenerate (naked signals forbidden).
    TP failing its sanity side-check is dropped to null, not fatal."""
    if side not in ("long", "short"):
        return None
    try:
        e, s = float(entry_ref), float(stop_price)
    except (TypeError, ValueError):
        return None
    if e <= 0 or s <= 0:
        return None
    if side == "long" and s >= e:
        return None
    if side == "short" and s <= e:
        return None
    tp: Optional[float] = None
    if tp1_price is not None:
        try:
            t = float(tp1_price)
            if t > 0 and ((side == "long" and t > e) or (side == "short" and t < e)):
                tp = round(t, 8)
        except (TypeError, ValueError):
            tp = None
    return {
        "id": f"{symbol}_{side}_{int(ts_ms)}",
        "symbol": symbol,
        "side": side,
        "entry_ref": round(e, 8),
        "stop_price": round(s, 8),
        "tp1_price": tp,
        "coherence": COHERENCE_SENTINEL,
        "ts_ms": int(ts_ms),
        "src": SRC,
        "arcus_position_id": position_id,
    }


# ── Dedup state (atomic, tolerant) ───────────────────────────────────────────

class ArcusBroadcastState:
    """seen: {"SYMBOL|side": {"position_id": str, "first_seen_ms": int}}.
    Atomic tmp+replace; corrupt/missing file = cold start, never a crash."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.seen: Dict[str, Dict[str, Any]] = {}

    def load(self) -> int:
        try:
            with open(self.path) as f:
                doc = json.load(f)
            rows = doc.get("seen") if isinstance(doc, dict) else None
            if isinstance(rows, dict):
                self.seen = {str(k): dict(v) for k, v in rows.items()
                             if isinstance(v, dict) and v.get("position_id")}
        except Exception:
            self.seen = {}
        return len(self.seen)

    def persist(self) -> None:
        """Best-effort — a persist failure must never kill the poll loop;
        last-good in-memory state keeps deduping this process."""
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"seen": self.seen}, f)
            os.replace(tmp, self.path)
        except Exception as e:
            logger.warning("arcus_broadcast_state_persist_failed",
                           error=str(e)[:120])


# ── The poller ───────────────────────────────────────────────────────────────

class ArcusSignalPoller:
    """Observer-class: inject the venue client (duck-typed: get_positions /
    get_open_orders), own the outbox append + dedup state. poll_once() NEVER
    raises — fail-closed per the build order."""

    def __init__(self, client: Any, outbox_path: str, state_path: str,
                 symbols: Optional[frozenset] = None,
                 poll_interval_s: Optional[float] = None,
                 stop_pct: Optional[float] = None) -> None:
        self.client = client
        self.outbox_path = outbox_path
        self.state = ArcusBroadcastState(state_path)
        self.symbols = symbols          # None = no filter
        self.poll_interval_s = float(
            poll_interval_s
            if poll_interval_s is not None
            else os.environ.get("ARCUS_BROADCAST_POLL_S", DEFAULT_POLL_S)
            or DEFAULT_POLL_S)
        self.stop_pct = float(
            stop_pct
            if stop_pct is not None
            else os.environ.get("ARCUS_BROADCAST_STOP_PCT", DEFAULT_STOP_PCT)
            or DEFAULT_STOP_PCT)
        self._warn_last = 0.0
        self.state.load()

    def _filter(self) -> Optional[frozenset]:
        return env_symbol_filter() or self.symbols

    def _throttled_warn(self, event: str, **fields: Any) -> None:
        now = time.time()
        if now - self._warn_last >= _WARN_THROTTLE_S:
            self._warn_last = now
            logger.warning(event, **fields)

    def _append_outbox(self, record: Dict[str, Any]) -> None:
        """Append-only doctrine: one complete line, never a rewrite."""
        os.makedirs(os.path.dirname(self.outbox_path) or ".", exist_ok=True)
        with open(self.outbox_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")

    async def poll_once(self, now_ms: Optional[int] = None) -> Dict[str, Any]:
        """One poll cycle. Returns a summary dict (telemetry/tests)."""
        if not (broadcast_enabled() and outbox_enabled()):
            return {"enabled": False, "emitted": 0}
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        try:
            positions = await self.client.get_positions()
            orders = await self.client.get_open_orders()
        except Exception as e:
            # Fail-closed: venue read failed → skip the cycle, keep state.
            self._throttled_warn("arcus_broadcast_poll_failed",
                                 error=str(e)[:160])
            return {"enabled": True, "emitted": 0, "error": str(e)[:160]}

        allow = self._filter()
        current_keys = set()
        emitted = 0
        changed = False
        for pos in positions or []:
            key = position_key(pos)
            if key is None:
                continue
            sym, side = key
            if allow is not None and sym not in allow:
                continue
            current_keys.add(key)
            skey = f"{sym}|{side}"
            if skey in self.state.seen:
                continue                    # already broadcast this position
            entry = float(pos.get("entry") or pos.get("avgEntryPrice") or 0.0)
            pid = f"arcus_{sym}_{side}_{int(now_ms)}"
            stop, tp = infer_tpsl(pos, orders or [])
            stop_source = "native"
            if stop is None:
                stop = synthesize_stop(side, entry, self.stop_pct)
                stop_source = "synthesized"
            rec = build_signal_record(sym, side, entry, stop, tp, now_ms, pid)
            # Mark seen EITHER WAY — a degenerate row must not spam every poll.
            self.state.seen[skey] = {"position_id": pid,
                                     "first_seen_ms": int(now_ms)}
            changed = True
            if rec is None:
                logger.warning("arcus_broadcast_skipped", symbol=sym, side=side,
                               reason="no_stop_geometry", entry=entry)
                continue
            try:
                self._append_outbox(rec)
            except Exception as e:
                self._throttled_warn("arcus_broadcast_emit_failed",
                                     symbol=sym, error=str(e)[:160])
                continue
            emitted += 1
            logger.info("arcus_broadcast_emitted", symbol=sym, side=side,
                        entry_ref=rec["entry_ref"], stop_price=rec["stop_price"],
                        tp1_price=rec["tp1_price"], stop_source=stop_source,
                        arcus_position_id=pid)
        # Clear keys whose positions disappeared → next fill rebroadcasts.
        stale = [k for k in self.state.seen
                 if tuple(k.rsplit("|", 1)) not in current_keys
                 and (allow is None or k.rsplit("|", 1)[0] in allow)]
        for k in stale:
            self.state.seen.pop(k, None)
            changed = True
        if changed:
            self.state.persist()
        return {"enabled": True, "emitted": emitted,
                "tracked": len(self.state.seen)}
