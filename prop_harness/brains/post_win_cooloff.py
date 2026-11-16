"""Post-win same-direction cooloff (prop harness only).

After ANY qualifying win close, refuse same-direction re-entries in the
same asset class for a short TTL. The 2026-11-15 turbo cluster: small
wins (+$1.5-5 class) were immediately re-spent into same-direction
re-entries at +7/+15/+22 min — crossing symbol families (ETH majors,
ONDO defi, ARB l1_alt) — each paying full taker fees on the way in and
out. pwr could not catch it: its $25/0.70R trigger sat above the $7-risk
tickets and its $15 respend cap still ALLOWED the re-entries.

This latch is an outright block, keyed on (asset_class, side) — not
symbol, not family — because the toxic pattern is directional re-spend,
not symbol repetition. The sleeve's proven serial winners (11-14 BTC
+12.71/+24.85/+5.76/+10.95 spaced ≥42 min) sit OUTSIDE the TTL and are
untouched by construction.

Design: sliding latch (each qualifying win re-arms), lazy expiry,
persistent via the executor. Kill switch PROP_POST_WIN_COOLOFF=0 →
legacy bit-for-bit. No Kant / Nietzsche / Chancellor / tiers 1–6.
"""

from __future__ import annotations

from typing import Any

COOLOFF_BLOCK = "post_win_cooloff_standdown"

DEFAULT_MIN_WIN_USD = 1.5
DEFAULT_MIN_WIN_R = 0.20
DEFAULT_TTL_S = 1800.0  # 30 min — toxic respends at +7/+15/+22 min


def class_of(asset: str | None) -> str:
    a = (asset or "").strip()
    return "tradfi" if a.startswith("xyz:") else "crypto"


def latch_key(asset_class: str | None, side: str | None) -> str:
    return "%s:%s" % ((asset_class or "crypto").lower(),
                      (side or "").lower())


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def win_qualifies(
        realized_pnl: Any,
        risk_usd: Any,
        *,
        min_win_usd: float = DEFAULT_MIN_WIN_USD,
        min_win_r: float = DEFAULT_MIN_WIN_R,
) -> bool:
    """Arm when the win clears max($ floor, min_win_r × risk)."""
    pnl = _f(realized_pnl)
    if pnl is None or pnl <= 0:
        return False
    try:
        floor = float(min_win_usd or 0.0)
    except (TypeError, ValueError):
        floor = DEFAULT_MIN_WIN_USD
    risk = _f(risk_usd) or 0.0
    try:
        r_mult = float(min_win_r or 0.0)
    except (TypeError, ValueError):
        r_mult = DEFAULT_MIN_WIN_R
    return pnl >= max(floor, r_mult * max(0.0, risk))


def arm_latch(
        latches: dict[str, dict],
        *,
        asset_class: str,
        side: str | None,
        realized_pnl: float,
        risk_usd: Any,
        now: float,
        ttl_s: float = DEFAULT_TTL_S,
        semantic_id: str | None = None,
) -> dict[str, dict]:
    """Sliding re-arm: a fresh qualifying win extends the latch."""
    try:
        ttl = float(ttl_s or 0.0)
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL_S
    key = latch_key(asset_class, side)
    prior = latches.get(key) or {}
    latches[key] = {
        "armed": True,
        "asset_class": asset_class,
        "side": (side or "").lower(),
        "win_usd": float(realized_pnl),
        "win_risk_usd": _f(risk_usd),
        "armed_at": float(now),
        "expires_at": float(now) + max(0.0, ttl),
        "ttl_s": max(0.0, ttl),
        "wins": int(prior.get("wins") or 0) + 1,
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
    """Block same-(class, side) entries while the latch is hot.

    Returns (reason_or_None, detail). Kill off / no latch / latch
    expired → allow. Fail-closed ONLY on the enabled+latch-hot path.
    """
    detail: dict[str, Any] = {}
    if not enabled:
        return None, detail
    if not isinstance(latches, dict) or not latches:
        return None, detail
    key = latch_key(class_of(asset), side)
    latch = latches.get(key)
    if not latch_active(latch, now):
        return None, detail
    assert latch is not None
    detail = _block_detail(latch, asset, side, now)
    return COOLOFF_BLOCK, detail


def note_cooloff_block(latches: dict[str, dict],
                       asset: str,
                       side: str | None) -> dict[str, dict]:
    key = latch_key(class_of(asset), side)
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
        "asset_class": latch.get("asset_class"),
        "latch_side": latch.get("side"),
        "win_usd": latch.get("win_usd"),
        "win_risk_usd": latch.get("win_risk_usd"),
        "wins": latch.get("wins"),
        "ttl_remaining_s": max(0.0, exp - float(now)),
        "expires_at": exp,
        "semantic_id": latch.get("semantic_id"),
    }
