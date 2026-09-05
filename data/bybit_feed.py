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

# Map ARIA symbols (14-coin universe) to Bybit linear perpetual symbols.
# "unknown" = no Bybit perp exists (equity synthetics only).
# OI + funding intelligence flows via tickers.{bybit_symbol} subscription.
BYBIT_SYMBOL_MAP = {
    # Tier A crypto — price discovery leaders
    "BTC-USD":       "BTCUSDT",
    "ETH-USD":       "ETHUSDT",
    "SOL-USD":       "SOLUSDT",
    "BNB-USD":       "BNBUSDT",
    # Commodity
    "XAUT-USD":      "XAUTUSDT",
    # L2 ecosystem
    "OP-USD":        "OPUSDT",
    "ARB-USD":       "ARBUSDT",
    # Alt L1
    "AVAX-USD":      "AVAXUSDT",
    "SUI-USD":       "SUIUSDT",
    "NEAR-USD":      "NEARUSDT",
    # DeFi infra
    "LINK-USD":      "LINKUSDT",
    # Meme
    "1000PEPE-USD":  "1000PEPEUSDT",
    "TRUMP-USD":     "TRUMPUSDT",
    "BASED-USD":     "BASEDUSDT",
    # High-cap alt
    "XRP-USD":       "XRPUSDT",
    "DOGE-USD":      "DOGEUSDT",
    "HBAR-USD":      "HBARUSDT",
    # Legacy L1
    "LTC-USD":       "LTCUSDT",
    # Bybit-venue seed universe (execution/venue.py partition — activated with
    # bybit_assets; subscriptions stay gated on config.assets so these are
    # inert until activation day).
    "HYPE-USD":      "HYPEUSDT",
    "ADA-USD":       "ADAUSDT",
    "UNI-USD":       "UNIUSDT",
    "ONDO-USD":      "ONDOUSDT",
    "TAO-USD":       "TAOUSDT",
    "ENA-USD":       "ENAUSDT",
    "KAITO-USD":     "KAITOUSDT",
    "WIF-USD":       "WIFUSDT",
    "ZEC-USD":       "ZECUSDT",
    "VIRTUAL-USD":   "VIRTUALUSDT",
    "AAVE-USD":      "AAVEUSDT",
    "1000BONK-USD":  "1000BONKUSDT",
    "SEI-USD":       "SEIUSDT",
    "PENGU-USD":     "PENGUUSDT",
    "INJ-USD":       "INJUSDT",
    "TIA-USD":       "TIAUSDT",
    "APT-USD":       "APTUSDT",
    # Aster-expansion seed universe (2026-08-15) — execution routes to Aster,
    # candle/ticker/liq data rides this Bybit public map. Inert until the
    # symbols join config.assets on Aster activation day.
    "TRX-USD":       "TRXUSDT",
    "BCH-USD":       "BCHUSDT",
    "XLM-USD":       "XLMUSDT",
    "FARTCOIN-USD":  "FARTCOINUSDT",
    "VELVET-USD":    "VELVETUSDT",
    "AKE-USD":       "AKEUSDT",
    "CYS-USD":       "CYSUSDT",
    "ASTER-USD":     "ASTERUSDT",
    "ACE-USD":       "ACEUSDT",
    "MUBARAK-USD":   "MUBARAKUSDT",
    "DOS-USD":       "DOSUSDT",
    "SNXX-USD":      "SNXXUSDT",
    "HEMI-USD":      "HEMIUSDT",
    "AIO-USD":       "AIOUSDT",
    "ARIA-USD":      "ARIAUSDT",
    # 45-symbol expansion (2026-08-21) — dual-verified Aster TRADING +
    # ≥$390K Aster 24h vol + this Bybit perp path for candles/OI/funding.
    "WLD-USD":       "WLDUSDT",
    "BOME-USD":      "BOMEUSDT",
    "ICP-USD":       "ICPUSDT",
    "XMR-USD":       "XMRUSDT",
    "ORDI-USD":      "ORDIUSDT",
    "WLFI-USD":      "WLFIUSDT",
    "LIT-USD":       "LITUSDT",
    "PAXG-USD":      "PAXGUSDT",
    # 2026-09-05 Saturday-mover expansion (operator + CEO staged endorsement;
    # candle/OI/funding path for FLOCK/FF execution + TRIA/NOM/ZEN/ICX incubation)
    "FLOCK-USD":     "FLOCKUSDT",
    "FF-USD":        "FFUSDT",
    "TRIA-USD":      "TRIAUSDT",
    "NOM-USD":       "NOMUSDT",
    "ZEN-USD":       "ZENUSDT",
    "ICX-USD":       "ICXUSDT",
    # SoDEX-only synthetic instruments — no Bybit perp; OI/funding not available
    # These are handled by SoDEXFeed only. Omitted here so _build_topics() skips them.
}

# Assets for which Bybit provides OI + funding data.
# Only symbols with a known Bybit mapping will be subscribed (no "unknown" fallback needed).
SUPPORTED_ASSETS = [
    "BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD",
    "XAUT-USD",
    "OP-USD", "ARB-USD",
    "AVAX-USD", "SUI-USD", "NEAR-USD",
    "LINK-USD",
    "1000PEPE-USD",
    "XRP-USD",
    "DOGE-USD",
    "HBAR-USD",
    "LTC-USD",
    "TRUMP-USD",
    "BASED-USD",
    # Bybit-venue seed universe
    "HYPE-USD", "ADA-USD", "UNI-USD", "ONDO-USD",
    "TAO-USD", "ENA-USD", "KAITO-USD", "WIF-USD",
    "ZEC-USD", "VIRTUAL-USD", "AAVE-USD", "1000BONK-USD",
    "SEI-USD", "PENGU-USD",
    "INJ-USD", "TIA-USD", "APT-USD",
    # Aster-expansion seed universe (inert until config.assets activation)
    "TRX-USD", "BCH-USD", "XLM-USD", "FARTCOIN-USD",
    "VELVET-USD", "AKE-USD", "CYS-USD", "ASTER-USD",
    "ACE-USD", "MUBARAK-USD", "DOS-USD", "SNXX-USD",
    "HEMI-USD", "AIO-USD", "ARIA-USD",
    # 45-symbol expansion (2026-08-21)
    "WLD-USD", "BOME-USD", "ICP-USD", "XMR-USD",
    "ORDI-USD", "WLFI-USD", "LIT-USD", "PAXG-USD",
    # 2026-09-05 Saturday-mover expansion
    "FLOCK-USD", "FF-USD", "TRIA-USD", "NOM-USD",
    "ZEN-USD", "ICX-USD",
]

BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"

# Deep-venue 24h quote turnover per ARIA symbol, updated from ticker WS pushes.
# SoDEX's own 24h turnover measures a few-thousand-user book (NEAR: $14k on
# SoDEX vs $48M on Bybit) — the turnover gate uses this as the liquidity
# backstop for crypto, the same way tradfi_health() backstops equities.
_turnover_24h: dict[str, float] = {}


def bybit_turnover_24h(symbol: str) -> float | None:
    """Latest Bybit 24h quote turnover (USD) for an ARIA symbol, None if unseen."""
    return _turnover_24h.get(symbol)

class BybitFeed:
    def __init__(self,
        config,
        mark_price_stores: dict,
        orderbook_stores: dict,
        candle_buffers: dict,
        trade_flow_stores: dict,
        bybit_ticker_stores: dict = None,
        funding_history=None):

        self.config = config
        self.mark_price_stores = mark_price_stores
        self.orderbook_stores = orderbook_stores
        self.candle_buffers = candle_buffers
        self.trade_flow_stores = trade_flow_stores
        self.bybit_ticker_stores = bybit_ticker_stores  # OI + funding intelligence
        self._funding_history = funding_history          # FundingHistory for cross-venue rates
        self._running = False
        self._task: asyncio.Task | None = None
        self._msg_count = 0
        # Liquidation listeners — callbacks receive (symbol, direction, size, price, timestamp)
        self._liquidation_listeners: list = []
        self._last_liq_ts: float = 0.0      # last liquidation push (watchdog)
        self._liq_watchdog_started: bool = False
        # Subscription state — mirrors SoDEXFeed pattern for ensure_subscribed().
        self._subscribed: set[str] = set()
        # Per-symbol last-message timestamps — coverage audit (dark-symbol detector)
        self._last_msg_ts: dict[str, float] = {}
        self._coverage_task: asyncio.Task | None = None
        # Layer 2 — last-candle cache (stale candle access during/after outage)
        # Populated on every candle received; survives reconnect cycles.
        # Use get_stale_candle(symbol, timeframe) to read the last known candle
        # during an outage or immediately after reconnect before buffers repopulate.
        self._last_candle_cache: dict = {}   # symbol → {"1m": Candle, "4h": Candle}
        self._reconnect_attempts: int = 0    # cumulative reconnect counter for log context

    async def start(self) -> None:
        """Starts Bybit WebSocket connection as a background task."""
        self._running = True
        logger.info("starting_bybit_feed")
        self._task = asyncio.create_task(self._run_stream())
        self._coverage_task = asyncio.create_task(self._coverage_loop())

    async def stop(self) -> None:
        """Stops the Bybit stream task."""
        self._running = False
        logger.info("stopping_bybit_feed")
        if self._coverage_task:
            self._coverage_task.cancel()
            self._coverage_task = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def get_stale_candle(self, symbol: str, timeframe: str = "1m"):
        """Return the last received candle for symbol+timeframe, or None.

        Safe to call at any time — survives reconnect cycles.
        Useful in signal_generator or coherence engine to fill in gaps
        when the buffer has been empty since reconnect.

        timeframe: "1m" or "4h"
        """
        return self._last_candle_cache.get(symbol, {}).get(timeframe)

    async def _coverage_loop(self) -> None:
        """Every 30 min: warn about symbols we believe are subscribed but from
        which no message has arrived in 30 min. The 2026-08-19 audit found 9
        aster-sleeve symbols dark for days while every health signal looked
        green — an unobserved degree of freedom (Ashby variety budget)."""
        while self._running:
            await asyncio.sleep(1800)
            try:
                now = time.time()
                dark = [s for s in sorted(self._subscribed)
                        if now - self._last_msg_ts.get(s, 0.0) > 1800]
                if dark:
                    logger.warning("bybit_coverage_dark",
                                   symbols=dark, count=len(dark),
                                   note="subscribed but silent >30min — check bybit_subscribe_rejected")
            except Exception:
                pass

    async def ensure_subscribed(self, symbol: str) -> None:
        """Hot-path guard — same contract as SoDEXFeed.ensure_subscribed."""
        if symbol in self._subscribed:
            return
        deadline = time.monotonic() + 2.0
        while symbol not in self._subscribed and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    def add_liquidation_listener(self, callback):
        """Register a callback for liquidation events. Thread-safe append."""
        if callback not in self._liquidation_listeners:
            self._liquidation_listeners.append(callback)

    def remove_liquidation_listener(self, callback):
        """Deregister a callback."""
        if callback in self._liquidation_listeners:
            self._liquidation_listeners.remove(callback)

    def _build_topics(self, symbol: str) -> list[str]:
        """Return the Bybit topic strings for a given ARIA symbol."""
        b = BYBIT_SYMBOL_MAP.get(symbol)
        if not b or b == "unknown":
            return []
        topics = []
        if self.mark_price_stores or self.bybit_ticker_stores is not None:
            topics.append(f"tickers.{b}")
        topics.append(f"kline.1.{b}")
        topics.append(f"kline.5.{b}")      # 5m ATR source (crypto strategy class)
        topics.append(f"kline.240.{b}")    # 4H HTF trend filter
        topics.append(f"publicTrade.{b}")
        topics.append(f"orderbook.50.{b}")
        # Liquidation topics are subscribed via _liq_topics() in a SEPARATE
        # frame — mixing liquidation.* with other topics in one subscribe
        # batch made Bybit v5 silently drop ALL data (2026-05-12 incident).
        return topics

    def _liq_topics(self, symbols) -> list[str]:
        """allLiquidation.{symbol} topics — must be sent in their own subscribe
        frame (see _build_topics note). Re-enabled 2026-07-30 for Phase 3:
        deep-venue liquidations feed Tier-6 liq_phase_engine as the leading
        cascade indicator for both venues. 2026-08-20: renamed from
        liquidation.{symbol} — Bybit sunset that topic (subscribes returned
        'handler not found', unlogged, stream dark for days)."""
        out = []
        for s in symbols:
            b = BYBIT_SYMBOL_MAP.get(s)
            if b and b != "unknown":
                out.append(f"allLiquidation.{b}")
        return out

    async def _run_stream(self) -> None:
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        backoff = 1.0

        while self._running:
            try:
                logger.info("connecting_to_bybit", url=BYBIT_WS_URL)
                async with websockets.connect(
                    BYBIT_WS_URL,
                    ssl=ssl_ctx,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    backoff = 1.0
                    self._subscribed.clear()

                    # Step 1 — subscribe core assets immediately
                    core_topics = []
                    for sym in self.config.core_assets:
                        if sym not in self.config.assets:
                            continue
                        core_topics.extend(self._build_topics(sym))
                    if core_topics:
                        await ws.send(json.dumps({
                            "op": "subscribe", "args": core_topics
                        }))
                        self._subscribed.update(
                            s for s in self.config.core_assets
                            if BYBIT_SYMBOL_MAP.get(s, "unknown") != "unknown"
                        )
                        _now = time.time()
                        for s in self._subscribed:
                            self._last_msg_ts.setdefault(s, _now)
                        _core_li = self._liq_topics(
                            s for s in self.config.core_assets
                            if s in self.config.assets)
                        if _core_li:
                            await ws.send(json.dumps({
                                "op": "subscribe", "args": _core_li
                            }))
                    logger.info("bybit_core_subscribed",
                                symbols=[s for s in self.config.core_assets
                                         if BYBIT_SYMBOL_MAP.get(s, "unknown") != "unknown"])

                    # Step 2 — stagger remaining watchlist (3/batch, 2 s apart)
                    asyncio.create_task(self._stagger_remaining(ws))

                    # Silent-death watchdog: the 2026-05-12 failure mode was a
                    # SILENT stream (no errors, zero data). 30+ symbols push
                    # liquidations daily even in calm tape — 4h of total silence
                    # means the stream is dead, not the market.
                    if not self._liq_watchdog_started:
                        self._liq_watchdog_started = True
                        self._last_liq_ts = time.time()
                        async def _liq_watchdog():
                            while self._running:
                                await asyncio.sleep(1800)
                                silent_h = (time.time() - self._last_liq_ts) / 3600
                                if silent_h >= 4.0:
                                    logger.warning("bybit_liq_stream_silent",
                                                   hours=round(silent_h, 1))
                        asyncio.create_task(_liq_watchdog())

                    # Step 3 — application-level keepalive (15s) to prevent
                    # Bybit 1011 keepalive timeout errors. Bybit requires an
                    # explicit {"op": "ping"} message — protocol-level WS pings
                    # (ping_interval=20) are not sufficient on their own.
                    async def _bybit_keepalive():
                        while self._running:
                            await asyncio.sleep(15)
                            try:
                                await ws.send(json.dumps({"op": "ping"}))
                            except Exception:
                                break
                    asyncio.create_task(_bybit_keepalive())

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
                self._subscribed.clear()
                self._reconnect_attempts += 1
                logger.warning("bybit_connection_lost",
                               error=str(e),
                               retry_in=backoff,
                               attempt=self._reconnect_attempts,
                               cached_symbols=len(self._last_candle_cache))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _stagger_remaining(self, ws) -> None:
        """Subscribe non-core assets 3 at a time, 2 s apart."""
        remaining = [
            s for s in SUPPORTED_ASSETS
            if s in self.config.assets and s not in self.config.core_assets
            and BYBIT_SYMBOL_MAP.get(s, "unknown") != "unknown"
        ]
        batch_size = 3
        for i in range(0, len(remaining), batch_size):
            await asyncio.sleep(2.0)
            if not self._running:
                return
            batch = remaining[i:i + batch_size]
            topics = []
            for sym in batch:
                topics.extend(self._build_topics(sym))
            if not topics:
                continue
            try:
                await ws.send(json.dumps({"op": "subscribe", "args": topics}))
                _li = self._liq_topics(batch)
                if _li:
                    await ws.send(json.dumps({"op": "subscribe", "args": _li}))
                self._subscribed.update(batch)
                _now = time.time()
                for s in batch:
                    self._last_msg_ts.setdefault(s, _now)
                logger.info("bybit_watchlist_subscribed",
                            batch=batch, total=len(self._subscribed))
            except Exception as e:
                logger.warning("bybit_stagger_failed",
                               batch=batch, error=str(e))

    async def _handle(self, msg: dict) -> None:
        self._msg_count += 1
        topic = msg.get("topic", "")
        data = msg.get("data", {})
        now_ms = int(time.time() * 1000)

        # Op responses (subscribe acks/rejections) carry no topic. Log them —
        # unlogged rejections were why the sunset liquidation.* topic and any
        # silently-dropped watchlist topics stayed invisible (2026-08-19 audit).
        if not topic:
            if msg.get("op") == "subscribe":
                if msg.get("success"):
                    logger.info("bybit_subscribe_ack", conn_id=msg.get("conn_id"))
                else:
                    logger.warning("bybit_subscribe_rejected",
                                   ret_msg=str(msg.get("ret_msg"))[:200])
            return

        # Find which ARIA symbol this is
        symbol = None
        for aria_sym, bybit_sym in BYBIT_SYMBOL_MAP.items():
            if bybit_sym != "unknown" and bybit_sym in topic:
                symbol = aria_sym
                break

        if not symbol or symbol not in self.config.assets:
            return
        self._last_msg_ts[symbol] = time.time()

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
                    funding_rate_raw = float(data.get("fundingRate", 0) or 0)
                    if self.bybit_ticker_stores is not None and symbol in self.bybit_ticker_stores:
                        prev = self.bybit_ticker_stores[symbol]
                        prev_oi = prev.get("open_interest", 0.0)
                        prev_mp = prev.get("mark_price", 0.0)
                        self.bybit_ticker_stores[symbol] = {
                            "funding_rate": funding_rate_raw,
                            "open_interest": float(data.get("openInterest", 0) or 0),
                            "prev_open_interest": prev_oi,
                            "prev_mark_price": prev_mp if prev_mp > 0 else float(mark),
                            "mark_price": float(mark),
                        }
                    # Delta pushes omit unchanged fields — only overwrite when present
                    _t24 = data.get("turnover24h")
                    if _t24:
                        _turnover_24h[symbol] = float(_t24)
                    # Feed Bybit funding rate to FundingHistory for cross-venue Tier 7 signal
                    if self._funding_history is not None and funding_rate_raw != 0:
                        try:
                            self._funding_history.add_bybit_rate(symbol, funding_rate_raw)
                        except Exception:
                            pass

        # 2. Kline (candle) — 1m, 5m, 4H
        elif topic.startswith("kline."):
            # Determine timeframe from topic: "kline.1.BTCUSDT" → "1m", "kline.5.BTCUSDT" → "5m"
            parts = topic.split(".")
            tf_raw = parts[1] if len(parts) > 1 else "1"
            if tf_raw == "240":
                buf_key = "4h"
            elif tf_raw == "5":
                buf_key = "5m"
            else:
                buf_key = "1m"

            if isinstance(data, list):
                for k in data:
                    try:
                        candle = Candle(
                            open_time=int(k["start"]),
                            open=float(k["open"]),
                            high=float(k["high"]),
                            low=float(k["low"]),
                            close=float(k["close"]),
                            volume=float(k["volume"]),
                            close_time=int(k["end"])
                        )

                        buf = self.candle_buffers.get(symbol, {}).get(buf_key)
                        if buf is not None:
                            buf.add(candle)
                            count = buf.count()
                        # Always cache last candle per symbol+timeframe — survives reconnects
                        self._last_candle_cache.setdefault(symbol, {})[buf_key] = candle

                        # Only publish CANDLE_CLOSED for 1m candles — 5m and 4H are passive ATR/trend buffers
                        if buf_key == "1m":
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

        # 5. Liquidation — predictive lead indicator (allLiquidation topic, 2026-08-20)
        elif topic.startswith("allLiquidation."):
            if isinstance(data, list) and data:
                self._last_liq_ts = time.time()
            if isinstance(data, list):
                for liq in data:
                    try:
                        _side = liq.get("S", "")  # "Buy" = short liquidated (bullish pressure)
                        _qty = float(liq.get("v", 0) or 0)
                        _price = float(liq.get("p", 0) or 0)
                        _ts = int(liq.get("T", now_ms))
                        if _qty <= 0 or _price <= 0:
                            continue
                        # Direction: Buy liquidation = shorts wiped → bullish pressure
                        #            Sell liquidation = longs wiped → bearish pressure
                        _direction = "bullish" if _side == "Buy" else "bearish"
                        for cb in self._liquidation_listeners:
                            try:
                                asyncio.create_task(cb(symbol, _direction, _qty, _price, _ts))
                            except Exception:
                                pass
                    except Exception as e:
                        logger.warning("bybit_liquidation_parse_error", symbol=symbol, error=str(e))

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
        """Fetches last 55 1m candles + 50 4H candles per asset to seed both buffers."""
        import httpx
        import certifi
        from data.candle_buffer import Candle

        BYBIT_REST = "https://api.bybit.com/v5/market/kline"

        for symbol in SUPPORTED_ASSETS:
            if symbol not in self.config.assets:
                continue
            bybit_sym = BYBIT_SYMBOL_MAP.get(symbol)
            if not bybit_sym or bybit_sym == "unknown":
                continue

            # Bybit returns newest-first — reverse to get chronological order
            async def _fetch(interval: str, limit: int, buf_key: str, close_offset_ms: int):
                try:
                    async with httpx.AsyncClient(verify=certifi.where()) as http:
                        resp = await asyncio.wait_for(
                            http.get(BYBIT_REST, params={
                                "category": "linear",
                                "symbol": bybit_sym,
                                "interval": interval,
                                "limit": limit
                            }),
                            timeout=10.0
                        )
                    rows = resp.json().get("result", {}).get("list", [])
                    buf = self.candle_buffers.get(symbol, {}).get(buf_key)
                    if buf is not None:
                        for row in reversed(rows):
                            buf.add(Candle(
                                open_time=int(row[0]),
                                open=float(row[1]),
                                high=float(row[2]),
                                low=float(row[3]),
                                close=float(row[4]),
                                volume=float(row[5]),
                                close_time=int(row[0]) + close_offset_ms
                            ))
                        logger.info("historical_loaded", symbol=symbol,
                                    interval=buf_key, candles=buf.count())
                except Exception as e:
                    logger.warning("historical_fetch_failed", symbol=symbol,
                                   interval=buf_key, error=str(e))

            await _fetch("1",   55, "1m",  60_000)       # 55 × 1m  = 55 min history
            await _fetch("5",   55, "5m",  300_000)      # 55 × 5m  = ~4.5h history (crypto ATR source)
            await _fetch("240", 50, "4h",  14_400_000)   # 50 × 4h  = HTF trend

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

    async def ensure_subscribed(self, symbol: str) -> None:
        """
        Delegates to both legs. Fast path (set lookup) if both already have the
        symbol; otherwise waits for both to confirm subscription.
        """
        await asyncio.gather(
            self._intel.ensure_subscribed(symbol),
            self._marks.ensure_subscribed(symbol),
        )

    async def fetch_historical(self) -> None:
        """Bybit REST seeds mapped crypto; SoDEX REST seeds SoDEX-only symbols.

        Equities/commodities have no Bybit klines — without the SoDEX leg they
        warm up from live WS only (~1 candle/min off-hours) and starve the
        interpreter's 50-candle minimum for hours after every off-hours boot.
        Observed 2026-07-28: all equity symbols stuck at count=30, zero stock
        trades. Legs are isolated — one failing must not block the other.
        """
        results = await asyncio.gather(
            self._intel.fetch_historical(),
            self._marks.fetch_historical(),
            return_exceptions=True,
        )
        for leg, res in zip(("bybit", "sodex"), results):
            if isinstance(res, Exception):
                logger.warning("hybrid_historical_leg_failed", leg=leg, error=str(res))

    async def fetch_funding_rates(self) -> dict:
        """Funding rates are SoDEX-native only (we arb SoDEX funding, not Bybit)."""
        return await self._marks.fetch_funding_rates()

    def health_check(self) -> dict:
        hc = self._intel.health_check()
        hc["sodex_marks_connected"] = self._marks._running
        hc["architecture"] = "bybit_intel+sodex_marks"
        return hc
