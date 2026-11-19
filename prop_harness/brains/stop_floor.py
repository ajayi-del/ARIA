"""Widen-only birth stop floor (prop harness, 2026-11-19).

Doctrine (Governor / soft-box $150): capture-first, not fee-tick cosmetics.
Live census: hold<90s scrapes print median birth_stop_pct ≈ 0.350%
(= 3.9× round-trip @ 0.045%/side) then die — noise+fees, not edge.

Birth geometry ONLY (management is early_tighten_gate):
  floor_dist = max(pct_of_mark, atr_mult × ATR) when ATR readable;
  ATR dark → % leg alone; both dark/degenerate → fail-OPEN keep signal stop.
Never tightens. Constant risk_usd ⇒ wider stop shrinks NOTIONAL.

Class asymmetry: crypto floor > tradfi (vol ≠ index). Kill PROP_STOP_FLOOR=0.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any

# Fee plane (config.taker_fee_rate default 0.00045): RT = 0.09%.
# Crypto 0.75% = 8.3× RT (clears 2–3× fee bar + wick buffer).
# Tradfi 0.50% = 5.6× RT (quieter tape; still widens 0.35% XYZ scrapes).
DEFAULT_FLOOR_PCT_CRYPTO = 0.75
DEFAULT_FLOOR_PCT_TRADFI = 0.50
DEFAULT_ATR_MULT = 1.0


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def _tick_for(mark: float) -> float:
    if mark >= 1000:
        return 0.5
    if mark >= 100:
        return 0.01
    if mark >= 1:
        return 0.0001
    if mark >= 0.01:
        return 1e-6
    return 1e-8


def _to_grid(raw: float, tick: float) -> float | None:
    if tick <= 0:
        return raw
    try:
        d = Decimal(str(raw))
        t = Decimal(str(tick))
        n = (d / t).to_integral_value(rounding=ROUND_DOWN) * t
        return float(n)
    except Exception:
        return None


def floor_pct_for_class(asset_class: str | None,
                        crypto_pct: float = DEFAULT_FLOOR_PCT_CRYPTO,
                        tradfi_pct: float = DEFAULT_FLOOR_PCT_TRADFI
                        ) -> float:
    """Class asymmetry: crypto vol gets the higher % floor."""
    ac = (asset_class or "all").lower()
    if ac == "tradfi":
        return float(tradfi_pct)
    return float(crypto_pct)  # crypto + all


def stop_floor_verdict(
        stop: float | str | None,
        entry: float | str | None,
        side: str | None,
        floor_pct: float | None = None,
        mark: float | str | None = None,
        tick: float | None = None,
        atr: float | str | None = None,
        atr_mult: float = DEFAULT_ATR_MULT,
        asset_class: str | None = None,
        crypto_pct: float = DEFAULT_FLOOR_PCT_CRYPTO,
        tradfi_pct: float = DEFAULT_FLOOR_PCT_TRADFI,
) -> tuple[float, bool, dict[str, Any]]:
    """Widen-only stop floor.

    floor_dist = max(pct/100 × mark, atr_mult × atr) when atr > 0;
    atr dark/unreadable → pct leg only (ATR fail-OPEN).
    Returns (new_or_old_stop, floored, detail).
    """
    detail: dict[str, Any] = {"floored": False, "asset_class": asset_class}
    sp = _f(stop)
    en = _f(entry)
    mk = _f(mark) if mark is not None else en
    if floor_pct is None:
        floor_pct = floor_pct_for_class(asset_class, crypto_pct, tradfi_pct)
    detail["floor_pct"] = float(floor_pct) if floor_pct is not None else None
    if floor_pct is None or float(floor_pct) <= 0:
        return (sp if sp is not None else 0.0), False, detail
    if sp is None or en is None or en <= 0 or mk is None or mk <= 0:
        return (sp if sp is not None else 0.0), False, detail
    if side not in ("buy", "sell"):
        return sp, False, detail
    cur = abs(en - sp)
    if cur <= 0:
        return sp, False, detail

    pct_dist = float(floor_pct) / 100.0 * mk
    atr_v = _f(atr)
    atr_m = _f(atr_mult) or 0.0
    atr_dist = None
    if atr_v is not None and atr_v > 0 and atr_m > 0:
        atr_dist = atr_m * atr_v
    # Wider of readable legs; ATR dark → pct alone.
    if atr_dist is not None:
        floor_dist = max(pct_dist, atr_dist)
        detail["floor_leg"] = ("atr" if atr_dist > pct_dist else "pct")
    else:
        floor_dist = pct_dist
        detail["floor_leg"] = "pct_atr_dark"

    detail.update({
        "entry": en, "mark": mk, "side": side,
        "birth_stop": sp, "birth_dist": cur,
        "birth_pct": cur / en * 100.0,
        "floor_dist": floor_dist,
        "pct_dist": pct_dist,
        "atr": atr_v, "atr_mult": atr_m, "atr_dist": atr_dist,
    })
    if cur >= floor_dist:
        return sp, False, detail

    raw = (en - floor_dist) if side == "buy" else (en + floor_dist)
    t = float(tick) if tick is not None and float(tick) > 0 else _tick_for(mk)
    new = _to_grid(raw, t)
    if new is None:
        new = raw
    new_dist = abs(en - new)
    if new_dist <= cur:
        detail["grid_refused"] = True
        return sp, False, detail
    if side == "buy" and not (new < en):
        return sp, False, detail
    if side == "sell" and not (new > en):
        return sp, False, detail
    detail.update({
        "floored": True,
        "new_stop": new,
        "new_dist": new_dist,
        "new_pct": new_dist / en * 100.0,
        "tick": t,
    })
    return new, True, detail
