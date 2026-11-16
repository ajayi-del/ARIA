"""Sleeve selection router — pure measure/rank/allocate/overflow verdicts.

Doctrine (docs store: sleeve-selection-router-model.md; prefs 2026-11-16):
  - Infinite ARIA signals → finite sleeves (risk box, slots, DD headroom,
    asset-class fit, cooloff latches).
  - PRIMARY ownership is binding: turbo = crypto; turbo2 = stocks.
  - Free-trial / other sleeves are NEIGHBOURS — overflow / parallel
    learning only. They never steal turbo's crypto mandate.
  - Allocate highest EV that fits on primary; else overflow to next
    neighbour of the SAME asset class; else drop with reason.
  - SPLIT preserved — no MIXED crypto+stocks into one box.

This module is zero-I/O and does NOT gate live execution until a caller
splices it behind an explicit kill switch. Shadow callers journal the
returned action/reason; LIVE refuse is Governor-only.

Kill-switch mental model at the caller: unset/disabled → today's
per-sleeve independent tap (bit-for-bit).
"""

from __future__ import annotations

from typing import Any

# Canonical sleeve ids (state-root roles, not account suffixes).
SLEEVE_TURBO = "turbo"           # crypto PRIMARY
SLEEVE_TURBO2 = "turbo2"         # stocks PRIMARY
SLEEVE_FT_CRYPTO = "ft_crypto"   # kwuT neighbour
SLEEVE_FT_STOCKS = "ft_stocks"   # jkRB neighbour

ASSET_CRYPTO = "crypto"
ASSET_TRADFI = "tradfi"

ACTION_PRIMARY = "primary_fit"
ACTION_OVERFLOW = "overflow_neighbour"
ACTION_DROP = "drop"

# Fail-closed / standdown reasons (stable strings for journal census).
REASON_BAD_CLASS = "asset_class_mismatch"
REASON_LOCKED = "drawdown_locked"
REASON_BREACHED = "drawdown_breached"
REASON_NO_SLOTS = "max_positions"
REASON_NO_BUDGET = "day_budget_exhausted"
REASON_NO_HEADROOM = "hard_zero_headroom"
REASON_COOLOFF = "cooloff_latched"
REASON_STALE = "sleeve_snap_stale"
REASON_PRIMARY_HOLDS = "primary_has_capacity"  # neighbour must not front-run
REASON_NO_CANDIDATE = "no_fitting_sleeve"
REASON_BAND_A = "band_a_admission"  # optional caller-supplied refuse

# Formation-style unit weights for EV ranking (higher = draft first).
_UNIT_WEIGHT = {
    "cavalry": 1.00,
    "giant": 0.90,
    "archer": 0.55,
    "park": 0.20,
}


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def _i(x: Any) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def asset_class_of(asset: Any) -> str | None:
    """xyz: → tradfi; else crypto. None on empty."""
    a = str(asset or "").strip()
    if not a:
        return None
    return ASSET_TRADFI if a.startswith("xyz:") else ASSET_CRYPTO


def ownership_chain(asset_class: Any) -> tuple[str, tuple[str, ...]] | None:
    """Return (primary, neighbours) for an asset class.

    Binding Dayo 2026-11-16: turbo owns crypto; FT crypto is neighbour only.
    turbo2 owns stocks; jkRB FT is neighbour only.
    """
    cls = str(asset_class or "").strip().lower()
    if cls == ASSET_CRYPTO:
        return SLEEVE_TURBO, (SLEEVE_FT_CRYPTO,)
    if cls in (ASSET_TRADFI, "stocks", "stock", "equity"):
        return SLEEVE_TURBO2, (SLEEVE_FT_STOCKS,)
    return None


def ownership_chain_for_asset(asset: Any) -> tuple[str, tuple[str, ...]] | None:
    cls = asset_class_of(asset)
    if cls is None:
        return None
    return ownership_chain(cls)


def ev_score(
    coherence: Any,
    *,
    formation_unit: Any = None,
    band_a: bool = False,
) -> float:
    """Cheap EV proxy for ranking — not a Kelly claim.

    Band-A flicker ranks to the floor so overflow does not export the
    turbo2 wound onto the FT neighbour (adjudication Thesis C).
    """
    if band_a:
        return -1.0
    coh = _f(coherence)
    if coh is None:
        return 0.0
    unit = str(formation_unit or "").strip().lower()
    w = _UNIT_WEIGHT.get(unit, 0.70)
    return max(0.0, coh) * w


def measure_fit(
    snap: dict | None,
    *,
    asset_class: str,
    now_s: float | None = None,
    stale_s: float = 90.0,
    hard_zero: float = 30.0,
) -> str | None:
    """Return standdown reason, or None if the sleeve can take risk.

    Expected snap keys (kingdom / heartbeat shaped — all optional with
    fail-closed defaults): sleeve, band, breached, open_n, max_positions,
    budget_remaining, headroom, cooloff, ts.
    """
    if not isinstance(snap, dict):
        return REASON_STALE
    sleeve_cls = str(snap.get("sleeve") or snap.get("asset_class") or "").lower()
    if sleeve_cls and sleeve_cls not in ("all", asset_class):
        # tradfi alias
        if not (asset_class == ASSET_TRADFI and sleeve_cls in ("tradfi", "stocks")):
            if not (asset_class == ASSET_CRYPTO and sleeve_cls == "crypto"):
                return REASON_BAD_CLASS
    if now_s is not None:
        ts = _f(snap.get("ts"))
        if ts is None or (now_s - ts) > stale_s:
            return REASON_STALE
    if bool(snap.get("breached")):
        return REASON_BREACHED
    band = str(snap.get("band") or "").upper()
    if band in ("LOCKED", "BREACHED"):
        return REASON_LOCKED if band == "LOCKED" else REASON_BREACHED
    mult = _f(snap.get("mult"))
    if mult is not None and mult <= 0:
        return REASON_LOCKED
    if bool(snap.get("cooloff")):
        return REASON_COOLOFF
    open_n = _i(snap.get("open_n"))
    if open_n is None:
        open_n = len(snap.get("positions") or [])
    max_n = _i(snap.get("max_positions"))
    if max_n is not None and max_n > 0 and open_n >= max_n:
        return REASON_NO_SLOTS
    rem = _f(snap.get("budget_remaining"))
    if rem is not None and rem <= 0:
        return REASON_NO_BUDGET
    headroom = _f(snap.get("headroom"))
    if headroom is not None and headroom <= hard_zero:
        return REASON_NO_HEADROOM
    return None


def select_sleeve(
    asset: Any,
    coherence: Any,
    snaps: dict[str, dict | None],
    *,
    formation_unit: Any = None,
    band_a: bool = False,
    now_s: float | None = None,
    stale_s: float = 90.0,
    allow_overflow: bool = True,
    allow_neighbour_parallel: bool = False,
) -> dict[str, Any]:
    """Pick primary / overflow neighbour / drop.

    ``snaps`` keyed by sleeve id (turbo / turbo2 / ft_crypto / ft_stocks).
    ``allow_neighbour_parallel``: FT shadow twin while primary ALSO fits —
    default False so neighbours never front-run free primary capacity.
    """
    chain = ownership_chain_for_asset(asset)
    if chain is None:
        return {
            "action": ACTION_DROP,
            "sleeve": None,
            "reason": REASON_BAD_CLASS,
            "score": 0.0,
            "primary": None,
            "candidates": [],
        }
    primary, neighbours = chain
    cls = asset_class_of(asset) or ASSET_CRYPTO
    score = ev_score(coherence, formation_unit=formation_unit, band_a=band_a)
    if band_a:
        return {
            "action": ACTION_DROP,
            "sleeve": None,
            "reason": REASON_BAND_A,
            "score": score,
            "primary": primary,
            "candidates": [],
        }

    order = (primary,) + tuple(neighbours)
    fits: list[tuple[str, float]] = []
    blocks: list[dict[str, str]] = []
    for sid in order:
        reason = measure_fit(
            snaps.get(sid), asset_class=cls, now_s=now_s, stale_s=stale_s)
        if reason is None:
            # Prefer primary strongly: score boost so equal-EV stays primary.
            adj = score + (100.0 if sid == primary else 0.0)
            fits.append((sid, adj))
        else:
            blocks.append({"sleeve": sid, "reason": reason})

    if not fits:
        # Prefer the primary's block reason when present.
        primary_block = next((b for b in blocks if b["sleeve"] == primary), None)
        reason = (primary_block or (blocks[0] if blocks else {})).get(
            "reason", REASON_NO_CANDIDATE)
        return {
            "action": ACTION_DROP,
            "sleeve": None,
            "reason": reason,
            "score": score,
            "primary": primary,
            "candidates": blocks,
        }

    fits.sort(key=lambda t: -t[1])
    best_id, _ = fits[0]

    if best_id == primary:
        return {
            "action": ACTION_PRIMARY,
            "sleeve": primary,
            "reason": ACTION_PRIMARY,
            "score": score,
            "primary": primary,
            "candidates": blocks,
        }

    # Neighbour wins only via overflow path.
    primary_fit = any(sid == primary for sid, _ in fits)
    if primary_fit and not allow_neighbour_parallel:
        return {
            "action": ACTION_PRIMARY,
            "sleeve": primary,
            "reason": REASON_PRIMARY_HOLDS,
            "score": score,
            "primary": primary,
            "candidates": blocks,
        }
    if not allow_overflow:
        primary_block = next((b for b in blocks if b["sleeve"] == primary), None)
        return {
            "action": ACTION_DROP,
            "sleeve": None,
            "reason": (primary_block or {}).get("reason", REASON_NO_CANDIDATE),
            "score": score,
            "primary": primary,
            "candidates": blocks,
        }
    return {
        "action": ACTION_OVERFLOW,
        "sleeve": best_id,
        "reason": ACTION_OVERFLOW,
        "score": score,
        "primary": primary,
        "candidates": blocks,
    }
