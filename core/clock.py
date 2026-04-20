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


class DailyTradeTracker:
    """
    Persistent daily trade counter — survives restarts.

    The ExchangeClock is the authoritative date source (UTC day boundary).
    State persists to logs/daily_trades.json so session counts are never lost
    on restart. ARIA's "today" resets at UTC midnight aligned with exchange time.

    Usage:
        from core.clock import daily_tracker
        daily_tracker.record_open(symbol="BTC-USD", direction="long")
        daily_tracker.record_close(symbol="BTC-USD", pnl_usd=12.5)
        count = daily_tracker.trades_today()
        print(daily_tracker.summary())
    """

    _PERSIST_PATH = "logs/daily_trades.json"

    def __init__(self, clock: ExchangeClock):
        self._clock = clock
        self._data: dict = {}
        self._loaded: bool = False
        self._load()

    def _load(self) -> None:
        if self._loaded:
            return
        import json, os
        if os.path.exists(self._PERSIST_PATH):
            try:
                with open(self._PERSIST_PATH) as f:
                    self._data = json.load(f)
                logger.info("daily_tracker_loaded",
                            path=self._PERSIST_PATH,
                            dates=list(self._data.keys()),
                            trades_today=self._data.get(
                                self._clock.now_date_str(), {}
                            ).get("count", 0))
            except Exception as e:
                logger.warning("daily_tracker_load_failed", error=str(e))
                self._data = {}
        self._loaded = True

    def _save(self) -> None:
        import json
        try:
            with open(self._PERSIST_PATH, "w") as f:
                json.dump(self._data, f, indent=2)
        except Exception as e:
            logger.warning("daily_tracker_save_failed", error=str(e))

    def _today(self) -> str:
        return self._clock.now_date_str()

    def _ensure_today(self) -> dict:
        today = self._today()
        if today not in self._data:
            self._data[today] = {
                "count": 0,
                "pnl_usd": 0.0,
                "symbols": {},
                "directions": {"long": 0, "short": 0},
            }
        return self._data[today]

    def record_open(self, symbol: str, direction: str) -> None:
        """
        Record a new trade entry.
        Called immediately on successful order placement in main.py _bracket_task().
        """
        bucket = self._ensure_today()
        bucket["count"] += 1
        bucket["symbols"][symbol] = bucket["symbols"].get(symbol, 0) + 1
        bucket["directions"][direction] = bucket["directions"].get(direction, 0) + 1
        self._save()
        logger.info("daily_trade_open",
                    date=self._today(),
                    count=bucket["count"],
                    symbol=symbol,
                    direction=direction)

    def record_close(self, symbol: str, pnl_usd: float) -> None:
        """
        Update today's realized PnL when a position closes (TP or SL).
        Called from _record_close() in main.py.
        """
        bucket = self._ensure_today()
        bucket["pnl_usd"] = round(bucket["pnl_usd"] + pnl_usd, 4)
        self._save()
        logger.info("daily_trade_close",
                    date=self._today(),
                    symbol=symbol,
                    pnl_usd=round(pnl_usd, 4),
                    daily_pnl=bucket["pnl_usd"])

    def trades_today(self) -> int:
        """Total trades opened today."""
        return self._ensure_today()["count"]

    def pnl_today(self) -> float:
        """Total realized PnL for today."""
        return self._ensure_today()["pnl_usd"]

    def get_today(self) -> dict:
        """Full snapshot of today's stats (copy — not a live reference)."""
        return dict(self._ensure_today())

    def summary(self) -> str:
        """One-line display string for terminal panels and logging."""
        b = self._ensure_today()
        return (
            f"Trades: {b['count']} | "
            f"PnL: ${b['pnl_usd']:+.2f} | "
            f"L:{b['directions']['long']} S:{b['directions']['short']}"
        )


# Both singletons — the tracker depends on the clock
daily_tracker = DailyTradeTracker(exchange_clock)
