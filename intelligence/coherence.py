import structlog
import numpy as np
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass

logger = structlog.get_logger(__name__)

# v2 Tier Correlation Matrix — used to apply independence discount
# Higher value = more redundant (penalised harder)
TIER_CORRELATIONS = {
    ("institutional", "regime"):      0.65,
    ("microstructure", "structure"):  0.45,
    ("regime", "structure"):          0.55,
    ("institutional", "oi_momentum"): 0.50,
    ("regime", "oi_momentum"):        0.35,
    # Liquidation signals overlap with forced-close proxies (OI, funding squeeze)
    # but are still structurally independent (actual on-chain event vs model estimate)
    ("liquidation", "oi_momentum"):   0.40,
    ("liquidation", "funding"):       0.35,
}

class CoherenceEngine:
    """
    v2 Weighted Coherence Scoring with Independence Discount.

    v2 changes vs v1.3:
    - Microstructure rewritten: volume surge + candle conviction as always-available proxies.
      Previously depended on rare sweeps or VPIN > 0.70 → almost always 0.
    - VPIN threshold lowered 0.70 → 0.60 (Bybit liquid market calibration).
    - Tier score ceilings raised: regime 1.5, structure 2.0, funding 1.5, OI 1.5,
      institutional 2.0, micro 4.0 (capped). Ensures full-alignment can reach 8–10.
    - Independence discount cap reduced 30% → 15%, allowing legitimate independent
      signals (funding, OI) to contribute fully without excessive penalty.
    - Quiet trending market target: ~2.5–3.0 (size ≥ 35%).
    - Full hot-market alignment target: 8–10.
    """

    def __init__(self, stop_clusters=None):
        self.stop_clusters = stop_clusters

    def calculate_weighted_score(
        self,
        symbol: str,
        analyzers_output: Dict[str, Any],
        freshness: float = 1.0,
        tier_weight_overrides: Optional[Dict[str, float]] = None,
    ) -> Tuple[float, int, Dict[str, float]]:
        """
        Computes weighted score with Tier Independence Discount.
        Returns (weighted_score, raw_score, component_scores)

        tier_weight_overrides: per-tier multipliers from SignalFeedbackEngine.
          Applied before the independence discount so the discount still reflects
          actual signal overlap after feedback scaling.
        """
        components = {}
        raw_score = 0

        # ── Tier 4: Microstructure (v2) ────────────────────────────────────────
        # Replaces rare sweep-only logic with always-available volume/conviction signals.
        sweep       = analyzers_output.get("sweep", "none")
        sweep_price = analyzers_output.get("sweep_price", 0)
        sweep_side  = analyzers_output.get("sweep_side", "none")
        vpin_hot    = analyzers_output.get("vpin_hot", False)
        vpin_val    = analyzers_output.get("vpin", 0.0)
        imbalance   = analyzers_output.get("imbalance", 0.0)
        vol_surge   = analyzers_output.get("volume_surge", 1.0)
        conviction  = analyzers_output.get("candle_conviction", 0.0)

        micro_score = 0.0

        # Liquidity sweep (rare but high-value confirmation)
        if sweep != "none":
            micro_score = 1.0
            if self.stop_clusters:
                cluster_valid, cluster_strength = self.stop_clusters.validate_sweep(
                    symbol, sweep_price, sweep_side
                )
                if cluster_valid:
                    micro_score = 1.5
                    if cluster_strength > 0.8:
                        micro_score = 2.0

        # VPIN: toxic-flow proxy (threshold 0.60 — calibrated for Bybit liquid market)
        if vpin_hot or vpin_val >= 0.60:
            micro_score += 0.75

        # OB imbalance: live bid/ask size asymmetry
        abs_imb = abs(imbalance)
        if abs_imb >= 0.40:
            micro_score += 0.50
        elif abs_imb >= 0.25:
            micro_score += 0.25

        # Volume surge: last-candle volume vs 20-bar average — directional pressure proxy
        if vol_surge >= 2.5:
            micro_score += 1.0
        elif vol_surge >= 1.5:
            micro_score += 0.5

        # Candle conviction: body/range ratio — strong directional candles
        if conviction >= 0.70:
            micro_score += 0.75
        elif conviction >= 0.55:
            micro_score += 0.35

        micro_score = min(micro_score, 4.0)  # Hard cap — single tier should not dominate
        components["microstructure"] = micro_score
        if micro_score >= 1.0:
            raw_score += 1

        # ── Tier 1: Institutional (SSI + OI confirmation) ──────────────────────
        ssi_status = analyzers_output.get("ssi_status", "neutral")
        oi_label   = analyzers_output.get("oi_signal", "NEUTRAL")

        ssi_score = 0.0
        if ssi_status == "strong_inflow":
            ssi_score = 1.5
        elif ssi_status == "inflow":
            ssi_score = 0.75
        if "EXPANSION" in oi_label:
            ssi_score += 0.50

        ssi_score = min(ssi_score, 2.0)
        components["institutional"] = ssi_score
        if ssi_score >= 1.0:
            raw_score += 1

        # ── Tier 6: OI Momentum ─────────────────────────────────────────────────
        oi_score = 0.0
        if oi_label in ("BULLISH_EXPANSION", "BEARISH_EXPANSION"):
            oi_score = 1.5
        elif oi_label in ("SHORT_COVERING", "LONG_LIQUIDATION"):
            oi_score = 0.75

        components["oi_momentum"] = oi_score
        if oi_score >= 1.0:
            raw_score += 1

        # ── Tier 2: Regime ──────────────────────────────────────────────────────
        regime = analyzers_output.get("regime", "neutral")
        regime_score = 0.0
        if regime in ("risk_on", "risk_off"):
            regime_score = 1.5
        elif regime == "rotational":
            regime_score = 0.5

        components["regime"] = regime_score
        if regime_score >= 1.0:
            raw_score += 1

        # ── Tier 3: Structure ────────────────────────────────────────────────────
        market_type = analyzers_output.get("market_type", "chop")
        struct_score = 0.0
        if market_type == "expansion":
            struct_score = 2.0
        elif market_type == "trend":
            struct_score = 1.5
        elif market_type == "compression":
            struct_score = 0.5

        components["structure"] = struct_score
        if struct_score >= 1.0:
            raw_score += 1

        # ── Tier 5: Funding ──────────────────────────────────────────────────────
        # Calibrated for Bybit 8h rates (e.g. 0.0001 = 0.01%/8h normal bull market).
        # See funding_analyzer.py for recalibrated thresholds.
        funding_class = analyzers_output.get("funding_class", "neutral")
        funding_score = 0.0
        if "extreme" in funding_class:
            funding_score = 1.5
        elif funding_class in ("positive", "negative"):
            funding_score = 0.75

        components["funding"] = funding_score
        if funding_score >= 0.75:
            raw_score += 1

        # ── Tier 6: Liquidation Intelligence (from LiquidationSignalEngine) ────────
        # On-chain liquidation events — higher quality than OI/funding proxies because
        # they are actual forced position closures, not model-inferred signals.
        # Weight cap 1.5 — a single tier should never dominate the score.
        # Conflict with directional lock is handled upstream (70% penalty in interpreter).
        liq_score = min(float(analyzers_output.get("tier6_liq_score", 0.0)), 1.5)
        components["liquidation"] = liq_score
        if liq_score >= 0.75:
            raw_score += 1

        # ── Tier 1 (new): MAG7 Macro Regime — USTECH100 price action ────────────
        # Direction-neutral strength (0.0–1.5). Direction-awareness (bonus/penalty)
        # is applied in the interpreter's Enhancement Layer post-coherence, so the
        # independence discount here correctly reflects the raw signal magnitude.
        # Stale or neutral → 0.0. Active Nasdaq trend → up to 1.5 contribution.
        mag7_strength = min(float(analyzers_output.get("mag7_strength", 0.0)), 1.5)
        components["mag7_macro"] = mag7_strength
        if mag7_strength >= 0.75:
            raw_score += 1

        # ── Feedback tier-weight overrides (from SignalFeedbackEngine) ───────────
        # Applied before independence discount so overlap penalty still reflects
        # actual relative contributions after feedback scaling.
        if tier_weight_overrides:
            for tier_key, mult in tier_weight_overrides.items():
                if tier_key in components:
                    components[tier_key] = round(components[tier_key] * mult, 4)

        # ── Independence Discount (v2) ───────────────────────────────────────────
        # Cap reduced from 30% to 15% — legitimate independent tiers (funding, OI,
        # microstructure) should contribute fully without excessive penalty.
        base_weighted_score = sum(components.values())
        independence_factor = self._calculate_independence_factor(components)

        weighted_score = base_weighted_score * independence_factor

        # Apply freshness decay
        if freshness < 1.0:
            weighted_score *= freshness

        # Clamp to MarketState field ceilings
        weighted_score = min(weighted_score, 10.0)
        raw_score = min(raw_score, 7)  # 7 possible tiers (Tier1-Tier6 + MAG)

        components["independence_discount"] = independence_factor

        return weighted_score, raw_score, components

    def _calculate_independence_factor(self, components: Dict[str, float]) -> float:
        """
        Returns a multiplier (0.85–1.0) based on signal overlap.
        v2: Max discount reduced from 30% → 15% to allow legitimate independent
        tiers to contribute without over-penalisation.
        """
        active_tiers = [k for k, v in components.items() if v > 0 and k != "independence_discount"]
        if len(active_tiers) <= 1:
            return 1.0

        total_redundancy = 0.0
        matches = 0

        for k1, k2 in TIER_CORRELATIONS:
            if k1 in active_tiers and k2 in active_tiers:
                total_redundancy += TIER_CORRELATIONS[(k1, k2)]
                matches += 1

        if matches == 0:
            return 1.0

        avg_redundancy = total_redundancy / matches
        discount = min(0.15, avg_redundancy * 0.25)

        return 1.0 - discount

    def get_size_multiplier(self, weighted_score: float) -> float:
        """
        Maps coherence score to position size multiplier.
        v2 calibrated for achievable score range:
          - 2.5 (quiet trend)  → 0.35
          - 4.0 (active)       → 0.50
          - 6.0 (hot)          → 1.00
          - 8.0 (peak)         → 1.25
        """
        if weighted_score < 1.0:  return 0.0
        if weighted_score < 1.5:  return 0.10
        if weighted_score < 2.0:  return 0.20
        if weighted_score < 3.0:  return 0.35
        if weighted_score < 4.0:  return 0.50
        if weighted_score < 5.0:  return 0.75
        if weighted_score < 6.0:  return 1.00
        if weighted_score < 7.0:  return 1.25
        if weighted_score < 9.0:  return 1.50
        return 1.75
