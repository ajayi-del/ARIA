"""KingdomView crisis valve — joint thesis-budget / n_eff across armies.

ATS-26 / graph-synthesis Law 1 / prop-tp-turbo2 stress plane.

Same thesis (family|side) across demo+turbo+turbo2 counts as **one**
risk factor. This is an *exposure* n_eff for entry budget — NOT CohortStats
learning merge (that cross-account pool remains REFUSED).

Actions (entry path only in v1):
  CLEAR  — proceed at planned risk
  REDUCE — haircut risk_usd by ``reduce_mult`` (default 0.5)
  BLOCK  — refuse new risk that would raise kingdom n_eff

Anti-lock (must all hold):
  - Kill ``PROP_CRISIS_VALVE=0`` → always CLEAR (bit-for-bit absent)
  - Missing / schema-bad / stale / incomplete KingdomView → CLEAR (fail-OPEN)
  - Never writes algedonic, never erases ArmyState, never widens TP
  - Never mutates EVAL_LADDER / treasury ROE basket (NOT a TP owner)
  - ``thesis_reserve`` (coh≥reserve_coh OR already held same thesis /
    be_armed runner) exempt from BLOCK; REDUCE still applies under stress
  - Optional stress latch is OBSERVE-triggered; HL marks never enter
    equity / headroom / breach math (hl_marks DESIGN LAW)

Kill switches: ``PROP_CRISIS_VALVE``, ``PROP_CRISIS_STRESS`` (stress latch).
"""

from __future__ import annotations

from typing import Any

from prop_harness.families import family_of
from prop_harness.brains.kingdom_view import SCHEMA as KINGDOM_SCHEMA

SCHEMA = 1

# Defaults — eval book with 3 armies × max 3 slots; joint factor budget
# is tighter than sum of slots (correlated crash defense).
DEFAULT_N_EFF_BUDGET = 4
DEFAULT_REDUCE_AT = 3          # n_eff ≥ this → REDUCE new distinct factors
DEFAULT_BLOCK_AT = 4           # n_eff ≥ this → BLOCK new distinct factors
DEFAULT_REDUCE_MULT = 0.5
DEFAULT_RESERVE_COH = 8.5
DEFAULT_STALE_S = 90.0


def factor_key(asset: str, side: str) -> str:
    """One risk factor = theme family × direction (selloff convergence)."""
    return f"{family_of(asset)}|{side}"


def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def open_factors(view: dict | None) -> dict[str, dict]:
    """Map factor_key → occupancy metadata across all armies.

    First sighting wins for ``army_id`` / ``be_armed`` (telemetry only).
    """
    out: dict[str, dict] = {}
    if not view or not isinstance(view, dict):
        return out
    armies = view.get("armies")
    if not isinstance(armies, dict):
        return out
    for aid, snap in armies.items():
        if not isinstance(snap, dict):
            continue
        for pos in snap.get("positions") or []:
            if not isinstance(pos, dict):
                continue
            asset = pos.get("asset")
            side = pos.get("side")
            if not asset or not side:
                continue
            key = factor_key(str(asset), str(side))
            if key in out:
                # Upgrade be_armed if any army has it.
                if pos.get("be_armed"):
                    out[key]["be_armed"] = True
                out[key]["n_legs"] = int(out[key].get("n_legs") or 0) + 1
                continue
            out[key] = {
                "family": family_of(str(asset)),
                "side": side,
                "army_id": aid,
                "asset": asset,
                "be_armed": bool(pos.get("be_armed")),
                "n_legs": 1,
            }
    return out


def compute_n_eff(view: dict | None) -> int:
    """Joint effective factor count across the kingdom."""
    return len(open_factors(view))


def view_usable(
    view: dict | None,
    *,
    now: float,
    stale_s: float = DEFAULT_STALE_S,
) -> bool:
    """Anti-lock gate: unusable view → crisis valve stands down (CLEAR)."""
    if not view or not isinstance(view, dict):
        return False
    if view.get("schema") != KINGDOM_SCHEMA:
        return False
    if view.get("incomplete"):
        return False
    ts = _f(view.get("ts"))
    if ts is None or now - ts > stale_s:
        return False
    return True


def is_thesis_reserved(
    *,
    asset: str,
    side: str,
    coherence: float | None,
    factors: dict[str, dict],
    reserve_coh: float = DEFAULT_RESERVE_COH,
) -> bool:
    """High-coh or already-held / BE-armed factor — exempt from BLOCK."""
    if coherence is not None:
        try:
            if float(coherence) >= reserve_coh:
                return True
        except (TypeError, ValueError):
            pass
    key = factor_key(asset, side)
    held = factors.get(key)
    if held and (held.get("be_armed") or held.get("n_legs", 0) >= 1):
        # Already occupying this factor: reload / same-thesis add is reserved
        # against BLOCK (family_cap still owns same-army doubles).
        return True
    return False


def crisis_plane(
    view: dict | None,
    *,
    now: float,
    n_eff_budget: int = DEFAULT_N_EFF_BUDGET,
    reduce_at: int = DEFAULT_REDUCE_AT,
    block_at: int = DEFAULT_BLOCK_AT,
    stale_s: float = DEFAULT_STALE_S,
    stress_active: bool = False,
) -> dict:
    """Governor-facing plane folded into KingdomView (observe + act hint).

    Factors always counted for observe (even incomplete views). Mode stays
    CLEAR when unusable — actuation is fail-OPEN, census is not.
    """
    usable = view_usable(view, now=now, stale_s=stale_s)
    factors = open_factors(view)
    n_eff = len(factors)
    mode = "CLEAR"
    if usable:
        if n_eff >= block_at or (stress_active and n_eff >= reduce_at):
            mode = "BLOCK"
        elif n_eff >= reduce_at or stress_active:
            mode = "REDUCE"
    return {
        "schema": SCHEMA,
        "ts": now,
        "usable": usable,
        "n_eff": n_eff,
        "n_eff_budget": int(n_eff_budget),
        "reduce_at": int(reduce_at),
        "block_at": int(block_at),
        "stress_active": bool(stress_active),
        "mode": mode,
        "factors": factors,
        # Doctrine markers — consumers must honor these.
        "owns_tp": False,
        "hl_in_risk_math": False,
    }


def crisis_verdict(
    *,
    enabled: bool,
    view: dict | None,
    asset: str,
    side: str,
    coherence: float | None,
    planned_risk_usd: float,
    now: float,
    reduce_mult: float = DEFAULT_REDUCE_MULT,
    reduce_at: int = DEFAULT_REDUCE_AT,
    block_at: int = DEFAULT_BLOCK_AT,
    n_eff_budget: int = DEFAULT_N_EFF_BUDGET,
    reserve_coh: float = DEFAULT_RESERVE_COH,
    stale_s: float = DEFAULT_STALE_S,
    stress_active: bool = False,
    stress_enabled: bool = True,
) -> dict:
    """Entry-path verdict. Pure — never mutates view or ArmyState.

    Returns dict with keys:
      action ∈ {CLEAR, REDUCE, BLOCK}
      risk_usd (post-haircut when REDUCE)
      n_eff, would_raise, reserved, reason, plane
    """
    planned = float(planned_risk_usd)
    plane = crisis_plane(
        view, now=now, n_eff_budget=n_eff_budget, reduce_at=reduce_at,
        block_at=block_at, stale_s=stale_s,
        stress_active=bool(stress_active and stress_enabled),
    )
    base = {
        "action": "CLEAR",
        "risk_usd": planned,
        "n_eff": plane["n_eff"],
        "would_raise": False,
        "reserved": False,
        "reason": "ok",
        "plane": plane,
    }
    if not enabled:
        base["reason"] = "crisis_valve_off"
        return base
    if not plane["usable"]:
        base["reason"] = "view_unusable_fail_open"
        return base

    factors = plane["factors"]
    key = factor_key(asset, side)
    would_raise = key not in factors
    base["would_raise"] = would_raise
    reserved = is_thesis_reserved(
        asset=asset, side=side, coherence=coherence,
        factors=factors, reserve_coh=reserve_coh)
    base["reserved"] = reserved

    n_eff = plane["n_eff"]
    stress = plane["stress_active"]
    # Prospective n_eff if this entry opens a new factor.
    prospective = n_eff + (1 if would_raise else 0)

    # BLOCK: new factor that breaches budget, or stress+new factor at reduce_at.
    block_new = would_raise and (
        prospective > n_eff_budget
        or prospective > block_at
        or n_eff >= block_at
        or (stress and n_eff >= reduce_at)
    )
    if block_new and not reserved:
        return {
            **base,
            "action": "BLOCK",
            "risk_usd": 0.0,
            "reason": (
                f"n_eff={n_eff}→{prospective} budget={n_eff_budget}"
                + (";stress" if stress else "")
            ),
        }

    # REDUCE: at/above reduce_at, or stress — haircut (reserved still reduces
    # under stress so runners don't ignore crash defense entirely).
    reduce = (
        n_eff >= reduce_at
        or (would_raise and prospective >= reduce_at)
        or stress
    )
    if reduce:
        mult = max(0.0, min(1.0, float(reduce_mult)))
        haircut = planned * mult
        return {
            **base,
            "action": "REDUCE",
            "risk_usd": haircut,
            "reason": (
                f"n_eff={n_eff} reduce_at={reduce_at} mult={mult}"
                + (";stress" if stress else "")
                + (";reserved" if reserved else "")
            ),
        }

    return base
