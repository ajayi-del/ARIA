"""Post-loss same-(asset, side) cooloff (prop harness only).

After ANY losing close (realized pnl < 0), refuse same-side re-entries on
the SAME asset for a short TTL. The 2026-11-17 quant finding: the lethal
loop is not symbol churn across families — it is the machine re-entering
the exact (asset, side) that just lost, paying full taker fees on the
revenge re-entry minutes later.

Keyed on (normalized asset, side) — xyz: prefix stripped so GOLD and
xyz:GOLD share one latch. Complements the post-win cooloff (which keys
on asset CLASS): this latch is the loss-side mirror at asset granularity.

Design: sliding latch (each losing close re-arms), lazy expiry,
persistent via the harness state root. Kill switch
PROP_POST_LOSS_COOLOFF=0 → legacy bit-for-bit. No Kant / Nietzsche /
Chancellor / tiers 1–6.
"""

from __future__ import annotations

from typing import Any

COOLOFF_BLOCK = "post_loss_cooloff_block"

DEFAULT_TTL_S = 1800.0


def norm_asset(asset: str | None) -> str:
    """WHY: the venue prefix (xyz:) must not fork the latch keyspace."""
    a = (asset or "").strip()
    return a.split(":", 1)[1] if a.startswith("xyz:") else a


def latch_key(asset: str | None, side: str | None) -> str:
    return "%s:%s" % (norm_asset(asset), (side or "").lower())


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def loss_qualifies(realized_pnl: Any) -> bool:
    """Arm on ANY losing close."""
    pnl = _f(realized_pnl)
    return pnl is not None and pnl < 0


def arm_latch(
        latches: dict[str, dict],
        *,
        asset: str,
        side: str | None,
        realized_pnl: float,
        now: float,
        ttl_s: float = DEFAULT_TTL_S,
        semantic_id: str | None = None,
) -> dict[str, dict]:
    """Sliding re-arm: a fresh losing close extends the latch."""
    try:
        ttl = float(ttl_s or 0.0)
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL_S
    key = latch_key(asset, side)
    prior = latches.get(key) or {}
    latches[key] = {
        "armed": True,
        "asset": norm_asset(asset),
        "side": (side or "").lower(),
        "loss_usd": float(realized_pnl),
        "armed_at": float(now),
        "expires_at": float(now) + max(0.0, ttl),
        "ttl_s": max(0.0, ttl),
        "losses": int(prior.get("losses") or 0) + 1,
        "blocks": int(prior.get("blocks") or 0),
        "semantic_id": semantic_id,
    }
    return latches


def prune_expired(latches: dict[str, dict],
                  now: float) -> dict[str, dict]:
    return {k: v for k, v in latches.items() if latch_active(v, now)}


def latch_active(latch: dict | None, now: float) -> bool:
    if not isinstance(latch, dict) or not latch.get("armed"):
        return False
    exp = _f(latch.get("expires_at"))
    if exp is None:
        return False
    return float(now) < exp


def cooloff_verdict(
        enabled: bool,
        latches: dict[str, dict] | None,
        asset: str,
        side: str | None,
        now: float,
) -> tuple[str | None, dict[str, Any]]:
    """Block same-(asset, side) entries while the latch is hot.

    Returns (reason_or_None, detail). Kill off / no latch / latch
    expired → allow. Fail-closed ONLY on the enabled+latch-hot path.
    """
    detail: dict[str, Any] = {}
    if not enabled:
        return None, detail
    if not isinstance(latches, dict) or not latches:
        return None, detail
    key = latch_key(asset, side)
    latch = latches.get(key)
    if not latch_active(latch, now):
        return None, detail
    assert latch is not None
    detail = _block_detail(latch, asset, side, now)
    return COOLOFF_BLOCK, detail


def note_cooloff_block(latches: dict[str, dict],
                       asset: str,
                       side: str | None) -> dict[str, dict]:
    key = latch_key(asset, side)
    latch = latches.get(key)
    if isinstance(latch, dict):
        latch["blocks"] = int(latch.get("blocks") or 0) + 1
    return latches


def _block_detail(latch: dict, asset: str,
                  side: str | None, now: float) -> dict[str, Any]:
    exp = _f(latch.get("expires_at")) or 0.0
    return {
        "reason": COOLOFF_BLOCK,
        "asset": asset,
        "side": (side or "").lower(),
        "latch_asset": latch.get("asset"),
        "latch_side": latch.get("side"),
        "loss_usd": latch.get("loss_usd"),
        "losses": latch.get("losses"),
        "ttl_remaining_s": max(0.0, exp - float(now)),
        "expires_at": exp,
        "semantic_id": latch.get("semantic_id"),
    }
