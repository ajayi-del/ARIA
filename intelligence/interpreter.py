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
        trade_flow_stores: Dict[str, Any],
        bybit_ticker_stores: Dict[str, Any] = None,
        market_hours: Any = None
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
        self.bybit_ticker_stores = bybit_ticker_stores  # OI + funding from Bybit tickers
        self.market_hours = market_hours  # Session gating + soft multipliers

        # Caches
        self._tier3_cache: Dict[str, Dict[str, Any]] = {}  # Structure (Slow Path)
        self._tier4_cache: Dict[str, Dict[str, Any]] = {}  # Microstructure (Fast Path)
        self._atr_cache: Dict[str, float] = {}
        self._market_states: Dict[str, MarketState] = {}
        # Rate limiting: minimum seconds between signal publishes per symbol.
        # Prevents 3-5 risk gate evaluations/sec (144 DB reads/sec on 8 symbols).
        self._last_publish_ts: Dict[str, float] = {}
        self._MIN_PUBLISH_INTERVAL_S = 15.0  # max 4 publishes/min per symbol

        # ── HTF (4H) trend bias ─────────────────────────────────────────────────
        # EMA21 of 4H closes. "bullish" = price > EMA21, "bearish" = below, "neutral" = flat.
        # Recomputed in _build_and_publish whenever 4H buffer updates.
        self._htf_bias: Dict[str, str] = {}

        # ── Directional stability lock ──────────────────────────────────────────
        # Prevents the same symbol from flipping long→short (or short→long) within
        # _DIRECTION_LOCK_S seconds. A liquidity sweep that explicitly confirms
        # the reversal direction can break the lock early.
        self._direction_lock: Dict[str, str] = {}       # symbol → "long" | "short"
        self._direction_lock_ts: Dict[str, float] = {}  # symbol → timestamp
        self._DIRECTION_LOCK_S = 1800.0                 # 30 min hold

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
        
        logger.debug("candle_event_received",
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
            
            if buf.count() < 50:
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

            # Volume surge and candle conviction — always computable, no external deps
            vol_surge = 1.0
            conviction = 0.0
            volumes = [c.volume for c in candle_list]
            if len(volumes) >= 21:
                avg_vol = sum(volumes[-21:-1]) / 20
            elif len(volumes) >= 2:
                avg_vol = sum(volumes[:-1]) / (len(volumes) - 1)
            else:
                avg_vol = 0.0
            if avg_vol > 0:
                vol_surge = volumes[-1] / avg_vol
            last_c = candle_list[-1]
            c_range = last_c.high - last_c.low
            c_body = abs(last_c.close - last_c.open)
            if c_range > 0:
                conviction = c_body / c_range

            self._tier3_cache[symbol] = {
                "atr": atr,
                "atr_vs_baseline": ratio,
                "market_type": market_type,
                "volume_surge": vol_surge,
                "candle_conviction": conviction,
                "timestamp_ms": event.timestamp_ms
            }
            self._atr_cache[symbol] = atr

            logger.debug("tier3_structure_updated", symbol=symbol, atr=atr, type=market_type)

            # PUBLISH TRIGGER — with rate limiter to prevent DB thrash
            # Regime change and confirmed candle close are always immediate.
            # Trending markets throttled to max 1 publish per MIN_PUBLISH_INTERVAL_S.
            now_ts = time.time()
            last_ts = self._last_publish_ts.get(symbol, 0.0)
            elapsed = now_ts - last_ts
            regime_changed = market_type != prev_type
            # Immediate triggers: regime flip, confirmed close, or sweep (fast path)
            immediate = regime_changed or confirmed
            # Rate-limited: trending/expanding structure, heartbeat
            throttled = (
                (market_type in ("trend", "expansion") or (count % 10 == 0))
                and elapsed >= self._MIN_PUBLISH_INTERVAL_S
            )
            if immediate or throttled:
                self._last_publish_ts[symbol] = now_ts
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
            
        # If strong divergence, build and publish — rate-limited to _MIN_PUBLISH_INTERVAL_S
        if divergence != "none":
            now = time.time()
            if now - self._last_publish_ts.get(symbol, 0.0) >= self._MIN_PUBLISH_INTERVAL_S:
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
                processed["_t3_volume_surge"] = t3.get("volume_surge", 1.0)
                processed["_t3_candle_conviction"] = t3.get("candle_conviction", 0.0)

            # ── Inject Bybit OI + funding intelligence (always use Bybit rates, not SoDEX) ──
            # Bybit fundingRate: 8h rate, e.g. 0.0001 = 0.01% per 8h. Far more liquid
            # and crowd-sentiment-representative than SoDEX's near-zero ~1.25e-05/hr rates.
            if self.bybit_ticker_stores:
                ticker = self.bybit_ticker_stores.get(symbol, {})
                if ticker:
                    processed["funding_rate"] = ticker.get("funding_rate", 0.0)
                    processed["open_interest"] = ticker.get("open_interest", 0.0)
                    processed["prev_open_interest"] = ticker.get("prev_open_interest", 0.0)
                    prev_mp = ticker.get("prev_mark_price", 0.0)
                    cur_mp = processed.get("mark_price", 0.0)
                    if prev_mp > 0:
                        processed["prev_mark_price"] = prev_mp
                    elif cur_mp > 0:
                        processed["prev_mark_price"] = cur_mp

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

            # ── Market hours gate (XAUT, USTECH100) ─────────────────────────────
            market_hours_ok = True
            session_size_mult = 1.0
            if self.market_hours:
                ctx = self.market_hours.get_session_context(symbol)
                market_hours_ok = ctx["active"]
                session_size_mult = ctx.get("size_mult", 1.0)
                if not market_hours_ok:
                    logger.debug("signal_suppressed_market_closed",
                                 symbol=symbol, reason=ctx["reason"])
                    # Still update cached state so display shows correct hours gate
                    # but skip publish so no trade is attempted
                    return

            state = self.signal_generator.generate_market_state(
                symbol, processed, market_hours_ok=market_hours_ok
            )

            # Inject live mark_price via model_copy — frozen-safe, no mutation
            mark_store = self.mark_price_stores.get(symbol)
            mark_price = mark_store.mark_price if mark_store else 0.0
            state = state.model_copy(update={"mark_price": mark_price})

            # ── HTF bias filter (4H EMA21) ──────────────────────────────────────
            # Recompute on every publish — O(21) EMA, negligible cost.
            htf_bias = self._compute_htf_bias(symbol)
            self._htf_bias[symbol] = htf_bias

            new_dir = state.trade_direction
            if htf_bias != "neutral" and new_dir != "none":
                if (htf_bias == "bullish" and new_dir == "short") or \
                   (htf_bias == "bearish" and new_dir == "long"):
                    state = state.model_copy(update={
                        "trade_direction": "none",
                        "invalidation_reason": f"4H trend {htf_bias} disagrees with {new_dir}"
                    })
                    new_dir = "none"
                    logger.debug("htf_direction_suppressed",
                                 symbol=symbol, htf=htf_bias, attempted=new_dir)

            # ── Directional stability lock (30-min anti-flip) ───────────────────
            # Prevents whipsawing: once a direction is committed, hold it unless
            # (a) 30 min have elapsed, OR (b) a liquidity sweep confirms the reversal.
            locked_dir = self._direction_lock.get(symbol)
            locked_ts  = self._direction_lock_ts.get(symbol, 0.0)

            if new_dir != "none":
                if locked_dir and locked_dir != new_dir:
                    elapsed = time.time() - locked_ts
                    # Allow sweep-confirmed reversals to break the lock early
                    sweep = self._tier4_cache.get(symbol, {}).get("sweep", "none")
                    sweep_confirms = (
                        (locked_dir == "long"  and sweep == "sell_side") or
                        (locked_dir == "short" and sweep == "buy_side")
                    )
                    if elapsed < self._DIRECTION_LOCK_S and not sweep_confirms:
                        state = state.model_copy(update={
                            "trade_direction": "none",
                            "invalidation_reason": f"direction_locked:{locked_dir} ({int(elapsed)}s/{int(self._DIRECTION_LOCK_S)}s)"
                        })
                        new_dir = "none"
                        logger.debug("direction_lock_active",
                                     symbol=symbol, locked=locked_dir,
                                     attempted=locked_dir, elapsed_s=int(elapsed))
                    else:
                        self._direction_lock[symbol] = new_dir
                        self._direction_lock_ts[symbol] = time.time()
                        logger.info("direction_lock_flipped",
                                    symbol=symbol, prev=locked_dir, new=new_dir,
                                    reason="sweep" if sweep_confirms else "lock_expired")
                else:
                    # Same direction or no lock — set/refresh lock
                    self._direction_lock[symbol] = new_dir
                    self._direction_lock_ts[symbol] = time.time()

            self._market_states[symbol] = state

            # Broadcast to Execution — stamp rate-limit timestamp
            self._last_publish_ts[symbol] = time.time()
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
                            score=state.coherence_score,
                            htf=htf_bias)
        except Exception as e:
            import traceback as _tb
            logger.error("build_publish_failed", symbol=symbol, error=str(e),
                         traceback=_tb.format_exc())

    def _compute_htf_bias(self, symbol: str) -> str:
        """
        Compute 4H trend bias via EMA21 of 4H closes.

        Returns "bullish" (price > EMA21 by >0.5%), "bearish" (price < EMA21 by >0.5%),
        or "neutral" (price within ±0.5% of EMA21 or insufficient data).

        Uses the 4H CandleBuffer populated by Bybit kline.240 WS stream.
        Requires ≥5 candles to produce a result; ≥21 for a full EMA21.
        """
        buf = self.candle_buffers.get(symbol, {}).get("4h")
        if not buf or buf.count() < 5:
            return "neutral"

        n = buf.count()
        candles = buf.latest(min(n, 21))
        closes = [c.close for c in candles]

        if len(closes) < 3:
            return "neutral"

        # Wilder-style EMA: k = 2/(period+1)
        period = min(21, len(closes))
        k = 2.0 / (period + 1)
        ema = closes[0]
        for price in closes[1:]:
            ema = price * k + ema * (1 - k)

        current = closes[-1]
        deviation = (current - ema) / ema

        if deviation > 0.005:     # 0.5% above EMA21 → bullish
            return "bullish"
        elif deviation < -0.005:  # 0.5% below EMA21 → bearish
            return "bearish"
        return "neutral"

    def get_market_state(self, symbol: str) -> Optional[MarketState]:
        return self._market_states.get(symbol)
