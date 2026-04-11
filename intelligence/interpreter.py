import asyncio
import time
import structlog
from typing import Dict, Any, List, Optional
from core.event_bus import event_bus, Event, EventType
from intelligence.market_state import MarketState
from core.system_state import SystemStateManager
from intelligence.freshness import compute_freshness

logger = structlog.get_logger(__name__)

class IntelligenceInterpreter:
    """
    v1.3 Brain of ARIA.
    Coalesced event-driven architecture with per-symbol warm-up state machine.
    Target: avg signal-to-order latency < 200ms.
    """
    def __init__(
        self,
        config: Any,
        system_state: SystemStateManager,
        signal_generator: Any,
        data_processor: Any,
        orderbook_stores: Dict[str, Any],
        mark_price_stores: Dict[str, Any],
        candle_buffers: Dict[str, Dict[str, Any]],
        trade_flow_stores: Dict[str, Any]
    ):
        self.config = config
        self.system_state = system_state
        self.signal_generator = signal_generator
        self.data_processor = data_processor
        
        # Stores for raw data access
        self.orderbook_stores = orderbook_stores
        self.mark_price_stores = mark_price_stores
        self.candle_buffers = candle_buffers
        self.trade_flow_stores = trade_flow_stores

        # Caches
        self._tier3_cache: Dict[str, Dict[str, Any]] = {}  # Structure (Slow Path)
        self._tier4_cache: Dict[str, Dict[str, Any]] = {}  # Microstructure (Fast Path)
        self._atr_cache: Dict[str, float] = {}
        self._market_states: Dict[str, MarketState] = {}
        
        self._is_active = False

    async def start(self):
        """Subscribe to the coalesced event bus."""
        if self._is_active:
            return
            
        self._is_active = True
        
        event_bus.subscribe(EventType.CANDLE_CLOSED, self._on_candle_close)
        event_bus.subscribe(EventType.ORDERBOOK_UPDATED, self._on_orderbook_update)
        event_bus.subscribe(EventType.MARK_PRICE_UPDATED, self._on_mark_update)
        # Trade flow is handled via VPIN which is called in OB update
        
        logger.info("interpreter_v1.3_started")

    async def _on_candle_close(self, event: Event) -> None:
        """
        SLOW PATH: Recompute Structure (Tier 3).
        Also updates the SystemStateManager.
        """
        symbol = event.symbol
        count = event.data.get("count", 0)
        confirmed = event.data.get("confirmed", False)
        
        # Diagnostic instrumentation for live flow
        logger.info("candle_event_received", 
                    symbol=symbol, 
                    count=count, 
                    confirmed=confirmed,
                    can_signal=self.system_state.can_signal(symbol))
        
        # Check health
        try:
            ob_ok = self.orderbook_stores[symbol].is_healthy(500)
            mark_ok = self.mark_price_stores[symbol].is_healthy(500)
        except (KeyError, AttributeError):
            ob_ok = False
            mark_ok = False
        
        # Always update warmup state regardless of confirmed status
        # TODO: re-enable ob_healthy gate when SoDEX native OB data flows
        phase = self.system_state.update(
            symbol, 
            count, 
            ob_ok, 
            mark_ok, 
            require_ob=False
        )
        
        # SoDEX sends x=true only on confirmed candle close. We process all ticks
        # during warmup (count <= 60) to fill the ATR buffer faster. After warmup,
        # unconfirmed ticks still flow through — Tier 3 re-computes on every tick
        # but the interpreter's publish trigger (`should_publish`) limits actual signal
        # generation to structure changes and heartbeats only.

        if not self.system_state.can_signal(symbol):
            return  # Still warming up

        try:
            # Retrieve candles (1m interval assumed)
            buf = self.candle_buffers.get(symbol, {}).get("1m")
            if buf is None:
                logger.warning("no_candle_buffer", symbol=symbol)
                return
            
            if buf.count() < 20:
                logger.warning("insufficient_candles", symbol=symbol, count=buf.count())
                return
                
            candle_list = buf.latest(50)
            
            logger.info("running_signal_analysis",
                        symbol=symbol,
                        count=len(candle_list),
                        confirmed=confirmed)
            
            # Tier 3 - Structure Analysis
            sa = self.signal_generator.structure_analyzer
            atr = sa.calculate_atr(candle_list)
            
            logger.info("atr_result",
                        symbol=symbol,
                        atr=atr,
                        candle_count=len(candle_list))
            
            if atr == 0 or atr is None:
                logger.warning("atr_zero", symbol=symbol)
                return

            baseline = sa.calculate_baseline_atr(candle_list)
            ratio = sa.atr_ratio(atr, baseline)
            market_type = sa.classify_regime(candle_list, atr, ratio)

            # Detect structure change vs. prior state
            prev_type = self._tier3_cache.get(symbol, {}).get("market_type", "chop")

            self._tier3_cache[symbol] = {
                "atr": atr,
                "atr_vs_baseline": ratio,
                "market_type": market_type,
                "timestamp_ms": event.timestamp_ms
            }
            self._atr_cache[symbol] = atr

            logger.debug("tier3_structure_updated", symbol=symbol, atr=atr, type=market_type)

            # PUBLISH TRIGGER: fire signal on active structure OR regime change OR heartbeat.
            # Previously only sweep/divergence triggered publish — this was the primary blocker.
            should_publish = (
                market_type in ("trend", "expansion")  # Actionable structure
                or market_type != prev_type            # Regime transition
                or (count % 10 == 0)                  # Heartbeat: every 10 candles
            )
            if should_publish:
                await self._build_and_publish(symbol)

        except Exception as e:
            import traceback
            logger.error("signal_analysis_error",
                         symbol=symbol,
                         error=str(e),
                         traceback=traceback.format_exc())

    async def _on_orderbook_update(self, event: Event) -> None:
        """
        FAST PATH: Recompute Microstructure (Tier 4).
        Triggered by 50ms coalesced OB updates.
        """
        symbol = event.symbol
        if not self.system_state.can_signal(symbol):
            return
            
        if symbol not in self._tier3_cache:
            return # Structure not ready
            
        try:
            # We need small window for absorbing/sweep logic
            candles = self.candle_buffers[symbol]["1m"].latest(20)
        except Exception:
            return

        atr = self._atr_cache.get(symbol, 0)
        if atr == 0:
            return

        # Compute Tier 4
        ma = self.signal_generator.microstructure_analyzer
        orderbook_store = self.orderbook_stores.get(symbol)
        trade_flow_store = self.trade_flow_stores.get(symbol)
        mark_price_store = self.mark_price_stores.get(symbol)

        imbalance = ma.score_imbalance(orderbook_store)
        absorption = ma.detect_absorption(
            orderbook_store=orderbook_store,
            trade_flow_store=trade_flow_store
        )
        
        last_candle = candles[-1] if candles else None
        last_price = last_candle.close if last_candle else mark_price_store.mark_price
        
        divergence = ma.score_divergence(
            mark_price=mark_price_store.mark_price,
            last_price=last_price,
            orderbook_store=orderbook_store
        )
        
        # VPIN can be heavy, but at 50ms it's fine for 8 assets
        # v1.3 Refactored to use trade history
        trades = trade_flow_store.get_recent(50) if trade_flow_store else []
        vpin_res = self.signal_generator.vpin_calculator.compute(symbol, trades)
        vpin = vpin_res.vpin
        
        sweep, sweep_idx = ma.detect_sweep(candles, atr, self.config)
        
        self._tier4_cache[symbol] = {
            "imbalance": imbalance,
            "absorption": absorption,
            "divergence": divergence,
            "vpin_score": vpin,
            "sweep": sweep,
            "sweep_index": sweep_idx,
            "timestamp_ms": event.timestamp_ms
        }
        
        # CRITICAL: If sweep detected, build and publish instantly
        if sweep != "none":
            await self._build_and_publish(symbol)

    async def _on_mark_update(self, event: Event) -> None:
        """Fast path price divergence update."""
        symbol = event.symbol
        if not self.system_state.can_signal(symbol):
            return
            
        ma = self.signal_generator.microstructure_analyzer
        mark = event.data.get("mark_price", 0.0)
        last = event.data.get("last_price", mark)
        
        divergence = ma.score_divergence(
            mark_price=mark,
            last_price=last,
            orderbook_store=self.orderbook_stores.get(symbol)
        )
        
        if symbol in self._tier4_cache:
            self._tier4_cache[symbol]["divergence"] = divergence
            
        # If strong divergence flip, build and publish
        if divergence != "none":
            await self._build_and_publish(symbol)

    async def _build_and_publish(self, symbol: str) -> None:
        """
        Assembles full MarketState and broadcasts SIGNAL_READY.
        """
        try:
            processed = self.data_processor.process_market_data(
                symbol,
                self.orderbook_stores[symbol],
                self.mark_price_stores[symbol],
                self.candle_buffers[symbol],
                self.trade_flow_stores[symbol]
            )

            # ── Inject Tier 3 cache (50-candle Wilder ATR, warmed-up baseline) ──
            # The data_processor only uses 20 candles and a fresh StructureAnalyzer
            # instance with no atr_history. This override ensures generate_market_state
            # uses the authoritative computation.
            if symbol in self._tier3_cache:
                t3 = self._tier3_cache[symbol]
                processed["_t3_market_type"] = t3["market_type"]
                processed["_t3_atr"] = t3["atr"]
                processed["_t3_atr_vs_baseline"] = t3["atr_vs_baseline"]

            # ── Inject Tier 4 cache (swing-based sweep, VPIN, imbalance) ──
            # generate_market_state() calls analyze_microstructure() which uses the
            # old _detect_sweep() (trade-data based). The interpreter uses the correct
            # fixed detect_sweep() (candle/ATR based). Inject the interpreter's results.
            if symbol in self._tier4_cache:
                t4 = self._tier4_cache[symbol]
                processed["_t4_sweep"] = t4.get("sweep", "none")
                processed["_t4_sweep_index"] = t4.get("sweep_index", 0)
                processed["_t4_imbalance"] = t4.get("imbalance", 0.0)
                processed["_t4_absorption"] = t4.get("absorption", False)
                processed["_t4_divergence"] = t4.get("divergence", "none")
                processed["_t4_vpin"] = t4.get("vpin_score", 0.0)

            # ── Compute candle momentum for macro/regime fallback ──
            buf = self.candle_buffers.get(symbol, {}).get("1m")
            if buf:
                candles = buf.latest(20)
                if len(candles) >= 5:
                    closes = [c.close for c in candles]
                    c0 = closes[0]
                    if c0 > 0:
                        processed["_momentum_pct"] = (closes[-1] - c0) / c0
                        # Real returns from candle closes (replace mock)
                        real_returns = [
                            (closes[i] - closes[i - 1]) / closes[i - 1]
                            for i in range(1, len(closes))
                            if closes[i - 1] > 0
                        ]
                        if real_returns:
                            processed["asset_returns"] = {symbol: real_returns}

            state = self.signal_generator.generate_market_state(symbol, processed)
            
            # Record metadata
            state.signal_age_ms = 0 # Freshly computed
            state.mark_price = self.mark_price_stores[symbol].mark_price
            
            self._market_states[symbol] = state
            
            # Broadcast to Execution
            event_bus.publish(Event(
                EventType.SIGNAL_READY,
                symbol,
                int(time.time() * 1000),
                {"state": state}
            ))
            
            if state.is_valid_signal():
                logger.info("signal_ready", 
                            symbol=symbol, 
                            dir=state.trade_direction, 
                            score=state.coherence_score)
        except Exception as e:
            logger.error("build_publish_failed", symbol=symbol, error=str(e))

    def get_market_state(self, symbol: str) -> Optional[MarketState]:
        return self._market_states.get(symbol)
