import asyncio
import json
import ssl
import certifi
import websockets
import time
import structlog
from core.event_bus import event_bus, Event, EventType
from data.trade_flow_store import Trade
from data.candle_buffer import Candle

logger = structlog.get_logger(__name__)

# Map ARIA symbols to Bybit symbols
BYBIT_SYMBOL_MAP = {
    "BTC-USD":       "BTCUSDT",
    "ETH-USD":       "ETHUSDT",
    "SOL-USD":       "SOLUSDT",
    "XAUT-USD":      "XAUTUSDT",
    "BNB-USD":       "BNBUSDT",
    "LINK-USD":      "LINKUSDT",
    "AVAX-USD":      "AVAXUSDT",
    "USTECH100-USD": "unknown",
}

SUPPORTED_ASSETS = [
    "BTC-USD", "ETH-USD", "SOL-USD",
    "BNB-USD", "LINK-USD", "AVAX-USD",
    "XAUT-USD"
]

BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"

class BybitFeed:
    def __init__(self,
        config,
        mark_price_stores: dict,
        orderbook_stores: dict,
        candle_buffers: dict,
        trade_flow_stores: dict):

        self.config = config
        self.mark_price_stores = mark_price_stores
        self.orderbook_stores = orderbook_stores
        self.candle_buffers = candle_buffers
        self.trade_flow_stores = trade_flow_stores
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Starts Bybit WebSocket connection as a background task."""
        self._running = True
        logger.info("starting_bybit_feed")
        self._task = asyncio.create_task(self._run_stream())

    async def stop(self) -> None:
        """Stops the Bybit stream task."""
        self._running = False
        logger.info("stopping_bybit_feed")
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_stream(self) -> None:
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())

        # Build subscription list
        subs = []
        for symbol in SUPPORTED_ASSETS:
            if symbol not in self.config.assets:
                continue
            b = BYBIT_SYMBOL_MAP.get(symbol)
            if not b or b == "unknown":
                continue
            subs.append(f"tickers.{b}")
            subs.append(f"kline.1.{b}")
            subs.append(f"publicTrade.{b}")

        if not subs:
            logger.warning("bybit_no_subscriptions", message="No supported assets configured")
            return

        backoff = 1.0
        while self._running:
            try:
                logger.info("connecting_to_bybit", url=BYBIT_WS_URL)
                async with websockets.connect(
                    BYBIT_WS_URL,
                    ssl=ssl_ctx,
                    ping_interval=20,
                    ping_timeout=10
                ) as ws:

                    # Subscribe to all topics
                    await ws.send(json.dumps({
                        "op": "subscribe",
                        "args": subs
                    }))

                    backoff = 1.0
                    logger.info("bybit_connected_and_subscribed")
                    
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            await self._handle(msg)
                        except Exception as e:
                            logger.error("bybit_msg_parse_error", error=str(e))

            except Exception as e:
                if not self._running:
                    break
                logger.warning("bybit_connection_lost", error=str(e), retry_in=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _handle(self, msg: dict) -> None:
        topic = msg.get("topic", "")
        data = msg.get("data", {})
        now_ms = int(time.time() * 1000)

        # Find which ARIA symbol this is
        symbol = None
        for aria_sym, bybit_sym in BYBIT_SYMBOL_MAP.items():
            if bybit_sym != "unknown" and bybit_sym in topic:
                symbol = aria_sym
                break

        if not symbol or symbol not in self.config.assets:
            return

        # 1. Tickers (mark price)
        if topic.startswith("tickers."):
            if isinstance(data, dict):
                mark = data.get("markPrice")
                last = data.get("lastPrice")
                if mark and float(mark) > 0:
                    self.mark_price_stores[symbol].update(
                        float(last or mark),
                        float(mark),
                        now_ms
                    )
                    # Event published by store implicitly

        # 2. Kline (candle)
        elif topic.startswith("kline."):
            if isinstance(data, list):
                for k in data:
                    try:
                        # ARIA Candle: open_time, open, high, low, close, volume, close_time
                        candle = Candle(
                            open_time=int(k["start"]),
                            open=float(k["open"]),
                            high=float(k["high"]),
                            low=float(k["low"]),
                            close=float(k["close"]),
                            volume=float(k["volume"]),
                            close_time=int(k["end"])
                        )
                        
                        buf = self.candle_buffers.get(symbol, {}).get("1m")
                        if buf is None:
                            continue
                            
                        buf.add(candle)
                        count = buf.count()
                        
                        # ALWAYS publish - every tick, not just confirmed close
                        # Interpreter needs this to count candles for warmup
                        event_bus.publish(Event(
                            event_type=EventType.CANDLE_CLOSED,
                            symbol=symbol,
                            timestamp_ms=int(k["start"]),
                            data={
                                "count": count,
                                "close": float(k["close"]),
                                "confirmed": bool(k.get("confirm", False))
                            }
                        ))
                    except Exception as e:
                        logger.warning("bybit_candle_parse_error", symbol=symbol, error=str(e))

        # 3. Public trades
        elif topic.startswith("publicTrade."):
            if isinstance(data, list):
                for t in data:
                    price = float(t.get("p", 0))
                    qty = float(t.get("v", 0))
                    side = "buy" if t.get("S") == "Buy" else "sell"
                    if price > 0:
                        # ARIA Trade: timestamp_ms, price, size, side, is_aggressor_buy
                        trade = Trade(
                            timestamp_ms=int(t.get("ts", now_ms)),
                            price=price,
                            size=qty,
                            side=side,
                            is_aggressor_buy=(side == "buy")
                        )
                        self.trade_flow_stores[symbol].add(trade)

    def health_check(self) -> dict:
        return {
            "feed": "bybit_public",
            "url": BYBIT_WS_URL,
            "status": "running" if self._running else "stopped",
            "connected": self._running,
            "total_messages_received": 0,
            "latency_ms": 0,
            "supported": SUPPORTED_ASSETS
        }
