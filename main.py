import asyncio
import os
import structlog
from dotenv import load_dotenv
import logging

from core.config import Settings
from data.websocket_manager import WebSocketManager
from data.orderbook_store import OrderbookStore
from data.mark_price_store import MarkPriceStore
from data.candle_buffer import CandleBuffer
from data.trade_flow_store import TradeFlowStore
from display.terminal import TerminalDisplay

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

    # 4. WebSocketManager
    ws_manager = WebSocketManager(
        config=config,
        orderbook_stores=orderbook_stores,
        mark_price_stores=mark_price_stores,
        candle_buffers=candle_buffers,
        trade_flow_stores=trade_flow_stores
    )

    # 5. TerminalDisplay
    display = TerminalDisplay(
        config=config,
        orderbook_stores=orderbook_stores,
        mark_price_stores=mark_price_stores,
        candle_buffers=candle_buffers,
        trade_flow_stores=trade_flow_stores,
        health_check=ws_manager.health_check
    )

    # 6. Start concurrently
    try:
        await asyncio.gather(
            ws_manager.start(),
            display.start()
        )
    except Exception as e:
        logger.error(f"Error {e}")
    except BaseException:
        pass
    finally:
        # 7. Graceful shutdown
        await ws_manager.stop()
        await display.stop()
        logger.info("ARIA shutdown complete")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
