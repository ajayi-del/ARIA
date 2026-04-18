"""
intelligence/agents/structure_agent.py — StructureAgent: ATR/market-type perception.

Perceives market structure via ATR expansion/compression and trend classification.
Natural frequency: 1 minute (candle close cadence).

Philosophical role: structure is the terrain, not the weather.
Regime changes are weather. Structure is the mountain range — it determines where
momentum can run and where it will stall. Expansion is a runway; compression is a
coiling spring. The structure agent reads the physical reality of price motion.
"""

from __future__ import annotations

import time
import structlog
from typing import List
from .base import BaseAgent, AgentOutput, TradeOutcome

log = structlog.get_logger(__name__)

# ATR ratio thresholds for market classification
_EXPANSION_RATIO  = 1.30   # ATR > 130% of baseline → expansion
_COMPRESSION_RATIO = 0.70  # ATR < 70% of baseline → compression
_TREND_MIN_CANDLES = 10    # minimum candles to assess trend


class StructureAgent(BaseAgent):
    """
    Reads candle buffers to compute ATR ratio and classify market structure.

    Invoked by:
      - CANDLE_CLOSED event for this symbol
      - Every 1-minute candle close
    """

    def __init__(
        self,
        candle_buffers: dict | None = None,
        symbols: list | None = None,
    ) -> None:
        super().__init__()
        self._candle_buffers = candle_buffers or {}
        self._symbols        = symbols or []

    @property
    def name(self) -> str:
        return "structure"

    @property
    def natural_frequency_seconds(self) -> float:
        return 60.0   # 1 minute

    @property
    def symbols(self) -> list:
        return self._symbols

    async def on_candle_close(self, event) -> None:
        """EventBus handler for CANDLE_CLOSED — invokes perceive for the symbol."""
        sym = getattr(event, "symbol", None) or (event.get("symbol") if isinstance(event, dict) else None)
        if sym and sym in self._symbols:
            await self.perceive(sym, reason="candle_close")

    async def perceive(self, symbol: str, **context) -> AgentOutput:
        self.record_invocation()
        try:
            return self._store(self._perceive_internal(symbol, **context))
        except Exception as e:
            log.warning("structure_agent_perceive_error", symbol=symbol, error=str(e))
            return self._store(self._make_neutral(symbol, reason="error"))

    def _perceive_internal(self, symbol: str, **context) -> AgentOutput:
        reason = context.get("reason", "candle_close")

        # Read candle buffer for 1m timeframe
        sym_buffers  = self._candle_buffers.get(symbol) or {}
        candles_1m   = sym_buffers.get("1m") or []
        candle_count = len(candles_1m)

        if candle_count < 20:
            return self._make_neutral(symbol, reason="warmup",
                                      candle_count=candle_count, market_type="warmup")

        # Compute ATR (True Range average over last 14 bars)
        atr = self._compute_atr(candles_1m, period=14)
        baseline_atr = self._compute_atr(candles_1m[-60:], period=40) if candle_count >= 60 else atr
        atr_ratio = atr / baseline_atr if baseline_atr > 0 else 1.0

        # Classify market type
        trend_consistency = 0.0
        if atr_ratio >= _EXPANSION_RATIO:
            market_type = "expansion"
        elif atr_ratio <= _COMPRESSION_RATIO:
            market_type = "compression"
        else:
            # Differentiate trend vs chop using directional consistency
            trend_consistency = self._trend_consistency(candles_1m[-_TREND_MIN_CANDLES:])
            market_type = "trend" if trend_consistency >= 0.65 else "chop"

        # Determine trend direction from recent closes
        trend_direction = self._classify_trend(candles_1m[-20:])

        # Dynamic confidence — proportional to structural evidence strength.
        # Expansion: how far above the 1.30× threshold?  maps [1.30, 2.00] → [0.55, 0.85].
        # Trend: how directionally consistent? maps consistency [0.65, 1.00] → [0.60, 0.85].
        # Compression/chop: agent demurs — 0.40 (acknowledges noise).
        if market_type == "expansion":
            _excess = min(1.0, (atr_ratio - _EXPANSION_RATIO) / 0.70)
            _base_conf = round(min(0.85, 0.55 + _excess * 0.30), 3)
        elif market_type == "trend":
            _excess = min(1.0, (trend_consistency - 0.65) / 0.35)
            _base_conf = round(min(0.85, 0.60 + _excess * 0.25), 3)
        else:
            _base_conf = 0.40

        # Direction logic
        if market_type == "expansion":
            if trend_direction == "up":
                direction, confidence, fired = "long",  _base_conf, True
            elif trend_direction == "down":
                direction, confidence, fired = "short", _base_conf, True
            else:
                direction, confidence, fired = "neutral", 0.50, False
        elif market_type == "trend":
            if trend_direction == "up":
                direction, confidence, fired = "long",  _base_conf, True
            elif trend_direction == "down":
                direction, confidence, fired = "short", _base_conf, True
            else:
                direction, confidence, fired = "neutral", 0.45, False
        else:
            # compression or chop
            direction, confidence, fired = "neutral", _base_conf, False

        return AgentOutput(
            agent_name        = self.name,
            symbol            = symbol,
            timestamp_ms      = int(time.time() * 1000),
            fired             = fired,
            direction         = direction,
            confidence        = confidence,
            invocation_reason = reason,
            raw_data          = {
                "atr":               round(atr, 6),
                "baseline_atr":      round(baseline_atr, 6),
                "atr_ratio":         round(atr_ratio, 3),
                "market_type":       market_type,
                "trend_direction":   trend_direction,
                "trend_consistency": round(trend_consistency, 3),
                "candle_count":      candle_count,
            },
        )

    def is_correct(self, output: AgentOutput, outcome: TradeOutcome) -> bool:
        """
        Neutral calls correct (agent demurred appropriately).
        Expansion calls correct if trade captured > 0.5R (expansion delivers).
        Trend calls correct if net_pnl_r > 0.
        """
        try:
            market_type = output.raw_data.get("market_type", "chop")
            if output.direction == "neutral":
                return True
            if market_type == "expansion":
                return outcome.net_pnl_r > 0.5
            return outcome.net_pnl_r > 0
        except Exception:
            return False

    # ── Internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _compute_atr(candles: list, period: int = 14) -> float:
        """Average True Range over `period` bars. Candle dicts: {high, low, close}."""
        if len(candles) < 2:
            return 0.0
        trs = []
        for i in range(1, min(len(candles), period + 1)):
            c  = candles[-i]
            c2 = candles[-(i + 1)] if i + 1 <= len(candles) else c
            h  = float(c.get("high", 0) or c.get("h", 0) or 0)
            lo = float(c.get("low",  0) or c.get("l", 0) or 0)
            pc = float(c2.get("close", 0) or c2.get("c", 0) or 0)
            tr = max(h - lo, abs(h - pc), abs(lo - pc)) if h > 0 and lo > 0 else 0.0
            trs.append(tr)
        return sum(trs) / len(trs) if trs else 0.0

    @staticmethod
    def _trend_consistency(candles: list) -> float:
        """Fraction of candles that close in the dominant direction (0–1)."""
        if len(candles) < 3:
            return 0.5
        ups = sum(
            1 for i in range(1, len(candles))
            if float(candles[i].get("close", 0) or candles[i].get("c", 0) or 0)
            > float(candles[i-1].get("close", 0) or candles[i-1].get("c", 0) or 0)
        )
        downs = len(candles) - 1 - ups
        return max(ups, downs) / (len(candles) - 1)

    @staticmethod
    def _classify_trend(candles: list) -> str:
        """Simple linear trend: compare first-half avg to second-half avg close."""
        if len(candles) < 4:
            return "sideways"
        closes = [float(c.get("close", 0) or c.get("c", 0) or 0) for c in candles]
        half   = len(closes) // 2
        first  = sum(closes[:half]) / half
        second = sum(closes[half:]) / (len(closes) - half)
        if first <= 0:
            return "sideways"
        pct = (second - first) / first
        if pct > 0.002:
            return "up"
        if pct < -0.002:
            return "down"
        return "sideways"
