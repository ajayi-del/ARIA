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
        market_hours: Any = None,
        liq_engine: Any = None,
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
        self.liq_engine = liq_engine      # Tier 6: on-chain liquidation signals

        # Caches
        self._tier3_cache: Dict[str, Dict[str, Any]] = {}  # Structure (Slow Path)
        self._tier4_cache: Dict[str, Dict[str, Any]] = {}  # Microstructure (Fast Path)
        self._atr_cache: Dict[str, float] = {}
        self._market_states: Dict[str, MarketState] = {}
        # Rate limiting: minimum seconds between signal publishes per symbol.
        # Prevents 3-5 risk gate evaluations/sec (144 DB reads/sec on 8 symbols).
        self._last_publish_ts: Dict[str, float] = {}
        self._MIN_PUBLISH_INTERVAL_S = 15.0  # max 4 publishes/min per symbol
        # Separate sweep rate limiter — sweeps re-detect on every 50ms OB update
        # from the same candle data. 10s window: fast enough to capture a real sweep,
        # slow enough to prevent the 15-20x/sec signal runaway observed in production.
        self._last_sweep_ts: Dict[str, float] = {}
        self._MIN_SWEEP_INTERVAL_S = 10.0    # 1 sweep-triggered publish per 10s

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

        # Enhancement layer — signal quality amplifiers (v1.7)
        from intelligence.signal_momentum import SignalMomentumTracker
        from intelligence.session_timing import SessionTimingMultiplier
        from intelligence.mag7_signal import MAG7SSISignal
        self._momentum = SignalMomentumTracker()
        self._session  = SessionTimingMultiplier()
        self._mag7     = MAG7SSISignal()  # Tier 1: USTECH100 macro regime
        self._current_directions: dict = {}  # symbol → "long"|"short"|"none"
        self.vc_monitor = None   # Wired from main.py after construction
        self.oi_monitor = None   # OI arb monitor — wired from main.py after construction
        # Portfolio-level cross-asset macro signals (7 signals, v1.8)
        from intelligence.macro_signals import MacroSignalEngine
        self._macro = MacroSignalEngine(config)

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
        # ONLY run the heavy Tier-3 analysis on confirmed candle close.
        # Unconfirmed ticks are skipped — the OB update path handles real-time
        # microstructure (Tier 4) without the ATR/structure compute overhead.
        # This reduces CPU and log noise from ~70 events/sec to ~7/min.

        if not self.system_state.can_signal(symbol):
            return  # Still warming up

        # After warmup: skip unconfirmed ticks — OB path handles intra-candle signals.
        # During warmup: process all ticks to fill ATR buffer fast.
        if not confirmed and count > 60:
            return

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

            logger.debug("running_signal_analysis",
                        symbol=symbol,
                        count=len(candle_list),
                        confirmed=confirmed)

            # ── MAG7 Tier 1 update (USTECH100 → macro regime) ────────────────
            # USTECH100-USD is the native SoDEX Nasdaq 100 proxy for Mag7 sentiment.
            # Update on every confirmed candle — applies to ALL assets via coherence tier.
            if symbol == "USTECH100-USD" and candle_list:
                latest_candle = candle_list[-1]
                close_price = float(getattr(latest_candle, "close", 0.0))
                ts_ms = int(getattr(latest_candle, "open_time", 0) or 0)
                if close_price > 0:
                    self._mag7.update(close_price, ts_ms)
                    logger.info(
                        "mag7_updated",
                        direction=self._mag7.direction,
                        strength=round(self._mag7.strength, 3),
                        candles=self._mag7._candle_count,
                    )

            # Tier 3 - Structure Analysis
            sa = self.signal_generator.structure_analyzer
            atr = sa.calculate_atr(candle_list)
            
            logger.debug("atr_result",
                        symbol=symbol,
                        atr=atr,
                        candle_count=len(candle_list))
            
            if atr == 0 or atr is None:
                # ATR=0 is expected for equity-hours symbols (USTECH100) during closed market.
                # Debug-only to avoid spam; the symbol is simply skipped this cycle.
                logger.debug("atr_zero", symbol=symbol)
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
        
        # If sweep detected, build and publish — rate-limited to prevent re-fires
        # on the same candle data across consecutive 50ms OB updates (signal runaway).
        if sweep != "none":
            _now_sweep = time.time()
            _last_sweep = self._last_sweep_ts.get(symbol, 0.0)
            if _now_sweep - _last_sweep >= self._MIN_SWEEP_INTERVAL_S:
                self._last_sweep_ts[symbol] = _now_sweep   # stamp before await
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
        # Stamp BEFORE await to prevent async race: multiple coroutines queuing up and
        # all passing the interval check before any of them stamps (causes 20x/sec spam).
        if divergence != "none":
            now = time.time()
            if now - self._last_publish_ts.get(symbol, 0.0) >= self._MIN_PUBLISH_INTERVAL_S:
                self._last_publish_ts[symbol] = now   # reserve slot before yielding
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

            # ── Inject Tier 1: MAG7 macro regime (USTECH100 direction-neutral strength) ──
            # Direction-neutral: coherence engine scores the magnitude, Enhancement Layer
            # applies +bonus (aligned) or -penalty (opposing) after generate_market_state().
            if not self._mag7.is_stale() and self._mag7.strength > 0:
                processed["mag7_direction"] = self._mag7.direction
                processed["mag7_strength"]  = round(self._mag7.strength, 4)
            else:
                processed["mag7_direction"] = "neutral"
                processed["mag7_strength"]  = 0.0

            # ── Inject Tier 6: LiquidationSignalEngine score ─────────────────────
            # On-chain liq events → coherence boost or directional hint.
            # Conflict with direction_lock gets a 70% penalty here before injection.
            if self.liq_engine:
                t6_score = self.liq_engine.get_tier6_score(symbol)
                if t6_score > 0:
                    # Conflict check: if best signal disagrees with direction lock, penalise
                    best_sig = self.liq_engine.get_best_signal(symbol)
                    locked_dir = self._direction_lock.get(symbol)
                    if best_sig and locked_dir and best_sig.direction != locked_dir:
                        t6_score *= 0.70  # 70% penalty for conflict (not suppression)
                processed["tier6_liq_score"] = t6_score

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

            # ── XAUT / inverse-asset direction (gold uses its OWN structure) ───────
            # Gold is NOT anti-correlated to crypto on a trade-by-trade basis.
            # Gold is a structural bull — near $4,667 ATH it should trade long
            # regardless of what crypto is doing. Crypto's HTF bias is IRRELEVANT
            # for gold. Only suppress XAUT longs if gold's own structure is bearish
            # (i.e. if it scored high but its signal generator shows "short").
            #
            # Rules:
            # 1. If gold's own signals already produced a direction ("long"/"short"), trust it.
            # 2. If gold produced "none" AND crypto is risk_off → assign "long" (hedge demand).
            # 3. If gold produced "none" AND crypto is risk_on → keep "none" (no forced trade).
            # 4. Gold's HTF bias is SKIPPED — crypto's HTF does not govern gold.
            _INVERSE_SYMBOLS = {"XAUT-USD"}
            _xaut_direction_set = False
            if symbol in _INVERSE_SYMBOLS and state.weighted_score >= 2.0:
                _regime = getattr(state, "regime", "neutral")
                if new_dir in ("long", "short"):
                    # Gold's own signals are directional — honour them; skip HTF filter below.
                    _xaut_direction_set = True
                    logger.info("xaut_own_signal",
                                symbol=symbol, direction=new_dir, score=round(state.weighted_score, 2),
                                regime=_regime, note="gold own structure — HTF filter bypassed")
                elif new_dir == "none" and _regime in ("risk_off", "confused"):
                    # No own signal but crypto is falling → risk-off demand → LONG gold
                    state = state.model_copy(update={"trade_direction": "long"})
                    new_dir = "long"
                    _xaut_direction_set = True
                    logger.info("xaut_riskoff_long",
                                symbol=symbol, regime=_regime,
                                note="gold long: no own signal but risk-off crypto regime")
                # risk_on + direction none → no trade; don't force a long if own signals neutral

            # ── HTF bias filter (skipped for inverse assets that set their own direction) ──
            if not _xaut_direction_set and htf_bias != "neutral" and new_dir != "none":
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

            # ── Enhancement Layer v1.7 ────────────────────────────────────────────
            # Applied AFTER HTF suppression and direction lock.
            # Adjustments are additive bonuses to the CoherenceEngine base score.
            # All applied via model_copy — frozen MarketState is never mutated.
            _dir    = state.trade_direction
            _base   = state.weighted_score

            # Enhancement 1: HTF amplifier (aligned = ×1.30, opposing already suppressed above)
            _htf = self._htf_bias.get(symbol, "neutral")
            _htf_mult = (
                1.30 if (_htf == "bearish" and _dir == "short") or
                        (_htf == "bullish" and _dir == "long")
                else 1.00
            )

            # Enhancement 2: Cross-asset confirmation bonus
            _cross = self._compute_cross_asset_bonus(symbol, _dir) if _dir != "none" else 0.0

            # Enhancement 3: Signal momentum bonus
            _momentum = self._momentum.get_momentum_bonus(symbol, _dir) if _dir != "none" else 0.0

            # Enhancement 5: Funding pressure bonus
            _funding_bonus = (
                self._compute_funding_pressure_bonus(_dir, processed.get("funding_rate", 0.0))
                if _dir != "none" else 0.0
            )

            # Enhancement 6: MAG7 Tier 1 direction bonus/penalty
            # Applied AFTER base score (MAG7 strength already in base via coherence mag7_macro tier).
            # Here we apply the directional modifier: aligned = +bonus, opposing = −penalty.
            _mag7_bonus = 0.0
            if _dir != "none" and symbol != "USTECH100-USD":
                _mag7_dir = processed.get("mag7_direction", "neutral")
                _mag7_str = processed.get("mag7_strength", 0.0)
                if _mag7_dir != "neutral" and _mag7_str > 0:
                    if (_mag7_dir == "bearish" and _dir == "short") or \
                       (_mag7_dir == "bullish" and _dir == "long"):
                        _mag7_bonus = min(0.5, _mag7_str * 0.4)   # Aligned: up to +0.5
                    else:
                        _mag7_bonus = -min(0.3, _mag7_str * 0.25)  # Opposing: up to -0.3

            # Tier 6A: ValueChain on-chain position flow (SoDEX-native)
            _vc_bonus = 0.0
            if self.vc_monitor and hasattr(self.vc_monitor, 'get_onchain_score') and _dir != "none":
                _vc_score = self.vc_monitor.get_onchain_score(symbol)
                _vc_dir   = self.vc_monitor.get_onchain_direction(symbol)
                if _vc_score > 0:
                    _vc_bonus = _vc_score if _vc_dir == _dir else _vc_score * 0.3

            # Tier 6B: OI arb — Bybit OI divergence vs SoDEX price
            # Takes the stronger of vc_bonus and oi_score when both are aligned.
            # BNB primary (thin SoDEX book amplifies Bybit OI signals).
            if self.oi_monitor is not None and _dir != "none":
                self.oi_monitor.evaluate(symbol)
                _oi_score, _oi_dir = self.oi_monitor.get_oi_score(symbol, _dir)
                if _oi_score > 0:
                    if _oi_dir == _dir:
                        # OI signal aligned — take strongest of vc or oi
                        _vc_bonus = max(_vc_bonus, _oi_score)
                    else:
                        # OI conflicts with direction — slight confidence reduction
                        _vc_bonus = max(0.0, _vc_bonus - _oi_score * 0.15)

            # Aggregate: (base + additive bonuses) × HTF multiplier
            _pre_htf  = _base + _cross + _momentum + _funding_bonus + _mag7_bonus + _vc_bonus
            _enhanced = min(10.0, _pre_htf * _htf_mult)

            # Enhancement 4: Session timing — adjusts threshold, not score.
            # Store effective_min_coherence on state for the risk gate to read.
            _sess_mult, _sess_name = self._session.get_multiplier()
            _eff_threshold = self._session.adjusted_threshold(
                getattr(self.config, 'min_coherence', 2.0), _sess_mult
            )

            # Apply enhancements via model_copy only when score actually changed.
            if abs(_enhanced - _base) > 0.001 and _dir != "none":
                # Recompute size_multiplier from new score to keep consistency
                from intelligence.coherence import CoherenceEngine as _CE
                _new_size_mult = _CE(None).get_size_multiplier(_enhanced)
                state = state.model_copy(update={
                    "weighted_score":  round(_enhanced, 4),
                    "coherence_score": round(_enhanced, 4),
                    "size_multiplier": _new_size_mult,
                })

            logger.debug("enhancement_layer",
                         symbol=symbol,
                         base=round(_base, 3),
                         cross=round(_cross, 3),
                         momentum=round(_momentum, 3),
                         funding_bonus=round(_funding_bonus, 3),
                         mag7_bonus=round(_mag7_bonus, 3),
                         vc_bonus=round(_vc_bonus, 3),
                         htf_mult=_htf_mult,
                         enhanced=round(_enhanced, 3),
                         session=_sess_name,
                         sess_mult=_sess_mult,
                         mag7_direction=processed.get("mag7_direction", "neutral"),
                         eff_threshold=round(_eff_threshold, 3))

            # Record direction for cross-asset bonus on next cycle
            self._current_directions[symbol] = _dir

            # Record for momentum tracker (confirmed signals only)
            if _dir != "none":
                self._momentum.record(symbol, _dir, _enhanced, confirmed=True)
            # ── End Enhancement Layer ─────────────────────────────────────────────

            # ── Macro Signal Engine (7 cross-asset portfolio signals) ─────────────
            # Update engine with this asset's resolved signal, then apply portfolio-
            # level adjustments to get the final coherence score.
            _funding_rate   = processed.get("funding_rate", 0.0)
            _oi_flow_dir    = self.vc_monitor.get_onchain_direction(symbol) \
                if self.vc_monitor and hasattr(self.vc_monitor, "get_onchain_direction") \
                else "none"
            _mark_price     = getattr(state, "mark_price", 0.0)
            self._macro.update_asset_signal(
                symbol=symbol,
                direction=_dir,
                coherence=round(state.weighted_score, 4),
                funding_rate=_funding_rate,
                oi_flow_direction=_oi_flow_dir,
                mark_price=_mark_price,
            )
            # Apply macro adjustments when this is an actionable signal
            if _dir != "none" and state.weighted_score >= self._macro._min_coherence:
                _macro_adj, _macro_bd = self._macro.apply_macro_to_coherence(
                    symbol=symbol,
                    direction=_dir,
                    base_coherence=round(state.weighted_score, 4),
                    tiers_fired=[],   # populated once tiers_fired tracking is added
                )
                if abs(_macro_adj - state.weighted_score) > 0.01:
                    from intelligence.coherence import CoherenceEngine as _CE2
                    _macro_size_mult = _CE2(None).get_size_multiplier(_macro_adj)
                    state = state.model_copy(update={
                        "weighted_score":  round(_macro_adj, 4),
                        "coherence_score": round(_macro_adj, 4),
                        "size_multiplier": _macro_size_mult,
                    })

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

    def _compute_cross_asset_bonus(self, symbol: str, direction: str) -> float:
        """
        Bonus for cross-asset confirmation: how many OTHER symbols are
        pointing the same direction right now (≥2 required for any bonus).
        """
        if direction == "none":
            return 0.0
        aligned = sum(
            1 for sym, d in self._current_directions.items()
            if sym != symbol and d == direction
        )
        if aligned >= 4: return 0.9
        if aligned == 3: return 0.6
        if aligned == 2: return 0.3
        return 0.0

    def _compute_funding_pressure_bonus(self, direction: str, funding_rate: float) -> float:
        """
        +0.3 when trading WITH funding flow (collecting funding).
        −0.3 when trading AGAINST funding flow (paying funding).
        Neutral inside ±0.0001 threshold.
        """
        THRESHOLD = 0.0001  # 0.01% per 8h
        if abs(funding_rate) < THRESHOLD:
            return 0.0
        if funding_rate > THRESHOLD:
            return 0.3 if direction == "short" else -0.3
        return 0.3 if direction == "long" else -0.3

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
