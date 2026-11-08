"""intelligence/sodex_mark_rest_seed.py — SoDEX REST mark → MarkPriceStore seed
(QNT / thin-book P0, audit docs/qnt-tradfi-coherence-audit.md §1).

WHY: SystemStateManager readiness requires mark_healthy within SoDEX
mark_slack (interpreter historically 500ms). Thin SoDEX books (QNT-USD)
get sparse WS `markPrice` pushes → age ≫ 500ms at candle ticks →
permanent WARMING_UP / never `symbol_ready`. REST GET /markets/mark-prices
already includes these marks (QNT live-verified) and the funding_loop
already polls that endpoint for rates + equity OI — but never wrote the
MarkPriceStore.

MECHANISM — PIGGYBACK + SLACK BIND (equity_oi_plane idiom):
  - Zero new API cost: seed from the same stashed mark-prices rows the
    funding_loop already fetched (ws_manager.latest_mark_price_rows).
  - When enabled, SoDEX mark_slack widens to the poll cadence + buffer
    (default 310_000ms = 300s funding interval + 10s) so a seed keeps
    mark_healthy until the next poll. Aster keeps its own 5s slack.
  - Aster-routed symbols are excluded (AsterFeed owns their mark plane).
  - Kill switch OFF = pre-fix bit-for-bit (no seed, SoDEX slack 500ms).

Kill switches (env):
  SODEX_MARK_REST_SEED_ENABLED  default "true"
  SODEX_MARK_REST_SEED_SLACK_MS default "310000" (ignored when disabled)
"""
from __future__ import annotations

import math
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

# ── Kill switches ─────────────────────────────────────────────────────────────

_DEFAULT_SLACK_MS = 310_000  # funding_loop 300s + 10s buffer
_LEGACY_SODEX_SLACK_MS = 500


def sodex_mark_rest_seed_enabled() -> bool:
    """Env kill switch (SODEX_MARK_REST_SEED_ENABLED, default "true")."""
    return os.environ.get("SODEX_MARK_REST_SEED_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on")


def sodex_mark_slack_ms() -> int:
    """SoDEX mark_healthy slack for the interpreter.

    Enabled → REST-seed binding (default 310s). Disabled → legacy 500ms.
    Aster path is unchanged (caller still hard-codes 5000 for aster_assets).
    """
    if not sodex_mark_rest_seed_enabled():
        return _LEGACY_SODEX_SLACK_MS
    raw = os.environ.get("SODEX_MARK_REST_SEED_SLACK_MS", str(_DEFAULT_SLACK_MS))
    try:
        v = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return _DEFAULT_SLACK_MS
    return v if v > 0 else _DEFAULT_SLACK_MS


# ── Telemetry contract (splice emits these) ───────────────────────────────────

EVENT_SEEDED = "sodex_mark_rest_seeded"
EVENT_FAILED = "sodex_mark_rest_seed_failed"


# ── Wire parser (pure) ────────────────────────────────────────────────────────

def parse_mark_price_rows(payload) -> List[Tuple[str, float, float]]:
    """Normalize mark-prices response to [(symbol, mark, index)].

    Accepts Governor-verified bare JSON LIST and {"data"/"rows"/"result"}
    wrappers. markPrice required; indexPrice falls back to mark (WS path
    does the same when `i` is absent). One bad row kills only itself.
    """
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("rows") or payload.get("result") or []
    elif isinstance(payload, (list, tuple)):
        rows = payload
    else:
        rows = []
    out: List[Tuple[str, float, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = row.get("symbol") or row.get("s")
        try:
            mark = float(row.get("markPrice", row.get("p")))
        except (TypeError, ValueError):
            continue
        if not sym or not math.isfinite(mark) or mark <= 0.0:
            continue
        try:
            index = float(row.get("indexPrice", row.get("i", mark)))
        except (TypeError, ValueError):
            index = mark
        if not math.isfinite(index) or index <= 0.0:
            index = mark
        out.append((str(sym).strip(), mark, index))
    return out


# ── Seed (pure aside from store.update side-effect) ───────────────────────────

def seed_mark_stores(
    stores: Dict[str, Any],
    rows,
    *,
    now_ms: Optional[int] = None,
    assets: Optional[Iterable[str]] = None,
    exclude: Optional[Iterable[str]] = None,
) -> int:
    """Write REST marks into MarkPriceStore. Returns count of stores updated.

    Fail-closed: kill switch off → 0; garbage rows skipped; missing store
    skipped; exclude wins over assets. Never fabricates a mark.
    """
    if not sodex_mark_rest_seed_enabled():
        return 0
    if not stores:
        return 0
    parsed = parse_mark_price_rows(rows)
    if not parsed:
        return 0
    allow: Optional[Set[str]] = set(assets) if assets is not None else None
    deny: Set[str] = set(exclude) if exclude is not None else set()
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    n = 0
    for sym, mark, index in parsed:
        if sym in deny:
            continue
        if allow is not None and sym not in allow:
            continue
        store = stores.get(sym)
        if store is None:
            continue
        try:
            store.update(mark, index, ts)
        except Exception:
            continue
        n += 1
    return n
