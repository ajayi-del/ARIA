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
from execution.order_manager import OrderManager
from execution.emergency import EmergencyFlatten
from execution.metrics import metrics_logger
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

# v1.4 New intelligence layers
from execution.sodex_spot_client import SoDEXSpotClient
from data.valuechain_monitor import ValueChainMonitor, LiquidationSignal
from funding.arb_strategy import TrueDeltaNeutralArb
from risk.drawdown_guard import DrawdownGuard
from risk.drawdown_manager import DrawdownManager
from intelligence.liquidation_signal import LiquidationSignalEngine

# v1.5 Fee Intelligence System
from core.fee_engine import SoDEXFeeEngine as SoDEXFeeIntelligence
from memory.volume_tracker import VolumeTracker

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

    # Early declarations — Python scoping requires these before any conditional
    # assignment lower in the function to avoid UnboundLocalError.
    spot_client: "SoDEXSpotClient | None" = None
    true_arb: "TrueDeltaNeutralArb | None" = None
    vc_monitor: "ValueChainMonitor | None" = None
    liq_engine: "LiquidationSignalEngine | None" = None       # v1.6: Tier 6 on-chain liq signals
    drawdown_manager: "DrawdownManager | None" = None         # v1.6: 4-level circuit breaker
    
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
    metrics_logger.start()  # non-blocking async metrics queue

    # 6. Initialize monitoring & Vault
    alert_system = AlertSystem(config)
    vault_manager = VaultManager(config.log_dir)
    vault_manager.load()
    fee_engine = FeeEngine()
    perf_cert = PerformanceCert(config.log_dir)

    # Per-bot surgical fee ledgers — independent HWM per bot, same recipient address
    # bot_id determines the log file: logs/fees_aria.json or logs/fees_phantom.json
    bot_fee_ledger = BotFeeLedger(
        bot_id="aria",
        starting_balance=0.0,   # deferred init: HWM set on first real balance fetch
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
        market_hours=market_hours,
        liq_engine=liq_engine,
    )
    # 5. Production Safety Gate
    if config.mode == "live" and not config.live_mode_confirmed:
        logger.critical("PRODUCTION_MODE_NOT_CONFIRMED", message="Aborting to prevent accidental trading. Set LIVE_MODE_CONFIRMED=true in .env")
        return

    # 6. Create execution client — SoDEX mainnet
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
    if address:
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

    # Wire NUMERIC_ACCOUNT_ID into spot client and true_arb now that it is resolved
    if spot_client is not None:
        spot_client.set_account_id(NUMERIC_ACCOUNT_ID)
    if true_arb is not None:
        true_arb.set_symbol_ids(SYMBOL_IDS, NUMERIC_ACCOUNT_ID)

    # 5.7 Resolve registered API key name (X-API-Key must be the name, not the raw address)
    try:
        await asyncio.wait_for(client.resolve_api_key_name(), timeout=8.0)
    except Exception as e:
        logger.critical("api_key_name_resolution_failed", error=str(e),
                        action="Register signing key on SoDEX dashboard before trading")
        return

    # 5.8 Set leverage for all active symbols at startup.
    # Prevents residual leverage from a prior session causing unexpected position sizing.
    if NUMERIC_ACCOUNT_ID > 0:
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
    if address:
        try:
            live_positions = await asyncio.wait_for(
                client.get_positions(address), timeout=8.0
            )
            synced_count = 0
            for pos_data in live_positions:
                sym = pos_data.get("symbol", "") or pos_data.get("coin", "")
                # SoDEX uses NEGATIVE size for short positions — abs() required.
                size = abs(float(pos_data.get("size", 0) or pos_data.get("qty", 0) or 0))
                if size <= 0 or sym not in config.assets:
                    continue
                side_raw = str(pos_data.get("side", "") or pos_data.get("direction", "") or "")
                if side_raw.lower() in ("long", "buy", "1"):
                    side = "long"
                elif side_raw.lower() in ("short", "sell", "2"):
                    side = "short"
                else:
                    # SoDEX oneway: no explicit side field — infer from size sign.
                    # Positive size = long (normal). Negative size = short (rare).
                    _raw_sz = str(pos_data.get("size", "0") or "0").strip()
                    side = "short" if _raw_sz.startswith("-") else "long"
                # SoDEX returns "avgEntryPrice" (confirmed via live API) — NOT "entryPrice" or "avgCost"
                entry_px = float(
                    pos_data.get("avgEntryPrice", 0) or pos_data.get("entryPrice", 0)
                    or pos_data.get("ep", 0) or pos_data.get("avgCost", 0) or 0
                )
                liq_px = float(pos_data.get("liqPrice", 0) or pos_data.get("liquidationPrice", 0) or 0)
                lev = int(float(pos_data.get("leverage", config.default_leverage) or config.default_leverage))
                if entry_px <= 0:
                    logger.warning("startup_sync_skipped_no_entry", symbol=sym, fields=list(pos_data.keys()))
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
                    note="protective stop will be placed after startup"
                )
                # Queue protective stop — cannot place here (client not fully ready).
                # The reconciliation loop runs at ~30s and calls _place_orphan_stop
                # automatically when it finds the position has stop_price=0.
                # This is safe: stop_price=0 is the trigger for the loop to act.
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
        paper_client=None,
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

    # v1.4 — True delta-neutral arb (spot+perp) + ValueChain RPC monitor
    spot_client = None
    true_arb = None
    vc_monitor = None
    drawdown_guard = DrawdownGuard(config)

    # v1.6 DrawdownManager — 4-level circuit breaker (NORMAL/REDUCED/MINIMAL/HALTED)
    # Seed 0 → deferred init on first real balance fetch (prevents false 97% DD).
    drawdown_manager = DrawdownManager(starting_balance=0.0)
    logger.info("drawdown_manager_initialized",
                mode=config.mode,
                max_total_dd=DrawdownManager.MAX_TOTAL_DD,
                max_daily_dd=DrawdownManager.MAX_DAILY_DD)

    # v1.6 LiquidationSignalEngine — Tier 6 on-chain intelligence
    liq_engine = LiquidationSignalEngine()
    interpreter.liq_engine = liq_engine   # Wire Tier 6 engine into interpreter

    # v1.5 — Fee Intelligence System
    # Loaded from env: SOSO_STAKED (default 0). Volume tracker persists 14D history.
    _soso_staked = float(os.getenv("SOSO_STAKED", "0"))
    sdex_fee_engine = SoDEXFeeIntelligence(soso_staked=_soso_staked)
    volume_tracker = VolumeTracker()
    # Bootstrap from saved volume history
    sdex_fee_engine.update(
        soso_staked=_soso_staked,
        weighted_14d_volume=volume_tracker.get_14d_weighted(),
    )
    logger.info(
        "fee_intelligence_initialized",
        tier=sdex_fee_engine.current_tier(),
        weighted_14d=f"${sdex_fee_engine.weighted_14d_volume:,.0f}",
        soso_staked=_soso_staked,
    )

    if True:
        spot_client = SoDEXSpotClient(config)
        # Discover spot symbol IDs at startup (non-fatal)
        try:
            await asyncio.wait_for(spot_client.discover_spot_symbols(), timeout=8.0)
        except Exception as _se:
            logger.warning("spot_symbol_discovery_failed", error=str(_se))

        try:
            true_arb = TrueDeltaNeutralArb(
                config=config,
                perp_client=client,
                spot_client=spot_client,
                funding_radar=funding_radar,
                fee_engine=sdex_fee_engine,   # Gate 0: fee viability check
            )
            # Symbol IDs wired after NUMERIC_ACCOUNT_ID resolved below
        except Exception as _arb_ex:
            logger.warning("true_arb_init_failed", error=str(_arb_ex),
                           action="arb disabled for this session")
            true_arb = None

        try:
            vc_monitor = ValueChainMonitor()
        except Exception as _vc_ex:
            logger.warning("vc_monitor_init_failed", error=str(_vc_ex),
                           action="valuechain cascade guard disabled for this session")
            vc_monitor = None

    # Emergency Handler
    emergency = EmergencyFlatten(config, signer)

    # Latency optimizations — shared mutable state between loops
    # Init to 0.0 so execution_cleanup_loop fetches real balance on tick 1 before
    # DrawdownManager.update_balance() is called — prevents false drawdown on startup.
    _cached_balance = [0.0]  # [0] = latest perps balance; list for closure mutation
    _cached_spot_balance = [0.0]  # [0] = latest spot balance (independent from perps on SoDEX)
    _open_entry_ids: dict = {}   # symbol -> journal entry_id
    _feedback_pending: dict = {}  # entry_id -> {"symbol": ..., "coherence": ..., "tier_scores": ...}
    # Post-rejection cooldown: prevents the same symbol re-entering all 12 gates every
    # second after a SoDEX rejection (274 wasted gate cycles observed in one session).
    # Structural rejection (code:-1): 600s. Transient failure (timeout, network): 90s.
    _rejection_cooldown: dict = {}  # symbol -> float (unix ts of when cooldown expires)
    # Deferred protective orders: symbol → (bracket, attempt_count, next_retry_ts)
    # Populated when place_bracket returns partial success (entry filled, stop/TP failed).
    # Retried by execution_cleanup_loop up to 3 times with 10s back-off between attempts.
    _deferred_brackets: dict = {}
    # API circuit breaker: block new orders after N consecutive exchange rejections.
    # Resets on any successful order. Prevents runaway retries during exchange outages.
    _api_consecutive_failures: list = [0]   # [0] = count; list for closure mutation
    _api_circuit_open_until: list = [0.0]   # [0] = unix ts when circuit re-closes
    # In-flight bracket lock: prevents a second signal from opening a concurrent bracket
    # for the same symbol while the first bracket is waiting 30s for fill confirmation.
    # Without this, position_manager is empty during fill wait, so the second signal
    # passes the position_manager.count() check and places a duplicate entry.
    _pending_entry_symbols: set = set()   # symbols currently in-flight
    _last_signal_ts: dict = {}           # symbol → unix ts: dedup rapid burst duplicates

    # v1.4 Liquidation signal buffer — sliding window for cascade detection
    _liquidation_signals: list = []   # list of LiquidationSignal (timestamp gated)

    async def on_liquidation_signal(sig: LiquidationSignal) -> None:
        """
        Callback for ValueChain liquidation events.
        Cascade guard: ≥3 liquidations in 60s → block new directional trades.
        Non-cascade: log signal for Tier 6 intelligence.
        """
        nonlocal _liquidation_signals
        now = time.time()
        _liquidation_signals.append(sig)
        # Prune signals older than 120s (2× cascade window for safety)
        _liquidation_signals = [s for s in _liquidation_signals if now - s.timestamp < 120.0]

        # Feed into Tier 6 LiquidationSignalEngine (non-fatal)
        try:
            await liq_engine.process_liquidation(sig)
        except Exception as _le:
            logger.debug("liq_engine_process_failed", error=str(_le))

        if sig.cascade:
            logger.warning(
                "vc_cascade_signal",
                events_60s=sig.event_count_60s,
                direction=sig.direction,
                symbol=sig.symbol or "all",
                action="blocking_new_trades",
            )
        else:
            logger.info(
                "vc_liquidation_signal",
                direction=sig.direction,
                symbol=sig.symbol or "all",
                notional_usd=round(sig.notional_usd, 2),
                events_60s=sig.event_count_60s,
            )

    # Register VC listener
    if vc_monitor is not None:
        vc_monitor.add_listener(on_liquidation_signal)

    async def on_signal_ready(event: Event):
        """Event-driven execution handler. Uses cached balance to avoid async latency."""
        state = event.data.get("state")
        if not state:
            return

        symbol = event.symbol

        # ── Burst deduplication — same symbol processed within last 5s → skip ──
        # Candle update bursts can fire the same signal 10-18× in <2s.
        # This guard prevents duplicate bracket attempts during that window.
        _now_ts = time.time()
        if _now_ts - _last_signal_ts.get(symbol, 0) < 5.0:
            logger.debug("signal_burst_dedup", symbol=symbol)
            return
        _last_signal_ts[symbol] = _now_ts

        # ── Signal freshness gate — discard stale events from event queue backup ──
        # If the event loop backed up (e.g. during a 30s fill wait), a signal can be
        # 60s old by the time it fires. Entering on a 60-second-old signal is entering
        # at the wrong price in a moved market. Gate: 30s max signal age.
        _signal_age_ms = int(time.time() * 1000) - getattr(state, 'timestamp_ms', int(time.time() * 1000))
        if _signal_age_ms > 30_000:
            logger.debug("signal_stale_dropped", symbol=symbol, age_ms=_signal_age_ms)
            return

        # ── Rejection cooldown — skip immediately if this symbol was recently rejected ──
        _now = time.time()
        _cooldown_until = _rejection_cooldown.get(symbol, 0.0)
        if _now < _cooldown_until:
            _remaining = int(_cooldown_until - _now)
            logger.info("signal_cooldown_active",
                        symbol=symbol, remaining_s=_remaining,
                        direction=getattr(state, 'trade_direction', 'none'),
                        score=round(getattr(state, 'coherence_score', 0), 2))
            return

        # ── Open position guard — no hedging, no pyramiding before TP1 ─────────
        # SoDEX oneway mode: sending opposite-side order while a position is open
        # creates a cross which the exchange then auto-closes at a loss. Block here.
        if position_manager.count(symbol) > 0:
            logger.debug("signal_skipped_has_position", symbol=symbol)
            return

        # ── Arb position guard — prevent directional trade on an arb-locked symbol ──
        # If TrueDeltaNeutralArb has an open position on this symbol, opening a
        # directional perp trade would break the delta-neutral hedge:
        #   arb:        long spot + short perp  (delta = 0)
        #   directional: short perp             (delta = -1)
        #   combined:   long spot + 2× short perp → net short, NOT flat
        # Block the directional entry until the arb position is closed.
        if true_arb is not None:
            _arb_syms = {p.symbol for p in true_arb.get_open_positions()}
            if symbol in _arb_syms:
                logger.debug("signal_skipped_arb_active",
                             symbol=symbol,
                             action="arb position open — directional entry blocked to preserve delta-neutral hedge")
                return

        # ── In-flight bracket lock — prevent duplicate entries during fill wait ──
        # place_bracket waits up to 30s for fill confirmation. During that window
        # position_manager is still empty, so a second signal would pass the guard
        # above and fire a second concurrent bracket for the same symbol.
        if symbol in _pending_entry_symbols:
            logger.debug("signal_skipped_pending", symbol=symbol)
            return

        # ── Global concurrent position cap ──────────────────────────────────
        # Hard cap prevents overdeployment on thin accounts. On $300: 5 positions
        # at $60 margin each = $300 fully deployed. Gate before risk eval for speed.
        # Include arb positions so they consume capacity — arb uses arb_capital_pct
        # but the exchange still has the perp margin locked.
        _arb_count = len(true_arb.get_open_positions()) if true_arb else 0
        _active_count = len(position_manager.get_all()) + len(_pending_entry_symbols) + _arb_count
        _max_pos = getattr(config, 'max_concurrent_positions', 5)
        if _active_count >= _max_pos:
            logger.debug("max_concurrent_positions_reached",
                         symbol=symbol, active=_active_count, cap=_max_pos)
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

        # ── DrawdownManager halt gate — absolute block on 25%+ total or 5% daily DD ──
        if drawdown_manager is not None and not drawdown_manager.can_trade_directional():
            logger.warning("drawdown_manager_halt",
                           symbol=symbol, reason=drawdown_manager._halt_reason)
            return

        # ── Drawdown guard — scale size down during losing streaks ───────────
        _dd_mult = drawdown_guard.size_multiplier()
        if _dd_mult < 1.0:
            candidate.size = round(candidate.size * _dd_mult, 8)
            candidate.initial_margin = round(candidate.initial_margin * _dd_mult, 8)
            logger.debug("drawdown_guard_applied",
                         symbol=symbol, dd_mult=round(_dd_mult, 2),
                         size=candidate.size)

        # ── Time-of-day size multiplier (feedback v2) ─────────────────────────
        # Reduces size during UTC hours that have historically underperformed.
        # Range [0.5, 1.2]; no-op when feedback engine has <4 settled trades per bucket.
        _tod_mult = feedback.get_hour_multiplier()
        if _tod_mult != 1.0:
            candidate.size = round(candidate.size * _tod_mult, 8)
            candidate.initial_margin = round(candidate.initial_margin * _tod_mult, 8)
            logger.debug("tod_multiplier_applied",
                         symbol=symbol, tod_mult=round(_tod_mult, 3),
                         size=candidate.size)

        # ── DrawdownManager size multiplier — LAST in chain ──────────────────
        # Applied after ALL other multipliers: temporal × dd_guard × tod × dm = final_size.
        # 1.0 (normal) / 0.75 (10–20% DD) / 0.50 (20–25% DD) / 0.0 (halted — already gated above)
        _dm_mult = drawdown_manager.get_size_multiplier() if drawdown_manager else 1.0
        if _dm_mult < 1.0:
            candidate.size = round(candidate.size * _dm_mult, 8)
            candidate.initial_margin = round(candidate.initial_margin * _dm_mult, 8)
            logger.debug("drawdown_manager_size_reduced",
                         symbol=symbol, dm_mult=round(_dm_mult, 2),
                         size=candidate.size)

        # ── Minimum notional guard — SoDEX rejects sub-floor orders (code:-1) ──
        # After all multipliers (temporal, drawdown, tod, dm), guard against sizes that
        # produce notionals below exchange minimum. $10 absolute floor; skip rather
        # than burn a circuit-breaker slot on a structurally-guaranteed rejection.
        _notional = candidate.entry_price * candidate.size
        if _notional < 10.0:
            logger.warning("signal_rejected_min_notional",
                           symbol=symbol,
                           notional=round(_notional, 2),
                           price=round(candidate.entry_price, 4),
                           size=candidate.size,
                           reason="below_exchange_floor_$10")
            return

        # ── ValueChain cascade guard ──────────────────────────────────────────
        _now_vc = time.time()
        _recent_liq = [s for s in _liquidation_signals if _now_vc - s.timestamp < 60.0]
        if len(_recent_liq) >= 3:
            logger.warning("vc_cascade_trade_blocked",
                           symbol=symbol, cascade_events=len(_recent_liq))
            return

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

        # Apply per-symbol / per-regime adaptive coherence floor (feedback v2).
        # feedback.get_adjusted_threshold() resolves priority: symbol → regime → global.
        # We update config.min_coherence here so _gate_coherence() inside validate()
        # picks up the symbol-specific threshold without any other callsite changes.
        # asyncio.create_task() is cooperative — no concurrent mutation risk.
        config.min_coherence = feedback.get_adjusted_threshold(
            symbol=symbol, regime=state.regime
        )

        # Risk validation — all gates with full context
        approved, reason = await risk_engine.validate(
            candidate, balance,
            regime=_risk_regime,
            funding_rate=_funding_rate,
            current_atr=candidate.atr,
            avg_atr=_avg_atr,
            orderbook_store=orderbook_stores.get(symbol),
            drawdown_manager=drawdown_manager,
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

        # Circuit breaker: if exchange has rejected N consecutive orders, pause trading.
        # Prevents runaway order attempts during exchange outages / auth issues.
        if _api_circuit_open_until[0] > time.time():
            logger.warning("circuit_breaker_open",
                           symbol=symbol,
                           open_until=time.strftime('%H:%M:%S',
                               time.localtime(_api_circuit_open_until[0])))
            return

        # ── Account registration guard ────────────────────────────────────────
        # NUMERIC_ACCOUNT_ID=0 means the wallet is not registered on SoDEX yet.
        # Every order with accountID=0 is structurally rejected (code:-1 unknown).
        # Block here to avoid burning circuit-breaker slots on an unregisterable state.
        if NUMERIC_ACCOUNT_ID == 0:
            logger.warning("signal_skipped_account_not_registered",
                           symbol=symbol,
                           action="deposit USDC to SoDEX to register account (aid)")
            return

        # Execute bracket — non-blocking background task.
        # Running place_bracket as a task means the event bus returns immediately
        # and can dispatch signals for OTHER symbols during the 60s fill wait.
        # _pending_entry_symbols is added BEFORE task creation so that any signals
        # arriving before the first await point already see the lock.
        bracket = BracketOrder(
            candidate=candidate,
            account_id=str(NUMERIC_ACCOUNT_ID),
            symbol_id=SYMBOL_IDS.get(symbol, 0)
        )
        _pending_entry_symbols.add(symbol)

        # Capture loop-locals needed by the task (closure over mutable shared state)
        _sym = symbol
        _cand = candidate
        _state = state
        _eid = entry_id
        _brkt = bracket

        async def _bracket_task():
            try:
                result = await client.place_bracket(_brkt)

                if result.success:
                    # stop_failed_after_fill: entry is open but stop did NOT place.
                    # Set stop_price=0.0 so the reconciliation loop's "missing stop"
                    # detection fires as a backstop if deferred retry also exhausts.
                    # (Deferred retry is the primary path; reconciliation is the last resort.)
                    _stop_confirmed = not result.error or "stop_failed" not in result.error
                    position = Position(
                        symbol=_sym,
                        side=_cand.side,
                        entry_price=_cand.entry_price,
                        size=_cand.size,
                        stop_price=_cand.stop_price if _stop_confirmed else 0.0,
                        tp1_price=_cand.tp1_price,
                        tp2_price=_cand.tp2_price,
                        tp3_price=_cand.tp3_price,
                        liq_price=_cand.liq_price,
                        initial_margin=_cand.initial_margin,
                        leverage=_cand.leverage,
                        # Use current time (≈ fill-confirmed time) not signal time.
                        # Signal-time caused time-stop to fire 15-45s early on slow fills.
                        opened_at_ms=int(time.time() * 1000),
                    )
                    position.order_ids = {
                        "entry": result.entry_order_id,
                        "stop":  result.stop_order_id,
                        "tp1":   result.tp1_order_id,
                        "tp2":   result.tp2_order_id,
                        "tp3":   result.tp3_order_id,
                    }
                    position.atr = _cand.atr
                    position.initial_size = _cand.size
                    position_manager.add(position)
                    _open_entry_ids[_sym] = _eid
                    if _eid:
                        tier_scores = sig_gen._last_components.get(_sym, {})
                        feedback.record_open(
                            entry_id=_eid,
                            symbol=_sym,
                            direction=_cand.side,
                            coherence=_state.coherence_score,
                            tier_scores=tier_scores,
                            regime=getattr(_state, "regime", "neutral"),
                        )
                    alert_system.notify_trade_placed(
                        symbol=_sym,
                        side=_cand.side,
                        price=_cand.entry_price,
                        stop=_cand.stop_price,
                        size=_cand.size,
                        rr=_cand.rr_ratio
                    )
                    journal.update_outcome(entry_id=_eid, outcome="open")
                    _api_consecutive_failures[0] = 0
                    # Record perps notional for fee tier volume tracking
                    volume_tracker.record_trade(
                        perps_notional=_cand.entry_price * _cand.size,
                    )
                    if result.error:
                        _deferred_brackets[_sym] = (_brkt, 0, time.time() + 5.0)
                        logger.warning("bracket_partial",
                                       symbol=_sym, entry=_cand.entry_price,
                                       partial_error=result.error,
                                       action="stop/TP retry scheduled in 5s")
                    else:
                        logger.info("bracket_placed", symbol=_sym, entry=_cand.entry_price)
                    display.push_trade_candidate(
                        symbol=_sym,
                        direction=_cand.side,
                        score=_state.coherence_score,
                        entry=_cand.entry_price,
                        stop=_cand.stop_price,
                        tp1=_cand.tp1_price,
                        size=_cand.size,
                        leverage=_cand.leverage,
                        rr=_cand.rr_ratio,
                        status="PARTIAL" if result.error else "PLACED",
                    )

                else:
                    # Structural rejection (exchange code:-1) → 10min cooldown.
                    # Transient failure (fill timeout, network) → 90s cooldown.
                    _err = result.error or ""
                    _cooldown = 600.0 if "SoDEX error -1" in _err else 90.0
                    _rejection_cooldown[_sym] = time.time() + _cooldown
                    _api_consecutive_failures[0] += 1
                    if _api_consecutive_failures[0] >= 5:
                        _api_circuit_open_until[0] = time.time() + 60.0
                        logger.critical("circuit_breaker_tripped",
                                        consecutive_failures=_api_consecutive_failures[0],
                                        paused_s=60,
                                        action="all new bracket orders blocked for 60s")
                        _api_consecutive_failures[0] = 0
                    logger.error("bracket_failed", symbol=_sym, error=_err,
                                 score=round(_state.coherence_score, 2),
                                 direction=_cand.side,
                                 entry=_cand.entry_price,
                                 stop=_cand.stop_price,
                                 size=_cand.size,
                                 leverage=_cand.leverage,
                                 rr=round(_cand.rr_ratio, 2),
                                 cooldown_s=int(_cooldown),
                                 cooldown_until=time.strftime('%H:%M:%S',
                                     time.localtime(time.time() + _cooldown)))
                    display.push_trade_candidate(
                        symbol=_sym,
                        direction=_cand.side,
                        score=_state.coherence_score,
                        entry=_cand.entry_price,
                        stop=_cand.stop_price,
                        tp1=_cand.tp1_price,
                        size=_cand.size,
                        leverage=_cand.leverage,
                        rr=_cand.rr_ratio,
                        status="REJECTED",
                        error=_err,
                    )

            except Exception as _bex:
                _rejection_cooldown[_sym] = time.time() + 90.0
                logger.error("bracket_exception", symbol=_sym, error=str(_bex))

            finally:
                _pending_entry_symbols.discard(_sym)

        asyncio.create_task(_bracket_task())

    async def execution_cleanup_loop():
        """Handles equity updates, balance caching, position reconciliation, and feedback."""
        _balance_log_counter = 0
        _balance_poll_counter = 0
        _position_poll_counter = 0  # live position reconciliation cadence
        _feedback_sync_counter = 0  # feedback threshold/weight sync cadence
        _trail_check_counter = 0    # software trailing stop cadence (10s)
        _time_stop_counter = 0      # time stop cadence (60s)
        _trail_highs_lows: dict = {} # symbol → best mark price (high for long, low for short)

        while True:
            try:
                # Balance polling: every 5s to avoid hammering the API on each signal.
                # on_signal_ready reads _cached_balance (set here) instead of awaiting get_account_balance.
                _balance_poll_counter += 1
                if _balance_poll_counter >= 5 or _cached_balance[0] == 0.0:
                    _balance_poll_counter = 0
                    acc_id = config.sodex_account_id or config.account_id or ""
                    _cached_balance[0] = await client.get_account_balance(acc_id)
                    # Spot balance is independent from perps on SoDEX — fetch separately.
                    if spot_client is not None:
                        _cached_spot_balance[0] = await spot_client.get_spot_balance(acc_id)

                display.update_equity(_cached_balance[0])
                if _cached_spot_balance[0] > 0:
                    display.update_spot_balance(_cached_spot_balance[0])
                drawdown_guard.update_balance(_cached_balance[0])
                if _cached_balance[0] > 0 and drawdown_manager is not None:
                    drawdown_manager.update_balance(_cached_balance[0])

                # Refresh VC + true arb display panels
                if vc_monitor is not None:
                    display.update_vc_status(vc_monitor.get_status())
                if true_arb is not None:
                    display.update_true_arb_positions(true_arb.get_open_positions())

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

                # Live position reconciliation — every 30s poll exchange.
                # Detects CLOSES (tracked but gone), NEW UNTRACKED positions,
                # size mismatches, and missing stops.
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
                                # SoDEX uses NEGATIVE size for short positions — abs() required.
                                size = abs(float(pos.get("size", 0) or pos.get("qty", 0) or 0))
                                if size > 0 and sym:
                                    exchange_open[sym] = (size, pos)

                            # ── Sync position size + stop from exchange ───────────
                            # Position size can diverge if: partial TP fills, manual
                            # size changes, or startup sync captured wrong size.
                            # Stop price is synced here when: user placed stop manually
                            # on the exchange dashboard (stop_price == 0 in tracker),
                            # or stop was replaced and we lost the order_id.
                            try:
                                open_orders = await client.get_open_orders(addr)
                            except Exception:
                                open_orders = []

                            for sym, positions in list(position_manager._positions.items()):
                                if not positions:
                                    continue
                                pos = positions[0]

                                # Size sync: exchange is authoritative
                                if sym in exchange_open:
                                    ex_size = exchange_open[sym][0]
                                    if abs(ex_size - pos.size) > 0.001:
                                        logger.info("position_size_synced",
                                                    symbol=sym,
                                                    tracked=round(pos.size, 4),
                                                    exchange=round(ex_size, 4))
                                        pos.size = ex_size

                                # ── Stop missing for tracked position ────────────
                                # Fires for startup-synced positions (stop_price=0)
                                # and for any position whose stop failed to place.
                                # Conservative 1.5% stop placed immediately.
                                _rsym_id = SYMBOL_IDS.get(sym, 0)
                                if (pos.stop_price == 0.0 and
                                        _rsym_id > 0 and NUMERIC_ACCOUNT_ID > 0):
                                    _rmark = (mark_price_stores[sym].mark_price
                                              if sym in mark_price_stores else pos.entry_price)
                                    _ref_px = pos.entry_price if pos.entry_price > 0 else _rmark
                                    if _ref_px > 0:
                                        _pstop_pct = 0.015
                                        if pos.side == "long":
                                            _rp_stop = _ref_px * (1 - _pstop_pct)
                                        else:
                                            _rp_stop = _ref_px * (1 + _pstop_pct)
                                        try:
                                            _rr = await client.replace_stop_order(
                                                symbol=sym, symbol_id=_rsym_id,
                                                account_id=NUMERIC_ACCOUNT_ID,
                                                new_stop_price=_rp_stop,
                                                old_stop_order_id=None,
                                                side=pos.side, size=pos.size,
                                            )
                                            if _rr.success:
                                                pos.stop_price = _rp_stop
                                                if pos.order_ids is None:
                                                    pos.order_ids = {}
                                                pos.order_ids["stop"] = _rr.order_id
                                                logger.info("missing_stop_placed",
                                                            symbol=sym,
                                                            stop=round(_rp_stop, 4))
                                            else:
                                                logger.error("missing_stop_failed",
                                                             symbol=sym, error=_rr.error)
                                        except Exception as _re:
                                            logger.error("missing_stop_exception",
                                                         symbol=sym, error=str(_re))

                                # Stop sync: find the protective order on the exchange.
                                # For a long: stop = lowest-priced reduce-only SELL < entry.
                                # For a short: stop = highest-priced reduce-only BUY > entry.
                                # This picks up manually-placed stops AND replaces stale IDs.
                                sym_orders = [
                                    o for o in open_orders
                                    if (o.get("symbol", "") or o.get("coin", "")) == sym
                                    and (o.get("reduceOnly") or o.get("reduce_only"))
                                ]
                                if sym_orders and pos.entry_price > 0:
                                    if pos.side == "long":
                                        stop_candidates = [
                                            float(o.get("price", 0) or 0)
                                            for o in sym_orders
                                            if int(o.get("side", 0) or 0) == 2  # SELL
                                            and float(o.get("price", 0) or 0) < pos.entry_price
                                        ]
                                        if stop_candidates:
                                            ex_stop = min(stop_candidates)
                                            if abs(ex_stop - pos.stop_price) > 0.001:
                                                logger.info("stop_synced_from_exchange",
                                                            symbol=sym, old=round(pos.stop_price, 4),
                                                            new=round(ex_stop, 4))
                                                pos.stop_price = ex_stop
                                    else:  # short
                                        stop_candidates = [
                                            float(o.get("price", 0) or 0)
                                            for o in sym_orders
                                            if int(o.get("side", 0) or 0) == 1  # BUY
                                            and float(o.get("price", 0) or 0) > pos.entry_price
                                        ]
                                        if stop_candidates:
                                            ex_stop = max(stop_candidates)
                                            if abs(ex_stop - pos.stop_price) > 0.001:
                                                logger.info("stop_synced_from_exchange",
                                                            symbol=sym, old=round(pos.stop_price, 4),
                                                            new=round(ex_stop, 4))
                                                pos.stop_price = ex_stop

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
                                    drawdown_guard.record_close(pnl)
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
                                    side_raw = str(pos_data.get("side", "") or pos_data.get("direction", "") or "")
                                    if side_raw.lower() in ("long", "buy", "1"):
                                        side = "long"
                                    elif side_raw.lower() in ("short", "sell", "2"):
                                        side = "short"
                                    else:
                                        _raw_sz = str(pos_data.get("size", "0") or "0").strip()
                                        side = "short" if _raw_sz.startswith("-") else "long"
                                    entry_px = float(
                                        pos_data.get("avgEntryPrice", 0) or pos_data.get("entryPrice", 0)
                                        or pos_data.get("ep", 0) or pos_data.get("avgCost", 0) or 0
                                    )
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
                                    synced.initial_size = size  # for TP detection
                                    position_manager.add(synced)
                                    logger.warning(
                                        "untracked_position_synced",
                                        symbol=sym, side=side, size=size,
                                        entry=entry_px, leverage=lev,
                                        note="placing protective stop immediately"
                                    )

                                    # ── Immediate protective stop ──────────────
                                    # This fires whenever a position is discovered
                                    # on the exchange that ARIA has no record of:
                                    # restart while filled, fill detected after cancel,
                                    # manual trade, etc.
                                    # Conservative stop: 1.5% from entry (enough to
                                    # survive normal volatility, tight enough to protect).
                                    # asyncio.create_task = fire-and-forget, no blocking.
                                    _psym_id = SYMBOL_IDS.get(sym, 0)
                                    if _psym_id > 0 and NUMERIC_ACCOUNT_ID > 0:
                                        _pstop_pct = 0.015  # 1.5% default
                                        if synced.side == "long":
                                            _pstop = entry_px * (1 - _pstop_pct)
                                        else:
                                            _pstop = entry_px * (1 + _pstop_pct)
                                        synced.stop_price = _pstop

                                        async def _place_orphan_stop(
                                            _s=sym, _sid=_psym_id,
                                            _pos=synced, _stop=_pstop
                                        ):
                                            try:
                                                _res = await client.replace_stop_order(
                                                    symbol=_s,
                                                    symbol_id=_sid,
                                                    account_id=NUMERIC_ACCOUNT_ID,
                                                    new_stop_price=_stop,
                                                    old_stop_order_id=None,
                                                    side=_pos.side,
                                                    size=_pos.size,
                                                )
                                                if _res.success:
                                                    if _pos.order_ids is None:
                                                        _pos.order_ids = {}
                                                    _pos.order_ids["stop"] = _res.order_id
                                                    logger.info(
                                                        "orphan_stop_placed",
                                                        symbol=_s,
                                                        stop=round(_stop, 4),
                                                        order_id=_res.order_id,
                                                    )
                                                else:
                                                    logger.error(
                                                        "orphan_stop_failed",
                                                        symbol=_s,
                                                        stop=round(_stop, 4),
                                                        error=_res.error,
                                                    )
                                            except Exception as _e:
                                                logger.error(
                                                    "orphan_stop_exception",
                                                    symbol=_s, error=str(_e)
                                                )

                                        asyncio.create_task(_place_orphan_stop())

                            # ── Live TP1 / TP2 hit detection ─────────────────────
                            # SoDEX closes TP orders when price reaches them, reducing
                            # position size. Detect the size drop and ratchet the stop.
                            for sym, (exchange_size, _) in exchange_open.items():
                                positions = position_manager.get(sym)
                                if not positions:
                                    continue
                                pos = positions[0]
                                initial_sz = pos.initial_size if pos.initial_size > 0 else pos.size
                                sym_id = SYMBOL_IDS.get(sym, 0)

                                if not pos.tp1_hit and exchange_size <= initial_sz * 0.65:
                                    # TP1 hit: position reduced to ~50% or less
                                    new_stop = position_manager.mark_tp1_hit(sym, 0)
                                    pos.size = exchange_size
                                    logger.info("tp1_detected_live",
                                                symbol=sym, new_stop=new_stop,
                                                exchange_size=exchange_size)
                                    if new_stop and new_stop > 0 and sym_id > 0:
                                        old_stop_id = (pos.order_ids or {}).get("stop")
                                        try:
                                            _ts_res = await client.replace_stop_order(
                                                symbol=sym, symbol_id=sym_id,
                                                account_id=NUMERIC_ACCOUNT_ID,
                                                new_stop_price=new_stop,
                                                old_stop_order_id=old_stop_id,
                                                side=pos.side, size=pos.size,
                                            )
                                            if _ts_res.success:
                                                if pos.order_ids is None:
                                                    pos.order_ids = {}
                                                pos.order_ids["stop"] = _ts_res.order_id
                                        except Exception as _e:
                                            logger.warning("tp1_stop_ratchet_failed",
                                                           symbol=sym, error=str(_e))

                                elif pos.tp1_hit and not pos.tp2_hit and exchange_size <= initial_sz * 0.35:
                                    # TP2 hit: position reduced to ~20% or less
                                    new_stop = position_manager.mark_tp2_hit(sym, 0)
                                    pos.size = exchange_size
                                    logger.info("tp2_detected_live",
                                                symbol=sym, new_stop=new_stop,
                                                exchange_size=exchange_size)
                                    if new_stop and new_stop > 0 and sym_id > 0:
                                        old_stop_id = (pos.order_ids or {}).get("stop")
                                        try:
                                            _ts_res = await client.replace_stop_order(
                                                symbol=sym, symbol_id=sym_id,
                                                account_id=NUMERIC_ACCOUNT_ID,
                                                new_stop_price=new_stop,
                                                old_stop_order_id=old_stop_id,
                                                side=pos.side, size=pos.size,
                                            )
                                            if _ts_res.success:
                                                if pos.order_ids is None:
                                                    pos.order_ids = {}
                                                pos.order_ids["stop"] = _ts_res.order_id
                                        except Exception as _e:
                                            logger.warning("tp2_stop_ratchet_failed",
                                                           symbol=sym, error=str(_e))

                        except Exception as _pe:
                            logger.warning("position_poll_failed", error=str(_pe))

                # Deferred bracket retry — re-place stop/TP for partial-success entries.
                # Runs every tick (1s) but gates on next_retry_ts per symbol.
                for _sym, (_bkt, _attempts, _next_retry) in list(_deferred_brackets.items()):
                    if time.time() < _next_retry:
                        continue
                    # Stop if position closed before we could protect it
                    if not position_manager.get(_sym):
                        del _deferred_brackets[_sym]
                        continue
                    if _attempts >= 3:
                        del _deferred_brackets[_sym]
                        logger.error("deferred_bracket_max_retries",
                                     symbol=_sym,
                                     action="place stop manually on SoDEX dashboard")
                        continue
                    try:
                        _prot_result = await client.place_protective_orders(_bkt)
                        if _prot_result.success:
                            del _deferred_brackets[_sym]
                            logger.info("deferred_bracket_placed",
                                        symbol=_sym, attempt=_attempts + 1)
                        else:
                            _deferred_brackets[_sym] = (_bkt, _attempts + 1,
                                                         time.time() + 10.0)
                            logger.warning("deferred_bracket_retry",
                                           symbol=_sym, attempt=_attempts + 1,
                                           error=_prot_result.error)
                    except Exception as _de:
                        _deferred_brackets[_sym] = (_bkt, _attempts + 1, time.time() + 10.0)
                        logger.warning("deferred_bracket_exception",
                                       symbol=_sym, error=str(_de))

                # ── Software trailing stop — every 10s ────────────────────────
                # Ratchets stop in favorable direction as price improves.
                # Activation and distance driven by config (default 0.5×ATR each) for
                # faster engagement on the $300/30-min cycling model.
                # Never moves stop backwards (one-way ratchet).
                # Minimum update threshold: 0.3×ATR (avoids excessive API calls).
                # Best practice (freqtrade/hummingbot): place new stop FIRST, cancel
                # old AFTER — position is never unprotected during the swap.
                _trail_check_counter += 1
                if _trail_check_counter >= 10:
                    _trail_check_counter = 0
                    _trail_act_atr = getattr(config, 'trail_activation_atr', 0.5)
                    _trail_dist_atr = getattr(config, 'trail_distance_atr', 0.5)
                    for _sym, _positions in list(position_manager._positions.items()):
                        if not _positions:
                            continue
                        _pos = _positions[0]
                        if _pos.atr <= 0:
                            continue   # no ATR stored — skip (synced position without ATR)
                        _mark_store = mark_price_stores.get(_sym)
                        if not _mark_store:
                            continue
                        _mark = _mark_store.mark_price
                        if _mark <= 0:
                            continue
                        _sym_id = SYMBOL_IDS.get(_sym, 0)
                        if _sym_id == 0:
                            continue

                        if _pos.side == "long":
                            # Update high-water mark
                            _best = _trail_highs_lows.get(_sym, _pos.entry_price)
                            if _mark > _best:
                                _trail_highs_lows[_sym] = _mark
                                _best = _mark
                            # Activate after trail_activation_atr favorable move
                            if _best < _pos.entry_price + _trail_act_atr * _pos.atr:
                                continue
                            # Trail: stop = best - trail_distance_atr, never below break-even
                            _new_stop = max(_best - _trail_dist_atr * _pos.atr, _pos.entry_price)
                            # Only update if improved by ≥ 0.3×ATR
                            if _new_stop <= _pos.stop_price + 0.3 * _pos.atr:
                                continue
                        else:
                            # Update low-water mark
                            _best = _trail_highs_lows.get(_sym, _pos.entry_price)
                            if _mark < _best or _sym not in _trail_highs_lows:
                                _trail_highs_lows[_sym] = _mark
                                _best = _mark
                            if _best > _pos.entry_price - _trail_act_atr * _pos.atr:
                                continue
                            _new_stop = min(_best + _trail_dist_atr * _pos.atr, _pos.entry_price)
                            if _pos.stop_price > 0 and _new_stop >= _pos.stop_price - 0.3 * _pos.atr:
                                continue

                        _old_stop_id = (_pos.order_ids or {}).get("stop")
                        try:
                            _trail_res = await client.replace_stop_order(
                                symbol=_sym, symbol_id=_sym_id,
                                account_id=NUMERIC_ACCOUNT_ID,
                                new_stop_price=_new_stop,
                                old_stop_order_id=_old_stop_id,
                                side=_pos.side, size=_pos.size,
                            )
                            if _trail_res.success:
                                logger.info("trailing_stop_updated",
                                            symbol=_sym,
                                            old_stop=round(_pos.stop_price, 4),
                                            new_stop=round(_new_stop, 4),
                                            best_price=round(_best, 4),
                                            atr=round(_pos.atr, 4))
                                _pos.stop_price = _new_stop
                                if _pos.order_ids is None:
                                    _pos.order_ids = {}
                                _pos.order_ids["stop"] = _trail_res.order_id
                        except Exception as _te:
                            logger.warning("trailing_stop_update_failed",
                                           symbol=_sym, error=str(_te))

                # ── Time stop — every 60s ─────────────────────────────────────
                # Capital-efficiency discipline: close flat/losing positions that are
                # older than max_hold_minutes and haven't reached TP1.
                # Preserves winners (tp1_hit=True) so the trailing stop handles them.
                # Threshold: upnl < 0.3×ATR means "not meaningfully in profit" — exit.
                _time_stop_counter += 1
                if _time_stop_counter >= 60:
                    _time_stop_counter = 0
                    _max_hold_ms = getattr(config, 'max_hold_minutes', 30) * 60 * 1000
                    _now_ms = int(time.time() * 1000)
                    for _sym, _positions in list(position_manager._positions.items()):
                        if not _positions:
                            continue
                        _pos = _positions[0]
                        # Skip if TP1 already hit — trailing stop handles this position
                        if _pos.tp1_hit:
                            continue
                        # Skip if position is too new
                        _age_ms = _now_ms - _pos.opened_at_ms
                        if _age_ms < _max_hold_ms:
                            continue
                        # Check if meaningfully in profit (> 0.3×ATR gain)
                        _mark_store = mark_price_stores.get(_sym)
                        if not _mark_store:
                            continue
                        _mark = _mark_store.mark_price
                        if _mark <= 0:
                            continue
                        if _pos.side == "long":
                            _upnl = (_mark - _pos.entry_price) * _pos.size
                            _profit_threshold = 0.3 * _pos.atr * _pos.size if _pos.atr > 0 else 0
                        else:
                            _upnl = (_pos.entry_price - _mark) * _pos.size
                            _profit_threshold = 0.3 * _pos.atr * _pos.size if _pos.atr > 0 else 0
                        # Exit if not meaningfully in profit after max_hold_minutes
                        if _upnl < _profit_threshold:
                            _sym_id = SYMBOL_IDS.get(_sym, 0)
                            if _sym_id == 0:
                                logger.warning("time_stop_skipped_no_sym_id", symbol=_sym)
                                continue
                            logger.info("time_stop_triggered",
                                        symbol=_sym,
                                        age_minutes=round(_age_ms / 60000, 1),
                                        upnl=round(_upnl, 4),
                                        mark=round(_mark, 4),
                                        entry=round(_pos.entry_price, 4))
                            try:
                                _ts_close = await client.close_position_market(
                                    symbol=_sym,
                                    symbol_id=_sym_id,
                                    account_id=NUMERIC_ACCOUNT_ID,
                                    side=_pos.side,
                                    size=_pos.size,
                                )
                                if _ts_close.success:
                                    logger.info("time_stop_close_sent",
                                                symbol=_sym, order_id=_ts_close.order_id)
                            except Exception as _tse:
                                logger.warning("time_stop_close_failed",
                                               symbol=_sym, error=str(_tse))

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

    async def true_arb_loop():
        """
        True delta-neutral arb loop (Tier 7 — spot+perp funding harvest).

        Runs every 5 minutes:
          1. Fetch live funding rates via the funding radar.
          2. For each asset, check if funding rate warrants a new arb position.
          3. For open positions, check exit conditions (basis convergence, rate flip, time).
          4. Accrue funding every 8h to open positions.

        Only active in live mode with a real spot client.
        ValueChain cascade guard applied before any new position opens.
        """
        if true_arb is None or spot_client is None:
            return   # Paper mode — not applicable

        _funding_accrue_counter = 0   # 8h = 96 × 5-min ticks

        while True:
            try:
                # Get latest funding rates
                # Use spot balance for arb capital — spot and perps have INDEPENDENT
                # balances on SoDEX. Directional loop uses perps balance; arb uses spot.
                balance = _cached_spot_balance[0] if _cached_spot_balance[0] > 0 else _cached_balance[0]
                if balance <= config.balance_floor:
                    await asyncio.sleep(300)
                    continue

                real_rates = await ws_manager.fetch_funding_rates()

                # Determine cascade state from VC monitor
                cascade = vc_monitor.is_cascade_active() if vc_monitor else False

                for symbol in config.assets:
                    rate = real_rates.get(symbol, 0.0) if real_rates else 0.0
                    if rate == 0.0:
                        continue

                    # Check exits for open positions
                    if symbol in [p.symbol for p in true_arb.get_open_positions()]:
                        spot_price = await spot_client.get_spot_price(symbol)
                        perp_price = getattr(mark_price_stores.get(symbol, None),
                                             "mark_price", spot_price)
                        await true_arb.check_exits(symbol, rate, spot_price, perp_price)
                        continue

                    # Evaluate new entry — record notional for fee tier tracking on success
                    _positions_before = len(true_arb.get_open_positions())
                    await true_arb.evaluate_and_open(
                        symbol=symbol,
                        funding_rate=rate,
                        balance=balance,
                        cascade_active=cascade,
                    )
                    if len(true_arb.get_open_positions()) > _positions_before:
                        # New arb position opened — record both spot and perp notional.
                        # Spot counts 2× toward SoDEX tier weighted volume.
                        _new_pos = true_arb.get_open_positions()[-1]
                        _notional = _new_pos.spot_qty * _new_pos.spot_entry
                        volume_tracker.record_trade(
                            perps_notional=_notional,
                            spot_notional=_notional,  # spot counts 2× in weighted formula
                        )

                # Accrue funding every 8h
                _funding_accrue_counter += 1
                if _funding_accrue_counter >= 96:   # 96 × 5m = 8h
                    _funding_accrue_counter = 0
                    if real_rates:
                        for pos in true_arb.get_open_positions():
                            sym = pos.symbol
                            rate = real_rates.get(sym, 0.0)
                            notional = pos.spot_qty * pos.spot_entry
                            true_arb.accrue_funding(sym, rate, notional)

                # Update display
                display.update_true_arb_positions(true_arb.get_open_positions())

            except Exception as e:
                logger.error("true_arb_loop_error", error=str(e))

            await asyncio.sleep(300)   # 5-minute cycle

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

    async def balance_monitor_loop():
        """
        v1.6: Updates DrawdownManager with live balance every 30s.
        Also resets daily/weekly tracking at UTC midnight and Monday 00:00.
        """
        import datetime as _dt
        _last_day = _dt.datetime.now(_dt.timezone.utc).day
        _last_weekday = _dt.datetime.now(_dt.timezone.utc).weekday()

        while True:
            try:
                balance = _cached_balance[0]
                if balance > 0:
                    drawdown_manager.update_balance(balance)

                # Daily reset at UTC midnight
                now_utc = _dt.datetime.now(_dt.timezone.utc)
                if now_utc.day != _last_day:
                    drawdown_manager.reset_daily()
                    _last_day = now_utc.day
                    logger.info("drawdown_manager_daily_reset",
                                balance=round(balance, 2))

                # Weekly reset on Monday UTC midnight
                if now_utc.weekday() == 0 and _last_weekday != 0:
                    drawdown_manager.reset_weekly()
                    logger.info("drawdown_manager_weekly_reset",
                                balance=round(balance, 2))
                _last_weekday = now_utc.weekday()

                # Log status when not normal (silent when all good)
                dm_status = drawdown_manager.status()
                if dm_status.halted:
                    logger.warning(
                        "drawdown_manager_halted",
                        reason=dm_status.halt_reason,
                        total_dd=f"{dm_status.total_drawdown_pct:.1f}%",
                        balance=dm_status.current_balance,
                        low_watermark=dm_status.low_watermark,
                    )
                elif dm_status.size_multiplier < 1.0:
                    logger.info(
                        "drawdown_manager_reduced",
                        multiplier=dm_status.size_multiplier,
                        total_dd=f"{dm_status.total_drawdown_pct:.1f}%",
                    )

            except Exception as _bme:
                logger.error("balance_monitor_loop_error", error=str(_bme))
            await asyncio.sleep(30)

    async def recovery_signal_loop():
        """
        v1.6: Polls LiquidationSignalEngine every 30s for Type B recovery signals.
        Type B fires when 2min silence elapses after a cascade — confirmed exhaustion.
        """
        while True:
            try:
                await liq_engine.check_recovery_signals()
            except Exception as _rse:
                logger.error("recovery_signal_loop_error", error=str(_rse))
            await asyncio.sleep(30)

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

    async def fee_update_loop():
        """
        Refresh SoDEX fee tier data once per day at UTC midnight + 5 min.
        Also fetches live maker/taker rates from the exchange fee-rate endpoint
        so the fee engine uses authoritative rates (not just hardcoded tables).

        Rate budget: balance=5 + fee-rate=2 per call × 2 (spot+perp) = 9 weight/day.
        Negligible vs 1200/min budget.
        """
        while True:
            try:
                acc_id = config.sodex_account_id or config.account_id or ""

                # Fetch live rates from exchange (weight=2 each)
                if spot_client is not None and acc_id:
                    live_spot_rates = await spot_client.fetch_fee_rate(address=acc_id)
                    live_perp_rates = {}
                    live_perp_rates = await client.fetch_perp_fee_rate(address=acc_id)
                    sdex_fee_engine.apply_live_rates(live_spot_rates, live_perp_rates)

                # Refresh volume-based tier calculation
                weighted = volume_tracker.get_14d_weighted()
                sdex_fee_engine.update(
                    soso_staked=float(os.getenv("SOSO_STAKED", "0")),
                    weighted_14d_volume=weighted,
                )

                # Push summary to display
                display.update_fee_data(sdex_fee_engine.tier_summary())

                logger.info(
                    "fee_update_complete",
                    tier=sdex_fee_engine.current_tier(),
                    weighted_14d=f"${weighted:,.0f}",
                    perp_taker=f"{sdex_fee_engine.perps_taker_fee()*100:.4f}%",
                    spot_taker=f"{sdex_fee_engine.spot_taker_fee()*100:.4f}%",
                )
            except Exception as e:
                logger.error("fee_update_loop_error", error=str(e))

            # Sleep until next UTC midnight + 5min — volume resets at midnight
            import datetime as _dt
            now_utc = _dt.datetime.now(_dt.timezone.utc)
            next_run = (now_utc + _dt.timedelta(days=1)).replace(
                hour=0, minute=5, second=0, microsecond=0
            )
            sleep_s = (next_run - now_utc).total_seconds()
            await asyncio.sleep(sleep_s)

    async def health_server():
        """
        Lightweight health endpoint for Railway liveness checks.

        Port conflict behaviour:
          1. Try PORT env var (default 8080).
          2. If busy, try up to 10 sequential fallback ports.
          3. If all busy (e.g. running locally with many instances), log and
             return — health server is non-critical; ARIA continues trading.
        """
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

        base_port = int(os.environ.get("PORT", 8080))
        bound_port = None
        for attempt, port in enumerate(range(base_port, base_port + 10)):
            try:
                site = _aiohttp_web.TCPSite(runner, "0.0.0.0", port)
                await site.start()
                bound_port = port
                break
            except OSError as _e:
                if attempt == 0:
                    logger.warning(
                        "health_server_port_busy",
                        port=port,
                        error=str(_e),
                        action=f"trying ports {base_port+1}–{base_port+9}",
                    )
                continue

        if bound_port is None:
            logger.warning(
                "health_server_unavailable",
                tried=f"{base_port}–{base_port+9}",
                action="continuing without health endpoint — ARIA trading is unaffected",
            )
            return  # non-fatal — trading loops are unaffected

        logger.info("health_server_started", port=bound_port)
        await asyncio.Event().wait()  # run forever

    # 11. Subscribe and Start
    event_bus.subscribe(EventType.SIGNAL_READY, on_signal_ready)

    # Seed fee display with initial data from volume history
    display.update_fee_data(sdex_fee_engine.tier_summary())
    
    logger.info("Starting ARIA execution gather")
    
    # ARC v1.3 Patch Part A: Historical fetch on startup
    if hasattr(ws_manager, "fetch_historical"):
        logger.info("fetching_historical_data", source=type(ws_manager).__name__)
        await ws_manager.fetch_historical()
        logger.info("historical_complete")

    try:
        # ARC 1.3: Terminal MUST be first to takeover screen.
        # We wrap in gather for concurrent execution of loops.
        _gather_coros = [
            display.run(),            # Priority 1: Terminal UI
            event_bus.start(),        # Priority 2: Event system
            interpreter.start(),      # Priority 3: Intelligence
            ws_manager.start(),
            execution_cleanup_loop(),
            funding_loop(),
            true_arb_loop(),          # v1.4: true delta-neutral arb
            fee_update_loop(),        # v1.5: daily fee tier refresh + live rate fetch
            vault_loop(),
            calendar_loop(),
            balance_monitor_loop(),   # v1.6: DrawdownManager balance updates + resets
            recovery_signal_loop(),   # v1.6: Tier 6 recovery signal polling
            health_server(),          # Railway liveness check
        ]
        # ValueChain monitor only in live mode
        if vc_monitor is not None:
            _gather_coros.append(vc_monitor.run())

        await asyncio.gather(*_gather_coros, return_exceptions=False)
    except Exception as e:
        logger.error("system_gather_critical_failure", error=str(e))
        raise
    finally:
        # 9. Graceful shutdown — only flatten if we actually have tracked positions
        if position_manager.get_all():
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

    # ATR-based stop: configurable ATR buffer (default 0.75×ATR for tight cycling)
    atr = getattr(state, 'atr', 0.0)
    if atr <= 0:
        return None

    stop_atr_mult = getattr(cfg, 'stop_atr_mult', 0.75)
    stop_buffer = atr * stop_atr_mult
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

    # ── Fixed floor notional sizing (v1.6) ───────────────────────────────────
    # When base_trade_usd > 0, use conviction-scaled fixed notional instead of
    # Kelly (balance × risk_pct). Prevents dust trades on depleted $300 accounts.
    #   Conviction multipliers from coherence score:
    #     score < 3.0 → 1.0×  (base)
    #     score 3–5   → 1.4×  (confirmed signal)
    #     score ≥ 5.0 → 2.0×  (strong alignment)
    base_usd = getattr(cfg, 'base_trade_usd', 0.0)
    lev = min(getattr(cfg, 'default_leverage', 10),
              cfg.ASSET_CONFIG.get(state.symbol, {}).get('max_leverage', 25))

    if base_usd > 0 and entry > 0:
        coherence = getattr(state, 'coherence_score', 0.0)
        if coherence >= 5.0:
            conv_mult = 2.0
        elif coherence >= 3.0:
            conv_mult = 1.4
        else:
            conv_mult = 1.0
        target_notional = base_usd * conv_mult
        target_notional = max(target_notional, getattr(cfg, 'min_trade_usd', 15.0))
        target_notional = min(target_notional, getattr(cfg, 'max_trade_usd', 50.0))
        size = target_notional / entry
        margin = target_notional / max(lev, 1)
    else:
        # Kelly sizing: risk_pct × balance, leverage-capped
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

        # Max margin per trade cap — prevents one oversized position (e.g. BTC)
        # from consuming all capital. At $300: 20% cap = $60 max → 5 concurrent.
        _max_margin_pct = getattr(cfg, 'max_margin_per_trade_pct', 0.20)
        _max_margin = balance * _max_margin_pct
        if margin > _max_margin and _max_margin > 0:
            scale = _max_margin / margin
            size = size * scale
            margin = _max_margin
            if size * entry < cfg.min_trade_notional_usd:
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
