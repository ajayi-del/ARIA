"""Post-win re-spend latch (prop harness only).

After a large right-tail crypto win (BTC short class), refuse immediate
same-factor / β-proximate crypto re-entries that re-spend the gain into
NEAR/SOL-class fast full-R losses.

Specimen (turbo 2026-11-08): BTC short +$51.2 @ ~$50 risk → SOL −$38
(~7m) → NEAR −$35 (~80m). Gross edge existed; correlated re-spend +
taker fees destroyed it.

Design: TTL + family/β proximity + max consecutive re-spend notional.
Journal every would-block. Kill switch PROP_POST_WIN_RESPEND=0 → legacy.
No Kant / Nietzsche / Chancellor / tiers 1–6.
"""

from __future__ import annotations

from typing import Any

from . import crypto_comove as ccm

RESPEND_BLOCK = "post_win_respend"

# Families that ride the same crypto factor as majors (corr ≈0.85–0.90).
# SOL=large_alt, NEAR=l1_alt, BTC=majors — all proximate for this latch.
CRYPTO_BETA_CLUSTER = frozenset({
    "majors", "large_alt", "l1_alt", "exchange", "defi", "ai",
})

# Defaults (Θ / TTL choice documented in ship doc).
DEFAULT_MIN_WIN_USD = 25.0
DEFAULT_MIN_WIN_R = 0.70
DEFAULT_MIN_RISK_FOR_R = 20.0
DEFAULT_COH_BIG = 7.0
DEFAULT_TTL_S = 5400.0          # 90m — covers NEAR ~80m after BTC1
DEFAULT_MAX_RESPEND_USD = 15.0  # SOL $32 / NEAR $30 both exceed


def beta_proximate(winner_family: str | None,
                   cand_family: str | None) -> bool:
    """True when candidate shares family or both sit in the β-cluster."""
    wf = (winner_family or "").strip()
    cf = (cand_family or "").strip()
    if not wf or not cf:
        return False
    if wf == cf:
        return True
    return wf in CRYPTO_BETA_CLUSTER and cf in CRYPTO_BETA_CLUSTER


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
        coherence: Any = None,
        *,
        min_win_usd: float = DEFAULT_MIN_WIN_USD,
        min_win_r: float = DEFAULT_MIN_WIN_R,
        min_risk_for_r: float = DEFAULT_MIN_RISK_FOR_R,
        coh_big: float = DEFAULT_COH_BIG,
) -> bool:
    """Arm when absolute $ win OR high-coh R-multiple on a real ticket.

    Arm A: realized ≥ min_win_usd (BTC +$42–51 class).
    Arm B: coh>coh_big ∧ risk≥min_risk_for_r ∧ pnl/risk ≥ min_win_r.
    """
    pnl = _f(realized_pnl)
    if pnl is None or pnl <= 0:
        return False
    try:
        floor = float(min_win_usd or 0.0)
    except (TypeError, ValueError):
        floor = DEFAULT_MIN_WIN_USD
    if pnl >= floor:
        return True
    risk = _f(risk_usd)
    coh = _f(coherence)
    if risk is None or risk < float(min_risk_for_r or 0.0):
        return False
    if coh is None or not (coh > float(coh_big or 0.0)):
        return False
    return (pnl / risk) >= float(min_win_r or 0.0)


def build_latch(
        *,
        asset: str,
        family: str,
        side: str | None,
        realized_pnl: float,
        risk_usd: Any,
        coherence: Any,
        now: float,
        ttl_s: float = DEFAULT_TTL_S,
        max_respend_usd: float = DEFAULT_MAX_RESPEND_USD,
        semantic_id: str | None = None,
) -> dict[str, Any]:
    """Fresh latch state after a qualifying win close."""
    win = float(realized_pnl)
    try:
        cap = max(0.0, float(max_respend_usd or 0.0))
    except (TypeError, ValueError):
        cap = DEFAULT_MAX_RESPEND_USD
    # Cap re-spend at min(configured, 30% of win). After +$51 → ≤$15;
    # SOL/NEAR $30–32 tickets both die. Floor at $7 so one probe can clear.
    frac_cap = 0.30 * win if win > 0 else cap
    effective = min(cap, frac_cap) if frac_cap > 0 else cap
    if effective > 0 and cap >= 7.0:
        effective = max(7.0, effective)
    try:
        ttl = float(ttl_s or 0.0)
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL_S
    return {
        "armed": True,
        "winner_asset": asset,
        "winner_family": family,
        "winner_side": side,
        "win_usd": win,
        "win_risk_usd": _f(risk_usd),
        "win_coherence": _f(coherence),
        "armed_at": float(now),
        "expires_at": float(now) + max(0.0, ttl),
        "ttl_s": max(0.0, ttl),
        "max_respend_usd": effective,
        "respend_committed": 0.0,
        "blocks": 0,
        "semantic_id": semantic_id,
    }


def latch_active(latch: dict | None, now: float) -> bool:
    if not isinstance(latch, dict) or not latch.get("armed"):
        return False
    exp = _f(latch.get("expires_at"))
    if exp is None:
        return False
    return float(now) < exp


def respend_verdict(
        enabled: bool,
        latch: dict | None,
        asset: str,
        family: str,
        risk_usd: Any,
        now: float,
        *,
        asset_class: str = "crypto",
) -> tuple[str | None, dict[str, Any]]:
    """Refuse β-proximate crypto when remaining re-spend budget is too small.

    Returns (reason_or_None, detail). detail always filled when a block
    would fire so the caller can journal would-block telemetry.
    Kill off / tradfi sleeve / non-crypto / latch expired → allow.
    """
    detail: dict[str, Any] = {}
    if not enabled:
        return None, detail
    sleeve = (asset_class or "all").lower()
    if sleeve == "tradfi":
        return None, detail
    if not ccm.is_crypto_asset(asset):
        return None, detail
    if not latch_active(latch, now):
        return None, detail
    assert latch is not None
    wf = str(latch.get("winner_family") or "")
    if not beta_proximate(wf, family):
        return None, detail
    risk = _f(risk_usd)
    if risk is None or risk <= 0:
        # Degenerate risk — fail-closed refuse while latched+proximate.
        detail = _block_detail(latch, asset, family, risk, now, remaining=0.0)
        return RESPEND_BLOCK, detail
    committed = _f(latch.get("respend_committed")) or 0.0
    cap = _f(latch.get("max_respend_usd")) or 0.0
    remaining = max(0.0, cap - committed)
    detail = _block_detail(latch, asset, family, risk, now, remaining=remaining)
    if risk > remaining + 1e-9:
        return RESPEND_BLOCK, detail
    return None, detail


def note_respend_commit(latch: dict, risk_usd: Any) -> dict:
    """Accumulate committed proximate risk after an allowed place."""
    risk = _f(risk_usd) or 0.0
    if risk <= 0:
        return latch
    committed = (_f(latch.get("respend_committed")) or 0.0) + risk
    latch["respend_committed"] = committed
    return latch


def note_respend_block(latch: dict) -> dict:
    latch["blocks"] = int(latch.get("blocks") or 0) + 1
    return latch


def _block_detail(latch: dict, asset: str, family: str, risk: float | None,
                  now: float, *, remaining: float) -> dict[str, Any]:
    exp = _f(latch.get("expires_at")) or 0.0
    return {
        "reason": RESPEND_BLOCK,
        "asset": asset,
        "family": family,
        "risk_usd": risk,
        "remaining_respend_usd": remaining,
        "max_respend_usd": latch.get("max_respend_usd"),
        "respend_committed": latch.get("respend_committed"),
        "winner_asset": latch.get("winner_asset"),
        "winner_family": latch.get("winner_family"),
        "win_usd": latch.get("win_usd"),
        "ttl_remaining_s": max(0.0, exp - float(now)),
        "expires_at": exp,
    }
