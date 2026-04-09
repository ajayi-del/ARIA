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
        self.trade_flow_stores = trade_flow_stores

        self._spot_connected = False
        self._perps_connected = False
        self._spot_last_msg_ms: int = 0
        self._perps_last_msg_ms: int = 0
        self._total_messages = 0

        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        if self.config.mode == "paper":
            logger.info("Starting in PAPER mode (synthetic data generator)")
            task = asyncio.create_task(self._synthetic_generator())
            self._tasks.append(task)
        else:
            logger.info(f"Starting WebSockets in {self.config.mode.upper()} mode")
            t_spot = asyncio.create_task(self._connect_spot())
            t_perps = asyncio.create_task(self._connect_perps())
            self._tasks.extend([t_spot, t_perps])
        
        await asyncio.gather(*self._tasks)

    async def stop(self) -> None:
        logger.info("Stopping WebSocket Manager")
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def health_check(self) -> dict:
        now = int(time.time() * 1000)
        spot_age = now - self._spot_last_msg_ms if self._spot_last_msg_ms else 999999
        perps_age = now - self._perps_last_msg_ms if self._perps_last_msg_ms else 999999

        if self.config.mode == "paper":
            return {
                "spot_connected": True,
                "perps_connected": True,
                "spot_last_msg_age_ms": spot_age,
                "perps_last_msg_age_ms": perps_age,
                "total_messages_received": self._total_messages
            }

        return {
            "spot_connected": self._spot_connected,
            "perps_connected": self._perps_connected,
            "spot_last_msg_age_ms": spot_age,
            "perps_last_msg_age_ms": perps_age,
            "total_messages_received": self._total_messages
        }

    async def _connect_spot(self) -> None:
        await self._connect_with_retry(self.config.ws_spot_url, feed_name="spot", is_spot=True)

    async def _connect_perps(self) -> None:
        await self._connect_with_retry(self.config.ws_perps_url, feed_name="perps", is_spot=False)

    async def _connect_with_retry(self, url: str, feed_name: str, is_spot: bool) -> None:
        delays = [1, 2, 4, 8, 16]
        attempt = 0

        while attempt < len(delays):
            try:
                async with websockets.connect(url, ping_interval=30) as ws:
                    if is_spot:
                        self._spot_connected = True
                    else:
                        self._perps_connected = True
                    
                    logger.info(f"Connected to {feed_name.upper()} feed", url=url)
                    attempt = 0
                    
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
                delay = delays[attempt]
                logger.warning(f"{feed_name.upper()} WebSocket disconnected. Reconnecting in {delay}s...", error=str(e))
                if is_spot:
                    self._spot_connected = False
                else:
                    self._perps_connected = False
                
                await asyncio.sleep(delay)
                attempt += 1

        logger.error(f"{feed_name.upper()} WebSocket failed after {len(delays)} attempts.")

    async def _subscribe(self, ws, symbol: str, streams: list[str]) -> None:
        msg = {
            "op": "subscribe",
            "params": streams
        }
        await ws.send(json.dumps(msg))

    async def _handle_message(self, msg: str, feed: str) -> None:
        try:
            data = json.loads(msg)
            # Hypothetical handler for real stream
        except Exception as e:
            logger.error("Error parsing message", error=str(e))

    async def _synthetic_generator(self) -> None:
        prices = {
            "BTC": 71000.0,
            "ETH": 2200.0,
            "SOL": 83.0,
            "XAUT": 3018.0
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
