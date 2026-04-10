import asyncio
import json
import time
import random
import structlog
import websockets
from websockets.exceptions import ConnectionClosed

from core.config import Settings
from data.orderbook_store import OrderbookStore
from data.mark_price_store import MarkPriceStore
from data.candle_buffer import CandleBuffer, Candle
from data.trade_flow_store import TradeFlowStore, Trade

logger = structlog.get_logger(__name__)

class WebSocketManager:
    def __init__(
        self,
        config: Settings,
        orderbook_stores: dict[str, OrderbookStore],
        mark_price_stores: dict[str, MarkPriceStore],
        candle_buffers: dict[str, dict[str, CandleBuffer]],
        trade_flow_stores: dict[str, TradeFlowStore]
    ):
        self.config = config
        self.orderbook_stores = orderbook_stores
        self.mark_price_stores = mark_price_stores
        self.candle_buffers = candle_buffers
        self._trade_flow_stores = trade_flow_stores
        
        # Connection management (v1.3 Hardening)
        self._is_active = False
        self._reconnect_delay = 1.0  # Base delay in seconds
        self._max_reconnect_delay = 60.0
        self._jitter_factor = 0.2
        
        logger.info("websocket_manager_initialized")
        self._spot_last_msg_ms: int = 0
        self._perps_last_msg_ms: int = 0
        self._total_messages = 0

        self._tasks: list[asyncio.Task] = []
        self._spot_connected = False
        self._perps_connected = False

    async def start(self) -> None:
        """
        Starts WebSocket connections as background tasks and returns immediately.
        Connections are self-healing (retry logic in _connect_with_retry).
        """
        self._is_active = True
        
        if self.config.data_source == "synthetic":
            logger.info("Starting in SYNTHETIC data mode")
            task = asyncio.create_task(self._synthetic_generator())
            self._tasks.append(task)
        else:
            logger.info(f"Starting real WebSockets (Data Source: {self.config.data_source.upper()})")
            # Fire both streams as background tasks
            asyncio.create_task(self._connect_spot())
            asyncio.create_task(self._connect_perps())
        
        logger.info("websocket_manager_start_complete_returning")

    async def stop(self) -> None:
        self._is_active = False
        logger.info("Stopping WebSocket Manager")
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def health_check(self) -> dict:
        now = int(time.time() * 1000)
        spot_age = now - self._spot_last_msg_ms if self._spot_last_msg_ms else 999999
        perps_age = now - self._perps_last_msg_ms if self._perps_last_msg_ms else 999999

        if self.config.data_source == "synthetic":
            return {
                "spot_connected": True,
                "perps_connected": True,
                "spot_last_msg_age_ms": spot_age,
                "perps_last_msg_age_ms": perps_age,
                "total_messages_received": self._total_messages,
                "data_source": "synthetic"
            }

        return {
            "spot_connected": self._spot_connected,
            "perps_connected": self._perps_connected,
            "spot_last_msg_age_ms": spot_age,
            "perps_last_msg_age_ms": perps_age,
            "total_messages_received": self._total_messages,
            "data_source": self.config.data_source
        }

    async def _connect_spot(self) -> None:
        await self._connect_with_retry(self.config.ws_spot_url, feed_name="spot", is_spot=True)

    async def _connect_perps(self) -> None:
        await self._connect_with_retry(self.config.ws_perps_url, feed_name="perps", is_spot=False)

    async def _connect_with_retry(self, url: str, feed_name: str, is_spot: bool) -> None:
        while self._is_active:
            try:
                logger.info("connecting_to_websocket", url=url)
                async with websockets.connect(
                    url, 
                    ping_interval=20, 
                    ping_timeout=10,
                    ssl=False  # Dev Bypass: Ignore SSL certs
                ) as ws:
                    # Connection successful - reset backoff
                    self._reconnect_delay = 1.0
                    
                    if is_spot:
                        self._spot_connected = True
                    else:
                        self._perps_connected = True
                    
                    logger.info("websocket_connected", url=url)
                    
                    for asset in self.config.assets:
                        streams = [f"{asset}@orderbook", f"{asset}@trade", f"{asset}@kline_1m", f"{asset}@kline_15m"]
                        if not is_spot:
                            streams.append(f"{asset}@markPrice")
                        await self._subscribe(ws, asset, streams)

                    async for msg in ws:
                        if is_spot:
                            self._spot_last_msg_ms = int(time.time() * 1000)
                        else:
                            self._perps_last_msg_ms = int(time.time() * 1000)
                        
                        self._total_messages += 1
                        await self._handle_message(msg, feed_name)

            except (ConnectionClosed, Exception) as e:
                if not self._is_active:
                    break
                    
                # Calculate exponential delay with jitter
                delay = self._reconnect_delay * (1 + random.uniform(-self._jitter_factor, self._jitter_factor))
                
                logger.warning(f"{feed_name.upper()} WebSocket disconnected. Reconnecting in {delay:.2f}s...", error=str(e))
                
                if is_spot:
                    self._spot_connected = False
                else:
                    self._perps_connected = False
                
                await asyncio.sleep(delay)
                
                # Update backoff for next attempt
                self._reconnect_delay = min(self._max_reconnect_delay, self._reconnect_delay * 1.5)

        logger.info(f"{feed_name.upper()} WebSocket loop terminated.")

    async def _subscribe(self, ws, symbol: str, streams: list[str]) -> None:
        msg = {
            "op": "subscribe",
            "params": streams
        }
        await ws.send(json.dumps(msg))

    async def _handle_message(self, msg: str, feed: str) -> None:
        try:
            data = json.loads(msg)
            stream = data.get("stream", "")
            if not stream:
                return

            payload = data.get("data", {})
            symbol = stream.split("@")[0]
            msg_type = stream.split("@")[1]
            now = int(time.time() * 1000)

            # 1. Orderbook depth
            if msg_type == "orderbook":
                if symbol in self.orderbook_stores:
                    bids = [(float(p), float(q)) for p, q in payload.get("bids", [])]
                    asks = [(float(p), float(q)) for p, q in payload.get("asks", [])]
                    self.orderbook_stores[symbol].update(bids, asks, now)

            # 2. Mark Price (Perps only)
            elif msg_type == "markPrice":
                if symbol in self.mark_price_stores:
                    mark = float(payload.get("markPrice", 0))
                    last = float(payload.get("lastPrice", 0))
                    self.mark_price_stores[symbol].update(mark, last, now)

            # 3. Trades
            elif msg_type == "trade":
                if symbol in self.trade_flow_stores:
                    side = payload.get("side", "buy").lower()
                    t = Trade(
                        timestamp_ms=int(payload.get("time", now)),
                        price=float(payload.get("price", 0)),
                        size=float(payload.get("quantity", 0)),
                        side=side,
                        is_aggressor_buy=(side == "buy")
                    )
                    self.trade_flow_stores[symbol].add(t)

            # 4. Klines (1m/15m)
            elif "kline" in msg_type:
                interval = msg_type.split("_")[1]
                if symbol in self.candle_buffers and interval in self.candle_buffers[symbol]:
                    k = payload.get("kline", {})
                    c = Candle(
                        open_time=int(k.get("t", 0)),
                        open=float(k.get("o", 0)),
                        high=float(k.get("h", 0)),
                        low=float(k.get("l", 0)),
                        close=float(k.get("c", 0)),
                        volume=float(k.get("v", 0)),
                        close_time=int(k.get("T", 0))
                    )
                    self.candle_buffers[symbol][interval].add(c)

        except Exception as e:
            logger.error("Error parsing WebSocket message", error=str(e), msg=msg[:100])

    async def _synthetic_generator(self) -> None:
        prices = {
            "BTC-USD": 71000.0,
            "ETH-USD": 2200.0,
            "SOL-USD": 83.0,
            "XAUT-USD": 3018.0,
            "BNB-USD": 600.0,
            "LINK-USD": 18.0,
            "AVAX-USD": 35.0
        }
        
        while True:
            now = int(time.time() * 1000)
            self._spot_last_msg_ms = now
            self._perps_last_msg_ms = now
            self._total_messages += len(self.config.assets) * 4

            for asset in self.config.assets:
                if asset not in prices:
                    prices[asset] = 100.0
                
                move = prices[asset] * random.uniform(-0.001, 0.001)
                prices[asset] += move

                spread = prices[asset] * 0.0005
                bids = [
                    (prices[asset] - spread - (i * spread), random.uniform(0.1, 5.0))
                    for i in range(10)
                ]
                asks = [
                    (prices[asset] + spread + (i * spread), random.uniform(0.1, 5.0))
                    for i in range(10)
                ]
                if asset in self.orderbook_stores:
                    self.orderbook_stores[asset].update(bids, asks, now)

                if asset in self.mark_price_stores:
                    mark = prices[asset] * random.uniform(0.999, 1.001)
                    self.mark_price_stores[asset].update(mark, prices[asset], now)

                if asset in self.candle_buffers:
                    for interval in self.candle_buffers[asset]:
                        c = Candle(
                            open_time=now - 60000,
                            open=prices[asset] * random.uniform(0.999, 1.001),
                            high=prices[asset] * 1.002,
                            low=prices[asset] * 0.998,
                            close=prices[asset],
                            volume=random.uniform(10, 100),
                            close_time=now
                        )
                        self.candle_buffers[asset][interval].add(c)

                if asset in self.trade_flow_stores:
                    num_trades = random.randint(0, 5)
                    for _ in range(num_trades):
                        side = random.choice(["buy", "sell"])
                        t = Trade(
                            timestamp_ms=now - random.randint(0, 500),
                            price=prices[asset] * random.uniform(0.999, 1.001),
                            size=random.uniform(0.01, 2.0),
                            side=side,
                            is_aggressor_buy=(side == "buy")
                        )
                        self.trade_flow_stores[asset].add(t)

            await asyncio.sleep(self.config.loop_interval_ms / 1000.0)
