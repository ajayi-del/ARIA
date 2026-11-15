"""Formation router brain (prop harness only) — the draft board.

Doctrine (docs/SLEEVE_FORMATION_20261114.md, Governor 2026-11-14): one
signal engine drafts soldiers by coherence. This module tags every signal
with its would-be formation unit so the journal can grade each rank
BEFORE new money moves. Pure, zero-I/O: same verdict live at the tap and
offline in tools/formation_sim.py over historical registry rows (the tags
are a deterministic function of asset/side/coherence/ts — history is
backfillable by construction).

Routing precedence (first match wins):
  1. coh >= 7.5 and macro asset (BTC/ETH/gold/index)  -> CAVALRY
     (Lucid Pro grand strikes; Propr GIANT carries it until Cavalry arms)
  2. 23 <= hour_utc <= 02 and coh >= 6 and long       -> GIANT (golden window)
  3. long and coh >= 6                                 -> GIANT
  4. short or 5.7 <= coh < 6                           -> ARCHER (expendable farm)
  5. long and coh >= 6.5                               -> SNIPER candidate tag
     (account-designated; tag only — never changes the unit)
  6. else                                              -> park (formation_park)

Kill switch: PROP_FORMATION_TAGGING=0 -> no formation fields anywhere
(pre-module bit-for-bit). Tagging never gates, never sizes — telemetry.
"""

from __future__ import annotations

import time
from typing import Any

UNIT_CAVALRY = "cavalry"
UNIT_GIANT = "giant"
UNIT_ARCHER = "archer"
UNIT_PARK = "park"

# Cavalry macro strikes (Lucid futures-mappable): BTC/ETH + gold + indices.
# Everything else (single-name stocks, alts, FX, bonds) is Propr-line flow.
MACRO_ASSETS = frozenset({
    "BTC", "ETH",
    "xyz:GOLD",
    "xyz:SP500", "xyz:XYZ100",
})

COH_CAVALRY = 7.5
COH_GIANT = 6.0
COH_ARCHER_LO = 5.7
COH_SNIPER = 6.5

# Golden window (turbo2 autopsy: entry hours 23-02 UTC paid +$70 of the
# +$47 long net; Lucid flat-by-16:45-ET makes it physically Propr-only).
GOLDEN_HOURS_UTC = frozenset({23, 0, 1, 2})


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def hour_utc(ts_s: Any) -> int | None:
    t = _f(ts_s)
    if t is None or t <= 0:
        return None
    return int(time.gmtime(t).tm_hour)


def formation_verdict(
    asset: Any,
    side: Any,
    coherence: Any,
    ts_s: Any,
    *,
    cavalry_armed: bool = False,
) -> dict[str, Any] | None:
    """Classify a signal into its would-be formation unit.

    Returns None on unclassifiable input (bad side / no coherence) —
    callers treat None as "no tag" (never a block). cavalry_armed=False
    (the default until the Lucid purchase clears Gate-0) folds rule 1 into
    GIANT, matching the doc: "if armed, CAVALRY; else GIANT".
    """
    a = str(asset or "")
    s = str(side or "").lower()
    if s not in ("buy", "sell"):
        return None
    coh = _f(coherence)
    if coh is None:
        return None
    hr = hour_utc(ts_s)
    is_long = s == "buy"
    is_macro = a in MACRO_ASSETS
    golden = hr in GOLDEN_HOURS_UTC if hr is not None else False

    if coh >= COH_CAVALRY and is_macro:
        unit = UNIT_CAVALRY if cavalry_armed else UNIT_GIANT
        edge = "cavalry_macro"
    elif is_long and coh >= COH_GIANT and golden:
        unit = UNIT_GIANT
        edge = "golden_long"
    elif is_long and coh >= COH_GIANT:
        unit = UNIT_GIANT
        edge = "giant_long"
    elif (not is_long) or (COH_ARCHER_LO <= coh < COH_GIANT):
        unit = UNIT_ARCHER
        edge = "archer_short" if not is_long else "archer_probe"
    else:
        return {
            "unit": UNIT_PARK,
            "edge_point": "formation_park",
            "golden_window": golden,
            "macro": is_macro,
            "sniper_candidate": False,
        }
    return {
        "unit": unit,
        "edge_point": edge,
        "golden_window": golden,
        "macro": is_macro,
        "sniper_candidate": bool(is_long and coh >= COH_SNIPER),
    }


# Draft order (management science: when budget/slots bind, the decisive
# soldiers arm first; expendables queue last). Lower sorts first.
DRAFT_ORDER = {UNIT_CAVALRY: 0, UNIT_GIANT: 1, UNIT_ARCHER: 2, UNIT_PARK: 3}

ARCHER_SLOT_REASON = "formation_slot_reserved"


def draft_sort_key(tag: dict | None) -> int:
    """Stable sort key for a signal batch. Untagged/park sorts last."""
    if not isinstance(tag, dict):
        return DRAFT_ORDER[UNIT_PARK] + 1
    return DRAFT_ORDER.get(str(tag.get("unit")), DRAFT_ORDER[UNIT_PARK])


def archer_slot_verdict(
    tag: dict | None,
    open_n: Any,
    max_positions: Any,
    reserve_slots: Any,
) -> str | None:
    """Archer-class flow never squats the slot(s) a Giant/Cavalry needs.

    Expendable by doctrine (doc §2): when only the reserve slots remain,
    archer probes/shorts stand down. Returns ARCHER_SLOT_REASON or None.
    Caller decides live-refuse vs shadow-journal (knob)."""
    if not isinstance(tag, dict) or tag.get("unit") != UNIT_ARCHER:
        return None
    try:
        open_i = int(open_n)
        max_i = int(max_positions)
        res_i = max(0, int(reserve_slots))
    except (TypeError, ValueError):
        return None
    if max_i <= 0 or res_i <= 0:
        return None
    if open_i >= max_i - res_i:
        return ARCHER_SLOT_REASON
    return None


def formation_tag_for_signal(sig: dict, *, enabled: bool = True,
                             cavalry_armed: bool = False
                             ) -> dict[str, Any] | None:
    """Tap-side wrapper: signal dict carries ts_ms (epoch milliseconds)."""
    if not enabled or not isinstance(sig, dict):
        return None
    ts_ms = _f(sig.get("ts_ms"))
    ts_s = (ts_ms / 1000.0) if ts_ms is not None else None
    return formation_verdict(
        sig.get("asset"), sig.get("side"), sig.get("coherence"), ts_s,
        cavalry_armed=cavalry_armed)


def formation_tag_for_bracket(bracket: dict, *, cavalry_armed: bool = False
                              ) -> dict[str, Any] | None:
    """Registry-side wrapper: bracket rows carry opened_ts (epoch seconds)
    and coherence_at_entry — the historical-backfill path for the sim."""
    if not isinstance(bracket, dict):
        return None
    return formation_verdict(
        bracket.get("asset"), bracket.get("side"),
        bracket.get("coherence_at_entry", bracket.get("coherence")),
        bracket.get("opened_ts"),
        cavalry_armed=cavalry_armed)
