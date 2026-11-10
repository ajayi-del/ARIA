"""Cost-to-expected-move gate (prop harness only).

Refuse when round-trip taker fee estimate / expected move $ ≥ Θ.
Complements PROP_EVAL_CRYPTO_COST_HARD_CAP (which RAISES the fee/risk
allowance up to 0.30 so ATR probes can clear): this gate is a hard
ceiling that refuses fee-dominated geometry without mutating allowance.

Estimate: fee_est = 2 × taker_rate × notional (turbo is 100% taker —
no maker fantasy). Expected move $ = risk_usd (stop×qty) as the
entry-time proxy; optional atr_usd overrides when a plane is live.

Θ choice (2026-11-10 nine-trade counterfactual):
  Θ = 0.25 — would-block ≤~0.36% stop dust (ratio≥0.25) while BTC
  right-tail shorts at ~0.5–1% stops (ratio ~0.09–0.18) stay clear.
  NEAR/SOL full-R (~0.5%, ratio~0.18) also clear here — those are the
  post-win re-spend latch's job. ATS-39 hard-cap 0.30 intentionally
  clears ~0.35% ATR probes, so LIVE defaults OFF (shadow from birth);
  arm PROP_COST_MOVE_LIVE=1 only after would-block census.

Kill switch PROP_COST_MOVE_GATE=0 → legacy. PROP_COST_MOVE_LIVE=0 →
shadow would-block only (default). Missing fee/stop/risk → abstain.
No Kant / Nietzsche / Chancellor.
"""

from __future__ import annotations

from typing import Any

COST_MOVE_BLOCK = "cost_to_expected_move"

DEFAULT_THETA = 0.25


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return v


def fee_est_usd(notional: Any, taker_rate: Any) -> float | None:
    """Round-trip taker estimate: 2 × rate × notional."""
    n = _f(notional)
    r = _f(taker_rate)
    if n is None or r is None or n <= 0 or r < 0:
        return None
    return 2.0 * r * n


def expected_move_usd(risk_usd: Any, atr_usd: Any = None) -> float | None:
    """Entry-time expected move proxy. Prefer ATR$ when provided & >0."""
    atr = _f(atr_usd)
    if atr is not None and atr > 0:
        return atr
    risk = _f(risk_usd)
    if risk is None or risk <= 0:
        return None
    return risk


def cost_move_ratio(fee_est: Any, expected_move: Any) -> float | None:
    fee = _f(fee_est)
    move = _f(expected_move)
    if fee is None or move is None or move <= 0:
        return None
    return fee / move


def cost_move_verdict(
        enabled: bool,
        live: bool,
        *,
        notional: Any,
        taker_rate: Any,
        risk_usd: Any,
        theta: float = DEFAULT_THETA,
        atr_usd: Any = None,
        asset_class: str = "crypto",
        asset: str | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Return (standdown_reason_or_None, detail).

    - enabled=False → (None, {})
    - tradfi sleeve / xyz: asset → (None, {}) — crypto fee furnace only
    - missing inputs → abstain (None) with detail.abstain=True
    - ratio ≥ Θ + live → (COST_MOVE_BLOCK, detail)
    - ratio ≥ Θ + shadow → (None, detail with shadow=True) — caller journals
    """
    detail: dict[str, Any] = {}
    if not enabled:
        return None, detail
    sleeve = (asset_class or "all").lower()
    if sleeve == "tradfi":
        return None, detail
    if asset and str(asset).startswith("xyz:"):
        return None, detail

    fee = fee_est_usd(notional, taker_rate)
    move = expected_move_usd(risk_usd, atr_usd)
    ratio = cost_move_ratio(fee, move)
    try:
        th = float(theta)
    except (TypeError, ValueError):
        th = DEFAULT_THETA
    detail = {
        "fee_est_usd": fee,
        "expected_move_usd": move,
        "ratio": ratio,
        "theta": th,
        "risk_usd": _f(risk_usd),
        "notional": _f(notional),
        "live": bool(live),
    }
    if ratio is None:
        detail["abstain"] = True
        return None, detail
    if ratio < th:
        return None, detail
    detail["would_block"] = True
    if live:
        return COST_MOVE_BLOCK, detail
    detail["shadow"] = True
    return None, detail
