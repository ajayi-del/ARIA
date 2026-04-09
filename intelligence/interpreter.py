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
        
        # Check health
        ob_ok = self.orderbook_stores[symbol].is_healthy(500)
        mark_ok = self.mark_price_stores[symbol].is_healthy(500)
        
        phase = self.system_state.update(symbol, count, ob_ok, mark_ok)
        
        if not self.system_state.can_signal(symbol):
            return  # Still warming up

        # Retrieve candles (1m interval assumed)
        try:
            candles = self.candle_buffers[symbol]["1m"].latest(50)
        except Exception:
            return

        # Recompute Tier 3 (Structure)
        sa = self.signal_generator.structure_analyzer
        atr = sa.calculate_atr(candles)
        if atr == 0:
            return

        baseline = sa.calculate_baseline_atr(candles)
        ratio = sa.atr_ratio(atr, baseline)
        market_type = sa.classify_regime(candles, atr, ratio)
        
        self._tier3_cache[symbol] = {
            "atr": atr,
            "atr_vs_baseline": ratio,
            "market_type": market_type,
            "timestamp_ms": event.timestamp_ms
        }
        self._atr_cache[symbol] = atr
        
        logger.debug("tier3_structure_updated", symbol=symbol, atr=atr, type=market_type)

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
        imbalance = ma.score_imbalance(self.orderbook_stores[symbol])
        absorption = ma.detect_absorption(
            self.orderbook_stores[symbol], 
            [c.close for c in candles]
        )
        divergence = ma.score_divergence(self.mark_price_stores[symbol])
        
        # VPIN can be heavy, but at 50ms it's fine for 8 assets
        # v1.3 Refactored to use trade history
        trades = self.trade_flow_stores[symbol].get_recent(50)
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
        divergence = ma.score_divergence(self.mark_price_stores[symbol])
        
        if symbol in self._tier4_cache:
            self._tier4_cache[symbol]["divergence"] = divergence
            
        # If strong divergence flip, build and publish
        if divergence != "none":
            await self._build_and_publish(symbol)

    async def _build_and_publish(self, symbol: str) -> None:
        """
        Assembles full MarketState and broadcasts SIGNAL_READY.
        """
        # We call the generator to assemble everything into one state object
        # but we overlay the fresh timestamp metadata.
        try:
            processed = self.data_processor.process_market_data(
                symbol,
                self.orderbook_stores[symbol],
                self.mark_price_stores[symbol],
                self.candle_buffers[symbol],
                self.trade_flow_stores[symbol]
            )
            
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
