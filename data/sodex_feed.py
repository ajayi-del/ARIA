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

_5M_MS  = 5  * 60 * 1_000   # 300_000 ms
_15M_MS = 15 * 60 * 1_000   # 900_000 ms


def _aggregate_higher_tf(sym_bufs: dict, closed_1m) -> None:
    """
    Aggregate closed 1m candles into 5m and 15m buffers at their respective boundaries.
    Called after every confirmed 1m candle close.  Equities / commodities only have
    SoDEX 1m data, so this is how their higher-TF ATR buffers get populated.
    """
    from data.candle_buffer import Candle
    buf_1m = sym_bufs.get("1m")
    if buf_1m is None:
        return
    open_time_ms = int(getattr(closed_1m, "open_time", 0))
    minute_idx = open_time_ms // 60_000

    # 5-minute boundary: the 5th minute in each 5-min window (index % 5 == 4)
    if minute_idx % 5 == 4:
        candles = buf_1m.latest(5)
        if len(candles) == 5:
            agg = Candle(
                open_time=candles[0].open_time,
                open=float(candles[0].open),
                high=float(max(c.high for c in candles)),
                low=float(min(c.low for c in candles)),
                close=float(candles[-1].close),
                volume=float(sum(c.volume for c in candles)),
                close_time=int(getattr(closed_1m, "close_time", open_time_ms + _5M_MS)),
            )
            buf_5m = sym_bufs.get("5m")
            if buf_5m is not None:
                buf_5m.add(agg)

    # 15-minute boundary: the 15th minute in each 15-min window (index % 15 == 14)
    if minute_idx % 15 == 14:
        candles = buf_1m.latest(15)
        if len(candles) == 15:
            agg = Candle(
                open_time=candles[0].open_time,
                open=float(candles[0].open),
                high=float(max(c.high for c in candles)),
                low=float(min(c.low for c in candles)),
                close=float(candles[-1].close),
                volume=float(sum(c.volume for c in candles)),
                close_time=int(getattr(closed_1m, "close_time", open_time_ms + _15M_MS)),
            )
            buf_15m = sym_bufs.get("15m")
            if buf_15m is not None:
                buf_15m.add(agg)


# Whitelist of symbols that SoDEX perps supports.
# Used as fallback when config is not available; at runtime _subscribe_core /
# _stagger_remaining use config.assets directly (already pruned by
# fetch_symbol_ids if called).
SODEX_SUPPORTED = [
    # Core crypto
    "BTC-USD", "ETH-USD", "SOL-USD", "XAUT-USD",
    "BNB-USD", "LINK-USD", "AVAX-USD",
    "SUI-USD", "ARB-USD", "OP-USD", "NEAR-USD",
    "MNT-USD", "1000PEPE-USD", "XRP-USD",
    "TRUMP-USD", "BASED-USD",
    # Commodities
    "CL-USD", "COPPER-USD",
    # Equities — all SoDEX perps, need historical seed to avoid 50-min warmup
    "TSM-USD", "ORCL-USD",
    "NVDA-USD", "MSFT-USD", "AAPL-USD", "AMZN-USD",
    "GOOGL-USD", "META-USD", "TSLA-USD",
]


class SoDEXFeed:
    """
    v2.0 Native SoDEX WebSocket Feed — staggered subscription.

    Core assets (config.core_assets) subscribe immediately on connect so the
    display starts with data right away. Remaining watchlist assets stagger in
    3-at-a-time every 2 s, keeping the event loop free and the Rich terminal
    display stable.
    """

    def __init__(
        self,
        config: Settings,
        mark_price_stores: dict,
        orderbook_stores: dict,
        candle_buffers: dict,
        trade_flow_stores: dict,
    ):
        self.config = config
        self.mark_price_stores = mark_price_stores
        self.orderbook_stores = orderbook_stores
        self.candle_buffers = candle_buffers
        self.trade_flow_stores = trade_flow_stores

        self._running = False
        self._msg_count = 0
        self._task: asyncio.Task | None = None

        # Subscription state — cleared on disconnect, rebuilt on reconnect.
        # Hot-path guard: ensure_subscribed() checks this set first (one lookup).
        self._subscribed: set[str] = set()

    # ── Public API ───────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Starts the SoDEX feed as a background task."""
        self._running = True
        logger.info("starting_sodex_feed",
                    core=len(self.config.core_assets),
                    total=len(self.config.assets))
        self._task = asyncio.create_task(self._run_perps_stream())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("sodex_feed_stopped")

    async def ensure_subscribed(self, symbol: str) -> None:
        """
        Hot-path guard: if symbol is already subscribed, returns in ~50 ns
        (one set lookup). If not yet subscribed, waits up to 2 s.

        In normal operation this is always a no-op — stagger_remaining brings
        all 14 assets online within ~8 s of connect, long before any signal
        can fire (signals need candle history).
        """
        if symbol in self._subscribed:
            return  # fast path

        logger.info("ensure_subscribed_waiting", symbol=symbol)
        deadline = time.monotonic() + 2.0
        while symbol not in self._subscribed and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

        if symbol not in self._subscribed:
            logger.warning("ensure_subscribed_timeout", symbol=symbol)

    def health_check(self) -> dict:
        return {
            "feed": "sodex_mainnet",
            "url": self.config.sodex_ws_perps,
            "status": "running" if self._running else "stopped",
            "connected": self._running,
            "total_messages_received": self._msg_count,
            "subscribed_symbols": len(self._subscribed),
        }

    # ── Connection loop ──────────────────────────────────────────────────────────

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
                    close_timeout=5,
                ) as ws:
                    backoff = 1.0
                    self._subscribed.clear()

                    # Step 1 — core assets immediately.
                    # Intersect with config.assets: fetch_symbol_ids() may have pruned
                    # symbols not yet listed on SoDEX perps.
                    _core = [a for a in self.config.core_assets if a in self.config.assets]
                    await self._subscribe_batch(ws, _core)
                    self._subscribed.update(_core)
                    logger.info("sodex_core_subscribed", symbols=_core)

                    # Step 2 — watchlist staggers in the background
                    asyncio.create_task(
                        self._stagger_remaining(ws)
                    )

                    # Step 3 — keepalive + message loop
                    last_msg_time = time.time()

                    async def _keepalive():
                        while self._running:
                            await asyncio.sleep(30)
                            if time.time() - last_msg_time > 30:
                                try:
                                    await ws.send(json.dumps({"op": "ping"}))
                                except Exception:
                                    pass

                    ka_task = asyncio.create_task(_keepalive())
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
                        await asyncio.gather(ka_task, return_exceptions=True)

            except Exception as e:
                if not self._running:
                    break
                self._subscribed.clear()
                import random as _rand
                jitter = backoff * (0.8 + 0.4 * _rand.random())
                logger.warning("sodex_feed_disconnected",
                               error=str(e), reconnect_in=round(jitter, 2))
                await asyncio.sleep(jitter)
                backoff = min(backoff * 2, 60)

    async def _subscribe_batch(self, ws, symbols: list[str]) -> None:
        """
        Subscribe to all channels for a batch of symbols in two round-trips:
        1. markPrice (batch message — one list, efficient)
        2. Per-symbol: candle + l2Book + marketTrade (3 messages × N symbols)
        """
        if not symbols:
            return

        # markPrice supports bulk subscription
        await ws.send(json.dumps({
            "op": "subscribe",
            "params": {"channel": "markPrice", "symbols": symbols},
        }))

        # Per-symbol streams — send each symbol as its own message to avoid
        # oversized frames being rejected by the server
        for symbol in symbols:
            await ws.send(json.dumps({
                "op": "subscribe",
                "params": {"channel": "candle", "symbol": symbol, "interval": "1m"},
            }))
            await ws.send(json.dumps({
                "op": "subscribe",
                "params": {"channel": "l2Book", "symbol": symbol, "tickSize": "0.01"},
            }))
            await ws.send(json.dumps({
                "op": "subscribe",
                "params": {"channel": "l4Book", "symbol": symbol, "level": 10},
            }))
            await ws.send(json.dumps({
                "op": "subscribe",
                "params": {"channel": "marketTrade", "symbol": symbol},
            }))

    async def _stagger_remaining(self, ws) -> None:
        """
        Subscribe watchlist assets in batches of 3, 2 s apart.
        7 remaining assets → 3 batches → done in ~4 s from connect.
        The display stabilises on core data before any watchlist traffic arrives.
        """
        remaining = [a for a in self.config.assets
                     if a not in self.config.core_assets]
        batch_size = 3
        for i in range(0, len(remaining), batch_size):
            await asyncio.sleep(2.0)
            if not self._running:
                return
            batch = remaining[i:i + batch_size]
            try:
                await self._subscribe_batch(ws, batch)
                self._subscribed.update(batch)
                logger.info("sodex_watchlist_subscribed",
                            batch=batch, total_subscribed=len(self._subscribed))
            except Exception as e:
                logger.warning("sodex_stagger_failed",
                               batch=batch, error=str(e))

    # ── Message dispatcher ───────────────────────────────────────────────────────

    async def _handle(self, msg: dict) -> None:
        channel = msg.get("channel", "")
        data = msg.get("data", {})
        now_ms = int(time.time() * 1000)

        # ── Mark Price (+ funding rate) ──────────────────────────────────────────
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
                    store.update(mark, index, event_time)
                event_bus.publish(Event(
                    event_type=EventType.MARK_PRICE_UPDATED,
                    symbol=symbol,
                    timestamp_ms=event_time,
                    data={
                        "mark_price": mark,
                        "last_price": index,
                        "funding_rate": funding_rate,
                    },
                ))

        # ── Candles ──────────────────────────────────────────────────────────────
        elif channel == "candle":
            if not isinstance(data, dict):
                return
            symbol = data.get("s", "")
            if not symbol or symbol not in self.config.assets:
                return
            try:
                from data.candle_buffer import Candle
                candle = Candle(
                    open_time=int(data.get("t", now_ms)),
                    open=float(data.get("o", 0)),
                    high=float(data.get("h", 0)),
                    low=float(data.get("l", 0)),
                    close=float(data.get("c", 0)),
                    volume=float(data.get("v", 0)),
                    close_time=int(data.get("T", now_ms + 60000)),
                )
                confirmed = bool(data.get("x", False))
                sym_bufs = self.candle_buffers.get(symbol, {})
                buf = sym_bufs.get("1m")
                if buf is not None:
                    buf.add(candle)
                    # Aggregate to higher timeframes for equity/commodity ATR
                    if confirmed:
                        _aggregate_higher_tf(sym_bufs, candle)
                    event_bus.publish(Event(
                        event_type=EventType.CANDLE_CLOSED,
                        symbol=symbol,
                        timestamp_ms=int(data.get("t", now_ms)),
                        data={
                            "count": buf.count(),
                            "close": float(data.get("c", 0)),
                            "confirmed": confirmed,
                        },
                    ))
            except Exception as e:
                logger.warning("candle_parse_error", symbol=symbol, error=str(e))

        # ── L2 Orderbook ─────────────────────────────────────────────────────────
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
                        p, q = float(item[0]), float(item[1])
                        if p > 0 and q > 0:
                            bids.append((p, q))
                    except Exception:
                        continue
                asks = []
                for item in asks_raw[:20]:
                    try:
                        p, q = float(item[0]), float(item[1])
                        if p > 0 and q > 0:
                            asks.append((p, q))
                    except Exception:
                        continue
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
                            "best_ask": asks[0][0] if asks else 0.0,
                        },
                    ))
            except Exception as e:
                logger.warning("orderbook_parse_error", symbol=symbol, error=str(e))

        # ── L4 Orderbook (full depth, per-block) ──────────────────────────────────
        elif channel == "l4Book":
            if not isinstance(data, dict):
                return
            symbol = data.get("s", "")
            if not symbol or symbol not in self.config.assets:
                return
            try:
                msg_type = msg.get("type", "update")
                bids_raw = data.get("b", [])
                asks_raw = data.get("a", [])
                event_time = int(data.get("E", now_ms))
                bids = [(float(p), float(q)) for p, q in bids_raw if float(q) > 0]
                asks = [(float(p), float(q)) for p, q in asks_raw if float(q) > 0]
                bids.sort(key=lambda x: x[0], reverse=True)
                asks.sort(key=lambda x: x[0])
                store = self.orderbook_stores.get(symbol)
                if store and (bids or asks):
                    if msg_type == "snapshot":
                        store.update(bids, asks, event_time)
                    else:
                        store.update_l4_diff(bids, asks, event_time)
                    event_bus.publish(Event(
                        event_type=EventType.ORDERBOOK_UPDATED,
                        symbol=symbol,
                        timestamp_ms=event_time,
                        data={
                            "source": "l4Book",
                            "best_bid": bids[0][0] if bids else 0.0,
                            "best_ask": asks[0][0] if asks else 0.0,
                            "imbalance": store.imbalance(depth=5),
                        },
                    ))
            except Exception as e:
                logger.warning("l4book_parse_error", symbol=symbol, error=str(e))

        # ── Market Trades ─────────────────────────────────────────────────────────
        elif channel == "marketTrade":
            trades = data if isinstance(data, list) else [data]
            for trade in trades:
                symbol = trade.get("s", "")
                if not symbol or symbol not in self.config.assets:
                    continue
                try:
                    price = float(trade.get("p", 0))
                    size = float(trade.get("q", 0))
                    is_buyer_maker = bool(trade.get("m", False))
                    side = "sell" if is_buyer_maker else "buy"
                    if price > 0 and size > 0:
                        from data.trade_flow_store import Trade
                        store = self.trade_flow_stores.get(symbol)
                        if store:
                            store.add(Trade(
                                timestamp_ms=now_ms,
                                price=price,
                                size=size,
                                side=side,
                                is_aggressor_buy=(side == "buy"),
                            ))
                            event_bus.publish(Event(
                                event_type=EventType.TRADE_FLOW_UPDATED,
                                symbol=symbol,
                                timestamp_ms=now_ms,
                                data={},
                            ))
                except Exception:
                    continue

    # ── REST helpers ─────────────────────────────────────────────────────────────

    async def fetch_historical(self) -> None:
        """
        Fetches last 55 1m candles for each asset to seed the candle buffer
        before the WS stream provides live closes.
        Only fetches for SODEX_SUPPORTED symbols.
        """
        import httpx
        rest_url = self.config.sodex_rest_perps
        endpoint = f"{rest_url}/markets"

        for symbol in self.config.assets:
            if symbol not in SODEX_SUPPORTED:
                continue
            try:
                async with httpx.AsyncClient(
                    verify=certifi.where(), timeout=10.0
                ) as client:
                    resp = await client.get(
                        f"{endpoint}/{symbol}/klines",
                        params={"interval": "1m", "limit": 55},
                    )
                    if resp.status_code != 200:
                        logger.warning("historical_fetch_failed",
                                       symbol=symbol, status=resp.status_code)
                        continue
                    candles_raw = list(reversed(resp.json().get("data", [])))
                    from data.candle_buffer import Candle
                    for row in candles_raw:
                        try:
                            buf = self.candle_buffers.get(symbol, {}).get("1m")
                            if buf is not None:
                                buf.add(Candle(
                                    open_time=int(row.get("t", 0)),
                                    open=float(row.get("o", 0)),
                                    high=float(row.get("h", 0)),
                                    low=float(row.get("l", 0)),
                                    close=float(row.get("c", 0)),
                                    volume=float(row.get("v", 0)),
                                    close_time=int(row.get("T", 0)),
                                ))
                        except Exception:
                            continue
                    buf = self.candle_buffers.get(symbol, {}).get("1m")
                    if buf is not None:
                        logger.info("sodex_historical_loaded",
                                    symbol=symbol, candles=buf.count())
            except Exception as e:
                logger.warning("sodex_historical_error",
                               symbol=symbol, error=str(e))

    async def fetch_funding_rates(self) -> dict:
        """Fetches definitive funding rates from SoDEX REST API."""
        import httpx
        rates = {}
        rest_url = self.config.sodex_rest_perps
        try:
            async with httpx.AsyncClient(
                verify=certifi.where(), timeout=10.0
            ) as client:
                resp = await client.get(f"{rest_url}/markets/mark-prices")
                if resp.status_code == 200:
                    for item in resp.json().get("data", []):
                        symbol = item.get("symbol", item.get("s", ""))
                        rate = float(item.get("fundingRate", item.get("r", 0)))
                        if symbol in self.config.assets:
                            rates[symbol] = rate
        except Exception as e:
            logger.warning("sodex_funding_fetch_error", error=str(e))
        return rates
