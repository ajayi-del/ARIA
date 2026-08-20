"""
Aster DEX public WebSocket feed — Binance-protocol streams.

Two jobs:
  1. !forceOrder@arr — all-market liquidation snapshots (1s). Second cascade
     lens alongside the Bybit liq stream: venues_confirming breadth for the
     Tier-6 liq_phase_engine (deferred item 6c made real). Schema is the
     Binance forceOrder shape — same semantics as the Bybit handler.
  2. <symbol>@markPrice@1s — mark/index/funding for Aster-tracked symbols.
     Reference price for cross-venue basis + hedge routing decisions.
  3. <symbol>@kline_1m (kline_symbols only, 2026-08-18) — execution-venue
     candles for aster-routed symbols whose external candle source is too
     slow for the interpreter's 90s staleness guard (Yahoo futures 1m lags
     ~10 min overnight). Closed bars → candle_buffers + CANDLE_CLOSED.
  4. <symbol>@depth20@100ms (ob_symbols only, 2026-08-20) — execution-venue
     L4 for aster-routed symbols. Cascade/sweep/imbalance previously read
     Bybit's book while execution hit Aster's — signal and fill now see the
     same liquidity. Bybit yields those stores in main.py. NOTE (live-probed
     2026-08-20): Aster ignores Binance partial-book semantics — every
     depthUpdate carries the FULL top-20 (20 bids + 20 asks, no qty=0
     removals, pu chains u perfectly), so each message is treated as a
     snapshot; no REST-seed/diff bridging needed.

Same listener contract as data/bybit_feed.py:
    add_liquidation_listener(cb) → cb(canonical_symbol, direction, qty, price, ts_ms)
    direction: "bearish" = longs wiped (SELL forceOrder), "bullish" = shorts wiped.

Inert unless constructed and started from main.py (aster_enabled + symbols).
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from typing import Any, Dict, List, Optional

import certifi
import httpx
import structlog
import websockets

from core.event_bus import event_bus, Event, EventType
from data.candle_buffer import Candle
from execution.aster_client import to_aster_symbol, to_canonical_symbol

logger = structlog.get_logger(__name__)

ASTER_WS_URL = "wss://fstream.asterdex.com/stream"
ASTER_REST_URL = "https://fapi.asterdex.com"


class AsterFeed:
    def __init__(self, symbols: Optional[List[str]] = None,
                 shadow_symbols: Optional[List[str]] = None,
                 kline_symbols: Optional[List[str]] = None,
                 candle_buffers: Optional[Dict] = None,
                 orderbook_stores: Optional[Dict] = None,
                 ob_symbols: Optional[List[str]] = None):
        self._symbols: List[str] = list(symbols or [])   # canonical (BTC-USD)
        # Shadow-dual symbols (2026-08-16): SoDEX-routed live; we subscribe
        # markPrice + bookTicker for venue-comparison data only. Never traded.
        self._shadow_symbols: List[str] = list(shadow_symbols or [])
        # Aster-owned candles (2026-08-18): aster-routed symbols whose other
        # candle sources are too slow for the 90s interpreter staleness guard
        # (Yahoo GC=F/CL=F lags ~10 min overnight). We own their 1m candles:
        # kline_1m stream → candle_buffers + CANDLE_CLOSED; tradfi_feed yields.
        self._kline_symbols: List[str] = list(kline_symbols or [])
        self._candle_buffers = candle_buffers if candle_buffers is not None else {}
        # Aster-owned L4 (2026-08-20): depth20@100ms → orderbook_stores for
        # aster-routed symbols so cascade/aftermath read the book we execute
        # against. Only symbols with an injected store are subscribed.
        self._ob_stores = orderbook_stores if orderbook_stores is not None else {}
        self._ob_symbols: List[str] = [s for s in (ob_symbols or [])
                                       if s in self._ob_stores]
        self._running = False
        self._liquidation_listeners: list = []
        self._last_liq_ts: float = 0.0
        self._liq_watchdog_started = False
        self._last_bar_ts: Dict[str, int] = {}
        # canonical → {"mark_price", "index_price", "funding_rate", "ts"}
        self.mark_prices: Dict[str, Dict[str, float]] = {}
        # canonical → {"bid", "ask", "bid_qty", "ask_qty", "ts"} (shadow syms)
        self.book: Dict[str, Dict[str, float]] = {}

    # ── Listeners ────────────────────────────────────────────────────────────

    def add_liquidation_listener(self, callback):
        if callback not in self._liquidation_listeners:
            self._liquidation_listeners.append(callback)

    def remove_liquidation_listener(self, callback):
        if callback in self._liquidation_listeners:
            self._liquidation_listeners.remove(callback)

    def get_mark_price(self, symbol: str) -> float:
        return float(self.mark_prices.get(symbol, {}).get("mark_price", 0.0) or 0.0)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        await self._seed_klines()
        await self._run_stream()

    async def stop(self) -> None:
        self._running = False

    async def _seed_klines(self) -> None:
        """Boot history for kline-owned symbols — the interpreter needs ~50
        candles before it can signal; waiting for 50 live 1m bars would mute
        the symbol for its first hour. Public REST, no auth. Seed failures are
        non-fatal: the stream still appends live bars from boot."""
        if not self._kline_symbols:
            return
        async with httpx.AsyncClient(timeout=10.0) as client:
            for sym in self._kline_symbols:
                buf = self._candle_buffers.get(sym, {}).get("1m")
                if buf is None:
                    continue
                for path in ("/fapi/v1/klines", "/fapi/v3/klines"):
                    try:
                        resp = await client.get(
                            f"{ASTER_REST_URL}{path}",
                            params={"symbol": to_aster_symbol(sym),
                                    "interval": "1m", "limit": 200})
                        if resp.status_code != 200:
                            continue
                        now_ms = int(time.time() * 1000)
                        added = 0
                        for k in resp.json():
                            if int(k[6]) >= now_ms:
                                continue   # in-progress bar
                            buf.add(Candle(
                                open_time=int(k[0]), open=float(k[1]),
                                high=float(k[2]), low=float(k[3]),
                                close=float(k[4]), volume=float(k[5]),
                                close_time=int(k[6]),
                            ))
                            added += 1
                        if added:
                            logger.info("aster_klines_seeded", symbol=sym,
                                        candles=buf.count(), path=path)
                            break
                    except Exception as e:
                        logger.warning("aster_kline_seed_error", symbol=sym,
                                       path=path, error=str(e)[:120])

    def _stream_url(self) -> str:
        streams = ["!forceOrder@arr"]
        for sym in self._symbols:
            streams.append(f"{to_aster_symbol(sym).lower()}@markPrice@1s")
        for sym in self._shadow_symbols:
            if sym in self._symbols:
                continue
            _a = to_aster_symbol(sym).lower()
            streams.append(f"{_a}@markPrice@1s")
            streams.append(f"{_a}@bookTicker")
        for sym in self._kline_symbols:
            streams.append(f"{to_aster_symbol(sym).lower()}@kline_1m")
        for sym in self._ob_symbols:
            streams.append(f"{to_aster_symbol(sym).lower()}@depth20@100ms")
        return f"{ASTER_WS_URL}?streams={'/'.join(streams)}"

    async def _run_stream(self) -> None:
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        backoff = 1.0
        while self._running:
            try:
                url = self._stream_url()
                logger.info("connecting_to_aster", streams=len(self._symbols) + 1)
                # Server drops connections at the 24h mark — the reconnect loop
                # handles it; ping frames arrive server-side every 5min.
                async with websockets.connect(
                    url, ssl=ssl_ctx, ping_interval=None, max_queue=1000,
                ) as ws:
                    backoff = 1.0
                    if not self._liq_watchdog_started:
                        self._liq_watchdog_started = True
                        self._last_liq_ts = time.time()
                        asyncio.create_task(self._liq_watchdog())
                    logger.info("aster_feed_connected",
                                symbols=len(self._symbols))
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        data = msg.get("data", msg)
                        if not isinstance(data, dict):
                            continue
                        etype = data.get("e")
                        if etype == "forceOrder":
                            self._handle_force_order(data)
                        elif etype == "markPriceUpdate":
                            self._handle_mark_price(data)
                        elif etype == "kline":
                            self._handle_kline(data)
                        elif etype == "depthUpdate":
                            self._handle_depth_snapshot(data)
                        elif etype is None and "b" in data and "a" in data:
                            # bookTicker carries no "e" field on the
                            # Binance-protocol streams — shape is u/s/b/B/a/A.
                            self._handle_book_ticker(data)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("aster_connection_lost", error=str(e)[:160],
                               reconnect_s=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    # ── Handlers ─────────────────────────────────────────────────────────────

    def _handle_force_order(self, data: Dict[str, Any]) -> None:
        o = data.get("o") or {}
        if not isinstance(o, dict):
            return
        self._last_liq_ts = time.time()
        try:
            symbol = to_canonical_symbol(o.get("s", ""))
            side = o.get("S", "")
            qty = float(o.get("z") or o.get("q") or 0)      # filled accumulated
            price = float(o.get("ap") or o.get("p") or 0)   # avg fill price
            ts = int(o.get("T", data.get("E", 0)) or 0)
            if not symbol or qty <= 0 or price <= 0:
                return
            # SELL forceOrder = long liquidated → bearish pressure
            # BUY  forceOrder = short liquidated → bullish pressure
            direction = "bearish" if side == "SELL" else "bullish"
            for cb in self._liquidation_listeners:
                try:
                    asyncio.create_task(cb(symbol, direction, qty, price, ts))
                except Exception:
                    pass
        except Exception as e:
            logger.warning("aster_liquidation_parse_error", error=str(e)[:120])

    def _handle_mark_price(self, data: Dict[str, Any]) -> None:
        symbol = to_canonical_symbol(data.get("s", ""))
        if not symbol:
            return
        try:
            self.mark_prices[symbol] = {
                "mark_price": float(data.get("p", 0) or 0),
                "index_price": float(data.get("i", 0) or 0),
                "funding_rate": float(data.get("r", 0) or 0),
                "ts": float(data.get("E", 0) or 0) / 1000.0,
            }
        except Exception:
            pass

    def _handle_book_ticker(self, data: Dict[str, Any]) -> None:
        symbol = to_canonical_symbol(data.get("s", ""))
        if not symbol:
            return
        try:
            _ts = float(data.get("E") or data.get("T") or 0) / 1000.0
            self.book[symbol] = {
                "bid": float(data.get("b", 0) or 0),
                "bid_qty": float(data.get("B", 0) or 0),
                "ask": float(data.get("a", 0) or 0),
                "ask_qty": float(data.get("A", 0) or 0),
                "ts": _ts if _ts > 0 else time.time(),
            }
        except Exception:
            pass

    def _handle_depth_snapshot(self, data: Dict[str, Any]) -> None:
        """depthUpdate → OrderbookStore (Aster sends the FULL top-20 per
        message — live-probed 2026-08-20: 20 bids + 20 asks, zero qty=0
        entries, pu chains u exactly; Binance partial-book semantics are not
        implemented server-side).

        We still reconcile via update_l4_diff instead of store.update() so
        queue-age tracking and cancel velocity survive the 10 Hz snapshots —
        a level persisting across pushes keeps its age stamp; a level dropping
        out of the top 20 registers as a removal (cancel/fill proxy), same
        semantics as the SoDEX L4 diff path.
        """
        symbol = to_canonical_symbol(data.get("s", ""))
        store = self._ob_stores.get(symbol) if symbol else None
        if store is None:
            return
        try:
            new_bids: Dict[float, float] = {}
            for item in data.get("b") or []:
                p, q = float(item[0]), float(item[1])
                if p > 0 and q > 0:
                    new_bids[p] = q
            new_asks: Dict[float, float] = {}
            for item in data.get("a") or []:
                p, q = float(item[0]), float(item[1])
                if p > 0 and q > 0:
                    new_asks[p] = q
            if not new_bids or not new_asks:
                return
            now_ms = int(data.get("E") or 0) or int(time.time() * 1000)
            bid_diffs = list(new_bids.items()) + [
                (p, 0.0) for p, _ in store.bids if p not in new_bids]
            ask_diffs = list(new_asks.items()) + [
                (p, 0.0) for p, _ in store.asks if p not in new_asks]
            store.update_l4_diff(bid_diffs, ask_diffs, now_ms)
            # Same event contract as bybit_feed/sodex_feed — the interpreter's
            # Tier-4 fast path (sweep/imbalance/absorption) is event-driven;
            # a store write without the event would silence those signals.
            event_bus.publish(Event(
                event_type=EventType.ORDERBOOK_UPDATED,
                symbol=symbol,
                timestamp_ms=now_ms,
                data={
                    "bids_len": len(store.bids),
                    "asks_len": len(store.asks),
                    "best_bid": max(new_bids),
                    "best_ask": min(new_asks),
                    "venue": "aster",
                },
            ))
        except Exception:
            pass

    def _handle_kline(self, data: Dict[str, Any]) -> None:
        k = data.get("k") or {}
        if not isinstance(k, dict):
            return
        symbol = to_canonical_symbol(k.get("s") or data.get("s", ""))
        buf = self._candle_buffers.get(symbol, {}).get("1m")
        if not symbol or buf is None:
            return
        try:
            buf.add(Candle(
                open_time=int(k["t"]), open=float(k["o"]), high=float(k["h"]),
                low=float(k["l"]), close=float(k["c"]),
                volume=float(k.get("v") or 0.0), close_time=int(k["T"]),
            ))
        except (KeyError, TypeError, ValueError):
            return
        # Publish only on bar CLOSE (k.x). The forming bar is still written
        # above — same contract as the Bybit feed — so the interpreter's 90s
        # staleness guard (measured from the tail's open_time) stays green.
        if not k.get("x"):
            return
        if int(k["t"]) > self._last_bar_ts.get(symbol, 0):
            self._last_bar_ts[symbol] = int(k["t"])
            event_bus.publish(Event(
                event_type=EventType.CANDLE_CLOSED,
                symbol=symbol,
                timestamp_ms=int(k["t"]),
                data={"count": buf.count(), "close": float(k["c"]),
                      "confirmed": True},
            ))

    async def _liq_watchdog(self) -> None:
        """Silent-death guard — same failure class as the 2026-05-12 Bybit
        silent stream. All-market liqs print daily even in calm tape; 4h of
        total silence means the stream is dead, not the market."""
        while self._running:
            await asyncio.sleep(1800.0)
            gap = time.time() - self._last_liq_ts
            if gap > 4 * 3600:
                logger.warning("aster_liq_stream_silent",
                               hours=round(gap / 3600.0, 1))

    def health_check(self) -> dict:
        return {
            "feed": "aster_public",
            "url": ASTER_WS_URL,
            "status": "running" if self._running else "stopped",
            "symbols": len(self._symbols),
            "last_liq_age_s": round(time.time() - self._last_liq_ts, 1)
            if self._last_liq_ts else None,
            "marks_tracked": len(self.mark_prices),
            "ob_symbols": len(self._ob_symbols),
        }
