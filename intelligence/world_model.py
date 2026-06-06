"""
intelligence/world_model.py — Environmental Ontology Engine.

The World Model does not predict prices. It classifies the current market
environment into a unified WorldState that downstream systems (WillEngine,
Portfolio Allocator) use to modulate risk and sizing.

Design principle: deterministic classification from existing signals.
No new data sources. No ML. Pure logic, fast, testable.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class WorldState:
    """Immutable snapshot of the market environment."""
    risk_appetite: float = 0.5          # 0.0 = full defensive, 1.0 = full aggressive
    preferred_asset_class: str = "mixed"  # "crypto" | "equity" | "commodity" | "mixed"
    volatility_regime: str = "normal"     # "low" | "normal" | "elevated" | "extreme"
    correlation_regime: str = "neutral"   # "divergent" | "convergent" | "neutral"
    liquidity_regime: str = "normal"      # "deep" | "normal" | "thin"
    time_quality: float = 0.5             # 0.0 = blocked, 1.0 = optimal
    narrative: str = ""                   # human-readable summary


class WorldModel:
    """
    Continuous environmental classifier.

    Called every 30s from a background loop (or on every signal).
    All state is recomputed from fresh inputs — no persistent memory.
    """

    # ── Classification tables ────────────────────────────────────────────────

    _DRAWDOWN_RISK_MAP = {
        (0.35, float("inf")): 0.00,
        (0.20, 0.35): 0.05,
        (0.10, 0.20): 0.15,
        (0.05, 0.10): 0.30,
        (0.03, 0.05): 0.45,
        (0.00, 0.03): 0.50,
    }

    _REGIME_RISK_DELTA = {
        "risk_on":       +0.20,
        "alt_season":    +0.15,
        "btc_dominance": +0.10,
        "risk_off":      -0.30,
        "confused":      -0.20,
        "transitioning": -0.15,
        "chop":          -0.10,
        "geopolitical_stress": -0.25,
    }

    _CALENDAR_RISK_DELTA = {
        "BLOCK":    -0.40,
        "CAUTION":  -0.15,
        "CLEAR":    +0.10,
    }

    _VOLATILITY_THRESHOLDS = {
        "extreme":  4.0,   # cascade zscore or ATR/baseline
        "elevated": 2.0,
        "low":      0.5,
    }

    # ── Public API ───────────────────────────────────────────────────────────

    def update(
        self,
        regime: str,
        drawdown_pct: float,          # decimal, e.g. 0.08 = 8%
        macro_direction: str = "none",
        macro_confirmation: float = 0.0,
        cascade_phase: str = "none",
        cascade_zscore: float = 0.0,
        calendar_regime: str = "CLEAR",
        xaut_direction: str = "none",
        xaut_confirms: bool = False,
        atr_vs_baseline: float = 1.0,
        basis_stress_count: int = 0,
        volume_regime: str = "normal",
        cross_market_direction: str = "none",
        cross_market_boost: float = 0.0,
        time_regime: str = "US",        # "ASIAN" | "LONDON" | "US" | "OVERLAP"
        leading_sector: str = "",
        positions: Optional[List[dict]] = None,
    ) -> WorldState:
        """
        Recompute WorldState from current environmental inputs.
        All arguments are optional — missing values use safe defaults.
        """
        _dd = max(0.0, drawdown_pct)

        # 1. Risk appetite ──────────────────────────────────────────────────
        _base_risk = self._lookup_drawdown_risk(_dd)
        _base_risk += self._REGIME_RISK_DELTA.get(regime, 0.0)
        _base_risk += self._CALENDAR_RISK_DELTA.get(calendar_regime, 0.0)

        if cascade_phase in ("expansion", "exhaustion", "primed"):
            _base_risk -= 0.25
        if xaut_confirms and xaut_direction in ("long", "short"):
            _base_risk -= 0.20  # gold risk-off confirmation reduces appetite
        if macro_confirmation >= 0.7:
            _base_risk += 0.10  # strong macro alignment earns confidence
        elif macro_confirmation <= 0.3:
            _base_risk -= 0.10  # macro disagreement = caution

        risk_appetite = round(max(0.0, min(1.0, _base_risk)), 3)

        # 2. Preferred asset class ──────────────────────────────────────────
        preferred = self._classify_preferred_asset_class(
            regime=regime,
            macro_direction=macro_direction,
            xaut_confirms=xaut_confirms,
            leading_sector=leading_sector,
            positions=positions,
        )

        # 3. Volatility regime ──────────────────────────────────────────────
        vol_regime = self._classify_volatility(
            cascade_phase=cascade_phase,
            cascade_zscore=cascade_zscore,
            atr_vs_baseline=atr_vs_baseline,
            basis_stress_count=basis_stress_count,
        )

        # 4. Correlation regime ─────────────────────────────────────────────
        corr_regime = self._classify_correlation(
            macro_confirmation=macro_confirmation,
            cross_market_direction=cross_market_direction,
            cross_market_boost=cross_market_boost,
        )

        # 5. Liquidity regime ───────────────────────────────────────────────
        liq_regime = self._classify_liquidity(
            volume_regime=volume_regime,
            time_regime=time_regime,
            calendar_regime=calendar_regime,
        )

        # 6. Time quality ───────────────────────────────────────────────────
        tq = self._compute_time_quality(
            calendar_regime=calendar_regime,
            time_regime=time_regime,
            cascade_phase=cascade_phase,
        )

        # 7. Narrative ──────────────────────────────────────────────────────
        narrative = self._build_narrative(
            risk_appetite=risk_appetite,
            preferred=preferred,
            vol=vol_regime,
            corr=corr_regime,
            liq=liq_regime,
            regime=regime,
            calendar=calendar_regime,
            time=time_regime,
        )

        return WorldState(
            risk_appetite=risk_appetite,
            preferred_asset_class=preferred,
            volatility_regime=vol_regime,
            correlation_regime=corr_regime,
            liquidity_regime=liq_regime,
            time_quality=tq,
            narrative=narrative,
        )

    # ── Internal classifiers ─────────────────────────────────────────────────

    @classmethod
    def _lookup_drawdown_risk(cls, dd: float) -> float:
        for (lo, hi), risk in cls._DRAWDOWN_RISK_MAP.items():
            if lo <= dd < hi:
                return risk
        return 0.0

    @classmethod
    def _classify_preferred_asset_class(
        cls,
        regime: str,
        macro_direction: str,
        xaut_confirms: bool,
        leading_sector: str,
        positions: Optional[List[dict]],
    ) -> str:
        """Determine which asset class the environment favors."""
        if regime in ("alt_season", "btc_dominance"):
            return "crypto"
        if regime == "risk_off" and xaut_confirms:
            return "commodity"
        if regime == "risk_on" and leading_sector in ("tech", "index_tech"):
            return "equity"
        if macro_direction in ("bullish", "bearish") and leading_sector in ("tech", "mag7"):
            return "equity"
        if regime in ("confused", "transitioning", "chop"):
            return "mixed"

        # Portfolio concentration guard: if already 80%+ in one class, suggest mixed
        if positions:
            _crypto_notional = sum(
                p.get("notional", 0) for p in positions
                if p.get("asset_class") == "crypto"
            )
            _equity_notional = sum(
                p.get("notional", 0) for p in positions
                if p.get("asset_class") == "equity"
            )
            _total = _crypto_notional + _equity_notional
            if _total > 0:
                if _crypto_notional / _total >= 0.80:
                    return "equity"  # suggest rebalancing
                if _equity_notional / _total >= 0.80:
                    return "crypto"

        return "mixed"

    @classmethod
    def _classify_volatility(
        cls,
        cascade_phase: str,
        cascade_zscore: float,
        atr_vs_baseline: float,
        basis_stress_count: int,
    ) -> str:
        _score = 0.0
        if cascade_phase in ("expansion", "exhaustion"):
            _score += 3.0
        if cascade_zscore >= 3.0:
            _score += cascade_zscore
        if atr_vs_baseline >= cls._VOLATILITY_THRESHOLDS["extreme"]:
            return "extreme"
        if atr_vs_baseline >= cls._VOLATILITY_THRESHOLDS["elevated"]:
            _score += 1.5
        if basis_stress_count >= 3:
            _score += 1.0

        if _score >= 4.0:
            return "extreme"
        if _score >= 2.0:
            return "elevated"
        if atr_vs_baseline <= cls._VOLATILITY_THRESHOLDS["low"]:
            return "low"
        return "normal"

    @classmethod
    def _classify_correlation(
        cls,
        macro_confirmation: float,
        cross_market_direction: str,
        cross_market_boost: float,
    ) -> str:
        if cross_market_direction == "diverging" and cross_market_boost > 0:
            return "divergent"
        if macro_confirmation >= 0.7:
            return "convergent"
        return "neutral"

    @classmethod
    def _classify_liquidity(
        cls,
        volume_regime: str,
        time_regime: str,
        calendar_regime: str,
    ) -> str:
        if calendar_regime == "BLOCK":
            return "thin"
        if time_regime == "OVERLAP":
            return "deep"
        if volume_regime == "high":
            return "deep"
        if volume_regime == "low":
            return "thin"
        return "normal"

    @classmethod
    def _compute_time_quality(
        cls,
        calendar_regime: str,
        time_regime: str,
        cascade_phase: str,
    ) -> float:
        if calendar_regime == "BLOCK":
            return 0.0
        if calendar_regime == "CAUTION":
            return 0.3
        _base = {
            "OVERLAP": 1.0,
            "US":      0.9,
            "LONDON":  0.8,
            "ASIAN":   0.6,
        }.get(time_regime, 0.5)
        if cascade_phase in ("expansion", "exhaustion"):
            _base *= 0.7  # cascade reduces execution quality
        return round(_base, 2)

    @classmethod
    def _build_narrative(
        cls,
        risk_appetite: float,
        preferred: str,
        vol: str,
        corr: str,
        liq: str,
        regime: str,
        calendar: str,
        time: str,
    ) -> str:
        parts = []
        if risk_appetite >= 0.7:
            parts.append("aggressive")
        elif risk_appetite <= 0.2:
            parts.append("defensive")
        else:
            parts.append("measured")

        parts.append(f"{preferred}-biased")
        parts.append(f"{vol}-vol")
        parts.append(f"{corr}-corr")
        parts.append(f"{liq}-liq")
        parts.append(f"regime={regime}")
        parts.append(f"calendar={calendar}")
        parts.append(f"time={time}")

        return " | ".join(parts)
