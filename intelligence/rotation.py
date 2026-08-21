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


def rotation_enabled() -> bool:
    return os.environ.get("ROTATION_MODIFIER_ENABLED", "true").strip().lower() != "false"


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
