"""
Aster DEX public WebSocket feed — Binance-protocol streams.

Two jobs:
  1. !forceOrder@arr — all-market liquidation snapshots (1s). Second cascade
     lens alongside the Bybit liq stream: venues_confirming breadth for the
     Tier-6 liq_phase_engine (deferred item 6c made real). Schema is the
     Binance forceOrder shape — same semantics as the Bybit handler.
  2. <symbol>@markPrice@1s — mark/index/funding for Aster-tracked symbols.
     Reference price for cross-venue basis + hedge routing decisions.

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
import structlog
import websockets

from execution.aster_client import to_aster_symbol, to_canonical_symbol

logger = structlog.get_logger(__name__)

ASTER_WS_URL = "wss://fstream.asterdex.com/stream"


class AsterFeed:
    def __init__(self, symbols: Optional[List[str]] = None):
        self._symbols: List[str] = list(symbols or [])   # canonical (BTC-USD)
        self._running = False
        self._liquidation_listeners: list = []
        self._last_liq_ts: float = 0.0
        self._liq_watchdog_started = False
        # canonical → {"mark_price", "index_price", "funding_rate", "ts"}
        self.mark_prices: Dict[str, Dict[str, float]] = {}

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
        await self._run_stream()

    async def stop(self) -> None:
        self._running = False

    def _stream_url(self) -> str:
        streams = ["!forceOrder@arr"]
        for sym in self._symbols:
            streams.append(f"{to_aster_symbol(sym).lower()}@markPrice@1s")
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
        }
