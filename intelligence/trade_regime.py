"""
intelligence/trade_regime.py — Trade Regime Classifier

Determines whether a given signal should be traded as a TREND, SCALP, or DEFAULT
entry. This drives:
  • Dynamic leverage      (scalp → up to 10×, trend → 5×)
  • Dynamic profit caps   (trend → 10% ROE, scalp → 4% ROE, default → 6%)
  • Trailing-stop style   (trend → wide MFE trail, scalp → tight MFE trail)

Inputs (all already computed on the hot path — zero extra I/O):
  • kant_structure        MarketStructure enum from KantEngine.assess()
  • atr_vs_baseline       float — ATR / baseline ATR
  • session_type          str — "asian" | "london" | "us" | "overlap"
  • cascade_phase         str — from cascade_tracker

Rules (priority order):
  1. CHAOS / DISTRIBUTION  → DEFAULT (no directional conviction)
  2. TREND                 → TREND
  3. ACCUMULATION          → SCALP (coil / range-bound, quick moves)
  4. NORMAL + atr < 0.8    → SCALP (low vol, mean-reversion favourable)
  5. NORMAL + atr >= 0.8   → DEFAULT
  6. overlap session       → SCALP (short windows, quick in/out)
  7. cascade in momentum   → TREND (directional thrust)

Latency: O(1) dict lookups + float compares. ~0.01 ms.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from intelligence.kant_engine import MarketStructure


class TradeRegime(Enum):
    TREND   = "trend"
    SCALP   = "scalp"
    DEFAULT = "default"


# ── Leverage map ─────────────────────────────────────────────────────────────
# SCALP: higher leverage because hold time is short and profit cap is tight.
# TREND: baseline leverage — let the move work.
# DEFAULT: baseline leverage.
# All values clamped to per-symbol max_leverage at execution time.
_LEVERAGE_MAP: dict[str, int] = {
    TradeRegime.TREND.value:   5,   # default / conservative
    TradeRegime.SCALP.value:   10,  # up to 10× — clamped by symbol max
    TradeRegime.DEFAULT.value: 5,
}

# ── ROE profit caps ──────────────────────────────────────────────────────────
# Return-on-Equity % at which the position is force-closed.
# TREND: let winners run — 10% ROE
# SCALP: lock in quickly — 4% ROE  (whipsaw protection)
# DEFAULT: middle ground — 6% ROE
_ROE_CAP_MAP: dict[str, float] = {
    TradeRegime.TREND.value:   10.0,
    TradeRegime.SCALP.value:    4.0,
    TradeRegime.DEFAULT.value:  6.0,
}

# ── Trailing-stop MFE params ─────────────────────────────────────────────────
# (activation_pct_of_mfe, retention_pct_of_mfe)
# Trend: activate early (30% of MFE), retain 70%  → wide trail
# Scalp: activate late  (60% of MFE), retain 85%  → tight trail
_TRAIL_MFE_MAP: dict[str, tuple[float, float]] = {
    TradeRegime.TREND.value:   (0.30, 0.70),
    TradeRegime.SCALP.value:   (0.60, 0.85),
    TradeRegime.DEFAULT.value: (0.45, 0.75),
}


class TradeRegimeClassifier:
    """
    Stateless classifier — call classify() per signal.
    Thread-safe: no mutable state.
    """

    @staticmethod
    def classify(
        kant_structure,
        atr_vs_baseline: float,
        session_type: str = "",
        cascade_phase: str = "",
    ) -> TradeRegime:
        struct = ""
        if kant_structure is not None:
            struct = getattr(kant_structure, "value", str(kant_structure))

        # 1. CHAOS / DISTRIBUTION → no clear direction
        if struct in ("chaos", "distribution"):
            return TradeRegime.DEFAULT

        # 2. TREND → directional thrust
        if struct == "trend":
            return TradeRegime.TREND

        # 3. Cascade momentum → directional thrust (even if Kant says NORMAL)
        if cascade_phase in ("momentum", "building", "expansion"):
            return TradeRegime.TREND

        # 4. ACCUMULATION → coil / range, scalp the breakout
        if struct == "accumulation":
            return TradeRegime.SCALP

        # 5. Overlap session → short windows
        if session_type == "overlap":
            return TradeRegime.SCALP

        # 6. NORMAL + low vol → scalp mean-reversion
        if struct == "normal" and atr_vs_baseline < 0.8:
            return TradeRegime.SCALP

        # 7. Fallback — when no Kant structure, use ATR heuristic
        if not struct:
            if atr_vs_baseline >= 1.2:
                return TradeRegime.TREND
            if atr_vs_baseline <= 0.8:
                return TradeRegime.SCALP

        return TradeRegime.DEFAULT

    @staticmethod
    def get_leverage(regime: TradeRegime) -> int:
        return _LEVERAGE_MAP.get(regime.value, 5)

    @staticmethod
    def get_roe_cap(regime: TradeRegime) -> float:
        return _ROE_CAP_MAP.get(regime.value, 6.0)

    @staticmethod
    def get_trail_mfe_params(regime: TradeRegime) -> tuple[float, float]:
        return _TRAIL_MFE_MAP.get(regime.value, (0.45, 0.75))
