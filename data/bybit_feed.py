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
        trade_flow_stores: dict,
        bybit_ticker_stores: dict = None):

        self.config = config
        self.mark_price_stores = mark_price_stores
        self.orderbook_stores = orderbook_stores
        self.candle_buffers = candle_buffers
        self.trade_flow_stores = trade_flow_stores
        self.bybit_ticker_stores = bybit_ticker_stores  # OI + funding intelligence
        self._running = False
        self._task: asyncio.Task | None = None
        self._msg_count = 0

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
            # Subscribe to tickers when:
            #  - mark_price_stores populated (standalone mode), OR
            #  - bybit_ticker_stores provided (hybrid mode OI+funding intelligence)
            if self.mark_price_stores or self.bybit_ticker_stores is not None:
                subs.append(f"tickers.{b}")
            subs.append(f"kline.1.{b}")
            subs.append(f"publicTrade.{b}")
            subs.append(f"orderbook.50.{b}")

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
        self._msg_count += 1
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

        # 1. Tickers — mark price + OI/funding intelligence
        if topic.startswith("tickers."):
            if isinstance(data, dict):
                mark = data.get("markPrice")
                last = data.get("lastPrice")
                if mark and float(mark) > 0:
                    # Update mark price store (standalone mode)
                    store = self.mark_price_stores.get(symbol)
                    if store:
                        store.update(float(last or mark), float(mark), now_ms)
                    # Update ticker intelligence store (hybrid mode)
                    if self.bybit_ticker_stores is not None and symbol in self.bybit_ticker_stores:
                        prev = self.bybit_ticker_stores[symbol]
                        prev_oi = prev.get("open_interest", 0.0)
                        prev_mp = prev.get("mark_price", 0.0)
                        self.bybit_ticker_stores[symbol] = {
                            "funding_rate": float(data.get("fundingRate", 0) or 0),
                            "open_interest": float(data.get("openInterest", 0) or 0),
                            "prev_open_interest": prev_oi,
                            "prev_mark_price": prev_mp if prev_mp > 0 else float(mark),
                            "mark_price": float(mark),
                        }

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
                        if buf:
                            buf.add(candle)
                            count = buf.count()
                        
                        # ALWAYS publish after buffer update
                        # This keeps the interpreter running on every tick
                        event_bus.publish(Event(
                            event_type=EventType.CANDLE_CLOSED,
                            symbol=symbol,
                            timestamp_ms=int(k["start"]),
                            data={
                                "count": count if buf else 0,
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
                    try:
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
                    except Exception as e:
                        logger.warning("bybit_trade_parse_error", symbol=symbol, error=str(e))

        # 4. Orderbook — incremental L2 maintenance
        # Bybit sends: type="snapshot" (full book) then type="delta" (changed levels only).
        # Deltas may have an empty "b" or "a" list — must MERGE into existing book,
        # NOT replace. Replacing with a partial side leaves bids=[] or asks=[] which
        # causes DataStaleError in top_of_book() → ob_error_skip in the liquidity gate.
        elif topic.startswith("orderbook."):
            if not isinstance(data, dict):
                return

            bids_raw = data.get("b", [])
            asks_raw = data.get("a", [])
            msg_type = msg.get("type", "delta")   # "snapshot" | "delta"

            if not bids_raw and not asks_raw:
                return

            store = self.orderbook_stores.get(symbol)
            if not store:
                return

            def _parse_levels(raw: list) -> dict:
                """Convert [[price_str, size_str], ...] → {price: size}. size=0 → removal."""
                out = {}
                for item in raw:
                    try:
                        p, s = float(item[0]), float(item[1])
                        if p > 0:
                            out[p] = s
                    except (IndexError, ValueError):
                        continue
                return out

            if msg_type == "snapshot":
                # Full replacement — use all levels from message
                bids_map = _parse_levels(bids_raw)
                asks_map = _parse_levels(asks_raw)
            else:
                # Delta — merge into existing book
                bids_map = {p: s for p, s in store.bids}  # existing
                asks_map = {p: s for p, s in store.asks}  # existing
                for p, s in _parse_levels(bids_raw).items():
                    if s == 0.0:
                        bids_map.pop(p, None)   # size=0 means remove level
                    else:
                        bids_map[p] = s
                for p, s in _parse_levels(asks_raw).items():
                    if s == 0.0:
                        asks_map.pop(p, None)
                    else:
                        asks_map[p] = s

            # Build sorted lists — top 20 levels, filter zero-size
            bids = sorted(
                [(p, s) for p, s in bids_map.items() if s > 0],
                key=lambda x: x[0], reverse=True
            )[:20]
            asks = sorted(
                [(p, s) for p, s in asks_map.items() if s > 0],
                key=lambda x: x[0]
            )[:20]

            if not bids or not asks:
                return

            store.update(bids, asks, now_ms)

            event_bus.publish(Event(
                event_type=EventType.ORDERBOOK_UPDATED,
                symbol=symbol,
                timestamp_ms=now_ms,
                data={
                    "bids_len": len(bids),
                    "asks_len": len(asks),
                    "best_bid": bids[0][0],
                    "best_ask": asks[0][0],
                    "type": msg_type,
                }
            ))

    def health_check(self) -> dict:
        return {
            "feed": "bybit_public",
            "url": BYBIT_WS_URL,
            "status": "running" if self._running else "stopped",
            "connected": self._running,
            "total_messages_received": self._msg_count,
            "latency_ms": 0,
            "supported": SUPPORTED_ASSETS
        }

    async def fetch_historical(self) -> None:
        """Fetches last 55 candles for all assets to eliminate warmup latency."""
        import httpx
        import certifi

        BYBIT_REST = "https://api.bybit.com/v5/market/kline"

        for symbol in SUPPORTED_ASSETS:
            if symbol not in self.config.assets:
                continue
            bybit_sym = BYBIT_SYMBOL_MAP.get(symbol)
            if not bybit_sym or bybit_sym == "unknown":
                continue
            try:
                async with httpx.AsyncClient(verify=certifi.where()) as client:
                    resp = await asyncio.wait_for(
                        client.get(BYBIT_REST, params={
                            "category": "linear",
                            "symbol": bybit_sym,
                            "interval": "1",
                            "limit": 55
                        }),
                        timeout=10.0
                    )
                    data = resp.json()
                    candles_raw = data.get("result", {}).get("list", [])

                    # Bybit returns newest first, we MUST reverse to maintain chronological order in buf.add()
                    from data.candle_buffer import Candle
                    for row in reversed(candles_raw):
                        candle = Candle(
                            open_time=int(row[0]),
                            open=float(row[1]),
                            high=float(row[2]),
                            low=float(row[3]),
                            close=float(row[4]),
                            volume=float(row[5]),
                            close_time=int(row[0]) + 60000
                        )
                        buf = self.candle_buffers.get(symbol, {}).get("1m")
                        if buf:
                            buf.add(candle)

                    count = self.candle_buffers.get(symbol, {}).get("1m").count()
                    logger.info("historical_loaded", symbol=symbol, candles=count)

            except Exception as e:
                logger.warning("historical_fetch_failed", symbol=symbol, error=str(e))

    async def fetch_real_funding_rates(self) -> dict:
        """Fetches definitive funding rates from Bybit REST API."""
        import httpx
        import certifi

        rates = {}
        url = "https://api.bybit.com/v5/market/tickers"

        try:
            async with httpx.AsyncClient(verify=certifi.where()) as client:
                for symbol in SUPPORTED_ASSETS:
                    if symbol not in self.config.assets:
                        continue
                    bybit_sym = BYBIT_SYMBOL_MAP.get(symbol, "unknown")
                    if bybit_sym == "unknown":
                        continue
                    try:
                        resp = await asyncio.wait_for(
                            client.get(url, params={
                                "category": "linear",
                                "symbol": bybit_sym
                            }),
                            timeout=5.0
                        )
                        data = resp.json()
                        items = data.get("result", {}).get("list", [])
                        if items:
                            rate = float(items[0].get("fundingRate", "0"))
                            rates[symbol] = rate
                    except Exception:
                        continue
        except Exception as e:
            logger.warning("funding_rate_fetch_error", error=str(e))

        return rates


class HybridFeed:
    """
    Hybrid data architecture:
      - BybitFeed  → candles, orderbook, trade flow  (intelligence / ATR / VPIN)
      - SoDEXFeed  → mark prices only                (execution reference / divergence)

    Funding rates always come from SoDEX.
    Historical candle fetch uses Bybit (confirmed closes, real volume).
    health_check delegates to the Bybit leg (richer message stats).
    """

    def __init__(self, intelligence_feed: "BybitFeed", marks_feed):
        self._intel = intelligence_feed   # BybitFeed
        self._marks = marks_feed          # SoDEXFeed (mark prices only)

    async def start(self) -> None:
        await self._intel.start()
        await self._marks.start()

    async def stop(self) -> None:
        await self._intel.stop()
        await self._marks.stop()

    async def fetch_historical(self) -> None:
        """Bybit REST historical candle fetch — real confirmed closes."""
        await self._intel.fetch_historical()

    async def fetch_funding_rates(self) -> dict:
        """Funding rates are SoDEX-native only (we arb SoDEX funding, not Bybit)."""
        return await self._marks.fetch_funding_rates()

    def health_check(self) -> dict:
        hc = self._intel.health_check()
        hc["sodex_marks_connected"] = self._marks._running
        hc["architecture"] = "bybit_intel+sodex_marks"
        return hc
