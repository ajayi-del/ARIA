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
from data.bybit_feed import BybitFeed, HybridFeed
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
from risk.correlation_engine import CorrelationEngine
from core.event_bus import event_bus, EventType, Event
from core.system_state import SystemStateManager

# Monitoring layer imports
from monitoring.alerts import AlertSystem

# Vault layer imports
from vault.vault_manager import VaultManager
from vault.fee_engine import FeeEngine
from vault.performance_cert import PerformanceCert


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
    
    file_handler = logging.FileHandler(f"{config.log_dir}/aria.log")
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
            "15m": CandleBuffer(symbol=asset, interval="15m")
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
    
    # Intelligence Upgrade: Interpreter & Correlation
    correlation_engine = CorrelationEngine()
    system_state = SystemStateManager(assets=config.assets)
    
    # We still need the signal generator from the market engine logic
    from core.signal_generator import SignalGenerator
    sig_gen = SignalGenerator(stop_clusters=stop_clusters)
    
    from core.data_processor import DataProcessor
    interpreter = IntelligenceInterpreter(
        config=config,
        data_processor=DataProcessor(),
        signal_generator=sig_gen,
        orderbook_stores=orderbook_stores,
        mark_price_stores=mark_price_stores,
        candle_buffers=candle_buffers,
        trade_flow_stores=trade_flow_stores,
        system_state=system_state
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
            mark_price_stores={},              # SoDEX owns mark prices
            orderbook_stores=orderbook_stores, # Bybit real L2 depth
            candle_buffers=candle_buffers,     # Bybit confirmed 1m closes
            trade_flow_stores=trade_flow_stores# Bybit real VPIN
        )
        sodex_marks_feed = SoDEXFeed(
            config=config,
            mark_price_stores=mark_price_stores,  # SoDEX mark → entry price
            orderbook_stores={},
            candle_buffers={},
            trade_flow_stores={}
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

    async def on_signal_ready(event: Event):
        """Event-driven execution handler."""
        state = event.data.get("state")
        if not state:
            return
            
        symbol = event.symbol
        balance = await client.get_account_balance(config.sodex_account_id or config.account_id or "")
        
        # Build candidate
        candidate = build_candidate(state, balance, margin_engine)
        if not candidate:
            return

        # Risk validation
        approved, reason = await risk_engine.validate(candidate, balance)

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

        # Execute bracket
        bracket = BracketOrder(
            candidate=candidate,
            account_id=config.sodex_account_id or config.account_id or "",
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
        else:
            logger.error("bracket_failed", error=result.error)

    async def execution_cleanup_loop():
        """Handles equity updates and paper fills (non-signal logic)."""
        _balance_log_counter = 0
        while True:
            try:
                acc_id = config.sodex_account_id or config.account_id or ""
                balance = await client.get_account_balance(acc_id)
                display.update_equity(balance)

                # Log balance telemetry every 60 seconds
                _balance_log_counter += 1
                if _balance_log_counter >= 60:
                    _balance_log_counter = 0
                    logger.info(
                        "account_balance",
                        balance=f"${balance:.2f}",
                        risk_per_trade=f"${balance * config.risk_pct:.2f}",
                        arb_capital=f"${balance * config.arb_capital_pct:.2f}",
                        min_notional=f"${config.min_trade_notional_usd:.2f}",
                        max_notional=f"${config.max_trade_notional_usd:.2f}",
                    )

                # v1.3: Paper fills are now event-driven via EventType.MARK_PRICE_UPDATED
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
        """Phase 6: Hourly vault and performance reporting"""
        while True:
            try:
                # 1. Update Vault NAV
                acc_id = config.sodex_account_id or config.account_id or ""
                balance = await client.get_account_balance(acc_id)
                nav = vault_manager.get_total_nav(balance)
                
                # 2. Accrue Fees (hourly)
                fees = fee_engine.process_vault_fees(nav, vault_manager.high_water_mark)
                
                # 3. Save performance cert
                perf_cert.save_to_file()
                
                logger.info("vault_report", nav=nav, fees=fees["total_fees"], hwm=vault_manager.high_water_mark)
                
                # Update HWM if needed
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
        # 9. Graceful shutdown
        if config.mode != "paper":
            logger.warning("triggering_emergency_flatten")
            await emergency.flatten_all()
            
        await event_bus.stop()
        await journal.stop_writer()
        await alert_system.stop()
        await market_engine.stop()
        await ws_manager.stop()
        logger.info("ARIA shutdown complete")


def build_candidate(state, balance, margin_engine):
    """Takes MarketState + balance + margin_engine. Returns TradeCandidate or None."""
    from execution.schemas import TradeCandidate

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
        from core.config import Settings as _Settings
        _cfg = _Settings()
        size, margin, lev = margin_engine.compute_size(
            balance, _cfg.risk_pct, entry, stop, _cfg.default_leverage,
            state.symbol, atr_ratio=atr_ratio,
            min_notional_usd=_cfg.min_trade_notional_usd,
            max_notional_usd=_cfg.max_trade_notional_usd,
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
    )


# SYMBOL IDs mapping (Initially empty, populated by fetch_symbol_ids)
SYMBOL_IDS = {}

async def fetch_symbol_ids(client, config, logger):
    """
    Fetches symbol IDs from SoDEX API and updates config.assets
    if any symbols are missing from the exchange.
    """
    import httpx
    import json
    global SYMBOL_IDS
    try:
        # v1.3 Resiliency: Check for PaperClient to avoid network calls
        if "PaperClient" in str(type(client)):
            logger.info("paper_mode_detected", message="Using static symbol fallback")
            SYMBOL_IDS = {"BTC-USD": 1, "ETH-USD": 2, "SOL-USD": 3, "XAUT-USD": 4, "BNB-USD": 5, "LINK-USD": 6, "AVAX-USD": 7, "USTECH100-USD": 8}
            return

        async with httpx.AsyncClient(timeout=10.0) as http:
            # Ensure client has base_url attribute
            base_url = getattr(client, "base_url", None)
            if not base_url:
                raise AttributeError("Client missing base_url")
                
            response = await http.get(f"{base_url}/symbols")
            
            if response.status_code != 200:
                logger.warning("failed_to_fetch_symbols", status=response.status_code)
                SYMBOL_IDS = {"BTC-USD": 1, "ETH-USD": 2, "SOL-USD": 3, "XAUT-USD": 4, "BNB-USD": 5, "LINK-USD": 6, "AVAX-USD": 7}
                return

            symbols_data = response.json()
            
            # Found mapping
            found_map = {}
            for s in symbols_data:
                name = s.get("name", "").upper()
                symbol_id = s.get("symbolID")
                if name and symbol_id:
                    found_map[name] = symbol_id

            SYMBOL_IDS = {}
            missing = []
            for asset in config.assets:
                if asset in found_map:
                    SYMBOL_IDS[asset] = found_map[asset]
                else:
                    missing.append(asset)

            if missing:
                logger.warning("symbols_not_found", missing=missing)
                config.assets = [a for a in config.assets if a not in missing]
                logger.info("active_assets_updated", assets=config.assets)

    except Exception as e:
        logger.error("symbol_fetch_error", error=str(e))
        # Critical fallback to avoid crash
        SYMBOL_IDS = {"BTC-USD": 1, "ETH-USD": 2, "SOL-USD": 3, "XAUT-USD": 4, "BNB-USD": 5, "LINK-USD": 6, "AVAX-USD": 7}

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
