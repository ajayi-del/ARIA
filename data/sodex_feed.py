import asyncio
import json
import ssl
import certifi
import time
import websockets
from core.event_bus import event_bus, Event, EventType
from core.config import Settings
import structlog

logger = structlog.get_logger(__name__)

# SoDEX symbol format matches ARIA
# BTC-USD, ETH-USD etc — no mapping needed
SODEX_SUPPORTED = [
    "BTC-USD", "ETH-USD", "SOL-USD",
    "XAUT-USD", "BNB-USD", "LINK-USD",
    "AVAX-USD"
]

class SoDEXFeed:
    """
    v1.3 Native SoDEX WebSocket Feed.
    Replaces Bybit fallback for mainnet trading.
    """
    def __init__(self,
        config: Settings,
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
        self._msg_count = 0
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Starts the SoDEX feed as a background task."""
        self._running = True
        logger.info("starting_sodex_feed", assets=self.config.assets)
        self._task = asyncio.create_task(self._run_perps_stream())

    async def stop(self) -> None:
        """Stops the feed."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("sodex_feed_stopped")

    async def _run_perps_stream(self) -> None:
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        url = self.config.sodex_ws_perps

        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    url,
                    ssl=ssl_ctx,
                    ping_interval=None,
                    close_timeout=5
                ) as ws:

                    # Subscribe to all needed channels
                    await self._subscribe_all(ws)

                    backoff = 1.0
                    last_msg_time = time.time()

                    async def keepalive():
                        while self._running:
                            await asyncio.sleep(30)
                            elapsed = time.time() - last_msg_time
                            if elapsed > 30:
                                try:
                                    await ws.send(json.dumps({"op": "ping"}))
                                except Exception:
                                    pass

                    ka_task = asyncio.create_task(keepalive())

                    logger.info("sodex_feed_connected", url=url)

                    try:
                        async for raw in ws:
                            if not self._running:
                                break
                            last_msg_time = time.time()
                            self._msg_count += 1
                            try:
                                msg = json.loads(raw)
                                if msg.get("op") == "pong":
                                    continue
                                await self._handle(msg)
                            except Exception as e:
                                logger.warning("sodex_msg_error", error=str(e))
                    finally:
                        ka_task.cancel()

            except Exception as e:
                if not self._running:
                    break
                logger.warning("sodex_feed_disconnected", error=str(e), reconnect_in=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _subscribe_all(self, ws) -> None:
        assets = [a for a in self.config.assets if a in SODEX_SUPPORTED]

        # 1. Subscribe mark price (has funding rate)
        await ws.send(json.dumps({
            "op": "subscribe",
            "params": {
                "channel": "markPrice",
                "symbols": assets
            }
        }))

        # 2. Per-asset subscriptions
        for symbol in assets:
            # Candles 1m
            await ws.send(json.dumps({
                "op": "subscribe",
                "params": {
                    "channel": "candle",
                    "symbol": symbol,
                    "interval": "1m"
                }
            }))
            # L2 Orderbook
            await ws.send(json.dumps({
                "op": "subscribe",
                "params": {
                    "channel": "l2Book",
                    "symbol": symbol,
                    "tickSize": "0.01"
                }
            }))
            # Market Trades
            await ws.send(json.dumps({
                "op": "subscribe",
                "params": {
                    "channel": "marketTrade",
                    "symbol": symbol
                }
            }))

        logger.info("sodex_subscribed", assets=assets)

    async def _handle(self, msg: dict) -> None:
        channel = msg.get("channel", "")
        data = msg.get("data", {})
        now_ms = int(time.time() * 1000)

        # ── Mark Price (+ funding rate) ─────
        if channel == "markPrice":
            items = data if isinstance(data, list) else [data]
            for item in items:
                symbol = item.get("s", "")
                if not symbol or symbol not in self.config.assets:
                    continue
                mark = float(item.get("p", 0))
                index = float(item.get("i", mark))
                funding_rate = float(item.get("r", 0))
                event_time = int(item.get("E", now_ms))

                if mark == 0:
                    continue

                store = self.mark_price_stores.get(symbol)
                if store:
                    store.update(index, mark, event_time)

                event_bus.publish(Event(
                    event_type=EventType.MARK_PRICE_UPDATED,
                    symbol=symbol,
                    timestamp_ms=event_time,
                    data={
                        "mark_price": mark,
                        "last_price": index,
                        "funding_rate": funding_rate
                    }
                ))

        # ── Candles ─────────────────────────
        elif channel == "candle":
            if not isinstance(data, dict):
                return
            symbol = data.get("s", "")
            if not symbol or symbol not in self.config.assets:
                return

            try:
                from data.candle_buffer import Candle
                # SoDEX candle fields: t=open_time, T=close_time, o, h, l, c, v, x=closed
                candle = Candle(
                    open_time=int(data.get("t", now_ms)),
                    open=float(data.get("o", 0)),
                    high=float(data.get("h", 0)),
                    low=float(data.get("l", 0)),
                    close=float(data.get("c", 0)),
                    volume=float(data.get("v", 0)),
                    close_time=int(data.get("T", now_ms + 60000))
                )
                confirmed = bool(data.get("x", False))

                buf = self.candle_buffers.get(symbol, {}).get("1m")
                if buf:
                    buf.add(candle)
                    count = buf.count()

                    event_bus.publish(Event(
                        event_type=EventType.CANDLE_CLOSED,
                        symbol=symbol,
                        timestamp_ms=int(data.get("t", now_ms)),
                        data={
                            "count": count,
                            "close": float(data.get("c", 0)),
                            "confirmed": confirmed
                        }
                    ))
            except Exception as e:
                logger.warning("candle_parse_error", symbol=symbol, error=str(e))

        # ── L2 Orderbook ────────────────────
        elif channel == "l2Book":
            if not isinstance(data, dict):
                return
            symbol = data.get("s", "")
            if not symbol or symbol not in self.config.assets:
                return

            try:
                bids_raw = data.get("b", [])
                asks_raw = data.get("a", [])
                event_time = int(data.get("E", now_ms))

                bids = []
                for item in bids_raw[:20]:
                    try:
                        price = float(item[0])
                        size = float(item[1])
                        if price > 0 and size > 0:
                            bids.append((price, size))
                    except Exception:
                        continue

                asks = []
                for item in asks_raw[:20]:
                    try:
                        price = float(item[0])
                        size = float(item[1])
                        if price > 0 and size > 0:
                            asks.append((price, size))
                    except Exception:
                        continue

                # Sort: bids descending, asks ascending
                bids.sort(key=lambda x: x[0], reverse=True)
                asks.sort(key=lambda x: x[0])

                store = self.orderbook_stores.get(symbol)
                if store and (bids or asks):
                    store.update(bids, asks, event_time)

                    event_bus.publish(Event(
                        event_type=EventType.ORDERBOOK_UPDATED,
                        symbol=symbol,
                        timestamp_ms=event_time,
                        data={
                            "bids_len": len(bids),
                            "asks_len": len(asks),
                            "best_bid": bids[0][0] if bids else 0.0,
                            "best_ask": asks[0][0] if asks else 0.0
                        }
                    ))
            except Exception as e:
                logger.warning("orderbook_parse_error", symbol=symbol, error=str(e))

        # ── Market Trades ────────────────────
        elif channel == "marketTrade":
            trades = data if isinstance(data, list) else [data]
            for trade in trades:
                symbol = trade.get("s", "")
                if not symbol or symbol not in self.config.assets:
                    continue
                try:
                    price = float(trade.get("p", 0))
                    size = float(trade.get("q", 0))
                    # m=true means buyer is maker -> seller is aggressor -> sell
                    is_buyer_maker = bool(trade.get("m", False))
                    side = "sell" if is_buyer_maker else "buy"

                    if price > 0 and size > 0:
                        from data.trade_flow_store import Trade
                        trade_obj = Trade(
                            timestamp_ms=now_ms,
                            price=price,
                            size=size,
                            side=side,
                            is_aggressor_buy=(side == "buy")
                        )
                        store = self.trade_flow_stores.get(symbol)
                        if store:
                            store.add(trade_obj)
                            # Event bus notification is implicit via store or we can add it
                            event_bus.publish(Event(
                                event_type=EventType.TRADE_FLOW_UPDATED,
                                symbol=symbol,
                                timestamp_ms=now_ms,
                                data={}
                            ))
                except Exception:
                    continue

    async def fetch_historical(self) -> None:
        """Fetches last 55 candles for all assets to eliminate warmup latency."""
        import httpx
        rest_url = self.config.sodex_rest_perps
        endpoint = f"{rest_url}/markets"

        for symbol in self.config.assets:
            if symbol not in SODEX_SUPPORTED:
                continue
            try:
                async with httpx.AsyncClient(verify=certifi.where(), timeout=10.0) as client:
                    resp = await client.get(
                        f"{endpoint}/{symbol}/klines",
                        params={"interval": "1m", "limit": 55}
                    )
                    if resp.status_code != 200:
                        logger.warning("historical_fetch_failed", symbol=symbol, status=resp.status_code)
                        continue

                    data = resp.json()
                    candles_raw = data.get("data", [])

                    from data.candle_buffer import Candle
                    for row in candles_raw:
                        try:
                            # row format expected: t, T, o, h, l, c, v
                            candle = Candle(
                                open_time=int(row.get("t", 0)),
                                open=float(row.get("o", 0)),
                                high=float(row.get("h", 0)),
                                low=float(row.get("l", 0)),
                                close=float(row.get("c", 0)),
                                volume=float(row.get("v", 0)),
                                close_time=int(row.get("T", 0))
                            )
                            buf = self.candle_buffers.get(symbol, {}).get("1m")
                            if buf:
                                buf.add(candle)
                        except Exception:
                            continue

                    buf = self.candle_buffers.get(symbol, {}).get("1m")
                    if buf:
                        logger.info("sodex_historical_loaded", symbol=symbol, candles=buf.count())

            except Exception as e:
                logger.warning("sodex_historical_error", symbol=symbol, error=str(e))

    async def fetch_funding_rates(self) -> dict:
        """Fetches definitive funding rates from SoDEX REST API."""
        import httpx
        rates = {}
        rest_url = self.config.sodex_rest_perps

        try:
            async with httpx.AsyncClient(verify=certifi.where(), timeout=10.0) as client:
                resp = await client.get(f"{rest_url}/markets/mark-prices")
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("data", [])
                    for item in items:
                        symbol = item.get("symbol", item.get("s", ""))
                        rate = float(item.get("fundingRate", item.get("r", 0)))
                        if symbol in self.config.assets:
                            rates[symbol] = rate
        except Exception as e:
            logger.warning("sodex_funding_fetch_error", error=str(e))

        return rates

    def health_check(self) -> dict:
        return {
            "feed": "sodex_mainnet",
            "url": self.config.sodex_ws_perps,
            "status": "running" if self._running else "stopped",
            "connected": self._running,
            "total_messages_received": self._msg_count,
            "latency_ms": 0
        }
