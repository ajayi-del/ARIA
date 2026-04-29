import asyncio
import json as _json_kingdom
import math
import os
import structlog
import signal as sys_signal
import time
import traceback as _traceback
from dotenv import load_dotenv
import logging
from pathlib import Path
from aiohttp import web as _aiohttp_web
from filelock import FileLock as _FileLock

# ── Pre-configure structlog at module level ───────────────────────────────────
# CRITICAL: Must happen before ANY module-level import that calls a logger.
# `from core.clock import daily_tracker` triggers daily_tracker.load() which
# logs — if structlog is unconfigured at that point it uses the dev-mode
# ConsoleRenderer which writes to stdout, leaking into the Rich terminal.
# This shim routes all pre-main() logging to file only; main() reconfigures
# with the full processor chain after Settings are loaded.
import logging as _log_pre
from pathlib import Path as _Path_pre
_Path_pre("logs").mkdir(exist_ok=True)
_log_pre.basicConfig(
    level=_log_pre.INFO,
    handlers=[_log_pre.FileHandler("logs/aria.log", mode="a")],
    format="%(message)s",
)
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=False,  # main() will reconfigure with full chain
)
# Silence noisy third-party loggers even during pre-init phase
for _noisy in ("websockets", "aiohttp", "asyncio"):
    _log_pre.getLogger(_noisy).setLevel(_log_pre.WARNING)
del _log_pre, _Path_pre, _noisy
# ─────────────────────────────────────────────────────────────────────────────

from core.config import Settings, SYMBOL_MIN_COHERENCE as _SYMBOL_MIN_COHERENCE
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
from execution.sodex_client import SoDEXClient, STEP_SIZES as _CLOSE_STEP_SIZES
from execution.order_manager import OrderManager
from execution.metrics import metrics_logger
from risk.margin_engine import MarginEngine
from risk.position_manager import PositionManager
from risk.risk_engine import RiskEngine

# Memory layer imports
from memory.trade_journal import TradeJournal
from memory.performance import PerformanceTracker, SessionDrawdownTracker
from memory.session_summary import SessionSummary
from execution.schemas import Position, BracketOrder

# Intelligence layer imports
from intelligence.stop_clusters import StopClusterMap
from intelligence.market_hours import MarketHoursGate
from intelligence.market_context import MarketContext

# Funding layer imports
from funding.history import FundingHistory
from funding.radar import FundingRadar
# Intelligence Expansion
from intelligence.relative_strength import RelativeStrengthEngine, ASSET_CATEGORIES
from intelligence.regime_engine import RegimeMultiplierEngine, XAUTThermometer, AutoAdjustmentEngine
from intelligence.signal_guard import SignalGuard
from intelligence.oracle_engine import OracleEngine
from risk_calendar import CalendarEngine
from risk_calendar.time_regime import evaluate as evaluate_time_regime
from intelligence.interpreter import IntelligenceInterpreter
from intelligence.feedback import SignalFeedbackEngine
from risk.correlation_engine import CorrelationEngine
from core.event_bus import event_bus, EventType, Event
from core.system_state import SystemStateManager

# Monitoring layer imports
from monitoring.alerts import AlertSystem

# Personality engine — Phase 12
from intelligence.personality import PersonalityEngine, PersonalityContextCache
from core.asset_classes import ASSET_CLASS_ATR_THRESHOLDS, get_asset_class as _get_asset_class

# v1.4 New intelligence layers
from execution.sodex_spot_client import SoDEXSpotClient
from data.valuechain_monitor import ValueChainMonitor, LiquidationSignal
from funding.arb_strategy import TrueDeltaNeutralArb
from risk.drawdown_guard import DrawdownGuard
from risk.drawdown_manager import DrawdownManager
from intelligence.liquidation_signal import LiquidationSignalEngine
from intelligence.cascade_orchestrator import CascadeOrchestrator

# v1.5 Fee Intelligence System
from core.fee_engine import SoDEXFeeEngine as SoDEXFeeIntelligence
from memory.volume_tracker import VolumeTracker

# Learning system
from memory.trade_db import TradeDatabase, TradeRecord
from memory.param_store import ParamStore
from memory.calibration_engine import CalibrationEngine

# Vault layer imports
from vault.vault_manager import VaultManager
from vault.fee_engine import FeeEngine
from vault.performance_cert import PerformanceCert
from vault.bot_fee_ledger import BotFeeLedger
from core.clock import exchange_clock, daily_tracker
from core.ecs import ecs_engine
from core.ui_state import ui_state as _ui_state
from execution.candidate_pool import CandidatePool, tag_strategy
from execution.signal_dedup import signal_deduplicator
from core.agent_winrates import AgentWinrates
from core.session_config import session_manager
from intelligence.agents import (
    MacroAgent, RegimeAgent, StructureAgent, MicroAgent, FundingAgent, SSIAgent
)
from intelligence.sovereign_signal import SovereignSignalGenerator
from memory.outcome_recorder import OutcomeRecorder

# Execution Alpha Patch imports
from intelligence.signal_tier import SignalTier, classify_signal, TIER_SIZE_MULT
from intelligence.trade_type import TradeType, tag_trade_type, TIME_STOP_SECONDS
from intelligence.dispersion_gate import DispersionGate
from risk.regime_sizing import regime_size_mult
from risk.streak_sizing import StreakTracker
from risk.coherence_decay import CoherenceDecayMonitor


# Globals for signal handler
journal = None
perf = None
session_summary = None
session_start_ms = 0


def _build_trade_record(
    position,
    exit_price: float,
    exit_reason: str,
    net_pnl: float,
) -> "TradeRecord":
    """
    Build a TradeRecord from a Position object.
    Uses getattr with safe defaults everywhere — never raises.
    Callable from any position close path.
    """
    entry = getattr(position, "entry_price", 0.0)
    size = getattr(position, "size", 0.0)
    side = getattr(position, "side", "unknown")
    opened_at_ms = getattr(position, "opened_at_ms", 0)

    if side == "long":
        dir_pnl = (exit_price - entry) * size
    else:
        dir_pnl = (entry - exit_price) * size

    hold_s = max(0.0, (time.time() - opened_at_ms / 1000)) if opened_at_ms > 0 else 0.0
    symbol = getattr(position, "symbol", "UNKNOWN")

    return TradeRecord(
        trade_id=f"{symbol}_{opened_at_ms}",
        symbol=symbol,
        side=side,
        timestamp_open_ms=opened_at_ms,
        timestamp_close_ms=int(time.time() * 1000),
        coherence_score=getattr(position, "entry_coherence", 0.0),
        tiers_fired=list(getattr(position, "tiers_fired", [])),
        htf_regime=getattr(position, "entry_htf", "unknown"),
        session_name=getattr(position, "entry_session", "unknown"),
        session_mult=getattr(position, "entry_session_mult", 1.0),
        entry_price=entry,
        exit_price=exit_price,
        notional_usd=round(entry * size, 4),
        leverage=getattr(position, "leverage", 6),
        stop_price=getattr(position, "stop_price", 0.0),
        tp1_price=getattr(position, "tp1_price", 0.0),
        atr=getattr(position, "atr", 0.0),
        hold_seconds=round(hold_s, 1),
        directional_pnl=round(dir_pnl, 6),
        net_pnl=round(net_pnl, 6),
        max_adverse_excursion=getattr(position, "max_adverse_excursion", 0.0),
        max_favourable_excursion=getattr(position, "max_favourable_excursion", 0.0),
        exit_reason=exit_reason,
    )


def _apply_calibration(cal: dict, param_store: "ParamStore") -> dict:
    """
    Apply calibration results to ParamStore with 30% blending.
    Returns dict of changes made. Logs every parameter update.
    All values clamped to safe ranges before writing.
    """
    changes: dict = {}
    BLEND = CalibrationEngine.BLEND_FACTOR if hasattr(CalibrationEngine, "BLEND_FACTOR") else 0.30

    # ── Stop multipliers ──────────────────────────────────────────────────────
    for sym, optimal in cal.get("stop_multipliers", {}).items():
        current = param_store.get_stop_mult(sym)
        blended = round(current * (1 - BLEND) + optimal * BLEND, 3)
        blended = max(1.0, min(4.0, blended))
        if abs(blended - current) > 0.05:
            param_store.set_stop_mult(sym, blended)
            changes[f"stop_{sym}"] = {"from": current, "to": blended, "target": optimal}

    # ── Coherence threshold ───────────────────────────────────────────────────
    coh_cal = cal.get("coherence_thresholds", {})
    optimal_coh = coh_cal.get("optimal_threshold")
    if optimal_coh is not None:
        current_coh = param_store.get_coherence_threshold()
        blended_coh = round(current_coh * (1 - BLEND) + optimal_coh * BLEND, 3)
        blended_coh = max(1.5, min(4.0, blended_coh))
        if abs(blended_coh - current_coh) > 0.05:
            param_store.set_coherence_threshold(blended_coh)
            changes["coherence"] = {"from": current_coh, "to": blended_coh}

    # ── Session weights ───────────────────────────────────────────────────────
    for sess, data in cal.get("session_weights", {}).items():
        recommended = data.get("recommended_mult", 1.0)
        current_sw = param_store.get_session_weight(sess)
        if abs(recommended - current_sw) > 0.05:
            param_store.set_session_weight(sess, recommended)
            changes[f"session_{sess}"] = {"from": current_sw, "to": recommended}

    if changes:
        import structlog
        _log = structlog.get_logger(__name__)
        _log.info("calibration_applied",
                  trade_count=cal.get("trade_count", 0),
                  changes=len(changes),
                  details=changes)
    return changes


async def main():
    # 0. Single-instance lock — prevent multiple ARIA processes on same machine.
    # Uses a PID file in the log directory. Stale PID (process dead) is overwritten.
    import fcntl as _fcntl
    _lock_path = Path("logs/aria.pid")
    _lock_path.parent.mkdir(parents=True, exist_ok=True)
    _lock_fh = open(_lock_path, "w")
    try:
        _fcntl.flock(_lock_fh, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except BlockingIOError:
        _existing_pid = _lock_path.read_text().strip()
        import sys as _sys_lock
        _sys_lock.stderr.write(
            f"[ARIA] Another instance is already running (PID {_existing_pid}). "
            f"Kill it first: kill {_existing_pid}\n"
        )
        return
    _lock_fh.write(str(os.getpid()))
    _lock_fh.flush()

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
    _sovereign_agent = None                                    # Sovereign portfolio agent (set later)
    _agent_wr = AgentWinrates()                                # Per-agent win/loss tracker (persistent)
    # Phase 11: signal agents and outcome recorder (wired post-store-init)
    _sig_agents: dict = {}                                     # {name: BaseAgent} — 6 signal agents
    _outcome_recorder: "OutcomeRecorder | None" = None         # per-agent outcome attributor
    
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
    # Console handler — INFO+ to stderr so it doesn't corrupt the Rich Live
    # alternate-screen buffer (which owns stdout).  Use `tail -f logs/aria.log`
    # to watch live; stderr events appear only if not in screen mode.
    import sys as _sys
    console_handler = logging.StreamHandler(_sys.stderr)
    console_handler.setLevel(logging.WARNING)  # stderr only for warnings+ — avoids noise
    # Strip stdlib prefix from structlog lines — without this, the file contains
    # double timestamps: stdlib's "2026-04-15 ... INFO ..." + structlog's ISO JSON.
    file_handler.setFormatter(logging.Formatter("%(message)s"))

    logging.getLogger("httpx").setLevel(logging.WARNING)      # suppress HTTP noise
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)  # suppress WS handshake noise
    logging.getLogger("aiohttp").setLevel(logging.WARNING)     # suppress HTTP client noise
    logging.getLogger("asyncio").setLevel(logging.WARNING)     # suppress event loop debug

    logger = structlog.get_logger(__name__)

    logging.basicConfig(level=config.log_level, handlers=[file_handler, console_handler])
    
    logger.info(f"Starting ARIA in {config.mode.upper()} mode")

    # ── Startup config validation — confirms mainnet sizing values loaded from .env ──
    logger.info(
        "config_sizing_loaded",
        base_trade_usd=config.base_trade_usd,
        min_trade_usd=config.min_trade_usd,
        max_trade_usd=config.max_trade_usd,
        min_trade_notional_usd=config.min_trade_notional_usd,
        default_leverage=config.default_leverage,
        note=(
            f"target=${config.base_trade_usd:.0f} notional "
            f"= ${config.base_trade_usd/max(config.default_leverage,1):.0f} margin at {config.default_leverage}x; "
            f"floor=${config.min_trade_notional_usd:.0f} post-multiplier"
        ),
    )

    # 3. Create data stores
    orderbook_stores = {}
    mark_price_stores = {}
    candle_buffers = {}
    trade_flow_stores = {}

    for asset in config.assets:
        orderbook_stores[asset] = OrderbookStore(symbol=asset)
        mark_price_stores[asset] = MarkPriceStore(symbol=asset)
        candle_buffers[asset] = {
            "1m":  CandleBuffer(symbol=asset, interval="1m"),
            "5m":  CandleBuffer(symbol=asset, interval="5m"),
            "15m": CandleBuffer(symbol=asset, interval="15m"),
            "4h":  CandleBuffer(symbol=asset, interval="4h", maxlen=50),
        }
        trade_flow_stores[asset] = TradeFlowStore(symbol=asset)

    # Signal-only assets: SSI spot tokens — 1m candle buffers for regime classification.
    # NEVER in orderbook_stores / mark_price_stores / trade_flow_stores (no perp).
    signal_price_stores: dict = {}
    for _sig in config.signal_assets:
        candle_buffers[_sig] = {"1m": CandleBuffer(symbol=_sig, interval="1m")}
        signal_price_stores[_sig] = {}

    # SSI spot feed — connects to wss://mainnet-gw.sodex.dev/ws/spot
    from data.ssi_spot_feed import SSISpotFeed as _SSISpotFeed
    _ssi_spot_feed = _SSISpotFeed(config, candle_buffers, signal_price_stores)

    # 4. Initialize memory layer
    global journal, perf, session_summary, session_start_ms
    journal = TradeJournal()
    journal.load()

    # Startup journal hygiene: mark orphaned approved+open entries as "abandoned".
    # These accumulate when ARIA restarts while trades are in flight — the entry was
    # logged as approved but position_manager never tracked it (no _open_entry_ids
    # entry survived the restart), so update_outcome is never called.
    # Leaving them as outcome=None distorts win rate (they count as "open" forever).
    _abandoned = 0
    for _je in journal.entries:
        if _je.get("approved") and _je.get("outcome") in (None, "open"):
            _je["outcome"] = "abandoned"
            _abandoned += 1
    if _abandoned:
        logger.info("journal_startup_cleanup", abandoned=_abandoned,
                    action="orphaned approved entries marked abandoned")
        journal.save_nonblocking()

    perf = PerformanceTracker()
    perf.restore_from_journal(log_dir=str(journal.log_dir))   # loads all-time stats
    dd_tracker = SessionDrawdownTracker()   # session drawdown / regime gate
    session_summary = SessionSummary()
    session_start_ms = int(time.time() * 1000)

    # 5. Create intelligence & risk layer
    stop_clusters = StopClusterMap()
    market_hours = MarketHoursGate()
    regime_engine       = RelativeStrengthEngine(config)
    _regime_mult_engine = RegimeMultiplierEngine()
    _xaut_thermometer   = XAUTThermometer()
    _auto_adj_engine    = AutoAdjustmentEngine()
    _signal_guard       = SignalGuard()
    _oracle_engine      = OracleEngine()  # ORACLE pre-cascade smart money detector
    from execution.execution_guardian import ExecutionGuardian
    _exec_guardian   = ExecutionGuardian()
    _dispersion_gate = DispersionGate()
    _streak_tracker  = StreakTracker()
    _coherence_decay = CoherenceDecayMonitor()
    _live_funding_rates: dict = {}       # funding_loop writes; on_signal_ready reads
    _calendar_block_active = [False]     # tracks earnings block state for post-block queue
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

    # Phase 12: Personality engine — 6-personality trading intelligence layer
    # PersonalityContextCache holds slow-changing fields updated by background loops.
    # PersonalityEngine.assess() is called on hot path in on_signal_ready (~0.1ms).
    context_cache      = PersonalityContextCache()
    personality_engine = PersonalityEngine(config)

    # ── Philosophical intelligence layers ────────────────────────────────────
    # Kant   → structure-aware threshold overrides (sits between personality and risk)
    # Nietzsche → continuous conviction-based sizing (sits after risk, before execution)
    # Conviction → aggregates signal evidence to [0,1] for Nietzsche
    from intelligence.kant_engine       import KantEngine, MarketStructure as _MarketStructure
    from intelligence.nietzsche_engine  import NietzscheEngine, WillState as _WillState
    from intelligence.conviction_engine import compute_conviction
    from intelligence.prediction_market import (
        PredictionStore, CrossAgentBetEngine, PredictionRecord, build_calibration_result
    )
    kant_engine       = KantEngine(config)
    nietzsche_engine  = NietzscheEngine(config)
    prediction_store  = PredictionStore()
    bet_engine        = CrossAgentBetEngine()
    logger.info("philosophical_layers_init",
                kant="ready", nietzsche="ready", conviction="ready",
                prediction_market="ready")

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

    # 5.4.5 Sync exchange clock — offset applied to all internal timestamps so
    # journal entries match exchange trade history exactly (prevents drift where
    # local clock is ahead/behind by 1-10s, making reconciliation off-by-one).
    await exchange_clock.sync()
    asyncio.ensure_future(exchange_clock.start_auto_sync())

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
    if _sovereign_agent is not None:
        _sovereign_agent.set_account_id(NUMERIC_ACCOUNT_ID)

    # 5.7 Resolve registered API key name (X-API-Key must be the name, not the raw address)
    try:
        await asyncio.wait_for(client.resolve_api_key_name(), timeout=8.0)
    except Exception as e:
        logger.critical("api_key_name_resolution_failed", error=str(e),
                        action="Register signing key on SoDEX dashboard before trading")
        return

    # 5.7b Fetch dynamic symbol specs (tick/step sizes) from SoDEX /perps/markets.
    # Overwrites static _TICK_STEP tables with live API values — prevents "price is
    # invalid" and "quantity is invalid" rejections when hardcoded values drift.
    try:
        await asyncio.wait_for(client.fetch_symbol_mapping(), timeout=8.0)
        logger.info("exchange_info_fetched",
                    symbols_loaded=len(client.symbol_info),
                    sample=list(client.symbol_info.keys())[:3])
    except Exception as _e:
        logger.warning("exchange_info_fetch_failed", error=str(_e),
                       note="falling_back_to_hardcoded_tick_step_tables")

    # 5.8 Set leverage for all active symbols at startup.
    # Uses min(default, per-symbol max) to avoid "leverage is invalid" rejections
    # on symbols that cap below the global default (e.g. ARB/OP max 5x, not 6x).
    # Pre-flight: skip symbols with open positions or orders — avoids API noise
    # and the "cannot update leverage with open positions/orders" errors.
    _symbols_with_positions: set = set()
    _symbols_with_orders: set = set()
    if NUMERIC_ACCOUNT_ID > 0 and address:
        try:
            _pos_snapshot = await asyncio.wait_for(
                client.get_positions(address), timeout=5.0
            )
            for _p in _pos_snapshot:
                _sym = _p.get("symbol", "") or _p.get("coin", "")
                if _sym:
                    _symbols_with_positions.add(_sym)
        except Exception as _e:
            logger.info("leverage_preflight_positions_failed", error=str(_e))
        try:
            _ord_snapshot = await asyncio.wait_for(
                client.get_open_orders(address), timeout=5.0
            )
            for _o in _ord_snapshot:
                _sym = _o.get("symbol", "")
                if _sym:
                    _symbols_with_orders.add(_sym)
        except Exception as _e:
            logger.info("leverage_preflight_orders_failed", error=str(_e))

    if NUMERIC_ACCOUNT_ID > 0:
        for sym in list(config.assets):
            sym_id = SYMBOL_IDS.get(sym, 0)
            if sym_id == 0:
                continue
            if sym in _symbols_with_positions:
                logger.info("leverage_set_skipped_open_position", symbol=sym)
                continue
            if sym in _symbols_with_orders:
                logger.info("leverage_set_skipped_open_orders", symbol=sym)
                continue
            # Use preferred_leverage if set (e.g. BTC/ETH/SOL at 10x), else default.
            # Never exceed per-symbol max_leverage cap.
            _scfg    = config.ASSET_CONFIG.get(sym, {})
            _sym_pref = _scfg.get("preferred_leverage", config.default_leverage)
            _sym_max  = _scfg.get("max_leverage", config.default_leverage)
            _sym_lev  = min(_sym_pref, _sym_max)
            try:
                ok = await asyncio.wait_for(
                    client.update_leverage(sym_id, _sym_lev, NUMERIC_ACCOUNT_ID),
                    timeout=5.0
                )
                if ok:
                    logger.info("leverage_set", symbol=sym, leverage=_sym_lev)
                else:
                    logger.info("leverage_set_skipped", symbol=sym, leverage=_sym_lev)
            except Exception as e:
                _err_str = str(e).lower()
                if "open position" in _err_str or "cannot update leverage" in _err_str:
                    logger.info("leverage_set_skipped_open_position", symbol=sym)
                else:
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
                # Assign a software TP target so the software_tp_loop can book
                # profits on this position. Exchange bracket orders are not recovered
                # across session boundaries, so we use a fixed 1.5% target.
                _sync_tp1_pct = 0.015
                _sync_tp1 = (
                    entry_px * (1 + _sync_tp1_pct) if side == "long"
                    else entry_px * (1 - _sync_tp1_pct)
                )
                synced_pos = Position(
                    symbol=sym,
                    side=side,
                    entry_price=entry_px,
                    size=size,
                    initial_size=size,    # critical: TP detection uses initial_size for 65%/35% thresholds
                    stop_price=0.0,       # not recoverable across session boundary
                    tp1_price=_sync_tp1,  # 1.5% software TP; guardian fires at market
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
                    note="protective stop queued immediately"
                )
                # Place protective stop immediately via fire-and-forget task.
                # Client is fully ready here (REST calls already succeeded above).
                # stop_price=0.0 signals "unprotected" until the task confirms.
                _startup_sym_id = SYMBOL_IDS.get(sym, 0)
                _startup_notional = entry_px * synced_pos.size
                if _startup_sym_id > 0 and NUMERIC_ACCOUNT_ID > 0 and entry_px > 0:
                    if _startup_notional < config.min_trade_notional_usd:
                        # Position notional is below SoDEX minimum — skip stop placement.
                        # This happens with NEAR/LINK dust positions left open from a
                        # prior session with different sizing. They'll be managed via
                        # the time-stop in the main loop instead.
                        logger.info(
                            "startup_stop_skipped_dust",
                            symbol=sym,
                            notional=round(_startup_notional, 4),
                            min_notional=config.min_trade_notional_usd,
                        )
                    else:
                        _startup_stop_pct = 0.015  # 1.5% conservative stop
                        if side == "long":
                            _startup_stop_px = entry_px * (1 - _startup_stop_pct)
                        else:
                            _startup_stop_px = entry_px * (1 + _startup_stop_pct)

                        async def _place_startup_stop(
                            _s=sym, _sid=_startup_sym_id,
                            _pos=synced_pos, _stop=_startup_stop_px
                        ):
                            try:
                                _res = await client.replace_stop_order(
                                    symbol=_s, symbol_id=_sid,
                                    account_id=NUMERIC_ACCOUNT_ID,
                                    new_stop_price=_stop,
                                    old_stop_order_id=None,
                                    side=_pos.side, size=_pos.size,
                                )
                                if _res.success:
                                    _pos.stop_price = _stop
                                    if _pos.order_ids is None:
                                        _pos.order_ids = {}
                                    _pos.order_ids["stop"] = _res.order_id
                                    logger.info("startup_stop_placed",
                                                symbol=_s, stop=round(_stop, 4),
                                                order_id=_res.order_id)
                                else:
                                    logger.error("startup_stop_failed",
                                                 symbol=_s, stop=round(_stop, 4),
                                                 error=_res.error)
                            except Exception as _e:
                                logger.error("startup_stop_exception",
                                             symbol=_s, error=str(_e))

                        asyncio.create_task(_place_startup_stop())
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
    
    # 8. Funding Intelligence Layer — must be initialised before BybitFeed so the feed
    # can call funding_history.add_bybit_rate() as Bybit ticker updates arrive.
    funding_history = FundingHistory()
    funding_history.load()

    # 8b. Data Feed — SoDEX primary, Bybit always alive for liquidations + funding
    # Bybit feed runs regardless: it supplies predictive liquidation lead (1–3s)
    # and cross-venue funding intelligence. SoDEX owns candles, OB, mark prices.
    bybit_feed = BybitFeed(
        config=config,
        mark_price_stores={},                # SoDEX owns mark prices
        orderbook_stores=orderbook_stores if config.data_source == "bybit" else {},
        candle_buffers=candle_buffers if config.data_source == "bybit" else {},
        trade_flow_stores=trade_flow_stores if config.data_source == "bybit" else {},
        bybit_ticker_stores=bybit_ticker_stores,  # OI + funding intelligence (always)
        funding_history=funding_history,     # v1.9: Bybit rates → cross-venue Tier 7 (always)
    )

    if config.data_source == "bybit":
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
        market_engine=None,
        calendar_engine=calendar_engine,
        journal=journal,
        perf=perf,
        system_state=system_state,
        position_manager=position_manager,
        interpreter=interpreter,
        ws_manager=ws_manager,
        dd_tracker=dd_tracker,
        bybit_ticker_stores=bybit_ticker_stores,  # Bybit OI + funding for live display
        signal_price_stores=signal_price_stores,  # SSI spot prices for regime + SLP panel
    )

    # 10. Funding Radar — uses funding_history already initialised in step 8
    funding_radar = FundingRadar(
        config=config,
        trade_flow_stores=trade_flow_stores,
        history=funding_history
    )
    # Wire FundingRadar into RegimeEngine so funding_bias is non-zero in regime output
    regime_engine.set_funding_radar(funding_radar)

    # v1.4 — True delta-neutral arb (spot+perp) + ValueChain RPC monitor
    # (spot_client, true_arb, vc_monitor, _sovereign_agent, _agent_wr declared early above)
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
    liq_engine.restore_state("logs/liq_phase_state.json")   # warm up zscore from last run
    interpreter.liq_engine = liq_engine   # Wire Tier 6 engine into interpreter
    interpreter.vc_monitor = vc_monitor   # Wire ValueChain on-chain signals into Tier 4/6 bonus

    # v1.9 Cascade Intelligence — state machine + adaptive calibrator + signal ranker
    from intelligence.cascade_tracker import CascadeTracker
    from intelligence.signal_ranker import SignalRanker
    from memory.adaptive_calibrator import AdaptiveCalibrator
    cascade_tracker = CascadeTracker(
        config=config,
        mark_price_stores=mark_price_stores,
        funding_history=funding_history,
        vpin_calculator=None,   # VPIN not exposed as separate object — cascade uses proxy
    )
    # Recover cascade phase from pre-restart state (BLOCKED/PRIMED/MOMENTUM survive restarts)
    cascade_tracker.restore_state()
    _signal_ranker = SignalRanker()
    _adaptive_calibrator = AdaptiveCalibrator(config)
    # Wire cascade tracker into risk engine (BLOCKED → hard gate; PRIMED → relaxed floor)
    risk_engine.set_cascade_tracker(cascade_tracker)
    risk_engine.set_adaptive_calibrator(_adaptive_calibrator)
    # Wire cascade tracker + calibrator into display (late-bind since display is constructed earlier)
    display._cascade_tracker = cascade_tracker
    display._adaptive_calibrator = _adaptive_calibrator
    # v2.0 CascadeOrchestrator — Special Operations Commander
    # Unifies Bybit (predictive, 1–3s lead) + ValueChain (authoritative SoDEX ground truth)
    cascade_orchestrator = CascadeOrchestrator(
        config=config,
        mark_price_stores=mark_price_stores,
        orderbook_stores=orderbook_stores,
    )
    cascade_orchestrator.start()
    # Delegate old tracker's phase queries to orchestrator so BLOCKED doesn't
    # suppress trades when orchestrator has advanced to EXPANSION/AFTERMATH.
    cascade_tracker.set_orchestrator(cascade_orchestrator)
    # Always wire Bybit liquidation feed — predictive 1–3s lead regardless of data_source
    bybit_feed.add_liquidation_listener(cascade_orchestrator.on_bybit_liquidation)
    # Latency bypass: direct callbacks avoid 50ms event-bus coalescing on cascades
    cascade_orchestrator.add_momentum_listener(
        lambda d: asyncio.create_task(_execute_cascade_momentum(d["direction"], d.get("notional_60s", 0.0)))
    )
    logger.info("cascade_orchestrator_started")
    logger.info("cascade_intelligence_initialized")
    # v1.8 OI Arb Monitor — Tier 6B Bybit OI divergence signal
    from intelligence.oi_monitor import OIArbMonitor
    interpreter.oi_monitor = OIArbMonitor(bybit_ticker_stores)  # reads already-populated ticker stores

    # Learning system — TradeDatabase + CalibrationEngine + ParamStore
    # Non-critical: if any component fails, ARIA continues trading unaffected.
    try:
        _trade_db = TradeDatabase()
        _calibration_engine = CalibrationEngine(_trade_db)
        _param_store = ParamStore(config)
        # Apply any previously calibrated parameters at startup
        _startup_cal = _calibration_engine.run()
        if _startup_cal and len(_trade_db.get_all()) >= 10:
            _startup_changes = _apply_calibration(_startup_cal, _param_store)
            if _startup_changes:
                logger.info("startup_calibration_applied",
                            trade_history=len(_trade_db.get_all()),
                            changes=len(_startup_changes))
    except Exception as _ls_ex:
        logger.warning("learning_system_init_failed", error=str(_ls_ex),
                       note="trading proceeds without learning system")
        _trade_db = None
        _calibration_engine = None
        _param_store = None

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
    # Wire portfolio tracker — includes staked MAG7 in total portfolio value.
    # READ-ONLY: does not affect trade execution, only fee tier calculation.
    from core.portfolio import PortfolioValue as _PortfolioValue
    _portfolio = _PortfolioValue()
    sdex_fee_engine.set_portfolio(_portfolio)
    _mag7_staked_usd = _portfolio.get_mag7_stake_usd()
    if _mag7_staked_usd > 0:
        logger.info("staked_balance_loaded",
                    symbol="MAG7", amount_tokens=_portfolio.get_staked_amount("MAG7"),
                    usd_estimate=round(_mag7_staked_usd, 2),
                    source="config/staked_balances.json")
    else:
        logger.warning("no_staked_balance_configured",
                       note="Check config/staked_balances.json")
    logger.info(
        "fee_intelligence_initialized",
        tier=sdex_fee_engine.current_tier(),
        weighted_14d=f"${sdex_fee_engine.weighted_14d_volume:,.0f}",
        soso_staked=_soso_staked,
    )

    # ── SOVEREIGN: Yield-funded budget + component divergence monitor ─────────
    # Architecture: StakingMonitor owns stake, YieldTracker owns budget.
    # They connect only through context_cache.update_sovereign() — no shared state.
    from core.yield_tracker import YieldTracker as _YieldTracker
    from intelligence.ssi_component_monitor import SSIComponentMonitor as _SSIComponentMonitor
    from intelligence.staking_monitor import StakingMonitor as _StakingMonitor

    _staking_monitor = _StakingMonitor(default_stake_usd=_mag7_staked_usd or float(os.getenv("SLP_VAULT_ENTRY_USD", "201.33")))
    _staking_monitor.initialise()
    _initial_yield = _staking_monitor.accrue_yield()   # seed budget from accrued yield

    # Startup seed: $50 gives SOVEREIGN a $40 working budget (80% of seed).
    # The yield_accrual_loop tops it up every 8h from real accrual.
    # Floor ensures SOVEREIGN can actually trade on day 1 without waiting weeks
    # for yield to accumulate from the MAG7 stake at 5% APY.
    _stake_for_seed = _mag7_staked_usd or float(os.getenv("SLP_VAULT_ENTRY_USD", "201.33"))
    _startup_seed = max(_initial_yield, _stake_for_seed * 0.05 / 12, 50.0)  # min $50 → $40 budget

    _yield_tracker = _YieldTracker()
    _yield_tracker.initialise(_startup_seed)

    # SLP Vault + SOSO staking monitor — all balances from env vars, never hardcoded.
    # Feeds 6-hourly yield slice into _yield_tracker (SOVEREIGN budget source).
    # "yield" is a Python keyword so importlib is required for this package name.
    import importlib as _importlib
    _slp_mod = _importlib.import_module("yield.slp_tracker")
    _SLPVaultTracker = _slp_mod.SLPVaultTracker
    _slp_tracker = _SLPVaultTracker(config, _yield_tracker)
    display._slp_tracker = _slp_tracker
    # Wire MAG7SSI price callback so SLP yield estimation uses live spot price
    _ssi_spot_feed.on_mag7ssi_price = _slp_tracker.update_mag7ssi_price

    _ssi_monitor = _SSIComponentMonitor()

    # ── Sovereign portfolio agent ──────────────────────────────────────────────
    # Manages long-term SSI index positions on a 6-hour cycle.
    # set_dependencies() wired here; set_account_id() wired after NUMERIC_ACCOUNT_ID resolves.
    from sovereign.agent import SovereignAgent as _SovereignAgent
    _sovereign_agent = _SovereignAgent(config)
    _sovereign_agent.set_dependencies(
        funding_radar=funding_radar,
        signal_price_stores=signal_price_stores,
        slp_tracker=_slp_tracker,
    )
    display._sovereign_agent = _sovereign_agent
    display._agent_wr = _agent_wr

    # ── Phase 11: Signal agents ────────────────────────────────────────────────
    _macro_agent     = MacroAgent(
        ssi_store=signal_price_stores,
        symbols=config.assets,
    )
    _regime_agent    = RegimeAgent(
        relative_strength_engine=regime_engine,
        symbols=config.assets,
    )
    _structure_agent = StructureAgent(
        candle_buffers=candle_buffers,
        symbols=config.assets,
    )
    _micro_agent     = MicroAgent(
        orderbook_stores=orderbook_stores,
        mark_price_stores=mark_price_stores,
        trade_flow_stores=trade_flow_stores,
        candle_buffers=candle_buffers,
        stop_cluster_map=stop_clusters if "stop_clusters" in dir() else None,
        symbols=config.assets,
    )
    _funding_agent   = FundingAgent(
        funding_history=funding_history if "funding_history" in dir() else None,
        funding_radar=funding_radar,
        symbols=config.assets,
    )
    _ssi_agent       = SSIAgent(
        ostium_feed=None,           # wired by ostium_loop update_cache
        binance_ref=signal_price_stores,
        mark_price_stores=mark_price_stores,
        ssi_momentum=signal_price_stores,
        symbols=config.assets,
    )
    _sig_agents = {
        "macro":     _macro_agent,
        "regime":    _regime_agent,
        "structure": _structure_agent,
        "micro":     _micro_agent,
        "funding":   _funding_agent,
        "ssi":       _ssi_agent,
    }
    _outcome_recorder = OutcomeRecorder(
        agents=list(_sig_agents.values()),
        journal=journal,
    )
    await _outcome_recorder.init()
    display._outcome_recorder = _outcome_recorder
    logger.info("phase11_signal_agents_initialized", agents=list(_sig_agents.keys()))

    logger.info(
        "sovereign_initialized",
        stake_usd=round(_staking_monitor.get_total_stake_balance(), 2),
        startup_seed_yield=round(_startup_seed, 4),
        initial_budget=round(_yield_tracker.available_budget, 4),
        components=list(_ssi_monitor.get_all_z_scores().keys()),
        note="SOVEREIGN ready to trade — campaigns funded by staking yield",
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
                fee_engine=sdex_fee_engine,       # Gate 0: fee viability check
                calendar_engine=calendar_engine,  # Gate -1: macro event block/caution
            )
            # Symbol IDs wired after NUMERIC_ACCOUNT_ID resolved below
        except Exception as _arb_ex:
            logger.warning("true_arb_init_failed", error=str(_arb_ex),
                           action="arb disabled for this session")
            true_arb = None

        try:
            vc_monitor = ValueChainMonitor(calendar_engine=calendar_engine)
            # Restore zscore history + last_block so the first poll has a meaningful
            # baseline instead of starting cold from zero.
            vc_monitor.restore_state()
            vc_monitor.add_listener(cascade_orchestrator.on_valuechain_liquidation)
        except Exception as _vc_ex:
            logger.warning("vc_monitor_init_failed", error=str(_vc_ex),
                           action="valuechain cascade guard disabled for this session")
            vc_monitor = None

    # Latency optimizations — shared mutable state between loops
    # Init to 0.0 so execution_cleanup_loop fetches real balance on tick 1 before
    # DrawdownManager.update_balance() is called — prevents false drawdown on startup.
    _cached_balance = [0.0]  # [0] = latest perps balance; list for closure mutation
    _cached_spot_balance = [0.0]  # [0] = latest spot balance (independent from perps on SoDEX)
    _open_entry_ids: dict = {}   # symbol -> journal entry_id
    _feedback_pending: dict = {}  # entry_id -> {"symbol": ..., "coherence": ..., "tier_scores": ...}

    # ── Candidate pool — single source for signal selection ──────────────────
    # Signals pass all gates, enter pool with strategy tag + score.
    # Selection loop picks top-N by score respecting position cap.
    # Eviction: candidates older than 30s are discarded — stale signals are noise.
    _candidate_pool = CandidatePool(max_age_s=30.0, max_slots=len(config.assets))
    # Post-rejection cooldown: prevents the same symbol re-entering all 12 gates every
    # second after a SoDEX rejection (274 wasted gate cycles observed in one session).
    # Structural rejection (code:-1): 600s. Transient failure (timeout, network): 90s.
    _rejection_cooldown: dict = {}  # symbol -> float (unix ts of when cooldown expires)
    # API circuit breaker: block new orders after N consecutive exchange rejections.
    # Resets on any successful order. Prevents runaway retries during exchange outages.
    _api_consecutive_failures: list = [0]   # [0] = count; list for closure mutation
    _api_circuit_open_until: list = [0.0]   # [0] = unix ts when circuit re-closes
    # In-flight bracket lock: prevents a second signal from opening a concurrent bracket
    # for the same symbol while the first bracket is waiting 30s for fill confirmation.
    # Without this, position_manager is empty during fill wait, so the second signal
    # passes the position_manager.count() check and places a duplicate entry.
    _pending_entry_symbols: set = set()   # symbols currently in-flight
    # Close-failure circuit breaker: after 3 consecutive rejected close orders for the
    # same symbol, back off for 30s before retrying. Prevents runaway order spam when
    # the exchange rejects with "quantity is invalid" or similar permanent errors.
    # Format: symbol → {"count": int, "backoff_until": float, "last_err": str}
    _stop_close_fails: dict = {}

    # Dust-purge blocklist: after dust_position_purged the exchange still holds the
    # position (the close order was rejected). Block reconciliation from re-adding it
    # for 120s — breaks the purge→resync→stop→purge infinite loss loop.
    # Format: symbol → float (expiry unix timestamp)
    # Cleared when reconciliation confirms the exchange position is gone.
    _dust_purge_blocklist: dict = {}

    # Order deduplication cooldown: prevents re-entry on the same symbol within 60s
    # of the last order. Eliminates 1-second trade clusters where signal fires on
    # every tick (5 ticks/s = 5 orders) and 4th order closes 3rd via position limit.
    _order_cooldown: dict = {}  # symbol -> float (unix ts when cooldown expires)
    _last_signal_ts:  dict = {}   # symbol → unix ts: dedup rapid burst duplicates
    _last_signal_coh: dict = {}   # symbol → float: coherence of last processed signal (best-signal-wins)

    # ── Global kill switch ────────────────────────────────────────────────────
    # Set _trading_halted = True to immediately block all new order placements.
    # Triggered automatically by: drawdown auto-halt, rapid-loss circuit breaker.
    # Reset requires manual intervention (restart or API call) — intentional.
    # Rapid-loss circuit: if account loses ≥3% in any rolling 30-min window,
    # halt all new trades for the remainder of the session.
    _trading_halted: list = [False]    # [0] = bool; list for closure mutation
    _session_loss_window: list = []    # [(unix_ts, pnl_usd), ...] — rolling 30-min trades
    # Quiet market tracker: unix ts of last observed events_60s >= 40.
    # Initialised to now so the filter doesn't block on fresh start before vc_monitor
    # has had a chance to report any liquidations.
    _last_active_market_ts: list = [time.time()]  # [0] = float; list for closure mutation

    # v2.0 MarketContext — built once per signal tick, frozen, passed to all components
    # Initialised to None; built in on_signal_ready() before risk validation.
    _last_market_context = None
    # Latest calendar state — cached to avoid async lookup inside sync MarketContext.build()
    _last_calendar_state = None

    # v1.4 Liquidation signal buffer — sliding window for cascade detection
    _liquidation_signals: list = []   # list of LiquidationSignal (timestamp gated)

    # v2.1 Cascade dedup gate — prevents 30× re-processing of same batch
    # Cascade is NEVER a block; it feeds coherence scoring via liq_engine (Tier 6).
    _cascade_block_active: bool = False     # dedup only: one activation per 90s
    _cascade_block_expires_ms: int = 0
    _last_cascade_direction: str = "none"
    # Aftermath primed state — set 90s after cascade if recovery signals confirm
    _aftermath_primed: bool = False
    _aftermath_direction: str = "none"
    _aftermath_expires_ms: int = 0

    async def on_liquidation_signal(sig: LiquidationSignal) -> None:
        """
        Callback for ValueChain liquidation events.

        v1.9 Cascade architecture:
          - cascade=True  → delegate to CascadeTracker.on_liquidation_batch()
            (state machine handles dedup, BLOCKED/PRIMED/MOMENTUM transitions)
          - cascade=False → feed Tier 6 LiquidationSignalEngine for coherence score
        """
        nonlocal _liquidation_signals
        now = time.time()
        _liquidation_signals.append(sig)
        # Prune signals older than 120s (2× cascade window for safety)
        _liquidation_signals = [s for s in _liquidation_signals if now - s.timestamp < 120.0]

        # Feed into Tier 6 LiquidationSignalEngine (non-fatal)
        try:
            _liq_sym = getattr(sig, "symbol", "") or ""
            _bybit_t = bybit_ticker_stores.get(_liq_sym, {})
            _bybit_p = float(_bybit_t.get("mark_price", 0.0) or _bybit_t.get("last_price", 0.0))
            _sodex_store = mark_price_stores.get(_liq_sym)
            _sodex_p = float(
                getattr(_sodex_store, "latest_mark", None) or getattr(_sodex_store, "_mark", 0.0)
            ) if _sodex_store else 0.0
            await liq_engine.process_liquidation(sig, bybit_price=_bybit_p, sodex_price=_sodex_p)
        except Exception as _le:
            logger.debug("liq_engine_process_failed", error=str(_le))

        if sig.cascade:
            nonlocal _cascade_block_active, _cascade_block_expires_ms, _last_cascade_direction
            now_ms = int(time.time() * 1000)

            # Dedup gate — one activation per 90s, ignore re-triggers in window
            if _cascade_block_active or now_ms < _cascade_block_expires_ms:
                logger.debug("cascade_dedup_cooldown",
                             remaining_ms=max(0, _cascade_block_expires_ms - now_ms))
                return

            _cascade_block_active = True
            _cascade_block_expires_ms = now_ms + 90_000
            _last_cascade_direction = sig.direction

            events_60s = sig.event_count_60s
            is_extreme = events_60s > 50

            # Cascade = coherence intelligence, not a block.
            # Extreme cascade → higher tier6 score (size_factor 1.5 via cascade=True flag).
            # liq_engine.process_liquidation() already called above for all sigs.
            logger.warning("cascade_detected",
                           direction=sig.direction,
                           events_60s=events_60s,
                           notional_usd=round(sig.notional_usd, 0),
                           extreme=is_extreme)

            # CascadeTracker updated for intelligence/display/MarketContext
            try:
                cascade_tracker.on_liquidation_batch(
                    events_in_window=events_60s,
                    total_notional=sig.notional_usd,
                    direction=sig.direction,
                    symbol=sig.symbol or "",
                    zscore=sig.zscore,
                )
            except Exception as _ct_ex:
                logger.debug("cascade_tracker_error", error=str(_ct_ex))

            # Schedule dedup release + state clear + aftermath evaluation
            asyncio.create_task(_release_cascade_block(90))
        else:
            # Institutional threshold: $60k+ liquidations represent meaningful
            # cascade signals. Sub-$60k events are retail noise — they do not move
            # perp markets and logging them creates spike spam with no signal value.
            if sig.notional_usd >= 60_000:
                logger.info(
                    "vc_liquidation_signal",
                    direction=sig.direction,
                    symbol=sig.symbol or "all",
                    notional_usd=round(sig.notional_usd, 0),
                    events_60s=sig.event_count_60s,
                )

    async def _release_cascade_block(seconds: int) -> None:
        """Release cascade dedup gate and trigger aftermath evaluation."""
        nonlocal _cascade_block_active
        await asyncio.sleep(seconds)
        _cascade_block_active = False
        logger.info("cascade_dedup_released",
                    direction=_last_cascade_direction,
                    action="evaluating_aftermath")
        asyncio.create_task(_evaluate_cascade_aftermath())

    async def _evaluate_cascade_aftermath() -> None:
        """
        Called 90s after cascade block activates.
        Requires 3 of 4 recovery signals to confirm PRIMED state.
        PRIMED opens a 5-minute aftermath trade window.
        """
        nonlocal _aftermath_primed, _aftermath_direction, _aftermath_expires_ms
        confirmed = 0

        # Signal 1: VPIN recovering (proxy via OB imbalance < 0.3 for BTC/ETH)
        try:
            for sym in ["BTC-USD", "ETH-USD", "SOL-USD"]:
                store = orderbook_stores.get(sym)
                if store and abs(store.imbalance()) < 0.3 and store.age_ms() < 2000:
                    confirmed += 1
                    break
        except Exception:
            pass

        # Signal 2: Funding rates normalizing (< 0.0003 for ≥2 assets)
        try:
            normalising = 0
            for sym in config.assets[:4]:
                rate = funding_history.get_latest_bybit_rate(sym)
                if rate is not None and abs(rate) < 0.0003:
                    normalising += 1
            if normalising >= 2:
                confirmed += 1
        except Exception:
            pass

        # Signal 3: Mark prices healthy (fresh within 500ms for ≥3 of top 4)
        try:
            healthy = sum(
                1 for sym in config.assets[:4]
                if mark_price_stores.get(sym) and mark_price_stores[sym].is_healthy(500)
            )
            if healthy >= 3:
                confirmed += 1
        except Exception:
            pass

        # Signal 4: No new cascade events in last 60s (silence = exhaustion confirmed)
        try:
            if vc_monitor and not vc_monitor.is_cascade_active():
                confirmed += 1
        except Exception:
            pass

        logger.info("cascade_aftermath_signals",
                    confirmed=confirmed,
                    needed=2,
                    cascade_direction=_last_cascade_direction)

        if confirmed >= 2:
            primed_direction = "long" if _last_cascade_direction == "bearish" else "short"
            _aftermath_primed = True
            _aftermath_direction = primed_direction
            _aftermath_expires_ms = int(time.time() * 1000) + 300_000  # 5 min
            logger.info("cascade_aftermath_primed",
                        direction=primed_direction,
                        confirmed_signals=confirmed,
                        window_seconds=300)
        else:
            logger.info("cascade_aftermath_no_trade",
                        confirmed=confirmed,
                        reason="insufficient_signals")

    # Register VC listener
    if vc_monitor is not None:
        vc_monitor.add_listener(on_liquidation_signal)

    # ── Kingdom publisher ────────────────────────────────────────────────────────
    _KINGDOM_PATH = Path(
        os.environ.get("KINGDOM_STATE_PATH", os.path.expanduser("~/kingdom/kingdom_state.json"))
    )
    _KINGDOM_LOCK = _FileLock(str(_KINGDOM_PATH.with_suffix(".lock")), timeout=3)

    async def _write_aria_bet_to_kingdom(
        symbol: str,
        direction: str,
        coherence: float,
        confidence: float,
        cascade_phase: str,
        funding_rate: float,
    ) -> None:
        """Publish ARIA signal intent to kingdom_state.json for AUGUR to read."""
        try:
            bet = {
                "agent_id": "aria",
                "symbol": symbol,
                "direction": direction,
                "confidence": confidence,
                "evidence_type": "microstructure",
                "coherence": coherence,
                "cascade_phase": cascade_phase,
                "funding_rate": funding_rate,
                "timestamp_ms": int(time.time() * 1000),
                "expires_ms": int(time.time() * 1000) + 300_000,
            }
            _KINGDOM_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _KINGDOM_LOCK:
                try:
                    with open(_KINGDOM_PATH) as _f:
                        _state = _json_kingdom.load(_f)
                except Exception:
                    _state = {"aria": {}, "augur": {}}

                if "aria" not in _state:
                    _state["aria"] = {}

                _now_ms = int(time.time() * 1000)
                _existing = _state["aria"].get("active_bets", [])
                # Purge expired + deduplicate symbol
                _active = [b for b in _existing
                           if b.get("expires_ms", 0) > _now_ms
                           and b.get("symbol") != symbol]
                _active.append(bet)
                _state["aria"]["active_bets"] = _active

                # Snapshot regime / pnl / drawdown from live state
                _aria_regime = getattr(context_cache, "_regime", "unknown") if context_cache else "unknown"
                _state["aria"]["regime"]     = _aria_regime
                _state["aria"]["daily_pnl"]  = float(_cached_balance[0] - config.paper_starting_balance if config.mode == "paper" else 0.0)
                _state["aria"]["drawdown"]   = float(drawdown_manager.status().total_drawdown_pct if drawdown_manager else 0.0)

                _tmp = _KINGDOM_PATH.with_suffix(".tmp")
                with open(_tmp, "w") as _f:
                    _json_kingdom.dump(_state, _f, indent=2)
                _tmp.replace(_KINGDOM_PATH)

            logger.info("aria_bet_published_to_kingdom",
                        symbol=symbol, direction=direction, coherence=coherence)
        except Exception as _ke:
            logger.warning("kingdom_write_failed", error=str(_ke))

    def _read_bybit_cascade_delta(symbol: str, direction: str) -> float:
        """
        Read AUGUR's Bybit cascade intelligence from kingdom.
        Returns a coherence modifier: +0.5 (confirms), -0.3 (conflicts), 0.0 (absent).

        Bybit is 10x larger than SoDEX. When Bybit cascades first,
        SoDEX follows within 200–800ms. AUGUR writes the Bybit cascade
        state so ARIA can calibrate its confidence accordingly.
        """
        try:
            with _KINGDOM_LOCK:
                if not _KINGDOM_PATH.exists():
                    return 0.0
                with open(_KINGDOM_PATH) as _f:
                    _ks = _json_kingdom.load(_f)
            bybit_sig = _ks.get("augur_data", {}).get(f"bybit_cascade.{symbol}")
            if not bybit_sig or not bybit_sig.get("active"):
                return 0.0
            bybit_dir = bybit_sig.get("direction", "")
            # bybit_dir: "bullish" → long confirms, "bearish" → short confirms
            aria_is_long  = direction == "long"
            bybit_bullish = bybit_dir == "bullish"
            if aria_is_long == bybit_bullish:
                logger.info("bybit_cascade_confirms_aria",
                            symbol=symbol,
                            bybit_zscore=bybit_sig.get("zscore", 0),
                            coherence_boost=0.5)
                return 0.5
            else:
                logger.info("bybit_cascade_conflicts_aria",
                            symbol=symbol,
                            bybit_zscore=bybit_sig.get("zscore", 0),
                            coherence_penalty=-0.3)
                return -0.3
        except Exception:
            return 0.0

    def _read_augur_whisper(symbol: str, direction: str) -> tuple:
        """
        Read AUGUR's whisper for a symbol — tier-classified Bybit cascade lead.
        Returns (coherence_boost, tier). Both 0 if no valid / matching whisper.

        Tier 1 (zscore>3.5, $500k+, expansion): +1.5 boost — act immediately
        Tier 2 (zscore≥2.5, $200k+):           +0.8 boost — act with confirmation
        Tier 3 (zscore≥1.5):                   +0.3 boost — monitor

        Boost only applies when whisper direction matches ARIA's signal direction.
        Expired whispers (>90s) are silently ignored — stale intelligence is noise.
        """
        _WHISPER_BOOST = {1: 1.5, 2: 0.8, 3: 0.3}
        try:
            with _KINGDOM_LOCK:
                if not _KINGDOM_PATH.exists():
                    return 0.0, 0
                with open(_KINGDOM_PATH) as _f:
                    _ks = _json_kingdom.load(_f)
            whisper = _ks.get("augur_data", {}).get(f"whisper.{symbol}")
            if not whisper:
                return 0.0, 0
            now_ms = int(time.time() * 1000)
            if whisper.get("expires_ms", 0) < now_ms:
                return 0.0, 0
            whisper_dir = whisper.get("direction", "mixed")
            if whisper_dir == "mixed":
                return 0.0, 0
            # Match directions: AUGUR says "bullish"/"bearish", ARIA uses "long"/"short"
            aria_long      = direction == "long"
            whisper_bullish = whisper_dir == "bullish"
            if aria_long != whisper_bullish:
                return 0.0, 0   # direction mismatch — whisper doesn't help this signal
            tier  = whisper.get("tier", 0)
            boost = _WHISPER_BOOST.get(tier, 0.0)
            return boost, tier
        except Exception:
            return 0.0, 0

    def _write_aria_whisper(
        symbol: str,
        direction: str,
        coherence: float,
        entry_price: float,
        cascade_zscore: float,
        personality: str,
    ) -> None:
        """
        Patch 3 — ARIA publishes execution whisper to kingdom after confirmed fill.
        AUGUR reads this within 300s to boost alignment scoring on the same symbol.
        Written to kingdom["aria_whisper"] (global key — one active ARIA whisper at a time).
        """
        try:
            now_ms = int(time.time() * 1000)
            whisper = {
                "symbol":         symbol,
                "direction":      direction,
                "coherence":      round(coherence, 3),
                "entry_price":    round(entry_price, 6),
                "cascade_zscore": round(cascade_zscore, 3),
                "personality":    personality,
                "from_agent":     "aria",
                "expires_ms":     now_ms + 300_000,
                "timestamp_ms":   now_ms,
            }
            _KINGDOM_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _KINGDOM_LOCK:
                try:
                    with open(_KINGDOM_PATH) as _f:
                        _ks = _json_kingdom.load(_f)
                except Exception:
                    _ks = {}
                _ks["aria_whisper"] = whisper
                _ks["version"] = "2.0"
                _tmp = _KINGDOM_PATH.with_suffix(".tmp")
                with open(_tmp, "w") as _f:
                    _json_kingdom.dump(_ks, _f, indent=2)
                _tmp.replace(_KINGDOM_PATH)
            logger.info("aria_whisper_published",
                        symbol=symbol, direction=direction,
                        coherence=round(coherence, 3),
                        cascade_zscore=round(cascade_zscore, 3),
                        personality=personality,
                        expires_in_s=300)
        except Exception as _we:
            logger.warning("aria_whisper_write_failed", error=str(_we))

    async def _execute_cascade_momentum(direction: str, notional_usd: float) -> None:
        """
        Spartan fast path for MOMENTUM cascade execution.
        Bypasses the interpreter entirely — liquidations are exogenous shocks,
        not organic signals. Market-order entry, tight stop, hard expiry.

        Emperor (Chancellor) still governs: daily loss limit, balance floor,
        max concurrent positions. Commander (this coroutine) executes without
        debate — the liquidation is the debate.
        """
        try:
            # ── Chancellor gate ── drawdown / balance / concurrent cap
            if _trading_halted[0]:
                logger.info("cascade_momentum_halted", reason="trading_halted")
                return
            _dd_pct = dd_tracker.session_drawdown_pct
            if _dd_pct >= 10.0:
                logger.warning("cascade_momentum_halted", reason="drawdown_10pct")
                return
            if len(position_manager.get_all()) >= config.max_concurrent_positions:
                logger.info("cascade_momentum_halted", reason="max_positions")
                return

            # ── Symbol selection ── cascades are market-wide: prefer BTC → ETH → SOL
            def _is_warmed_and_liquid(s: str) -> bool:
                _st = mark_price_stores.get(s)
                if not _st:
                    return False
                _mk = float(getattr(_st, 'mark_price', None) or 0.0)
                if _mk <= 0:
                    return False
                # Non-crypto assets need market-hours warmup before cascade trading
                if s not in ("BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "LINK-USD",
                             "AVAX-USD", "OP-USD", "ARB-USD", "SUI-USD", "NEAR-USD",
                             "MNT-USD", "1000PEPE-USD", "XRP-USD", "TRUMP-USD", "BASED-USD"):
                    if market_hours and not market_hours.is_open(s):
                        return False
                return True

            _sym_candidates = [s for s in ("BTC-USD", "ETH-USD", "SOL-USD") if _is_warmed_and_liquid(s)]
            if not _sym_candidates:
                _sym_candidates = [s for s in config.assets if _is_warmed_and_liquid(s)]
            if not _sym_candidates:
                logger.warning("cascade_momentum_no_symbol", direction=direction)
                return
            symbol = _sym_candidates[0]

            # ── Price / ATR fetch ──
            _store = mark_price_stores.get(symbol)
            _mark = float(getattr(_store, 'mark_price', None) or 0.0)
            if _mark <= 0:
                logger.warning("cascade_momentum_no_mark", symbol=symbol)
                return

            # ATR: interpreter cache → candle buffer → 1% fallback
            _atr = 0.0
            if interpreter is not None:
                _atr = getattr(interpreter, '_atr_cache', {}).get(symbol, 0.0)
            if _atr <= 0:
                _buf = candle_buffers.get(symbol, {}).get("1m")
                if _buf and _buf.is_ready(14):
                    _candles = _buf.latest(14)
                    if len(_candles) >= 2:
                        _trs = []
                        for i in range(1, len(_candles)):
                            c, p = _candles[i], _candles[i-1]
                            _trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
                        _atr = sum(_trs) / len(_trs)
            if _atr <= 0:
                _atr = _mark * 0.01  # 1% fallback for cascade stops
                logger.info("cascade_atr_fallback_used", symbol=symbol, atr=round(_atr, 4))

            # ── Balance check ──
            balance = _cached_balance[0]
            if balance <= 0:
                logger.warning("cascade_momentum_no_balance")
                return

            # ── Build candidate with cascade_phase="momentum" ──
            from intelligence.market_state import MarketState
            _state = MarketState(
                symbol=symbol,
                timestamp_ms=int(time.time() * 1000),
                mark_price=_mark,
                macro_bias="neutral", macro_source="cascade", macro_confidence=1.0,
                regime="risk_on" if direction == "long" else "risk_off",
                leading_asset=symbol, lagging_asset="",
                market_type="expansion",
                atr=_atr, atr_vs_baseline=1.0,
                sweep="none", sweep_price=0.0, reclaim=False,
                imbalance=0.0, vpin=0.0, vpin_hot=False, absorption=False,
                divergence_signal="none", mark_local_spread_pct=0.0,
                funding_class="neutral", oi_signal="NEUTRAL", oi_strength=0.0,
                mag_active=False, mag_direction="none", mag_lag_remaining_min=0,
                market_hours_gate=True,
                weighted_score=8.0, raw_score=6, coherence_score=8.0,
                size_multiplier=1.0,
                trade_direction=direction,
            )
            candidate = build_candidate(
                _state, balance, margin_engine, config=config,
                param_store=_param_store, cascade_phase="momentum",
            )
            if not candidate:
                logger.warning("cascade_momentum_candidate_failed", symbol=symbol)
                return

            # ── Override size: 1.0×–1.5× base depending on cascade notional ──
            _size_mult = 1.0
            if notional_usd >= 200_000:
                _size_mult = 1.5
            elif notional_usd >= 50_000:
                _size_mult = 1.3
            elif notional_usd >= 10_000:
                _size_mult = 1.1
            candidate.size = round(candidate.size * _size_mult, 8)
            candidate.initial_margin = round(
                candidate.size / getattr(candidate, 'leverage', config.default_leverage), 8
            )
            # Hard cap: never risk more than 3% of balance on one cascade
            _max_risk = balance * 0.03
            _risk = candidate.size * abs(candidate.entry_price - candidate.stop_price)
            if _risk > _max_risk:
                _step = config.ASSET_CONFIG.get(symbol, {}).get('tick_size', 0.01)
                _new_size = math.floor((_max_risk / abs(candidate.entry_price - candidate.stop_price)) / _step) * _step
                candidate.size = max(_new_size, _step)
                candidate.initial_margin = candidate.size / getattr(candidate, 'leverage', config.default_leverage)

            # ── Symbol ID resolve ──
            _sym_id = SYMBOL_IDS.get(symbol, 0)
            if not _sym_id:
                logger.warning("cascade_momentum_no_symbol_id", symbol=symbol)
                return

            # ── Tria Bridge outbox: emit cascade signal ───────────────────────────
            try:
                _tria_signal = {
                    "id": f"{symbol}_cascade_{int(time.time() * 1000)}",
                    "symbol": symbol,
                    "direction": direction,
                    "size": round(candidate.size, 8),
                    "leverage": getattr(candidate, "leverage", config.default_leverage),
                    "entry_price": round(candidate.entry_price, 4),
                    "stop_price": round(candidate.stop_price, 4) if candidate.stop_price else None,
                    "tp1_price": round(candidate.tp1_price, 4) if candidate.tp1_price else None,
                    "tp2_price": round(candidate.tp2_price, 4) if candidate.tp2_price else None,
                    "tp3_price": round(candidate.tp3_price, 4) if candidate.tp3_price else None,
                    "coherence_score": 9.0,  # cascade momentum = highest conviction
                    "notional_usd": round(candidate.entry_price * candidate.size, 2),
                    "timestamp": time.time(),
                    "source": "cascade_momentum",
                }
                _tria_outbox_path = os.path.join(os.path.dirname(__file__), "signals", "aria_outbox.json")
                os.makedirs(os.path.dirname(_tria_outbox_path), exist_ok=True)
                _existing: list = []
                if os.path.exists(_tria_outbox_path):
                    try:
                        with open(_tria_outbox_path, "r", encoding="utf-8") as f:
                            _existing = _json_kingdom.load(f)
                        if not isinstance(_existing, list):
                            _existing = []
                    except (_json_kingdom.JSONDecodeError, OSError):
                        _existing = []
                _existing.append(_tria_signal)
                _existing = _existing[-200:]
                _tria_outbox_tmp = _tria_outbox_path + ".tmp"
                with open(_tria_outbox_tmp, "w", encoding="utf-8") as f:
                    _json_kingdom.dump(_existing, f)
                os.replace(_tria_outbox_tmp, _tria_outbox_path)
                logger.debug("tria_outbox_emitted_cascade", symbol=symbol, path=_tria_outbox_path)
            except Exception as _tria_emit_err:
                logger.warning("tria_outbox_emit_failed_cascade", error=str(_tria_emit_err))
            # ── End Tria Bridge outbox ─────────────────────────────────────────────

            # ── Market-order bracket ── entry + TP1/TP2/TP3 in one flow
            # place_bracket handles market entry (IOC), fill confirmation, then TPs.
            candidate.order_type = "market"
            logger.info("cascade_momentum_executing",
                        symbol=symbol, direction=direction,
                        size=candidate.size, entry=candidate.entry_price,
                        stop=candidate.stop_price, notional=round(candidate.size * candidate.entry_price, 2))
            from execution.schemas import BracketOrder
            _brkt = BracketOrder(
                candidate=candidate,
                account_id=str(NUMERIC_ACCOUNT_ID),
                symbol_id=_sym_id,
            )
            _bracket_result = await client.place_bracket(_brkt)
            if not _bracket_result.success:
                _bracket_err = _bracket_result.error or "unknown"
                logger.error("cascade_momentum_bracket_failed",
                             symbol=symbol, error=_bracket_err)
                if alert_system:
                    asyncio.create_task(alert_system.send(
                        f"Cascade MOMENTUM bracket failed on {symbol}: {_bracket_err}", level="WARNING"
                    ))
                return  # entry failed or fill timeout — no position to track

            # ── Track position ──
            from execution.schemas import Position
            _lev = getattr(candidate, 'leverage', config.default_leverage)
            _pos = Position(
                symbol=symbol,
                side=direction,
                size=candidate.size,
                initial_size=candidate.size,
                entry_price=candidate.entry_price,
                stop_price=candidate.stop_price,
                tp1_price=candidate.tp1_price,
                tp2_price=candidate.tp2_price,
                tp3_price=candidate.tp3_price,
                liq_price=getattr(candidate, 'liq_price', 0.0),
                initial_margin=candidate.entry_price * candidate.size / max(_lev, 1),
                leverage=_lev,
                opened_at_ms=int(time.time() * 1000),
                order_ids={"entry": _bracket_result.entry_order_id},
            )
            position_manager.add(_pos)

            # ── Alert ──
            if alert_system:
                asyncio.create_task(alert_system.send(
                    f"⚔️ *CASCADE MOMENTUM*\n{direction.upper()} {symbol}\n"
                    f"Entry: {candidate.entry_price:.2f}\n"
                    f"Stop: {candidate.stop_price:.2f}\n"
                    f"Size: {candidate.size:.6f}\n"
                    f"Notional: ${candidate.size * candidate.entry_price:.2f}",
                    level="INFO",
                ))

            logger.info("cascade_momentum_complete",
                        symbol=symbol, direction=direction,
                        order_id=_bracket_result.entry_order_id,
                        notional=round(candidate.size * candidate.entry_price, 2))

        except Exception as _cm_ex:
            logger.error("cascade_momentum_exception", error=str(_cm_ex))
            if alert_system:
                asyncio.create_task(alert_system.send(
                    f"Cascade MOMENTUM exception: {_cm_ex}", level="ERROR"
                ))

    async def on_signal_ready(event: Event):
        """Event-driven execution handler. Uses cached balance to avoid async latency."""
        nonlocal _last_market_context, _last_calendar_state
        nonlocal _aftermath_primed, _aftermath_direction, _aftermath_expires_ms
        state = event.data.get("state")
        if not state:
            return

        # XAUT thermometer — update on every gold signal, regardless of trade outcome
        if event.symbol == "XAUT-USD":
            _xd = getattr(state, 'trade_direction', 'none')
            if _xd in ("long", "short"):
                _xaut_thermometer.update(_xd, float(getattr(state, 'coherence_score', 0.0)))

        # ── Global kill switch — block all new orders when halted ────────────────
        if _trading_halted[0]:
            logger.debug("trading_halted_signal_blocked", symbol=event.symbol)
            return

        symbol = event.symbol

        # ── Subscription guard — one set lookup (~50 ns) when already subscribed;
        # waits ≤2 s on the first signal for a watchlist symbol not yet online.
        # Placed before the throttle so we don't consume the 30s window waiting.
        if hasattr(ws_manager, "ensure_subscribed"):
            await ws_manager.ensure_subscribed(symbol)

        # ── Per-symbol signal throttle — prevents a single symbol from burning
        # through all 12 risk gates repeatedly on thin market noise.
        # Cascade signals get a tighter 10s window (time-critical).
        # Standard signals: 30s minimum between processing attempts per symbol.
        _now_ts = time.time()
        _strategy_tag_pre = tag_strategy(
            state,
            cascade_phase=cascade_tracker.get_phase().value if cascade_tracker else "idle",
        ) if hasattr(state, "regime") else "unknown"
        _throttle_s = 10.0 if _strategy_tag_pre.startswith("cascade") else 60.0
        _age_since_last = _now_ts - _last_signal_ts.get(symbol, 0)
        if _age_since_last < _throttle_s:
            # Best-signal-wins: allow through if coherence is meaningfully higher
            _incoming_coh = float(getattr(state, "coherence_score", 0.0) or 0.0)
            _prev_coh = _last_signal_coh.get(symbol, 0.0)
            if _incoming_coh < _prev_coh + 1.5:   # must beat last by ≥1.5 to bypass throttle
                logger.debug("signal_throttled", symbol=symbol, throttle_s=_throttle_s,
                             incoming_coh=round(_incoming_coh, 2), prev_coh=round(_prev_coh, 2))
                return
            logger.info("signal_throttle_bypassed_high_coh", symbol=symbol,
                        incoming_coh=round(_incoming_coh, 2), prev_coh=round(_prev_coh, 2))
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

        # ── Session symbol exclusion gate ────────────────────────────────────────
        if symbol in session_manager.get_excluded_symbols():
            _bypass_coh = getattr(state, 'coherence_score', 0.0)
            _bypass_min = getattr(config, 'aftermath_session_bypass_min_coherence', 5.0)
            # Aftermath bypass: a post-cascade primed signal at ≥5.0 coherence overrides
            # the session exclusion. Rationale: aftermath is high-conviction mean-reversion
            # that runs independent of session liquidity conditions. Weak signals (<5.0)
            # never bypass — Asian session exclusions exist for a reason.
            if _aftermath_primed and _bypass_coh >= _bypass_min:
                logger.info("session_exclusion_bypassed_aftermath",
                            symbol=symbol,
                            coherence=round(_bypass_coh, 2),
                            bypass_min=_bypass_min,
                            session=session_manager.get_current_session())
            else:
                logger.info("session_excluded_symbol",
                            symbol=symbol,
                            session=session_manager.get_current_session(),
                            excluded=session_manager.get_excluded_symbols())
                return

        # ── Regime stability suppression — Gap 6 ─────────────────────────────────
        # Transitioning + conf≤0.3 for >3 min: 8+ min of churn in live logs, 20+ useless signals.
        _rs_for_guard = regime_engine.last_state()
        _signal_guard.update_regime(_rs_for_guard)
        if _rs_for_guard is not None:
            _exec_guardian.update_regime_confidence(
                float(getattr(_rs_for_guard, 'confidence', 0.0) or 0.0)
            )
        if _signal_guard.is_regime_suppressed:
            # Elite signals (coherence ≥ 7.0) bypass regime instability suppression.
            # A 7+ coherence signal is its own evidence — regime uncertainty is noise.
            _pre_coh = float(getattr(state, 'coherence_score', 0.0) or 0.0)
            if _pre_coh < 6.0:
                logger.info("signal_suppressed_regime_instability", symbol=symbol,
                             coherence=round(_pre_coh, 2))
                return
            logger.info("regime_suppression_bypassed_elite",
                        symbol=symbol, coherence=round(_pre_coh, 2))

        # ── Open position guard — block hedges; allow pyramid only after TP1 ──────
        # SoDEX oneway mode: an opposite-side order creates a cross the exchange
        # auto-closes at a loss. Same-side pyramid entries are allowed ONLY when:
        #   (a) exactly one position open (count == 1)
        #   (b) TP1 is already hit (golden stop locked in, risk free)
        #   (c) signal direction matches existing position side
        # This makes pyramid behaviour deterministic: TP1 hit → allow one add.
        _pyramid_base_pos = None   # set below if this signal is a pyramid add
        if position_manager.count(symbol) > 0:
            if position_manager.count(symbol) >= 2:
                # Hard cap: never hold more than 2 layers on a single symbol
                logger.debug("signal_skipped_pyramid_cap", symbol=symbol, count=position_manager.count(symbol))
                return
            if not position_manager.can_pyramid(symbol):
                # TP1 not yet hit — too early to add
                logger.debug("signal_skipped_has_position", symbol=symbol, reason="tp1_not_hit")
                return
            _existing_pos = position_manager.get(symbol)[0]
            _signal_dir = getattr(state, 'trade_direction', 'none')
            if _existing_pos.side != _signal_dir:
                # Evaluate auto-adjustment: high-conviction opposing signals reduce/close the position
                _adj_cal = _last_calendar_state
                _adj_tr  = evaluate_time_regime(
                    event_type=getattr(_adj_cal, 'nearest_event_type', None) if _adj_cal else None,
                    hours_to_event=getattr(_adj_cal, 'hours_to_event', None) if _adj_cal else None,
                )
                _adj = _auto_adj_engine.evaluate(
                    symbol=symbol,
                    signal_direction=_signal_dir,
                    coherence=float(getattr(state, 'coherence_score', 0.0)),
                    open_position_side=_existing_pos.side,
                    cascade_phase=cascade_tracker.get_phase().value,
                    cascade_zscore=float(getattr(cascade_tracker, '_block_zscore', 0.0)),
                    regime_state=regime_engine.last_state(),
                    time_regime_mult=_adj_tr.risk_multiplier * _adj_tr.confidence_multiplier,
                    size_mult=float(getattr(state, 'size_multiplier', 1.0)),
                )
                if _adj.action != "none" and getattr(config, 'auto_adj_enabled', False):
                    _close_sz = round(float(_existing_pos.size) * _adj.close_pct, 8)
                    _sym_id   = SYMBOL_IDS.get(symbol, 0)
                    if _sym_id and _close_sz > 0:
                        asyncio.ensure_future(_close_with_retry(
                            symbol, _sym_id, _existing_pos.side, _close_sz,
                            reason=f"auto_adj:{_adj.reason}",
                        ))
                logger.debug("signal_skipped_opposite_direction",
                             symbol=symbol, pos_side=_existing_pos.side, signal_dir=_signal_dir,
                             auto_adj=_adj.action, auto_adj_coh=round(float(getattr(state, 'coherence_score', 0.0)), 2))
                return
            # All checks passed: TP1 hit, same direction, count==1 → pyramid allowed
            # Gate: pyramid requires ≥7.0 coherence — only add to the strongest signals.
            # Livermore rule: never average down, only add to proven winners with conviction.
            _pyr_coh = float(getattr(state, 'coherence_score', 0) or 0)
            if _pyr_coh < 7.0:
                logger.debug("pyramid_skipped_low_coherence",
                             symbol=symbol, coherence=round(_pyr_coh, 2),
                             required=7.0)
                return
            _pyramid_base_pos = _existing_pos
            logger.info("pyramid_entry_allowed",
                        symbol=symbol, coherence=round(_pyr_coh, 2),
                        tp1_hit=True, existing_entry=round(_existing_pos.entry_price, 4),
                        base_size=round(_existing_pos.initial_size, 6))

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
        _global_cap = config.max_concurrent_positions
        # alt_season: cap at alt_season_max_positions (default 3) to concentrate
        # capital on fewer, larger positions in the leading alt_l1 sector.
        _pos_regime = regime_engine.last_state()
        if _pos_regime is not None and _pos_regime.regime == "alt_season":
            _global_cap = min(_global_cap, getattr(config, 'alt_season_max_positions', 3))
        _max_pos = min(_global_cap, session_manager.get_max_positions())
        if _active_count >= _max_pos:
            logger.debug("max_concurrent_positions_reached",
                         symbol=symbol, active=_active_count, cap=_max_pos,
                         regime=getattr(_pos_regime, 'regime', 'unknown') if _pos_regime else 'unknown')
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

        # ── Temporal market-hours gate ────────────────────────────────────────
        # temporal_mult=0.0 → market is hard-closed (XAUT overnight, USTECH100 weekends).
        # Soft multipliers (crypto weekend 0.75, pre-mkt 0.5) are intentionally NOT
        # applied to size: ARIA targets full $200 notional every trade regardless of
        # session. Drawdown and feedback multipliers provide sufficient risk management.
        temporal_mult = market_hours.get_combined_multiplier(symbol)
        if temporal_mult <= 0.0:
            logger.debug("signal_dropped_temporal_closed", symbol=symbol, temporal_mult=temporal_mult)
            return  # Hard closed (belt-and-suspenders)

        # Record signal direction for extreme-market directional consensus.
        # RiskEngine uses this to boost dominant direction size in ATR ratio > 1.5.
        _sig_dir = getattr(state, 'trade_direction', 'none')
        _sig_coh = getattr(state, 'coherence_score', 0.0)
        _last_signal_coh[symbol] = _sig_coh   # track for best-signal-wins throttle
        _pub_coherence = 0.0  # populated below; used for Kingdom publish after sizing
        if _sig_dir in ("long", "short"):
            # Apply Bybit cross-venue cascade modifier to published coherence.
            # Bybit cascades lead SoDEX by 200–800ms — AUGUR writes confirmation/conflict.
            # This only adjusts the kingdom-published value; ARIA's internal execution
            # uses state.coherence_score unchanged (no execution stack modification).
            _bybit_delta   = _read_bybit_cascade_delta(symbol, _sig_dir)
            _pub_coherence = max(0.0, min(10.0, _sig_coh + _bybit_delta))
            # Directional veto: funding carry headwind + rolling win-rate bias (Gaps 2, 4)
            _fr = _live_funding_rates.get(symbol, 0.0)
            if _signal_guard.should_reject_direction(symbol, _sig_dir, _fr):
                logger.info("signal_rejected_directional_guard",
                            symbol=symbol, direction=_sig_dir, funding_rate=round(_fr, 4))
                return
            risk_engine.record_signal(symbol, _sig_dir, _sig_coh)

            # ── ExecutionGuardian: symbol limits, balance tier, flip cooldown ─
            _guard_zs = float(
                vc_monitor.get_status().get("cascade_zscore", 0.0)
                if vc_monitor is not None else 0.0
            )
            _guard_regime_conf = float(
                getattr(regime_engine.last_state(), 'confidence', 0.0) or 0.0
                if regime_engine.last_state() is not None else 0.0
            )
            _guard_v = _exec_guardian.check(
                symbol        = symbol,
                direction     = _sig_dir,
                coherence     = _sig_coh,
                rr_ratio      = 0.0,    # R:R checked in late gate after candidate built
                balance       = balance,
                regime_state  = None,   # alignment handled by existing gate below
                cascade_zscore= _guard_zs,
                regime_conf   = _guard_regime_conf,
            )
            if not _guard_v.allowed:
                logger.info(_guard_v.log_event,
                            symbol=symbol, direction=_sig_dir,
                            coherence=round(_sig_coh, 2),
                            reason=_guard_v.reason)
                if _outcome_recorder is not None:
                    _blk_mp = 0.0
                    _blk_mps = mark_price_stores.get(symbol)
                    if _blk_mps and hasattr(_blk_mps, "latest"):
                        _blk_mp = float(_blk_mps.latest or 0.0)
                    _blk_rs = regime_engine.last_state()
                    asyncio.create_task(_outcome_recorder.record_blocked(
                        symbol=symbol, direction=_sig_dir,
                        coherence=_sig_coh, gate_reason=_guard_v.reason,
                        mark_price=_blk_mp,
                        regime=getattr(_blk_rs, "regime", "") if _blk_rs else "",
                        strategy_type=_strategy_tag_pre,
                    ))
                return
            # ── End ExecutionGuardian early gate ─────────────────────────────

            # Throttle tracker update: only signals that pass the guardian get tracked.
            # Prevents rejected signals from poisoning the 60s throttle window.
            _last_signal_ts[symbol]  = _now_ts
            _last_signal_coh[symbol] = float(getattr(state, "coherence_score", 0.0) or 0.0)

            # Kingdom publish moved to after sizing chain — Gap 1 fix:
            # only signals that pass notional/regime/coherence gates reach AUGUR.

            # ── Regime-direction alignment gate ──────────────────────────────
            # Universal: rejects ANY signal fighting structural regime flow,
            # across all coin pairs and all regime types.
            # Rule: don't short the leading sector; don't long the lagging sector.
            # The regime classifier's leading_category/lagging_category dynamically
            # resolves to whichever sector is outperforming (alt_l1, large_cap,
            # l2, meme, cex_ecosystem) — so this gate applies to every asset.
            # Confidence ≥ 0.60 to avoid acting on unstable regime readings.
            # Aftermath signals bypass — they exploit exhaustion, not momentum.
            if not _aftermath_primed:
                _ral_rs = regime_engine.last_state()
                if _ral_rs is not None and _ral_rs.confidence >= 0.60:
                    _ral_cat  = config.ASSET_CONFIG.get(symbol, {}).get("category", "none")
                    _ral_lead = _ral_rs.leading_category
                    _ral_lag  = _ral_rs.lagging_category
                    _ral_blocked = False
                    _ral_reason  = ""

                    # Universal leading/lagging rule — applies to all symbols
                    if _ral_lead not in ("none", "unknown", "") and _ral_cat == _ral_lead and _sig_dir == "short":
                        _ral_blocked, _ral_reason = True, "short_against_leading_sector"
                    elif _ral_lag not in ("none", "unknown", "") and _ral_cat == _ral_lag and _sig_dir == "long":
                        _ral_blocked, _ral_reason = True, "long_against_lagging_sector"

                    if _ral_blocked:
                        logger.info("signal_rejected_regime_alignment",
                                    symbol=symbol, direction=_sig_dir,
                                    regime=_ral_rs.regime, category=_ral_cat,
                                    leading=_ral_lead, lagging=_ral_lag,
                                    confidence=round(_ral_rs.confidence, 3),
                                    reason=_ral_reason)
                        return

            # ── Cascade expansion direction veto ──────────────────────────────
            # When cascade is EXPANSION + zscore > 3.0, signals opposing cascade
            # direction are vetoed. Aftermath bypass: aftermath trades against
            # cascade intentionally (they are recovery entries post-exhaustion).
            if not _aftermath_primed and liq_engine is not None:
                _cav_snap = liq_engine.get_phase_snapshot("")  # market-wide
                if (_cav_snap.phase.value == "expansion" and _cav_snap.zscore > 3.0
                        and _cav_snap.last_direction in ("bearish", "bullish")):
                    _cav_conflict = (
                        (_cav_snap.last_direction == "bearish" and _sig_dir == "long") or
                        (_cav_snap.last_direction == "bullish" and _sig_dir == "short")
                    )
                    if _cav_conflict:
                        logger.info("signal_rejected_cascade_expansion",
                                    symbol=symbol, direction=_sig_dir,
                                    cascade_dir=_cav_snap.last_direction,
                                    zscore=round(_cav_snap.zscore, 2))
                        return

        # Build candidate — pass config and param_store for per-asset stop mults
        candidate = build_candidate(state, balance, margin_engine, config=config,
                                    param_store=_param_store)
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
        # ── Pyramid sizing override — Livermore/Druckenmiller ladder ─────────
        # Pyramid unit = 50% of original position size, stop at original entry
        # (risk-free: worst case the pyramid unit breaks even, base unit is gold-stopped).
        # TPs inherit the same structure; build_candidate already set them correctly.
        if _pyramid_base_pos is not None:
            _base_initial = float(_pyramid_base_pos.initial_size or _pyramid_base_pos.size or 0.0)
            if _base_initial > 0:
                _pyr_size = round(_base_initial * 0.50, 8)
                _pyr_size = max(_pyr_size,
                                config.SYMBOL_MIN_QUANTITY.get(symbol, 0.0))
                candidate.size = _pyr_size
                candidate.initial_margin = round(
                    _pyr_size * candidate.entry_price / max(candidate.leverage, 1), 8
                )
                # Stop = original entry (breakeven for the whole ladder)
                candidate.stop_price = _pyramid_base_pos.entry_price
                logger.info("pyramid_sized",
                            symbol=symbol,
                            base_size=round(_base_initial, 6),
                            pyr_size=round(_pyr_size, 6),
                            stop=round(candidate.stop_price, 4),
                            entry=round(candidate.entry_price, 4))

        # ── Execution Alpha Patch: Dispersion Gate → Signal Tier → Regime/Streak sizing ──
        _ap_regime_state = regime_engine.last_state()
        _ap_regime_name  = getattr(_ap_regime_state, 'regime', 'unknown') if _ap_regime_state else 'unknown'
        _ap_regime_conf  = float(getattr(_ap_regime_state, 'confidence', 0.5) or 0.5) if _ap_regime_state else 0.5
        _ap_disp         = float(getattr(_ap_regime_state, 'dispersion', 0.003) or 0.003) if _ap_regime_state else 0.003
        _ap_lead_sector  = getattr(_ap_regime_state, 'leading_category', '') if _ap_regime_state else ''
        _ap_asset_cat    = config.ASSET_CONFIG.get(symbol, {}).get('category', '')

        # Dispersion gate — block alts when market is correlated (no independent alt edge)
        if config.dispersion_gate_enabled:
            _dg_ok, _dg_reason = _dispersion_gate.should_trade(
                symbol=symbol, dispersion=_ap_disp,
                leading_sector=_ap_lead_sector, asset_category=_ap_asset_cat,
            )
            if not _dg_ok:
                # Micro-mode bypass: with $88, sector rotation is less important than
                # raw directional edge. A 5.0+ coherence signal on an alt is actionable.
                if balance < 300.0 and _sig_coh >= 5.0:
                    logger.info("dispersion_micro_mode_bypass",
                                symbol=symbol, coherence=round(_sig_coh, 2),
                                reason=_dg_reason)
                else:
                    logger.info("signal_rejected_dispersion_gate",
                                symbol=symbol, direction=_sig_dir,
                                dispersion=round(_ap_disp, 5), reason=_dg_reason)
                    return

        # Signal tier classification → C-tier skip → tier size multiplier
        _signal_tier = None
        _ap_cascade_zs = float(getattr(state, 'cascade_zscore', 0.0) or 0.0)
        if config.signal_tier_enabled:
            _ap_agg_ratio  = float(getattr(state, 'aggressor_ratio', 0.5) or 0.5)
            _ap_hist_wr    = float(getattr(state, 'historical_wr', 0.5) or 0.5)
            _ap_macro_conf = bool(getattr(state, 'macro_aligned', False))
            _ap_session_q  = getattr(state, 'session_quality', 'normal') or 'normal'
            _signal_tier   = classify_signal(
                coherence=_sig_coh, cascade_zscore=_ap_cascade_zs,
                agg_ratio=_ap_agg_ratio, regime_confidence=_ap_regime_conf,
                hist_wr=_ap_hist_wr, macro_confirmation=_ap_macro_conf,
                session=_ap_session_q, regime_bypass_elite=bool(_aftermath_primed),
            )
            if _signal_tier.value == "c_tier":
                # Micro-mode bypass: sub-$100 accounts with high coherence get a second chance.
                # On micro accounts we cannot afford to reject 6.0+ coherence signals
                # because the composite edge score is dragged down by poor hist_wr.
                if balance < 300.0 and _sig_coh >= 6.0:
                    logger.info("c_tier_micro_mode_bypass",
                                symbol=symbol, coherence=round(_sig_coh, 2),
                                balance=round(balance, 2))
                    _signal_tier = SignalTier.B
                else:
                    logger.info("signal_rejected_c_tier",
                                symbol=symbol, direction=_sig_dir,
                                coherence=round(_sig_coh, 2), tier="c_tier")
                    return
            _tier_mult = TIER_SIZE_MULT.get(_signal_tier, 1.0)
            if _tier_mult not in (0.0, 1.0):
                candidate.size = round(candidate.size * _tier_mult, 8)
                candidate.initial_margin = round(candidate.initial_margin * _tier_mult, 8)
                logger.debug("tier_size_applied", symbol=symbol,
                             tier=_signal_tier.value, mult=_tier_mult,
                             size=round(candidate.size, 6))

        # Trade type tagging (stored on candidate for time-stop and TP routing)
        _trade_type = None
        if config.trade_type_enabled:
            _ap_personality = getattr(state, 'personality', 'default') or 'default'
            _ap_vol_pct     = float(getattr(state, 'volatility_percentile', 0.5) or 0.5)
            _trade_type = tag_trade_type(
                symbol=symbol, personality=_ap_personality,
                cascade_zscore=_ap_cascade_zs, regime=_ap_regime_name,
                volatility_percentile=_ap_vol_pct,
            )
            if hasattr(candidate, '__dict__'):
                candidate.__dict__['trade_type'] = _trade_type.value
            logger.debug("trade_type_tagged", symbol=symbol,
                         trade_type=_trade_type.value,
                         tier=_signal_tier.value if _signal_tier else "none")

        # Regime-aware size multiplier
        if config.regime_sizing_enabled and _ap_regime_name not in ('unknown', ''):
            _rsz_mult = regime_size_mult(
                regime=_ap_regime_name, regime_confidence=_ap_regime_conf,
                symbol=symbol, direction=_sig_dir,
            )
            if _rsz_mult != 1.0:
                candidate.size = round(candidate.size * _rsz_mult, 8)
                candidate.initial_margin = round(candidate.initial_margin * _rsz_mult, 8)
                logger.debug("regime_size_applied", symbol=symbol,
                             regime=_ap_regime_name, mult=round(_rsz_mult, 3),
                             size=round(candidate.size, 6))

        # Streak compounding — consecutive winners → 1.1x / 1.2x / 1.3x cap
        if config.streak_sizing_enabled:
            _streak_mult = _streak_tracker.get_streak_multiplier(symbol, _sig_dir)
            if _streak_mult != 1.0:
                candidate.size = round(candidate.size * _streak_mult, 8)
                candidate.initial_margin = round(candidate.initial_margin * _streak_mult, 8)
        # ── End Execution Alpha Patch sizing ─────────────────────────────────

        # ── ExecutionGuardian late gate: R:R + coherence tier size multiplier ─
        _late_g = _exec_guardian.check(
            symbol        = symbol,
            direction     = _sig_dir,
            coherence     = _sig_coh,
            rr_ratio      = float(candidate.rr_ratio or 0.0),
            balance       = balance,
            regime_state  = None,   # alignment already handled
            cascade_zscore= _guard_zs if '_guard_zs' in dir() else 0.0,
            regime_conf   = _guard_regime_conf if '_guard_regime_conf' in dir() else 0.0,
        )
        if not _late_g.allowed:
            logger.info(_late_g.log_event,
                        symbol=symbol, rr=round(candidate.rr_ratio, 2),
                        coherence=round(_sig_coh, 2), reason=_late_g.reason)
            if _outcome_recorder is not None:
                _blk_mp2 = float(candidate.entry_price or 0.0)
                _blk_rs2 = regime_engine.last_state()
                asyncio.create_task(_outcome_recorder.record_blocked(
                    symbol=symbol, direction=_sig_dir,
                    coherence=_sig_coh, gate_reason=_late_g.reason,
                    mark_price=_blk_mp2,
                    regime=getattr(_blk_rs2, "regime", "") if _blk_rs2 else "",
                    strategy_type=_strategy_tag_pre,
                ))
            return
        # Apply guardian coherence-tier size multiplier (Nietzsche supplements this)
        # Micro-mode: skip guardian size_mult — COHERENCE_TIERS is calibrated for
        # $200+ accounts. At $88, 0.50× on a $79 base = $39.60, below SoDEX $50 min.
        if _late_g.size_mult not in (1.0, 0.0) and balance >= 300.0:
            candidate.size = round(candidate.size * _late_g.size_mult, 8)
            candidate.initial_margin = round(
                candidate.size / getattr(candidate, 'leverage', config.default_leverage), 8
            )
        # ── End ExecutionGuardian late gate ──────────────────────────────────

        # temporal_mult is logged in sizing_chain below but NOT applied to size —
        # full $200 base notional is preserved regardless of session quality.

        # ── DrawdownManager halt gate — absolute block on 50%+ total or 15% daily DD ──
        if drawdown_manager is not None and not drawdown_manager.can_trade_directional():
            _halt_reason = getattr(drawdown_manager, '_halt_reason', 'drawdown_limit')
            if not _trading_halted[0]:
                _trading_halted[0] = True
                logger.critical("trading_halted_drawdown",
                                reason=_halt_reason,
                                note="all new trades blocked by drawdown manager")
            else:
                logger.warning("drawdown_manager_halt",
                               symbol=symbol, reason=_halt_reason)
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

        # ── DrawdownManager size multiplier ──────────────────────────────────
        # 1.0 (normal) / 0.75 (10–20% DD) / 0.50 (20–25% DD) / 0.0 (halted — already gated above)
        _dm_mult = drawdown_manager.get_size_multiplier() if drawdown_manager else 1.0
        if _dm_mult < 1.0:
            candidate.size = round(candidate.size * _dm_mult, 8)
            candidate.initial_margin = round(candidate.initial_margin * _dm_mult, 8)
            logger.debug("drawdown_manager_size_reduced",
                         symbol=symbol, dm_mult=round(_dm_mult, 2),
                         size=candidate.size)

        # ── Time Regime Overlay — LAST in sizing chain ────────────────────────
        # Stateless, deterministic multiplier based on time-of-month / day / hour
        # and macro event proximity.  Applied after all other multipliers so the
        # output reflects the final intended size before minimum notional guard.
        _cal_state = _last_calendar_state
        _cal_event_type    = getattr(_cal_state, "nearest_event_type", None)  if _cal_state else None
        _cal_hours_to_evt  = getattr(_cal_state, "hours_to_event", None)      if _cal_state else None
        _time_regime = evaluate_time_regime(
            event_type=_cal_event_type,
            hours_to_event=_cal_hours_to_evt,
        )
        _tr_mult = _time_regime.risk_multiplier * _time_regime.confidence_multiplier
        if _tr_mult != 1.0:
            candidate.size = round(candidate.size * _tr_mult, 8)
            candidate.initial_margin = round(candidate.initial_margin * _tr_mult, 8)
            logger.debug("time_regime_applied",
                         symbol=symbol,
                         phase=_time_regime.phase,
                         risk_mult=_time_regime.risk_multiplier,
                         conf_mult=_time_regime.confidence_multiplier,
                         combined=round(_tr_mult, 4),
                         size=candidate.size)

        # ── Sizing chain audit log — always emitted so we can trace every trade ──
        _notional = candidate.entry_price * candidate.size
        logger.info(
            "sizing_chain",
            symbol=symbol,
            temporal_mult=round(temporal_mult, 3),   # informational — NOT applied to size
            dd_mult=round(_dd_mult, 3),
            tod_mult=round(_tod_mult, 3),
            dm_mult=round(_dm_mult, 3),
            time_regime_mult=round(_tr_mult, 4),
            time_regime_phase=_time_regime.phase,
            combined_mult=round(_dd_mult * _tod_mult * _dm_mult * _tr_mult, 4),  # actual size multiplier
            size=round(candidate.size, 6),
            entry=round(candidate.entry_price, 4),
            notional=round(_notional, 2),
            margin=round(candidate.initial_margin, 2),
            leverage=getattr(candidate, 'leverage', config.default_leverage),
        )

        # ── Session size multiplier — applied last in chain ──────────────────────
        _sess_mult = session_manager.get_size_multiplier()
        if _sess_mult != 1.0:
            candidate.size = round(candidate.size * _sess_mult, 8)
            candidate.initial_margin = round(candidate.initial_margin * _sess_mult, 8)
            logger.debug("session_size_multiplier_applied",
                         symbol=symbol,
                         session=session_manager.get_current_session(),
                         sess_mult=_sess_mult,
                         size=candidate.size)

        # ── Minimum notional guard — SoDEX absolute minimum post all multipliers ──
        if _notional < 10.0:
            logger.warning("signal_rejected_dust_notional",
                           symbol=symbol,
                           notional=round(_notional, 2),
                           price=round(candidate.entry_price, 4),
                           size=candidate.size,
                           reason="below_sodex_minimum")
            return

        # Block just lifted: clear state + apply post-block conviction boosts (Gap 5)
        if _calendar_block_active[0]:
            _signal_guard.on_block_state(False)
            _calendar_block_active[0] = False

        # Post-block conviction boost — 1.15× size for pent-up signals (Gap 5)
        if _sig_dir in ("long", "short"):
            _pb_boost = _signal_guard.consume_post_block_boost(symbol, _sig_dir)
            if _pb_boost != 1.0:
                _boosted_notional = candidate.entry_price * candidate.size * _pb_boost
                if _boosted_notional <= config.max_notional_usd:
                    candidate.size = round(candidate.size * _pb_boost, 8)
                    candidate.initial_margin = round(candidate.initial_margin * _pb_boost, 8)

        # Kingdom publish — gated: only when sizing chain passed (Gap 1)
        if _sig_dir in ("long", "short") and _tr_mult > 0.0:
            asyncio.ensure_future(_write_aria_bet_to_kingdom(
                symbol=symbol,
                direction=_sig_dir,
                coherence=_pub_coherence,
                confidence=min(_sig_coh / 10.0, 1.0),
                cascade_phase=getattr(cascade_tracker, "_block_phase", "none") or "none",
                funding_rate=float(getattr(state, "funding_rate", 0.0) or 0.0),
            ))

        # ── Build MarketContext — unified frozen snapshot for this tick ──────────
        # Built once here; stored on interpreter so coherence scoring picks it up
        # for the NEXT tick (context weights apply prospectively). Also passed to
        # adaptive_calibrator and tagged onto journal entries.
        try:
            _last_calendar_state = await calendar_engine.get_state(symbol)
        except Exception:
            pass  # Keep last cached state
        try:
            _last_market_context = MarketContext.build(
                cascade_tracker          = cascade_tracker,
                funding_history          = funding_history,
                trade_flow_stores        = trade_flow_stores,
                relative_strength_engine = regime_engine,
                candle_buffers           = candle_buffers,
                adaptive_calibrator      = _adaptive_calibrator,
                calendar_state           = _last_calendar_state,
                assets                   = list(config.assets),
                time_regime              = _time_regime,
            )
            interpreter.set_market_context(_last_market_context)
            display.update_market_context(_last_market_context)
        except Exception as _ctx_ex:
            logger.debug("market_context_build_failed", error=str(_ctx_ex))

        # Cascade is a coherence input (Tier 6 score), not a trade gate.
        # CascadeTracker PRIMED/MOMENTUM phases pass through — only BLOCKED was a gate,
        # and that gate is now removed. cascade_tracker still drives MarketContext/display.

        # ── Aftermath primed: tag trade, reduce size, lower coherence floor ───
        _is_aftermath_trade = False
        if _aftermath_primed:
            now_ms_aft = int(time.time() * 1000)
            if now_ms_aft > _aftermath_expires_ms:
                # Window expired without a trade
                _aftermath_primed = False
                logger.info("cascade_aftermath_expired")
            elif candidate.side == _aftermath_direction:
                _is_aftermath_trade = True
                candidate.strategy_tag = "cascade_aftermath"
                # Lower coherence floor for aftermath trades (confirmed exhaustion)
                candidate.coherence_override = max(
                    3.0, getattr(candidate, "min_coherence", config.live_min_coherence) - 1.0
                )
                # Cascade-native stop: tighter than normal ATR stop for recovery plays.
                # Recompute stop, risk distance, and TPs to match aftermath profile.
                _entry = candidate.entry_price
                _atr = getattr(state, 'atr', 0.0)
                _stop_buffer = max(_entry * 0.005, _atr * 0.75)
                if candidate.side == "long":
                    candidate.stop_price = _entry - _stop_buffer
                    candidate.tp1_price = _entry + _stop_buffer * 1.5
                    candidate.tp2_price = _entry + _stop_buffer * 2.5
                    candidate.tp3_price = _entry + _stop_buffer * 3.5
                else:
                    candidate.stop_price = _entry + _stop_buffer
                    candidate.tp1_price = _entry - _stop_buffer * 1.5
                    candidate.tp2_price = _entry - _stop_buffer * 2.5
                    candidate.tp3_price = _entry - _stop_buffer * 3.5
                # Cap notional so aftermath size never exceeds 1.5× base
                _max_aftermath_notional = getattr(config, 'base_trade_usd', 200.0) * 1.5
                _current_notional = candidate.size * _entry
                if _current_notional > _max_aftermath_notional:
                    _step = config.ASSET_CONFIG.get(candidate.symbol, {}).get('tick_size', 0.01)
                    candidate.size = math.floor((_max_aftermath_notional / _entry) / _step) * _step
                    candidate.initial_margin = candidate.size / getattr(candidate, 'leverage', config.default_leverage)
                # ORACLE fusion: when cascade aftermath and oracle cluster align,
                # amplify size by 1.10–1.25× (oracle.get_fusion_mult checks direction).
                _aft_oracle_fusion = _oracle_engine.get_fusion_mult(_aftermath_direction)
                if _aft_oracle_fusion > 1.0:
                    candidate.size = round(candidate.size * _aft_oracle_fusion, 8)
                    candidate.initial_margin = round(
                        candidate.size / getattr(candidate, 'leverage', config.default_leverage), 8
                    )
                logger.info("cascade_aftermath_trade_tagged",
                            symbol=symbol,
                            direction=_aftermath_direction,
                            stop_buffer=round(_stop_buffer, 2),
                            oracle_fusion=round(_aft_oracle_fusion, 3),
                            notional=round(candidate.size * candidate.entry_price, 0))

        # ── Session drawdown regime gate ─────────────────────────────────────
        # True halt — 10%+ is existential. Hard stop, no new entries.
        # DD 3–10%: Nietzsche applies CONSERVATIVE/DEFENSIVE sizing via Will Table.
        # No hard block below 10% — system stays in the game with reduced size.
        _dd_pct = dd_tracker.session_drawdown_pct
        if _dd_pct >= 10.0:
            logger.warning("new_entry_halted",
                           symbol=symbol,
                           reason="drawdown_halt_10pct",
                           drawdown_pct=round(_dd_pct, 2))
            return

        # ECS gate — replaces binary consecutive_loss_skip with continuous capacity score.
        # Signal Preservation Rule: coherence ≥ 5.2 → ALWAYS execute regardless of loss history.
        # "Losses cannot override current edge." — ECS design principle
        _ecs_coherence = getattr(state, 'coherence_score', 0.0)
        if ecs_engine.should_bypass_loss_gate(_ecs_coherence):
            # Exceptional quality — bypass loss history entirely
            pass
        elif ecs_engine.blocks_entry(_ecs_coherence):
            logger.info("ecs_entry_blocked",
                        symbol=symbol,
                        mode=ecs_engine.get_mode(),
                        ecs=ecs_engine.get_ecs(),
                        score=round(_ecs_coherence, 2))
            return
        elif dd_tracker.too_many_losses():
            # Recovery mode: scale down rather than hard-block
            _ecs_size_mult = ecs_engine.get_size_mult()
            candidate.size = round(candidate.size * _ecs_size_mult, 8)
            candidate.initial_margin = round(candidate.initial_margin * _ecs_size_mult, 8)
            logger.info("ecs_recovery_size_scaled",
                        symbol=symbol,
                        mode=ecs_engine.get_mode(),
                        ecs=ecs_engine.get_ecs(),
                        size_mult=_ecs_size_mult,
                        losses=dd_tracker.consecutive_losses)

        # ── Recovery Mode gate (AdaptiveCalibrator v2.1) ──────────────────────
        # Triggered by drawdown ≥ 3% OR 10-trade win rate < 35%.
        # Does NOT hard-block — applies size cap and raises coherence floor.
        _rec_params = _adaptive_calibrator.get_recovery_params()
        if _rec_params:
            _rec_coh_min = _rec_params["coherence_min"]          # 5.6
            _rec_size_cap = _rec_params["size_cap"]               # 0.5
            _rec_tp_factor = _rec_params["tp_sl_factor"]          # 0.8
            if state.coherence_score < _rec_coh_min:
                logger.info("recovery_mode_coherence_skip",
                            symbol=symbol,
                            coherence=round(state.coherence_score, 2),
                            required=_rec_coh_min,
                            reason=_rec_params.get("reason", ""))
                return
            candidate.size = round(candidate.size * _rec_size_cap, 8)
            candidate.initial_margin = round(candidate.initial_margin * _rec_size_cap, 8)
            # Tighten TP/SL around the risk distance
            _r_dist = abs(candidate.entry_price - candidate.stop_price)
            if candidate.side == "long":
                candidate.tp1_price = candidate.entry_price + _r_dist * _rec_tp_factor
            else:
                candidate.tp1_price = candidate.entry_price - _r_dist * _rec_tp_factor
            logger.info("recovery_mode_applied",
                        symbol=symbol, size_cap=_rec_size_cap, tp_factor=_rec_tp_factor,
                        reason=_rec_params.get("reason", ""))

        # ── Momentum block gate (LiqPhaseEngine v2.1) ─────────────────────────
        # EXHAUSTION: block momentum strategies; reversal/aftermath continue with 1.2×.
        # AFTERMATH: never blocks — all strategies get 1.5× size boost.
        _liq_snap = liq_engine.get_phase_snapshot(symbol)
        _liq_phase_val = _liq_snap.phase.value
        _phase_size_mult = _liq_snap.size_mult
        if liq_engine.is_momentum_blocked(symbol):
            _strat = getattr(candidate, "strategy_tag", "unknown") or "unknown"
            if "cascade_momentum" in _strat or "momentum" in _strat.lower():
                logger.info("liq_exhaustion_momentum_blocked",
                            symbol=symbol, strategy=_strat, phase=_liq_phase_val)
                return
        # Apply phase size_mult for EXHAUSTION (1.2×) and AFTERMATH (1.5×) —
        # outside momentum_blocked check so AFTERMATH multiplier is never skipped.
        if _phase_size_mult != 1.0:
            candidate.size = round(candidate.size * _phase_size_mult, 8)
            candidate.initial_margin = round(candidate.initial_margin * _phase_size_mult, 8)
            logger.debug("liq_phase_size_applied",
                         symbol=symbol, phase=_liq_phase_val, mult=_phase_size_mult)

        # Apply drawdown TP modification before risk engine validation.
        # Modifies candidate.tp1/tp2/tp3 in-place based on current dd_tracker regime.
        _tp1_mult, _include_tp2, _include_tp3 = dd_tracker.tp_multipliers()
        if _tp1_mult != 1.0 or not _include_tp2:
            _risk_dist = abs(candidate.entry_price - candidate.stop_price)
            if candidate.side == "long":
                candidate.tp1_price = candidate.entry_price + _risk_dist * _tp1_mult
                candidate.tp2_price = (candidate.entry_price + _risk_dist * 2.0) if _include_tp2 else candidate.tp1_price
                candidate.tp3_price = (candidate.entry_price + _risk_dist * 3.0) if _include_tp3 else candidate.tp1_price
            else:
                candidate.tp1_price = candidate.entry_price - _risk_dist * _tp1_mult
                candidate.tp2_price = (candidate.entry_price - _risk_dist * 2.0) if _include_tp2 else candidate.tp1_price
                candidate.tp3_price = (candidate.entry_price - _risk_dist * 3.0) if _include_tp3 else candidate.tp1_price
            if dd_tracker.drawdown_regime in ("caution", "defensive"):
                logger.info(f"{dd_tracker.drawdown_regime}_tp_mode",
                            symbol=symbol,
                            drawdown_pct=round(dd_tracker.session_drawdown_pct, 2),
                            tp1_mult=_tp1_mult,
                            runners=_include_tp2)

        # Map MarketState regime → risk engine convention (BULL/BEAR/RANGING)
        _regime_map = {
            "risk_on": "BULL", "risk_off": "BEAR",
            "rotational": "RANGING", "confused": "RANGING",
        }
        _risk_regime = _regime_map.get(state.regime, "RANGING")

        # Reconcile 1m regime with 4H HTF bias.
        # HTF bearish used to FORCE BEAR → hard-blocked all longs for weeks.
        # Now HTF is a score multiplier only (0.5× counter, 1.3× aligned, interpreter.py).
        # We soften the risk_regime override: HTF bearish → RANGING (allows longs with
        # sufficient coherence) instead of BEAR (blocks all longs unconditionally).
        # HTF bullish + 1m BEAR → RANGING (shorts still allowed via coherence).
        _htf = interpreter._htf_bias.get(symbol, "neutral")
        if _htf == "bearish" and _risk_regime == "BULL":
            _risk_regime = "RANGING"   # HTF opposes 1m bullish → cautious, not hard-blocked
        elif _htf == "bullish" and _risk_regime == "BEAR":
            _risk_regime = "RANGING"   # HTF opposes 1m bearish → cautious, not hard-blocked

        # Derive avg_atr: candidate.atr_ratio = current / avg → avg = current / ratio
        _avg_atr = (candidate.atr / candidate.atr_ratio) if candidate.atr_ratio > 0 else 0.0

        # Approximate funding rate from categorical funding_class (Gate C input)
        _funding_map = {
            "extreme_positive": 0.002, "positive": 0.0005, "neutral": 0.0,
            "negative": -0.0005, "extreme_negative": -0.002,
        }
        _funding_rate = _funding_map.get(state.funding_class, 0.0)

        # Resolve strategy tag here — used by feedback floor and fast-block guard below,
        # then again for candidate pool submission. Defined once to avoid UnboundLocalError.
        _strategy_tag = tag_strategy(
            state,
            cascade_phase=cascade_tracker.get_phase().value if cascade_tracker else "idle",
        )

        # ── Quant execution filters ──────────────────────────────────────────────
        # Evidence-based filters derived from live trade history analysis.
        # Ordered cheapest-first (no I/O for filters 1-2; one dict call for 3-5).
        #
        # Filter | Evidence
        # ────────────────────────────────────────────────────────────────────────
        # 1. Volatility regime | atr_vs_baseline < 0.70 → COIL (not a hard block)
        # 2. HTF counter-trend | HTF-aligned 38% WR vs counter-trend 21% WR
        # 3. Cascade counter   | With cascade 71% WR +$1.93, against 14% WR -$2.03
        # 4. Cascade expansion | Limit orders don't fill in PHASE_EXPANSION
        # 5. Quiet market      | Active market 50% WR +$1.83, quiet 22% WR -$4.21

        # ── Filter 1: Volatility regime — COIL routing (not a hard block) ───────
        # Absolute ATR% (e.g. 1.0%) is wrong for crypto: 1m ATR on BTC is 0.05-0.15%,
        # on ARB 0.15-0.30%. An absolute threshold blocks every trade at all times.
        #
        # Correct metric: atr_vs_baseline = current_atr / 20-bar_avg_atr
        # This is self-calibrating — it measures whether the market is quiet
        # RELATIVE TO ITSELF, not against an arbitrary absolute.
        #
        # COIL_THRESHOLD = 0.70: if ATR is < 70% of the symbol's own baseline,
        # the market is in a coiling/consolidation regime. Directional signals
        # are low-confidence but arb and funding trades remain valid.
        # Not a block — set personality and continue.
        _atr_coil_threshold = ASSET_CLASS_ATR_THRESHOLDS.get(_get_asset_class(symbol), 0.80)
        _atr_ratio = candidate.atr_ratio  # current_atr / 20-bar_avg_atr (already computed)
        _is_coil = _atr_ratio < _atr_coil_threshold and _atr_ratio > 0
        if _is_coil:
            logger.info("personality_coil_atr_low",
                        symbol=symbol,
                        atr_vs_baseline=round(_atr_ratio, 3),
                        threshold=_atr_coil_threshold,
                        asset_class=_get_asset_class(symbol),
                        atr_dollars=round(candidate.atr, 4),
                        note="low_vol_regime_coil_not_blocked_arb_funding_allowed")

        # ── Filter 2: HTF counter-trend block ───────────────────────────────────
        # `_htf` resolved above (~line 1638) from interpreter._htf_bias.
        # Hard block when HTF is clear — counter-trend 21% WR vs aligned 38% WR.
        # Exception: "confused" regime has no dominant HTF direction; instead of
        # hard-blocking, apply a 20% coherence penalty and continue.
        _qf_side = candidate.side  # "long" | "short"
        _htf_confused_penalty = 1.0   # folded into _effective_coherence below
        _preliminary_coherence = state.coherence_score * _htf_confused_penalty

        def _apply_htf_counter_trend(htf: str, direction: str, coherence: float) -> bool:
            """
            Returns True if execution should continue (reduced or unchanged),
            False if the signal must be hard-blocked.
            Mutates candidate.size / _htf_confused_penalty via nonlocal.
            """
            nonlocal _htf_confused_penalty
            if state.regime == "confused":
                # Confused: no dominant HTF direction — soft penalty, do not block
                _htf_confused_penalty = 0.80
                logger.info("htf_confused_regime_soft",
                            symbol=symbol, htf=htf, direction=direction,
                            penalty=0.80,
                            note="confused_regime_no_dominant_bias_soft_penalty")
                return True

            # ACCUMULATION: HTF signal lags the 1m breakdown — probe entry at 40% size
            # Last known Kant frame for this symbol (O(1) dict lookup — no extra compute)
            _last_kant = kant_engine._last_frames.get(symbol)
            if (_last_kant is not None and
                    _last_kant.structure == _MarketStructure.ACCUMULATION):
                candidate.size = round(candidate.size * 0.40, 8)
                candidate.initial_margin = round(
                    candidate.size / getattr(candidate, "leverage", config.default_leverage), 8
                )
                logger.info("htf_counter_trend_accumulation_probe",
                            symbol=symbol, htf=htf, direction=direction,
                            size_mult=0.40,
                            note="accumulation_htf_stale_1m_leading_signal")
                return True

            # Elite exception: coherence ≥ 8.0 = confirmed high-conviction counter-trend.
            # E.g., a long signal during a bear trend with oracle + cascade + sweep all firing.
            # Enter at 50% size — Nietzsche will scale once the move confirms.
            if coherence >= 8.0:
                candidate.size = round(candidate.size * 0.50, 8)
                candidate.initial_margin = round(
                    candidate.size / getattr(candidate, "leverage", config.default_leverage), 8
                )
                logger.info("htf_counter_trend_elite_probe",
                            symbol=symbol, htf=htf, direction=direction,
                            coherence=round(coherence, 2),
                            size_mult=0.50,
                            note="coherence_8.0_plus_overrides_htf_block_at_half_size")
                return True

            logger.info("quant_filter_blocked",
                        reason="htf_counter_trend",
                        symbol=symbol, htf=htf, direction=direction,
                        evidence="htf_aligned_38pct_wr_counter_21pct_need_8.0_coherence")
            return False

        # Cascade aftermath is a Nietzschean force signal — confirmed exhaustion cascade.
        # It overrides the static HTF Kantian gate: the cascade IS the new regime.
        _aftermath_active = (
            _is_aftermath_trade
            and int(time.time() * 1000) < _aftermath_expires_ms
        )
        if symbol in config.TRADFI_ASSETS:
            # TradFi assets (gold, oil, equities) use their own price structure.
            # BTC HTF bias does not apply — skip gate entirely.
            logger.info("htf_gate_skipped_tradfi_asset",
                        symbol=symbol, direction=_qf_side, btc_htf=_htf,
                        note="tradfi_uses_own_structure")
        elif not _aftermath_active:
            if _htf == "bullish" and _qf_side == "short":
                if not _apply_htf_counter_trend(_htf, _qf_side, _preliminary_coherence):
                    return
            if _htf == "bearish" and _qf_side == "long":
                if not _apply_htf_counter_trend(_htf, _qf_side, _preliminary_coherence):
                    return
        else:
            logger.info("htf_counter_trend_bypassed_aftermath",
                        symbol=symbol, htf=_htf, direction=_qf_side,
                        note="cascade_aftermath_overrides_static_htf_gate")

        # ── Filters 3-5: ValueChain status (single dict call) ───────────────────
        _vc_status = vc_monitor.get_status() if vc_monitor is not None else {}
        _vc_zscore    = float(_vc_status.get("cascade_zscore",   0.0))
        _vc_direction = str(_vc_status.get("cascade_direction",  "none"))  # "bullish"|"bearish"|"mixed"
        _vc_phase     = str(_vc_status.get("cascade_phase",      "none"))
        _vc_events_60 = int(_vc_status.get("events_60s",         999))     # 999 = vc_monitor unavailable

        # ── Filter 3: Cascade alignment ─────────────────────────────────────────
        # Cascade direction: "bearish" = longs liquidated → price falling → trade SHORT.
        #                    "bullish" = shorts liquidated → price rising → trade LONG.
        # Only enforce when statistically significant (zscore > 2.0) and unambiguous.
        if _vc_zscore > 2.0 and _vc_direction not in ("mixed", "none", ""):
            _cascade_with_dir = "short" if _vc_direction == "bearish" else "long"
            if _qf_side != _cascade_with_dir:
                logger.info("quant_filter_blocked",
                            reason="cascade_counter_direction",
                            symbol=symbol,
                            trade_dir=_qf_side,
                            cascade_dir=_vc_direction,
                            zscore=round(_vc_zscore, 2),
                            evidence="with_cascade_71pct_wr_against_14pct")
                return

        # ── Filter 4: Cascade expansion block ───────────────────────────────────
        # During PHASE_EXPANSION the price is accelerating — limit orders fail to fill
        # (exchange fills at a worse price or rejects outright). Block and wait for
        # PHASE_EXHAUSTION / aftermath where entries actually land.
        if _vc_phase == "expansion" and _vc_zscore > 2.5:
            logger.info("quant_filter_blocked",
                        reason="cascade_expansion_unfillable",
                        symbol=symbol, phase=_vc_phase, zscore=round(_vc_zscore, 2),
                        evidence="limit_orders_miss_during_expansion_wait_for_aftermath")
            return

        # ── Filter 5: Quiet market pause ─────────────────────────────────────────
        # Update the shared activity timestamp whenever the market is live.
        # Block if the market has been quiet (< 40 liqs/60s) for > 30 minutes.
        _qm_now = time.time()
        if _vc_events_60 < 999:   # only update when vc_monitor is alive
            if _vc_events_60 >= 40:
                _last_active_market_ts[0] = _qm_now
        _quiet_s = _qm_now - _last_active_market_ts[0]

        # Cascade aftermath overrides quiet filter: post-cascade silence IS the cascade.
        # Low events_60s after a cascade means activity was exhausted — the opposite of
        # genuine quietness. Only bypass when the original cascade z-score was ≥2.0.
        if _aftermath_active and cascade_tracker._block_zscore >= 2.0:
            logger.info("quiet_filter_bypassed_aftermath",
                        symbol=symbol,
                        note="cascade_aftermath_overrides_quiet_market_filter",
                        events_60s=_vc_events_60,
                        quiet_minutes=round(_quiet_s / 60.0, 1),
                        cascade_zscore=round(cascade_tracker._block_zscore, 2))
        elif _vc_events_60 != 999 and _vc_events_60 < 40 and _quiet_s > 1800.0:
            logger.info("quant_filter_blocked",
                        reason="quiet_market_pause",
                        symbol=symbol,
                        events_60s=_vc_events_60,
                        quiet_minutes=round(_quiet_s / 60.0, 1),
                        evidence="quiet_22pct_wr_neg4.21_active_50pct_pos1.83")
            return

        # ── Filter 6: Order flow multiplier + tiered coherence gate ─────────────
        #
        # Step A — Order flow multiplier (0.7× / 1.0× / 1.2×)
        # Source: TradeFlowStore — already computed for terminal display; zero extra I/O.
        #   delta()           = buy_volume - sell_volume (60s window)  → net pressure
        #   aggressor_ratio() = buy_volume / total_volume              → aggressor fraction
        #
        # Thresholds (from live data: ARB 0.58, OP 0.53 = bullish; LINK 0.16, AVAX 0.22 = bearish):
        #   bullish: net_flow > 0  AND ratio > 0.45
        #   bearish: net_flow < 0  AND ratio < 0.35
        #   neutral: everything else
        #
        # Multiplier is applied to state.coherence_score to get _effective_coherence.
        # Flow confirms direction → 1.2× (boosts weak signals over threshold)
        # Flow contradicts       → 0.7× (demotes strong signals below threshold)
        _tfs = trade_flow_stores.get(symbol)
        _flow_mult = 1.0
        if _tfs is not None:
            try:
                _net_flow  = _tfs.delta(window_ms=60000)
                _agg_ratio = _tfs.aggressor_ratio(window_ms=60000)
                _flow_bull = _net_flow > 0 and _agg_ratio > 0.45
                _flow_bear = _net_flow < 0 and _agg_ratio < 0.35
                if (_flow_bull and _qf_side == "long") or (_flow_bear and _qf_side == "short"):
                    _flow_mult = 1.2   # flow confirms direction
                elif (_flow_bull and _qf_side == "short") or (_flow_bear and _qf_side == "long"):
                    _flow_mult = 0.7   # flow contradicts direction
                if _flow_mult != 1.0:
                    logger.info("order_flow_coherence_adjusted",
                                symbol=symbol, direction=_qf_side,
                                net_flow=round(_net_flow, 2),
                                agg_ratio=round(_agg_ratio, 3),
                                flow_mult=_flow_mult,
                                raw_coherence=round(state.coherence_score, 3))
            except Exception:
                pass   # TradeFlowStore not populated yet (startup) → neutral

        _effective_coherence = state.coherence_score * _flow_mult * _htf_confused_penalty
        # Fundamental bias coherence boost from DB (e.g. TSMC guidance, AI cycle)
        _cal_coherence_add = getattr(_cal_state, "coherence_add", 0.0) if _cal_state else 0.0
        if _cal_coherence_add != 0.0:
            _effective_coherence = _effective_coherence + _cal_coherence_add

        # ORACLE pre-cascade cluster boost — smart money positioning intelligence.
        # Fires when ≥3 of 4 cross-venue sub-signals (VPIN, OI, basis, funding) align.
        # 3/4 → +0.8 coherence  |  4/4 → +1.5 coherence
        # Only fires when oracle direction matches signal direction — no false boosts.
        if _sig_dir in ("long", "short") and getattr(config, 'oracle_enabled', True):
            _oracle_boost = _oracle_engine.get_coherence_boost(symbol, _sig_dir)
            if _oracle_boost > 0.0:
                _pre_oracle = _effective_coherence
                _effective_coherence = min(10.0, _effective_coherence + _oracle_boost)
                logger.info("oracle_cluster_boost_applied",
                            symbol    = symbol,
                            direction = _sig_dir,
                            boost     = _oracle_boost,
                            original  = round(_pre_oracle, 3),
                            effective = round(_effective_coherence, 3))

        # Propagate adjusted coherence to candidate so the risk engine's Gate 5
        # reads the post-adjustment value, not the raw signal-generation value.
        candidate.coherence_score = _effective_coherence

        # ── Kant + Nietzsche leading-sector concentration boost ───────────────
        # Kant has already validated structure (HTF gate passed). Nietzsche now
        # applies will-to-power sizing: signal in the LEADING sector of the
        # current regime = highest conviction trade available. 1.5× amplification.
        # Universal: works for any leading sector across all coin pairs and regimes.
        # Only applies to direction-aligned signals (regime gate above ensures
        # leading-sector short is already blocked).
        if not _is_aftermath_trade:
            _kant_rs = regime_engine.last_state()
            if _kant_rs is not None and _kant_rs.confidence >= 0.60:
                _sym_cat  = config.ASSET_CONFIG.get(symbol, {}).get("category", "none")
                _lead_cat = _kant_rs.leading_category
                _lag_cat  = _kant_rs.lagging_category
                _conc_mult = 1.0
                if _lead_cat not in ("none", "unknown", "") and _sym_cat == _lead_cat and _sig_dir == "long":
                    _conc_mult = 1.5  # leading sector long — ride the momentum
                elif _lag_cat not in ("none", "unknown", "") and _sym_cat == _lag_cat and _sig_dir == "short":
                    _conc_mult = 1.5  # lagging sector short — same conviction, fade the weak
                if _conc_mult != 1.0:
                    _conc_notional = candidate.entry_price * candidate.size * _conc_mult
                    if _conc_notional <= config.max_notional_usd:
                        candidate.size = round(candidate.size * _conc_mult, 8)
                        candidate.initial_margin = round(candidate.initial_margin * _conc_mult, 8)
                        logger.info("regime_concentration_boost",
                                    symbol=symbol, direction=_sig_dir,
                                    category=_sym_cat, leading=_lead_cat, lagging=_lag_cat,
                                    regime=_kant_rs.regime, mult=_conc_mult,
                                    new_notional=round(_conc_notional, 2),
                                    note="kant_validated_nietzsche_sized")

        # ── Liq conviction amplifier ──────────────────────────────────────────
        # Extreme cascade (expansion + zscore>3.5) aligned with trade direction
        # + confident regime (>0.6) → 2× size. Captures full momentum of spike.
        # Cap at max_notional_usd. Aftermath bypass: they're already correctly sized.
        if _sig_dir in ("long", "short") and not _is_aftermath_trade and liq_engine is not None:
            _lca_snap = liq_engine.get_phase_snapshot("")  # market-wide
            _lca_rs = regime_engine.last_state()
            if (_lca_snap.phase.value == "expansion"
                    and _lca_snap.zscore > 3.5
                    and _lca_rs is not None and _lca_rs.confidence > 0.6
                    and _lca_snap.last_direction in ("bearish", "bullish")):
                _lca_aligned = (
                    (_lca_snap.last_direction == "bearish" and _sig_dir == "short") or
                    (_lca_snap.last_direction == "bullish" and _sig_dir == "long")
                )
                if _lca_aligned:
                    _lca_new_notional = candidate.entry_price * candidate.size * 2.0
                    if _lca_new_notional <= config.max_notional_usd:
                        candidate.size = round(candidate.size * 2.0, 8)
                        candidate.initial_margin = round(candidate.initial_margin * 2.0, 8)
                        logger.info("liq_conviction_amplifier",
                                    symbol=symbol, direction=_sig_dir,
                                    zscore=round(_lca_snap.zscore, 2),
                                    cascade_dir=_lca_snap.last_direction,
                                    regime=_lca_rs.regime,
                                    new_notional=round(_lca_new_notional, 2))

        # Step B — Tiered coherence gate
        #
        # Tier 1 (≥5.0): Unconditional pass — strong signal, all conditions met.
        #                 60% WR +$0.77 net in backtest.
        # Tier 2 (≥4.0): Pass — note: Filter 2 already eliminated counter-HTF cases,
        #                 so any signal reaching here is HTF-aligned or HTF-neutral.
        # Tier 3 (≥3.5): Speculative — only enter during active cascade (zscore > 2.0).
        #                 Without cascade these entries show <25% WR at this score band.
        # Tier 4 (<3.5):  Block — <3.5 has 22% WR -$3.82 net historically.
        #                 Raises the effective floor from the current config.min_coherence=3.0.
        # ── Session coherence floor — overrides tier thresholds in restricted sessions ──
        _sess_coh_min = session_manager.get_coherence_minimum()
        # Per-symbol alpha floor elevation based on rolling win rate (Gap 3)
        _alpha_floor_add = _signal_guard.get_coherence_floor_add(symbol)
        if _alpha_floor_add > 0.0:
            _sess_coh_min = max(_sess_coh_min, config.live_min_coherence + _alpha_floor_add)
            logger.debug("alpha_floor_elevated", symbol=symbol,
                         floor_add=_alpha_floor_add, new_floor=round(_sess_coh_min, 1))
        # Regime leading-category coherence discount: when regime is clear (conf≥0.8)
        # and symbol is the leading sector, reduce floor by 0.5 — regime mult already
        # gives 1.2× size; floor alignment prevents blocking the same trades we want.
        _rs_now = regime_engine.last_state()
        if (_rs_now is not None and _rs_now.confidence >= 0.8
                and _rs_now.regime not in ("transitioning", "confused")
                and ASSET_CATEGORIES.get(symbol) == _rs_now.leading_category):
            _sess_coh_min = max(3.5, _sess_coh_min - 0.5)
        # Aftermath coherence override: honour the lowered floor set at trade-tagging
        _coh_override = getattr(candidate, "coherence_override", 0.0)
        if _coh_override > 0.0:
            _sess_coh_min = min(_sess_coh_min, _coh_override)

        # Per-symbol minimum coherence floor (evidence-based audit Apr-2026).
        # Symbols with no demonstrated edge at low conviction are elevated here.
        _sym_coh_min = _SYMBOL_MIN_COHERENCE.get(symbol, 0.0)
        if _sym_coh_min > 0.0 and _effective_coherence < _sym_coh_min:
            logger.info("symbol_coherence_floor_blocked",
                        symbol=symbol,
                        effective_coherence=round(_effective_coherence, 3),
                        symbol_minimum=_sym_coh_min,
                        evidence="per_symbol_floor_audit_apr2026")
            return

        if _effective_coherence < _sess_coh_min:
            logger.info("session_coherence_floor",
                        symbol=symbol,
                        session=session_manager.get_current_session(),
                        effective_coherence=round(_effective_coherence, 3),
                        session_minimum=_sess_coh_min)
            return

        # ── Coherence tier gate (updated Apr-2026: global floor lowered to 3.0) ──
        # Tier 1 (≥5.0): unconditional pass — demonstrated edge at this band.
        # Tier 2 (≥4.0): requires cascade confirmation — edge only with liquidation flow.
        # Tier 3 (≥3.0): speculative — strong cascade (zscore > 0.5) only.
        # Tier 4 (<3.0):  hard block — 22% WR -$3.82 net historically.
        if _effective_coherence >= 5.0:
            pass   # Tier 1 — unconditional pass
        elif _effective_coherence >= 4.0:
            # Tier 2: edge only when cascade/liquidation flow is active
            if _vc_zscore < 0.5:
                logger.info("quant_filter_blocked",
                            reason="tier2_coherence_no_cascade",
                            symbol=symbol,
                            effective_coherence=round(_effective_coherence, 3),
                            raw_coherence=round(state.coherence_score, 3),
                            cascade_zscore=round(_vc_zscore, 2),
                            evidence="4.0_5.0_band_edge_only_with_liq_flow")
                return
        elif _effective_coherence >= 3.0:
            # Tier 3: speculative — require cascade confirmation
            if _vc_zscore < 0.5:
                logger.info("quant_filter_blocked",
                            reason="tier3_coherence_no_cascade",
                            symbol=symbol,
                            effective_coherence=round(_effective_coherence, 3),
                            raw_coherence=round(state.coherence_score, 3),
                            flow_mult=round(_flow_mult, 2),
                            cascade_zscore=round(_vc_zscore, 2),
                            evidence="speculative_band_3.0_4.0_only_profitable_during_cascade")
                return
        else:
            # Tier 4: block — below effective floor
            logger.info("quant_filter_blocked",
                        reason="coherence_below_floor",
                        symbol=symbol,
                        effective_coherence=round(_effective_coherence, 3),
                        raw_coherence=round(state.coherence_score, 3),
                        flow_mult=round(_flow_mult, 2),
                        floor=3.0,
                        evidence="below_3.0_22pct_wr_neg3.82_net")
            return

        # ── Personality assessment — Phase 12 ────────────────────────────────────
        # Runs AFTER coherence gate passes — personality gates on top of coherence,
        # never before it. Hot path cost: ~0.1ms (PersonalityContextCache design).
        _t_pers = time.perf_counter()

        # Update ATR context for this symbol before building personality context
        context_cache.update_atr(symbol, _atr_ratio)

        # Update regime from current signal state (available per-signal).
        # Preserve confidence from SSI loop — MarketState has no regime_confidence
        # field, so getattr fallback would overwrite SSI-computed confidence on
        # every signal tick with a constant 0.5. Read live value from cache instead.
        _regime_str = str(state.regime)
        _regime_conf = float(context_cache._regime_confidence or 0.5)
        context_cache.update_regime(regime=_regime_str, confidence=_regime_conf)
        _ui_state.update_regime(regime=_regime_str, confidence=_regime_conf)
        interpreter.set_regime_confidence(_regime_conf)

        # RPC health and freeze state from ValueChain monitor
        _vc_fail    = int(_vc_status.get("consecutive_failures", 0)) if vc_monitor else 0
        _rpc_health = max(0.0, 1.0 - _vc_fail / 10.0)
        _freeze_active = (
            bool(_vc_status.get("cascade_active", False)) and
            bool(_vc_status.get("freeze_active", False))
        ) if vc_monitor else False
        # Feed freeze state and rpc health into cache (idempotent — no-op if unchanged)
        if _freeze_active:
            context_cache.update_freeze(True)
        context_cache.update_rpc_health(_vc_fail, recovered=not _freeze_active)

        # Build lightweight PersonalityContext from cached slow fields + hot fields
        _personality_ctx = context_cache.build(
            symbol=symbol,
            coherence=_effective_coherence,
            direction=_qf_side,
            htf=_htf,
        )

        # Assess personality — hysteresis applied internally (3-period, SHIELD instant)
        _personality_params = personality_engine.assess(symbol, _personality_ctx)
        _personality_name   = _personality_params.name.value

        # ── Session strategy gate ─────────────────────────────────────────────────
        if not session_manager.is_strategy_allowed(_personality_name):
            logger.info("session_strategy_not_allowed",
                        symbol=symbol,
                        session=session_manager.get_current_session(),
                        strategy=_personality_name,
                        allowed=session_manager.get_allowed_strategies())
            return

        _pers_ms = (time.perf_counter() - _t_pers) * 1000
        if _pers_ms > 1.0:
            logger.warning("personality_latency_high",
                           symbol=symbol, ms=round(_pers_ms, 2))

        # Push personality to display — cheap dict update, hot path safe
        _pmap = display._display_cache.get("personality_map")
        if _pmap is None:
            _pmap = {}
            display._display_cache["personality_map"] = _pmap
        _pmap[symbol] = _personality_name

        # SHIELD: hard block — all market conditions prohibit new entries
        if not _personality_params.directional and _personality_name == "SHIELD":
            logger.info("personality_shield_blocked",
                        symbol=symbol, direction=_qf_side,
                        coherence=round(_effective_coherence, 2))
            return

        # COIL: block directional trades; arb/funding strategies are still allowed
        if not _personality_params.directional and _personality_name == "COIL":
            _is_arb = "arb" in _strategy_tag or "funding" in _strategy_tag
            if not _is_arb:
                logger.info("personality_coil_directional_blocked",
                            symbol=symbol, direction=_qf_side,
                            atr_vs_baseline=round(_atr_ratio, 3),
                            coherence=round(_effective_coherence, 2))
                return

        # Apply personality size multiplier (e.g. AFTERMATH 1.0×, APEX 1.0×, SCOUT 0.5×)
        if _personality_params.size_mult > 0 and _personality_params.size_mult != 1.0:
            candidate.size = round(candidate.size * _personality_params.size_mult, 8)
            candidate.initial_margin = round(
                candidate.initial_margin * _personality_params.size_mult, 8
            )

        logger.info("personality_assigned",
                    symbol=symbol,
                    personality=_personality_name,
                    direction=_qf_side,
                    size_mult=_personality_params.size_mult,
                    coherence=round(_effective_coherence, 2),
                    cascade_phase=_vc_phase,
                    directional=_personality_params.directional)

        # ── Signal deduplication — reject exact duplicates within 30s window ──────
        # Prevents the same (symbol + direction + strategy + regime) from executing
        # twice in one burst. Cascade strategy uses a tighter 10s window.
        _sig_direction = getattr(state, 'trade_direction', 'none')
        if signal_deduplicator.is_duplicate(symbol, _sig_direction, _strategy_tag, state.regime):
            logger.debug("signal_deduped", symbol=symbol, direction=_sig_direction, strategy=_strategy_tag)
            return
        signal_deduplicator.record(symbol, _sig_direction, _strategy_tag, state.regime)

        # ── PHILOSOPHICAL STACK: kant_frame → conviction_computed → nietzsche_output
        # ── KANT LAYER — structure-aware threshold calibration ─────────────────
        # Reads fields already on hot path: zero extra I/O, ~0.05ms.
        _kant_frame = kant_engine.assess(
            symbol             = symbol,
            atr_vs_baseline    = _atr_ratio,
            cascade_phase      = _vc_phase,
            cascade_zscore     = _vc_zscore,
            basis_stress_count = _personality_ctx.basis_stress_count,
            rpc_health         = _personality_ctx.rpc_health_score,
            regime             = state.regime,
            liq_60s            = _vc_events_60 if _vc_events_60 != 999 else 0,
        )
        logger.info("kant_frame",
            symbol     = symbol,
            structure  = _kant_frame.structure.value,
            confidence = _kant_frame.confidence,
            coherence_min = _kant_frame.coherence_min,
            order_type    = _kant_frame.order_type,
            size_cap      = _kant_frame.size_cap,
        )
        _ui_state.update_kant(
            symbol        = symbol,
            structure     = _kant_frame.structure.value,
            confidence    = _kant_frame.confidence,
            coherence_min = _kant_frame.coherence_min,
            order_type    = _kant_frame.order_type,
            size_cap      = _kant_frame.size_cap,
        )

        # ── CONVICTION LAYER — aggregate signal evidence to [0,1] ─────────────
        _historical_wr = perf.get_win_rate(_personality_name)
        _is_cascade_active = _vc_phase in ("trigger", "expansion", "exhaustion")
        _flow_store  = trade_flow_stores.get(symbol)
        _flow_ratio  = (_flow_store.aggressor_ratio() if _flow_store else 0.5)

        # ── Agent alignment — poll fresh agent votes (max 20min staleness) ────
        # Excludes RegimeAgent (regime already captured in regime_aligned param).
        # Each agent casts long/short/neutral; count how many align with signal direction.
        _ALIGNMENT_AGENTS = {"macro", "structure", "micro", "funding", "ssi"}
        _now_ms_align = int(time.time() * 1000)
        _stale_cutoff = 20 * 60 * 1000   # 20 minutes in ms
        _ag_aligned = 0.0
        _ag_opposing = 0.0
        _ag_voted = 0.0
        for _ag_name, _ag_obj in _sig_agents.items():
            if _ag_name not in _ALIGNMENT_AGENTS:
                continue
            _ag_out = getattr(_ag_obj, "_last_outputs", {}).get(symbol)
            if _ag_out is None:
                continue
            if (_now_ms_align - _ag_out.timestamp_ms) > _stale_cutoff:
                continue
            if not _ag_out.fired:
                continue
            # Weight each agent vote by their historical accuracy (floor 0.5)
            _ag_weight = max(0.5, getattr(getattr(_ag_obj, "_accuracy", None), "accuracy", 0.5))
            if _ag_out.direction == _sig_direction:
                _ag_aligned += _ag_weight
            elif _ag_out.direction in ("long", "short"):
                _ag_opposing += _ag_weight
            _ag_voted += _ag_weight
        # [0,1]: 0.5=neutral, 1.0=all agree, 0.0=all oppose
        if _ag_voted > 0:
            _agent_alignment = ((_ag_aligned - _ag_opposing) / _ag_voted + 1.0) / 2.0
        else:
            _agent_alignment = 0.5

        _conviction  = compute_conviction(
            coherence       = _effective_coherence,
            regime_aligned  = state.regime not in ("confused",),
            order_flow_ratio= _flow_ratio,
            cascade_active  = _is_cascade_active,
            cascade_zscore  = _vc_zscore,
            historical_wr   = _historical_wr,
            kant_confidence = _kant_frame.confidence,
            agent_alignment = _agent_alignment,
        )

        # ── Prediction calibration gate — reduce conviction when personality is
        # systematically overconfident (calibration error > 5% MSE over last 50).
        try:
            _cal_result = build_calibration_result(prediction_store, _personality_name)
            if _cal_result is not None and _cal_result.budget_multiplier < 1.0:
                _conviction = max(0.0, min(1.0, _conviction * _cal_result.budget_multiplier))
                logger.debug("conviction_calibrated",
                             symbol=symbol,
                             personality=_personality_name,
                             budget_mult=round(_cal_result.budget_multiplier, 3),
                             conviction_after=round(_conviction, 3))
        except Exception:
            pass  # calibration is non-critical — never block a trade

        logger.info("conviction_computed",
            symbol          = symbol,
            conviction      = _conviction,
            coherence       = round(_effective_coherence, 2),
            hist_wr         = round(_historical_wr, 3),
            agent_alignment = round(_agent_alignment, 3),
            agents_voted    = round(_ag_voted, 2),
            agents_aligned  = round(_ag_aligned, 2),
            agents_opposing = round(_ag_opposing, 2),
        )
        _ui_state.update_conviction(
            symbol     = symbol,
            conviction = _conviction,
            coherence  = _effective_coherence,
            hist_wr    = _historical_wr,
        )

        # Apply per-symbol / per-regime / per-strategy adaptive coherence floor (feedback v3).
        # Priority: symbol > regime > global (strategy fast-block removed — Nietzsche handles
        # loss-streak sizing continuously via Will Table; per-strategy binary blocks are pro-cyclical).
        config.min_coherence = feedback.get_adjusted_threshold(
            symbol=symbol, regime=state.regime, strategy_tag=_strategy_tag
        )

        # Build kant_overrides dict for risk engine — passes Kant thresholds
        # without touching risk engine internals. Coherence min takes max
        # with adaptive calibrator inside _gate_coherence().
        _kant_overrides = {
            "coherence_min":       _kant_frame.coherence_min,
            "kant_confidence":     _kant_frame.confidence,
            "basis_stress_weight": _kant_frame.basis_stress_weight,
            "atr_baseline_min":    _kant_frame.atr_baseline_min,
        }

        # Risk validation — all gates with full context + Kant overrides
        _t_risk_start = time.perf_counter()
        approved, reason = await risk_engine.validate(
            candidate, balance,
            regime=_risk_regime,
            funding_rate=_funding_rate,
            current_atr=candidate.atr,
            avg_atr=_avg_atr,
            orderbook_store=orderbook_stores.get(symbol),
            drawdown_manager=drawdown_manager,
            kant_overrides=_kant_overrides,
        )
        _t_risk_done = time.perf_counter()

        # Apply Gate A regime multiplier — structural alignment sizing adjustment
        # 0.75× counter-trend (BEAR+long / BULL+short) | 1.15× aligned | 1.0× ranging
        if approved and risk_engine._regime_mult != 1.0:
            candidate.size = round(candidate.size * risk_engine._regime_mult, 8)
            candidate.initial_margin = round(
                candidate.initial_margin * risk_engine._regime_mult, 8
            )

        # Apply Gate C funding multiplier to position size
        if approved and risk_engine._funding_mult != 1.0:
            candidate.size = round(candidate.size * risk_engine._funding_mult, 8)
            candidate.initial_margin = round(
                candidate.initial_margin * risk_engine._funding_mult, 8
            )

        # ── Regime-first sizing override ─────────────────────────────────────
        # Applies Kent structure: geopolitical_stress/stagflation lock non-leading
        # assets to 0×; cex_flow/alt_season/btc_dominance bias sector sizing.
        # Aftermath trades bypass this gate — they represent confirmed cascade
        # exhaustion and have already passed 14 risk gates; regime sizing would
        # kill legitimate high-conviction entries at min_trade_notional boundary.
        if approved and not _is_aftermath_trade:
            _rs = regime_engine.last_state()
            # Only apply regime mult when confidence is high enough to trust classification.
            # Below 0.60, transitioning/confused readings are noise — old risk_engine gate
            # already handled sizing; applying a second 0.5× here double-gates and kills
            # trades that all 14 gates approved.
            if _rs is not None and _rs.confidence >= 0.60:
                _rmv2 = _regime_mult_engine.get_new_entry_multiplier(symbol, _rs)
                # For hard-block regimes (geo_stress, stagflation) that give 0×:
                # alignment gate already blocks LONGS in lagging sectors.
                # Shorts during geopolitical stress / stagflation may be the correct
                # Nietzsche trade (crypto falls as energy/gold rises) — allow at 0.8×.
                if _rmv2 == 0.0 and _sig_dir == "short":
                    _rmv2 = 0.8
                if _rmv2 != 1.0:
                    _test_notional = candidate.entry_price * candidate.size * _rmv2
                    if _test_notional >= config.min_trade_notional_usd:
                        candidate.size = round(candidate.size * _rmv2, 8)
                        candidate.initial_margin = round(candidate.initial_margin * _rmv2, 8)
                    else:
                        logger.info("regime_lock_preserves_size",
                                    symbol=symbol, regime=_rs.regime,
                                    confidence=round(_rs.confidence, 3),
                                    mult=_rmv2, direction=_sig_dir,
                                    reason="post_mult_below_min_notional")
                        # Preserve original size so small-account trades can still execute

        # ── XAUT thermometer — macro compass for all crypto ───────────────────
        # Gold falling (XAUT short) = risk-on → amplify crypto longs 1.10×
        # Gold rising  (XAUT long)  = risk-off → reduce crypto longs 0.90×
        if approved:
            _xm = _xaut_thermometer.get_crypto_multiplier(candidate.side, symbol)
            if _xm != 1.0:
                candidate.size = round(candidate.size * _xm, 8)
                candidate.initial_margin = round(candidate.initial_margin * _xm, 8)

        # ── Regime concentration — Kent says: when regime confidence is high, ──
        # concentrate force. 70% into ONE position when alt_season/trending/etc.
        # at confidence ≥ 0.85 with coherence ≥ 7.0. This is the will to power.
        _CONC_REGIMES = frozenset({"alt_season", "trending", "geopolitical_stress",
                                   "cex_flow", "btc_dominance", "risk_on"})
        if approved:
            _conc_rs = regime_engine.last_state()
            _conc_conf = float(getattr(_conc_rs, 'confidence', 0.0) or 0.0) if _conc_rs else 0.0
            _conc_regime = str(getattr(_conc_rs, 'regime', '') or '') if _conc_rs else ''
            _conc_pct = 0.0
            if _conc_conf >= 0.85 and _conc_regime in _CONC_REGIMES and _sig_coh >= 7.0:
                _conc_pct = 0.70
            elif _conc_conf >= 0.70 and _conc_regime in _CONC_REGIMES and _sig_coh >= 7.0:
                _conc_pct = 0.50
            if _conc_pct > 0.0 and balance > 0 and candidate.entry_price > 0:
                _conc_notional = max(balance * _conc_pct, config.min_trade_notional_usd)
                _conc_raw_size  = _conc_notional / candidate.entry_price
                _conc_leverage  = getattr(candidate, 'leverage', config.default_leverage)
                candidate.size         = round(_conc_raw_size, 8)
                candidate.initial_margin = round(_conc_notional / _conc_leverage, 8)
                logger.info("concentration_active",
                            regime=_conc_regime, regime_confidence=round(_conc_conf, 3),
                            symbol=symbol, coherence=round(_sig_coh, 2),
                            position_size=round(_conc_notional, 2),
                            percent_of_account=_conc_pct)

        # ── Cascade size multiplier — confirmed cascade = add conviction ─────
        # trigger phase + liq>30 → 1.3×  |  cascade active + zscore≥1.5 → 1.5×
        # (expansion is blocked upstream by Filter 4 at zscore>2.5 so never reaches here)
        if approved and vc_monitor is not None:
            _casc_active = bool(_vc_status.get("cascade_active", False))
            _casc_mult = 1.0
            if _vc_phase == "trigger" and _vc_events_60 > 30 and _vc_events_60 != 999:
                _casc_mult = 1.3
            elif _casc_active and _vc_zscore >= 1.5:
                _casc_mult = 1.5
            if _casc_mult != 1.0:
                # Direction check: only add conviction when cascade aligns with trade.
                # bearish cascade (longs liq'd) → price falls → only SHORT gets mult.
                # bullish cascade (shorts liq'd) → price rises → only LONG gets mult.
                _casc_trade_dir = "long" if _vc_direction == "bullish" else (
                    "short" if _vc_direction == "bearish" else None)
                if _casc_trade_dir is not None and _casc_trade_dir != _sig_dir:
                    logger.info("cascade_mult_skipped_counter_direction",
                                symbol=symbol, cascade_dir=_vc_direction,
                                trade_dir=_sig_dir, would_have_been=_casc_mult)
                    _casc_mult = 1.0
            if _casc_mult != 1.0:
                candidate.size = round(candidate.size * _casc_mult, 8)
                candidate.initial_margin = round(candidate.initial_margin * _casc_mult, 8)
                logger.info("cascade_size_mult", symbol=symbol,
                            cascade_mult=_casc_mult, phase=_vc_phase,
                            zscore=round(_vc_zscore, 2), liq_60s=_vc_events_60)

        # ── Elite 5:1 TP extension — coherence ≥ 8.0 + oracle cluster + cascade ─
        # Only fires when all three confirm: oracle smart money, cascade momentum, elite coherence.
        if approved and _sig_coh >= 8.0:
            _elite_oracle = _oracle_engine.get_fusion_mult(_sig_dir)
            if _elite_oracle > 1.0 and _vc_zscore >= 1.5:
                _r_dist_elite = abs(candidate.entry_price - candidate.stop_price)
                if _r_dist_elite > 0:
                    if candidate.side == "long":
                        candidate.tp1_price = candidate.entry_price + _r_dist_elite * 2.0
                        candidate.tp2_price = candidate.entry_price + _r_dist_elite * 5.0
                        candidate.tp3_price = candidate.entry_price + _r_dist_elite * 7.0
                    else:
                        candidate.tp1_price = candidate.entry_price - _r_dist_elite * 2.0
                        candidate.tp2_price = candidate.entry_price - _r_dist_elite * 5.0
                        candidate.tp3_price = candidate.entry_price - _r_dist_elite * 7.0
                    logger.info("elite_5to1_brackets", symbol=symbol,
                                coherence=round(_sig_coh, 2),
                                oracle_fusion=round(_elite_oracle, 3),
                                tp1=round(candidate.tp1_price, 4),
                                tp2=round(candidate.tp2_price, 4),
                                tp3=round(candidate.tp3_price, 4))

        _ui_feed_agent = {
            "crypto": "perp", "commodity": "gold",
            "equity": "equity", "equity_index": "equity",
        }.get(_get_asset_class(symbol), "perp")

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
            _ui_state.add_feed_entry(
                agent          = _ui_feed_agent,
                symbol         = symbol,
                direction      = state.trade_direction or candidate.side,
                score          = round(_effective_coherence, 2),
                result         = "REJECTED",
                reason         = reason,
                personality    = _personality_name,
                kant_structure = _kant_frame.structure.value,
                will_state     = None,
                conviction     = round(_conviction, 3),
                ml_prob        = None,
            )
            return

        # ── PREDICTION MARKET — record signal; check cross-agent bet ──────────
        # add_pending() is synchronous and hot-path safe (queue.put_nowait).
        # check_bet() scans drained records for a matching partner from a different
        # agent/personality — returns BetResult (p_joint ≥ 0.70) or None.
        _agent_type = {
            "crypto": "perp", "commodity": "gold",
            "equity": "equity", "equity_index": "equity",
        }.get(_get_asset_class(symbol), "perp")
        _pred_record = PredictionRecord(
            id              = f"{symbol}_{int(time.time() * 1000)}",
            agent           = _agent_type,
            personality     = _personality_name,
            symbol          = symbol,
            direction       = candidate.side,
            confidence      = _conviction,
            ml_probability  = _conviction,
            coherence       = _effective_coherence,
            entry_price     = candidate.entry_price,
            predicted_exit  = candidate.tp1_price,
            timestamp_ms    = int(time.time() * 1000),
        )
        # NOTE: add_pending() is deferred until AFTER Nietzsche runs so
        # will_state can be stamped on the record before it enters the store.

        _now_ms_pm  = _pred_record.timestamp_ms
        _existing_preds = [
            r for r in prediction_store._records
            if r.outcome is None
            and r.symbol == symbol
            and (_now_ms_pm - r.timestamp_ms) < 300_000
        ]
        class _BudgetMgr:
            """Inline budget manager — reads live balance from closure for bet sizing."""
            @property
            def _total_balance(self_bm):  # noqa: N805
                return _cached_balance[0]
            def get_budget(self_bm, agent, personality):  # noqa: N805
                bal = _cached_balance[0]
                return max(15.0, bal * float(getattr(config, "risk_pct", 0.05) or 0.05))

        _bet_result = bet_engine.check_bet(_pred_record, _existing_preds, budget_manager=_BudgetMgr())
        if _bet_result:
            logger.info("cross_agent_bet",
                        symbol=symbol,
                        agent_a=_bet_result.agent_a,
                        agent_b=_bet_result.agent_b,
                        p_joint=round(_bet_result.p_joint, 3),
                        size_mult=_bet_result.combined_size_mult)
            display.push_bet_event(
                symbol    = symbol,
                agent_a   = _bet_result.agent_a,
                agent_b   = _bet_result.agent_b,
                p_joint   = _bet_result.p_joint,
                size_mult = _bet_result.combined_size_mult,
            )

        # ── NIETZSCHE LAYER — continuous conviction-based sizing ───────────────
        # Runs AFTER all hard gates pass. Never blocks — only scales size.
        # Persistent memory: streaks read from journal-backed perf tracker.
        _t_nietzsche_start = time.perf_counter()
        _dd_decimal  = dd_tracker.session_drawdown_pct / 100.0
        _win_streak, _loss_streak = perf.get_streaks(_personality_name)
        _mark_px = getattr(state, 'mark_price', candidate.entry_price) or candidate.entry_price
        _n_output = nietzsche_engine.compute(
            drawdown_pct     = _dd_decimal,
            win_streak       = _win_streak,
            loss_streak      = _loss_streak,
            conviction_score = _conviction,
            coherence        = _effective_coherence,
            kant_frame       = _kant_frame,
            base_size_units  = candidate.size,
            min_notional_usd = config.min_trade_notional_usd,
            mark_price       = _mark_px,
            balance          = balance,
            symbol           = symbol,
        )
        logger.info("nietzsche_output",
            symbol       = symbol,
            will_state   = _n_output.will_state.value,
            size_mult    = _n_output.size_multiplier,
            order_type   = _n_output.order_type,
            adjusted_size= round(_n_output.adjusted_size, 6),
            reason       = _n_output.reason,
        )
        _ui_state.update_nietzsche(
            symbol     = symbol,
            will_state = _n_output.will_state.value,
            size_mult  = _n_output.size_multiplier,
            order_type = _n_output.order_type,
            reason     = _n_output.reason,
            conviction = _conviction,
        )

        # ── PREDICTION MARKET — stamp will_state and submit record ────────────
        # add_pending() is deferred here (not at PredictionRecord construction)
        # so will_state from Nietzsche is captured on every prediction record.
        _pred_record.will_state = _n_output.will_state.value
        prediction_store.add_pending(_pred_record)

        # DORMANT = mirrors dd_tracker halt but from journal-based streak perspective.
        # Exception: cascade personalities (APEX/AFTERMATH) bypass DORMANT — the
        # institutional cascade event IS the conviction; personal drawdown state is
        # secondary to a real liquidation wave. Entry is allowed at 25% survival size.
        if _n_output.will_state == _WillState.DORMANT:
            _is_cascade_pers = _personality_name in ("APEX", "AFTERMATH")
            if not _is_cascade_pers:
                logger.info("nietzsche_dormant_halt",
                            symbol=symbol, drawdown_pct=round(_dd_decimal * 100, 2))
                return
            logger.info("nietzsche_dormant_cascade_bypass",
                        symbol=symbol,
                        personality=_personality_name,
                        drawdown_pct=round(_dd_decimal * 100, 2))
            candidate.size = round(candidate.size * 0.25, 8)
            candidate.initial_margin = round(
                candidate.size / getattr(candidate, "leverage", config.default_leverage), 8
            )
            candidate.order_type = "limit"

        if not _n_output.min_notional_ok:
            logger.info("nietzsche_min_notional_fail",
                        symbol=symbol, adjusted_size=_n_output.adjusted_size,
                        mark_price=_mark_px, min_notional=config.min_trade_notional_usd)
            return

        # Apply Nietzsche-adjusted size — overrides all previous size multipliers
        # since it already incorporates DD, streak, conviction, and Kant cap.
        if _n_output.adjusted_size > 0 and _n_output.adjusted_size != candidate.size:
            candidate.size           = _n_output.adjusted_size
            candidate.initial_margin = round(
                candidate.size / getattr(candidate, 'leverage', config.default_leverage), 8
            )
        # Propagate Kant/Nietzsche order_type to the candidate so _place_entry_order()
        # can dispatch: "market" → IOC market fill, "probe" → aggressive limit half-size.
        candidate.order_type = _n_output.order_type

        # Apply cross-agent bet amplification (1.5×) when two independent agents
        # agree on the same symbol + direction with joint P ≥ 0.70.
        # Applied after Nietzsche so bet can only add on top of already-validated sizing.
        if _bet_result is not None:
            candidate.size           = round(candidate.size * _bet_result.combined_size_mult, 8)
            candidate.initial_margin = round(
                candidate.size / getattr(candidate, 'leverage', config.default_leverage), 8
            )
            logger.info("bet_size_applied",
                        symbol=symbol, mult=_bet_result.combined_size_mult,
                        final_size=round(candidate.size, 6))

        # Feed entry for gate-passed signal (will_state now known from Nietzsche)
        _ui_state.add_feed_entry(
            agent          = _ui_feed_agent,
            symbol         = symbol,
            direction      = candidate.side,
            score          = round(_effective_coherence, 2),
            result         = "FILLED" if _n_output.size_multiplier > 0 else "REDUCED",
            reason         = _n_output.reason,
            personality    = _personality_name,
            kant_structure = _kant_frame.structure.value,
            will_state     = _n_output.will_state.value,
            conviction     = round(_conviction, 3),
            ml_prob        = None,
        )

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
            personality=_personality_name,
            reason=_n_output.reason,
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

        # Order deduplication: block re-entry if same symbol ordered in last 60s.
        # Prevents tick-rate signal bursts (5 ticks/s) from placing 5 orders in 1s.
        _ORDER_COOLDOWN_S = 60.0
        if _order_cooldown.get(symbol, 0) > time.time():
            _remaining = round(_order_cooldown[symbol] - time.time())
            logger.info("order_deduplicated",
                        symbol=symbol, cooldown_remaining=_remaining,
                        note="same symbol ordered within 60s — skipped")
            return

        # Add to candidate pool — _strategy_tag already resolved above for feedback gate.
        # Pool selects top-N by coherence score on each selection tick.
        # Discard happens after execution (or if the candidate ages out > 30s).
        _pool_score = getattr(state, 'coherence_score', getattr(state, 'weighted_score', 0.0))
        _candidate_pool.add(
            symbol=symbol,
            state=state,
            strategy_tag=_strategy_tag,
            score=_pool_score,
            direction=getattr(state, 'trade_direction', 'none'),
        )
        logger.debug("candidate_pool_queued",
                     symbol=symbol, strategy=_strategy_tag, score=round(_pool_score, 3),
                     pool_size=_candidate_pool.size(), best=round(_candidate_pool.best_score(), 3))

        # Pool selection: only proceed if this symbol is the current top candidate
        # (or tied). This prevents executing a weaker signal while a stronger one
        # from another symbol is waiting in the same selection window.
        _top = _candidate_pool.select(n=1)
        if _top and _top[0].symbol != symbol:
            logger.debug("candidate_pool_deferred",
                         symbol=symbol, score=round(_pool_score, 3),
                         top_symbol=_top[0].symbol, top_score=round(_top[0].score, 3))
            return

        # Remove from pool — about to execute
        _candidate_pool.discard(symbol)

        # Log decision only here — AFTER all early-return guards.
        # Calling log_decision before the cooldown check was creating one phantom
        # journal entry per signal tick (every 1s) during the 60s cooldown window.
        # Those entries had entry_price set but never received update_outcome(),
        # so they stayed outcome=None forever and poisoned performance stats.
        entry_id = journal.log_decision(
            state          = state,
            candidate      = candidate,
            approved       = approved,
            reason         = None,
            cal_state      = await calendar_engine.get_state(symbol),
            personality    = _personality_name,
            kant_structure = _kant_frame.structure.value,
            conviction     = _conviction,
            will_state     = _n_output.will_state.value,
            order_type_used= _n_output.order_type,
        )

        # Execute bracket — non-blocking background task.
        # Running place_bracket as a task means the event bus returns immediately
        # and can dispatch signals for OTHER symbols during the 60s fill wait.
        # Safety guard: never send an order for a symbol with no SoDEX ID.
        # fetch_symbol_ids() removes unlisted symbols from config.assets, but a
        # stale signal for a newly-pruned symbol could still arrive in the queue.
        _sym_id_check = SYMBOL_IDS.get(symbol, 0)
        if _sym_id_check == 0:
            logger.warning("order_blocked_no_symbol_id",
                           symbol=symbol, action="signal dropped — no SoDEX symbol ID")
            return

        # _pending_entry_symbols is added BEFORE task creation so that any signals
        # arriving before the first await point already see the lock.
        bracket = BracketOrder(
            candidate=candidate,
            account_id=str(NUMERIC_ACCOUNT_ID),
            symbol_id=_sym_id_check
        )
        _pending_entry_symbols.add(symbol)
        # Stamp cooldown immediately — blocks duplicates before bracket task resolves.
        # time_regime.cooldown_multiplier stretches window during event caution periods.
        _effective_cooldown = _ORDER_COOLDOWN_S * _time_regime.cooldown_multiplier
        _order_cooldown[symbol] = time.time() + _effective_cooldown
        # Record execution in guardian (daily counters + last-direction tracker)
        _exec_guardian.record_execution(symbol, _sig_dir)

        # ── Tria Bridge outbox: emit approved signal for GUI automation ────────
        try:
            _tria_signal = {
                "id": f"{symbol}_{int(time.time() * 1000)}",
                "symbol": symbol,
                "direction": _sig_dir,
                "size": round(candidate.size, 8),
                "leverage": getattr(candidate, "leverage", config.default_leverage),
                "entry_price": round(candidate.entry_price, 4),
                "stop_price": round(candidate.stop_price, 4) if candidate.stop_price else None,
                "tp1_price": round(candidate.tp1_price, 4) if candidate.tp1_price else None,
                "tp2_price": round(candidate.tp2_price, 4) if candidate.tp2_price else None,
                "tp3_price": round(candidate.tp3_price, 4) if candidate.tp3_price else None,
                "coherence_score": round(getattr(state, "coherence_score", 0.0), 3),
                "notional_usd": round(candidate.entry_price * candidate.size, 2),
                "timestamp": time.time(),
                "source": "aria",
            }
            _tria_outbox_path = os.path.join(os.path.dirname(__file__), "signals", "aria_outbox.json")
            os.makedirs(os.path.dirname(_tria_outbox_path), exist_ok=True)
            # Append to list (trim to last 200 to prevent unbounded growth)
            _existing: list = []
            if os.path.exists(_tria_outbox_path):
                try:
                    with open(_tria_outbox_path, "r", encoding="utf-8") as f:
                        _existing = _json_kingdom.load(f)
                    if not isinstance(_existing, list):
                        _existing = []
                except (_json_kingdom.JSONDecodeError, OSError):
                    _existing = []
            _existing.append(_tria_signal)
            _existing = _existing[-200:]
            _tria_outbox_tmp = _tria_outbox_path + ".tmp"
            with open(_tria_outbox_tmp, "w", encoding="utf-8") as f:
                _json_kingdom.dump(_existing, f)
            os.replace(_tria_outbox_tmp, _tria_outbox_path)
            logger.debug("tria_outbox_emitted", symbol=symbol, path=_tria_outbox_path, id=_tria_signal["id"])
        except Exception as _tria_emit_err:
            logger.warning("tria_outbox_emit_failed", error=str(_tria_emit_err))
        # ── End Tria Bridge outbox ─────────────────────────────────────────────

        # Capture loop-locals needed by the task (closure over mutable shared state)
        _sym = symbol
        _cand = candidate
        _state = state
        _eid = entry_id
        _brkt = bracket
        _t_dispatch = time.perf_counter()
        logger.info("pipeline_latency_breakdown",
                    symbol=symbol,
                    risk_ms=round((_t_risk_done - _t_risk_start) * 1000, 1),
                    sizing_ms=round((_t_dispatch - _t_nietzsche_start) * 1000, 1),
                    total_pre_dispatch_ms=round((_t_dispatch - _t_risk_start) * 1000, 1))

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
                        entry_coherence=_cand.coherence_score,
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
                    # Stamp personality at fill — survives personality_map overwrite by later signals
                    position.entry_personality = _personality_name
                    # Stamp phase context at fill time for adaptive calibrator learning
                    position.liq_phase = liq_engine.get_phase_snapshot(_sym).phase.value
                    _fr = float(bybit_ticker_stores.get(_sym, {}).get("funding_rate", 0.0))
                    position.funding_aligned = (
                        (_cand.side == "short" and _fr > 0) or
                        (_cand.side == "long"  and _fr < 0)
                    )

                    # ── Idempotency guard — prevents race with reconciliation loop ──
                    # reconciliation_loop runs every 5s and calls position_manager.add()
                    # for exchange-detected positions.  If it ran BETWEEN the bracket
                    # task's entry fill and this code path, there would be two entries
                    # for the same symbol.  Check first; update order IDs only if dup.
                    _existing_in_pm = position_manager.get(_sym)
                    if _existing_in_pm:
                        # Already synced by reconciliation — merge order IDs only
                        _existing_in_pm[0].order_ids = position.order_ids
                        _existing_in_pm[0].tp1_price = _cand.tp1_price
                        _existing_in_pm[0].tp2_price = _cand.tp2_price
                        _existing_in_pm[0].tp3_price = _cand.tp3_price
                        _existing_in_pm[0].stop_price = position.stop_price or _existing_in_pm[0].stop_price
                        _existing_in_pm[0].entry_coherence = _cand.coherence_score
                        logger.info("bracket_merged_to_existing", symbol=_sym,
                                    note="reconciliation already added — order IDs merged, no duplicate")
                    else:
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
                            strategy_tag=tag_strategy(
                                _state,
                                cascade_phase=cascade_tracker.get_phase().value if cascade_tracker else "idle",
                            ),
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
                    # Persistent daily trade count — survives restarts
                    daily_tracker.record_open(symbol=_sym, direction=_cand.side)
                    # Record perps notional for fee tier volume tracking
                    volume_tracker.record_trade(
                        perps_notional=_cand.entry_price * _cand.size,
                    )
                    if result.error:
                        # TP placement failed after confirmed fill.
                        # Do NOT retry automatically — retrying with stale candidate
                        # data causes repeated "quantity is invalid" rejections (observed
                        # LINK-USD: 8 attempts, all failing with wrong size).
                        # Software TP guardian (_software_tp_loop) is the fallback:
                        # it monitors price and market-closes when mark crosses tp1_price.
                        logger.error("bracket_partial_no_retry",
                                     symbol=_sym, entry=_cand.entry_price,
                                     partial_error=result.error,
                                     note="software_tp_guardian is active fallback")
                        await alert_system.send(
                            f"[ARIA] {_sym} TP placement failed — software TP guardian active. "
                            f"Error: {result.error}"
                        )
                    else:
                        logger.info("bracket_placed", symbol=_sym, entry=_cand.entry_price)
                    # Patch 3 — ARIA → AUGUR whisper: notify AUGUR of confirmed fill
                    _write_aria_whisper(
                        symbol        = _sym,
                        direction     = _cand.side,
                        coherence     = _effective_coherence,
                        entry_price   = _cand.entry_price,
                        cascade_zscore= _vc_zscore,
                        personality   = _personality_name,
                    )
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
                    # Structural rejection (exchange code:-1) → per-symbol cooldown only.
                    # Does NOT count toward the global circuit breaker — one symbol's
                    # structural rejection must never block entries for other symbols.
                    # Transient failures (fill timeout, network, auth) → global counter.
                    _err = result.error or ""
                    _is_structural = "SoDEX error -1" in _err
                    _cooldown = 120.0 if _is_structural else 90.0
                    _rejection_cooldown[_sym] = time.time() + _cooldown
                    if not _is_structural:
                        # Only network/transient failures trip the global circuit breaker.
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

    # ── Single-source position close handler ────────────────────────────────
    # ONE function that updates every state machine when a position closes:
    #   position_manager | journal | feedback | drawdown | fee ledger | learning DB
    # All callers use this instead of inlining the updates themselves.
    def _record_close(
        sym: str,
        pos_obj,
        pnl: float,
        exit_price: float,
        exit_reason: str,
    ) -> None:
        """
        Atomically record a position close across ALL subsystems.
        Called from reconciliation loop, time-stop handler, and TP close handler.
        """
        close_ms = exchange_clock.now_ms()

        # 1. Remove from position manager + arm 30s reconciliation grace period.
        # Exchange API propagation lag means get_positions() may still return this
        # position for 5–30s after a successful close. Without the grace period,
        # _reconciliation_loop re-adds it as "untracked" creating a sync-back loop.
        position_manager.close(sym, 0)
        _recently_closed[sym] = time.time() + 30.0

        # 2. Pop journal entry ID
        entry_id = _open_entry_ids.pop(sym, None)

        # 3. Orphan recovery — scan journal if entry_id missing (e.g. restart)
        if not entry_id:
            _orphan = next(
                (e for e in reversed(journal.entries)
                 if e.get("symbol") == sym
                 and e.get("approved")
                 and e.get("outcome") in (None, "open")),
                None
            )
            if _orphan:
                entry_id = _orphan["entry_id"]
                logger.info("journal_orphan_recovered", symbol=sym, entry_id=entry_id)

        # 4. Journal update
        outcome = "win" if pnl > 0 else "loss"
        if entry_id:
            journal.update_outcome(
                entry_id=entry_id,
                outcome=outcome,
                pnl_usd=pnl,
                closed_at_ms=close_ms,
            )
            feedback.record_result(entry_id, won=pnl > 0, pnl=pnl)

        # 4b. Philosophical feedback — Bayes update for Kant structure confidence
        # and prediction market resolution.  Both are best-effort (never block close).
        _initial_margin_close = float(getattr(pos_obj, "initial_margin", 0.0) or 0.0) if pos_obj else 0.0
        _pnl_r = pnl / _initial_margin_close if _initial_margin_close > 0.0 else 0.0
        try:
            kant_engine.on_outcome(symbol=sym, won=pnl > 0, pnl_r=_pnl_r)
        except Exception as _ke:
            logger.debug("kant_outcome_error", error=str(_ke))
        try:
            prediction_store.resolve(symbol=sym, outcome=outcome, actual_r=_pnl_r)
        except Exception as _pe:
            logger.debug("prediction_resolve_error", error=str(_pe))

        # 5. Drawdown trackers — dd_tracker is session-level; drawdown_guard is running avg
        try:
            drawdown_guard.record_close(pnl)
        except Exception as _dge:
            logger.debug("drawdown_guard_close_error", error=str(_dge))
        try:
            dd_tracker.on_trade_closed(pnl)
            dd_tracker.update_drawdown(_cached_balance[0])
        except Exception as _dde:
            logger.debug("dd_tracker_close_error", error=str(_dde))
        try:
            _signal_guard.record_trade(
                sym,
                getattr(pos_obj, "side", "long") if pos_obj else "long",
                pnl,
            )
        except Exception as _sge:
            logger.debug("signal_guard_record_error", error=str(_sge))
        # 5c. Streak tracker — compound winners, reset on loss
        try:
            _streak_tracker.on_trade_closed(
                symbol=sym,
                direction=getattr(pos_obj, "side", "long") if pos_obj else "long",
                pnl=pnl,
            )
            _coherence_decay.forget(pos_obj)
        except Exception as _ste:
            logger.debug("streak_tracker_close_error", error=str(_ste))
        # Feed current drawdown pct to adaptive calibrator for recovery mode trigger
        try:
            _adaptive_calibrator.update_drawdown(drawdown_guard.get_state().drawdown_pct)
        except Exception as _acde:
            logger.debug("adaptive_calibrator_drawdown_error", error=str(_acde))

        # 5b. Adaptive calibrator — fast/medium/cascade/phase loops
        # Read cascade phase from journal entry to correctly attribute cascade trades
        _cascade_phase = "none"
        if entry_id:
            _je = next((e for e in journal.entries if e.get("entry_id") == entry_id), None)
            if _je:
                _cascade_phase = _je.get("cascade_phase", "none") or "none"
        _tier_scores = {}
        if entry_id:
            _je2 = next((e for e in journal.entries if e.get("entry_id") == entry_id), None)
            if _je2:
                _tier_scores = {k: v for k, v in _je2.items() if k in (
                    "microstructure", "regime", "structure", "funding",
                    "institutional", "oi_momentum", "liquidation", "mag7_macro",
                )}
        # Read liq_phase and funding_aligned stamped at fill time on the position object
        _liq_phase_close   = getattr(pos_obj, "liq_phase",       "none")  if pos_obj else "none"
        _funding_aln_close = getattr(pos_obj, "funding_aligned",  False)  if pos_obj else False
        try:
            _adaptive_calibrator.on_trade_closed(
                won=pnl > 0,
                pnl=pnl,
                strategy_tag=getattr(pos_obj, "strategy_tag", "unknown") if pos_obj else "unknown",
                cascade_phase=_cascade_phase,
                liq_phase=_liq_phase_close,
                funding_aligned=_funding_aln_close,
                tier_scores=_tier_scores,
                market_context=_last_market_context,
            )
        except Exception as _ace:
            logger.debug("adaptive_calibrator_trade_error", error=str(_ace))

        # 6. Fee ledger
        try:
            fee = bot_fee_ledger.on_trade_closed(
                symbol=sym, pnl_usd=pnl, current_balance=_cached_balance[0]
            )
            if fee > 0:
                _cached_balance[0] = max(0.0, _cached_balance[0] - fee)
        except Exception as _fee_e:
            logger.debug("fee_ledger_close_error", error=str(_fee_e))

        # 7. Macro engine hold-time learning
        try:
            if hasattr(interpreter, "_macro") and pos_obj:
                _hold_s = (
                    (exchange_clock.now_s() - pos_obj.opened_at_ms / 1000)
                    if getattr(pos_obj, "opened_at_ms", 0) > 0
                    else 0.0
                )
                interpreter._macro.record_trade_outcome(
                    symbol=sym,
                    direction=pos_obj.side,
                    entry_coherence=getattr(pos_obj, "entry_coherence", 0.0),
                    tiers_fired=[],
                    hold_seconds=_hold_s,
                    pnl=pnl,
                )
        except Exception as _mace:
            logger.debug("macro_trade_outcome_error", error=str(_mace))

        # 8. Learning DB
        try:
            if _trade_db is not None:
                _rec = _build_trade_record(
                    pos_obj,
                    exit_price=exit_price,
                    exit_reason=exit_reason,
                    net_pnl=pnl,
                )
                _trade_db.record(_rec)
                _db_total = len(_trade_db.get_all())
                if _db_total >= 10 and _db_total % 20 == 0:
                    _cal = _calibration_engine.run()
                    if _cal:
                        _apply_calibration(_cal, _param_store)
        except Exception as _le:
            logger.debug("trade_record_error", error=str(_le))

        # 9. ECS decay — update confidence curve on each close
        try:
            ecs_engine.record_trade(pnl=pnl, risk_usd=getattr(pos_obj, "initial_margin", 0.0) if pos_obj else 0.0)
        except Exception as _ecse:
            logger.debug("ecs_record_trade_error", error=str(_ecse))

        # 10. Persistent daily PnL tracking
        try:
            daily_tracker.record_close(symbol=sym, pnl_usd=pnl)
        except Exception as _dte:
            logger.debug("daily_tracker_close_error", error=str(_dte))

        # 10b. Per-agent win/loss tracking — persists across restarts
        try:
            # Use personality stamped at entry (immune to later signal overwrites).
            # Fall back to personality_map lookup, then SCOUT.
            _agent_name = getattr(pos_obj, "entry_personality", None)
            if not _agent_name:
                _p_map = display._display_cache.get("personality_map") or {}
                _agent_name = _p_map.get(sym, "SCOUT")
            _agent_wr.record_outcome(_agent_name, won=pnl > 0, pnl=pnl)
        except Exception as _awe:
            logger.debug("agent_winrate_record_error", error=str(_awe))

        # 10c. Phase 11: OutcomeRecorder — per-signal-agent attribution
        # _record_close is synchronous; schedule the async recorder as a fire-and-forget task.
        if _outcome_recorder is not None:
            try:
                from intelligence.agents.base import TradeOutcome
                import uuid as _uuid
                _entry_ms = getattr(pos_obj, "opened_at_ms", int(time.time() * 1000 - 60000))
                _exit_ms  = int(time.time() * 1000)
                _hold_h   = (_exit_ms - _entry_ms) / 3_600_000
                _entry_p  = getattr(pos_obj, "entry_price", 0.0)
                _size     = getattr(pos_obj, "size", 0.0)
                # Collect current agent outputs for this symbol
                _agent_outputs = {}
                for _a_name, _a_obj in _sig_agents.items():
                    _a_out = _a_obj._last_outputs.get(sym) if hasattr(_a_obj, "_last_outputs") else None
                    if _a_out is not None:
                        _agent_outputs[_a_name] = _a_out
                _rs_close = regime_engine.last_state()
                _trade_outcome = TradeOutcome(
                    trade_id         = str(_uuid.uuid4()),
                    symbol           = sym,
                    direction        = getattr(pos_obj, "side", "long"),
                    net_pnl_r        = round(pnl / max(abs((_entry_p - getattr(pos_obj, "stop_price", _entry_p)) * _size), 0.01), 3),
                    net_pnl_usd      = round(pnl, 4),
                    exit_reason      = exit_reason,
                    entry_time_ms    = _entry_ms,
                    exit_time_ms     = _exit_ms,
                    hold_time_hours  = round(_hold_h, 3),
                    regime           = getattr(_rs_close, "regime", "") if _rs_close else "",
                    strategy_type    = getattr(pos_obj, "strategy_tag", "") if pos_obj else "",
                    agent_outputs    = _agent_outputs,
                )

                async def _record_outcome_async(_to=_trade_outcome):
                    try:
                        await _outcome_recorder.record(_to)
                        _acc_stats = await _outcome_recorder.get_agent_stats()
                        _total_t   = await _outcome_recorder.get_total_trades()
                        display.update_cache("agent_accuracy",     {k: v for k, v in _acc_stats.items()})
                        display.update_cache("agent_total_trades", _total_t)
                        _cal_recs = await _outcome_recorder.get_calibration_recommendations()
                        display.update_cache("calibration_alerts", _cal_recs)
                        _outcome_row = {
                            "symbol":      _to.symbol,
                            "net_pnl_r":   _to.net_pnl_r,
                            "exit_reason": _to.exit_reason,
                        }
                        for _ag in ("macro", "regime", "structure", "micro", "funding", "ssi"):
                            _cor = _to.agents_correct.get(_ag)
                            _outcome_row[f"{_ag}_correct"] = (1 if _cor is True else 0 if _cor is False else -1)
                        display.push_outcome(_outcome_row)
                    except Exception as _roe:
                        logger.debug("outcome_recorder_async_error", error=str(_roe))

                asyncio.create_task(_record_outcome_async())
            except Exception as _ore:
                logger.debug("outcome_recorder_error", error=str(_ore))

        # 11. Rapid-loss circuit breaker — halt all new trades if 3% of balance
        # is lost in any rolling 30-minute window. Institutional standard: automatic
        # session halt on accelerated drawdown, manual reset to resume.
        _now_halt = time.time()
        _session_loss_window.append((_now_halt, pnl))
        # Prune entries older than 30 minutes
        _session_loss_window[:] = [
            (ts, p) for ts, p in _session_loss_window
            if _now_halt - ts <= 1800.0
        ]
        _window_loss = sum(p for _, p in _session_loss_window if p < 0)
        _halt_threshold = -(_cached_balance[0] * 0.03) if _cached_balance[0] > 0 else -9.0
        if _window_loss < _halt_threshold and not _trading_halted[0]:
            _trading_halted[0] = True
            logger.critical("trading_halted_rapid_loss",
                            window_loss_usd=round(_window_loss, 2),
                            threshold_usd=round(_halt_threshold, 2),
                            balance=round(_cached_balance[0], 2),
                            note="all new trades blocked — manual restart required to resume")

        logger.info("position_closed",
                    symbol=sym, outcome=outcome, pnl=f"${pnl:.4f}",
                    exit_reason=exit_reason,
                    dd_regime=dd_tracker.drawdown_regime,
                    dd_pct=round(dd_tracker.session_drawdown_pct, 2),
                    daily_trades=daily_tracker.trades_today(),
                    daily_pnl=daily_tracker.pnl_today())

    # ── Execution sub-loops ────────────────────────────────────────────────────
    # execution_cleanup_loop() was a 460-line monolithic coroutine where a single
    # SoDEX REST call (80ms) could delay the software stop guardian for a full tick.
    # The fix: 7 independent coroutines, each with its own cadence and error boundary.
    # Shared state is captured from the main() closure — no locks needed (single-event-
    # loop, all mutations happen at yield points via `await`).

    async def _close_with_retry(
        symbol: str, symbol_id: int, side: str, size: float,
        *, reason: str, max_attempts: int = 3, delay_s: float = 1.0
    ) -> "OrderResult | None":
        """
        Shared close helper with retry semantics — FreqTrade/HummingBot standard.

        Attempts close_position_market up to max_attempts times (1s apart).
        Returns the first successful OrderResult, or the last failed one.
        Callers should still check .success on the return value.

        Why: transient network/exchange errors (SoDEX returns 503/timeout occasionally)
        must NOT leave a position permanently unprotected. Retry 3× before giving up.
        The 0.5s stop guardian re-runs anyway, so we only need short-lived retry here.
        """
        last_result = None
        for _attempt in range(1, max_attempts + 1):
            try:
                last_result = await client.close_position_market(
                    symbol=symbol, symbol_id=symbol_id,
                    account_id=NUMERIC_ACCOUNT_ID,
                    side=side, size=size,
                )
                if last_result.success:
                    if _attempt > 1:
                        logger.info("close_retry_succeeded",
                                    symbol=symbol, reason=reason, attempt=_attempt)
                    return last_result
                _err = (last_result.error or "").lower()
                # Structural rejections (qty invalid, no position) — no point retrying.
                # "quantity is invalid" means the position is sub-step dust on the exchange
                # side; arm the blocklist so reconciliation can't re-add it within 120s.
                if "quantity is invalid" in _err:
                    _dust_purge_blocklist[symbol] = time.time() + 120.0
                    logger.warning("close_structural_rejection",
                                   symbol=symbol, reason=reason, error=last_result.error,
                                   note="dust_purge_blocklist_armed")
                    return last_result
                if "no position" in _err or "not found" in _err:
                    logger.warning("close_structural_rejection",
                                   symbol=symbol, reason=reason, error=last_result.error)
                    return last_result
                logger.warning("close_attempt_failed",
                               symbol=symbol, reason=reason,
                               attempt=_attempt, error=last_result.error)
            except Exception as _ce:
                logger.warning("close_attempt_exception",
                               symbol=symbol, reason=reason, attempt=_attempt, error=str(_ce))
            if _attempt < max_attempts:
                await asyncio.sleep(delay_s)
        return last_result

    async def _stop_guardian_loop() -> None:
        """
        Software stop guardian — 0.5s cadence.
        Pure mark-vs-stop check between stop events; I/O ONLY when a stop fires.
        Runs independently so SoDEX REST latency in _reconciliation_loop never
        delays stop enforcement.
        """
        while True:
            try:
                for _ssym, _spositions in list(position_manager._positions.items()):
                    if not _spositions:
                        continue
                    _spos = _spositions[0]
                    # Defensive float() casts — Position dataclass doesn't enforce types;
                    # reconciliation or JSON loading can inject strings into numeric fields.
                    # Cast ALL numeric fields so every downstream <= / >= comparison is safe.
                    try:
                        _spos.stop_price      = float(_spos.stop_price or 0)
                        _spos.entry_price     = float(_spos.entry_price or 0)
                        _spos.size            = float(_spos.size or 0)
                        _spos.tp1_price       = float(_spos.tp1_price or 0)
                        _spos.initial_margin  = float(getattr(_spos, "initial_margin", 0) or 0)
                        _spos.liq_price       = float(getattr(_spos, "liq_price", 0) or 0)
                    except (TypeError, ValueError):
                        continue   # malformed position — skip this tick
                    if _spos.stop_price <= 0:
                        continue
                    _smk = mark_price_stores.get(_ssym)
                    if not _smk:
                        continue
                    _smark = _smk.mark_price
                    if _smark is None or float(_smark) <= 0:
                        continue
                    _smark = float(_smark)
                    _ssym_id = SYMBOL_IDS.get(_ssym, 0)
                    if _ssym_id == 0:
                        continue
                    _stop_hit = (
                        (_spos.side == "long"  and _smark <= _spos.stop_price) or
                        (_spos.side == "short" and _smark >= _spos.stop_price)
                    )
                    if not _stop_hit:
                        continue
                    # Circuit breaker — back off after 3 consecutive rejections
                    _scb = _stop_close_fails.get(_ssym, {})
                    if _scb.get("count", 0) >= 3 and time.time() < _scb.get("backoff_until", 0):
                        continue
                    logger.warning("software_stop_triggered",
                                   symbol=_ssym, side=_spos.side,
                                   mark=round(_smark, 6),
                                   stop_price=round(_spos.stop_price, 6),
                                   entry=round(_spos.entry_price, 6))
                    try:
                        _sclose = await client.close_position_market(
                            symbol=_ssym, symbol_id=_ssym_id,
                            account_id=NUMERIC_ACCOUNT_ID,
                            side=_spos.side, size=_spos.size,
                        )
                        if _sclose.success:
                            _stop_close_fails.pop(_ssym, None)
                            _spnl = (
                                (_smark - _spos.entry_price) * _spos.size
                                if _spos.side == "long"
                                else (_spos.entry_price - _smark) * _spos.size
                            )
                            _record_close(_ssym, _spos, _spnl, _smark, "software_stop")
                            logger.info("software_stop_closed",
                                        symbol=_ssym, pnl=round(_spnl, 4),
                                        order_id=_sclose.order_id)
                        else:
                            _serr = _sclose.error or ""
                            if "quantity is invalid" in _serr:
                                # Sub-step dust position — _round_qty rounds up to step
                                # so this fires only when the exchange itself sees size=0
                                # (i.e. position was already closed externally / net-zero).
                                # Purge immediately — no retry, no loop, no CRITICAL spam.
                                _spnl = (
                                    (_smark - _spos.entry_price) * _spos.size
                                    if _spos.side == "long"
                                    else (_spos.entry_price - _smark) * _spos.size
                                )
                                _record_close(_ssym, _spos, _spnl, _smark, "dust_purged")
                                _stop_close_fails.pop(_ssym, None)
                                # Block reconciliation from re-adding this position
                                # for 120s — the exchange close failed so the position
                                # may still appear in get_positions(). Without this
                                # block, reconciliation re-adds it every 5s creating
                                # an infinite loss loop.
                                _dust_purge_blocklist[_ssym] = time.time() + 120.0
                                logger.warning("dust_position_purged",
                                               symbol=_ssym, size=_spos.size,
                                               note="sub-step position removed from tracking")
                            elif "not found" in _serr.lower() or "no position" in _serr.lower():
                                try:
                                    _saddr = config.sodex_account_id or config.account_id or ""
                                    _slive = await client.get_positions(_saddr)
                                    _slive_syms = {
                                        p.get("symbol") or p.get("coin") or ""
                                        for p in _slive
                                    }
                                    if _ssym in _slive_syms:
                                        logger.warning("software_stop_retry_position_confirmed",
                                                       symbol=_ssym, error=_serr,
                                                       note="position verified on SoDEX, retry next tick")
                                    else:
                                        _spnl = (
                                            (_smark - _spos.entry_price) * _spos.size
                                            if _spos.side == "long"
                                            else (_spos.entry_price - _smark) * _spos.size
                                        )
                                        _record_close(_ssym, _spos, _spnl, _smark, "external_close")
                                        logger.info("software_stop_external_close_detected",
                                                    symbol=_ssym, pnl=round(_spnl, 4))
                                except Exception as _sver:
                                    logger.warning("software_stop_verify_error",
                                                   symbol=_ssym, error=str(_sver))
                            else:
                                _scb_entry = _stop_close_fails.setdefault(
                                    _ssym, {"count": 0, "backoff_until": 0.0, "last_err": ""}
                                )
                                _scb_entry["count"] += 1
                                _scb_entry["last_err"] = _serr
                                _scb_entry["backoff_until"] = time.time() + 5.0
                                logger.error("software_stop_close_failed",
                                             symbol=_ssym, error=_serr,
                                             fail_count=_scb_entry["count"])
                                if _scb_entry["count"] == 3:
                                    alert_system.notify_stop_fix_failed(_ssym, _serr)
                    except Exception as _se:
                        logger.error("software_stop_exception", symbol=_ssym, error=str(_se),
                                     traceback=_traceback.format_exc().strip())
            except Exception as _sge:
                logger.error("stop_guardian_loop_error", error=str(_sge))
            # Push current unclosed-order state to display header every cycle
            display.update_stuck_positions(_stop_close_fails)
            await asyncio.sleep(0.5)   # 0.5s — twice as fast as before, never delayed by REST

    async def _mae_mfe_loop() -> None:
        """MAE/MFE excursion tracking — 1s cadence, pure in-memory computation."""
        while True:
            try:
                for _sym, _positions in list(position_manager._positions.items()):
                    if not _positions:
                        continue
                    _pos = _positions[0]
                    _ep = float(getattr(_pos, "entry_price", 0) or 0)
                    if _ep <= 0:
                        continue
                    _mstore = mark_price_stores.get(_sym)
                    if not _mstore or _mstore.mark_price is None or _mstore.mark_price <= 0:
                        continue
                    _m = float(_mstore.mark_price)
                    if _pos.side == "long":
                        _adv = max(0.0, _ep - _m)
                        _fav = max(0.0, _m - _ep)
                    else:
                        _adv = max(0.0, _m - _ep)
                        _fav = max(0.0, _ep - _m)
                    if _adv > _pos.max_adverse_excursion:
                        _pos.max_adverse_excursion = _adv
                    if _fav > _pos.max_favourable_excursion:
                        _pos.max_favourable_excursion = _fav
            except Exception:
                pass   # never interrupt position monitoring
            await asyncio.sleep(1.0)

    async def _balance_and_feedback_loop() -> None:
        """
        Balance fetch + display update + feedback sync + cooldown purge.
        Cadences: balance=5s, P&L log=60s, feedback=30s, cooldown purge=3h.
        Ticks every 1s but only calls REST every 5s — no blocking of stop guardian.
        """
        _balance_poll_counter = 0
        _balance_log_counter = 0
        _feedback_sync_counter = 0
        _cooldown_purge_counter = 0
        _adl_counter = 0          # ADL risk assessment — every 300s (5 min)
        _last_balance_for_pnl: float = 0.0

        while True:
            try:
                # ── Balance fetch — every 5s (15s backoff on zero) ────────────────
                _balance_poll_counter += 1
                _balance_zero_backoff = _cached_balance[0] == 0.0
                _poll_interval = 15 if _balance_zero_backoff else 5
                if _balance_poll_counter >= _poll_interval:
                    _balance_poll_counter = 0
                    acc_id = config.sodex_account_id or config.account_id or ""
                    _new_bal = await client.get_account_balance(acc_id)
                    if _new_bal > 0:
                        _cached_balance[0] = _new_bal
                    if spot_client is not None:
                        _cached_spot_balance[0] = await spot_client.get_spot_balance(acc_id)

                # ── Display equity update — every tick ────────────────────────────
                display.update_equity(_cached_balance[0])
                if _cached_spot_balance[0] > 0:
                    display.update_spot_balance(_cached_spot_balance[0])
                    # Propagate latest spot balance to SovereignAgent for fee reserve checks
                    _sovereign_agent.set_spot_balance(_cached_spot_balance[0])
                drawdown_guard.update_balance(_cached_balance[0])
                if _cached_balance[0] > 0 and drawdown_manager is not None:
                    drawdown_manager.update_balance(_cached_balance[0])
                    # Align DrawdownGuard peak with authoritative DrawdownManager
                    drawdown_guard.sync_peak(drawdown_manager._peak_balance)
                if vc_monitor is not None:
                    _vc_st = vc_monitor.get_status()
                    display.update_vc_status(_vc_st)
                    # Personality cache: cascade phase MUST come from cascade_tracker
                    # (IDLE/BLOCKED/PRIMED/MOMENTUM), NOT vc_monitor.get_status() which
                    # returns its own internal phase strings (trigger/expansion/exhaustion).
                    # _is_apex() checks phase in ("blocked","momentum"), _is_aftermath()
                    # checks phase in ("primed","aftermath") — both require tracker strings.
                    _ct_phase   = cascade_tracker.get_phase().value
                    _ct_snap    = cascade_tracker.get_snapshot()
                    _ct_aft     = cascade_tracker.get_aftermath_signals()
                    _ct_dir     = (_ct_snap.batch_direction if _ct_snap else
                                   str(_vc_st.get("cascade_direction", "none")))
                    _ct_zscore  = float(_vc_st.get("cascade_zscore", 0.0))
                    _ct_notl    = float(_ct_snap.batch_notional_usd if _ct_snap else
                                        _vc_st.get("cascade_notional", 0.0))
                    _ct_aft_cnt = sum(1 for v in _ct_aft.values() if v) if _ct_aft else 0
                    context_cache.update_cascade(
                        phase=_ct_phase,
                        direction=_ct_dir,
                        zscore=_ct_zscore,
                        notional=_ct_notl,
                        aftermath_signals=_ct_aft_cnt,
                    )
                else:
                    context_cache.update_cascade(
                        phase="none", direction="none",
                        zscore=0.0, notional=0.0, aftermath_signals=0,
                    )
                if true_arb is not None:
                    display.update_true_arb_positions(true_arb.get_open_positions())

                # ── P&L attribution log + balance telemetry — every 60s ───────────
                _balance_log_counter += 1
                if _balance_log_counter >= 60:
                    _balance_log_counter = 0
                    balance = _cached_balance[0]
                    _eff_min = config.min_trade_notional_usd
                    logger.info(
                        "account_balance",
                        balance=f"${balance:.2f}",
                        risk_per_trade=f"${balance * config.risk_pct:.2f}",
                        arb_capital=f"${balance * config.arb_capital_pct:.2f}",
                        min_notional=f"${_eff_min:.2f}",
                        max_notional=f"${balance * config.default_leverage * 0.90:.2f} (dynamic)",
                    )
                    if _last_balance_for_pnl > 0:
                        _bal_delta = balance - _last_balance_for_pnl
                        _open_positions = list(position_manager.get_all())
                        _unrealized = 0.0
                        _pos_summary = []
                        for _pp in _open_positions:
                            _pm = mark_price_stores.get(_pp.symbol)
                            _mk = float(_pm.mark_price) if _pm and _pm.mark_price is not None else _pp.entry_price
                            if _mk > 0 and _pp.entry_price > 0:
                                _pnl_raw = (
                                    (_mk - _pp.entry_price) * _pp.size
                                    if _pp.side == "long"
                                    else (_pp.entry_price - _mk) * _pp.size
                                )
                                _unrealized += _pnl_raw
                                _pos_summary.append(
                                    f"{_pp.symbol}:{_pp.side[0].upper()}"
                                    f"@{_pp.entry_price:.4g}→{_mk:.4g}"
                                    f"={_pnl_raw:+.2f}"
                                )
                        logger.info(
                            "pnl_attribution",
                            balance_delta=round(_bal_delta, 4),
                            unrealized_total=round(_unrealized, 4),
                            realized_est=round(_bal_delta - _unrealized, 4),
                            open_positions=len(_open_positions),
                            breakdown=" | ".join(_pos_summary) or "none",
                        )
                    _last_balance_for_pnl = balance

                # ── ADL risk assessment — every 5 minutes (300s) ──────────────────
                _adl_counter += 1
                if _adl_counter >= 300:
                    _adl_counter = 0
                    _adl_vc     = vc_monitor.get_status() if vc_monitor is not None else {}
                    _adl_zscore = float(_adl_vc.get("cascade_zscore", 0.0))
                    for _adl_pp in list(position_manager.get_all()):
                        _adl_pm  = mark_price_stores.get(_adl_pp.symbol)
                        _adl_mk  = (float(_adl_pm.mark_price)
                                    if _adl_pm and _adl_pm.mark_price is not None
                                    else _adl_pp.entry_price)
                        if _adl_mk > 0 and _adl_pp.entry_price > 0:
                            _adl_pnl = (
                                (_adl_mk - _adl_pp.entry_price) * _adl_pp.size
                                if _adl_pp.side == "long"
                                else (_adl_pp.entry_price - _adl_mk) * _adl_pp.size
                            )
                            _adl_lev   = getattr(_adl_pp, "leverage", config.default_leverage) or config.default_leverage
                            _adl_score = _adl_pnl * _adl_lev
                            _adl_risk  = (
                                "critical" if _adl_score > 30 else
                                "high"     if _adl_score > 15 else
                                "elevated" if _adl_score > 5  else
                                "low"
                            )
                            logger.info(
                                "adl_risk_assessment",
                                symbol=_adl_pp.symbol,
                                adl_score=round(_adl_score, 2),
                                unrealised_pnl=round(_adl_pnl, 2),
                                leverage=_adl_lev,
                                adl_risk=_adl_risk,
                            )
                            if _adl_risk in ("high", "critical") and _adl_zscore > 2.0:
                                logger.warning(
                                    "adl_cascade_warning",
                                    symbol=_adl_pp.symbol,
                                    adl_score=round(_adl_score, 2),
                                    cascade_zscore=round(_adl_zscore, 2),
                                    action="consider_early_tp",
                                )

                # ── Cooldown purge — every 3h ──────────────────────────────────────
                _cooldown_purge_counter += 1
                if _cooldown_purge_counter >= 10_800:   # 3h × 3600s ÷ 1s tick
                    _cooldown_purge_counter = 0
                    _now_purge = time.time()
                    _stale = [s for s, exp in _rejection_cooldown.items() if exp < _now_purge]

                    # Quiet-aware purge: symbols still in genuine quiet market get a
                    # 30-min cooldown re-arm instead of a blind clear. Prevents the
                    # circuit reset from allowing trades in still-quiet conditions.
                    _purge_vc     = vc_monitor.get_status() if vc_monitor is not None else {}
                    _purge_ev60   = int(_purge_vc.get("events_60s", 999))
                    _purge_quiet_s = _now_purge - _last_active_market_ts[0]
                    _still_quiet  = (
                        _purge_ev60 != 999 and
                        _purge_ev60 < 40 and
                        _purge_quiet_s > 1800.0
                    )

                    _cleared: list = []
                    _preserved: list = []
                    for _s in _stale:
                        if _still_quiet:
                            _rejection_cooldown[_s] = _now_purge + 1800.0  # re-arm 30 min
                            logger.info("cooldown_preserved_quiet",
                                        symbol=_s,
                                        reason="still_in_quiet_market",
                                        events_60s=_purge_ev60,
                                        quiet_minutes=round(_purge_quiet_s / 60.0, 1))
                            _preserved.append(_s)
                        else:
                            del _rejection_cooldown[_s]
                            logger.info("cooldown_cleared_active",
                                        symbol=_s,
                                        reason="market_active",
                                        events_60s=_purge_ev60)
                            _cleared.append(_s)

                    _api_circuit_open_until[0] = 0.0
                    _api_consecutive_failures[0] = 0
                    logger.info("stale_cooldown_purge",
                                purged_symbols=_cleared,
                                preserved_quiet=_preserved,
                                circuit_reset=True,
                                action="3h hard reset — quiet-aware cooldown purge")

                # ── Feedback sync — every 30s ──────────────────────────────────────
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
                    # Personality cache: daily P&L % + win rate from feedback engine
                    _bal_for_perf = _cached_balance[0] or 1.0
                    _daily_pnl_pct = (
                        ((_cached_balance[0] - _last_balance_for_pnl) / _last_balance_for_pnl)
                        if _last_balance_for_pnl > 0 else 0.0
                    )
                    context_cache.update_performance(
                        daily_pnl_pct=_daily_pnl_pct,
                        win_rate=float(summary.get("win_rate", 0.5)),
                    )
                    # SOVEREIGN: update stake balance, live budget, and component z-scores.
                    # MAG7SSI price from spot SSI feed (signal_price_stores["MAG7SSI-USD"])
                    # which is updated in real-time by ssi_spot_feed. Falls back to
                    # SLPVaultTracker's last known price, then 0 (uses entry estimate).
                    _mag7_price = signal_price_stores.get("MAG7SSI-USD", {}).get("price", 0.0)
                    if _mag7_price <= 0:
                        _mag7_price = getattr(_slp_tracker, "_mag7ssi_price_current", 0.0)
                    _sovereign_stake = _portfolio.get_mag7_stake_usd(_mag7_price)
                    _sov_z_scores = _ssi_monitor.get_all_z_scores()
                    context_cache.update_sovereign(
                        stake_balance=_sovereign_stake,
                        sovereign_budget=_yield_tracker.available_budget,
                        component_signals=_sov_z_scores,
                    )
                    # Push sovereign snapshot to terminal display
                    _best_div = _ssi_monitor.get_best_divergence()
                    display.update_cache("sovereign", {
                        "stake_usd":     round(_sovereign_stake, 2),
                        "budget_usd":    round(_yield_tracker.available_budget, 4),
                        "reserve_usd":   round(_yield_tracker.get_snapshot().sovereign_reserve, 4),
                        "is_active":     _yield_tracker.can_trade(),
                        "z_scores":      dict(_sov_z_scores),
                        "best_sym":      _best_div.symbol if _best_div else "",
                        "best_z":        round(_best_div.z_score, 2) if _best_div else 0.0,
                        "best_dir":      _best_div.direction if _best_div else "",
                        "yield_accrued": round(_staking_monitor.get_total_accrued_yield(), 4),
                    })

            except Exception as e:
                logger.error("balance_feedback_loop_error", error=str(e))
            await asyncio.sleep(1.0)

    # Recently-closed grace set: after _record_close(), symbols stay here for 30s.
    # Prevents reconciliation from re-adding a closed position that SoDEX API still
    # reports (exchange propagation lag can be 5–30s after fill). Root cause of the
    # "untracked_position_synced immediately after close" pattern.
    # Format: symbol → float (unix ts when grace period expires)
    _recently_closed: dict = {}

    async def _reconciliation_loop() -> None:
        """
        REST position reconciliation — 5s cadence (adaptive backoff on SoDEX errors).

        Detects closes, syncs position sizes, picks up manually-placed stops,
        detects TP1/TP2 fills from exchange size drops.
        Isolated from stop guardian — SoDEX REST latency never delays stop checks.

        Exponential backoff pattern (Pattern A fix):
        SoDEX timeouts cluster because of repeated rapid calls during instability.
        On N consecutive failures: sleep = min(5 × 2^N, 120)s before retry.
        This prevents the "8 timeouts in 60 seconds" death spiral.
        """
        _recon_backoff = 5.0          # current sleep interval (grows on failure)
        _recon_failures = 0           # consecutive failure counter
        _recon_max_backoff = 120.0    # cap at 2 minutes

        while True:
            try:
                addr = config.sodex_account_id or config.account_id or ""
                live_positions = await client.get_positions(addr)
                # Success — reset backoff
                if _recon_failures > 0:
                    logger.info("reconciliation_recovered",
                                after_failures=_recon_failures,
                                next_interval_s=5.0)
                _recon_failures = 0
                _recon_backoff = 5.0

                exchange_open: dict = {}
                for pos in live_positions:
                    sym = pos.get("symbol", "") or pos.get("coin", "")
                    size = abs(float(pos.get("size", 0) or pos.get("qty", 0) or 0))
                    if size > 0 and sym:
                        exchange_open[sym] = (size, pos)

                # Prune expired recently-closed entries
                _now_rc = time.time()
                for _rc_sym in list(_recently_closed.keys()):
                    if _now_rc >= _recently_closed[_rc_sym]:
                        _recently_closed.pop(_rc_sym, None)

                try:
                    open_orders = await client.get_open_orders(addr)
                except Exception:
                    open_orders = []

                # ── Dust-purge blocklist maintenance ─────────────────────────
                # When a symbol is in the blocklist AND the exchange no longer
                # has the position, the close succeeded (delayed). Clear the block
                # so future entries on that symbol can be tracked normally.
                # Also purge expired entries to prevent memory growth.
                _now_ts = time.time()
                for _blk_sym in list(_dust_purge_blocklist.keys()):
                    if _blk_sym not in exchange_open:
                        # Exchange confirmed gone — safe to unblock
                        _dust_purge_blocklist.pop(_blk_sym, None)
                        logger.info("dust_purge_block_cleared",
                                    symbol=_blk_sym, reason="exchange_position_gone")
                    elif _now_ts >= _dust_purge_blocklist[_blk_sym]:
                        # TTL expired — unblock regardless (next close attempt will retry)
                        _dust_purge_blocklist.pop(_blk_sym, None)
                        logger.info("dust_purge_block_expired", symbol=_blk_sym)

                # ── Size sync + stop sync ──────────────────────────────────────
                for sym, positions in list(position_manager._positions.items()):
                    if not positions:
                        continue
                    try:
                        pos = positions[0]
                        if sym in exchange_open:
                            ex_size = exchange_open[sym][0]
                            if abs(ex_size - pos.size) > 0.001:
                                logger.info("position_size_synced", symbol=sym,
                                            tracked=round(pos.size, 4),
                                            exchange=round(ex_size, 4))
                                pos.size = ex_size

                        # Assign software stop when missing (startup sync, manual open)
                        if pos.stop_price == 0.0:
                            _rmark = (mark_price_stores[sym].mark_price
                                      if sym in mark_price_stores else pos.entry_price)
                            _ref_px = pos.entry_price if pos.entry_price > 0 else _rmark
                            if _ref_px > 0:
                                _pstop_pct = 0.015
                                _rp_stop = (
                                    _ref_px * (1 - _pstop_pct) if pos.side == "long"
                                    else _ref_px * (1 + _pstop_pct)
                                )
                                pos.stop_price = _rp_stop
                                logger.info("missing_stop_set_software", symbol=sym,
                                            stop=round(_rp_stop, 4),
                                            note="software stop guardian active")

                        # Sync stop from live exchange orders — picks up manually-placed
                        # stops and corrects stale IDs after SoDEX order replacement.
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
                                    if int(o.get("side", 0) or 0) == 2   # SELL
                                    and float(o.get("price", 0) or 0) < pos.entry_price
                                ]
                                if stop_candidates:
                                    ex_stop = min(stop_candidates)
                                    if abs(ex_stop - pos.stop_price) > 0.001:
                                        logger.info("stop_synced_from_exchange", symbol=sym,
                                                    old=round(pos.stop_price, 4),
                                                    new=round(ex_stop, 4))
                                        pos.stop_price = ex_stop
                            else:  # short
                                stop_candidates = [
                                    float(o.get("price", 0) or 0)
                                    for o in sym_orders
                                    if int(o.get("side", 0) or 0) == 1   # BUY
                                    and float(o.get("price", 0) or 0) > pos.entry_price
                                ]
                                if stop_candidates:
                                    ex_stop = max(stop_candidates)
                                    if abs(ex_stop - pos.stop_price) > 0.001:
                                        logger.info("stop_synced_from_exchange", symbol=sym,
                                                    old=round(pos.stop_price, 4),
                                                    new=round(ex_stop, 4))
                                        pos.stop_price = ex_stop
                    except Exception as _sym_e:
                        logger.warning("position_sync_error", symbol=sym, error=str(_sym_e))

                # ── Close detection ────────────────────────────────────────────
                for sym, positions in list(position_manager._positions.items()):
                    try:
                        if sym not in exchange_open and positions:
                            pos_obj = positions[0]
                            # Grace period: SoDEX API propagation can lag 5-30s after a fill.
                            # Also skip if a bracket task is still in-flight for this symbol.
                            _pos_age_s = (
                                time.time() - pos_obj.opened_at_ms / 1000
                                if pos_obj.opened_at_ms > 0 else 9999
                            )
                            if _pos_age_s < 90 or sym in _pending_entry_symbols:
                                logger.debug("reconciliation_grace_hold", symbol=sym,
                                             age_s=round(_pos_age_s, 1),
                                             pending=sym in _pending_entry_symbols)
                                continue
                            mark = float(
                                mark_price_stores[sym].mark_price
                                if sym in mark_price_stores else 0.0
                            )
                            # Defensive casts — reconciliation can inject strings from JSON
                            _entry = float(getattr(pos_obj, "entry_price", 0) or 0)
                            _stop  = float(getattr(pos_obj, "stop_price", 0) or 0)
                            if mark > 0 and _entry > 0:
                                _is_stop = (
                                    (pos_obj.side == "long" and mark <= _stop) or
                                    (pos_obj.side == "short" and mark >= _stop)
                                ) if _stop > 0 else False

                                _tp1 = float(getattr(pos_obj, "tp1_price", 0) or 0)
                                if _is_stop:
                                    _base_pr = mark
                                else:
                                    _base_pr = _tp1 if _tp1 > 0 else mark

                                _size = float(getattr(pos_obj, "size", 0) or 0)
                                pnl = (
                                    (_base_pr - _entry) * _size
                                    if pos_obj.side == "long"
                                    else (_entry - _base_pr) * _size
                                )
                            else:
                                pnl = 0.0
                                _base_pr = _entry if _entry > 0 else mark

                            _record_close(sym, pos_obj, pnl,
                                          _base_pr if _base_pr > 0 else _entry,
                                          "exchange_close")
                    except Exception as _sym_e:
                        logger.warning("close_detection_error", symbol=sym, error=str(_sym_e))

                # ── Untracked position detection ───────────────────────────────
                for sym, (size, pos_data) in exchange_open.items():
                    if sym not in config.assets:
                        continue
                    try:
                        if not position_manager.get(sym):
                            # Recently-closed grace period (Pattern B fix):
                            # After _record_close(), the exchange may still report this
                            # position for up to 30s due to propagation lag. Without this
                            # guard, reconciliation re-adds it as "untracked" immediately
                            # after a successful close — triggering another stop → close
                            # cycle, creating the sync-back loop seen in production.
                            _rc_expiry = _recently_closed.get(sym, 0.0)
                            if time.time() < _rc_expiry:
                                logger.debug("recently_closed_grace_hold", symbol=sym,
                                             expires_in=round(_rc_expiry - time.time(), 1))
                                continue

                            # Dust-purge blocklist: this symbol was recently purged
                            # because the close order failed (quantity invalid / exchange
                            # rejected). Skip re-sync until the block expires or the
                            # exchange confirms the position is gone — prevents the
                            # purge→resync→stop→purge infinite loop.
                            _dpb_expiry = _dust_purge_blocklist.get(sym, 0.0)
                            if time.time() < _dpb_expiry:
                                logger.debug("dust_purge_blocked_resync", symbol=sym,
                                             expires_in=round(_dpb_expiry - time.time(), 1))
                                continue
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
                            liq_px = float(
                                pos_data.get("liqPrice", 0)
                                or pos_data.get("liquidationPrice", 0) or 0
                            )
                            lev = int(float(
                                pos_data.get("leverage", config.default_leverage)
                                or config.default_leverage
                            ))
                            if entry_px <= 0:
                                continue
                            _sync_tp1 = (
                                entry_px * 1.015 if side == "long" else entry_px * 0.985
                            )
                            _pstop_pct = 0.015
                            _pstop = (
                                entry_px * (1 - _pstop_pct) if side == "long"
                                else entry_px * (1 + _pstop_pct)
                            )
                            synced = Position(
                                symbol=sym, side=side, entry_price=entry_px, size=size,
                                initial_size=size,
                                stop_price=_pstop,
                                tp1_price=_sync_tp1,  # software TP guardian target
                                tp2_price=0.0, tp3_price=0.0,
                                liq_price=liq_px,
                                initial_margin=entry_px * size / max(lev, 1),
                                leverage=lev,
                                opened_at_ms=int(time.time() * 1000),
                            )
                            position_manager.add(synced)
                            logger.warning("untracked_position_synced",
                                           symbol=sym, side=side, size=size,
                                           entry=entry_px, leverage=lev,
                                           software_stop=round(_pstop, 4),
                                           note="software stop guardian active")
                    except Exception as _sym_e:
                        logger.warning("untracked_sync_error", symbol=sym, error=str(_sym_e))

                # ── TP1 / TP2 hit detection ────────────────────────────────────
                # Detects exchange-side TP fills by monitoring position size drops.
                for sym, (exchange_size, _) in exchange_open.items():
                    try:
                        positions = position_manager.get(sym)
                        if not positions:
                            continue
                        pos = positions[0]
                        initial_sz = float(pos.initial_size if pos.initial_size > 0 else pos.size)
                        sym_id = SYMBOL_IDS.get(sym, 0)

                        # Skip dust positions — prior-session TP fills leave tiny remnants
                        # that would trigger false TP1/TP2 detection on restart.
                        if exchange_size < initial_sz * 0.05:
                            continue

                        if not pos.tp1_hit and exchange_size <= initial_sz * 0.65:
                            new_stop = position_manager.mark_tp1_hit(sym, 0)
                            pos.size = exchange_size
                            if new_stop and new_stop > 0:
                                pos.stop_price = new_stop
                            logger.info("tp1_detected_live", symbol=sym,
                                        new_software_stop=round(new_stop, 4) if new_stop else None,
                                        exchange_size=exchange_size)

                        elif pos.tp1_hit and not pos.tp2_hit and exchange_size <= initial_sz * 0.35:
                            new_stop = position_manager.mark_tp2_hit(sym, 0)
                            pos.size = exchange_size
                            if new_stop and new_stop > 0:
                                pos.stop_price = new_stop
                            logger.info("tp2_detected_live", symbol=sym,
                                        new_software_stop=round(new_stop, 4) if new_stop else None,
                                        exchange_size=exchange_size)
                    except Exception as _sym_e:
                        logger.warning("tp_detection_error", symbol=sym, error=str(_sym_e))

            except Exception as _pe:
                import traceback as _tb
                _recon_failures += 1
                _recon_backoff = min(_recon_max_backoff, 5.0 * (2 ** min(_recon_failures - 1, 4)))
                logger.warning("reconciliation_loop_error",
                               error=str(_pe),
                               consecutive_failures=_recon_failures,
                               backoff_s=round(_recon_backoff, 1),
                               traceback=_tb.format_exc().strip())
                await asyncio.sleep(_recon_backoff)
                continue   # skip the normal sleep below — already slept in backoff
            await asyncio.sleep(5.0)   # 5s — all REST calls complete before next poll

    async def _trailing_stop_loop() -> None:
        """
        Trailing stop ratchet — 10s cadence, pure in-memory computation.
        Activates after trail_activation_atr favorable move; never moves stop backwards.
        """
        # sym → (opened_at_ms, best_price): keyed by position open time so
        # stale data from a prior position never contaminates a new entry.
        _trail_data: dict = {}
        # Dynamic trail params by asset category.
        # large_cap  (BTC/ETH): macro momentum — moderate activation, wide distance
        # alt_l1/l2  (SOL/ARB): mid-vol — 2× activation, 1× distance
        # meme       (TRUMP/BASED): fast explosive moves — need wider trail to survive
        # commodity/index: slower drift — tight activation is fine
        _TRAIL_BY_CAT = {
            "large_cap":           (1.5, 1.5),
            "alt_l1":              (2.0, 1.0),
            "l2":                  (2.0, 1.0),
            "defi_infra":          (2.0, 1.0),
            "cex_ecosystem":       (2.0, 1.0),
            "meme":                (3.0, 1.5),
            "commodity_precious":  (1.5, 1.0),
            "commodity_energy":    (1.5, 1.0),
            "commodity_industrial":(1.5, 1.0),
            "index_tech":          (1.5, 1.0),
            "index_broad":         (1.5, 1.0),
            "index_meme":          (3.0, 1.5),
            "index_defi":          (2.0, 1.0),
            "index_equity":        (1.5, 1.0),
        }
        _trail_default_act  = getattr(config, 'trail_activation_atr', 2.0)
        _trail_default_dist = getattr(config, 'trail_distance_atr', 1.0)

        while True:
            await asyncio.sleep(10.0)
            try:
                for _sym, _positions in list(position_manager._positions.items()):
                    if not _positions:
                        continue
                    _pos = _positions[0]
                    _mark_store = mark_price_stores.get(_sym)
                    if not _mark_store:
                        continue
                    _mark = float(_mark_store.mark_price or 0)
                    if not _mark or _mark <= 0:
                        continue
                    # Synthetic ATR fallback for startup-synced positions (atr=0).
                    # Use 1.0% of price (vs 0.3% before) — 0.3% caused trail activation
                    # at just 0.15% gain on BTC ($112 at $74k), immediately stopped out
                    # by normal intrabar oscillation. 1.0% gives ~$745 trail on BTC,
                    # matching realistic 1h ATR range and preventing hair-trigger exits.
                    # Positions with real ATR > 0 are unaffected.
                    _eff_atr = _pos.atr if _pos.atr > 0 else float(_mark) * 0.010
                    # Guard: stop_price may be str if assigned from API without cast
                    if not isinstance(_pos.stop_price, (int, float)):
                        _pos.stop_price = float(_pos.stop_price or 0)

                    # Staleness guard: use opened_at_ms as position identity key.
                    # If a new position opened on the same symbol, the old best is stale.
                    _opened_at = getattr(_pos, "opened_at_ms", 0)

                    # Per-category dynamic trail parameters
                    _sym_cat = config.ASSET_CONFIG.get(_sym, {}).get("category", "")
                    _trail_act_atr, _trail_dist_atr = _TRAIL_BY_CAT.get(
                        _sym_cat, (_trail_default_act, _trail_default_dist)
                    )

                    if _pos.side == "long":
                        _stored = _trail_data.get(_sym)
                        if _stored is None or _stored[0] != _opened_at:
                            _best = float(_pos.entry_price)
                            _trail_data[_sym] = (_opened_at, _best)
                        else:
                            _best = _stored[1]
                        if _mark > _best:
                            _trail_data[_sym] = (_opened_at, _mark)
                            _best = _mark
                        if _best < _pos.entry_price + _trail_act_atr * _eff_atr:
                            continue
                        _new_stop = _best - _trail_dist_atr * _eff_atr
                        # No pre-TP1 breakeven clamp: with activation_atr=2.0 the trail
                        # only fires after meaningful profit; clamping to entry creates a
                        # zero-profit breakeven trap that costs fees on exit.
                        _new_stop = min(_new_stop, _mark * 0.9999)  # always below mark
                        if _new_stop <= _pos.stop_price:
                            continue  # no improvement
                    else:
                        _stored = _trail_data.get(_sym)
                        if _stored is None or _stored[0] != _opened_at:
                            _best = float(_pos.entry_price)
                            _trail_data[_sym] = (_opened_at, _best)
                        else:
                            _best = _stored[1]
                        if _mark < _best:
                            _trail_data[_sym] = (_opened_at, _mark)
                            _best = _mark
                        if _best > _pos.entry_price - _trail_act_atr * _eff_atr:
                            continue
                        _new_stop = _best + _trail_dist_atr * _eff_atr
                        # No pre-TP1 clamp for shorts either — same rationale as longs.
                        _new_stop = max(_new_stop, _mark * 1.0001)  # always above mark
                        if _new_stop >= _pos.stop_price:
                            continue  # no improvement (stop must move lower to improve)

                    logger.info("trailing_stop_updated", symbol=_sym,
                                old_stop=round(_pos.stop_price, 4),
                                new_stop=round(_new_stop, 4),
                                best_price=round(_best, 4),
                                atr=round(_pos.atr, 4),
                                category=_sym_cat,
                                act_atr=_trail_act_atr,
                                dist_atr=_trail_dist_atr)
                    _pos.stop_price = _new_stop

            except Exception as _te:
                logger.error("trailing_stop_loop_error", error=str(_te))

    async def _software_tp_loop() -> None:
        """
        Software TP guardian — 2s cadence.

        Handles positions whose exchange bracket TP order is absent:
          • Startup-synced positions (bracket not recovered across session boundary)
          • Positions where place_bracket partial-failed and TP order was never placed

        For these positions tp1_price is set to entry±1.5% at sync time.
        When mark crosses tp1_price this loop fires a market close — the only
        reliable exit for positions the exchange has no TP order for.

        Positions WITH a live exchange TP order (order_ids["tp1"] exists) are
        skipped — the exchange handles those; we only fire software TP when
        the exchange has nothing registered.
        """
        while True:
            await asyncio.sleep(2.0)
            try:
                for _sym, _positions in list(position_manager._positions.items()):
                    if not _positions:
                        continue
                    _pos = _positions[0]

                    # Exchange bracket has a TP order → skip, exchange handles it
                    if _pos.order_ids and _pos.order_ids.get("tp1"):
                        continue

                    if _pos.tp1_hit:
                        continue

                    # Safety net: assign a 1.5% target if none was set
                    # (e.g., very old synced position or edge-case missed at sync)
                    if _pos.tp1_price <= 0:
                        _pos.tp1_price = (
                            _pos.entry_price * 1.015 if _pos.side == "long"
                            else _pos.entry_price * 0.985
                        )
                        logger.info("software_tp_assigned", symbol=_sym,
                                    tp1=round(_pos.tp1_price, 6),
                                    entry=round(_pos.entry_price, 6))

                    _mk_store = mark_price_stores.get(_sym)
                    if not _mk_store:
                        continue
                    _mark = _mk_store.mark_price
                    if not _mark or float(_mark) <= 0:
                        continue
                    _mark = float(_mark)

                    _tp_hit = (
                        (_pos.side == "long"  and _mark >= _pos.tp1_price) or
                        (_pos.side == "short" and _mark <= _pos.tp1_price)
                    )
                    if not _tp_hit:
                        continue

                    _sym_id = SYMBOL_IDS.get(_sym, 0)
                    if _sym_id == 0:
                        logger.warning("software_tp_no_sym_id", symbol=_sym)
                        continue

                    # Pre-close dust guard — if position is already below one step,
                    # _close_with_retry will always get "quantity is invalid" and the
                    # 2s cadence creates a tight loss-logging loop. Purge now.
                    _tp_min_step = _CLOSE_STEP_SIZES.get(_sym, 0.01)
                    try:
                        _tp_size = float(_pos.size)
                    except (TypeError, ValueError):
                        _tp_size = 0.0
                    if _tp_size < _tp_min_step:
                        _tp_pnl = (
                            (_mark - float(_pos.entry_price)) * _tp_size
                            if _pos.side == "long"
                            else (float(_pos.entry_price) - _mark) * _tp_size
                        )
                        _record_close(_sym, _pos, _tp_pnl, _mark, "tp_dust_purged")
                        _dust_purge_blocklist[_sym] = time.time() + 120.0
                        logger.warning("software_tp_dust_purged", symbol=_sym,
                                       size=_tp_size, min_step=_tp_min_step,
                                       note="sub-step position removed before close attempt")
                        continue

                    _pct_gain = round(abs(_mark / _pos.entry_price - 1) * 100, 2)
                    logger.info("software_tp_triggered", symbol=_sym,
                                side=_pos.side, mark=round(_mark, 6),
                                tp1=round(_pos.tp1_price, 6),
                                entry=round(_pos.entry_price, 6),
                                gain_pct=_pct_gain)
                    _tp_res = await _close_with_retry(
                        _sym, _sym_id, _pos.side, _pos.size, reason="software_tp"
                    )
                    if _tp_res and _tp_res.success:
                        _tp_pnl = (
                            (_mark - _pos.entry_price) * _pos.size
                            if _pos.side == "long"
                            else (_pos.entry_price - _mark) * _pos.size
                        )
                        _record_close(_sym, _pos, _tp_pnl, _mark, "software_tp")
                        logger.info("software_tp_closed", symbol=_sym,
                                    pnl=round(_tp_pnl, 4),
                                    gain_pct=_pct_gain,
                                    order_id=_tp_res.order_id)
                    elif _tp_res:
                        logger.warning("software_tp_close_failed", symbol=_sym,
                                       error=_tp_res.error)
            except Exception as _outer:
                logger.error("software_tp_loop_error", error=str(_outer))

    async def _time_stop_loop() -> None:
        """
        Capital-efficiency time stop — 60s cadence.
        Tiered loser logic: 2h → full close of losing positions (no partial — SoDEX
        partial close requires explicit size; full close is cleaner and avoids dust).
        6h → close any position regardless of PnL (opportunity cost cap).
        Preserves winners (tp1_hit=True) — trailing stop handles those.
        """
        _LOSER_CUTOFF_MS  = 120 * 60 * 1000   # 2h — close losing positions
        _MAX_HOLD_MS      = 360 * 60 * 1000   # 6h — opportunity cost cap (close all)
        _cascade_ext_mult = 2.0                # cascade active → extend limits by 2×

        while True:
            await asyncio.sleep(60.0)
            try:
                _now_ms = int(time.time() * 1000)
                _cascade_alive = (
                    bool(vc_monitor and vc_monitor.get_status().get("cascade_active", False))
                ) if vc_monitor is not None else False
                _loser_cutoff = int(_LOSER_CUTOFF_MS * (_cascade_ext_mult if _cascade_alive else 1.0))
                _max_hold    = int(_MAX_HOLD_MS     * (_cascade_ext_mult if _cascade_alive else 1.0))

                for _sym, _positions in list(position_manager._positions.items()):
                    if not _positions:
                        continue
                    _pos = _positions[0]
                    if _pos.tp1_hit:
                        continue   # trailing stop owns this one
                    _age_ms = _now_ms - _pos.opened_at_ms
                    # No cutoff hit yet — skip entirely
                    if _age_ms < _loser_cutoff:
                        continue
                    _mark_store = mark_price_stores.get(_sym)
                    if not _mark_store:
                        continue
                    _mark = float(_mark_store.mark_price or 0)
                    if _mark <= 0:
                        continue
                    if _pos.side == "long":
                        _upnl = (_mark - _pos.entry_price) * _pos.size
                    else:
                        _upnl = (_pos.entry_price - _mark) * _pos.size
                    _profit_threshold = 0.3 * _pos.atr * _pos.size if _pos.atr > 0 else 0
                    _is_winner = _upnl >= _profit_threshold
                    # Winners skip the 2h loser cut — but not the 6h opportunity cap
                    if _is_winner and _age_ms < _max_hold:
                        continue
                    _sym_id = SYMBOL_IDS.get(_sym, 0)
                    if _sym_id == 0:
                        logger.warning("time_stop_skipped_no_sym_id", symbol=_sym)
                        continue

                    # Pre-close dust guard — sub-step positions can't be closed;
                    # purge tracking now rather than hitting "quantity is invalid" at 60s cadence.
                    _ts_min_step = _CLOSE_STEP_SIZES.get(_sym, 0.01)
                    try:
                        _ts_size = float(_pos.size)
                    except (TypeError, ValueError):
                        _ts_size = 0.0
                    if _ts_size < _ts_min_step:
                        _record_close(_sym, _pos, _upnl, _mark, "ts_dust_purged")
                        _dust_purge_blocklist[_sym] = time.time() + 120.0
                        logger.warning("time_stop_dust_purged", symbol=_sym,
                                       size=_ts_size, min_step=_ts_min_step,
                                       note="sub-step position removed before time-stop close")
                        continue

                    _ts_reason = "time_stop_max_hold_6h" if _age_ms >= _max_hold else "time_stop_loser_2h"
                    logger.info("time_stop_triggered", symbol=_sym,
                                reason=_ts_reason,
                                age_minutes=round(_age_ms / 60000, 1),
                                upnl=round(_upnl, 4),
                                mark=round(_mark, 4),
                                entry=round(_pos.entry_price, 4),
                                cascade_extended=_cascade_alive)
                    _ts_close = await _close_with_retry(
                        _sym, _sym_id, _pos.side, _pos.size, reason=_ts_reason
                    )
                    if _ts_close and _ts_close.success:
                        _ts_pnl = (
                            (_mark - _pos.entry_price) * _pos.size
                            if _pos.side == "long"
                            else (_pos.entry_price - _mark) * _pos.size
                        )
                        _record_close(_sym, _pos, _ts_pnl, _mark, _ts_reason)
                        logger.info("time_stop_closed", symbol=_sym,
                                    pnl=round(_ts_pnl, 4), order_id=_ts_close.order_id)
                    elif _ts_close:
                        _tserr = _ts_close.error or ""
                        if "not found" in _tserr.lower() or "no position" in _tserr.lower():
                            _ts_pnl = (
                                (_mark - _pos.entry_price) * _pos.size
                                if _pos.side == "long"
                                else (_pos.entry_price - _mark) * _pos.size
                            )
                            _record_close(_sym, _pos, _ts_pnl, _mark, "external_close")
                            logger.info("time_stop_external_close_detected",
                                        symbol=_sym, pnl=round(_ts_pnl, 4))
                        else:
                            logger.warning("time_stop_close_failed",
                                           symbol=_sym, error=_tserr)
            except Exception as _tse2:
                logger.error("time_stop_loop_error", error=str(_tse2))

    async def _regime_flip_monitor_loop() -> None:
        """
        Regime flip exit — 30s cadence.
        When regime confidence drops below 0.60, or the lagging category rotates to cover
        a symbol we hold, close that position. This prevents holding a position whose thesis
        has been invalidated by a macro regime shift.
        """
        _prev_lagging: str = "none"
        _prev_conf:    float = 0.0

        while True:
            await asyncio.sleep(30.0)
            try:
                _rfm_rs = regime_engine.last_state()
                if _rfm_rs is None:
                    continue
                _rfm_conf = float(getattr(_rfm_rs, "confidence", 0.0) or 0.0)
                _rfm_lag  = str(getattr(_rfm_rs, "lagging_category", "none") or "none")
                _rfm_reg  = str(getattr(_rfm_rs, "regime", "") or "")

                # Detect regime flip: confidence collapsed or lagging category changed
                _conf_collapsed = _rfm_conf < 0.60 and _prev_conf >= 0.70
                _lag_rotated    = (_rfm_lag != _prev_lagging and
                                   _rfm_conf >= 0.60 and
                                   _rfm_lag not in ("none", "unknown") and
                                   _prev_lagging not in ("none", "unknown"))
                _prev_conf    = _rfm_conf
                _prev_lagging = _rfm_lag

                if not _conf_collapsed and not _lag_rotated:
                    continue

                # Skip entirely when no positions are open — avoids 17x/session log noise
                # during transitioning when lagging rotates every 30s with nothing to close.
                _open_count = sum(len(v) for v in position_manager._positions.values())
                if _open_count == 0:
                    continue

                flip_reason = "regime_conf_collapse" if _conf_collapsed else "lagging_sector_rotated"
                logger.info("regime_flip_detected", reason=flip_reason,
                            confidence=round(_rfm_conf, 3), lagging=_rfm_lag, regime=_rfm_reg,
                            open_positions=_open_count)

                for _rsym, _rpositions in list(position_manager._positions.items()):
                    if not _rpositions:
                        continue
                    _rpos = _rpositions[0]
                    _rsym_cat = ASSET_CATEGORIES.get(_rsym, "unknown")

                    # Close if symbol is now in lagging sector (when lag rotated)
                    # OR close longs if confidence collapsed (regime structure invalid)
                    _should_exit = False
                    if _lag_rotated and _rsym_cat == _rfm_lag:
                        _should_exit = True
                    elif _conf_collapsed and _rfm_reg in ("transitioning", "confused"):
                        _should_exit = True   # no valid structure — don't hold

                    if not _should_exit:
                        continue

                    _rsym_id = SYMBOL_IDS.get(_rsym, 0)
                    if _rsym_id == 0:
                        continue
                    _rmk_store = mark_price_stores.get(_rsym)
                    if not _rmk_store:
                        continue
                    _rmk = _rmk_store.mark_price
                    if not _rmk or _rmk <= 0:
                        continue

                    logger.info("regime_flip_exit_triggered", symbol=_rsym,
                                side=_rpos.side, category=_rsym_cat,
                                reason=flip_reason, lagging=_rfm_lag,
                                confidence=round(_rfm_conf, 3))
                    _rfm_close = await _close_with_retry(
                        _rsym, _rsym_id, _rpos.side, _rpos.size,
                        reason=f"regime_flip:{flip_reason}",
                    )
                    if _rfm_close and _rfm_close.success:
                        _rfm_pnl = (
                            (_rmk - _rpos.entry_price) * _rpos.size
                            if _rpos.side == "long"
                            else (_rpos.entry_price - _rmk) * _rpos.size
                        )
                        _record_close(_rsym, _rpos, _rfm_pnl, _rmk, f"regime_flip:{flip_reason}")
                        logger.info("regime_flip_closed", symbol=_rsym,
                                    pnl=round(_rfm_pnl, 4), reason=flip_reason)
                    elif _rfm_close:
                        logger.warning("regime_flip_close_failed",
                                       symbol=_rsym, error=_rfm_close.error)

            except Exception as _rfme:
                logger.error("regime_flip_monitor_error", error=str(_rfme))

    async def _coherence_decay_loop() -> None:
        """
        60s cadence — checks open positions for signal coherence evaporation.
        Sources current coherence from _last_signal_coh (updated on every on_signal_ready call).
        Closes on severe decay (≥50%), closes losers on moderate decay (≥30%),
        and trims winners (≥25%) to lock in profit.
        """
        while True:
            await asyncio.sleep(60.0)
            try:
                if not config.coherence_decay_enabled:
                    continue
                for _cd_sym, _cd_positions in list(position_manager._positions.items()):
                    if not _cd_positions:
                        continue
                    _cd_pos  = _cd_positions[0]
                    _cd_coh  = float(_last_signal_coh.get(_cd_sym, 0.0))
                    _cd_mps  = mark_price_stores.get(_cd_sym)
                    _cd_mark = float(_cd_mps.mark_price or 0.0) if _cd_mps else 0.0
                    if _cd_mark <= 0:
                        continue
                    _cd_entry = float(getattr(_cd_pos, 'entry_price', 0.0) or 0.0)
                    _cd_side  = getattr(_cd_pos, 'side', 'long')
                    _cd_sz    = float(getattr(_cd_pos, 'size', 0.0) or 0.0)
                    _cd_upnl  = (
                        (_cd_mark - _cd_entry) * _cd_sz if _cd_side == 'long'
                        else (_cd_entry - _cd_mark) * _cd_sz
                    ) if _cd_entry > 0 and _cd_sz > 0 else 0.0

                    _cd_action = _coherence_decay.check_position(_cd_pos, _cd_coh, _cd_upnl)
                    if _cd_action in ("close_severe", "close_loss"):
                        _cd_sym_id = SYMBOL_IDS.get(_cd_sym, 0)
                        if _cd_sym_id == 0:
                            logger.warning("coherence_decay_no_sym_id", symbol=_cd_sym)
                            continue
                        _cd_close = await _close_with_retry(
                            _cd_sym, _cd_sym_id, _cd_side, _cd_sz,
                            reason=f"coherence_decay_{_cd_action}",
                        )
                        if _cd_close and _cd_close.success:
                            _record_close(_cd_sym, _cd_pos, _cd_upnl, _cd_mark,
                                          f"coherence_decay_{_cd_action}")
                            logger.warning("coherence_decay_closed",
                                           symbol=_cd_sym, action=_cd_action,
                                           coherence=round(_cd_coh, 2),
                                           upnl=round(_cd_upnl, 4))
                    elif _cd_action == "trim_winner":
                        # Log intent; full partial-close execution requires SoDEX reduce-only
                        logger.info("coherence_decay_trim_logged",
                                    symbol=_cd_sym, coherence=round(_cd_coh, 2),
                                    upnl=round(_cd_upnl, 4),
                                    note="trailing_stop_will_protect")
            except asyncio.CancelledError:
                raise
            except Exception as _cde:
                logger.debug("coherence_decay_loop_error", error=str(_cde))

    async def execution_cleanup_loop() -> None:
        """
        Execution monitoring supervisor.

        Replaces the former 460-line monolithic coroutine with 7 independent sub-loops,
        each with its own cadence and error boundary:

          Sub-loop              Cadence   What it does
          ─────────────────     ───────   ──────────────────────────────────────────
          _stop_guardian        0.5 s     mark-vs-stop check; I/O only when stop fires
          _mae_mfe              1.0 s     max adverse/favourable excursion tracking
          _balance_feedback     1.0 s     balance REST + display + feedback + purge
          _reconciliation       5.0 s     REST position sync + TP hit detection
          _trailing_stop        10  s     trailing stop ratchet
          _software_tp          2.0 s     software TP for positions without exchange TP
          _time_stop            60  s     capital-efficiency time stop
          _coherence_decay      60  s     close/trim on signal evaporation

        Key benefit: a 80ms SoDEX REST stall in _reconciliation_loop NO LONGER delays
        the stop guardian. Each sub-loop catches its own exceptions so one crash does
        not kill the others. The supervised outer gather (in the main gather) restarts
        this entire group if all sub-loops somehow exit.
        """
        _sub_names = [
            "stop_guardian", "mae_mfe",
            "balance_feedback", "reconciliation", "trailing_stop",
            "software_tp", "time_stop", "regime_flip_monitor",
            "coherence_decay",
        ]
        results = await asyncio.gather(
            _stop_guardian_loop(),
            _mae_mfe_loop(),
            _balance_and_feedback_loop(),
            _reconciliation_loop(),
            _trailing_stop_loop(),
            _software_tp_loop(),
            _time_stop_loop(),
            _regime_flip_monitor_loop(),
            _coherence_decay_loop(),
            return_exceptions=True,
        )
        for _name, _res in zip(_sub_names, results):
            if isinstance(_res, BaseException) and not isinstance(_res, asyncio.CancelledError):
                logger.critical("execution_sub_loop_exited", name=_name, error=repr(_res))

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
                    _live_funding_rates.update(real_rates)  # share with on_signal_ready for carry veto
                    logger.info("funding_rates_fetched", source="sodex_rest", count=len(real_rates))

                # Persist to history
                for symbol in config.assets:
                    rate = _last_known_rates.get(symbol, 0.0)
                    funding_history.add(symbol, rate, "sodex_rest")

                # Update funding radar and display
                snapshots = await funding_radar.update_all()
                display.update_funding(snapshots)
                # Log only arb-worthy opportunities (carry_score ≥ 1.5), not all 15 symbols
                _arb_opps = {s: sn for s, sn in snapshots.items() if abs(sn.carry_score) >= 1.5}
                if _arb_opps:
                    logger.info("funding_arb_opportunities",
                                count=len(_arb_opps),
                                symbols={s: round(sn.carry_score, 2) for s, sn in _arb_opps.items()})

                # Phase 11: run FundingAgent perceive() for each active asset
                for _fsym in config.assets:
                    try:
                        _fout = await _funding_agent.perceive(_fsym, reason="funding_loop")
                        display.push_agent_state("funding", _fout)
                    except Exception:
                        pass

            except Exception as e:
                logger.error("funding_loop_error", error=str(e), traceback=traceback.format_exc())

            await asyncio.sleep(300)

    async def true_arb_loop():
        """
        True delta-neutral arb loop (Tier 7 — spot+perp funding harvest).

        Runs every 5 minutes:
          1. Determine effective funding rate (Bybit leads SoDEX — use Bybit as entry signal).
          2. For each asset, check if funding rate warrants a new arb position.
          3. For open positions, check exit conditions (basis convergence, rate flip, time).
          4. Accrue funding every 8h to open positions.

        Entry signal: Bybit 8h funding rate (stored via add_bybit_rate()).
        Bybit rates reflect true market consensus — SoDEX follows within hours.
        This is the correct institutional approach: enter on Bybit signal,
        collect when SoDEX rate normalises upward to match.

        Only active in live mode with a real spot client.
        ValueChain cascade guard applied before any new position opens.
        """
        if true_arb is None or spot_client is None:
            return   # Spot client unavailable — arb not configured

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

                # Fetch SoDEX rates for exit / accrue reference
                sodex_rates = await ws_manager.fetch_funding_rates() or {}

                # Determine cascade state from VC monitor
                cascade = vc_monitor.is_cascade_active() if vc_monitor else False

                for symbol in config.assets:
                    # Entry signal: prefer Bybit rate (price-discovery leader).
                    # Fall back to SoDEX rate if Bybit not yet received.
                    # SoDEX rates are near-zero on thin books and never cross MIN_FUNDING_RATE
                    # alone — Bybit rates (0.0001–0.001) are the actionable signal.
                    bybit_rate = funding_history.get_latest_bybit_rate(symbol)
                    sodex_rate = sodex_rates.get(symbol, 0.0)
                    rate = bybit_rate if bybit_rate is not None else sodex_rate

                    if rate == 0.0:
                        continue

                    # Check exits for open positions (use SoDEX rate for exit logic —
                    # it's the rate we're actually collecting on the perp leg)
                    if symbol in [p.symbol for p in true_arb.get_open_positions()]:
                        spot_price = await spot_client.get_spot_price(symbol)
                        perp_price = getattr(mark_price_stores.get(symbol, None),
                                             "mark_price", spot_price)
                        exit_rate = sodex_rate if sodex_rate != 0.0 else rate
                        await true_arb.check_exits(symbol, exit_rate, spot_price, perp_price)
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

                # Accrue funding every 8h (use SoDEX rate — actual collected rate)
                _funding_accrue_counter += 1
                if _funding_accrue_counter >= 96:   # 96 × 5m = 8h
                    _funding_accrue_counter = 0
                    for pos in true_arb.get_open_positions():
                        sym = pos.symbol
                        # SoDEX rate is the actual funding collected; fall back to Bybit
                        _acc_rate = sodex_rates.get(sym, 0.0) or (
                            funding_history.get_latest_bybit_rate(sym) or 0.0
                        )
                        notional = pos.spot_qty * pos.spot_entry
                        true_arb.accrue_funding(sym, _acc_rate, notional)

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
                balance = _cached_balance[0] or await client.get_account_balance(acc_id) or 0.0
                if not balance or balance <= 0:
                    await asyncio.sleep(3600)
                    continue
                nav = vault_manager.get_total_nav(balance)
                if nav is None or nav <= 0:
                    await asyncio.sleep(3600)
                    continue

                # 2. Accrue legacy vault fees — guard against None HWM on first run
                _hwm = vault_manager.high_water_mark or 0.0
                fees = fee_engine.process_vault_fees(nav, _hwm)

                # 3. Accrue per-bot management fee (surgical ledger)
                mgmt_fee = bot_fee_ledger.accrue_management(balance)

                # 4. Save performance cert
                perf_cert.save_to_file()

                fee_summary = bot_fee_ledger.get_summary()
                logger.info("vault_report",
                            bot=bot_fee_ledger.bot_id,
                            nav=f"${nav:.2f}",
                            legacy_fees=f"${fees['total_fees']:.4f}",
                            bot_mgmt_fee=f"${mgmt_fee:.6f}",
                            total_perf_fees=f"${fee_summary['total_performance_fees']:.4f}",
                            total_mgmt_fees=f"${fee_summary['total_management_fees']:.6f}",
                            hwm=f"${fee_summary['high_water_mark']:.2f}",
                            recipient=fee_summary['recipient'])

                if nav > (vault_manager.high_water_mark or 0.0):
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

        _bm_prev_balance: float = 0.0  # withdrawal detection anchor

        while True:
            try:
                balance = _cached_balance[0]
                if balance > 0:
                    # Flag-based force reset: touch logs/reset_drawdown.flag to clear halt
                    _reset_flag = Path("logs/reset_drawdown.flag")
                    if _reset_flag.exists():
                        try:
                            drawdown_manager._peak_balance    = balance
                            drawdown_manager._low_watermark   = balance
                            drawdown_manager._session_start   = balance
                            drawdown_manager._week_start      = balance
                            drawdown_manager._halted          = False
                            drawdown_manager._halt_reason     = ""
                            drawdown_manager._size_multiplier = 1.0
                            drawdown_manager._save_state()
                            _reset_flag.unlink()
                            _bm_prev_balance = balance  # reset anchor too
                            logger.warning("drawdown_manager_force_reset",
                                           balance=round(balance, 2),
                                           note="reset_drawdown.flag consumed")
                        except Exception as _rfe:
                            logger.error("drawdown_reset_flag_error", error=str(_rfe))

                    # ── Withdrawal / deposit auto-detection ──────────────────
                    # If balance dropped by >$2 with ZERO open positions, the
                    # drop is an external withdrawal — not a trading loss.
                    # Shift all drawdown anchors down so DD% is not inflated.
                    if _bm_prev_balance > 0:
                        _bm_delta = balance - _bm_prev_balance
                        _open_pos = len(position_manager.get_all()) if position_manager else 0
                        if _bm_delta < -2.0 and _open_pos == 0:
                            drawdown_manager.apply_balance_adjustment(
                                _bm_delta, reason="external_withdrawal_detected"
                            )
                            logger.info(
                                "withdrawal_anchors_adjusted",
                                delta=round(_bm_delta, 2),
                                new_balance=round(balance, 2),
                                note="anchors shifted to prevent false DD halt",
                            )
                        elif _bm_delta > 2.0:
                            # Deposit: shift anchors UP so new capital isn't
                            # mistaken for recovery from a loss
                            drawdown_manager.apply_balance_adjustment(
                                _bm_delta, reason="external_deposit_detected"
                            )
                            logger.info(
                                "deposit_anchors_adjusted",
                                delta=round(_bm_delta, 2),
                                new_balance=round(balance, 2),
                            )
                    _bm_prev_balance = balance
                    # ─────────────────────────────────────────────────────────

                    drawdown_manager.update_balance(balance)

                # Daily reset at UTC midnight
                now_utc = _dt.datetime.now(_dt.timezone.utc)
                if now_utc.day != _last_day:
                    drawdown_manager.reset_daily()
                    _exec_guardian.reset_day()
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

                # Update shared UI state every 30s
                try:
                    _perf_stats = perf.compute(journal) if perf and journal else None
                except Exception:
                    _perf_stats = None
                _ui_state.update_session(
                    balance        = _cached_balance[0],
                    wr             = getattr(_perf_stats, "win_rate", 0.0) if _perf_stats else 0.0,
                    trades         = getattr(_perf_stats, "closed_trades", 0) if _perf_stats else 0,
                    pnl            = getattr(_perf_stats, "total_pnl_usd", 0.0) if _perf_stats else 0.0,
                    drawdown_pct   = dd_tracker.session_drawdown_pct,
                    will_state     = nietzsche_engine.will_state.value,
                    open_positions = len(position_manager.get_all()),
                )

            except Exception as _bme:
                logger.error("balance_monitor_loop_error", error=str(_bme))

            # Persist engine states every 30s so restarts warm up instantly.
            try:
                liq_engine.save_state("logs/liq_phase_state.json")
                regime_engine._save_state("logs/regime_state.json")
            except Exception:
                pass

            await asyncio.sleep(30)

    async def prediction_drain_loop():
        """
        Drains the PredictionStore queue every 1s.
        add_pending() is synchronous (queue.put_nowait); this loop moves items
        from the queue into the circular deque so check_bet() can see them.
        """
        while True:
            await asyncio.sleep(1.0)
            try:
                await prediction_store._drain_once()
                # Refresh prediction UI state after each drain
                _active  = [
                    {"id": r.id, "symbol": r.symbol, "direction": r.direction,
                     "confidence": r.confidence, "personality": r.personality,
                     "ts": r.timestamp_ms}
                    for r in prediction_store._records if r.outcome is None
                ]
                _resolved = [
                    {"id": r.id, "symbol": r.symbol, "outcome": r.outcome,
                     "actual_r": getattr(r, "actual_r", 0.0),
                     "personality": r.personality}
                    for r in prediction_store._records if r.outcome is not None
                ][-10:]
                _acc = prediction_store.accuracy_today()
                _ui_state.update_predictions(
                    active   = _active,
                    bets     = [],
                    resolved = _resolved,
                    accuracy = _acc,
                )
            except Exception as _pde:
                logger.debug("prediction_drain_loop_error", error=str(_pde))

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

    async def cascade_aftermath_loop():
        """
        v2.0: Polls CascadeTracker every 15s for aftermath evaluation.
        Three responsibilities:
          1. cascade_tracker.check_aftermath() — BLOCKED→PRIMED transition
             via 5-signal evaluation with dynamic dwell.
          2. Sync cascade_tracker PRIMED → _aftermath_primed so on_signal_ready
             uses the tracker's authoritative aftermath state (not just the
             delayed _evaluate_cascade_aftermath() which only fires at T+90s).
          3. Drive liq_engine.on_silence_tick() per symbol — required for
             LiqPhase.EXHAUSTION→AFTERMATH transition, which can only happen
             during silence (no new events → _advance_phase() never runs).
        """
        nonlocal _aftermath_primed, _aftermath_direction, _aftermath_expires_ms
        while True:
            try:
                _prev_cascade_phase = getattr(cascade_tracker, "_last_logged_phase", None)
                cascade_tracker.check_aftermath()
                phase = cascade_tracker.get_phase().value

                # ── MOMENTUM cascade: execute immediately ──
                # The highest-conviction cascade signal. Trade WITH the pressure.
                # No interpreter, no coherence floor — liquidation IS the signal.
                if cascade_tracker.is_momentum():
                    _mom_dir, _mom_notional = cascade_tracker.consume_momentum()
                    if _mom_dir:
                        logger.info("cascade_momentum_consumed_wired",
                                    direction=_mom_dir,
                                    notional_usd=round(_mom_notional, 0))
                        asyncio.ensure_future(_execute_cascade_momentum(_mom_dir, _mom_notional))

                # Sync cascade_tracker PRIMED → _aftermath_primed.
                # The tracker evaluates 5 signals (price_overshoot, vpin, funding,
                # orderbook, cross_venue) with dynamic dwell — it is the
                # authoritative aftermath signal. Wire it so trades don't wait
                # for the T+90s _evaluate_cascade_aftermath() hard delay.
                if cascade_tracker.is_primed() and not _aftermath_primed:
                    _ct_dir = cascade_tracker.get_primed_direction()
                    if _ct_dir:
                        _aftermath_primed = True
                        _aftermath_direction = _ct_dir
                        _aftermath_expires_ms = int(time.time() * 1000) + 300_000
                        logger.info("aftermath_primed_from_tracker",
                                    direction=_ct_dir,
                                    dwell_basis="cascade_tracker_dynamic",
                                    note="5signal_eval_wired_to_trade_path")
                        # Active scan: force signal evaluation for leading-regime symbols
                        # so aftermath window doesn't expire waiting for organic signals.
                        _rs_scan = regime_engine.last_state()
                        _lead = getattr(_rs_scan, "leading_category", "none") if _rs_scan else "none"
                        _scan_syms = [
                            s for s in config.assets
                            if ASSET_CATEGORIES.get(s) == _lead and s in mark_price_stores
                        ][:5]
                        for _asym in _scan_syms:
                            asyncio.ensure_future(interpreter._build_and_publish(_asym))
                        logger.info("aftermath_active_scan",
                                    direction=_ct_dir, category=_lead, symbols=_scan_syms)

                # Drive LiqPhaseEngine silence ticks for EXHAUSTION→AFTERMATH.
                # AFTERMATH requires silence — new events never arrive during it,
                # so _advance_phase() would never fire without explicit ticks.
                for _sil_sym in config.assets:
                    try:
                        liq_engine.on_silence_tick(_sil_sym)
                    except Exception:
                        pass

                # Log + push to display feed on transitions
                if phase != _prev_cascade_phase:
                    cascade_tracker._last_logged_phase = phase
                    if phase in ("primed", "momentum"):
                        summary = cascade_tracker.get_summary()
                        _dir = summary.get("primed_direction") or summary.get("momentum_direction")
                        logger.info("cascade_phase_changed",
                                    phase=phase,
                                    direction=_dir,
                                    aftermath=summary.get("aftermath_signals"))
                        display.push_cascade_phase_event(
                            from_phase=_prev_cascade_phase or "idle",
                            to_phase=phase,
                            direction=_dir or "",
                            summary=summary,
                        )
                    elif phase == "blocked":
                        logger.info("cascade_phase_changed", phase="blocked")
                        display.push_cascade_phase_event(
                            from_phase=_prev_cascade_phase or "idle",
                            to_phase="blocked",
                            direction="",
                            summary=cascade_tracker.get_summary(),
                        )
                    elif phase == "idle" and _prev_cascade_phase in ("primed", "momentum", "blocked"):
                        logger.info("cascade_phase_changed", phase="idle")
                        display.push_cascade_phase_event(
                            from_phase=_prev_cascade_phase,
                            to_phase="idle",
                            direction="",
                            summary={},
                        )
            except Exception as _cae:
                logger.error("cascade_aftermath_loop_error", error=str(_cae))
            await asyncio.sleep(15)

    async def oracle_loop():
        """
        ORACLE pre-cascade smart money detector — runs every 30s.
        Feeds VPIN, OI delta, cross-venue basis, and funding drift into OracleEngine.
        When ≥3 sub-signals align, OracleEngine fires a cluster signal that boosts
        coherence for matching signals in on_signal_ready.
        """
        while True:
            try:
                if getattr(config, 'oracle_enabled', True):
                    for sym in ("BTC-USD", "ETH-USD", "SOL-USD"):
                        # Sub-signal 1: VPIN from MarkPriceStore
                        _mp_store = mark_price_stores.get(sym)
                        if _mp_store:
                            _vpin_val = float(getattr(_mp_store, "_vpin", 0.0) or 0.0)
                            _oracle_engine.update_vpin(sym, _vpin_val)

                        # Sub-signal 2: Bybit OI (field is "open_interest" in ticker store)
                        _oi_val = float(bybit_ticker_stores.get(sym, {}).get("open_interest", 0.0) or 0.0)
                        if _oi_val > 0:
                            _oracle_engine.update_oi(sym, _oi_val)

                        # Sub-signal 3: Cross-venue basis (Bybit vs SoDEX mark)
                        _bybit_mk = float(bybit_ticker_stores.get(sym, {}).get("mark_price", 0.0) or 0.0)
                        _sodex_mk = 0.0
                        if _mp_store:
                            _sodex_mk = float(
                                getattr(_mp_store, "latest_mark", None) or
                                getattr(_mp_store, "_mark", 0.0) or 0.0
                            )
                        if _bybit_mk > 0 and _sodex_mk > 0:
                            _oracle_engine.update_basis(sym, _bybit_mk, _sodex_mk)

                        # Sub-signal 4 (per anchor): Bybit funding rate trend — far more
                        # meaningful than SoDEX rates (~1.25e-05/hr, essentially zero).
                        _bybit_fr = float(bybit_ticker_stores.get(sym, {}).get("funding_rate", 0.0) or 0.0)
                        if _bybit_fr != 0.0:
                            _oracle_engine.update_funding(sym, _bybit_fr)

                    _oracle_engine.tick()
            except Exception as _oe:
                logger.debug("oracle_loop_error", error=str(_oe))
            await asyncio.sleep(30)

    async def calendar_loop():
        """Periodic calendar updates and log blocks"""
        while True:
            try:
                states = await calendar_engine.get_states_all(config.assets)
                _any_block = False
                for symbol, s in states.items():
                    if s.regime == "BLOCK":
                        logger.warning("calendar_block_active", symbol=symbol, reason=s.reason)
                        _any_block = True
                    elif s.regime == "CAUTION":
                        logger.info("calendar_caution_active", symbol=symbol, reason=s.reason, size_mult=s.size_multiplier)
                # Notify macro engine of portfolio-level calendar regime
                _cal_regime = "BLOCK" if _any_block else "CLEAR"
                if hasattr(interpreter, "_macro"):
                    interpreter._macro.update_calendar(_cal_regime)
                # Personality cache: calendar states feed SHIELD detection
                context_cache.update_calendar(states)
            except Exception as e:
                logger.error("calendar_loop_error", error=str(e))
            await asyncio.sleep(300) # 5 mins

    async def fee_update_loop():
        """
        Refresh SoDEX fee tier data every 12 hours.
        Also fetches live maker/taker rates from the exchange fee-rate endpoint
        so the fee engine uses authoritative rates (not just hardcoded tables).

        Rate budget: fee-rate=2 per call × 2 (spot+perp) × 2/day = 8 weight/day.
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

            # Sleep 12 hours — refresh twice daily to keep fee tier current
            await asyncio.sleep(12 * 3600)

    async def journal_cleanup_loop():
        """
        Nightly journal maintenance at 00:05 UTC.

        1. Archives the previous day's journal to logs/journal_archive/.
        2. Marks any surviving approved+open entries as "abandoned" (restart orphans).
        3. Leaves closed (win/loss) and rejected entries intact.

        Runs every 24 hours. Wrapped in try/except — a cleanup failure never
        affects trading, signal flow, or learning system.
        """
        import datetime as _dt
        import shutil as _shutil
        _archive_dir = Path("logs/journal_archive")
        _archive_dir.mkdir(parents=True, exist_ok=True)

        while True:
            try:
                # Sleep until 00:05 UTC tomorrow
                _now = _dt.datetime.now(_dt.timezone.utc)
                _next = (_now + _dt.timedelta(days=1)).replace(
                    hour=0, minute=5, second=0, microsecond=0)
                await asyncio.sleep(max(60.0, (_next - _now).total_seconds()))

                # Archive yesterday's journal
                _yesterday = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=1)).date()
                _stale_file = Path(f"logs/trade_journal_{_yesterday}.json")
                if _stale_file.exists():
                    _dest = _archive_dir / _stale_file.name
                    _shutil.copy2(_stale_file, _dest)
                    logger.info("journal_archived", file=str(_stale_file), dest=str(_dest))

                # Mark orphaned entries in the current in-memory journal
                _abandoned = 0
                for _je in journal.entries:
                    if _je.get("approved") and _je.get("outcome") in (None, "open"):
                        _je["outcome"] = "abandoned"
                        _abandoned += 1
                if _abandoned:
                    journal.save_nonblocking()
                    logger.info("journal_nightly_cleanup", abandoned=_abandoned)

            except Exception as _jce:
                logger.error("journal_cleanup_error", error=str(_jce))
                await asyncio.sleep(3600)

    async def nightly_calibration_loop():
        """
        Runs full calibration at 00:30 UTC every night.
        Updates stop multipliers, coherence threshold, and session weights
        from accumulated trade history. All wrapped in try/except — a
        calibration failure never affects trading.
        """
        import datetime as _dt
        while True:
            try:
                if _trade_db is None or _calibration_engine is None or _param_store is None:
                    await asyncio.sleep(3600)
                    continue
                now_utc = _dt.datetime.now(_dt.timezone.utc)
                next_run = (now_utc + _dt.timedelta(days=1)).replace(
                    hour=0, minute=30, second=0, microsecond=0)
                sleep_s = max(60.0, (next_run - now_utc).total_seconds())
                await asyncio.sleep(sleep_s)
                logger.info("nightly_calibration_start",
                            trade_history=len(_trade_db.get_all()))
                _night_cal = _calibration_engine.run()
                _night_changes = _apply_calibration(_night_cal, _param_store)
                _stats = _trade_db.get_stats()
                logger.info("nightly_calibration_done",
                            **_stats,
                            params_updated=len(_night_changes),
                            stop_mults=_param_store.stop_mult_summary())
            except Exception as _nce:
                logger.error("nightly_calibration_error", error=str(_nce))
                await asyncio.sleep(3600)  # retry in 1h if it crashes

    async def display_refresh_loop():
        """
        5-second display heartbeat — pure READ, zero logic.

        Phase 11 removed the polling loop that kept the terminal fed with scores,
        regime, and rejection reasons every tick.  Event-driven agents are correct
        and stay untouched.  This loop only pushes already-computed state from the
        interpreter and risk-engine caches to the display every 5 s so the terminal
        never goes stale between events.

        Rules:
          - Never calls risk_engine.validate()
          - Never triggers trades
          - Never recomputes ATR, coherence, or MarketContext from scratch
          - Only reads _last_state_cache, _last_market_context, _rejection_cache
        """
        nonlocal _last_market_context
        while True:
            await asyncio.sleep(5)
            try:
                from core.system_state import SystemPhase as _SP
                if system_state.get_global_phase() == _SP.WARMING_UP:
                    continue

                # Push last known MarketContext — Market News panel
                if _last_market_context is not None:
                    display.update_market_context(_last_market_context)

                # Push last known score + rejection reason per symbol
                for _sym in config.assets:
                    try:
                        _state = interpreter.get_last_state(_sym)
                        if _state is not None:
                            display.update_cache(f"score_{_sym}", {
                                "score":     getattr(_state, "weighted_score",  0.0),
                                "coherence": getattr(_state, "coherence_score", 0.0),
                                "direction": getattr(_state, "trade_direction", "none"),
                                "atr":       getattr(_state, "atr",             0.0),
                                "mark_price": getattr(_state, "mark_price",     0.0),
                                "state":     _state,
                            })
                        _rej = risk_engine.get_last_rejection(_sym)
                        if _rej:
                            display.update_cache(f"rejection_{_sym}", _rej)
                    except Exception:
                        continue
            except Exception as _dre:
                logger.debug("display_refresh_loop_error", error=str(_dre))

    async def market_context_loop():
        """
        Periodic MarketContext refresh — every 10 s.

        MarketContext.build() lives inside on_signal_ready(), which only fires
        when a directional signal (long/short) is published.  In a flat or
        confused market that can be never, leaving the Market News panel stuck
        on "Awaiting market context...".

        This loop rebuilds the context independently of signal direction so the
        display always has a current snapshot of regime, cascade, funding and
        flow — even during warmup or quiet sessions.
        """
        nonlocal _last_market_context, _last_calendar_state
        while True:
            await asyncio.sleep(10)
            try:
                _cal_state        = _last_calendar_state
                _cal_event_type   = getattr(_cal_state, "nearest_event_type", None) if _cal_state else None
                _cal_hours_to_evt = getattr(_cal_state, "hours_to_event",     None) if _cal_state else None
                _tr = evaluate_time_regime(
                    event_type=_cal_event_type,
                    hours_to_event=_cal_hours_to_evt,
                )
                _last_market_context = MarketContext.build(
                    cascade_tracker          = cascade_tracker,
                    funding_history          = funding_history,
                    trade_flow_stores        = trade_flow_stores,
                    relative_strength_engine = regime_engine,
                    candle_buffers           = candle_buffers,
                    adaptive_calibrator      = _adaptive_calibrator,
                    calendar_state           = _cal_state,
                    assets                   = list(config.assets),
                    time_regime              = _tr,
                )
                interpreter.set_market_context(_last_market_context)
                display.update_market_context(_last_market_context)
            except Exception as _mctx_ex:
                logger.debug("market_context_loop_build_failed", error=str(_mctx_ex))

    async def signal_agent_loop():
        """
        Phase 11 — 15-minute slow-path agent perceive() loop.
        Runs all 6 signal agents for every active asset so each agent's
        _last_outputs is always populated for alignment scoring — even for
        quiet markets or TradFi assets with sparse orderbook events.
        MicroAgent and StructureAgent also fire on events (event bus),
        but this loop guarantees they fire at minimum every 15 minutes.
        """
        while True:
            await asyncio.sleep(900)   # 15 minutes
            for _a_sym in config.assets:
                try:
                    _m_out = await _macro_agent.perceive(_a_sym, reason="signal_agent_loop")
                    display.push_agent_state("macro", _m_out)
                except Exception:
                    pass
                try:
                    _r_out = await _regime_agent.perceive(_a_sym, reason="signal_agent_loop")
                    display.push_agent_state("regime", _r_out)
                except Exception:
                    pass
                try:
                    _s_out = await _ssi_agent.perceive(_a_sym, reason="signal_agent_loop")
                    display.push_agent_state("ssi", _s_out)
                except Exception:
                    pass
                try:
                    _mi_out = await _micro_agent.perceive(_a_sym, reason="signal_agent_loop")
                    display.push_agent_state("micro", _mi_out)
                except Exception:
                    pass
                try:
                    _st_out = await _structure_agent.perceive(_a_sym, reason="signal_agent_loop")
                    display.push_agent_state("structure", _st_out)
                except Exception:
                    pass
                try:
                    _f_out = await _funding_agent.perceive(_a_sym, reason="signal_agent_loop")
                    display.push_agent_state("funding", _f_out)
                except Exception:
                    pass

    async def sovereign_monitor_loop():
        """
        SOVEREIGN component divergence updater — runs every 15 minutes.
        Feeds mark prices of all MAG7 equity components into SSIComponentMonitor
        to compute rolling z-scores. Non-critical: errors logged and skipped.
        """
        from intelligence.ssi_component_monitor import MAG7_COMPONENTS as _MAG7_COMP
        while True:
            await asyncio.sleep(900)   # 15 minutes — cold path, not latency critical
            try:
                # Compute weighted MAG7 index price from live component mark prices
                _index_price = 0.0
                _live_components = 0
                for _sym, _wt in _MAG7_COMP.items():
                    _mstore = mark_price_stores.get(_sym)
                    if _mstore and getattr(_mstore, "mark_price", None) and _mstore.mark_price > 0:
                        _index_price += float(_mstore.mark_price) * _wt
                        _live_components += 1
                if _live_components < 3:
                    # Not enough components live (outside US market hours) — skip
                    logger.debug("sovereign_monitor_skip",
                                 reason="insufficient_component_prices",
                                 live=_live_components)
                    continue
                # Update index reference first so component spreads are computed correctly
                _ssi_monitor.update_index_price(_index_price)
                for _sym in _MAG7_COMP:
                    _mstore = mark_price_stores.get(_sym)
                    if _mstore and getattr(_mstore, "mark_price", None) and _mstore.mark_price > 0:
                        _ssi_monitor.update_price(_sym, float(_mstore.mark_price))
                _z_all = _ssi_monitor.get_all_z_scores()
                _best = _ssi_monitor.get_best_divergence()
                logger.debug(
                    "sovereign_monitor_updated",
                    index_price=round(_index_price, 4),
                    live_components=_live_components,
                    best_sym=_best.symbol if _best else "none",
                    best_z=round(_best.z_score, 2) if _best else 0.0,
                    spot_balance=round(_cached_spot_balance[0], 4),
                )
                # Push spot balance to sovereign display so terminal can show
                # spot balance alongside perp balance (fee reserve visibility)
                if _cached_spot_balance[0] > 0:
                    display.update_cache("sovereign_spot", {
                        "spot_balance_usd": round(_cached_spot_balance[0], 4),
                    })
            except Exception as _sme:
                logger.error("sovereign_monitor_loop_error", error=str(_sme))

    async def yield_accrual_loop():
        """
        SOVEREIGN yield accrual — runs every 8 hours (funding cycle alignment).
        Computes staking yield earned since last call, adds to YieldTracker budget.
        On overflow (budget >= 2× seed), 50% transfers to main capital reserve.
        Non-critical: errors logged and skipped.
        """
        while True:
            await asyncio.sleep(28800)  # 8 hours — aligned with funding periods
            try:
                _new_yield = _staking_monitor.accrue_yield()
                if _new_yield > 0:
                    await _yield_tracker.add_yield(_new_yield)
                    logger.info("yield_accrued",
                                yield_usd=round(_new_yield, 4),
                                budget_usd=round(_yield_tracker.available_budget, 4))
                # Check if budget has grown to 2× seed — transfer surplus to main
                _overflow = await _yield_tracker.check_overflow()
                if _overflow:
                    logger.info("sovereign_overflow_transfer",
                                transfer_usd=round(_overflow, 2),
                                budget_after=round(_yield_tracker.available_budget, 4),
                                note="50% transferred to main capital reserve")
            except Exception as _yae:
                logger.error("yield_accrual_loop_error", error=str(_yae))

    async def sovereign_signal_loop():
        """
        SOVEREIGN autonomous signal loop — runs every 5 minutes.
        Evaluates MAG7 component divergence via SovereignSignalGenerator and
        places bracket orders directly (bypasses coherence pipeline).
        Non-critical: errors logged and skipped.
        """
        from intelligence.ssi_component_monitor import MAG7_COMPONENTS as _SOV_MAG7_COMP
        while True:
            await asyncio.sleep(300)   # 5 minutes
            try:
                # ── Pre-flight guards ────────────────────────────────────────
                if _trading_halted[0]:
                    continue
                if NUMERIC_ACCOUNT_ID == 0:
                    continue
                if _api_circuit_open_until[0] > time.time():
                    continue

                # ── Sovereign context ─────────────────────────────────────────
                _sov_ctx = context_cache._sovereign  # dict: stake_balance, sovereign_budget, component_signals
                _sov_stake = float(_sov_ctx.get("stake_balance", 0.0))
                _sov_budget = float(_sov_ctx.get("sovereign_budget", 0.0))

                if _sov_stake <= 0:
                    continue
                if not _yield_tracker.can_trade():
                    continue

                # ── Open sovereign position cap (max 2) ────────────────────────
                _sov_open = sum(
                    1 for p in position_manager.get_all()
                    if getattr(p, "signal_reason", "").startswith("SOVEREIGN")
                )
                if _sov_open >= 2:
                    logger.debug("sovereign_signal_skipped",
                                 reason="max_open_positions", open_count=_sov_open)
                    continue

                # ── Best divergence from SSI monitor ────────────────────────────
                _sov_div = _ssi_monitor.get_best_divergence()
                if _sov_div is None:
                    continue

                # ── Regime from context cache ────────────────────────────────────
                _sov_regime = getattr(context_cache, "_regime", "confused") or "confused"

                # ── Calendar check for the divergence symbol ────────────────────
                _sov_cal = await calendar_engine.get_state(_sov_div.symbol)
                if _sov_cal.regime == "BLOCK":
                    logger.debug("sovereign_signal_skipped",
                                 symbol=_sov_div.symbol, reason="calendar_block",
                                 cal_regime=_sov_cal.regime)
                    continue

                # ── Hours to earnings — use CalendarState if nearest event is EARNINGS_MAG7
                _sov_hours_to_earnings = None
                if (
                    getattr(_sov_cal, "nearest_event_type", None) == "EARNINGS_MAG7"
                    and getattr(_sov_cal, "hours_to_event", None) is not None
                ):
                    _sov_hours_to_earnings = float(_sov_cal.hours_to_event)

                # ── Evaluate signal ──────────────────────────────────────────────
                _sov_signal = SovereignSignalGenerator().evaluate(
                    divergence        = _sov_div,
                    regime            = _sov_regime,
                    calendar_regime   = _sov_cal.regime,
                    hours_to_earnings = _sov_hours_to_earnings,
                    stake_balance     = _sov_stake,
                    component_weights = _SOV_MAG7_COMP,  # symbol→weight dict
                    sovereign_budget  = _sov_budget,
                )
                if _sov_signal is None:
                    continue

                # ── Mark price check ─────────────────────────────────────────────
                _sov_mstore = mark_price_stores.get(_sov_signal.symbol)
                if _sov_mstore is None:
                    continue
                _sov_entry = float(getattr(_sov_mstore, "mark_price", 0.0) or 0.0)
                if _sov_entry <= 0:
                    continue

                # ── ATR proxy + stop/TP calculation ──────────────────────────────
                _sov_atr = _sov_entry * 0.015   # 1.5% ATR proxy for equity perps
                if _sov_signal.side == "long":
                    _sov_stop = _sov_entry - 2.0 * _sov_atr
                    _sov_tp1  = _sov_entry + 1.5 * _sov_atr * 1.5
                else:
                    _sov_stop = _sov_entry + 2.0 * _sov_atr
                    _sov_tp1  = _sov_entry - 1.5 * _sov_atr * 1.5

                # ── Size calculation — Sovereign 20% capital pool + ORACLE fusion ──
                # Sovereign gets a dedicated 20% slice of the perp balance per trade.
                # ORACLE fusion_mult (1.10–1.25) amplifies when cluster signal aligns.
                # Floor at base_trade_usd ($200): hedge_notional from the portfolio model
                # can be sub-$50 (e.g. AAPL weight 15% × $201 stake = $30) which SoDEX
                # rejects. Use base_trade_usd as the minimum executable notional.
                _sov_step = _CLOSE_STEP_SIZES.get(_sov_signal.symbol, 0.01)
                _sovereign_pool = _cached_balance[0] * config.sovereign_capital_pct
                _sov_acfg  = config.ASSET_CONFIG.get(_sov_signal.symbol, {})
                _sov_lev   = min(
                    _sov_acfg.get("preferred_leverage", config.default_leverage),
                    _sov_acfg.get("max_leverage", 25),
                )
                _sov_notional = min(
                    max(_sov_signal.hedge_notional, config.base_trade_usd),
                    max(_sovereign_pool, config.base_trade_usd),
                )
                _oracle_fusion = _oracle_engine.get_fusion_mult(_sov_signal.side)
                _sov_raw_size = (_sov_notional * _oracle_fusion) / _sov_entry
                _sov_size = round(
                    round(_sov_raw_size / _sov_step) * _sov_step,
                    8
                )
                if _sov_size <= 0:
                    logger.debug("sovereign_signal_skipped",
                                 symbol=_sov_signal.symbol, reason="size_zero",
                                 raw_size=_sov_raw_size)
                    continue

                # ── Order cooldown and open position guards ──────────────────────
                if _order_cooldown.get(_sov_signal.symbol, 0) > time.time():
                    logger.debug("sovereign_signal_skipped",
                                 symbol=_sov_signal.symbol, reason="order_cooldown")
                    continue
                if _sov_signal.symbol in {p.symbol for p in position_manager.get_all()}:
                    logger.debug("sovereign_signal_skipped",
                                 symbol=_sov_signal.symbol, reason="already_open")
                    continue

                # ── Symbol ID check ────────────────────────────────────────────
                _sov_sym_id = SYMBOL_IDS.get(_sov_signal.symbol, 0)
                if _sov_sym_id == 0:
                    logger.warning("sovereign_signal_skipped",
                                   symbol=_sov_signal.symbol, reason="no_symbol_id")
                    continue

                # ── Build TradeCandidate ───────────────────────────────────────
                from execution.schemas import TradeCandidate
                import time as _time_mod
                _sov_risk  = abs(_sov_entry - _sov_stop)
                _sov_rr    = round(abs(_sov_tp1 - _sov_entry) / _sov_risk, 2) if _sov_risk > 0 else 2.0
                _sov_candidate = TradeCandidate(
                    symbol         = _sov_signal.symbol,
                    side           = _sov_signal.side,
                    entry_price    = _sov_entry,
                    stop_price     = round(_sov_stop, 8),
                    tp1_price      = round(_sov_tp1, 8),
                    tp2_price      = round(_sov_tp1 * 1.005 if _sov_signal.side == "long" else _sov_tp1 * 0.995, 8),
                    tp3_price      = round(_sov_tp1 * 1.010 if _sov_signal.side == "long" else _sov_tp1 * 0.990, 8),
                    size           = _sov_size,
                    leverage       = _sov_lev,
                    initial_margin = round(_sov_size * _sov_entry / _sov_lev, 8),
                    atr            = _sov_atr,
                    coherence_score= _sov_signal.confidence,
                    signal_reason  = "SOVEREIGN_DIVERGENCE",
                    order_type     = "limit",
                    rr_ratio       = _sov_rr,
                    size_multiplier= 1.0,
                    invalidation   = f"stop_loss:{round(_sov_stop, 4)}",
                    timestamp_ms   = int(_time_mod.time() * 1000),
                )

                # ── Place bracket ─────────────────────────────────────────────
                _sov_bracket = BracketOrder(
                    candidate  = _sov_candidate,
                    account_id = str(NUMERIC_ACCOUNT_ID),
                    symbol_id  = _sov_sym_id,
                )
                # Stamp cooldown before await to prevent duplicate signals
                _order_cooldown[_sov_signal.symbol] = time.time() + 300.0

                try:
                    _sov_result = await client.place_bracket(_sov_bracket)
                    if _sov_result.success:
                        logger.info(
                            "sovereign_signal_executed",
                            symbol        = _sov_signal.symbol,
                            side          = _sov_signal.side,
                            z_score       = round(_sov_signal.z_score, 2),
                            confidence    = round(_sov_signal.confidence, 3),
                            regime_type   = _sov_signal.regime_type,
                            entry         = round(_sov_entry, 4),
                            stop          = round(_sov_stop, 4),
                            tp1           = round(_sov_tp1, 4),
                            size          = _sov_size,
                            notional_usd  = round(_sov_notional, 2),
                            sovereign_pool= round(_sovereign_pool, 2),
                            oracle_fusion = round(_oracle_fusion, 3),
                            rationale     = _sov_signal.entry_rationale,
                        )
                    else:
                        # Reset cooldown on failure so the next tick can retry
                        _order_cooldown[_sov_signal.symbol] = 0.0
                        logger.warning(
                            "sovereign_signal_failed",
                            symbol  = _sov_signal.symbol,
                            error   = getattr(_sov_result, "error", "unknown"),
                        )
                except Exception as _sov_place_err:
                    _order_cooldown[_sov_signal.symbol] = 0.0
                    logger.warning("sovereign_signal_place_error",
                                   symbol=_sov_signal.symbol, error=str(_sov_place_err))

            except Exception as _ssl_err:
                logger.error("sovereign_signal_loop_error", error=str(_ssl_err))

    async def health_server():
        """
        Lightweight health endpoint for Railway liveness checks.
        Also serves /aria/state — read-only UI state snapshot, polled every 2s by UI.

        Port conflict behaviour:
          1. Try PORT env var (default 8080).
          2. If busy, try up to 10 sequential fallback ports.
          3. If all busy (e.g. running locally with many instances), log and
             return — health server is non-critical; ARIA continues trading.

        UI state endpoint always runs on port 8765 (fixed, independent of PORT env).
        """
        import json as _json

        async def _health(request):
            phase = system_state.get_global_phase().value if system_state else "unknown"
            return _aiohttp_web.Response(
                text=f'{{"status":"ok","phase":"{phase}","mode":"{config.mode}"}}',
                content_type="application/json"
            )

        async def _aria_state(request):
            """Read-only UI state snapshot — no trading path involved."""
            try:
                data = _ui_state.snapshot()
                return _aiohttp_web.Response(
                    text=_json.dumps(data, default=str),
                    content_type="application/json",
                    headers={"Access-Control-Allow-Origin": "*"},
                )
            except Exception as _se:
                return _aiohttp_web.Response(
                    text='{"error":"state_unavailable"}',
                    content_type="application/json",
                    status=500,
                )

        app = _aiohttp_web.Application()
        app.router.add_get("/health", _health)
        app.router.add_get("/", _health)
        app.router.add_get("/aria/state", _aria_state)
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

        # UI state server — fixed port 8765, independent of PORT env var.
        # Same aiohttp app serves /aria/state; this is just a second binding.
        _ui_port = 8765
        try:
            _ui_site = _aiohttp_web.TCPSite(runner, "localhost", _ui_port)
            await _ui_site.start()
            logger.info("ui_state_server_started", port=_ui_port,
                        endpoint=f"http://localhost:{_ui_port}/aria/state")
        except OSError:
            logger.warning("ui_state_server_port_busy", port=_ui_port,
                           action="UI polling will fail — trading unaffected")

        await asyncio.Event().wait()  # run forever

    # 11. Subscribe and Start
    event_bus.subscribe(EventType.SIGNAL_READY, on_signal_ready)

    # Phase 11: wire MicroAgent and StructureAgent to event bus
    async def _on_ob_update(event):
        try:
            await _micro_agent.on_orderbook_update(event)
            sym = getattr(event, "symbol", None) or (event.get("symbol") if isinstance(event, dict) else None)
            if sym:
                _out = _micro_agent._last_outputs.get(sym)
                if _out:
                    display.push_agent_state("micro", _out)
        except Exception:
            pass

    async def _on_mark_update(event):
        try:
            await _micro_agent.on_mark_update(event)
        except Exception:
            pass

    async def _on_candle_close(event):
        try:
            if hasattr(_structure_agent, "on_candle_close"):
                await _structure_agent.on_candle_close(event)
                sym = getattr(event, "symbol", None) or (event.get("symbol") if isinstance(event, dict) else None)
                if sym:
                    _out = _structure_agent._last_outputs.get(sym)
                    if _out:
                        display.push_agent_state("structure", _out)
        except Exception:
            pass

    # CascadeOrchestrator event handlers — Special Operations fast path
    async def _on_cascade_momentum(event):
        try:
            _dir = event.data.get("direction")
            _notional = event.data.get("notional_60s", 0.0)
            if _dir:
                await _execute_cascade_momentum(_dir, _notional)
        except Exception:
            pass

    async def _on_cascade_aftermath(event):
        try:
            nonlocal _aftermath_primed, _aftermath_direction, _aftermath_expires_ms
            _dir = event.data.get("direction")
            if _dir and not _aftermath_primed:
                _aftermath_primed = True
                _aftermath_direction = _dir
                _aftermath_expires_ms = int(time.time() * 1000) + 300_000
                logger.info("cascade_aftermath_primed_orchestrator",
                            direction=_dir, source="cascade_orchestrator")
        except Exception:
            pass

    if hasattr(EventType, "ORDERBOOK_UPDATED"):
        event_bus.subscribe(EventType.ORDERBOOK_UPDATED, _on_ob_update)
    if hasattr(EventType, "MARK_PRICE_UPDATED"):
        event_bus.subscribe(EventType.MARK_PRICE_UPDATED, _on_mark_update)
    if hasattr(EventType, "CANDLE_CLOSED"):
        event_bus.subscribe(EventType.CANDLE_CLOSED, _on_candle_close)
    # CASCADE_MOMENTUM_READY is consumed via direct callback from orchestrator
    # (bypasses 50ms event-bus coalescing). Event bus subscription removed to
    # prevent duplicate execution attempts.
    if hasattr(EventType, "CASCADE_AFTERMATH_READY"):
        event_bus.subscribe(EventType.CASCADE_AFTERMATH_READY, _on_cascade_aftermath)

    # Seed fee display with initial data from volume history
    display.update_fee_data(sdex_fee_engine.tier_summary())
    
    logger.info("Starting ARIA execution gather")
    
    # ARC v1.3 Patch Part A: Historical fetch on startup
    if hasattr(ws_manager, "fetch_historical"):
        logger.info("fetching_historical_data", source=type(ws_manager).__name__)
        await ws_manager.fetch_historical()
        logger.info("historical_complete")

    async def _supervise(coro_fn, name: str, *, critical: bool = False) -> None:
        """
        Supervised coroutine runner with exponential-backoff restart.

        critical=True  — propagates the exception upward to kill the whole gather.
                         Use for loops whose crash means the bot cannot function
                         (display, event_bus, interpreter, ws_manager).
        critical=False — restarts with 2s → 4s → 8s → … → 60s backoff, up to 20
                         restarts before giving up and propagating.
        """
        _attempts = 0
        _backoff = 2.0
        while True:
            try:
                await coro_fn()
                # A clean return from an infinite loop means intentional shutdown.
                logger.info("supervised_loop_clean_exit", name=name)
                return
            except asyncio.CancelledError:
                raise  # propagate cancellation — clean shutdown path
            except Exception as _sup_err:
                _attempts += 1
                logger.error(
                    "supervised_loop_crashed",
                    name=name,
                    attempt=_attempts,
                    error=repr(_sup_err),
                )
                if critical:
                    raise
                if _attempts > 20:
                    logger.critical("supervised_loop_giving_up", name=name, attempts=_attempts)
                    raise
                await asyncio.sleep(min(_backoff, 60.0))
                _backoff = min(_backoff * 2, 60.0)

    try:
        # Each loop is wrapped in _supervise so a single crash restarts that loop
        # with exponential backoff rather than killing the whole gather.
        # critical=True loops are mission-critical — their crash IS a fatal event.
        _gather_coros = [
            _supervise(display.run,              "display",              critical=True),
            _supervise(event_bus.start,          "event_bus",            critical=True),
            _supervise(interpreter.start,        "interpreter",          critical=True),
            _supervise(ws_manager.start,         "ws_manager",           critical=True),
            _supervise(execution_cleanup_loop,   "execution_cleanup"),
            _supervise(funding_loop,             "funding"),
            _supervise(true_arb_loop,            "true_arb"),
            _supervise(fee_update_loop,          "fee_update"),
            _supervise(vault_loop,               "vault"),
            _supervise(calendar_loop,            "calendar"),
            _supervise(balance_monitor_loop,     "balance_monitor"),
            _supervise(prediction_drain_loop,    "prediction_drain"),
            _supervise(recovery_signal_loop,     "recovery_signal"),
            _supervise(cascade_aftermath_loop,   "cascade_aftermath"),
            _supervise(nightly_calibration_loop, "nightly_calibration"),
            _supervise(journal_cleanup_loop,     "journal_cleanup"),
            _supervise(health_server,            "health_server"),
            _supervise(sovereign_monitor_loop,   "sovereign_monitor"),
            _supervise(yield_accrual_loop,       "yield_accrual"),
            _supervise(sovereign_signal_loop,    "sovereign_signal"),
            _supervise(display_refresh_loop,     "display_refresh"),
            _supervise(market_context_loop,      "market_context"),
            _supervise(signal_agent_loop,        "signal_agents_p11"),
            _supervise(_ssi_spot_feed.start,      "ssi_spot_feed"),
            _supervise(_slp_tracker.monitor_loop,        "slp_monitor"),
            _supervise(_slp_tracker.manage_loop,         "slp_hedge_manager"),
            _supervise(_sovereign_agent.sovereign_loop,  "sovereign_agent"),
            _supervise(oracle_loop,                      "oracle"),
        ]
        # ValueChain monitor only in live mode
        if vc_monitor is not None:
            _gather_coros.append(_supervise(vc_monitor.run, "valuechain_monitor"))
        # Bybit feed always runs for liquidations + funding, even when SoDEX is primary data source
        if config.data_source != "bybit":
            _gather_coros.append(_supervise(bybit_feed.start, "bybit_feed"))

        await asyncio.gather(*_gather_coros, return_exceptions=False)
    except Exception as e:
        logger.error("system_gather_critical_failure", error=str(e))
        raise
    finally:
        # 9. Graceful shutdown
        if 'cascade_orchestrator' in locals():
            cascade_orchestrator.stop()
        await event_bus.stop()
        await journal.stop_writer()
        await alert_system.stop()
        await market_engine.stop()
        await ws_manager.stop()
        logger.info("ARIA shutdown complete")


# Module-level config singleton for build_candidate — avoids re-parsing .env on every signal
_build_candidate_config = None

def build_candidate(state, balance, margin_engine, config=None, param_store=None, cascade_phase: str = ""):
    """Takes MarketState + balance + margin_engine + optional config/param_store. Returns TradeCandidate or None.

    cascade_phase: "momentum" | "aftermath" | "" — cascade-native stop logic.
      momentum  → tight stop: max(0.3% of entry, 0.5×ATR) for fast mechanical moves.
      aftermath → medium stop: max(0.5% of entry, 0.75×ATR) for recovery plays.
      ""        → standard ATR-based stop with per-asset floors/caps.
    """
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

    # ATR-based stop with minimum distance floor.
    # 1-minute ATR on small-price assets (AVAX $9, LINK $9) is typically $0.003–0.008 per candle.
    # stop_atr_mult=0.75 would place the stop <5 ticks from entry, guaranteed to trigger on noise.
    # Fix: use a minimum of 0.5% of price so the stop can survive the holding period.
    #   AVAX $8.94: max(0.005×1.5, 0.0447) = 0.0447 → 0.5% → 5% at 10x (survivable)
    #   BTC  $70k:  max(  45×1.5, 353   ) = 353   → 0.5% → 5% at 10x (consistent)
    atr = getattr(state, 'atr', 0.0)
    if atr <= 0:
        return None

    symbol_for_stop = getattr(state, 'symbol', '')

    # ── Cascade-native stop logic ─────────────────────────────────────────────
    # Mechanical forced moves need tighter stops than discretionary signals.
    # Momentum (trade WITH liquidations): 0.3% floor or 0.5×ATR — whichever is tighter.
    # Aftermath (fade the cascade):       0.5% floor or 0.75×ATR.
    # Rationale: cascades are 30–120s events; normal 2.5×–4.0× ATR stops are 4× too wide.
    if cascade_phase == "momentum":
        stop_buffer = max(entry * 0.003, atr * 0.5)
    elif cascade_phase in ("aftermath", "primed"):
        stop_buffer = max(entry * 0.005, atr * 0.75)
    else:
        # Per-asset ATR stop multiplier — calibrated for intraday noise survival.
        # Wider stops avoid noise-triggered losses; tighter stops reduce R:R.
        # These defaults are the minimum acceptable floor for each asset class.
        #   BTC/ETH  — deep liquidity, 1m ATR ~$50–200; 2.5× gives $125–500 buffer
        #   SOL/BNB  — mid-cap vol; 3.0× needed to survive 30-min hold at current ranges
        #   XAUT     — gold proxy, slow vol; 3.5× is conservative but necessary for leverage
        #   LINK     — thin intraday liquidity, sharp wicks; 2.5× previously 1.5× (too tight)
        #   AVAX     — high relative vol; 3.5× matches historical 2σ intraday range
        #   Small-cap altcoins (ARB/OP/NEAR) — illiquid spikes; 4.0× mandatory
        if param_store is not None:
            stop_atr_mult = param_store.get_stop_mult(symbol_for_stop)
        else:
            # Fallback per-asset defaults when learning system not available
            _ASSET_STOP_MULTS = {
                'BTC-USD': 2.5, 'ETH-USD': 2.5, 'SOL-USD': 3.0, 'XAUT-USD': 3.5,
                'BNB-USD': 3.0, 'LINK-USD': 2.5, 'AVAX-USD': 3.5,
                'ARB-USD': 4.0, 'OP-USD': 4.0,  'NEAR-USD': 4.0,
                'SUI-USD': 4.0, '1000PEPE-USD': 4.0,
                # Equities: 3.5× for high-vol (TSLA/NVDA/META), 3.0× for mega-cap
                'NVDA-USD': 3.5, 'TSLA-USD': 3.5, 'META-USD': 3.5, 'AMZN-USD': 3.0,
                'MSFT-USD': 3.0, 'AAPL-USD': 3.0, 'GOOGL-USD': 3.0,
                'TSM-USD':  3.0, 'ORCL-USD': 3.0,
                # Commodities
                'CL-USD': 3.0, 'COPPER-USD': 3.0,
            }
            stop_atr_mult = _ASSET_STOP_MULTS.get(symbol_for_stop, getattr(cfg, 'stop_atr_mult', 2.5))
        atr_based_stop_dist = atr * stop_atr_mult
        # Per-asset-class stop floors and caps.
        # Equities/commodities trade at $200–$700/share so need wider absolute floors.
        # 1.5% floor for equities: NVDA $207 → $3.11; TSLA $374 → $5.61.
        # 2.0% cap bump: US stocks regularly gap 2-3% intraday — 4% was too tight.
        _sym_category = cfg.ASSET_CONFIG.get(symbol_for_stop, {}).get('category', 'crypto')
        if _sym_category in ('equity', 'commodity'):
            min_stop_dist = entry * 0.015   # 1.5% floor — survives normal intraday noise
            max_stop_dist = entry * 0.060   # 6.0% cap  — covers gap-risk on $200-700 stocks
        else:
            min_stop_dist = entry * 0.012   # 1.2% crypto floor
            max_stop_dist = entry * 0.040   # 4.0% crypto cap
        stop_buffer = min(max(atr_based_stop_dist, min_stop_dist), max_stop_dist)

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
        import structlog as _sl
        _sl.get_logger(__name__).info(
            "build_candidate_rr_reject",
            symbol=state.symbol,
            rr=round(rr, 2),
            risk_distance=round(risk_distance, 4),
            entry=round(entry, 4),
            tp1=round(tp1, 4),
            tp2=round(tp2, 4),
            reason="tp2_still_below_2r",
        )
        return None

    atr_ratio = getattr(state, 'atr_vs_baseline', 1.0)

    # ── Fixed floor notional sizing (v1.7) ───────────────────────────────────
    # Conviction-scaled fixed notional. Balance safety cap prevents oversized
    # positions on depleted accounts. All values in USD notional (not margin).
    #
    #   Conviction multipliers from coherence score:
    #     score < 3.0 → 1.0×  ($200)   base
    #     score 3–4.5 → 1.5×  ($300)   confirmed signal
    #     score ≥ 4.5 → 2.0×  ($400)   strong alignment
    #   Ceiling: max_notional_usd = $500
    #   Balance safety cap: min(notional, balance × 0.50)
    #   Post-cap floor: if < min_trade_notional_usd ($50) → skip trade
    base_usd     = cfg.base_trade_usd      # 200.0 minimum per trade
    max_usd      = cfg.max_notional_usd    # 500.0 conviction ceiling
    min_notional = cfg.min_trade_notional_usd  # 80.0 — strategy floor (SoDEX exchange floor is $50)
    _sym_acfg = cfg.ASSET_CONFIG.get(state.symbol, {})
    _pref_lev = _sym_acfg.get('preferred_leverage', cfg.default_leverage)
    _max_lev  = _sym_acfg.get('max_leverage', 25)
    lev = min(_pref_lev, _max_lev)

    if base_usd > 0 and entry > 0:
        coherence = getattr(state, 'coherence_score', 0.0)
        # Updated conviction thresholds (v1.7)
        if coherence >= 4.5:
            conv_mult = 2.0   # $400
        elif coherence >= 3.0:
            conv_mult = 1.5   # $300
        else:
            conv_mult = 1.0   # $200
        target_notional = base_usd * conv_mult
        target_notional = max(target_notional, base_usd)   # floor = $200
        target_notional = min(target_notional, max_usd)    # ceiling = $500

        # Balance safety cap: never deploy more than 60% of account in one trade.
        # Raised from 50% → 60% to allow $80-minimum trades on $138 balance (Tria).
        _cap_pct = 0.60
        balance_cap = balance * _cap_pct
        target_notional = min(target_notional, balance_cap)

        # Effective floor = min(SoDEX dust minimum, base_usd).
        # SoDEX requires at least $50 notional. When base_usd > $50 (production: $200),
        # the $50 floor applies. When base_usd < $50 (test config / tiny accounts), we
        # scale down to base_usd so small-config tests don't false-reject valid candidates.
        _effective_min = min(min_notional, base_usd) if base_usd > 0 else min_notional
        if target_notional < _effective_min:
            import structlog as _sl
            _sl.get_logger(__name__).warning(
                "build_candidate_balance_too_low",
                symbol=state.symbol, balance=round(balance, 2),
                balance_cap=round(balance_cap, 2),
                min_required=min_notional,
            )
            return None

        size = target_notional / entry
        margin = target_notional / max(lev, 1)
        # Diagnostic log — always emitted so we can trace sizing end-to-end.
        import structlog as _sl
        _sl.get_logger(__name__).debug(
            "build_candidate_sizing",
            symbol=state.symbol,
            base_usd=base_usd, max_usd=max_usd,
            coherence=round(coherence, 3), conv_mult=conv_mult,
            balance=round(balance, 2), balance_cap=round(balance_cap, 2),
            target_notional=round(target_notional, 2),
            entry=entry, size=round(size, 6), lev=lev,
        )
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
            # Keep core_assets in sync — don't subscribe dead symbols
            config.core_assets = [a for a in config.core_assets if a in config.assets]
            logger.info("active_assets_updated",
                        assets=config.assets, core=config.core_assets)

    except Exception as e:
        logger.error("symbol_fetch_error", error=str(e))
        SYMBOL_IDS = _FALLBACK.copy()

if __name__ == "__main__":
    async def _run():
        """
        Wraps main() so that SIGINT/SIGTERM cancel the task cleanly, allowing
        the finally block inside main() to drain the journal and stop subsystems
        before the process exits.

        Root cause of the old hang: the synchronous shutdown_handler called
        loop.stop(), which killed the event loop before the finally-block awaits
        could complete → RuntimeError: Event loop stopped before Future completed.
        Fix: register handlers via loop.add_signal_handler() (async-safe), set an
        asyncio.Event, cancel the main task, and wait for its CancelledError so the
        finally block executes on a live loop.
        """
        loop = asyncio.get_running_loop()
        _stop = asyncio.Event()

        def _request_stop():
            import sys as _sys_stop
            _sys_stop.stderr.write("\nShutdown signal received — draining and exiting...\n")
            if not _stop.is_set():
                _stop.set()

        loop.add_signal_handler(sys_signal.SIGINT,  _request_stop)
        loop.add_signal_handler(sys_signal.SIGTERM, _request_stop)

        _main_task = asyncio.ensure_future(main())
        _sig_task  = asyncio.ensure_future(_stop.wait())

        done, _ = await asyncio.wait(
            [_main_task, _sig_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        if _sig_task in done:
            # Signal received — cancel main so its finally block runs on the live loop
            _main_task.cancel()
            try:
                await _main_task
            except (asyncio.CancelledError, Exception):
                pass

        loop.remove_signal_handler(sys_signal.SIGINT)
        loop.remove_signal_handler(sys_signal.SIGTERM)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
