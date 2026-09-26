"""intelligence/narrative_compass.py — Narrative + rotation slot-selection
brain (pure, zero I/O).

Governor directive (2026-09-26 evening, his words): "the narrative and
rotation structure should be utilised so trades are not totally lottery...
probability permutations and the right formulas for spontaneity."

TWO COMPOSITION LAWS (LOCKED — cross-review + the measured hard-gate
census):

  1. ALIGNMENT IS A SLOT-SELECTION MULTIPLIER, NEVER A HARD GATE. A hard
     narrative gate measured 0.64x net loss (the same class as the
     aftermath gate-tightening verdicts — the gate eats the right tail,
     not the left). Every eligible symbol keeps weight > 0; the compass
     re-DEALS the placement slots toward the narrative each 60s cycle.
  2. THE FREE OPTION AXIOM. Two-sided resting limits cost nothing
     unfilled. When no plane has an opinion (range day, no narrative) the
     correct posture is BOTH sides at HALF conviction — not silence.

Plane precedence (highest first):

  trend_day   — a LOCKED trend day outranks everything: side_bias = the
                aligned side, weight 1.0. The campaign harvests the flush
                INTO the trend; counter-side placements on a locked trend
                day are the measured-negative counter_trend class
                (2026-09-11 regime-engine-v1 doctrine).
  rotation    — the regime matrix (confidence >= 0.6, the rotation
                module's own CONF_MIN). Leader-aligned lean earns
                0.8-1.0; a lean INTO the laggard (long the lagging
                category = the lagging_knife class) is SOFT-CUT to 0.3
                with reason lagging_knife_soft — never zeroed.
  emerging    — the 1%/1.5% leading trend read: creates a lean at 0.8,
                boosts an agreeing rotation lean (cap 1.0).
  ssi         — SoSValue sector read: weak plane, creates a lean at 0.6
                only when nothing stronger spoke, +0.1 when agreeing.
  tide        — ETF-flow tide (BTC/ETH/SOL only, caller-injected for the
                lean side): "opposed" multiplies the assembled weight by
                0.5. The tide never creates a lean and never vetoes one.

Injection contract (the splice owns all I/O; every plane defaults None =
abstain):
  trend_day_verdict : "aligned_long" | "aligned_short" | anything else
                      (the caller composes the suffixed form from the
                      direction-conditional _trend_day_verdict helper).
  rotation_matrix   : the regime matrix object (RelativeStrengthEngine
                      RegimeState — confidence/leading_category/
                      lagging_category attrs).
  emerging_trend    : "long" | "short" | "neutral" | None (param_store
                      emerging_trend:{sym} read).
  ssi_state         : "long" | "short" | None.
  tide_aligned      : "opposed" | "aligned" | "neutral" | None — computed
                      by the caller FOR THE LEAN SIDE (tide is only
                      defined against a direction; BTC/ETH/SOL only).

weighted_slot_permutation: deterministic weight-proportional ordering of
placement candidates WITHOUT replacement (Efraimidis-Spirakis exponential
race: key_i = u_i ** (1/w_i), descending — each candidate's chance of
leading the deal is proportional to its weight, no replacement bias).
Seeded by cycle_ts // 60 so every 60s cycle draws a FRESH ordering (the
"probability permutations for spontaneity": volume keeps spinning while
slots lean with the narrative) and tests reproduce exactly. Degenerate
input (empty, all non-positive weights) -> input order unchanged.

Department shape (docs/DEPARTMENT_TEMPLATE.md): zero-I/O brain — no
network, no files, no clock, no logging; everything injected per call;
imports nothing from main.py (rotation.py is the shared pure brain).
Kill switch `narrative_compass_enabled` (config getattr, default True):
False -> compass_verdict returns None (the caller's uniform baseline:
both sides, weight 1.0, original order — the pre-module lottery) and
weighted_slot_permutation returns the input unchanged. Zero state
mutation either way (the module IS stateless).

Telemetry (emitted by the CALLER — the brain returns dicts only):
  narrative_compass_verdict     symbol, side_bias, weight, reason
  narrative_compass_permutation cycle, n_candidates, top3
"""
from __future__ import annotations

import math
import random
from typing import List, Optional

from intelligence.rotation import (
    CONF_MIN as _ROT_CONF_MIN,
    aftermath_rotation_verdict as _rot_verdict,
)
from intelligence.relative_strength import ASSET_CATEGORIES as _ASSET_CATS

# Weight floor: a 0.0 weight IS a hard gate in disguise (Composition Law 1)
# — every eligible symbol keeps a nonzero slot share.
WEIGHT_FLOOR = 0.05
LAGGING_KNIFE_SOFT_WEIGHT = 0.3     # soft cut, never zero (Law 1)
TIDE_OPPOSED_MULT = 0.5
NO_NARRATIVE_WEIGHT = 0.5           # Free Option axiom (Law 2)
ROTATION_LEAN_WEIGHT = 0.8          # rotation-only lean (leader band low)
EMERGING_LEAN_WEIGHT = 0.8
SSI_LEAN_WEIGHT = 0.6               # weak plane — never outranks rotation
AGREE_BOOST = 0.1                   # plane agreement bump (cap 1.0)


def _enabled(cfg) -> bool:
    return bool(getattr(cfg, "narrative_compass_enabled", True))


def compass_verdict(cfg, *, symbol, rotation_matrix=None, ssi_state=None,
                    emerging_trend=None, tide_aligned=None,
                    trend_day_verdict=None, now_ts=None) -> Optional[dict]:
    """One symbol's narrative slot-selection verdict.

    -> {"symbol", "side_bias": "long"|"short"|"both",
        "weight": float in (0, 1], "reason": str}
    None when the module is disabled or the symbol is unusable (the
    caller's None baseline = uniform lottery: both sides, full weight,
    original order — pre-module bit-for-bit).
    """
    if not _enabled(cfg):
        return None
    if not symbol or not isinstance(symbol, str):
        return None

    def _out(side: str, weight: float, reason: str) -> dict:
        # Never exactly 0 (Law 1), never above 1.
        w = min(1.0, max(WEIGHT_FLOOR, float(weight)))
        return {"symbol": symbol, "side_bias": side,
                "weight": round(w, 4), "reason": reason}

    # ── 1. locked trend day — outranks every other plane ────────────────
    if trend_day_verdict in ("aligned_long", "aligned_short"):
        side = "long" if trend_day_verdict == "aligned_long" else "short"
        return _out(side, 1.0, f"trend_day_aligned_{side}")

    # ── 2. rotation read (matrix confidence uses rotation's own floor) ──
    rot_side = None           # the side the rotation PREFERS
    rot_confident = False
    cat = _ASSET_CATS.get(symbol)
    if rotation_matrix is not None and cat is not None:
        try:
            conf = float(getattr(rotation_matrix, "confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf >= _ROT_CONF_MIN:
            leading = getattr(rotation_matrix, "leading_category", "none")
            lagging = getattr(rotation_matrix, "lagging_category", "none")
            if cat == leading and leading not in ("none", "", "unknown"):
                rot_side, rot_confident = "long", True
            elif cat == lagging and lagging not in ("none", "", "unknown"):
                rot_side, rot_confident = "short", True

    # ── 3. assemble the lean: emerging first, ssi as the weak tiebreak ──
    lean = None
    weight = 0.0
    reason = ""
    if emerging_trend in ("long", "short"):
        lean, weight, reason = emerging_trend, EMERGING_LEAN_WEIGHT, \
            f"emerging_{emerging_trend}"
    elif ssi_state in ("long", "short"):
        lean, weight, reason = ssi_state, SSI_LEAN_WEIGHT, \
            f"ssi_sector_{ssi_state}"

    # ── 4. rotation composes with the lean (SOFT, never a veto) ─────────
    if rot_confident and rot_side is not None:
        if lean is None:
            lean, weight, reason = rot_side, ROTATION_LEAN_WEIGHT, \
                "rotation_leader_aligned"
        elif lean == rot_side:
            weight = min(1.0, weight + AGREE_BOOST)
            reason = f"{reason}+rotation_agree"
        else:
            # The lean fights the rotation. Long into the lagging
            # category is the lagging_knife class (Murphy: money is
            # leaving it); short into the leading category fades
            # strength. SOFT cut to 0.3 — never zero (Law 1).
            knife = ("lagging_knife_soft" if lean == "long"
                     else "leading_strength_soft")
            return _apply_tide(_out, lean, LAGGING_KNIFE_SOFT_WEIGHT,
                               knife, tide_aligned)

    # ── 5. Free Option axiom: no plane spoke -> both sides, half weight ─
    if lean is None:
        return _out("both", NO_NARRATIVE_WEIGHT, "no_narrative_free_option")

    return _apply_tide(_out, lean, weight, reason, tide_aligned)


def _apply_tide(_out, lean: str, weight: float, reason: str,
                tide_aligned) -> dict:
    """The tide (BTC/ETH/SOL only, caller-scoped) scales, never flips:
    opposed x0.5; aligned +0.1 capped 1.0. Caller computes the verdict
    FOR THE LEAN SIDE."""
    if tide_aligned == "opposed":
        return _out(lean, weight * TIDE_OPPOSED_MULT, f"{reason}+tide_opposed")
    if tide_aligned == "aligned":
        return _out(lean, min(1.0, weight + AGREE_BOOST),
                    f"{reason}+tide_aligned")
    return _out(lean, weight, reason)


def weighted_slot_permutation(cfg, *, candidates: List[dict],
                              cycle_ts: int) -> List[dict]:
    """Deterministic weight-proportional ordering WITHOUT replacement.

    candidates: [{"symbol", "side", "weight", ...}, ...]. Returns a NEW
    list (input never mutated). Seeded RNG random.Random(cycle_ts // 60)
    — same cycle_ts reproduces exactly; a different minute re-deals.

    Efraimidis-Spirakis exponential race: draw u_i ~ U(0,1), key_i =
    u_i ** (1/w_i), sort descending. P(i first) = w_i / sum(w) and the
    full ordering is the size-biased permutation — slots lean with the
    narrative while every candidate keeps a nonzero chance of the lead
    (Law 1 at the permutation level). Non-positive weights never race:
    they append at the tail in input order. Degenerate (empty, ALL
    non-positive) -> input order unchanged. Disabled -> input unchanged.
    """
    items = list(candidates or [])
    if not _enabled(cfg) or len(items) < 2:
        return items

    def _w(c) -> float:
        try:
            w = float(c.get("weight"))
        except (TypeError, ValueError, AttributeError):
            return 0.0
        if not math.isfinite(w) or w <= 0.0:
            return 0.0
        return w

    racers = [(i, c, _w(c)) for i, c in enumerate(items)]
    positive = [(i, c, w) for i, c, w in racers if w > 0.0]
    if not positive:
        return items                       # all-zero -> input order
    rng = random.Random(int(cycle_ts) // 60)
    keyed = []
    for i, c, w in positive:
        u = rng.random()
        # u in (0,1); guard the exact-0 edge so the key stays finite.
        key = (max(u, 1e-12)) ** (1.0 / w)
        keyed.append((key, i, c))
    keyed.sort(key=lambda t: (-t[0], t[1]))   # key desc, index tiebreak
    out = [c for _, _, c in keyed]
    out.extend(c for i, c, w in racers if w <= 0.0)
    return out
