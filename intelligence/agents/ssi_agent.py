"""
intelligence/agents/ssi_agent.py — SSIAgent: OI lead, CEX divergence, MAG7 momentum.

Reads open interest trends, CEX/DEX price divergence, and MAG7 momentum.
Natural frequency: 15 minutes (Ostium and SSI polling cadence).

Philosophical role: OI is the market's commitment ledger.
Price is ephemeral; open interest is capital at risk — real skin in the game.
Expanding OI in a bull move means new longs are entering (conviction).
Expanding OI in a bear move means new shorts are entering (suppression).
The direction of OI tells you who is betting and with what conviction.
SoDEX/CEX divergence tells you where smart money is positioning.
"""

from __future__ import annotations

import time
import structlog
from .base import BaseAgent, AgentOutput, TradeOutcome

log = structlog.get_logger(__name__)

_CEX_PREMIUM_THRESHOLD  =  0.0015   # SoDEX mark > CEX by 0.15% → premium
_CEX_DISCOUNT_THRESHOLD = -0.0015   # SoDEX mark < CEX by 0.15% → discount


class SSIAgent(BaseAgent):
    """
    Reads Ostium OI feed, Binance reference prices, and MAG7 momentum.

    Invoked by:
      - ostium_loop() completion (15-minute cadence)
      - ssi_loop() completion
    """

    def __init__(
        self,
        ostium_feed=None,
        binance_ref: dict | None = None,
        mark_price_stores: dict | None = None,
        ssi_momentum: dict | None = None,
        symbols: list | None = None,
    ) -> None:
        super().__init__()
        self._ostium      = ostium_feed
        self._binance_ref = binance_ref or {}
        self._mp_stores   = mark_price_stores or {}
        self._ssi_momentum = ssi_momentum or {}
        self._symbols      = symbols or []

    @property
    def name(self) -> str:
        return "ssi"

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
            log.warning("ssi_agent_perceive_error", symbol=symbol, error=str(e))
            return self._store(self._make_neutral(symbol, reason="error",
                                                  oi_trend="flat",
                                                  oi_direction="neutral"))

    def _perceive_internal(self, symbol: str, **context) -> AgentOutput:
        reason = context.get("reason", "ostium_poll")

        # ── OI data ────────────────────────────────────────────────────────
        oi_trend     = "flat"
        oi_direction = "neutral"
        lead_signal  = ""
        oi_change_pct = 0.0   # kept in outer scope for confidence calculation

        if self._ostium is not None:
            try:
                oi_data = self._ostium.get(symbol) if hasattr(self._ostium, "get") else None
                if oi_data:
                    oi_change_pct = float(getattr(oi_data, "oi_change_pct", 0.0) or 0.0)
                    price_change  = float(getattr(oi_data, "price_change_pct", 0.0) or 0.0)
                    oi_trend = "expanding" if oi_change_pct > 0.01 else (
                               "contracting" if oi_change_pct < -0.01 else "flat")

                    # OI direction: OI expanding + price up = bullish
                    if oi_trend == "expanding" and price_change > 0:
                        oi_direction = "bullish_expansion"
                    elif oi_trend == "expanding" and price_change < 0:
                        oi_direction = "bearish_expansion"
                    elif oi_trend == "contracting" and price_change > 0:
                        oi_direction = "short_covering"
                    elif oi_trend == "contracting" and price_change < 0:
                        oi_direction = "long_liquidation"

                    lead_signal = getattr(oi_data, "lead_signal", "") or ""
            except Exception:
                pass

        # ── CEX divergence ─────────────────────────────────────────────────
        cex_divergence = 0.0
        cex_signal     = "aligned"

        mp_store = self._mp_stores.get(symbol)
        sodex_mark = getattr(mp_store, "mark_price", 0.0) if mp_store else 0.0

        cex_price = float(self._binance_ref.get(symbol, {}).get("price", 0.0) or 0.0)
        if sodex_mark > 0 and cex_price > 0:
            cex_divergence = (sodex_mark - cex_price) / cex_price
            if cex_divergence > _CEX_PREMIUM_THRESHOLD:
                cex_signal = "sodex_premium"    # SoDEX expensive vs CEX → short bias
            elif cex_divergence < _CEX_DISCOUNT_THRESHOLD:
                cex_signal = "sodex_discount"   # SoDEX cheap vs CEX → long bias

        # ── MAG7 momentum ──────────────────────────────────────────────────
        mag7_momentum = "flat"
        if symbol in ("NVDA-USD", "MSFT-USD", "AAPL-USD", "AMZN-USD",
                      "GOOGL-USD", "META-USD", "TSLA-USD"):
            ssi_change = float(self._ssi_momentum.get("mag7_change_1h", 0.0) or 0.0)
            if ssi_change > 0.005:
                mag7_momentum = "accelerating"
            elif ssi_change < -0.005:
                mag7_momentum = "decelerating"

        # ── Direction logic ────────────────────────────────────────────────
        # Dynamic confidence — proportional to signal magnitude:
        #   OI expansion (bullish/bearish): scale with |oi_change_pct|.
        #     oi_mag = min(1.0, |oi_change_pct| / 0.05)  → 0 at 0.01, 1.0 at 0.05+
        #     confidence = min(0.80, 0.55 + oi_mag × 0.25)
        #   OI contraction (short_covering/long_liq): weaker signal, lower ceiling.
        #     confidence = min(0.70, 0.48 + oi_mag × 0.22)
        #   CEX divergence: scale with |cex_divergence|.
        #     cex_mag = min(1.0, |cex_divergence| / 0.005)
        #     confidence = min(0.75, 0.55 + cex_mag × 0.20)
        _oi_mag = min(1.0, abs(oi_change_pct) / 0.05)
        _cex_mag = min(1.0, abs(cex_divergence) / 0.005)

        if oi_direction == "bullish_expansion":
            direction, confidence, fired = "long",  round(min(0.80, 0.55 + _oi_mag * 0.25), 3), True
        elif oi_direction == "bearish_expansion":
            direction, confidence, fired = "short", round(min(0.80, 0.55 + _oi_mag * 0.25), 3), True
        elif oi_direction == "short_covering":
            direction, confidence, fired = "long",  round(min(0.70, 0.48 + _oi_mag * 0.22), 3), True
        elif oi_direction == "long_liquidation":
            direction, confidence, fired = "short", round(min(0.70, 0.48 + _oi_mag * 0.22), 3), True
        elif cex_signal == "sodex_premium":
            direction, confidence, fired = "short", round(min(0.75, 0.55 + _cex_mag * 0.20), 3), True
        elif cex_signal == "sodex_discount":
            direction, confidence, fired = "long",  round(min(0.75, 0.55 + _cex_mag * 0.20), 3), True
        else:
            direction, confidence, fired = "neutral", 0.50, False

        return AgentOutput(
            agent_name        = self.name,
            symbol            = symbol,
            timestamp_ms      = int(time.time() * 1000),
            fired             = fired,
            direction         = direction,
            confidence        = confidence,
            invocation_reason = reason,
            raw_data          = {
                "oi_trend":       oi_trend,
                "oi_direction":   oi_direction,
                "cex_divergence": round(cex_divergence, 5),
                "cex_signal":     cex_signal,
                "mag7_momentum":  mag7_momentum,
                "lead_signal":    lead_signal,
            },
        )

    def is_correct(self, output: AgentOutput, outcome: TradeOutcome) -> bool:
        """
        OI bullish calls correct if trade won; bearish calls if trade lost (agent said short).
        CEX divergence calls correct if trade won.
        Neutral: always correct.
        """
        try:
            if not output.fired:
                return True
            oi_bullish = output.raw_data.get("oi_direction") in (
                "bullish_expansion", "short_covering")
            if oi_bullish and outcome.net_pnl_r > 0:
                return True
            if not oi_bullish and outcome.net_pnl_r < 0:
                return True
            # For CEX divergence calls: just check net win
            if output.raw_data.get("cex_signal") in ("sodex_premium", "sodex_discount"):
                return outcome.net_pnl_r > 0
            return False
        except Exception:
            return False
