"""
intelligence/agents/regime_agent.py — RegimeAgent: relative strength perception.

Perceives which assets are leading and lagging to determine regime alignment.
Natural frequency: 15 minutes (candle analysis cadence).

Philosophical role: regime is the water in which all fish swim.
A trade that aligns with regime is swimming with the current.
A trade that fights regime is betting the tide will turn before the stop is hit.
"""

from __future__ import annotations

import time
import structlog
from .base import BaseAgent, AgentOutput, TradeOutcome

log = structlog.get_logger(__name__)

# Regimes where each symbol has a directional bias
_BULLISH_REGIMES = {"risk_on", "alt_season", "btc_dominance", "tech_led"}
_BEARISH_REGIMES = {"risk_off"}

# Assets favoured per regime
_REGIME_LEADERS: dict = {
    "risk_on":      {"BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD"},
    "risk_off":     {"XAUT-USD"},
    "alt_season":   {"SOL-USD", "BNB-USD", "LINK-USD", "AVAX-USD"},
    "btc_dominance":{"BTC-USD"},
    "tech_led":     {"NVDA-USD", "MSFT-USD", "AAPL-USD", "TSLA-USD"},
    "confused":     set(),
    "rotational":   set(),
}


class RegimeAgent(BaseAgent):
    """
    Reads relative strength output to classify market regime and determine
    whether the current symbol is favoured.

    Invoked by:
      - Intelligence loop completion (15-minute cadence)
      - After 15 candles have closed since last computation
    """

    def __init__(
        self,
        candle_buffers: dict | None = None,
        relative_strength_engine=None,
        symbols: list | None = None,
    ) -> None:
        super().__init__()
        self._candle_buffers = candle_buffers or {}
        self._rs_engine      = relative_strength_engine
        self._symbols        = symbols or []

    @property
    def name(self) -> str:
        return "regime"

    @property
    def natural_frequency_seconds(self) -> float:
        return 900.0   # 15 minutes

    @property
    def symbols(self) -> list:
        return self._symbols

    async def perceive(self, symbol: str, **context) -> AgentOutput:
        self.record_invocation()
        try:
            return self._store(self._perceive_internal(symbol, **context))
        except Exception as e:
            log.warning("regime_agent_perceive_error", symbol=symbol, error=str(e))
            return self._store(self._make_neutral(symbol, reason="error"))

    def _perceive_internal(self, symbol: str, **context) -> AgentOutput:
        reason = context.get("reason", "intelligence_loop")

        # Get regime from RS engine or context
        regime          = "confused"
        leading_asset   = ""
        lagging_asset   = ""
        regime_strength = 0.0
        regime_age      = 0

        if self._rs_engine is not None:
            try:
                rs = self._rs_engine
                regime        = getattr(rs, "current_regime", "confused") or "confused"
                leading_asset = getattr(rs, "leading_asset", "") or ""
                lagging_asset = getattr(rs, "lagging_asset", "") or ""
                # Use explicit None-check — 0.0 is a valid strength (new regime)
                _raw_strength = getattr(rs, "regime_strength", None)
                regime_strength = float(_raw_strength) if _raw_strength is not None else 0.5
                regime_age    = int(getattr(rs, "regime_age_candles", 0) or 0)
            except Exception as _re:
                log.debug("regime_agent_rs_read_error", error=str(_re))

        # Fall back to context keys (e.g. from personality context)
        if not regime or regime == "confused":
            regime = str(context.get("regime", "confused") or "confused")

        leaders         = _REGIME_LEADERS.get(regime, set())
        symbol_aligned  = symbol in leaders

        # Direction logic with dynamic confidence.
        # Bullish aligned: min(0.85, 0.70 + strength × 0.15) — as before.
        # Bearish aligned: min(0.80, 0.65 + strength × 0.15) — slightly lower
        #   ceiling; bearish regimes are shorter-lived and noisier.
        # Bearish non-aligned (short): min(0.65, 0.45 + strength × 0.20) —
        #   shorting against a non-favoured regime is educated but uncertain.
        # Confused: 0.40 — the agent acknowledges seeing nothing actionable.
        if regime == "confused" or regime == "rotational":
            direction  = "neutral"
            confidence = 0.40
            fired      = False
        elif regime in _BEARISH_REGIMES:
            if symbol_aligned:        # XAUT in risk_off = long
                direction  = "long"
                confidence = round(min(0.80, 0.65 + regime_strength * 0.15), 3)
                fired      = True
            else:
                direction  = "short"
                confidence = round(min(0.65, 0.45 + regime_strength * 0.20), 3)
                fired      = True
        elif regime in _BULLISH_REGIMES:
            if symbol_aligned:
                direction  = "long"
                confidence = round(min(0.85, 0.70 + regime_strength * 0.15), 3)
                fired      = True
            else:
                direction  = "neutral"
                confidence = 0.30
                fired      = False
        else:
            direction  = "neutral"
            confidence = 0.40
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
                "regime":             regime,
                "leading_asset":      leading_asset,
                "lagging_asset":      lagging_asset,
                "symbol_aligned":     symbol_aligned,
                "regime_strength":    regime_strength,
                "regime_age_candles": regime_age,
            },
        )

    def is_correct(self, output: AgentOutput, outcome: TradeOutcome) -> bool:
        """
        Correct when:
          - Symbol was aligned to regime AND trade won (agent's vote validated)
          - Symbol was NOT aligned (agent said neutral) — abstention is never wrong
        """
        try:
            aligned = output.raw_data.get("symbol_aligned", False)
            if not aligned:
                return True   # agent correctly demurred
            if aligned and outcome.net_pnl_r > 0:
                return True
            return False
        except Exception:
            return False
