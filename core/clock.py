"""
Exchange Clock — authoritative server-time source for ARIA.

Problem: bot's local clock can drift relative to exchange. SoDEX and Bybit
both embed server_time in every WS message and REST response. If ARIA uses
local time for journal entries and order timestamps, records diverge from
exchange history when drift exceeds a few seconds.

Fix: fetch server time from Bybit at startup (SoDEX has no dedicated /time endpoint).
Compute offset = exchange_ms - local_ms. Apply to every internally-generated timestamp.

Usage:
    from core.clock import ExchangeClock
    clock = ExchangeClock()
    await clock.sync()          # call once at startup
    ts_ms = clock.now_ms()      # authoritative ms timestamp
    ts_iso = clock.now_iso()    # ISO-8601 string
"""

import time
import asyncio
import structlog
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = structlog.get_logger(__name__)

# Bybit public time endpoint — no auth required
_BYBIT_TIME_URL = "https://api.bybit.com/v5/market/time"
_SYNC_INTERVAL_S = 300  # re-sync every 5 min to catch drift


class ExchangeClock:
    """
    Single authoritative clock for all internal timestamps.

    All components that create journal entries, log events, or compute
    cooldown windows should call clock.now_ms() instead of time.time()*1000.

    The offset is applied transparently; callers never need to know about drift.
    """

    def __init__(self):
        self._offset_ms: float = 0.0       # exchange_ms - local_ms
        self._last_sync_ms: float = 0.0
        self._synced: bool = False

    async def sync(self, timeout: float = 5.0) -> bool:
        """
        Fetch server time from Bybit, compute offset.
        Returns True on success, False on failure (offset stays 0 = use local clock).
        """
        import httpx
        try:
            before_ms = time.time() * 1000
            async with httpx.AsyncClient(timeout=timeout) as http:
                resp = await http.get(_BYBIT_TIME_URL)
            after_ms = time.time() * 1000
            rtt_ms = after_ms - before_ms

            payload = resp.json()
            # Bybit returns: {"retCode":0,"result":{"timeSecond":"...","timeNano":"..."}}
            result = payload.get("result", {})
            server_ms = float(result.get("timeNano", 0)) / 1_000_000
            if server_ms == 0:
                # fallback: timeSecond
                server_ms = float(result.get("timeSecond", 0)) * 1000

            if server_ms == 0:
                logger.warning("clock_sync_no_time", payload=payload)
                return False

            # Use midpoint of request as local reference (halve the RTT)
            local_ref_ms = before_ms + rtt_ms / 2
            self._offset_ms = server_ms - local_ref_ms
            self._last_sync_ms = after_ms
            self._synced = True

            logger.info("clock_synced",
                        offset_ms=round(self._offset_ms, 1),
                        rtt_ms=round(rtt_ms, 1),
                        server_ms=int(server_ms),
                        local_ms=int(local_ref_ms))
            return True

        except Exception as e:
            logger.warning("clock_sync_failed", error=str(e),
                           note="using local clock — journal timestamps may drift")
            return False

    async def start_auto_sync(self):
        """Background task: re-syncs every SYNC_INTERVAL_S to handle drift."""
        while True:
            await asyncio.sleep(_SYNC_INTERVAL_S)
            await self.sync()

    def now_ms(self) -> int:
        """Current authoritative timestamp in milliseconds."""
        return int(time.time() * 1000 + self._offset_ms)

    def now_s(self) -> float:
        """Current authoritative timestamp in seconds."""
        return time.time() + self._offset_ms / 1000

    def now_iso(self) -> str:
        """Current authoritative timestamp as ISO-8601 UTC string."""
        return datetime.fromtimestamp(self.now_s(), tz=timezone.utc).isoformat()

    def now_date_str(self) -> str:
        """YYYY-MM-DD in UTC — used for journal file naming."""
        return datetime.fromtimestamp(self.now_s(), tz=timezone.utc).strftime("%Y-%m-%d")

    def offset_ms(self) -> float:
        return self._offset_ms

    def is_synced(self) -> bool:
        return self._synced

    def ms_to_iso(self, ms: int) -> str:
        """Convert an exchange-originated ms timestamp to ISO-8601."""
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


# Module-level singleton — import and use everywhere
exchange_clock = ExchangeClock()
