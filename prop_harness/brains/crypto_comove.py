"""Crypto co-move / single-β direction gate (prop harness only).

Most crypto names co-move (BTC–ETH–SOL corr ≈0.85–0.90; alts ride the
same beta). An opposite-signed add into a live crypto book creates an
accidental hedge / whipsaw — turbo 2026-11-10 paid −$8.84 in ~42 min on
BTC+SOL longs vs ETH short while full@4.

This gate refuses NEW crypto entries whose side opposes the live book's
dominant direction (net signed risk, majority-side tiebreak). Fail-closed
on a conflicted (net≈0 + tied) book. Tradfi / xyz: never binds. Kill
switch PROP_CRYPTO_COMOVE_GATE=0 → legacy bit-for-bit.
"""

from __future__ import annotations

from typing import Any

CRYPTO_COMOVE_OPPOSITE = "crypto_comove_opposite"
CRYPTO_COMOVE_CONFLICTED = "crypto_comove_conflicted"

_BUY = frozenset({"buy", "long"})
_SELL = frozenset({"sell", "short"})


def is_crypto_asset(asset: str | None) -> bool:
    """Non-xyz assets are treated as crypto for this gate."""
    if not asset:
        return False
    return not str(asset).startswith("xyz:")


def _side_sign(side: str | None) -> int:
    s = (side or "").lower()
    if s in _BUY:
        return 1
    if s in _SELL:
        return -1
    return 0


def _leg_risk(leg: dict) -> float:
    try:
        return abs(float(leg.get("risk_usd") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def crypto_legs(open_brackets: list | None) -> list[dict]:
    """Open crypto legs only (status open/missing; skip xyz:)."""
    out: list[dict] = []
    for held in open_brackets or []:
        if held.get("status") and held.get("status") != "open":
            continue
        if not is_crypto_asset(held.get("asset")):
            continue
        if _side_sign(held.get("side")) == 0:
            continue
        out.append(held)
    return out


def dominant_crypto_side(
        legs: list[dict],
        *,
        min_dom_risk_usd: float = 0.0) -> str | None:
    """Return 'buy' / 'sell' dominant, or None if empty / conflicted.

    Primary: sign of net signed risk (buy=+risk, sell=−risk).
    Tiebreak when |net| <= min_dom_risk: majority leg count.
    Perfect risk+count tie → None (caller fail-closes).
    """
    if not legs:
        return None
    net = 0.0
    n_buy = 0
    n_sell = 0
    for leg in legs:
        sign = _side_sign(leg.get("side"))
        risk = _leg_risk(leg)
        net += sign * risk
        if sign > 0:
            n_buy += 1
        elif sign < 0:
            n_sell += 1
    try:
        floor = float(min_dom_risk_usd or 0.0)
    except (TypeError, ValueError):
        floor = 0.0
    if abs(net) > floor:
        return "buy" if net > 0 else "sell"
    if n_buy > n_sell:
        return "buy"
    if n_sell > n_buy:
        return "sell"
    return None


def crypto_comove_verdict(
        enabled: bool,
        asset: str,
        side: str,
        open_brackets: list | None,
        *,
        min_legs: int = 1,
        min_dom_risk_usd: float = 0.0,
        asset_class: str = "crypto") -> str | None:
    """Refuse opposite-signed crypto entry vs live book dominant β.

    Returns standdown reason or None to allow.
    - Kill switch off / tradfi sleeve / non-crypto candidate → None
    - Fewer than min_legs open crypto → None
    - Conflicted book (no dominant) → CRYPTO_COMOVE_CONFLICTED
    - Candidate side ≠ dominant → CRYPTO_COMOVE_OPPOSITE
    - Same-direction add → None (family_cap still binds same-family)
    """
    if not enabled:
        return None
    sleeve = (asset_class or "all").lower()
    if sleeve == "tradfi":
        return None
    if not is_crypto_asset(asset):
        return None
    cand_sign = _side_sign(side)
    if cand_sign == 0:
        return None
    legs = crypto_legs(open_brackets)
    try:
        need = max(1, int(min_legs or 1))
    except (TypeError, ValueError):
        need = 1
    if len(legs) < need:
        return None
    dominant = dominant_crypto_side(legs, min_dom_risk_usd=min_dom_risk_usd)
    if dominant is None:
        return CRYPTO_COMOVE_CONFLICTED
    cand = "buy" if cand_sign > 0 else "sell"
    if cand != dominant:
        return CRYPTO_COMOVE_OPPOSITE
    return None


def crypto_comove_detail(
        open_brackets: list | None,
        *,
        min_dom_risk_usd: float = 0.0) -> dict[str, Any]:
    """Telemetry helper — net risk / counts / dominant for journals."""
    legs = crypto_legs(open_brackets)
    net = 0.0
    n_buy = n_sell = 0
    for leg in legs:
        sign = _side_sign(leg.get("side"))
        risk = _leg_risk(leg)
        net += sign * risk
        if sign > 0:
            n_buy += 1
        elif sign < 0:
            n_sell += 1
    return {
        "n_crypto": len(legs),
        "n_buy": n_buy,
        "n_sell": n_sell,
        "net_signed_risk": net,
        "dominant": dominant_crypto_side(
            legs, min_dom_risk_usd=min_dom_risk_usd),
    }
