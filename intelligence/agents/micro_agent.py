"""
intelligence/agents/micro_agent.py — MicroAgent: orderbook/sweep/VPIN perception.

The hot-path agent. Fires at 50ms resolution on orderbook updates.
Natural frequency: 50ms (but self-throttled to avoid I/O storms).

Philosophical role: microstructure is the immune system's first response.
Before macro, regime, or structure can vote, the micro dimension tells us
whether the market is currently being USED — by whom, in what direction, and
with what conviction. A sweep without cluster confirmation is noise.
A sweep with cluster confirmation is institutional intent.

HARD GATE (enforced in CoherenceScorer, not here):
If MicroAgent.fired=False, no directional trade is taken regardless of other agents.
This ensures ARIA never trades into an orderbook vacuum.
"""

from __future__ import annotations

import time
import structlog
from .base import BaseAgent, AgentOutput, TradeOutcome

log = structlog.get_logger(__name__)

_VPIN_HIGH_THRESHOLD = 0.70   # above this: high-conviction sweep
_VPIN_MIN_THRESHOLD  = 0.40   # below this: sweep is suspect


class MicroAgent(BaseAgent):
    """
    Reads orderbook, mark price, trade flow, candles, and stop clusters to
    detect sweeps, VPIN pressure, and divergence patterns.

    Invoked by:
      - ORDERBOOK_UPDATED event
      - MARK_PRICE_UPDATED event
    """

    def __init__(
        self,
        orderbook_stores: dict | None    = None,
        mark_price_stores: dict | None   = None,
        trade_flow_stores: dict | None   = None,
        candle_buffers: dict | None      = None,
        stop_cluster_map=None,
        vpin_calculator=None,
        symbols: list | None             = None,
    ) -> None:
        super().__init__()
        self._ob_stores    = orderbook_stores  or {}
        self._mp_stores    = mark_price_stores or {}
        self._tf_stores    = trade_flow_stores or {}
        self._candles      = candle_buffers    or {}
        self._stop_clusters = stop_cluster_map
        self._vpin          = vpin_calculator
        self._symbols       = symbols or []
        self._last_invoke:  dict = {}   # {symbol: float ts} — throttle

    @property
    def name(self) -> str:
        return "micro"

    @property
    def natural_frequency_seconds(self) -> float:
        return 0.05   # 50ms — driven by events

    @property
    def symbols(self) -> list:
        return self._symbols

    async def on_orderbook_update(self, event) -> None:
        sym = getattr(event, "symbol", None) or (event.get("symbol") if isinstance(event, dict) else None)
        if sym and sym in self._symbols:
            now = time.time()
            if now - self._last_invoke.get(sym, 0) >= 0.05:
                self._last_invoke[sym] = now
                await self.perceive(sym, reason="ob_update")

    async def on_mark_update(self, event) -> None:
        sym = getattr(event, "symbol", None) or (event.get("symbol") if isinstance(event, dict) else None)
        if sym and sym in self._symbols:
            now = time.time()
            if now - self._last_invoke.get(sym, 0) >= 0.05:
                self._last_invoke[sym] = now
                await self.perceive(sym, reason="mark_update")

    async def perceive(self, symbol: str, **context) -> AgentOutput:
        self.record_invocation()
        try:
            return self._store(self._perceive_internal(symbol, **context))
        except Exception as e:
            log.warning("micro_agent_perceive_error", symbol=symbol, error=str(e))
            return self._store(self._make_neutral(symbol, reason="error",
                                                  sweep="none", divergence="none"))

    def _perceive_internal(self, symbol: str, **context) -> AgentOutput:
        reason = context.get("reason", "ob_update")

        # ── Sweep detection ────────────────────────────────────────────────
        ob = self._ob_stores.get(symbol)
        sweep = "none"
        sweep_validated = False
        cluster_level   = None
        sweep_index     = None

        if ob is not None:
            bids = getattr(ob, "bids", []) or []
            asks = getattr(ob, "asks", []) or []
            if bids and asks:
                top_bid = float(bids[0][0]) if bids else 0.0
                top_ask = float(asks[0][0]) if asks else 0.0
                bid_depth = sum(float(b[1]) for b in bids[:5]) if bids else 0.0
                ask_depth = sum(float(a[1]) for a in asks[:5]) if asks else 0.0

                if bid_depth > 0 and ask_depth > 0:
                    imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)
                    if imbalance > 0.35:
                        sweep = "buy_side"
                    elif imbalance < -0.35:
                        sweep = "sell_side"
                else:
                    imbalance = 0.0
            else:
                imbalance = 0.0
        else:
            imbalance = 0.0

        # Validate sweep against stop clusters
        if sweep != "none" and self._stop_clusters is not None:
            try:
                mp_store  = self._mp_stores.get(symbol)
                mark      = float(getattr(mp_store, "mark_price", None) or 0.0) if mp_store else 0.0
                if mark > 0:
                    clusters = self._stop_clusters.get_clusters_near(symbol, mark, pct=0.015)
                    if clusters:
                        cluster_level   = clusters[0].price
                        sweep_validated = True
            except Exception:
                pass

        # ── VPIN ───────────────────────────────────────────────────────────
        vpin_score = 0.5
        if self._vpin is not None:
            try:
                vpin_score = float(self._vpin.get(symbol, 0.5) or 0.5)
            except Exception:
                pass

        # ── Divergence detection ────────────────────────────────────────────
        divergence     = "none"
        divergence_pct = 0.0
        candles_1m     = (self._candles.get(symbol) or {}).get("1m") or []
        if len(candles_1m) >= 20:
            closes = [float(c.get("close", 0) or c.get("c", 0) or 0) for c in candles_1m[-20:]]
            mp_store = self._mp_stores.get(symbol)
            mark     = float(getattr(mp_store, "mark_price", None) or 0.0) if mp_store else 0.0
            if mark > 0 and closes[-1] > 0:
                price_trend  = closes[-1] - closes[-10]
                flow_stores  = self._tf_stores.get(symbol)
                net_flow     = getattr(flow_stores, "net_flow", 0.0) if flow_stores else 0.0
                if price_trend < -0.001 * closes[-1] and net_flow > 0:
                    divergence     = "bullish_reversion"
                    divergence_pct = abs(price_trend / closes[-1])
                elif price_trend > 0.001 * closes[-1] and net_flow < 0:
                    divergence     = "bearish_reversion"
                    divergence_pct = abs(price_trend / closes[-1])

        # ── Absorption ─────────────────────────────────────────────────────
        absorption = "none"
        if vpin_score > _VPIN_HIGH_THRESHOLD:
            absorption = "buying" if imbalance > 0 else "selling"

        # ── Direction logic ────────────────────────────────────────────────
        # Dynamic confidence for sweeps: scales with VPIN score.
        #   confidence = min(0.85, 0.55 + vpin_score × 0.30)
        #   VPIN=0.40 → 0.67 (suspect); VPIN=0.70 → 0.76 (institutional);
        #   VPIN=1.00 → 0.85 (maximum conviction).
        # Divergence: scales with |price/flow divergence_pct|.
        #   confidence = min(0.80, 0.60 + min(divergence_pct / 0.02, 1.0) × 0.20)
        #   0% divergence → 0.60; 1%+ → 0.70; 2%+ → 0.80 (cap).
        if sweep == "buy_side" and sweep_validated:
            direction  = "long"
            confidence = round(min(0.85, 0.55 + vpin_score * 0.30), 3)
            fired      = True
        elif sweep == "sell_side" and sweep_validated:
            direction  = "short"
            confidence = round(min(0.85, 0.55 + vpin_score * 0.30), 3)
            fired      = True
        elif divergence == "bullish_reversion":
            _div_excess = min(1.0, divergence_pct / 0.02)
            direction, confidence, fired = "long",  round(min(0.80, 0.60 + _div_excess * 0.20), 3), True
        elif divergence == "bearish_reversion":
            _div_excess = min(1.0, divergence_pct / 0.02)
            direction, confidence, fired = "short", round(min(0.80, 0.60 + _div_excess * 0.20), 3), True
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
                "sweep":           sweep,
                "sweep_validated": sweep_validated,
                "divergence":      divergence,
                "divergence_pct":  round(divergence_pct, 4),
                "vpin_score":      round(vpin_score, 3),
                "imbalance":       round(imbalance, 3),
                "absorption":      absorption,
                "sweep_index":     sweep_index,
                "cluster_level":   cluster_level,
            },
        )

    def is_correct(self, output: AgentOutput, outcome: TradeOutcome) -> bool:
        """
        Sweep calls: correct if the sweep was real (cluster-validated) AND trade won.
        Divergence calls: correct if trade won.
        Neither: abstention — always correct.
        """
        try:
            sweep       = output.raw_data.get("sweep", "none")
            validated   = output.raw_data.get("sweep_validated", False)
            divergence  = output.raw_data.get("divergence", "none")

            sweep_real = sweep != "none" and validated

            if sweep_real and outcome.net_pnl_r > 0:
                return True
            if not sweep_real and divergence != "none" and outcome.net_pnl_r > 0:
                return True
            if not output.fired:
                return True
            return False
        except Exception:
            return False
