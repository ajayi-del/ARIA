import asyncio
import structlog
import time
from typing import Dict, Any, Optional

logger = structlog.get_logger(__name__)


class SoDEXMarketDataCache:
    """
    Lightweight cache for SoDEX market snapshot data.

    Populated by a background poller (main.py) every 5 minutes.
    Hot-path reads are O(1) dict lookups — zero latency impact on signals.

    Fields cached per symbol (from /markets/symbols or per-currency snapshot):
      - change_pct_24h:  24h price change % (momentum proxy)
      - high_24h:        24h high (volatility ceiling)
      - low_24h:         24h low (volatility floor)
      - turnover_24h:    24h volume USD (liquidity proxy)
      - ath:             All-time high (sentiment extreme)
      - down_from_ath:   % down from ATH (discount proxy)
      - cycle_low:       Cycle low (long-term support)
      - marketcap_rank:  Market cap rank (relative strength)
    """

    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._last_update_ms: int = 0

    def update(self, symbol: str, data: Dict[str, Any]) -> None:
        """Store snapshot for symbol."""
        self._cache[symbol] = data
        self._last_update_ms = int(time.time() * 1000)

    def get(self, symbol: str) -> Dict[str, Any]:
        """Return cached snapshot or empty dict."""
        return self._cache.get(symbol, {})

    def is_fresh(self, max_age_ms: int = 600_000) -> bool:
        """True if cache updated within max_age_ms (default 10 min)."""
        return (int(time.time() * 1000) - self._last_update_ms) < max_age_ms

    def age_ms(self) -> int:
        return int(time.time() * 1000) - self._last_update_ms


class SoDEXMarketDataPoller:
    """
    Background poller for SoDEX market data.

    Strategy:
      1. Poll /perps/markets/symbols every 5 min (re-uses existing endpoint).
         SoDEX returns full symbol specs including 24h stats when available.
      2. Extract change_pct_24h, high_24h, low_24h, turnover_24h, ath, etc.
      3. Cache in SoDEXMarketDataCache for hot-path consumption.

    Phase 2: Add per-currency /market-snapshot polling for richer fields
             (ATH, cycle_low, marketcap_rank) once currency_id mapping is known.
    """

    def __init__(
        self,
        sodex_client: Any,
        symbols: list[str],
        interval_seconds: float = 300.0,
    ):
        self.client = sodex_client
        self.symbols = set(symbols)
        self.interval = interval_seconds
        self.cache = SoDEXMarketDataCache()
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop())
        logger.info("sodex_market_data_poller_started",
                    symbols=len(self.symbols),
                    interval_s=self.interval)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("sodex_market_data_poller_stopped")

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._poll()
            except Exception as e:
                logger.warning("market_data_poll_error", error=str(e))
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.interval
                )
            except asyncio.TimeoutError:
                pass

    async def _poll(self) -> None:
        """Refresh symbol_info from SoDEX and extract snapshot fields."""
        # Re-use existing fetch_symbol_mapping — it hits /markets/symbols
        # and stores full market dicts in client.symbol_info.
        await self.client.fetch_symbol_mapping()

        _extracted = 0
        for sym in self.symbols:
            info = self.client.symbol_info.get(sym, {})
            if not info:
                continue

            # Extract fields that SoDEX includes in /markets/symbols response.
            # Field names are best-estimate — verified against live API responses.
            snapshot = {
                "change_pct_24h": self._parse_float(info.get("change24h", info.get("change_pct_24h", info.get("priceChangePercent")))),
                "high_24h": self._parse_float(info.get("high24h", info.get("high_24h", info.get("highPrice")))),
                "low_24h": self._parse_float(info.get("low24h", info.get("low_24h", info.get("lowPrice")))),
                "turnover_24h": self._parse_float(info.get("turnover24h", info.get("turnover_24h", info.get("volume24h")))),
                "mark_price": self._parse_float(info.get("markPrice", info.get("mark_price", info.get("price")))),
                "tick_size": self._parse_float(info.get("tickSize", info.get("tick_size"))),
                "step_size": self._parse_float(info.get("stepSize", info.get("step_size", info.get("minQty")))),
            }

            # Only store if we got at least one meaningful field
            if any(v is not None and v != 0 for v in snapshot.values()):
                self.cache.update(sym, snapshot)
                _extracted += 1

        logger.info("market_data_poll_complete",
                    symbols_polled=len(self.symbols),
                    symbols_extracted=_extracted,
                    cache_age_ms=self.cache.age_ms())

    @staticmethod
    def _parse_float(val: Any) -> Optional[float]:
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None
