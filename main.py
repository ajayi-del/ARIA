import asyncio
import os
import structlog
import signal as sys_signal
import time
from dotenv import load_dotenv
import logging
from aiohttp import web as _aiohttp_web

from core.config import Settings
from core.market_engine import MarketEngine
from data.sodex_feed import SoDEXFeed
from data.bybit_feed import BybitFeed, HybridFeed, BYBIT_SYMBOL_MAP, SUPPORTED_ASSETS
from data.basis_tracker import BasisTracker
from data.orderbook_store import OrderbookStore
from data.mark_price_store import MarkPriceStore
from data.candle_buffer import CandleBuffer
from data.trade_flow_store import TradeFlowStore
from display.terminal import TerminalDisplay

# Execution layer imports
from execution.signer import SoDEXSigner
from execution.nonce_manager import NonceManager
from execution.sodex_client import SoDEXClient
from execution.paper_client import PaperClient
from execution.order_manager import OrderManager
from execution.emergency import EmergencyFlatten
from risk.margin_engine import MarginEngine
from risk.position_manager import PositionManager
from risk.risk_engine import RiskEngine

# Memory layer imports
from memory.trade_journal import TradeJournal
from memory.performance import PerformanceTracker
from memory.session_summary import SessionSummary
from execution.schemas import Position, BracketOrder

# Intelligence layer imports
from intelligence.stop_clusters import StopClusterMap
from intelligence.market_hours import MarketHoursGate

# Funding layer imports
from funding.history import FundingHistory
from funding.radar import FundingRadar
from funding.arb_strategy import FundingArbStrategy

# Intelligence Expansion
from intelligence.relative_strength import RelativeStrengthEngine
from risk_calendar import CalendarEngine
from intelligence.interpreter import IntelligenceInterpreter
from intelligence.feedback import SignalFeedbackEngine
from risk.correlation_engine import CorrelationEngine
from core.event_bus import event_bus, EventType, Event
from core.system_state import SystemStateManager

# Monitoring layer imports
from monitoring.alerts import AlertSystem

# Vault layer imports
from vault.vault_manager import VaultManager
from vault.fee_engine import FeeEngine
from vault.performance_cert import PerformanceCert
from vault.bot_fee_ledger import BotFeeLedger


# Globals for signal handler
journal = None
perf = None
session_summary = None
session_start_ms = 0

async def main():
    # 1. Load config
    load_dotenv()
    config = Settings()
    
    # 2. Setup logger
    os.makedirs(config.log_dir, exist_ok=True)
    
    structlog.configure(
        processors=[
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer()
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    
    # Rotating log: 10 MB per file, keep 5 files → max 50 MB on disk.
    # Plain FileHandler grows unbounded (seen at 2.1M lines / ~300 MB in production).
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(
        f"{config.log_dir}/aria.log",
        maxBytes=10 * 1024 * 1024,   # 10 MB
        backupCount=5
    )
    logger = structlog.get_logger(__name__)

    logging.basicConfig(level=config.log_level, handlers=[file_handler])
    
    logger.info(f"Starting ARIA in {config.mode.upper()} mode")

    # 3. Create data stores
    orderbook_stores = {}
    mark_price_stores = {}
    candle_buffers = {}
    trade_flow_stores = {}

    for asset in config.assets:
        orderbook_stores[asset] = OrderbookStore(symbol=asset)
        mark_price_stores[asset] = MarkPriceStore(symbol=asset)
        candle_buffers[asset] = {
            "1m": CandleBuffer(symbol=asset, interval="1m"),
            "15m": CandleBuffer(symbol=asset, interval="15m"),
            "4h": CandleBuffer(symbol=asset, interval="4h", maxlen=50),
        }
        trade_flow_stores[asset] = TradeFlowStore(symbol=asset)

    # 4. Initialize memory layer
    global journal, perf, session_summary, session_start_ms
    journal = TradeJournal()
    journal.load()
    perf = PerformanceTracker()
    session_summary = SessionSummary()
    session_start_ms = int(time.time() * 1000)

    # 5. Create intelligence & risk layer
    stop_clusters = StopClusterMap()
    market_hours = MarketHoursGate()
    regime_engine = RelativeStrengthEngine(config)
    calendar_engine = CalendarEngine()
    
    margin_engine = MarginEngine()
    position_manager = PositionManager()
    order_manager = OrderManager()
    
    # v1.3 Async Init
    try:
        await asyncio.wait_for(calendar_engine.init(), timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning("calendar_init_timeout", message="Continuing without calendar state")
    except Exception as e:
        logger.warning("calendar_init_failed", error=str(e))
    journal.start_writer()
    

    # 6. Initialize monitoring & Vault
    alert_system = AlertSystem(config)
    vault_manager = VaultManager(config.log_dir)
    vault_manager.load()
    fee_engine = FeeEngine()
    perf_cert = PerformanceCert(config.log_dir)

    # Per-bot surgical fee ledgers — independent HWM per bot, same recipient address
    # bot_id determines the log file: logs/fees_aria.json or logs/fees_phantom.json
    _bot_id = "phantom" if config.mode == "paper" else "aria"
    _starting_bal = config.paper_starting_balance if config.mode == "paper" else 0.0
    bot_fee_ledger = BotFeeLedger(
        bot_id=_bot_id,
        starting_balance=_starting_bal,
        log_dir=config.log_dir
    )
    
    # Intelligence Upgrade: Interpreter & Correlation
    correlation_engine = CorrelationEngine()
    system_state = SystemStateManager(assets=config.assets)
    
    # We still need the signal generator from the market engine logic
    from core.signal_generator import SignalGenerator
    sig_gen = SignalGenerator(stop_clusters=stop_clusters)

    # Adaptive feedback engine — learns threshold + tier weights from realized outcomes
    feedback = SignalFeedbackEngine()

    # Bybit ticker intelligence store: OI + funding injected into coherence pipeline.
    # Dict per supported symbol; populated by BybitFeed when tickers subscribed.
    bybit_ticker_stores = {
        a: {} for a in config.assets if a in SUPPORTED_ASSETS
    }

    from core.data_processor import DataProcessor
    interpreter = IntelligenceInterpreter(
        config=config,
        data_processor=DataProcessor(),
        signal_generator=sig_gen,
        orderbook_stores=orderbook_stores,
        mark_price_stores=mark_price_stores,
        candle_buffers=candle_buffers,
        trade_flow_stores=trade_flow_stores,
        system_state=system_state,
        bybit_ticker_stores=bybit_ticker_stores,
        market_hours=market_hours
    )
    # 5. Production Safety Gate
    if config.mode == "live" and not config.live_mode_confirmed:
        logger.critical("PRODUCTION_MODE_NOT_CONFIRMED", message="Aborting to prevent accidental trading. Set LIVE_MODE_CONFIRMED=true in .env")
        return

    # 6. Create execution client — SoDEX mainnet only
    if config.mode == "paper":
        signer = None
        client = PaperClient(config, starting_balance=config.paper_starting_balance)
        logger.info("client_mode_activated", mode="paper", engine="PaperClient")
    else:
        # Require SoDEX private key for live/testnet
        pk = config.sodex_private_key or config.private_key
        if not pk:
            logger.critical("NO_PRIVATE_KEY", message="SODEX_PRIVATE_KEY must be set in .env for live mode")
            return
        signer = SoDEXSigner(
            private_key=pk,
            chain_id=config.sodex_chain_id,
            app_chain="futures"
        )
        nonce_mgr = NonceManager(pk)
        client = SoDEXClient(config, signer, nonce_mgr)
        logger.info("client_mode_activated",
                    mode=config.mode,
                    engine="SoDEXClient",
                    mainnet=config.sodex_mainnet,
                    address=signer.get_address())

    # Start Keepalive
    if hasattr(client, 'start_keepalive'):
        try:
            client.start_keepalive()
        except Exception as e:
            logger.warning("keepalive_start_failed", error=str(e))

    # 5.5 Fetch dynamic symbol IDs
    try:
        await asyncio.wait_for(
            fetch_symbol_ids(client, config, logger),
            timeout=8.0
        )
    except asyncio.TimeoutError:
        logger.warning("symbol_fetch_timeout", message="Continuing with fallback IDs")
    except Exception as e:
        logger.warning("symbol_fetch_failed", error=str(e))

    # 5.6 Resolve numeric Account ID (aid) from SoDEX
    # SoDEX order payloads require the numeric aid, NOT the hex wallet address.
    # aid is assigned when the account first receives a deposit on SoDEX.
    # If aid == 0: account is not yet registered — orders will be rejected.
    NUMERIC_ACCOUNT_ID: int = 0
    address = config.sodex_account_id or config.account_id or ""
    if config.mode != "paper" and address:
        try:
            NUMERIC_ACCOUNT_ID = await asyncio.wait_for(
                client.fetch_account_id(address), timeout=8.0
            )
            if NUMERIC_ACCOUNT_ID == 0:
                logger.warning(
                    "ACCOUNT_NOT_REGISTERED_ON_SODEX",
                    address=address,
                    action="Deposit USDC to SoDEX mainnet to register account and receive a numeric aid.",
                    note="Orders will fail with accountID=0 until account is registered."
                )
            else:
                logger.info("account_registered", aid=NUMERIC_ACCOUNT_ID, address=address)
        except Exception as e:
            logger.warning("account_id_resolution_failed", error=str(e))

    # 5.7 Resolve registered API key name (X-API-Key must be the name, not the raw address)
    if config.mode != "paper":
        try:
            await asyncio.wait_for(client.resolve_api_key_name(), timeout=8.0)
        except Exception as e:
            logger.critical("api_key_name_resolution_failed", error=str(e),
                            action="Register signing key on SoDEX dashboard before trading")
            return

    # 5.8 Set leverage for all active symbols at startup.
    # Prevents residual leverage from a prior session causing unexpected position sizing.
    if config.mode != "paper" and NUMERIC_ACCOUNT_ID > 0:
        for sym in list(config.assets):
            sym_id = SYMBOL_IDS.get(sym, 0)
            if sym_id == 0:
                continue
            try:
                ok = await asyncio.wait_for(
                    client.update_leverage(sym_id, config.default_leverage, NUMERIC_ACCOUNT_ID),
                    timeout=5.0
                )
                if ok:
                    logger.info("leverage_set", symbol=sym, leverage=config.default_leverage)
                else:
                    logger.warning("leverage_set_failed", symbol=sym, leverage=config.default_leverage)
            except Exception as e:
                logger.warning("leverage_set_error", symbol=sym, error=str(e))

    # 5.9 Startup position sync — populate position_manager from any live SoDEX positions.
    # Handles bot restarts while a position is open; shows the position in UI immediately.
    # Stop/TP order IDs are not recovered (session boundary) — UI shows NO STOP warning.
    if config.mode != "paper" and address:
        try:
            live_positions = await asyncio.wait_for(
                client.get_positions(address), timeout=8.0
            )
            synced_count = 0
            for pos_data in live_positions:
                sym = pos_data.get("symbol", "") or pos_data.get("coin", "")
                size = float(pos_data.get("size", 0) or pos_data.get("qty", 0) or 0)
                if size <= 0 or sym not in config.assets:
                    continue
                side_raw = str(pos_data.get("side", "") or pos_data.get("direction", ""))
                side = "long" if side_raw.lower() in ("long", "buy", "1") else "short"
                entry_px = float(pos_data.get("entryPrice", 0) or pos_data.get("avgCost", 0) or 0)
                liq_px = float(pos_data.get("liqPrice", 0) or pos_data.get("liquidationPrice", 0) or 0)
                lev = int(float(pos_data.get("leverage", config.default_leverage) or config.default_leverage))
                if entry_px <= 0:
                    logger.warning("startup_sync_skipped_no_entry", symbol=sym)
                    continue
                synced_pos = Position(
                    symbol=sym,
                    side=side,
                    entry_price=entry_px,
                    size=size,
                    stop_price=0.0,       # not recoverable across session boundary
                    tp1_price=0.0,
                    tp2_price=0.0,
                    tp3_price=0.0,
                    liq_price=liq_px,
                    initial_margin=entry_px * size / max(lev, 1),
                    leverage=lev,
                    opened_at_ms=int(time.time() * 1000),
                )
                position_manager.add(synced_pos)
                synced_count += 1
                logger.warning(
                    "startup_position_synced",
                    symbol=sym, side=side, size=size, entry=entry_px, leverage=lev,
                    note="stop/TP order IDs unknown — manually verify risk on SoDEX"
                )
            if synced_count:
                logger.info("startup_sync_complete", synced=synced_count)
        except Exception as e:
            logger.warning("startup_sync_failed", error=str(e))

    # 7. Create RiskEngine (Updated with Correlation + SoDEX OB Liquidity)
    # basis_tracker is wired in below after the feed is chosen (line ordering)
    risk_engine = RiskEngine(
        config,
        margin_engine,
        position_manager,
        calendar_engine,
        correlation_engine=correlation_engine,
        journal=journal,
        performance_tracker=perf,
        market_hours=market_hours,
        orderbook_stores=orderbook_stores,  # Gate 5: SoDEX live spread/depth check
    )
    
    # 8. Data Feed — Hybrid: Bybit intelligence + SoDEX mark prices
    # Bybit has 1000× SoDEX volume → real ATR, real sweeps, real VPIN, confirmed closes
    # SoDEX mark price = execution reference (divergence from Bybit = trade opportunity)
    if config.data_source == "bybit":
        bybit_feed = BybitFeed(
            config=config,
            mark_price_stores={},                # SoDEX owns mark prices
            orderbook_stores=orderbook_stores,   # Bybit real L2 depth
            candle_buffers=candle_buffers,       # Bybit confirmed 1m closes
            trade_flow_stores=trade_flow_stores, # Bybit real VPIN
            bybit_ticker_stores=bybit_ticker_stores  # OI + funding intelligence
        )
        # USTECH100-USD is SoDEX-only (Bybit doesn't carry it).
        # Pass its data stores to the SoDEX feed so candles/OB/trades flow through.
        _sodex_only = [a for a in config.assets if a not in BYBIT_SYMBOL_MAP or BYBIT_SYMBOL_MAP.get(a) == "unknown"]
        _ustech_candles = {a: candle_buffers[a] for a in _sodex_only if a in candle_buffers}
        _ustech_ob     = {a: orderbook_stores[a] for a in _sodex_only if a in orderbook_stores}
        _ustech_flow   = {a: trade_flow_stores[a] for a in _sodex_only if a in trade_flow_stores}

        sodex_marks_feed = SoDEXFeed(
            config=config,
            mark_price_stores=mark_price_stores,  # SoDEX mark → entry price (all assets)
            orderbook_stores=_ustech_ob,          # SoDEX-only assets get OB from SoDEX
            candle_buffers=_ustech_candles,       # SoDEX-only assets get candles from SoDEX
            trade_flow_stores=_ustech_flow        # SoDEX-only assets get trade flow from SoDEX
        )
        ws_manager = HybridFeed(bybit_feed, sodex_marks_feed)
        logger.info("data_architecture",
            intelligence="bybit_websocket",
            execution="sodex_mainnet",
            candles="bybit_1m_confirmed_closes",
            mark_prices="sodex_native",
            divergence_signal="sodex_mark_vs_bybit_close",
            btc_atr_expected="80_150_usd",
            basis_layer="active",
            reason="bybit_1000x_volume_price_discovery_first")
    else:
        ws_manager = SoDEXFeed(
            config=config,
            mark_price_stores=mark_price_stores,
            orderbook_stores=orderbook_stores,
            candle_buffers=candle_buffers,
            trade_flow_stores=trade_flow_stores
        )
        logger.info("data_source_selected", source="sodex_native",
                    ws_url=config.sodex_ws_perps,
                    mainnet=config.sodex_mainnet)

    # Layer 0: Basis tracker — measures SoDEX mark vs Bybit last close
    # Suspends directional trades during venue dislocation events
    basis_tracker = BasisTracker(
        mark_price_stores=mark_price_stores,
        candle_buffers=candle_buffers
    )
    risk_engine.basis_tracker = basis_tracker  # wire in after tracker is constructed

    # 9. TerminalDisplay (Updated to use Interpreter if needed, or keeping market_engine legacy reference)
    # We'll keep market_engine for the display for now, but it won't be running the loop
    market_engine = MarketEngine(
        config=config,
        orderbook_stores=orderbook_stores,
        mark_price_stores=mark_price_stores,
        candle_buffers=candle_buffers,
        trade_flow_stores=trade_flow_stores,
        stop_clusters=stop_clusters,
        market_hours=market_hours,
        risk_engine=risk_engine
    )
    market_engine.signal_generator = sig_gen

    display = TerminalDisplay(
        config=config,
        orderbook_stores=orderbook_stores,
        mark_price_stores=mark_price_stores,
        candle_buffers=candle_buffers,
        trade_flow_stores=trade_flow_stores,
        health_check=ws_manager.health_check,
        market_engine=None, # Legacy market_engine no longer needed for display
        calendar_engine=calendar_engine,
        journal=journal,
        perf=perf,
        system_state=system_state,
        paper_client=client,
        position_manager=position_manager,
        interpreter=interpreter, # v1.3 New source of truth
        ws_manager=ws_manager
    )

    # 10. Funding Intelligence Layer
    funding_history = FundingHistory()
    funding_history.load()
    funding_radar = FundingRadar(
        config=config,
        trade_flow_stores=trade_flow_stores,
        history=funding_history
    )
    arb_strategy = FundingArbStrategy(
        config=config,
        client=client,
        position_manager=position_manager,
        radar=funding_radar,
        history=funding_history
    )
    arb_strategy.risk_engine = risk_engine
    arb_strategy.system_state = system_state
    arb_strategy.candle_buffers = candle_buffers
    
    # Emergency Handler
    emergency = EmergencyFlatten(config, signer if config.mode != "paper" else None)

    # Latency optimizations — shared mutable state between loops
    _cached_balance = [config.paper_starting_balance]  # [0] = latest balance; list for closure mutation
    _open_entry_ids: dict = {}   # symbol -> journal entry_id (for paper fill wiring)
    _feedback_pending: dict = {}  # entry_id -> {"symbol": ..., "coherence": ..., "tier_scores": ...}
    # Post-rejection cooldown: prevents the same symbol re-entering all 12 gates every
    # second after a SoDEX rejection (274 wasted gate cycles observed in one session).
    # Symbol is blocked for 90s after any bracket failure (auth errors, exchange rejects).
    _rejection_cooldown: dict = {}  # symbol -> float (unix ts of when cooldown expires)

    async def on_signal_ready(event: Event):
        """Event-driven execution handler. Uses cached balance to avoid async latency."""
        state = event.data.get("state")
        if not state:
            return

        symbol = event.symbol

        # ── Rejection cooldown — skip immediately if this symbol was recently rejected ──
        _now = time.time()
        _cooldown_until = _rejection_cooldown.get(symbol, 0.0)
        if _now < _cooldown_until:
            return

        # ── Open position guard — no hedging, no pyramiding before TP1 ─────────
        # SoDEX oneway mode: sending opposite-side order while a position is open
        # creates a cross which the exchange then auto-closes at a loss. Block here.
        if position_manager.count(symbol) > 0:
            return

        # ── Market hours hard gate (XAUT, USTECH100) ────────────────────────
        # market_hours_gate=False means the asset's market is closed; the interpreter
        # suppresses publish for these, but belt-and-suspenders check here too.
        if not state.market_hours_gate:
            return

        # Use cached balance — updated every 5s by execution_cleanup_loop.
        # Avoids 10-50ms REST round-trip on every signal (Hummingbot/Freqtrade pattern).
        balance = _cached_balance[0]
        if balance <= 0:
            balance = await client.get_account_balance(config.sodex_account_id or config.account_id or "")
            _cached_balance[0] = balance

        # ── Temporal size multipliers ─────────────────────────────────────────
        # Session context (weekend crypto 0.75, pre-mkt 0.5), weekly patterns,
        # and Bybit 8h funding reset proximity all reduce position size softly.
        temporal_mult = market_hours.get_combined_multiplier(symbol)
        if temporal_mult <= 0.0:
            logger.debug("signal_dropped_temporal_closed", symbol=symbol, temporal_mult=temporal_mult)
            return  # Hard closed (belt-and-suspenders)

        # Build candidate — pass config to avoid re-parsing .env per signal
        candidate = build_candidate(state, balance, margin_engine, config=config)
        if not candidate:
            _dir = getattr(state, 'trade_direction', 'none')
            _score = getattr(state, 'coherence_score', 0.0)
            _mark = getattr(state, 'mark_price', 0.0)
            _atr = getattr(state, 'atr', 0.0)
            # Only log when score is meaningful — avoid spam on zero-score events
            if _score >= 1.5:
                logger.info(
                    "signal_candidate_failed",
                    symbol=symbol,
                    score=round(_score, 2),
                    direction=_dir,
                    mark_price=_mark,
                    atr=round(_atr, 6),
                    regime=getattr(state, 'regime', '?'),
                    macro=getattr(state, 'macro_bias', '?'),
                    reason=(
                        "no_direction" if _dir == "none" else
                        "mark_price_zero" if _mark <= 0 else
                        "atr_zero" if _atr <= 0 else
                        "size_zero_or_rr"
                    ),
                )
            return

        # Apply temporal multiplier to candidate size
        if temporal_mult < 1.0:
            candidate.size = round(candidate.size * temporal_mult, 8)
            candidate.initial_margin = round(candidate.initial_margin * temporal_mult, 8)

        # Map MarketState regime → risk engine convention (BULL/BEAR/RANGING)
        _regime_map = {
            "risk_on": "BULL", "risk_off": "BEAR",
            "rotational": "RANGING", "confused": "RANGING",
        }
        _risk_regime = _regime_map.get(state.regime, "RANGING")

        # Reconcile 1m regime with 4H HTF bias.
        # When they disagree (e.g. 1m=risk_off / BEAR but 4H=bullish), neutralise to
        # RANGING so Gate A doesn't hard-block the 4H-confirmed direction.
        # The interpreter's HTF filter already suppressed COUNTER-trend entries upstream —
        # if a long reached here it survived the HTF check, so Gate A should allow it.
        _htf = interpreter._htf_bias.get(symbol, "neutral")
        if _htf == "bullish" and _risk_regime == "BEAR":
            _risk_regime = "RANGING"
        elif _htf == "bearish" and _risk_regime == "BULL":
            _risk_regime = "RANGING"

        # Derive avg_atr: candidate.atr_ratio = current / avg → avg = current / ratio
        _avg_atr = (candidate.atr / candidate.atr_ratio) if candidate.atr_ratio > 0 else 0.0

        # Approximate funding rate from categorical funding_class (Gate C input)
        _funding_map = {
            "extreme_positive": 0.002, "positive": 0.0005, "neutral": 0.0,
            "negative": -0.0005, "extreme_negative": -0.002,
        }
        _funding_rate = _funding_map.get(state.funding_class, 0.0)

        # Risk validation — all gates with full context
        approved, reason = await risk_engine.validate(
            candidate, balance,
            regime=_risk_regime,
            funding_rate=_funding_rate,
            current_atr=candidate.atr,
            avg_atr=_avg_atr,
            orderbook_store=orderbook_stores.get(symbol),
        )

        # Apply Gate C funding multiplier to position size
        if approved and risk_engine._funding_mult != 1.0:
            candidate.size = round(candidate.size * risk_engine._funding_mult, 8)
            candidate.initial_margin = round(
                candidate.initial_margin * risk_engine._funding_mult, 8
            )

        # Log decision
        entry_id = journal.log_decision(
            state=state,
            candidate=candidate,
            approved=approved,
            reason=reason if not approved else None,
            cal_state=await calendar_engine.get_state(symbol)
        )

        logger.info("execution_decision",
            symbol=symbol,
            approved=approved,
            reason=reason,
            coherence=state.coherence_score,
            direction=state.trade_direction,
            coherence_mult=state.coherence_mult,
            freshness_mult=state.freshness_mult
        )

        if not approved:
            return

        # Push gate-passed candidate to UI before sending to exchange
        display.push_trade_candidate(
            symbol=symbol,
            direction=candidate.side,
            score=state.coherence_score,
            entry=candidate.entry_price,
            stop=candidate.stop_price,
            tp1=candidate.tp1_price,
            size=candidate.size,
            leverage=candidate.leverage,
            rr=candidate.rr_ratio,
            status="SUBMITTED",
        )

        # Execute bracket — use numeric aid (resolved at startup), NOT the hex address
        bracket = BracketOrder(
            candidate=candidate,
            account_id=str(NUMERIC_ACCOUNT_ID),
            symbol_id=SYMBOL_IDS.get(symbol, 0)
        )
        result = await client.place_bracket(bracket)

        if result.success:
            position = Position(
                symbol=symbol,
                side=candidate.side,
                entry_price=candidate.entry_price,
                size=candidate.size,
                stop_price=candidate.stop_price,
                tp1_price=candidate.tp1_price,
                tp2_price=candidate.tp2_price,
                tp3_price=candidate.tp3_price,
                liq_price=candidate.liq_price,
                initial_margin=candidate.initial_margin,
                leverage=candidate.leverage,
                opened_at_ms=candidate.timestamp_ms
            )
            position_manager.add(position)
            # Track entry_id for paper/live fill wiring (cleanup_loop closes journal on exit)
            _open_entry_ids[symbol] = entry_id
            # Register with feedback engine for outcome-based calibration
            if entry_id:
                tier_scores = sig_gen._last_components.get(symbol, {})
                feedback.record_open(
                    entry_id=entry_id,
                    symbol=symbol,
                    direction=candidate.side,
                    coherence=state.coherence_score,
                    tier_scores=tier_scores,
                )
            
            # Send alert
            alert_system.notify_trade_placed(
                symbol=symbol,
                side=candidate.side,
                price=candidate.entry_price,
                stop=candidate.stop_price,
                size=candidate.size,
                rr=candidate.rr_ratio
            )

            journal.update_outcome(entry_id=entry_id, outcome="open")
            logger.info("bracket_placed", symbol=symbol, entry=candidate.entry_price)
            display.push_trade_candidate(
                symbol=symbol,
                direction=candidate.side,
                score=state.coherence_score,
                entry=candidate.entry_price,
                stop=candidate.stop_price,
                tp1=candidate.tp1_price,
                size=candidate.size,
                leverage=candidate.leverage,
                rr=candidate.rr_ratio,
                status="PLACED",
            )
        else:
            # 90s cooldown: prevents hammering SoDEX / re-running 12 gates on same signal
            _rejection_cooldown[symbol] = time.time() + 90.0
            logger.error("bracket_failed", symbol=symbol, error=result.error,
                         score=round(state.coherence_score, 2),
                         direction=candidate.side,
                         entry=candidate.entry_price,
                         stop=candidate.stop_price,
                         size=candidate.size,
                         leverage=candidate.leverage,
                         rr=round(candidate.rr_ratio, 2),
                         cooldown_until=time.strftime('%H:%M:%S', time.localtime(time.time() + 90.0)))
            display.push_trade_candidate(
                symbol=symbol,
                direction=candidate.side,
                score=state.coherence_score,
                entry=candidate.entry_price,
                stop=candidate.stop_price,
                tp1=candidate.tp1_price,
                size=candidate.size,
                leverage=candidate.leverage,
                rr=candidate.rr_ratio,
                status="REJECTED",
                error=result.error,
            )

    async def execution_cleanup_loop():
        """Handles equity updates, balance caching, position reconciliation, and feedback."""
        _balance_log_counter = 0
        _balance_poll_counter = 0
        _position_poll_counter = 0  # live position reconciliation cadence
        _feedback_sync_counter = 0  # feedback threshold/weight sync cadence

        while True:
            try:
                # Balance polling: every 5s to avoid hammering the API on each signal.
                # on_signal_ready reads _cached_balance (set here) instead of awaiting get_account_balance.
                _balance_poll_counter += 1
                if _balance_poll_counter >= 5 or _cached_balance[0] == 0.0:
                    _balance_poll_counter = 0
                    acc_id = config.sodex_account_id or config.account_id or ""
                    _cached_balance[0] = await client.get_account_balance(acc_id)

                display.update_equity(_cached_balance[0])

                # Log balance telemetry every 60 seconds
                _balance_log_counter += 1
                if _balance_log_counter >= 60:
                    _balance_log_counter = 0
                    balance = _cached_balance[0]
                    logger.info(
                        "account_balance",
                        balance=f"${balance:.2f}",
                        risk_per_trade=f"${balance * config.risk_pct:.2f}",
                        arb_capital=f"${balance * config.arb_capital_pct:.2f}",
                        min_notional=f"${config.min_trade_notional_usd:.2f}",
                        max_notional=f"${balance * config.default_leverage * 0.90:.2f} (dynamic)",
                    )

                # Paper fill event processing — sync paper closes into position_manager + journal
                if config.mode == "paper" and hasattr(client, 'get_events'):
                    for ev in client.get_events():
                        sym = ev.get("symbol", "")
                        ev_type = ev.get("type", "")
                        if ev_type == "trade_closed":
                            # Remove from position_manager so risk gates see the freed slot
                            positions = position_manager.get(sym)
                            if positions:
                                position_manager.close(sym, 0)
                            # Update journal outcome
                            entry_id = _open_entry_ids.pop(sym, None)
                            pnl = ev.get("pnl", 0.0)
                            if entry_id:
                                outcome = "win" if pnl >= 0 else "loss"
                                journal.update_outcome(
                                    entry_id=entry_id,
                                    outcome=outcome,
                                    pnl_usd=pnl,
                                    closed_at_ms=int(time.time() * 1000)
                                )
                                feedback.record_result(entry_id, won=pnl >= 0, pnl=pnl)
                                logger.info("trade_closed",
                                            symbol=sym, outcome=outcome, pnl=f"${pnl:.2f}")
                            # Surgical fee: charge performance fee on profitable close
                            fee = bot_fee_ledger.on_trade_closed(
                                symbol=sym,
                                pnl_usd=pnl,
                                current_balance=_cached_balance[0]
                            )
                            if fee > 0:
                                _cached_balance[0] = max(0.0, _cached_balance[0] - fee)
                        elif ev_type == "tp1_hit":
                            position_manager.mark_tp1_hit(sym, 0)
                            logger.info("tp1_hit", symbol=sym)

                # Live position reconciliation — every 30s poll exchange.
                # Detects both CLOSES (tracked but gone) and NEW UNTRACKED positions
                # (exchange has it but we don't know — e.g. entry filled after bot restart,
                # or bracket returned partial success).
                elif config.mode != "paper":
                    _position_poll_counter += 1
                    if _position_poll_counter >= 30:
                        _position_poll_counter = 0
                        try:
                            addr = config.sodex_account_id or config.account_id or ""
                            live_positions = await client.get_positions(addr)
                            # Build map: symbol → (size, raw_pos_data)
                            exchange_open: dict = {}
                            for pos in live_positions:
                                sym = pos.get("symbol", "") or pos.get("coin", "")
                                size = float(pos.get("size", 0) or pos.get("qty", 0) or 0)
                                if size > 0 and sym:
                                    exchange_open[sym] = (size, pos)

                            # ── Detect closes ────────────────────────────────────
                            for sym, positions in list(position_manager._positions.items()):
                                if sym not in exchange_open and positions:
                                    pos_obj = positions[0]
                                    mark = mark_price_stores[sym].mark_price if sym in mark_price_stores else 0.0
                                    if mark > 0 and pos_obj.entry_price > 0:
                                        if pos_obj.side == "long":
                                            pnl = (mark - pos_obj.entry_price) * pos_obj.size
                                        else:
                                            pnl = (pos_obj.entry_price - mark) * pos_obj.size
                                    else:
                                        pnl = 0.0
                                    position_manager.close(sym, 0)
                                    entry_id = _open_entry_ids.pop(sym, None)
                                    outcome = "win" if pnl >= 0 else "loss"
                                    if entry_id:
                                        journal.update_outcome(
                                            entry_id=entry_id,
                                            outcome=outcome,
                                            pnl_usd=pnl,
                                            closed_at_ms=int(time.time() * 1000),
                                        )
                                        feedback.record_result(entry_id, won=pnl >= 0, pnl=pnl)
                                    fee = bot_fee_ledger.on_trade_closed(
                                        symbol=sym,
                                        pnl_usd=pnl,
                                        current_balance=_cached_balance[0],
                                    )
                                    if fee > 0:
                                        _cached_balance[0] = max(0.0, _cached_balance[0] - fee)
                                    logger.info("live_trade_closed",
                                                symbol=sym, outcome=outcome, pnl=f"${pnl:.4f}")

                            # ── Detect new untracked positions ───────────────────
                            for sym, (size, pos_data) in exchange_open.items():
                                if sym not in config.assets:
                                    continue
                                if not position_manager.get(sym):
                                    # Position on exchange not in position_manager.
                                    # Could be: entry filled while bot was down, or
                                    # bracket placed entry successfully but crashed before
                                    # adding to position_manager.
                                    side_raw = str(pos_data.get("side", "") or pos_data.get("direction", ""))
                                    side = "long" if side_raw.lower() in ("long", "buy", "1") else "short"
                                    entry_px = float(pos_data.get("entryPrice", 0) or pos_data.get("avgCost", 0) or 0)
                                    liq_px = float(pos_data.get("liqPrice", 0) or pos_data.get("liquidationPrice", 0) or 0)
                                    lev = int(float(pos_data.get("leverage", config.default_leverage) or config.default_leverage))
                                    if entry_px <= 0:
                                        continue
                                    synced = Position(
                                        symbol=sym,
                                        side=side,
                                        entry_price=entry_px,
                                        size=size,
                                        stop_price=0.0,
                                        tp1_price=0.0,
                                        tp2_price=0.0,
                                        tp3_price=0.0,
                                        liq_price=liq_px,
                                        initial_margin=entry_px * size / max(lev, 1),
                                        leverage=lev,
                                        opened_at_ms=int(time.time() * 1000),
                                    )
                                    position_manager.add(synced)
                                    logger.warning(
                                        "untracked_position_synced",
                                        symbol=sym, side=side, size=size,
                                        entry=entry_px, leverage=lev,
                                        note="stop/TP order IDs unknown — verify risk on SoDEX"
                                    )
                        except Exception as _pe:
                            logger.warning("position_poll_failed", error=str(_pe))

                # Feedback sync — every 30s update threshold + tier weights
                _feedback_sync_counter += 1
                if _feedback_sync_counter >= 30:
                    _feedback_sync_counter = 0
                    adj_threshold = feedback.get_adjusted_threshold()
                    config.min_coherence = adj_threshold
                    sig_gen.set_tier_weight_overrides(feedback.get_tier_weights())
                    summary = feedback.get_summary()
                    if summary["active"]:
                        logger.info("feedback_sync",
                                    threshold=adj_threshold,
                                    win_rate=summary["win_rate"],
                                    trades=summary["total_settled"])

            except Exception as e:
                logger.error("cleanup_loop_error", error=str(e))
            await asyncio.sleep(1.0)

    async def funding_loop():
        """Loop for funding radar updates and arb execution (SoDEX-native)"""
        import traceback
        _last_known_rates: dict[str, float] = {}

        while True:
            try:
                # Fetch rates from SoDEX REST (single source of truth)
                real_rates = await ws_manager.fetch_funding_rates()
                if real_rates:
                    _last_known_rates.update(real_rates)
                    logger.info("funding_rates_fetched", source="sodex_rest", count=len(real_rates))

                # Persist to history
                for symbol in config.assets:
                    rate = _last_known_rates.get(symbol, 0.0)
                    funding_history.add(symbol, rate, "sodex_rest")

                # Update funding radar and display
                snapshots = await funding_radar.update_all()
                display.update_funding(snapshots)
                arb_strategy.update_positions(mark_price_stores)
                display.update_arbs(arb_strategy.get_open_arbs())

                logger.info("funding_radar_updated", symbols=list(snapshots.keys()))

                # Evaluate and monitor arb positions
                candidate = await arb_strategy.evaluate()
                if candidate:
                    await arb_strategy.open_arb(candidate)
                await arb_strategy.monitor_arbs(snapshots)

                for symbol, snap in snapshots.items():
                    logger.info("funding_update",
                                symbol=symbol,
                                rate=snap.rate,
                                carry_score=snap.carry_score,
                                arb_signal=snap.arb_signal)

            except Exception as e:
                logger.error("funding_loop_error", error=str(e), traceback=traceback.format_exc())

            await asyncio.sleep(300)

    async def vault_loop():
        """Hourly vault NAV, fee accrual, and performance cert."""
        while True:
            try:
                # 1. Update Vault NAV
                acc_id = config.sodex_account_id or config.account_id or ""
                balance = _cached_balance[0] or await client.get_account_balance(acc_id)
                nav = vault_manager.get_total_nav(balance)

                # 2. Accrue legacy vault fees
                fees = fee_engine.process_vault_fees(nav, vault_manager.high_water_mark)

                # 3. Accrue per-bot management fee (surgical ledger)
                mgmt_fee = bot_fee_ledger.accrue_management(balance)

                # 4. Save performance cert
                perf_cert.save_to_file()

                fee_summary = bot_fee_ledger.get_summary()
                logger.info("vault_report",
                            bot=_bot_id,
                            nav=f"${nav:.2f}",
                            legacy_fees=f"${fees['total_fees']:.4f}",
                            bot_mgmt_fee=f"${mgmt_fee:.6f}",
                            total_perf_fees=f"${fee_summary['total_performance_fees']:.4f}",
                            total_mgmt_fees=f"${fee_summary['total_management_fees']:.6f}",
                            hwm=f"${fee_summary['high_water_mark']:.2f}",
                            recipient=fee_summary['recipient'])

                if nav > vault_manager.high_water_mark:
                    vault_manager.high_water_mark = nav
                    vault_manager.save()

            except Exception as e:
                logger.error("vault_loop_error", error=str(e))

            await asyncio.sleep(3600)  # Hourly

    async def calendar_loop():
        """Periodic calendar updates and log blocks"""
        while True:
            try:
                states = await calendar_engine.get_states_all(config.assets)
                for symbol, s in states.items():
                    if s.regime == "BLOCK":
                        logger.warning("calendar_block_active", symbol=symbol, reason=s.reason)
                    elif s.regime == "CAUTION":
                        logger.info("calendar_caution_active", symbol=symbol, reason=s.reason, size_mult=s.size_multiplier)
            except Exception as e:
                logger.error("calendar_loop_error", error=str(e))
            await asyncio.sleep(300) # 5 mins

    async def health_server():
        """Lightweight health endpoint for Railway liveness checks."""
        async def _health(request):
            phase = system_state.get_global_phase().value if system_state else "unknown"
            return _aiohttp_web.Response(
                text=f'{{"status":"ok","phase":"{phase}","mode":"{config.mode}"}}',
                content_type="application/json"
            )
        app = _aiohttp_web.Application()
        app.router.add_get("/health", _health)
        app.router.add_get("/", _health)
        runner = _aiohttp_web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", 8080))
        site = _aiohttp_web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info("health_server_started", port=port)
        await asyncio.Event().wait()  # run forever

    # 11. Subscribe and Start
    event_bus.subscribe(EventType.SIGNAL_READY, on_signal_ready)
    
    logger.info("Starting ARIA execution gather")
    
    # ARC v1.3 Patch Part A: Historical fetch on startup
    if hasattr(ws_manager, "fetch_historical"):
        logger.info("fetching_historical_data", source=type(ws_manager).__name__)
        await ws_manager.fetch_historical()
        logger.info("historical_complete")

    try:
        # ARC 1.3: Terminal MUST be first to takeover screen.
        # We wrap in gather for concurrent execution of loops.
        await asyncio.gather(
            display.run(),           # Priority 1: Terminal UI
            event_bus.start(),       # Priority 2: Event system
            interpreter.start(),      # Priority 3: Intelligence
            ws_manager.start(),
            execution_cleanup_loop(),
            funding_loop(),
            vault_loop(),
            calendar_loop(),
            health_server(),           # Railway liveness check
            return_exceptions=False
        )
    except Exception as e:
        logger.error("system_gather_critical_failure", error=str(e))
        raise
    finally:
        # 9. Graceful shutdown — only flatten if we actually have tracked positions
        if config.mode != "paper" and position_manager.get_all():
            logger.warning("triggering_emergency_flatten")
            await emergency.flatten_all()
            
        await event_bus.stop()
        await journal.stop_writer()
        await alert_system.stop()
        await market_engine.stop()
        await ws_manager.stop()
        logger.info("ARIA shutdown complete")


# Module-level config singleton for build_candidate — avoids re-parsing .env on every signal
_build_candidate_config = None

def build_candidate(state, balance, margin_engine, config=None):
    """Takes MarketState + balance + margin_engine + optional config. Returns TradeCandidate or None."""
    from execution.schemas import TradeCandidate
    global _build_candidate_config

    # Use provided config, or lazily cache one (never re-parse .env per call)
    cfg = config
    if cfg is None:
        if _build_candidate_config is None:
            from core.config import Settings as _Settings
            _build_candidate_config = _Settings()
        cfg = _build_candidate_config

    # Need a valid mark price as entry
    entry = getattr(state, 'mark_price', 0.0)
    if not entry or entry <= 0:
        return None

    # Need a clear direction
    direction = getattr(state, 'trade_direction', 'none')
    if direction not in ('long', 'short'):
        return None

    # ATR-based stop: 1.5 ATR buffer from entry
    atr = getattr(state, 'atr', 0.0)
    if atr <= 0:
        return None

    stop_buffer = atr * 1.5
    if direction == 'long':
        stop = entry - stop_buffer
    else:
        stop = entry + stop_buffer

    if stop <= 0:
        return None

    # TP levels at 1R, 2R, 3R
    risk_distance = abs(entry - stop)
    if risk_distance <= 0:
        return None

    if direction == 'long':
        tp1 = entry + risk_distance * 1.0
        tp2 = entry + risk_distance * 2.0
        tp3 = entry + risk_distance * 3.0
    else:
        tp1 = entry - risk_distance * 1.0
        tp2 = entry - risk_distance * 2.0
        tp3 = entry - risk_distance * 3.0

    rr = abs(tp1 - entry) / risk_distance  # = 1.0 for 1R TP1
    if rr < 2.0:
        # TP1 is only 1R — check TP2 for 2R gate
        rr = abs(tp2 - entry) / risk_distance
    if rr < 2.0:
        return None

    atr_ratio = getattr(state, 'atr_vs_baseline', 1.0)

    try:
        size, margin, lev = margin_engine.compute_size(
            balance, cfg.risk_pct, entry, stop, cfg.default_leverage,
            state.symbol, atr_ratio=atr_ratio,
            min_notional_usd=cfg.min_trade_notional_usd,
        )
    except Exception:
        return None

    if size <= 0:
        return None

    # Compute liquidation price
    from risk.margin_engine import MarginEngine
    liq_price = MarginEngine().compute_liquidation_price(
        state.symbol, entry, 1 if direction == 'long' else -1, lev, size
    )

    return TradeCandidate(
        symbol=state.symbol,
        side=direction,
        entry_price=entry,
        stop_price=stop,
        tp1_price=tp1,
        tp2_price=tp2,
        tp3_price=tp3,
        size=size,
        initial_margin=margin,
        leverage=lev,
        rr_ratio=rr,
        coherence_score=getattr(state, 'coherence_score', 0.0),
        size_multiplier=getattr(state, 'size_multiplier', 0.0),
        signal_reason=getattr(state, 'macro_bias', 'none'),
        invalidation=getattr(state, 'invalidation_reason', '') or '',
        timestamp_ms=getattr(state, 'timestamp_ms', 0),
        signal_age_ms=getattr(state, 'signal_age_ms', 0),
        atr=atr,
        atr_ratio=atr_ratio,  # Gate D: volatility guard — was missing, defaulted to 1.0
    )


# SYMBOL IDs mapping (Initially empty, populated by fetch_symbol_ids)
SYMBOL_IDS = {}

async def fetch_symbol_ids(client, config, logger):
    """
    Fetches symbol IDs from SoDEX GET /markets/symbols and populates SYMBOL_IDS.
    Response format: {"code":0,"data":[{"name":"BTC-USD","id":1,...},...]}
    Field: item["id"] (primary) or item["symbolID"] (fallback).
    """
    import httpx
    global SYMBOL_IDS
    # Correct fallback — real SoDEX symbol IDs verified from /markets/symbols
    _FALLBACK = {"BTC-USD": 1, "ETH-USD": 2, "SOL-USD": 6, "XAUT-USD": 11,
                 "BNB-USD": 9, "LINK-USD": 5, "AVAX-USD": 24}
    try:
        if "PaperClient" in str(type(client)):
            logger.info("paper_mode_detected", message="Using static symbol fallback")
            SYMBOL_IDS = _FALLBACK.copy()
            return

        base_url = getattr(client, "base_url", None)
        if not base_url:
            raise AttributeError("Client missing base_url")

        async with httpx.AsyncClient(timeout=10.0) as http:
            response = await http.get(f"{base_url}/markets/symbols")

        if response.status_code != 200:
            logger.warning("failed_to_fetch_symbols", status=response.status_code)
            SYMBOL_IDS = _FALLBACK.copy()
            return

        payload = response.json()
        if payload.get("code") != 0:
            logger.warning("symbols_api_error", code=payload.get("code"), msg=payload.get("msg"))
            SYMBOL_IDS = _FALLBACK.copy()
            return

        found_map = {}
        for item in payload.get("data", []):
            name = (item.get("name") or item.get("symbol") or "").upper()
            sid = int(item.get("id") or item.get("symbolID") or 0)
            if name and sid > 0:
                found_map[name] = sid

        SYMBOL_IDS = {}
        missing = []
        for asset in config.assets:
            if asset in found_map:
                SYMBOL_IDS[asset] = found_map[asset]
            else:
                missing.append(asset)

        logger.info("symbol_ids_loaded", mapping=SYMBOL_IDS)

        if missing:
            logger.warning("symbols_not_found", missing=missing)
            config.assets = [a for a in config.assets if a not in missing]
            logger.info("active_assets_updated", assets=config.assets)

    except Exception as e:
        logger.error("symbol_fetch_error", error=str(e))
        SYMBOL_IDS = _FALLBACK.copy()

def shutdown_handler(sig, frame):
    """Graceful shutdown — signals the asyncio event loop to stop cleanly."""
    print("\nShutdown signal received — draining journal and exiting...")
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
    except Exception:
        import sys
        sys.exit(0)


if __name__ == "__main__":
    # Register shutdown handlers
    sys_signal.signal(sys_signal.SIGINT, shutdown_handler)
    sys_signal.signal(sys_signal.SIGTERM, shutdown_handler)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
