"""
intelligence/agents/funding_agent.py — FundingAgent: funding rate perception.

Reads funding rates and carry scores to identify unsustainable positioning.
Natural frequency: 1 hour (funding settlement cadence).

Philosophical role: funding is the market's confession of its own excess.
When longs pay shorts at extreme rates, the market is telling us it has
overstretched itself. The FundingAgent reads this confession and positions
against the crowd — not from arrogance, but from structural reversion math.
Extreme funding always regresses to mean. Always.
"""

from __future__ import annotations

import time
import structlog
from .base import BaseAgent, AgentOutput, TradeOutcome

log = structlog.get_logger(__name__)

# Funding classification thresholds (annualized basis points)
_EXTREME_POS = 0.05    # 5% annualized → extreme positive (longs overpaying)
_MODERATE_POS = 0.01   # 1% annualized
_EXTREME_NEG  = -0.05  # -5% annualized → extreme negative
_MODERATE_NEG = -0.01  # -1% annualized


class FundingAgent(BaseAgent):
    """
    Reads funding history and radar to classify carry regime and detect arb signals.

    Invoked by:
      - funding_loop() completion (hourly cadence)
      - New funding rate received from SoDEX
    """

    def __init__(
        self,
        funding_history=None,
        funding_radar=None,
        symbols: list | None = None,
    ) -> None:
        super().__init__()
        self._funding_history = funding_history
        self._funding_radar   = funding_radar
        self._symbols         = symbols or []

    @property
    def name(self) -> str:
        return "funding"

    @property
    def natural_frequency_seconds(self) -> float:
        return 3600.0   # 1 hour

    @property
    def symbols(self) -> list:
        return self._symbols

    async def perceive(self, symbol: str, **context) -> AgentOutput:
        self.record_invocation()
        try:
            return self._store(self._perceive_internal(symbol, **context))
        except Exception as e:
            log.warning("funding_agent_perceive_error", symbol=symbol, error=str(e))
            return self._store(self._make_neutral(symbol, reason="error",
                                                  classification="neutral"))

    def _perceive_internal(self, symbol: str, **context) -> AgentOutput:
        reason = context.get("reason", "funding_loop")

        # Read funding rate
        funding_rate  = 0.0
        funding_24h   = 0.0
        carry_score   = 0.0
        arb_signal    = False
        arb_direction = "none"

        if self._funding_history is not None:
            try:
                rates = self._funding_history.get_recent(symbol, n=8) or []
                if rates:
                    funding_rate = float(rates[-1] if isinstance(rates[-1], float) else
                                         rates[-1].get("rate", 0.0) if isinstance(rates[-1], dict) else 0.0)
                    funding_24h  = sum(
                        float(r if isinstance(r, float) else r.get("rate", 0.0) if isinstance(r, dict) else 0.0)
                        for r in rates[:8]
                    ) / max(len(rates[:8]), 1)
            except Exception:
                pass

        if self._funding_radar is not None:
            try:
                snap = self._funding_radar.get_snapshot(symbol)
                if snap:
                    carry_score = float(getattr(snap, "carry_score", 0.0) or 0.0)
                    arb_signal  = bool(getattr(snap, "arb_signal", False))
                    arb_dir     = getattr(snap, "arb_direction", "none") or "none"
                    arb_direction = str(arb_dir)
            except Exception:
                pass

        # Classify
        effective_rate = carry_score * 0.5 + funding_24h * 0.5
        if effective_rate >= _EXTREME_POS or funding_rate >= _EXTREME_POS:
            classification = "extreme_positive"
        elif effective_rate >= _MODERATE_POS:
            classification = "positive"
        elif effective_rate <= _EXTREME_NEG or funding_rate <= _EXTREME_NEG:
            classification = "extreme_negative"
        elif effective_rate <= _MODERATE_NEG:
            classification = "negative"
        else:
            classification = "neutral"

        # Estimate next settlement payment (per 8h period)
        predicted_settlement = funding_rate * 8.0   # rough estimate in rate units

        # Direction logic: funding extremes predict mean reversion.
        # Dynamic confidence:
        #   Arb: scales with carry_score (0 → 0.65; 0.5+ → 0.85).
        #     confidence = min(0.85, 0.65 + min(|carry_score| / 0.5, 1.0) × 0.20)
        #   Extreme: scales with how far above the extreme threshold the rate sits.
        #     rate_excess = min(1.0, (|effective_rate| - |threshold|) / |threshold|)
        #     confidence = min(0.80, 0.60 + rate_excess × 0.20)
        #     At exactly threshold: 0.60; at 2× threshold: 0.80.
        if arb_signal:
            _carry_conf = min(1.0, abs(carry_score) / 0.5)
            direction   = arb_direction  # "short_arb" or "long_arb"
            confidence  = round(min(0.85, 0.65 + _carry_conf * 0.20), 3)
            fired       = True
        elif classification == "extreme_positive":
            _rate_excess = min(1.0, (effective_rate - _EXTREME_POS) / _EXTREME_POS)
            direction    = "short"        # longs paying too much → reversal coming
            confidence   = round(min(0.80, 0.60 + _rate_excess * 0.20), 3)
            fired        = True
        elif classification == "extreme_negative":
            _rate_excess = min(1.0, (abs(effective_rate) - abs(_EXTREME_NEG)) / abs(_EXTREME_NEG))
            direction    = "long"         # shorts paying too much → cover coming
            confidence   = round(min(0.80, 0.60 + _rate_excess * 0.20), 3)
            fired        = True
        else:
            direction  = "neutral"
            confidence = 0.50
            fired      = False

        return AgentOutput(
            agent_name        = self.name,
            symbol            = symbol,
            timestamp_ms      = int(time.time() * 1000),
            fired             = fired,
            direction         = direction,
            confidence        = confidence,
            invocation_reason = reason,
            raw_data          = {
                "funding_rate":         round(funding_rate, 6),
                "funding_24h_avg":      round(funding_24h, 6),
                "carry_score":          round(carry_score, 3),
                "classification":       classification,
                "arb_signal":           arb_signal,
                "arb_direction":        arb_direction,
                "predicted_settlement": round(predicted_settlement, 6),
            },
        )

    def is_correct(self, output: AgentOutput, outcome: TradeOutcome) -> bool:
        """
        Extreme funding calls: correct if mean reversion occurred (trade won).
        Arb calls: correct if arb was net profitable.
        Neutral: always correct.
        """
        try:
            if not output.fired:
                return True
            return outcome.net_pnl_r > 0
        except Exception:
            return False
