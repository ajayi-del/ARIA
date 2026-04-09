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

    # 4. Create risk and execution layer
    margin_engine = MarginEngine()
    position_manager = PositionManager()
    order_manager = OrderManager()
    risk_engine = RiskEngine(config, margin_engine, position_manager)

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

    # 6. Create MarketEngine
    market_engine = MarketEngine(config)

    # 7. Initialize memory layer
    journal = TradeJournal()
    journal.load()
    perf = PerformanceTracker()
    session_summary = SessionSummary()
    session_start_ms = int(time.time() * 1000)

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

    async def execution_loop():
        """Main execution loop for placing orders"""
        while True:
            try:
                balance = await client.get_account_balance(
                    config.account_id or "paper")

                for symbol in config.assets:
                    state = market_engine.get_market_state(symbol)
                    if not state:
                        continue

                    # Only act on strong signals
                    if state.coherence_score < 4:
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

    # 8. Start all components
    try:
        await asyncio.gather(
            market_engine.start(),
            ws_manager.start(),
            display.start(),
            execution_loop()
        )
    except Exception as e:
        logger.error(f"Error in main loop: {e}")
    finally:
        # 9. Graceful shutdown
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


# SYMBOL IDs mapping (SoDEX perps)
SYMBOL_IDS = {
    "BTC": 1,
    "ETH": 2,
    "SOL": 3,
    "XAUT": 4
}

def shutdown_handler(sig, frame):
    """Graceful shutdown handler"""
    print("\nShutting down ARIA...")
    
    # Generate session summary
    stats = perf.compute(journal)
    summary = session_summary.generate(
        journal, stats, session_start_ms)
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
