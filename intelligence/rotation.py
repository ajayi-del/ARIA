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


# ── Rotation Coherence Scalar (RCS) — wave-2 quant instrument ────────────────
# Governor order 2026-10-10; math spec /tmp/aria_wave1_quant.md §2.
#
# One signed scalar answering "is rotation harvestable now, and which way":
#   RCS = sign(S) · min(1, |Z_spread|/2 · D_pct · P_dwell)
#   Z_spread = (mean top-cat − mean bottom-cat) / σ(cats)   (category_scores)
#   D_pct    = percentile of dispersion vs its own 7d history (RegimeState.dispersion)
#   P_dwell  = min(1, dwell_bars / 8)                       (regime persistence)
#   S        = sign(leading-category mean momentum)         (asset_scores)
#
# Hysteresis (tide-transition, book net short into a recovering market):
#   - a flip ARMS only at |RCS| ≥ 0.5 with a 2-of-3 vote over
#     {S flip, tide print flip, Z_spread sign flip};
#   - the old direction DISARMS at |RCS| < 0.2 for 2 consecutive reads;
#   - a fresh tide print (age < 36h) opposing the RCS sign forces RCS → 0
#     immediately — the scalar never sits opposed to a live tide.
#
# SHADOW-FIRST: no gate consumes RCS yet. Every compute cycle logs
# rotation_coherence_scored with all components (Aronson: measured from day
# one, graduated later). Dwell comes from the regime_calculated stream +
# _REGIME_STATE_PATH restore (relative_strength.py:431-466).

RCS_ARM = 0.5            # |RCS| floor to arm a direction flip
RCS_DISARM = 0.2         # |RCS| floor to disarm the old direction
RCS_DISARM_READS = 2     # consecutive sub-disarm reads required
RCS_TIDE_FRESH_H = 36.0  # a tide print younger than this is "fresh"
RCS_DWELL_FULL_BARS = 8  # dwell bars at which P_dwell saturates


def _sign(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0
    if v != v:  # NaN
        return 0
    return 1 if v > 0 else (-1 if v < 0 else 0)


def _percentile_rank(history, current):
    """Percentile (0..1) of `current` within `history` (midpoint-of-ties).
    0.5 (neutral abstain) when the window is too thin to calibrate."""
    vals = []
    for v in history or []:
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv == fv:  # not NaN
            vals.append(fv)
    if len(vals) < 5:
        return 0.5
    try:
        cur = float(current)
    except (TypeError, ValueError):
        return 0.5
    if cur != cur:
        return 0.5
    below = sum(1 for v in vals if v < cur)
    ties = sum(1 for v in vals if v == cur)
    return (below + 0.5 * ties) / len(vals)


def dwell_bars_from_regimes(regimes):
    """Bars the CURRENT regime has persisted — trailing run of identical
    regime labels (oldest-first sequence from the regime_calculated stream
    + _REGIME_STATE_PATH restore). 0 on empty/garbage input."""
    seq = [str(r) for r in (regimes or []) if r]
    if not seq:
        return 0
    cur = seq[-1]
    n = 0
    for r in reversed(seq):
        if r != cur:
            break
        n += 1
    return n


def rotation_scalar(matrix, cat_history, dwell_bars, tide_state=None):
    """Pure RCS compute. Returns the shadow-log row (all components).

    matrix:      RegimeState (category_scores / asset_scores / dispersion /
                 leading_category read via getattr — SimpleNamespace-safe).
    cat_history: 7d history of dispersion observations (sequence of floats);
                 a dict carrying a "dispersion" list is also accepted.
    dwell_bars:  bars the current regime has persisted (int).
    tide_state:  None, or {"sum_3d_usd": float, "age_hours": float} — a fresh
                 (<36h) print opposing the RCS sign forces RCS → 0.

    Fail-closed: degenerate inputs read as RCS 0.0 with reason fields, never
    raise, never fabricate a direction."""
    row = {"rcs": 0.0, "z_spread": 0.0, "d_pct": 0.5, "p_dwell": 0.0,
           "s_sign": 0, "tide_sign": 0, "tide_age_h": None,
           "tide_forced_flat": False, "reason": "ok"}

    cat_scores = dict(getattr(matrix, "category_scores", None) or {}) if matrix is not None else {}
    cat_vals = []
    for v in cat_scores.values():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv == fv:
            cat_vals.append(fv)
    z_spread = 0.0
    if len(cat_vals) >= 2:
        mean_c = sum(cat_vals) / len(cat_vals)
        var_c = sum((v - mean_c) ** 2 for v in cat_vals) / len(cat_vals)
        sigma = var_c ** 0.5
        if sigma > 0.0:
            z_spread = (max(cat_vals) - min(cat_vals)) / sigma
    row["z_spread"] = round(z_spread, 6)

    # S = sign(leading-category mean momentum)
    lead = getattr(matrix, "leading_category", "none") if matrix is not None else "none"
    s_sign = _sign(cat_scores.get(lead, 0.0)) if lead not in _NO_LEADER else 0
    row["s_sign"] = s_sign

    # D_pct = percentile of dispersion vs its own 7d history
    hist = cat_history
    if isinstance(hist, dict):
        hist = hist.get("dispersion") or hist.get("history") or []
    d_pct = _percentile_rank(hist, getattr(matrix, "dispersion", 0.0) if matrix is not None else 0.0)
    row["d_pct"] = round(d_pct, 4)

    # P_dwell = min(1, dwell_bars / 8)
    try:
        dwell = max(0.0, float(dwell_bars))
    except (TypeError, ValueError):
        dwell = 0.0
    p_dwell = min(1.0, dwell / RCS_DWELL_FULL_BARS)
    row["p_dwell"] = round(p_dwell, 4)

    magnitude = min(1.0, abs(z_spread) / 2.0 * d_pct * p_dwell)
    rcs = (1.0 if s_sign > 0 else (-1.0 if s_sign < 0 else 0.0)) * magnitude
    if s_sign == 0:
        row["reason"] = "no_direction"

    # Fresh opposing tide forces flat immediately.
    if tide_state:
        try:
            tide_sign = _sign(tide_state.get("sum_3d_usd", tide_state.get("tide_sign", 0)))
            age = tide_state.get("age_hours")
            age = float(age) if age is not None else None
        except (AttributeError, TypeError, ValueError):
            tide_sign, age = 0, None
        row["tide_sign"] = tide_sign
        row["tide_age_h"] = age
        if (tide_sign != 0 and age is not None and age < RCS_TIDE_FRESH_H
                and rcs != 0.0 and tide_sign != _sign(rcs)):
            rcs = 0.0
            row["tide_forced_flat"] = True
            row["reason"] = "fresh_tide_opposed"

    row["rcs"] = round(rcs, 6)
    logger.info("rotation_coherence_scored", **row)
    return row


class RotationScalarHysteresis:
    """Stateful hysteresis over the pure RCS stream (zero-I/O, in-memory).

    Direction state ∈ {+1, 0, −1}. A flip ARMS only when |RCS| ≥ RCS_ARM with
    a 2-of-3 vote over {S flip, tide print flip, Z_spread sign flip} (each leg
    compares the current read's sign against the previous read's). The armed
    direction DISARMS to flat after RCS_DISARM_READS consecutive reads below
    RCS_DISARM. The band [0.2, 0.5] between flip-in and flip-out prevents
    churn (same doctrine as the flow_size_mult staleness ladder)."""

    def __init__(self):
        self.direction = 0
        self.armed = False
        self._disarm_streak = 0
        self._prev = None  # (s_sign, tide_sign, z_sign) of the last read

    def update(self, row):
        """Fold one rotation_scalar() row in. Returns the verdict dict:
        {direction, armed, rcs_effective, votes, disarm_streak}."""
        rcs = float(row.get("rcs", 0.0) or 0.0)
        s_sign = int(row.get("s_sign", 0) or 0)
        tide_sign = int(row.get("tide_sign", 0) or 0)
        z_sign = _sign(row.get("z_spread", 0.0))

        votes = 0
        if self._prev is not None:
            ps, pt, pz = self._prev
            for prev, cur in ((ps, s_sign), (pt, tide_sign), (pz, z_sign)):
                if prev != 0 and cur != 0 and cur != prev:
                    votes += 1
        self._prev = (s_sign, tide_sign, z_sign)

        # Disarm leg: two consecutive weak reads flatten the old direction.
        if abs(rcs) < RCS_DISARM or bool(row.get("tide_forced_flat")):
            self._disarm_streak += 1
        else:
            self._disarm_streak = 0
        if self._disarm_streak >= RCS_DISARM_READS:
            self.direction = 0
            self.armed = False
            self._disarm_streak = 0

        # Arm leg: strong read + 2-of-3 vote (or first arm from flat, where
        # the vote applies only when CHANGING an armed direction).
        new_dir = _sign(rcs)
        if abs(rcs) >= RCS_ARM and new_dir != 0:
            if not self.armed or new_dir == self.direction:
                self.direction = new_dir
                self.armed = True
            elif votes >= 2:
                self.direction = new_dir
                self.armed = True
            # else: armed direction holds (flip not confirmed)

        effective = rcs if (self.armed and _sign(rcs) == self.direction) else 0.0
        verdict = {"direction": self.direction, "armed": self.armed,
                   "rcs_effective": round(effective, 6), "votes": votes,
                   "disarm_streak": self._disarm_streak}
        logger.info("rotation_scalar_hysteresis", **verdict)
        return verdict
