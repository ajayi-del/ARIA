import structlog
import numpy as np
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass

logger = structlog.get_logger(__name__)


def _get_asset_tier_weights(symbol: str) -> Dict[str, float]:
    """
    Return asset-class terrain tier weight multipliers for a symbol.
    Lazy import avoids circular dependency at module load time.
    Returns empty dict (no-op) on import error — safe default.
    """
    try:
        from core.asset_classes import get_tier_weights
        return get_tier_weights(symbol)
    except Exception:
        return {}


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
        market_context=None,  # Optional[MarketContext]
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
        # Rotation regimes (cex_flow, defi_infra, transitioning, etc.) carry real
        # directional information even when the market hasn't committed to risk_on/off.
        # These get confidence-scaled partial credit (0.25–0.75) so the tier never
        # silences a genuine sector-rotation signal.
        # Tier-1 (full): clear directional consensus — same as risk_on/off in magnitude.
        # alt_season = alt_l1 outperforming large_cap at confidence≥1.0: as strong as risk_on.
        # defi_stress = DeFi tokens crashing vs BTC: as strong as risk_off for DeFi names.
        _REGIME_FULL = frozenset({"risk_on", "risk_off", "alt_season", "defi_stress"})
        # Tier-2 (medium): strong rotation with known directionality — confidence-scaled [0.5, 1.0].
        _REGIME_MEDIUM = frozenset({"btc_dominance", "tech_led", "mag7_led", "defi_active"})
        # Tier-3 (partial): rotation signal present but market unsettled — confidence-scaled [0.25, 0.75].
        _REGIME_PARTIAL_DEFAULTS: Dict[str, float] = {
            "transitioning": 0.30,
            "cex_flow":      0.60,
            "defi_infra":    0.60,
            "alt_l1_led":    0.65,
            "large_cap_led": 0.65,
            "meme_led":      0.60,
            "meme_euphoria": 0.60,
            "equity_led":    0.65,
            "confused":      0.10,
        }
        regime = analyzers_output.get("regime", "neutral")
        regime_confidence = float(
            analyzers_output.get("regime_confidence",
                                  _REGIME_PARTIAL_DEFAULTS.get(regime, 0.5))
        )
        regime_score = 0.0
        if regime in _REGIME_FULL:
            regime_score = 1.5
        elif regime == "rotational":
            regime_score = 0.5
        elif regime in _REGIME_MEDIUM:
            # Medium directional: confidence-scaled 0.5–1.0
            regime_score = round(min(1.0, max(0.5, regime_confidence)), 2)
        elif regime in _REGIME_PARTIAL_DEFAULTS:
            # Partial credit: confidence × 1.25, clamped [0.10, 0.75]
            regime_score = round(min(0.75, max(0.10, regime_confidence * 1.25)), 2)

        components["regime"] = regime_score
        if regime_score >= 0.25:
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

        # ── Tier 7: Cross-Venue Funding Arb ──────────────────────────────────────
        # Bybit leads SoDEX in price discovery. Spread between venues → directional edge.
        # Bonus is pre-computed by compute_cross_venue_signal() and passed as
        # tier7_cross_venue_bonus (0.0–0.5). Zero when spread below threshold or
        # direction does not match the current trade direction.
        cross_venue_bonus = min(float(analyzers_output.get("tier7_cross_venue_bonus", 0.0)), 0.5)
        components["cross_venue"] = cross_venue_bonus
        if cross_venue_bonus >= 0.25:
            raw_score += 1

        # ── Tier 8: Cascade Aftermath Boost ──────────────────────────────────────
        # When cascade_tracker transitions to PRIMED and trade direction matches the
        # expected recovery direction, fire a +1.0 coherence boost.
        # The flag tier8_cascade_fired is set by the interpreter BEFORE calling
        # calculate_weighted_score, after checking that direction aligns.
        cascade_boost = 1.0 if bool(analyzers_output.get("tier8_cascade_fired", False)) else 0.0
        components["cascade_aftermath"] = cascade_boost
        if cascade_boost > 0:
            raw_score += 1

        # ── Tier 9: Flow Confirmation ─────────────────────────────────────────────
        # Buy-side volume dominance aligned with long bias → +0.3.
        # Sell-side dominance aligned with short bias → +0.3.
        # Counter-flow (buy heavy but bearish bias, or vice-versa) → -0.2.
        # Source: trade_flow_store.buy/sell_volume injected as "flow_bias" by interpreter.
        flow_bias_val = str(analyzers_output.get("flow_bias", "neutral"))
        # macro_bias from the signal generator encodes the directional lean
        macro_bias_val = str(analyzers_output.get("macro_bias", "neutral")).lower()
        _is_bullish = macro_bias_val in ("bullish", "long", "buy")
        _is_bearish = macro_bias_val in ("bearish", "short", "sell")
        flow_score = 0.0
        if flow_bias_val == "buy" and _is_bullish:
            flow_score = 0.3
        elif flow_bias_val == "sell" and _is_bearish:
            flow_score = 0.3
        elif flow_bias_val == "buy" and _is_bearish:
            flow_score = -0.2
        elif flow_bias_val == "sell" and _is_bullish:
            flow_score = -0.2
        components["flow_confirmation"] = flow_score
        if flow_score > 0:
            raw_score += 1

        # ── Layered weight overrides (3 layers, each compounding on the previous) ──
        #
        # Layer 1: Asset-class terrain weights (base)
        #   Crypto: microstructure 1.5×, funding 0.5×
        #   Commodity: macro 1.25×, micro 0.5×, funding 0.0× (no perp funding)
        #   Equity: macro 1.5×, micro 0.25×, MAG7 2.0×, funding 0.0×
        #
        # Layer 2: Market-context weights (cascade/calendar mode adjustments)
        #   e.g. CASCADE_PRIMED: cascade_aftermath 2.0×, microstructure 1.5×
        #
        # Layer 3: Feedback tier-weight overrides (per-tier win-rate adjustments)
        #   Adjusted every 30s by SignalFeedbackEngine based on recent trade outcomes.
        _effective_overrides: Dict[str, float] = {}

        # Layer 1: Asset-class terrain weights
        _terrain_weights = _get_asset_tier_weights(symbol)
        if _terrain_weights:
            _effective_overrides.update(_terrain_weights)

        # Layer 2: Market-context mode weights (compound on terrain)
        if market_context is not None:
            ctx_weights = getattr(market_context, "signal_weights", {})
            for k, v in ctx_weights.items():
                _effective_overrides[k] = round(
                    _effective_overrides.get(k, 1.0) * v, 4
                )

        # Layer 3: Feedback tier-weight overrides (compound on terrain + context)
        if tier_weight_overrides:
            for k, v in tier_weight_overrides.items():
                _effective_overrides[k] = round(
                    _effective_overrides.get(k, 1.0) * v, 4
                )

        # ── Feedback tier-weight overrides (from SignalFeedbackEngine) ───────────
        # Applied before independence discount so overlap penalty still reflects
        # actual relative contributions after feedback scaling.
        if _effective_overrides:
            for tier_key, mult in _effective_overrides.items():
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

        # NaN/Inf guard — any upstream NaN (zero-vol ATR, div-by-zero in surge calc)
        # propagates through weighted_score and corrupts size_multiplier downstream.
        # Clamp to zero-score (no trade) rather than letting a NaN past the gate.
        if not np.isfinite(weighted_score):
            logger.warning("coherence_score_non_finite", symbol=symbol,
                           weighted_score=weighted_score, components=components)
            weighted_score = 0.0

        # Clamp to MarketState field ceilings
        weighted_score = min(weighted_score, 10.0)
        raw_score = min(raw_score, 9)  # 9 possible tiers (Tier1-Tier6 + MAG + XVenue + Cascade)

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


# ── Module-level helper functions ─────────────────────────────────────────────
# These are convenience wrappers used by tests and by the interpreter
# to compute ATR ratios and tier weights without instantiating CoherenceEngine.

def compute_atr_vs_baseline(current: float, history: list) -> float:
    """
    Compute current ATR relative to its own 20-bar baseline.

    Returns 1.0 when history is empty (neutral — no information).
    Never divides by zero.

    Args:
        current:  Most recent ATR value (dollars).
        history:  List of up to 20 historical ATR values for the same symbol.

    Returns:
        current / mean(history) — self-calibrating ratio.
        1.0 if history is empty or mean is zero.
    """
    if not history:
        return 1.0
    baseline = sum(history) / len(history)
    if baseline == 0:
        return 1.0
    return current / baseline


def get_tier_weights(symbol: str) -> Dict[str, float]:
    """
    Return coherence tier weights for a symbol with canonical tier-name keys.

    Keys use the tier-number naming convention (tier5_funding, tier6_lead, etc.)
    so tests can assert specific values without depending on internal key names.

    Maps to ASSET_CLASS_TIERS from core.asset_classes:
      tier1_institutional → institutional
      tier2_regime        → regime
      tier3_structure     → structure
      tier4_micro         → microstructure
      tier5_funding       → funding
      tier6_lead          → mag7_macro
      tier7_cross_venue   → cross_venue
      tier8_cascade       → cascade_aftermath
      tier9_flow          → flow_confirmation
    """
    try:
        from core.asset_classes import get_tier_weights as _gtw
        w = _gtw(symbol)
    except Exception:
        w = {}
    return {
        "tier1_institutional": w.get("institutional",     1.0),
        "tier2_regime":        w.get("regime",            1.0),
        "tier3_structure":     w.get("structure",         1.0),
        "tier4_micro":         w.get("microstructure",    1.0),
        "tier5_funding":       w.get("funding",           1.0),
        "tier6_lead":          w.get("mag7_macro",        1.0),
        "tier7_cross_venue":   w.get("cross_venue",       1.0),
        "tier8_cascade":       w.get("cascade_aftermath", 1.0),
        "tier9_flow":          w.get("flow_confirmation", 1.0),
    }


def score_coherence(
    state,
    direction: str,
    signal_age_ms: int = 0,
    symbol: str = "",
) -> tuple:
    """
    Compute coherence score from a MarketState object.

    Returns (weighted_score: float, size_mult: float, reason: str).

    For crypto symbols: applies a hard gate when Tier 4 (microstructure) = 0.
    Crypto trades require at least one microstructure signal.
    Commodities and equities do not have this hard gate.

    Args:
        state:         MarketState with coherence fields populated.
        direction:     "long" | "short" — used for flow bias.
        signal_age_ms: Signal age for freshness decay.
        symbol:        Asset symbol (determines hard gates).
    """
    # Extract symbol from state if not provided
    if not symbol and hasattr(state, "symbol"):
        symbol = state.symbol

    try:
        from core.asset_classes import get_asset_class
        asset_class = get_asset_class(symbol)
    except Exception:
        asset_class = "crypto"

    # Build analyzers_output dict from MarketState fields
    analyzers_output: Dict[str, Any] = {
        "sweep":          getattr(state, "sweep", "none"),
        "sweep_price":    getattr(state, "sweep_price", 0),
        "sweep_side":     getattr(state, "sweep_side", "none"),
        "vpin_hot":       getattr(state, "vpin", 0.0) >= 0.60,
        "vpin":           getattr(state, "vpin", 0.0),
        "imbalance":      getattr(state, "imbalance", 0.0),
        "volume_surge":   getattr(state, "volume_surge", 1.0),
        "candle_conviction": getattr(state, "candle_conviction", 0.0),
        "ssi_status":     getattr(state, "ssi_status", "neutral"),
        "oi_signal":      getattr(state, "oi_signal", "NEUTRAL"),
        "regime":             getattr(state, "regime", "neutral"),
        "regime_confidence":  getattr(state, "regime_confidence", 0.0),
        "market_type":    getattr(state, "market_type", "chop"),
        "funding_class":  getattr(state, "funding_class", "neutral"),
        "tier6_liq_score": getattr(state, "tier6_liq_score", 0.0),
        "mag7_strength":  getattr(state, "mag7_strength", 0.0),
        "tier7_cross_venue_bonus": getattr(state, "tier7_cross_venue_bonus", 0.0),
        "tier8_cascade_fired": getattr(state, "tier8_cascade_fired", False),
        "flow_bias":      getattr(state, "flow_bias", "neutral"),
        "macro_bias":     getattr(state, "macro_bias", direction),
    }

    # Freshness decay
    freshness = 1.0
    if signal_age_ms > 0:
        freshness = max(0.5, 1.0 - signal_age_ms / 60_000)

    engine = CoherenceEngine()
    weighted_score, raw_score, components = engine.calculate_weighted_score(
        symbol=symbol,
        analyzers_output=analyzers_output,
        freshness=freshness,
    )

    # Crypto hard gate: no microstructure signal → score = 0
    if asset_class == "crypto":
        micro = components.get("microstructure", 0.0)
        if micro <= 0.0:
            return 0.0, 0.0, "no_micro_signal_crypto_hard_gate"

    size_mult = engine.get_size_multiplier(weighted_score)
    return weighted_score, size_mult, f"scored_{symbol}"
