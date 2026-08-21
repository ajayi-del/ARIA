"""Rotation laggard-catch-up modifier (2026-08-21, operator directive — live).

Book grounding:
- Murphy, Intermarket Analysis: capital rotates between sectors in sequence —
  the leader moves first, laggards in the SAME leading sector catch up second.
- Chan, Algorithmic Trading (cross-sectional mean reversion): trade the
  RESIDUAL (category − asset), never the raw return — and only while the
  sector factor itself is confirmed positive.
- Clenow, Stocks on the Move: never buy a broken trend — an absolute-momentum
  floor on the laggard itself keeps falling knives out.
- Ilmanen, Expected Returns: at intraday horizons the reversal edge lives in
  the idiosyncratic component — the modifier stays small (cap 0.5, half the
  graduation privilege) until the journal proves it.
- Aronson, Evidence-Based Technical Analysis: unproven modifiers are measured,
  not trusted — every nonzero boost logs rotation_boost_applied so the shadow
  journal scores it counterfactually from day one.

Kill switch: ROTATION_MODIFIER_ENABLED=false reproduces the pre-module system
bit-for-bit (empty boost dict → coherence Tier 10 reads 0.0).
"""
import os
import structlog

from intelligence.relative_strength import ASSET_CATEGORIES

logger = structlog.get_logger(__name__)

BOOST_CAP = 0.5
GAP_MIN = 0.005        # residual (category − asset) floor: 0.5% over the 48-bar window
GAP_TO_BOOST = 50.0    # linear map: 0.5% gap → 0.25 boost, ≥1.0% gap → 0.5 cap
CONF_MIN = 0.6         # regime classifier confidence floor
ASSET_FLOOR = -0.005   # Clenow absolute-momentum floor: deeper than -0.5% = broken
_NO_LEADER = {"none", "", "unknown"}

# Cascade aftermath rotation filter (2026-08-21, operator directive — live).
# Murphy blocks the worst knife asymmetry; Chan ranks survivors by residual
# overshoot; Aronson: every block is shadow-scored from birth.
CASCADE_FILTER_RESIDUAL_MIN = 0.002  # |residual| floor for "overshoot" in ranking notes


def rotation_enabled() -> bool:
    return os.environ.get("ROTATION_MODIFIER_ENABLED", "true").strip().lower() != "false"


def cascade_filter_enabled() -> bool:
    return os.environ.get("CASCADE_ROTATION_FILTER_ENABLED", "true").strip().lower() != "false"


def laggard_boosts(matrix, symbols) -> dict:
    """{symbol: boost} for laggards inside the matrix's LEADING category.

    matrix: RegimeState from RelativeStrengthEngine. Long-side catch-up only —
    the short mirror (unfallen leader inside a lagging category) is deferred
    until the long side shows evidence. Fail-closed: any doubt → symbol absent.
    """
    if not rotation_enabled() or matrix is None:
        return {}
    if float(getattr(matrix, "confidence", 0.0) or 0.0) < CONF_MIN:
        return {}
    lead = getattr(matrix, "leading_category", "none")
    if lead in _NO_LEADER:
        return {}
    cat_score = float(getattr(matrix, "category_scores", {}).get(lead, 0.0) or 0.0)
    if cat_score <= 0.0:  # Chan: the sector factor itself must be confirmed
        return {}
    asset_scores = getattr(matrix, "asset_scores", {}) or {}
    out = {}
    for sym in symbols:
        if ASSET_CATEGORIES.get(sym) != lead:
            continue
        a = float(asset_scores.get(sym, 0.0) or 0.0)
        gap = cat_score - a
        if gap < GAP_MIN or a < ASSET_FLOOR:  # residual too small, or Clenow floor
            continue
        out[sym] = round(min(BOOST_CAP, gap * GAP_TO_BOOST), 4)
    return out


# ── Cascade aftermath rotation filter ────────────────────────────────────────
# Murphy, Intermarket Analysis (weak form): in a confirmed sector rotation,
# a dip in the LAGGING category is a knife (money is leaving it) — do not buy
# it; a fade of the LEADING category is fading strength — do not short it.
# Neutral categories and unknown state pass (abstain = pre-module behavior).
# Chan (cross-sectional mean reversion): within the survivors, rank by
# residual overshoot — the symbol that moved most relative to its own category
# mean has the deepest snap-back. Ilmanen: beta-reversion (residual ≈ 0 in a
# market-wide cascade) is itself a valid trade, so Chan RANKS, never blocks.
# Aronson: every block logs signal_rejected_rotation_filter → shadow gate
# "rotation_filter" scores it counterfactually from day one.


def aftermath_rotation_verdict(matrix, symbol, direction):
    """(verdict, reason) for a cascade-aftermath candidate.

    verdict ∈ {"blocked", "allowed", "abstain"}.
      blocked  — lagging_knife (long into lagging category) / leading_strength
                 (short into leading category). High-confidence negative EV.
      allowed  — leading_dip / lagging_fade / neutral_category.
      abstain  — filter_disabled / no_matrix / low_confidence / no_rotation /
                 uncategorized / unknown_direction. Abstain = pre-module system.
    """
    if not cascade_filter_enabled():
        return "abstain", "filter_disabled"
    if matrix is None:
        return "abstain", "no_matrix"
    if float(getattr(matrix, "confidence", 0.0) or 0.0) < CONF_MIN:
        return "abstain", "low_confidence"
    leading = getattr(matrix, "leading_category", "none")
    lagging = getattr(matrix, "lagging_category", "none")
    if leading in _NO_LEADER and lagging in _NO_LEADER:
        return "abstain", "no_rotation"
    if direction not in ("long", "short"):
        return "abstain", "unknown_direction"
    cat = ASSET_CATEGORIES.get(symbol)
    if cat is None:
        return "abstain", "uncategorized"
    if direction == "long":
        if cat == lagging and lagging not in _NO_LEADER:
            return "blocked", "lagging_knife"
        if cat == leading and leading not in _NO_LEADER:
            return "allowed", "leading_dip"
        return "allowed", "neutral_category"
    # short
    if cat == leading and leading not in _NO_LEADER:
        return "blocked", "leading_strength"
    if cat == lagging and lagging not in _NO_LEADER:
        return "allowed", "lagging_fade"
    return "allowed", "neutral_category"


def residual_overshoots(pre_prices, mark_prices):
    """{symbol: residual} — per-symbol cascade move minus its category mean.

    pre_prices: {symbol: price} snapshot at cascade start (CascadeTracker).
    mark_prices: {symbol: current mark}. Both plain dicts of floats.

    Chan: the tradeable overshoot is the RESIDUAL (symbol move − category
    factor), never the raw move. Category means use groups with ≥2 priced
    members; singleton/uncategorized symbols fall back to the all-symbol mean
    so the map is total and never crashes a ranker on an odd universe.
    """
    moves = {}
    for sym, pre in (pre_prices or {}).items():
        cur = (mark_prices or {}).get(sym)
        try:
            pre_f, cur_f = float(pre), float(cur)
        except (TypeError, ValueError):
            continue
        if pre_f > 0 and cur_f > 0:
            moves[sym] = (cur_f - pre_f) / pre_f
    if not moves:
        return {}

    cat_sums, cat_counts = {}, {}
    for sym, mv in moves.items():
        cat = ASSET_CATEGORIES.get(sym)
        if cat is None:
            continue
        cat_sums[cat] = cat_sums.get(cat, 0.0) + mv
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    cat_means = {c: cat_sums[c] / cat_counts[c]
                 for c in cat_sums if cat_counts[c] >= 2}
    all_mean = sum(moves.values()) / len(moves)

    out = {}
    for sym, mv in moves.items():
        cat = ASSET_CATEGORIES.get(sym)
        ref = cat_means.get(cat, all_mean)
        out[sym] = mv - ref
    return out
