"""
data/ssi_spot_feed.py — SSI Spot WebSocket Feed

Connects to the SoDEX spot WebSocket (wss://mainnet-gw.sodex.dev/ws/spot)
and subscribes to 1-minute kline data for SSI signal tokens.

SSI tokens are SPOT assets only — no perp contract exists.
This feed is READ-ONLY: candles and latest price for regime classification
and SLP vault yield estimation. Never used for order execution.

Candles stored in candle_buffers["MAG7SSI-USD"]["1m"] — same structure as
perp assets so RelativeStrengthEngine._compute_momentum() needs no changes.

signal_price_stores["MAG7SSI-USD"] = {
    "price": float,
    "ts_ms": int,
    "drift_1h": float,   # (price_now - price_60m_ago) / price_60m_ago
}
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from typing import Any, Callable, Dict, Optional

import certifi
import structlog
import websockets

from core.config import Settings
from data.candle_buffer import CandleBuffer, Candle

logger = structlog.get_logger(__name__)


class SSISpotFeed:
    """
    Read-only 1m kline feed for SSI spot tokens (MAG7SSI, DEFISSI, MEMESSI, USSI).

    Wire-up in main.py:
        ssi_spot_feed = SSISpotFeed(config, candle_buffers, signal_price_stores)
        ssi_spot_feed.on_mag7ssi_price = slp_tracker.update_mag7ssi_price
        # In asyncio.gather:
        ssi_spot_feed.start()
    """

    def __init__(
        self,
        config: Settings,
        candle_buffers: Dict[str, Dict[str, CandleBuffer]],
        signal_price_stores: Dict[str, Dict[str, Any]],
    ) -> None:
        self.config             = config
        self.candle_buffers     = candle_buffers
        self.signal_price_stores = signal_price_stores

        # Build spot_symbol → ARIA symbol lookup from ASSET_CONFIG
        # e.g. "MAG7SSI_USDC" → "MAG7SSI-USD"
        self._spot_to_aria: Dict[str, str] = {}
        for sym in config.signal_assets:
            cfg      = config.ASSET_CONFIG.get(sym, {})
            spot_sym = cfg.get("spot_ws_symbol")
            if spot_sym:
                self._spot_to_aria[spot_sym] = sym

        self._running    = False
        self._task: Optional[asyncio.Task] = None
        self._msg_count  = 0
        self._connected  = False

        # Optional callback — set after init once slp_tracker is available.
        # Called with the latest MAG7SSI price on every candle close.
        self.on_mag7ssi_price: Optional[Callable[[float], None]] = None

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the SSI spot feed as a background task (self-healing)."""
        self._running = True
        if not self._spot_to_aria:
            logger.info("ssi_spot_feed_skipped", reason="no_signal_assets_in_config")
            return
        logger.info("starting_ssi_spot_feed",
                    spot_symbols=list(self._spot_to_aria.keys()))
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def health(self) -> dict:
        return {
            "feed":        "ssi_spot",
            "connected":   self._connected,
            "msg_count":   self._msg_count,
            "symbols":     list(self._spot_to_aria.keys()),
        }

    # ── Connection loop ───────────────────────────────────────────────────────

    async def _run(self) -> None:
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        url     = self.config.ws_spot_url   # wss://mainnet-gw.sodex.dev/ws/spot
        backoff = 1.0

        while self._running:
            try:
                async with websockets.connect(
                    url,
                    ssl=ssl_ctx,
                    ping_interval=None,
                    close_timeout=5,
                ) as ws:
                    backoff          = 1.0
                    self._connected  = True
                    logger.info("ssi_spot_connected", url=url)

                    # Subscribe all SSI tokens to 1m kline
                    for spot_sym in self._spot_to_aria:
                        await ws.send(json.dumps({
                            "op":     "subscribe",
                            "params": {
                                "channel":  "kline",
                                "symbol":   spot_sym,
                                "interval": "1m",
                            },
                        }))

                    last_msg_t = time.time()

                    async def _keepalive():
                        while self._running:
                            await asyncio.sleep(20)
                            if time.time() - last_msg_t > 20:
                                try:
                                    await ws.send(json.dumps({"op": "ping"}))
                                except Exception:
                                    pass

                    ka = asyncio.create_task(_keepalive())
                    try:
                        async for raw in ws:
                            if not self._running:
                                break
                            last_msg_t      = time.time()
                            self._msg_count += 1
                            try:
                                msg = json.loads(raw)
                                if msg.get("op") == "pong":
                                    continue
                                await self._handle(msg)
                            except Exception as e:
                                logger.warning("ssi_spot_msg_error", error=str(e))
                    finally:
                        ka.cancel()
                        await asyncio.gather(ka, return_exceptions=True)

            except Exception as e:
                if not self._running:
                    break
                self._connected = False
                import random as _r
                jitter = backoff * (0.8 + 0.4 * _r.random())
                logger.warning("ssi_spot_disconnected",
                               error=str(e), reconnect_in=round(jitter, 2))
                await asyncio.sleep(jitter)
                backoff = min(backoff * 2, 60)

        self._connected = False

    # ── Message handler ───────────────────────────────────────────────────────

    async def _handle(self, msg: dict) -> None:
        channel = msg.get("channel", "")
        data    = msg.get("data", {})

        if channel != "kline" or not isinstance(data, dict):
            return

        spot_sym = data.get("s", "")
        aria_sym = self._spot_to_aria.get(spot_sym)
        if not aria_sym:
            return

        now_ms = int(time.time() * 1000)
        try:
            candle = Candle(
                open_time  = int(data.get("t", now_ms)),
                open       = float(data.get("o", 0)),
                high       = float(data.get("h", 0)),
                low        = float(data.get("l", 0)),
                close      = float(data.get("c", 0)),
                volume     = float(data.get("v", 0)),
                close_time = int(data.get("T", now_ms + 60_000)),
            )
        except (ValueError, TypeError):
            return

        if candle.close <= 0:
            return

        # Store candle — same buffer shape as perp assets
        buf = self.candle_buffers.get(aria_sym, {}).get("1m")
        if buf:
            buf.add(candle)

        # Compute 1h drift from candle history
        drift_1h = self._compute_1h_drift(aria_sym, candle.close)

        # Update price store (read by terminal display + SLP tracker)
        self.signal_price_stores[aria_sym] = {
            "price":    candle.close,
            "ts_ms":    now_ms,
            "drift_1h": drift_1h,
        }

        # Feed MAG7SSI price to SLP vault tracker if wired
        if aria_sym == "MAG7SSI-USD" and self.on_mag7ssi_price is not None:
            try:
                self.on_mag7ssi_price(candle.close)
            except Exception:
                pass

    # ── Drift calculation ─────────────────────────────────────────────────────

    def _compute_1h_drift(self, aria_sym: str, current_price: float) -> float:
        """
        Returns (close_now - close_60m_ago) / close_60m_ago.
        Requires ≥ 60 candles in the 1m buffer; returns 0.0 otherwise.
        """
        buf = self.candle_buffers.get(aria_sym, {}).get("1m")
        if buf is None:
            return 0.0
        candles = buf.latest(65)
        if len(candles) < 60:
            return 0.0
        ref = candles[-60].close
        if ref <= 0:
            return 0.0
        return (current_price - ref) / ref
