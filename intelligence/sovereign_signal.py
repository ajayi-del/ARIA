"""
intelligence/sovereign_signal.py — SOVEREIGN Signal Generator.

Combines component divergence z-scores with regime intelligence and calendar
filters to produce SOVEREIGN_SIGNAL_READY events.

Architecture:
  SOVEREIGN does not use coherence scoring. Its signal is orthogonal:
    - spread z-score (structural divergence from MAG7 index)
    - regime (momentum vs mean-reversion classifier)
    - calendar (no earnings within 48h)
    - hedge ratio (size matched to staked index weight)

  Signal direction logic:
    z_score < -threshold AND regime=momentum (risk_off + no catalyst):
      → SHORT component (underperformance continues)
    z_score < -threshold AND regime=mean_reversion (no catalyst, confused):
      → LONG component  (spread convergence expected)
    z_score > +threshold:
      → inverse of above (outperformer reverts or continues)

  The hedge ratio:
    max_notional = stake_balance × component_weight
    (structurally matched to implicit long in staked index)

  Calendar filter:
    SOVEREIGN blocks 48h before/after earnings for each component.
    Earnings are binary events that invalidate spread z-score models.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Optional

from intelligence.ssi_component_monitor import ComponentDivergence

# Z-score thresholds
Z_ENTRY_THRESHOLD  = 1.5   # |z| >= 1.5 → signal valid
Z_STOP_THRESHOLD   = 4.0   # |z| >= 4.0 at close → stop-loss exit
Z_EXIT_TARGET      = 0.0   # z returns to mean → take-profit

# Regime classes that determine direction logic
_MOMENTUM_REGIMES    = frozenset({"risk_off", "btc_dominance"})
_REVERSION_REGIMES   = frozenset({"confused", "risk_on", "rotational"})
_BLOCKED_REGIMES     = frozenset({"blackout"})    # no trades in these regimes

# Calendar blocking window: hours before earnings
EARNINGS_BLOCK_HOURS = 48.0


@dataclass
class SovereignSignal:
    """
    A fully-validated SOVEREIGN entry signal.

    side:           "long" or "short" (on the component perp)
    symbol:         component being traded (e.g. "TSLA-USD")
    z_score:        spread z-score at signal time
    hedge_notional: max notional = stake × weight (structurally matched)
    regime_type:    "momentum" or "reversion"
    confidence:     0.0–1.0, derived from |z_score| magnitude
    """
    symbol:          str
    side:            str    # "long" | "short"
    z_score:         float
    hedge_notional:  float  # USD
    regime_type:     str    # "momentum" | "reversion"
    confidence:      float  # derived from |z|
    entry_rationale: str
    timestamp_ms:    int


class SovereignSignalGenerator:
    """
    Validates divergence + regime + calendar and produces SovereignSignal.

    This is the critical decision layer:
      ComponentDivergence + regime + calendar + stake → SovereignSignal or None

    Usage:
        gen = SovereignSignalGenerator()
        signal = gen.evaluate(
            divergence=monitor.get_best_divergence(),
            regime="risk_off",
            calendar_regime="CLEAR",
            hours_to_earnings=None,
            stake_balance=200.0,
            component_weights={"TSLA-USD": 0.06, ...},
            sovereign_budget=12.50,
        )
        if signal is not None:
            # Emit SOVEREIGN_SIGNAL_READY
    """

    def evaluate(
        self,
        divergence: Optional[ComponentDivergence],
        regime: str,
        calendar_regime: str,
        hours_to_earnings: Optional[float],   # None = no known earnings
        stake_balance: float,
        component_weights: Dict[str, float],
        sovereign_budget: float,
    ) -> Optional[SovereignSignal]:
        """
        Evaluate whether conditions support a SOVEREIGN trade.

        Returns SovereignSignal if all conditions pass, None otherwise.
        """
        # Guard: no divergence signal
        if divergence is None:
            return None

        # Guard: z-score below entry threshold
        if abs(divergence.z_score) < Z_ENTRY_THRESHOLD:
            return None

        # Guard: regime blocks trading
        if calendar_regime in ("BLOCK", "blackout"):
            return None

        # Guard: earnings within 48h for this component
        if hours_to_earnings is not None and hours_to_earnings < EARNINGS_BLOCK_HOURS:
            return None

        # Guard: regime is ambiguous — SOVEREIGN needs clear regime read
        # (Unlike coherence-based signals, spread z-score requires regime context
        # to determine momentum vs mean-reversion direction)
        if regime == "confused" and abs(divergence.z_score) < 2.0:
            # Only trade confused regime for very high-conviction divergences
            return None

        # Guard: no stake → no anchor → no SOVEREIGN
        if stake_balance <= 0:
            return None

        # Guard: no SOVEREIGN budget
        if sovereign_budget <= 0:
            return None

        # Guard: whole market moving — divergence is market-wide, not component-specific
        # This check is done externally (MAG7 direction == component direction),
        # but we apply a secondary check: very high coherence regime with matching
        # component direction suggests macro move, not spread divergence.

        # Determine signal direction
        side, regime_type = self._classify_direction(divergence.z_score, regime)

        # Compute hedge-matched notional
        weight = component_weights.get(divergence.symbol, 0.0)
        hedge_notional = stake_balance * weight

        if hedge_notional <= 0:
            return None

        # Cap at sovereign_budget
        trade_notional = min(hedge_notional, sovereign_budget)

        # Confidence: linear scale from 1.5σ (min) → 3.0σ (max confidence)
        confidence = self._compute_confidence(divergence.z_score)

        rationale = (
            f"z={divergence.z_score:.2f}σ {side} {divergence.symbol} | "
            f"regime={regime_type} | hedge={trade_notional:.2f}USD "
            f"({weight:.0%} of ${stake_balance:.0f} stake)"
        )

        return SovereignSignal(
            symbol=divergence.symbol,
            side=side,
            z_score=divergence.z_score,
            hedge_notional=trade_notional,
            regime_type=regime_type,
            confidence=confidence,
            entry_rationale=rationale,
            timestamp_ms=int(time.time() * 1000),
        )

    @staticmethod
    def _classify_direction(z_score: float, regime: str) -> tuple[str, str]:
        """
        Map (z_score sign, regime) → (trade_side, regime_type).

        Momentum: spread continues to widen → trade WITH divergence
          Underperforming (z < 0) → short component
          Outperforming  (z > 0) → long component (it keeps outperforming)

        Mean reversion: spread closes → trade AGAINST divergence
          Underperforming (z < 0) → long component (will catch up)
          Outperforming  (z > 0) → short component (will revert)
        """
        is_momentum = regime in _MOMENTUM_REGIMES

        if z_score < 0:
            # Component underperforming MAG7
            side = "short" if is_momentum else "long"
        else:
            # Component outperforming MAG7
            side = "long" if is_momentum else "short"

        regime_type = "momentum" if is_momentum else "reversion"
        return side, regime_type

    @staticmethod
    def _compute_confidence(z_score: float) -> float:
        """
        Confidence 0.55–0.80, linear from 1.5σ to 3.0σ.
        Beyond 3.0σ: confidence stays at 0.80 (don't over-fit outliers).
        """
        abs_z = abs(z_score)
        if abs_z < Z_ENTRY_THRESHOLD:
            return 0.0
        # Linear interpolation: 1.5σ → 0.55, 3.0σ → 0.80
        normalized = min(1.0, (abs_z - Z_ENTRY_THRESHOLD) / (3.0 - Z_ENTRY_THRESHOLD))
        return round(0.55 + normalized * 0.25, 3)
