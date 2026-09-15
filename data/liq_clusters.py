"""
data/liq_clusters.py — Liquidation-cluster CAPTURE PLANE (observer-class).

Agent E build, 2026-09-15, Bybit AI cascade-aftermath ground-truth commission:
raw LiquidationSignal events flow through main.py on_liquidation_signal
(venues: valuechain/bybit/aster — each carrying symbol, direction,
notional_usd, timestamp) and are DISCARDED after the Tier-6/coherence reads.
No liquidation persistence existed. This plane captures them.

Model: events within a 60s gap (keyed per symbol+direction) accumulate into
an open cluster; a gap >60s flushes the completed cluster as ONE append-only
JSONL row:

  {ts_start_ms, ts_end_ms, symbol, direction, venue_set,
   total_notional, tier, event_count, max_single_notional}

The row is the ground truth the cascade-aftermath forward-path model
(tools/liq_cluster_stats.py) consumes: cluster notional, tier, print time →
coin-flip crossover minute.

House doctrines (mandatory):
  - Append-only JSONL, one-bad-line read. NEVER sqlite (the 2026-07-26
    calendar.db "disk image malformed" precedent is why).
  - Zero-I/O pure brain (LiqClusterBuilder): no clocks, no disk — every
    timestamp is injected. The I/O wrapper (append_cluster) is thin.
  - Env kill switch read per call: LIQ_CLUSTER_ENABLED="false" = module
    inert (ingest accumulates nothing, append writes nothing).

Observer-class: zero trade-path wiring, zero gate changes. The one-line tap
in main.py is spliced by the local orchestrator, not by this module.

Tier table (Bybit AI's BTC-row table, Governor-stamped as the market-level
default): small <$500K, medium $500K–$2M, large $2M–$5M, whale >=$5M.
symbol_kind is reserved for per-class tables (v2); ignored for v1.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

SCHEMA_VERSION = 1

# Tier boundaries (USD notional, Governor-stamped default table).
_TIER_SMALL_MAX = 500_000.0      # small  < $500K
_TIER_MEDIUM_MAX = 2_000_000.0   # medium < $2M
_TIER_LARGE_MAX = 5_000_000.0    # large  < $5M; whale >= $5M

_GAP_MS = 60_000                 # events within 60s = same cluster
_STALE_MS = 120_000              # open cluster silent >120s = drainable


def liq_cluster_enabled() -> bool:
    """Kill switch, read per call: 'false' = module inert."""
    return os.environ.get("LIQ_CLUSTER_ENABLED", "true").strip().lower() != "false"


def classify_cluster(notional_usd: float, symbol_kind: str = "major") -> str:
    """Tier by USD notional. Never raises; non-positive → 'small'."""
    try:
        n = float(notional_usd)
    except (TypeError, ValueError):
        return "small"
    if n < _TIER_SMALL_MAX:
        return "small"
    if n < _TIER_MEDIUM_MAX:
        return "medium"
    if n < _TIER_LARGE_MAX:
        return "large"
    return "whale"


def _new_cluster(ts_ms: int, symbol: str, direction: str,
                 notional: float, venue: str) -> dict:
    return {"ts_start_ms": ts_ms, "ts_end_ms": ts_ms, "last_ts": ts_ms,
            "symbol": symbol, "direction": direction,
            "venues": {venue}, "total_notional": notional,
            "event_count": 1, "max_single_notional": notional}


def _cluster_row(c: dict) -> dict:
    return {"schema": SCHEMA_VERSION,
            "ts_start_ms": c["ts_start_ms"],
            "ts_end_ms": c["ts_end_ms"],
            "symbol": c["symbol"],
            "direction": c["direction"],
            "venue_set": sorted(c["venues"]),
            "total_notional": round(c["total_notional"], 2),
            "tier": classify_cluster(c["total_notional"]),
            "event_count": c["event_count"],
            "max_single_notional": round(c["max_single_notional"], 2)}


class LiqClusterBuilder:
    """Zero-I/O cluster accumulator. All timestamps injected; never raises.

    ingest() returns the flushed cluster row ONLY when an event arrives
    >60s after the open cluster's last event (None while a cluster is open
    or the event is ignored). flush_all(now_ms) drains stale open clusters
    (>120s since their last event) for shutdown/periodic drain.
    """

    def __init__(self) -> None:
        self._open: Dict[Tuple[str, str], dict] = {}

    def ingest(self, ts_ms: int, symbol: str, direction: str,
               notional_usd: float, venue: str) -> Optional[dict]:
        try:
            if not liq_cluster_enabled():
                return None
            ts_ms = int(ts_ms)
            notional = float(notional_usd)
            if ts_ms <= 0 or notional <= 0:
                return None
            symbol = str(symbol or "").strip()
            direction = str(direction or "").strip()
            if not symbol or not direction:
                return None
            venue = str(venue or "unknown").strip() or "unknown"

            key = (symbol, direction)
            cur = self._open.get(key)
            if cur is not None and ts_ms - cur["last_ts"] > _GAP_MS:
                row = _cluster_row(cur)
                self._open[key] = _new_cluster(ts_ms, symbol, direction,
                                               notional, venue)
                return row
            if cur is None:
                self._open[key] = _new_cluster(ts_ms, symbol, direction,
                                               notional, venue)
            else:
                cur["venues"].add(venue)
                cur["total_notional"] += notional
                cur["event_count"] += 1
                cur["max_single_notional"] = max(cur["max_single_notional"],
                                                 notional)
                cur["last_ts"] = max(cur["last_ts"], ts_ms)
                cur["ts_end_ms"] = max(cur["ts_end_ms"], ts_ms)
                cur["ts_start_ms"] = min(cur["ts_start_ms"], ts_ms)
            return None
        except Exception:
            return None

    def flush_all(self, now_ms: int) -> List[dict]:
        """Drain open clusters whose last event is >120s stale. Never raises."""
        rows: List[dict] = []
        try:
            now_ms = int(now_ms)
            for key in list(self._open.keys()):
                cur = self._open.get(key)
                if cur is None:
                    continue
                if now_ms - cur["last_ts"] > _STALE_MS:
                    rows.append(_cluster_row(cur))
                    self._open.pop(key, None)
        except Exception:
            pass
        return rows

    def open_count(self) -> int:
        return len(self._open)


# ── Thin I/O wrapper (the only disk in the module) ───────────────────────────

def append_cluster(path: str, row: dict) -> bool:
    """Append one cluster row as one JSONL line. Kill-switch gated; never
    raises — a row that fails to write costs that row, never the caller."""
    if not liq_cluster_enabled():
        return False
    try:
        with open(path, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
        return True
    except Exception:
        return False


def read_clusters(path: str) -> List[dict]:
    """Tolerant reader — one bad line costs one record, never the file."""
    rows: List[dict] = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    if isinstance(rec, dict):
                        rows.append(rec)
                except Exception:
                    continue   # one-bad-line doctrine
    except Exception:
        pass
    return rows
