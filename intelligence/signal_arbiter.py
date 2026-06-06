"""
SignalArbiter — Hierarchical conflict resolver for competing signal tiers.

Replaces scalar weighted summation with conditional belief resolution.
When macro says "long" and micro says "short", the arbiter checks which
has higher empirical edge in this regime, not averages them.

Design principle: the confidence score reflects the strength of the WINNING
coalition, not the sum of all tiers. Opposing tiers are suppressed and do
not inflate confidence.
"""

import structlog
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

logger = structlog.get_logger(__name__)


# Tier correlation matrix — reused from CoherenceEngine for independence discount
TIER_CORRELATIONS = {
    ("institutional", "regime"):      0.65,
    ("microstructure", "structure"):  0.45,
    ("regime", "structure"):          0.55,
    ("institutional", "oi_momentum"): 0.50,
    ("regime", "oi_momentum"):        0.35,
    ("liquidation", "oi_momentum"):   0.40,
    ("liquidation", "funding"):       0.35,
}

# Static edge table — MVP until regime_memory is wired to nightly journal updates.
# Format: (regime, conflict_type) -> preferred_tier
# conflict_type: "macro_vs_micro", "sweep_vs_macro", "funding_vs_macro", etc.
_STATIC_EDGE_TABLE: Dict[Tuple[str, str], str] = {
    ("transitioning", "macro_vs_micro"): "microstructure",
    ("confused",      "macro_vs_micro"): "microstructure",
    ("chop",          "macro_vs_micro"): "microstructure",
    ("risk_on",       "macro_vs_micro"): "regime",
    ("risk_off",      "macro_vs_micro"): "regime",
    ("alt_season",    "macro_vs_micro"): "microstructure",
    ("btc_dominance", "macro_vs_micro"): "regime",
    ("transitioning", "sweep_vs_macro"):  "microstructure",
    ("risk_on",       "sweep_vs_macro"):  "microstructure",
    ("risk_off",      "sweep_vs_macro"):  "microstructure",
    ("transitioning", "funding_vs_macro"): "funding",
    ("risk_on",       "funding_vs_macro"): "regime",
    ("risk_off",      "funding_vs_macro"): "regime",
}


@dataclass(frozen=True)
class ArbiterResult:
    direction: str          # "long" | "short" | "none"
    confidence: float       # 0.0–10.0, reflects winning coalition strength
    dominant_tier: str      # tier that decided the direction
    suppressed_tiers: List[str] = field(default_factory=list)
    resolution_rule: str = "default"
    breakdown: Dict[str, float] = field(default_factory=dict)


@dataclass
class ArbiterContext:
    symbol: str
    asset_class: str        # "crypto" | "equity" | "commodity"
    current_direction: str  # from fallback chain
    base_confidence: float  # scalar sum from CoherenceEngine
    components: Dict[str, float]
    tier_directions: Dict[str, str]   # tier -> "long" | "short" | "none"
    regime: str = "neutral"
    macro_bias: str = "neutral"
    macro_confidence: float = 0.0
    cascade_phase: str = ""
    cascade_zscore: float = 0.0
    cascade_direction: str = "none"
    calendar_regime: str = "CLEAR"
    freshness: float = 1.0
    htf_bias: str = "neutral"


class SignalArbiter:
    """
    Hierarchical signal conflict resolver.

    Rules (applied in order; first match wins):
      1. Calendar BLOCK   → hard block
      2. Cascade z>3.0    → cascade direction overrides all
      3. Sweep confirmed + weak macro → microstructure wins
      4. Macro vs Micro conflict → regime-dependent edge lookup
      5. Simple majority  → side with higher aligned score wins
      6. Funding veto     → opposing extreme funding reduces confidence
      7. Default fallback → keep fallback-chain direction, confidence = aligned sum
    """

    def __init__(self, regime_memory=None):
        self.regime_memory = regime_memory  # optional: RegimeMemory for empirical WR

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def resolve(self, ctx: ArbiterContext) -> ArbiterResult:
        """Run full resolution pipeline."""

        # 1. Hard block
        if ctx.calendar_regime == "BLOCK":
            return ArbiterResult(
                direction="none",
                confidence=0.0,
                dominant_tier="calendar",
                resolution_rule="calendar_block",
                breakdown={"reason": "calendar_regime_block"},
            )

        # 2. Cascade supremacy
        _cascade_res = self._resolve_cascade(ctx)
        if _cascade_res is not None:
            return _cascade_res

        # 3. Sweep supremacy in weak macro
        _sweep_res = self._resolve_sweep(ctx)
        if _sweep_res is not None:
            return _sweep_res

        # 4–6. General conflict resolution
        return self._resolve_general(ctx)

    # ──────────────────────────────────────────────────────────────────────────
    # Rule implementations
    # ──────────────────────────────────────────────────────────────────────────

    def _resolve_cascade(self, ctx: ArbiterContext) -> Optional[ArbiterResult]:
        """Rule 2: liquidation cascade overrides all organic signals."""
        if ctx.cascade_phase not in ("expansion", "exhaustion", "primed"):
            return None
        if ctx.cascade_zscore <= 3.0:
            return None
        if ctx.cascade_direction not in ("long", "short"):
            return None

        _conf = min(9.0, 6.0 + ctx.cascade_zscore * 0.5)
        _suppressed = [
            t for t, d in ctx.tier_directions.items()
            if t != "liquidation" and d != "none" and d != ctx.cascade_direction
        ]
        return ArbiterResult(
            direction=ctx.cascade_direction,
            confidence=_conf,
            dominant_tier="liquidation",
            suppressed_tiers=_suppressed,
            resolution_rule="cascade_supremacy",
            breakdown={
                "cascade_phase": ctx.cascade_phase,
                "cascade_zscore": round(ctx.cascade_zscore, 2),
                "confidence": round(_conf, 2),
            },
        )

    def _resolve_sweep(self, ctx: ArbiterContext) -> Optional[ArbiterResult]:
        """Rule 3: confirmed liquidity sweep with weak macro → micro wins."""
        _micro_dir = ctx.tier_directions.get("microstructure", "none")
        _micro_score = ctx.components.get("microstructure", 0.0)
        if _micro_dir == "none" or _micro_score < 2.0:
            return None
        if ctx.macro_confidence >= 0.6:
            return None  # macro is strong enough to contest

        _conf = min(8.0, _micro_score * 1.2)
        _suppressed = [
            t for t, d in ctx.tier_directions.items()
            if t != "microstructure" and d != "none" and d != _micro_dir
        ]
        return ArbiterResult(
            direction=_micro_dir,
            confidence=_conf,
            dominant_tier="microstructure",
            suppressed_tiers=_suppressed,
            resolution_rule="sweep_supremacy_weak_macro",
            breakdown={
                "micro_score": round(_micro_score, 2),
                "macro_confidence": round(ctx.macro_confidence, 2),
            },
        )

    def _resolve_general(self, ctx: ArbiterContext) -> ArbiterResult:
        """Rules 4–7: score-based conflict resolution with regime edge lookup."""

        # Build direction buckets
        long_score, short_score = 0.0, 0.0
        long_tiers, short_tiers = [], []

        for tier, direction in ctx.tier_directions.items():
            score = ctx.components.get(tier, 0.0)
            if score <= 0 or direction == "none":
                continue
            if direction == "long":
                long_score += score
                long_tiers.append(tier)
            elif direction == "short":
                short_score += score
                short_tiers.append(tier)

        # If both sides have zero score → no signal
        if long_score <= 0 and short_score <= 0:
            return ArbiterResult(
                direction="none",
                confidence=0.0,
                dominant_tier="none",
                resolution_rule="no_directional_tiers",
            )

        # If only one side has score → that side wins
        if long_score <= 0:
            winner_dir = "short"
            winner_tiers = short_tiers
            winner_score = short_score
            loser_tiers = long_tiers
        elif short_score <= 0:
            winner_dir = "long"
            winner_tiers = long_tiers
            winner_score = long_score
            loser_tiers = short_tiers
        else:
            # Both sides active — resolve conflict
            winner_dir, winner_tiers, winner_score, loser_tiers, rule = self._resolve_conflict(
                ctx, long_score, short_score, long_tiers, short_tiers
            )

        # Compute confidence = sum of winning coalition scores with independence discount
        _winner_components = {t: ctx.components.get(t, 0.0) for t in winner_tiers}
        _independence = self._independence_factor(_winner_components)
        confidence = winner_score * _independence

        # Funding veto: extreme funding opposing winner reduces confidence
        _funding_dir = ctx.tier_directions.get("funding", "none")
        _funding_score = ctx.components.get("funding", 0.0)
        if _funding_score >= 1.0 and _funding_dir != "none" and _funding_dir != winner_dir:
            confidence *= 0.6
            rule = "funding_veto_reduced"

        # Cap and apply freshness
        confidence = min(10.0, confidence * ctx.freshness)

        # Determine dominant tier (highest score on winning side)
        dominant_tier = max(winner_tiers, key=lambda t: ctx.components.get(t, 0.0)) if winner_tiers else "none"

        return ArbiterResult(
            direction=winner_dir,
            confidence=round(confidence, 4),
            dominant_tier=dominant_tier,
            suppressed_tiers=loser_tiers,
            resolution_rule=rule,
            breakdown={
                "long_score": round(long_score, 3),
                "short_score": round(short_score, 3),
                "winner_score": round(winner_score, 3),
                "independence": round(_independence, 3),
                "dominant_tier": dominant_tier,
            },
        )

    def _resolve_conflict(
        self,
        ctx: ArbiterContext,
        long_score: float,
        short_score: float,
        long_tiers: List[str],
        short_tiers: List[str],
    ) -> Tuple[str, List[str], float, List[str], str]:
        """
        Resolve when both long and short have active tiers.
        Returns: (winner_dir, winner_tiers, winner_score, loser_tiers, rule)
        """
        ratio = max(long_score, short_score) / min(long_score, short_score)

        # Clear majority (>2x) → stronger side wins unconditionally
        if ratio >= 2.0:
            if long_score > short_score:
                return "long", long_tiers, long_score, short_tiers, "clear_majority_long"
            else:
                return "short", short_tiers, short_score, long_tiers, "clear_majority_short"

        # Close conflict — use regime edge lookup
        _preferred = self._lookup_edge(ctx.regime, "macro_vs_micro")
        if _preferred == "microstructure":
            # Microstructure wins if it has the highest single-tier score
            _micro_dir = ctx.tier_directions.get("microstructure", "none")
            _micro_score = ctx.components.get("microstructure", 0.0)
            _macro_dir = ctx.tier_directions.get("regime", "none")
            _macro_score = ctx.components.get("regime", 0.0)
            if _micro_dir != "none" and _micro_score >= _macro_score * 0.8:
                if _micro_dir == "long":
                    return "long", long_tiers, long_score, short_tiers, "regime_edge_microstructure"
                else:
                    return "short", short_tiers, short_score, long_tiers, "regime_edge_microstructure"

        elif _preferred == "funding":
            _fund_dir = ctx.tier_directions.get("funding", "none")
            _fund_score = ctx.components.get("funding", 0.0)
            if _fund_dir != "none" and _fund_score >= 1.0:
                if _fund_dir == "long":
                    return "long", long_tiers, long_score, short_tiers, "regime_edge_funding"
                else:
                    return "short", short_tiers, short_score, long_tiers, "regime_edge_funding"

        # Fallback: if macro/regime is the preferred tier, it wins
        _regime_dir = ctx.tier_directions.get("regime", "none")
        if _preferred in ("regime", "macro") and _regime_dir != "none":
            if _regime_dir == "long":
                return "long", long_tiers, long_score, short_tiers, "regime_edge_macro"
            else:
                return "short", short_tiers, short_score, long_tiers, "regime_edge_macro"

        # Ultimate fallback: keep fallback-chain direction if it has any support
        if ctx.current_direction == "long" and long_score > 0:
            return "long", long_tiers, long_score, short_tiers, "fallback_chain_long"
        if ctx.current_direction == "short" and short_score > 0:
            return "short", short_tiers, short_score, long_tiers, "fallback_chain_short"

        # Absolute fallback: higher score wins
        if long_score >= short_score:
            return "long", long_tiers, long_score, short_tiers, "score_tiebreaker_long"
        else:
            return "short", short_tiers, short_score, long_tiers, "score_tiebreaker_short"

    def _lookup_edge(self, regime: str, conflict_type: str) -> str:
        """Look up preferred tier for a conflict in a given regime."""
        # Try empirical memory first
        if self.regime_memory is not None:
            _emp = self.regime_memory.get_preferred_tier(regime, conflict_type)
            if _emp:
                return _emp
        # Fall back to static table
        return _STATIC_EDGE_TABLE.get((regime, conflict_type), "regime")

    # ──────────────────────────────────────────────────────────────────────────
    # Independence discount (copied from CoherenceEngine for consistency)
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _independence_factor(components: Dict[str, float]) -> float:
        active = [k for k, v in components.items() if v > 0]
        if len(active) <= 1:
            return 1.0
        total_redundancy = 0.0
        matches = 0
        for k1, k2 in TIER_CORRELATIONS:
            if k1 in active and k2 in active:
                total_redundancy += TIER_CORRELATIONS[(k1, k2)]
                matches += 1
        if matches == 0:
            return 1.0
        avg_redundancy = total_redundancy / matches
        discount = min(0.15, avg_redundancy * 0.25)
        return 1.0 - discount
