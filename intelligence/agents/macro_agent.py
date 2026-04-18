"""
intelligence/agents/macro_agent.py — MacroAgent: MAG7.ssi inflow perception.

Perceives macro capital flows into the MAG7.ssi index.
Natural frequency: 15 minutes (SSI polling cadence).

Philosophical role: the macro dimension is structural gravity.
Short-term price moves can contradict macro flow; they rarely survive it.
A trade against strong MAG7 inflow has asymmetric risk — the tide is incoming.
"""

from __future__ import annotations

import time
import structlog
from .base import BaseAgent, AgentOutput, TradeOutcome

log = structlog.get_logger(__name__)

# Inflow classification thresholds
_STRONG_THRESHOLD = 0.70   # inflow_strength > 0.70 → strong signal
_NEUTRAL_BAND     = 0.40   # below this → neutral


class MacroAgent(BaseAgent):
    """
    Reads MAG7.ssi sosovalue and inflow score to determine capital flow direction.

    Invoked by:
      - ssi_loop() completion (15-minute cadence)
      - Startup after warmup
    """

    def __init__(self, ssi_store: dict | None = None, symbols: list | None = None) -> None:
        super().__init__()
        self._ssi_store  = ssi_store or {}
        self._symbols    = symbols or []

    @property
    def name(self) -> str:
        return "macro"

    @property
    def natural_frequency_seconds(self) -> float:
        return 900.0   # 15 minutes

    @property
    def symbols(self) -> list:
        return self._symbols

    def update_ssi_store(self, store: dict) -> None:
        self._ssi_store = store

    async def perceive(self, symbol: str, **context) -> AgentOutput:
        self.record_invocation()
        try:
            return self._store(self._perceive_internal(symbol, **context))
        except Exception as e:
            log.warning("macro_agent_perceive_error", symbol=symbol, error=str(e))
            return self._store(self._make_neutral(symbol, reason="error"))

    def _perceive_internal(self, symbol: str, **context) -> AgentOutput:
        reason = context.get("reason", "ssi_poll")

        # Read inflow from SSI store (populated by ssi_loop)
        ssi_data         = self._ssi_store.get("MAG7SSI-USD") or self._ssi_store.get("MAG7SSI_USDC") or {}
        inflow_score     = float(ssi_data.get("inflow_score", 0.0) or 0.0)
        ssi_value        = float(ssi_data.get("value", 0.0) or 0.0)
        ssi_24h_change   = float(ssi_data.get("change_24h", 0.0) or 0.0)

        # Fallback: derive from context keys
        if inflow_score == 0.0:
            inflow_score = float(context.get("mag7_inflow_score", 0.0) or 0.0)

        inflow_strength = min(1.0, abs(inflow_score))

        # Dynamic confidence: scales linearly with signal strength.
        # Maps inflow_strength [0, 1] → confidence [0.50, 0.85].
        # Philosophically: a barely-threshold inflow (0.40) warrants modest
        # confidence (0.64); only near-perfect saturation (1.0) earns the cap
        # (0.85). No binary cliff-edges.
        confidence = round(min(0.85, 0.50 + inflow_strength * 0.35), 3)

        # Classify
        if inflow_score >= _STRONG_THRESHOLD:
            classification = "strong_inflow"
            direction      = "long"
        elif inflow_score >= _NEUTRAL_BAND:
            classification = "inflow"
            direction      = "long"
        elif inflow_score <= -_STRONG_THRESHOLD:
            classification = "strong_outflow"
            direction      = "short"
        elif inflow_score <= -_NEUTRAL_BAND:
            classification = "outflow"
            direction      = "short"
        else:
            classification = "neutral"
            direction      = "neutral"
            confidence     = 0.50

        fired = direction != "neutral"

        return AgentOutput(
            agent_name        = self.name,
            symbol            = symbol,
            timestamp_ms      = int(time.time() * 1000),
            fired             = fired,
            direction         = direction,
            confidence        = confidence,
            invocation_reason = reason,
            raw_data          = {
                "mag7_inflow":     classification,
                "ssi_value":       ssi_value,
                "ssi_24h_change":  ssi_24h_change,
                "inflow_strength": inflow_strength,
                "inflow_score":    inflow_score,
            },
        )

    def is_correct(self, output: AgentOutput, outcome: TradeOutcome) -> bool:
        """
        Neutral calls are always correct — the agent correctly demurred.
        Directional calls correct when the direction matched the profitable side.
        """
        try:
            direction = output.direction
            if direction == "neutral":
                return True
            if direction == "long"  and outcome.net_pnl_r > 0:
                return True
            if direction == "short" and outcome.net_pnl_r < 0:
                return True
            return False
        except Exception:
            return False
