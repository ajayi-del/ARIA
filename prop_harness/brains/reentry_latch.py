"""Re-entry close-epoch latch (prop harness only).

After a STOP-CLASS or external close, the registry row carries closed_ts;
re-entering the same (symbol, side) within the TTL is refused. This is
the machine forgetting how recently the venue proved the entry wrong —
the turbo-class wound where a stopped bracket is re-fired seconds later
against the same level.

Pure verdict over registry rows (zero I/O — the harness passes the row
list in). Rows without closed_ts are legacy-valid and never latch.
Kill switch PROP_REENTRY_LATCH=0 → legacy re-entry flow bit-for-bit.
"""

from __future__ import annotations

from typing import Any

REENTRY_BLOCK = "reentry_latch_block"

DEFAULT_TTL_S = 1800.0


def norm_asset(asset: str | None) -> str:
    """WHY: xyz: prefix must not fork the (symbol, side) latch keyspace."""
    a = (asset or "").strip()
    return a.split(":", 1)[1] if a.startswith("xyz:") else a


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def reentry_verdict(
        enabled: bool,
        rows: list[dict] | None,
        symbol: str,
        side: str | None,
        now: float,
        *,
        ttl_s: float = DEFAULT_TTL_S,
) -> tuple[str | None, dict[str, Any]]:
    """Refuse same (symbol, side) re-entry inside the close-epoch TTL.

    Returns (reason_or_None, detail). Kill off / no matching closed row /
    latch expired / legacy rows (no closed_ts) → allow. Fail-closed ONLY
    on the enabled + fresh-close path.
    """
    detail: dict[str, Any] = {}
    if not enabled:
        return None, detail
    if not isinstance(rows, list) or not rows:
        return None, detail
    try:
        ttl = float(ttl_s or 0.0)
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL_S
    if ttl <= 0:
        return None, detail
    want_sym = norm_asset(symbol)
    want_side = (side or "").lower()
    best_ts: float | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        # Registry stop-class/external closes stamp closed_ts; the live
        # status vocab is "closed_external" (legacy "closed" accepted).
        if row.get("status") not in ("closed", "closed_external"):
            continue
        if norm_asset(row.get("asset") or row.get("symbol")) != want_sym:
            continue
        if str(row.get("side") or "").lower() != want_side:
            continue
        closed_ts = _f(row.get("closed_ts"))
        if closed_ts is None:
            continue  # legacy row — never latches
        if best_ts is None or closed_ts > best_ts:
            best_ts = closed_ts
    if best_ts is None:
        return None, detail
    age = float(now) - best_ts
    if age >= ttl or age < 0:
        return None, detail
    detail = {
        "reason": REENTRY_BLOCK,
        "asset": symbol,
        "side": want_side,
        "last_close_ts": best_ts,
        "age_s": age,
        "ttl_remaining_s": ttl - age,
    }
    return REENTRY_BLOCK, detail
