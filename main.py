import asyncio
import os
import structlog
import signal as sys_signal
import time
from dotenv import load_dotenv
import logging

from core.config import Settings
from core.market_engine import MarketEngine
from data.websocket_manager import WebSocketManager
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
from data.ostium_feed import OstiumFeed

# Funding layer imports
from funding.history import FundingHistory
from funding.radar import FundingRadar
from funding.arb_strategy import FundingArbStrategy

# Intelligence Expansion
from intelligence.relative_strength import RelativeStrengthEngine

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
    ostium_feed = OstiumFeed()
    regime_engine = RelativeStrengthEngine(config)
    
    margin_engine = MarginEngine()
    position_manager = PositionManager()
    order_manager = OrderManager()
    risk_engine = RiskEngine(config, margin_engine, position_manager, journal, perf, market_hours=market_hours)

    # 6. Initialize monitoring & Vault
    alert_system = AlertSystem(config)
    vault_manager = VaultManager(config.log_dir)
    vault_manager.load()
    fee_engine = FeeEngine()
    perf_cert = PerformanceCert(config.log_dir)

    # 5. Create execution client
    if config.mode == "paper":
        client = PaperClient(config)
    elif config.mode == "testnet":
        signer = SoDEXSigner(
            private_key=config.private_key,
            chain_id=config.chain_id_testnet,
            app_chain="futures"
        )
        nonce_mgr = NonceManager(config.private_key)
        client = SoDEXClient(config, signer, nonce_mgr)
    elif config.mode == "live":
        # Same as testnet but mainnet chain_id
        # Only reachable if LIVE_MODE_CONFIRMED=true
        signer = SoDEXSigner(
            private_key=config.private_key,
            chain_id=config.chain_id_mainnet,
            app_chain="futures"
        )
        nonce_mgr = NonceManager(config.private_key)
        client = SoDEXClient(config, signer, nonce_mgr)
    else:
        raise ValueError(f"Unknown mode: {config.mode}")

    # 5.5 Fetch dynamic symbol IDs
    await fetch_symbol_ids(client, config, logger)

    # 7. Create MarketEngine
    market_engine = MarketEngine(
        config=config,
        orderbook_stores=orderbook_stores,
        mark_price_stores=mark_price_stores,
        candle_buffers=candle_buffers,
        trade_flow_stores=trade_flow_stores,
        stop_clusters=stop_clusters,
        market_hours=market_hours,
        ostium_feed=ostium_feed,
        risk_engine=risk_engine
    )

    # 8. WebSocketManager
    ws_manager = WebSocketManager(
        config=config,
        orderbook_stores=orderbook_stores,
        mark_price_stores=mark_price_stores,
        candle_buffers=candle_buffers,
        trade_flow_stores=trade_flow_stores
    )

    # 9. TerminalDisplay
    display = TerminalDisplay(
        config=config,
        orderbook_stores=orderbook_stores,
        mark_price_stores=mark_price_stores,
        candle_buffers=candle_buffers,
        trade_flow_stores=trade_flow_stores,
        health_check=ws_manager.health_check,
        market_engine=market_engine,
        journal=journal,
        perf=perf
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

    async def execution_loop():
        """Main execution loop for placing orders"""
        while True:
            try:
                balance = await client.get_account_balance(
                    config.account_id or "paper")

                # Phase 6: Sync equity curve
                display.update_equity(balance)

                # Phase 6: Check balance floor
                if balance < getattr(config, 'balance_floor', 500):
                    alert_system.notify_balance_floor_hit(balance)
                    logger.error("BALANCE_FLOOR_HIT", balance=balance)
                    await asyncio.sleep(5)  # Let alert send
                    break  # Stop ARIA

                # Phase 6: Poll client events (Paper mode)
                if config.mode == "paper" and hasattr(client, "get_events"):
                    for ev in client.get_events():
                        if ev["type"] == "tp1_hit":
                            alert_system.notify_tp1_hit(ev["symbol"])
                        elif ev["type"] == "trade_closed":
                            stats = perf.compute(journal)
                            alert_system.notify_trade_closed(
                                symbol=ev["symbol"],
                                outcome=ev["outcome"],
                                pnl=ev["pnl"],
                                r_multiple=ev["r_multiple"],
                                total_pnl=stats.total_pnl_usd
                            )
                            if ev["outcome"] == "stop_out":
                                alert_system.notify_stopped_out(ev["symbol"], ev["pnl"])

                for symbol in config.assets:
                    state = market_engine.get_market_state(symbol)
                    if not state:
                        continue

                    # Only act on strong signals (v1.2 uses weighted_score >= 4.0)
                    if getattr(state, 'weighted_score', state.coherence_score) < 4.0:
                        continue
                    if state.trade_direction == "none":
                        continue

                    # Build candidate
                    candidate = build_candidate(
                        state, balance, margin_engine)
                    if not candidate:
                        continue

                    # Risk validation
                    approved, reason = risk_engine.validate(
                        candidate, balance)

                    # Log every decision
                    entry_id = journal.log_decision(
                        state=state,
                        candidate=candidate,
                        approved=approved,
                        reason=reason if not approved else None
                    )

                    logger.info("execution_decision",
                        symbol=symbol,
                        approved=approved,
                        reason=reason,
                        coherence=state.coherence_score,
                        direction=state.trade_direction
                    )

                    if not approved:
                        continue

                    # Execute bracket
                    bracket = BracketOrder(
                        candidate=candidate,
                        account_id=config.account_id or "paper",
                        symbol_id=SYMBOL_IDS[symbol]
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
                        order_manager.track(...)  # Placeholder in Phase 1
                        
                        # Phase 6: Update dashboard equity
                        display.update_equity(balance)

                        # Phase 6: Send alert
                        alert_system.notify_trade_placed(
                            symbol=symbol,
                            side=candidate.side,
                            price=candidate.entry_price,
                            stop=candidate.stop_price,
                            size=candidate.size,
                            rr=candidate.rr_ratio
                        )

                        # Update journal with order IDs and mark as open
                        journal.update_outcome(
                            entry_id=entry_id,
                            outcome="open",
                            pnl_usd=None,
                            closed_at_ms=None
                        )
                        
                        logger.info("bracket_placed",
                            symbol=symbol,
                            entry=candidate.entry_price,
                            stop=candidate.stop_price,
                            liq=candidate.liq_price
                        )
                    else:
                        logger.error("bracket_failed",
                            error=result.error)

                # Update paper fills
                if config.mode == "paper":
                    await client.update_fills(
                        {s: mark_price_stores[s].get()["mark_price"]
                         for s in config.assets}
                    )

                await asyncio.sleep(
                    config.loop_interval_ms / 1000)

            except Exception as e:
                logger.error("execution_loop_error",
                    error=str(e))
                continue

    async def funding_loop():
        """Loop for funding radar updates and arb execution"""
        while True:
            try:
                # Update external feeds
                if ostium_feed:
                    await ostium_feed.update()
                    
                snapshots = await funding_radar.update_all()
                
                # Update terminal display
                display.update_funding(snapshots)
                display.update_arbs(arb_strategy.get_open_arbs())
                
                # Evaluate arb opportunity
                candidate = await arb_strategy.evaluate()
                if candidate:
                    await arb_strategy.open_arb(candidate)
                
                # Monitor existing arbs
                await arb_strategy.monitor_arbs(snapshots)
                
                # Log funding state
                for symbol, snap in snapshots.items():
                    logger.info("funding_update",
                        symbol=symbol,
                        rate=snap.rate,
                        carry_score=snap.carry_score,
                        arb_signal=snap.arb_signal
                    )
                
            except Exception as e:
                logger.error("funding_loop_error",
                    error=str(e))
            
            await asyncio.sleep(60)  # check every min

    async def vault_loop():
        """Phase 6: Hourly vault and performance reporting"""
        while True:
            try:
                # 1. Update Vault NAV
                balance = await client.get_account_balance(config.account_id or "paper")
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

    # 11. Start all components
    try:
        await asyncio.gather(
            market_engine.start(),
            ws_manager.start(),
            display.start(),
            execution_loop(),
            funding_loop(),
            vault_loop()
        )
    except Exception as e:
        logger.error(f"Error in main loop: {e}")
    finally:
        # 9. Graceful shutdown
        await alert_system.stop()
        await market_engine.stop()
        await ws_manager.stop()
        await display.stop()
        logger.info("ARIA shutdown complete")


def build_candidate(state, balance, margin_engine):
    """Takes MarketState + balance + margin_engine Returns TradeCandidate or None"""
    from execution.schemas import TradeCandidate
    
    # Entry = best bid if long else best ask
    entry = state.sweep_index if state.sweep == "buy_side" else state.sweep_index
    
    if not entry:
        return None
    
    # Stop = sweep level ± ATR buffer
    stop_buffer = state.atr * 0.5  # 0.5 ATR buffer
    if state.trade_direction == "long":
        stop = entry - stop_buffer
    else:
        stop = entry + stop_buffer
    
    # TP levels: 1R, 2R, 3R
    risk_distance = abs(entry - stop)
    tp1 = entry + (risk_distance * 1) if state.trade_direction == "long" else entry - (risk_distance * 1)
    tp2 = entry + (risk_distance * 2) if state.trade_direction == "long" else entry - (risk_distance * 2)
    tp3 = entry + (risk_distance * 3) if state.trade_direction == "long" else entry - (risk_distance * 3)
    
    try:
        size, margin, lev = margin_engine.compute_size(
            balance, 0.02, entry, stop, 10, state.symbol)
        
        rr = abs(tp1 - entry) / abs(entry - stop)
        if rr < 2.0:
            return None
        
        return TradeCandidate(
            symbol=state.symbol,
            side=state.trade_direction,
            entry_price=entry,
            stop_price=stop,
            tp1_price=tp1,
            tp2_price=tp2,
            tp3_price=tp3,
            size=size,
            initial_margin=margin,
            leverage=lev,
            rr_ratio=rr,
            coherence_score=state.coherence_score,
            size_multiplier=state.size_multiplier,
            signal_reason=state.macro_bias,
            invalidation=state.invalidation_reason,
            timestamp_ms=state.timestamp_ms
        )
    except Exception:
        return None


# SYMBOL IDs mapping (Initially empty, populated by fetch_symbol_ids)
SYMBOL_IDS = {}

async def fetch_symbol_ids(client, config, logger):
    """
    Fetches symbol IDs from SoDEX API and updates config.assets
    if any symbols are missing from the exchange.
    """
    global SYMBOL_IDS
    try:
        # User requested: GET https://testnet-gw.sodex.dev/api/v1/perps/symbols
        # We use the client to fetch symbols
        response = await client.client.get(f"{client.base_url}/symbols")
        if response.status_code != 200:
            logger.warning("failed_to_fetch_symbols", status=response.status_code)
            # Fallback to defaults if API is reachable but returns error
            SYMBOL_IDS = {"BTC": 1, "ETH": 2, "SOL": 3, "XAUT": 4, "BNB": 5, "LINK": 6, "AVAX": 7}
            return

        symbols_data = response.json()
        
        # Print response for debugging as requested
        print("\n--- SoDEX SYMBOLS API RESPONSE ---")
        import json
        print(json.dumps(symbols_data, indent=2))
        print("----------------------------------\n")

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
            # Remove missing assets from config
            config.assets = [a for a in config.assets if a not in missing]
            logger.info("active_assets_updated", assets=config.assets)

    except Exception as e:
        logger.error("symbol_fetch_error", error=str(e))
        # Critical fallback to avoid crash
        SYMBOL_IDS = {"BTC": 1, "ETH": 2, "SOL": 3, "XAUT": 4, "BNB": 5, "LINK": 6, "AVAX": 7}

def shutdown_handler(sig, frame):
    """Graceful shutdown handler"""
    print("\nShutting down ARIA...")
    
    import sys
    # Generate session summary
    if journal and perf and session_summary:
        stats = perf.compute(journal)
        summary = session_summary.generate(
            journal, stats, session_start_ms)
        
        # v1.2 Add calibration
        session_summary.add_calibration(summary, journal)
        
        session_summary.save(summary)
        session_summary.print_to_terminal(summary)
    
    sys.exit(0)


if __name__ == "__main__":
    # Register shutdown handlers
    sys_signal.signal(sys_signal.SIGINT, shutdown_handler)
    sys_signal.signal(sys_signal.SIGTERM, shutdown_handler)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
