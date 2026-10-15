"""
Arcus public WebSocket feed — ONE multiplexed socket (wss://api.arcus.xyz/v1/ws).

Wire facts (docs.arcus.xyz/api-reference/market-data/*.md, fetched 2026-10-15):
  - Envelope in:  {"type":"subscribe","channel":<name>,"id":<marketDisplayName>}
                  (global channels omit "id"). Ack = {"type":"subscribed",
                  "contents":<snapshot>}. Data = {"type":"channel_data",
                  "channel":..., "id":..., "contents":...}.
  - bbo (per market): bestBid/bestAsk {price,size}|null, timestamp µs,
    lastSequenceId. The mark-price write-through source (Arcus has no
    markPrice stream — mid of BBO is the top-of-book mark).
  - trades (per market): contents = ARRAY of fills sharing takerOrderId,
    price/size strings, timestamp µs. NO side field — taker direction is
    inferred vs the last BBO. Fires the signal-bus interrupt (the scalper's
    240s sleep wakes on real flow instead of polling blind).
  - predictedFunding (per market): {market, rate1h} — no timestamp.
  - marketAttributes (GLOBAL): {isSnapshot, entries[]} — every entry is the
    market's COMPLETE attribute block (fields null, never omitted) → replace
    by marketId wholesale. boundEvent ∈ exitRth|enterRth|
    expansionZoneEntered|expansionZoneExited|tradingBoundExpanded, boundSide
    ∈ upper|lower|null. This is the band clock's (#36) only data plane.
  - l2OrderbookUpdates (ob_symbols): subscribed.contents = full snapshot
    (bids/asks [price,size] pairs + lastSequenceId); channel_data.contents =
    DIFFS, size "0" = level removed, last-write-wins per price. Mid-stream
    sequence gap → resubscribe (the boundary gap right after the snapshot is
    expected and ignored).

Same listener contract family as aster_feed.py. Inert unless constructed and
started (arcus_feed_enabled kill switch lives at the call site in main.py).
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from typing import Any, Dict, List, Optional

import certifi
import structlog
import websockets

from core.event_bus import event_bus, Event, EventType

logger = structlog.get_logger(__name__)

ARCUS_WS_URL = "wss://api.arcus.xyz/v1/ws"


class ArcusFeed:
    def __init__(self, symbols: Optional[List[str]] = None,
                 mark_price_stores: Optional[Dict] = None,
                 orderbook_stores: Optional[Dict] = None,
                 ob_symbols: Optional[List[str]] = None,
                 signal_event: Optional[asyncio.Event] = None):
        self._symbols: List[str] = list(symbols or [])   # canonical (BTC-USD)
        # Mark write-through (Fix C doctrine): an arcus-routed symbol with no
        # store writer is silently dead to the signal plane (MUBARAK class).
        self._mark_stores = mark_price_stores if mark_price_stores is not None else {}
        # Execution-venue L4 for arcus-routed symbols (cascade/sweep/imbalance
        # must read the book ARIA executes against, same doctrine as Aster L4).
        self._ob_stores = orderbook_stores if orderbook_stores is not None else {}
        self._ob_symbols: List[str] = [s for s in (ob_symbols or [])
                                       if s in self._ob_stores]
        # Signal-bus interrupt (missed insight #1): the Arcus scalper parks on
        # this event instead of a blind 240s sleep; real taker flow wakes it.
        self._signal_event = signal_event
        self._running = False
        self._ws = None
        self._trade_listeners: list = []
        self._band_listeners: list = []
        self._last_msg_ts: float = 0.0
        self._watchdog_started = False
        # canonical → {"bid","ask","bid_qty","ask_qty","ts"}
        self.book: Dict[str, Dict[str, float]] = {}
        # canonical → predicted 1h funding rate (decimal)
        self.predicted_funding: Dict[str, float] = {}
        # marketId → complete attribute block (wholesale replace per docs)
        self.market_attrs: Dict[int, Dict[str, Any]] = {}
        self._market_names: Dict[int, str] = {}
        # L4 sequence state for gap detection. _ob_first_delta tracks the
        # documented boundary case: the FIRST delta after a snapshot may jump
        # a few sequences (expected); gaps are only corrupt mid-stream.
        self._ob_seq: Dict[str, int] = {}
        self._ob_seeded: Dict[str, bool] = {}
        self._ob_first_delta: Dict[str, bool] = {}

    # ── Listeners ────────────────────────────────────────────────────────────

    def add_trade_listener(self, callback):
        """cb(symbol, price, size, ts_us, taker_buy) — taker_buy None when the
        BBO is too stale to infer direction."""
        if callback not in self._trade_listeners:
            self._trade_listeners.append(callback)

    def add_band_event_listener(self, callback):
        """cb(entry, is_snapshot) — raw marketAttributes entry. The band clock
        (#36) owns interpretation; the feed only transports."""
        if callback not in self._band_listeners:
            self._band_listeners.append(callback)

    def get_mark_price(self, symbol: str) -> float:
        b = self.book.get(symbol) or {}
        bid, ask = b.get("bid", 0.0), b.get("ask", 0.0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        return 0.0

    def get_band_state(self, symbol: str) -> Optional[Dict[str, Any]]:
        for mid, name in self._market_names.items():
            if name == symbol:
                return self.market_attrs.get(mid)
        return None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        await self._run_stream()

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    def _subscribe_frames(self) -> List[dict]:
        frames: List[dict] = [{"type": "subscribe",
                               "channel": "marketAttributes"}]
        for sym in self._symbols:
            frames.append({"type": "subscribe", "channel": "bbo", "id": sym})
            frames.append({"type": "subscribe", "channel": "trades",
                           "id": sym})
            frames.append({"type": "subscribe",
                           "channel": "predictedFunding", "id": sym})
        for sym in self._ob_symbols:
            if sym not in self._symbols:
                frames.append({"type": "subscribe", "channel": "bbo",
                               "id": sym})
            frames.append({"type": "subscribe",
                           "channel": "l2OrderbookUpdates", "id": sym})
        return frames

    async def _run_stream(self) -> None:
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    ARCUS_WS_URL, ssl=ssl_ctx, max_queue=1000,
                ) as ws:
                    self._ws = ws
                    backoff = 1.0
                    self._ob_seeded = {s: False for s in self._ob_symbols}
                    for frame in self._subscribe_frames():
                        await ws.send(json.dumps(frame))
                    if not self._watchdog_started:
                        self._watchdog_started = True
                        self._last_msg_ts = time.time()
                        asyncio.create_task(self._silence_watchdog())
                    logger.info("arcus_feed_connected",
                                symbols=len(self._symbols),
                                ob_symbols=len(self._ob_symbols))
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        if not isinstance(msg, dict):
                            continue
                        self._last_msg_ts = time.time()
                        mtype = msg.get("type")
                        channel = msg.get("channel")
                        contents = msg.get("contents")
                        if mtype == "subscribed":
                            if channel == "l2OrderbookUpdates":
                                self._handle_ob_snapshot(msg.get("id", ""),
                                                         contents)
                            elif channel == "marketAttributes":
                                self._handle_market_attrs(contents, True)
                            elif channel == "predictedFunding":
                                self._handle_predicted_funding(contents)
                        elif mtype == "channel_data":
                            if channel == "bbo":
                                self._handle_bbo(msg.get("id", ""), contents)
                            elif channel == "trades":
                                self._handle_trades(msg.get("id", ""),
                                                    contents)
                            elif channel == "predictedFunding":
                                self._handle_predicted_funding(contents)
                            elif channel == "marketAttributes":
                                self._handle_market_attrs(contents, False)
                            elif channel == "l2OrderbookUpdates":
                                self._handle_ob_delta(msg.get("id", ""),
                                                      contents)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("arcus_connection_lost", error=str(e)[:160],
                               reconnect_s=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            finally:
                self._ws = None

    # ── Handlers ─────────────────────────────────────────────────────────────

    def _handle_bbo(self, symbol: str, c: Any) -> None:
        if not symbol or not isinstance(c, dict):
            return
        try:
            bb, ba = c.get("bestBid"), c.get("bestAsk")
            bid = float(bb["price"]) if isinstance(bb, dict) else 0.0
            ask = float(ba["price"]) if isinstance(ba, dict) else 0.0
            if bid <= 0 or ask <= 0:
                return
            ts_us = int(c.get("timestamp") or 0)
            self.book[symbol] = {
                "bid": bid, "ask": ask,
                "bid_qty": float(bb.get("size") or 0),
                "ask_qty": float(ba.get("size") or 0),
                "ts": ts_us / 1_000_000.0 if ts_us else time.time(),
            }
            mid = (bid + ask) / 2.0
            st = self._mark_stores.get(symbol)
            if st is not None:
                st.update(mid, mid, (ts_us // 1000) or int(time.time() * 1000))
        except Exception:
            pass

    def _handle_trades(self, symbol: str, c: Any) -> None:
        if not symbol or not isinstance(c, list):
            return
        # Real taker flow = the interrupt the scalper sleeps on. Setting an
        # already-set Event is O(1); no throttle needed.
        if self._signal_event is not None and c:
            try:
                self._signal_event.set()
            except Exception:
                pass
        b = self.book.get(symbol) or {}
        bid, ask = b.get("bid", 0.0), b.get("ask", 0.0)
        book_fresh = bool(b) and (time.time() - b.get("ts", 0.0)) < 30.0
        for fill in c:
            if not isinstance(fill, dict):
                continue
            try:
                price = float(fill.get("price") or 0)
                size = float(fill.get("size") or 0)
                ts_us = int(fill.get("timestamp") or 0)
            except (TypeError, ValueError):
                continue
            if price <= 0 or size <= 0:
                continue
            taker_buy: Optional[bool] = None
            if book_fresh and bid > 0 and ask > 0:
                if price >= ask:
                    taker_buy = True
                elif price <= bid:
                    taker_buy = False
            for cb in self._trade_listeners:
                try:
                    cb(symbol, price, size, ts_us, taker_buy)
                except Exception as e:
                    logger.warning("arcus_trade_listener_error",
                                   error=str(e)[:120])

    def _handle_predicted_funding(self, c: Any) -> None:
        if not isinstance(c, dict):
            return
        try:
            market = c.get("market")
            rate = float(c.get("rate1h"))
            if market:
                self.predicted_funding[market] = rate
        except (TypeError, ValueError):
            pass

    def _handle_market_attrs(self, c: Any, is_snapshot: bool) -> None:
        if not isinstance(c, dict):
            return
        entries = c.get("entries")
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            mid = entry.get("marketId")
            if not isinstance(mid, int):
                continue
            # Docs: fields are null, never omitted — replace wholesale.
            self.market_attrs[mid] = entry
            name = entry.get("marketDisplayName")
            if name:
                self._market_names[mid] = name
            for cb in self._band_listeners:
                try:
                    cb(entry, is_snapshot)
                except Exception as e:
                    logger.warning("arcus_band_listener_error",
                                   error=str(e)[:120])

    def _handle_ob_snapshot(self, symbol: str, c: Any) -> None:
        store = self._ob_stores.get(symbol)
        if store is None or not isinstance(c, dict):
            return
        try:
            bids = [(float(p), float(q)) for p, q in (c.get("bids") or [])
                    if float(q) > 0]
            asks = [(float(p), float(q)) for p, q in (c.get("asks") or [])
                    if float(q) > 0]
            seq = int(c.get("lastSequenceId") or 0)
            self._ob_seq[symbol] = seq
            self._ob_seeded[symbol] = True
            self._ob_first_delta[symbol] = True
            now_ms = (int(c.get("timestamp") or 0) // 1000) or \
                int(time.time() * 1000)
            store.update_l4_diff(bids, asks, now_ms)
        except Exception as e:
            logger.warning("arcus_ob_snapshot_error", symbol=symbol,
                           error=str(e)[:120])

    def _handle_ob_delta(self, symbol: str, c: Any) -> None:
        store = self._ob_stores.get(symbol)
        if store is None or not isinstance(c, dict):
            return
        if not self._ob_seeded.get(symbol):
            return   # delta before snapshot — wait for resubscribe snapshot
        try:
            seq = int(c.get("lastSequenceId") or 0)
            prev = self._ob_seq.get(symbol, 0)
            if seq <= prev:
                return
            # Docs: the first delta may jump a few sequences past the
            # snapshot — expected boundary gap. Contiguous after that; a
            # mid-stream gap means the book is corrupt → resubscribe.
            first = self._ob_first_delta.get(symbol, False)
            if not first and prev > 0 and seq != prev + 1:
                logger.warning("arcus_ob_seq_gap", symbol=symbol, prev=prev,
                               seq=seq)
                self._ob_seeded[symbol] = False
                self._resubscribe_ob(symbol)
                return
            self._ob_first_delta[symbol] = False
            self._ob_seq[symbol] = seq
            bid_diffs = [(float(p), float(q))
                         for p, q in (c.get("bids") or [])]
            ask_diffs = [(float(p), float(q))
                         for p, q in (c.get("asks") or [])]
            if not bid_diffs and not ask_diffs:
                return
            now_ms = int(time.time() * 1000)
            store.update_l4_diff(bid_diffs, ask_diffs, now_ms)
            bids = [p for p, q in bid_diffs if q > 0]
            asks = [p for p, q in ask_diffs if q > 0]
            event_bus.publish(Event(
                event_type=EventType.ORDERBOOK_UPDATED,
                symbol=symbol,
                timestamp_ms=now_ms,
                data={"bids_len": len(store.bids),
                      "asks_len": len(store.asks),
                      "best_bid": max(store.bids)[0] if store.bids else 0.0,
                      "best_ask": min(store.asks)[0] if store.asks else 0.0,
                      "venue": "arcus"},
            ))
        except Exception:
            pass

    def _resubscribe_ob(self, symbol: str) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            asyncio.get_event_loop().create_task(ws.send(json.dumps({
                "type": "subscribe", "channel": "l2OrderbookUpdates",
                "id": symbol})))
        except Exception:
            pass

    async def _silence_watchdog(self) -> None:
        """Silent-death guard (2026-05-12 Bybit class): BBO streams even in
        quiet tape; 30 min of total socket silence = dead connection, not a
        dead market."""
        while self._running:
            await asyncio.sleep(600.0)
            gap = time.time() - self._last_msg_ts
            if self._last_msg_ts and gap > 1800:
                logger.warning("arcus_stream_silent",
                               minutes=round(gap / 60.0, 1))

    def health_check(self) -> dict:
        return {
            "feed": "arcus_public",
            "url": ARCUS_WS_URL,
            "status": "running" if self._running else "stopped",
            "symbols": len(self._symbols),
            "ob_symbols": len(self._ob_symbols),
            "markets_tracked": len(self.market_attrs),
            "last_msg_age_s": round(time.time() - self._last_msg_ts, 1)
            if self._last_msg_ts else None,
        }
