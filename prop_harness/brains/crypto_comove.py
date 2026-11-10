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
CRYPTO_COMOVE_WOULD_FLIP = "crypto_comove_would_flip"
CRYPTO_COMOVE_FLIP_PREFER_FLAT = "comove_flip_prefer_flat"

# Underwater / MFE-decay flip (shadow-first). Defaults from overflow audit §4.
DEFAULT_FLIP_UNDERWATER_S = 900.0
DEFAULT_FLIP_GIVEBACK = 0.60

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


def _leg_upnl(leg: dict) -> float | None:
    for key in ("last_upnl", "unrealized_pnl", "unrealizedPnl"):
        try:
            v = float(leg.get(key))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if v == v:
            return v
    return None


def _leg_giveback(leg: dict) -> float | None:
    """1 - current_r/peak_r when peak>0; None if unknown."""
    try:
        peak = float(leg.get("mfe_peak_r") or 0.0)
    except (TypeError, ValueError):
        return None
    if peak <= 0:
        return None
    # Prefer explicit current_r; else infer from last_upnl / risk.
    cur = None
    try:
        if leg.get("current_r") is not None:
            cur = float(leg["current_r"])
    except (TypeError, ValueError):
        cur = None
    if cur is None:
        upnl = _leg_upnl(leg)
        risk = _leg_risk(leg)
        if upnl is None or risk <= 0:
            return None
        cur = upnl / risk
    return max(0.0, 1.0 - (cur / peak))


def dominant_side_legs(legs: list[dict], dominant: str) -> list[dict]:
    want = 1 if dominant == "buy" else -1
    return [leg for leg in legs if _side_sign(leg.get("side")) == want]


def flip_predicate(
        open_brackets: list | None,
        *,
        min_dom_risk_usd: float = 0.0,
        underwater_s: float = DEFAULT_FLIP_UNDERWATER_S,
        giveback_frac: float = DEFAULT_FLIP_GIVEBACK,
        now: float | None = None,
) -> dict[str, Any] | None:
    """Underwater / MFE-decay flip arm — book-truth, not clock-only.

    Arms when dominant-side legs show median uPnL < 0 for ≥ underwater_s
    (via upnl_negative_since stamps) OR median MFE giveback ≥ giveback_frac
    with median uPnL ≤ 0. Returns detail dict or None.
    """
    import time as _time
    ts = float(now if now is not None else _time.time())
    legs = crypto_legs(open_brackets)
    dominant = dominant_crypto_side(legs, min_dom_risk_usd=min_dom_risk_usd)
    if dominant is None:
        return None
    dom_legs = dominant_side_legs(legs, dominant)
    if not dom_legs:
        return None
    upnls = [_leg_upnl(leg) for leg in dom_legs]
    known = [u for u in upnls if u is not None]
    if not known:
        return None
    known_sorted = sorted(known)
    median_upnl = known_sorted[len(known_sorted) // 2]

    # Underwater duration: median of (now - upnl_negative_since) among
    # legs currently negative. Missing stamp → use opened_ts age only when
    # upnl<0 (conservative lower bound).
    ages: list[float] = []
    for leg in dom_legs:
        u = _leg_upnl(leg)
        if u is None or u >= 0:
            continue
        since = None
        try:
            if leg.get("upnl_negative_since") is not None:
                since = float(leg["upnl_negative_since"])
        except (TypeError, ValueError):
            since = None
        if since is None:
            try:
                since = float(leg.get("opened_ts") or 0.0)
            except (TypeError, ValueError):
                since = None
        if since is None or since <= 0:
            continue
        ages.append(max(0.0, ts - since))
    median_underwater_s = (
        sorted(ages)[len(ages) // 2] if ages else 0.0)

    givebacks = [g for g in (_leg_giveback(leg) for leg in dom_legs)
                 if g is not None]
    median_giveback = (
        sorted(givebacks)[len(givebacks) // 2] if givebacks else 0.0)

    try:
        need_s = float(underwater_s or 0.0)
    except (TypeError, ValueError):
        need_s = DEFAULT_FLIP_UNDERWATER_S
    try:
        need_g = float(giveback_frac or 0.0)
    except (TypeError, ValueError):
        need_g = DEFAULT_FLIP_GIVEBACK

    underwater_arm = median_upnl < 0 and median_underwater_s >= need_s
    giveback_arm = (
        median_upnl <= 0 and median_giveback >= need_g and bool(givebacks))
    if not (underwater_arm or giveback_arm):
        return None
    return {
        "event": CRYPTO_COMOVE_WOULD_FLIP,
        "dominant": dominant,
        "median_upnl": median_upnl,
        "median_underwater_s": median_underwater_s,
        "median_giveback": median_giveback,
        "underwater_arm": underwater_arm,
        "giveback_arm": giveback_arm,
        "n_dominant": len(dom_legs),
        "prefer_flat": True,
    }


def flip_shadow_verdict(
        flip_enabled: bool,
        opposite_reason: str | None,
        open_brackets: list | None,
        *,
        flip_live: bool = False,
        prefer_flat: bool = True,
        min_dom_risk_usd: float = 0.0,
        underwater_s: float = DEFAULT_FLIP_UNDERWATER_S,
        giveback_frac: float = DEFAULT_FLIP_GIVEBACK,
        now: float | None = None,
        flip_used_today: bool = False,
) -> tuple[str | None, dict[str, Any] | None]:
    """Shadow / controlled flip on top of an opposite (or conflicted) refuse.

    Returns (effective_reason, flip_detail):
      - flip off / no opposite → (opposite_reason, None)
      - predicate cold → (opposite_reason, None)
      - shadow (default): (opposite_reason, would_flip detail) — still refuse
      - live + prefer_flat: (FLIP_PREFER_FLAT, detail) — still refuse hedge
      - live + not prefer_flat + not flip_used: (None, detail) — allow ONE
      - live + flip_used: (opposite_reason, detail_exhausted)

    Never haircuts opposite size — hedges recreate the fee+δ wound.
    """
    if not flip_enabled or opposite_reason is None:
        return opposite_reason, None
    if opposite_reason not in (CRYPTO_COMOVE_OPPOSITE, CRYPTO_COMOVE_CONFLICTED):
        return opposite_reason, None
    detail = flip_predicate(
        open_brackets,
        min_dom_risk_usd=min_dom_risk_usd,
        underwater_s=underwater_s,
        giveback_frac=giveback_frac,
        now=now,
    )
    if detail is None:
        return opposite_reason, None
    detail["opposite_reason"] = opposite_reason
    detail["prefer_flat"] = bool(prefer_flat)
    detail["live"] = bool(flip_live)
    if not flip_live:
        detail["shadow"] = True
        return opposite_reason, detail
    if flip_used_today:
        detail["exhausted"] = True
        return opposite_reason, detail
    if prefer_flat:
        # Flat-then-reverse: do NOT open the hedge; stand down with a
        # distinct reason so exits can free slots first.
        return CRYPTO_COMOVE_FLIP_PREFER_FLAT, detail
    # Controlled live opposite allow (one-shot; caller latches used).
    detail["allow_opposite"] = True
    return None, detail
