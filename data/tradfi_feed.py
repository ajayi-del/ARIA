"""
TradFi underlying feed — signals from the deep market, execution on SoDEX.

SoDEX equity/commodity perps are thin (few thousand users): their own prints
cannot price-discover, so candles built from SoDEX marks are noise. This feed
polls the REAL underlying (Yahoo v8 chart API, free, no key) once a minute and
writes closed 1m candles into the same candle_buffers the interpreter already
reads — momentum/ATR/regime for these symbols now come from the deep market.

Execution is untouched: entry/stop/TP pricing still uses SoDEX marks. Because
SoDEX perps are rebased synthetics (SPCX ~110 vs SPX ~6500), level basis is
meaningless — the guard instead compares short-window RETURN divergence
(scale-invariant) between the SoDEX mark and the underlying, and exposes it
to the entry chokepoint.

Feed ownership: while this feed is healthy for a symbol, the SoDEX candle
path (WS channel + REST seed) must not write that symbol's buffers —
`sodex_feed` checks `tradfi_owns()`.
"""

import asyncio
import time
from collections import deque

import httpx
import structlog

from core.event_bus import event_bus, Event, EventType
from data.candle_buffer import Candle

logger = structlog.get_logger(__name__)

# SoDEX symbol → Yahoo chart symbol.
# Index perps map to the deep ETFs (0.01% tracking, real-time vs delayed indices).
TRADFI_SYMBOLS: dict[str, str] = {
    "SPCX-USD":      "SPY",
    "USTECH100-USD": "QQQ",
    "NVDA-USD":      "NVDA",
    "MSFT-USD":      "MSFT",
    "AAPL-USD":      "AAPL",
    "AMZN-USD":      "AMZN",
    "GOOGL-USD":     "GOOGL",
    "META-USD":      "META",
    "TSLA-USD":      "TSLA",
    "TSM-USD":       "TSM",
    "ORCL-USD":      "ORCL",
    "CRCL-USD":      "CRCL",
    "COIN-USD":      "COIN",
    "CL-USD":        "CL=F",
    "XAUT-USD":      "GC=F",
    "COPPER-USD":    "HG=F",
    "SILVER-USD":    "SI=F",
}

# Single-name equities — thin SoDEX books where crossing the spread is the
# dominant entry cost. The order-type selector forces maker-only for these.
TRADFI_SINGLE_NAMES: frozenset[str] = frozenset({
    "NVDA-USD", "MSFT-USD", "AAPL-USD", "AMZN-USD", "GOOGL-USD", "META-USD",
    "TSLA-USD", "TSM-USD", "ORCL-USD", "CRCL-USD", "COIN-USD",
})

_POLL_S = 60.0
_HEALTH_S = 120.0          # ~1.5 poll cycles: one slow poll never false-stales
_DIV_WINDOW = 5            # aligned samples (~5 min) for the divergence check
_DIV_BLOCK = 0.003         # 0.3% return divergence → block momentum entries
_DIV_UNBLOCK = 0.002       # hysteresis: re-engage below 0.2% (no flap at threshold)
_CONV_MIN_PERSIST_S = 900  # divergence this long = structural, not noise → convergence trade

_feed: "TradfiFeed | None" = None

# Symbols whose candles are owned by another feed (2026-08-18: Aster kline_1m
# for XAUT/CL — Yahoo futures 1m lags ~10 min overnight, tripping the 90s
# interpreter staleness guard 23.7k times). We still poll Yahoo for these
# (underlying price + divergence check stay useful) but never write candles.
_candle_yield: set[str] = set()


def set_candle_yield(symbols) -> None:
    global _candle_yield
    _candle_yield = set(symbols or [])


def tradfi_owns(symbol: str) -> bool:
    """True when this feed is the symbol's candle WRITER.

    Yielded symbols (Aster/SoDEX kline-owned: XAUT/CL/SILVER/COPPER) keep
    getting polled for the basis-divergence guard but this feed never writes
    their candles — so it must not claim ownership, or every feed yields and
    the plane goes dark (2026-08-24 design pin).
    """
    return (
        symbol not in _candle_yield
        and _feed is not None
        and _feed.healthy(symbol)
    )


def tradfi_health(symbol: str) -> bool:
    return _feed is not None and _feed.healthy(symbol)


def tradfi_basis_divergent(symbol: str) -> bool | None:
    """True = confirmed dislocation (block entries). None = insufficient data (don't block)."""
    if _feed is None:
        return None
    return _feed.basis_divergent(symbol)


def tradfi_convergence_signal(symbol: str) -> tuple[str, float] | None:
    if _feed is None:
        return None
    return _feed.convergence_signal(symbol)


def tradfi_underlying_price(symbol: str) -> float | None:
    if _feed is None:
        return None
    return _feed.underlying_price(symbol)


class TradfiFeed:
    def __init__(self, candle_buffers: dict, mark_price_stores: dict):
        self.candle_buffers = candle_buffers
        self.mark_price_stores = mark_price_stores
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_ok: dict[str, float] = {}            # sodex_sym → epoch of last good quote
        self._underlying_px: dict[str, float] = {}      # sodex_sym → latest underlying price
        self._u_series: dict[str, deque] = {}           # sodex_sym → deque[(ts, underlying_px)]
        self._s_series: dict[str, deque] = {}           # sodex_sym → deque[(ts, sodex_mark)]
        self._fail_counts: dict[str, int] = {}
        self._last_warn: dict[str, float] = {}
        self._seeded: set[str] = set()
        self._div_blocked: dict[str, bool] = {}     # hysteresis state per symbol
        self._div_since: dict[str, float] = {}      # continuous-divergence start epoch
        self._div_mag: dict[str, float] = {}        # latest |sodex_ret − underlying_ret|
        self._div_dir: dict[str, int] = {}          # +1 underlying leads → long; −1 → short
        self._last_bar_ts: dict[str, int] = {}      # sodex_sym → last published 1m bar open_time

    # ── Queries ──────────────────────────────────────────────────────────────

    def healthy(self, symbol: str) -> bool:
        ts = self._last_ok.get(symbol, 0.0)
        return ts > 0 and (time.time() - ts) <= _HEALTH_S

    def underlying_price(self, symbol: str) -> float | None:
        if not self.healthy(symbol):
            return None
        return self._underlying_px.get(symbol)

    def basis_divergent(self, symbol: str) -> bool | None:
        u = self._u_series.get(symbol)
        s = self._s_series.get(symbol)
        if not u or not s or len(u) < _DIV_WINDOW or len(s) < _DIV_WINDOW:
            return None
        if u[-1][0] - u[0][0] < (_DIV_WINDOW - 1) * _POLL_S * 0.8:
            return None   # window not yet spanning ~4 minutes
        u_ret = u[-1][1] / u[0][1] - 1.0 if u[0][1] > 0 else 0.0
        s_ret = s[-1][1] / s[0][1] - 1.0 if s[0][1] > 0 else 0.0
        div = s_ret - u_ret
        self._div_mag[symbol] = abs(div)
        self._div_dir[symbol] = 1 if div < 0 else -1   # SoDEX lags rally → long; lags selloff → short
        blocked = self._div_blocked.get(symbol, False)
        if abs(div) > _DIV_BLOCK:
            if not blocked:
                self._div_blocked[symbol] = True
                self._div_since.setdefault(symbol, time.time())
                logger.warning("tradfi_basis_divergence_onset", symbol=symbol,
                               divergence_pct=round(abs(div) * 100, 3))
            return True
        if blocked and abs(div) < _DIV_UNBLOCK:
            self._div_blocked[symbol] = False
            self._div_since.pop(symbol, None)
            logger.info("tradfi_basis_divergence_cleared", symbol=symbol,
                        divergence_pct=round(abs(div) * 100, 3))
        return blocked

    def convergence_signal(self, symbol: str) -> tuple[str, float] | None:
        """(direction, magnitude) when divergence has persisted ≥15min.

        A persistent return gap means the SoDEX mark is structurally off-peg,
        not noisy — the MM's hedge pulls it back. Direction: SoDEX lagged the
        underlying's move, so bet the catch-up. Magnitude feeds the stop
        (2× divergence at entry).
        """
        if not self._div_blocked.get(symbol):
            return None
        since = self._div_since.get(symbol, 0.0)
        if not since or (time.time() - since) < _CONV_MIN_PERSIST_S:
            return None
        if not self.healthy(symbol):
            return None
        d = self._div_dir.get(symbol, 0)
        mag = self._div_mag.get(symbol, 0.0)
        if d == 0 or mag <= 0:
            return None
        return ("long" if d > 0 else "short", mag)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        global _feed
        _feed = self
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("tradfi_feed_started", symbols=len(TRADFI_SYMBOLS))

    async def stop(self) -> None:
        global _feed
        self._running = False
        _feed = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── Poll loop ────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        # Stagger the first sweep so boot doesn't fire 17 concurrent requests.
        await asyncio.sleep(5.0)
        async with httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ARIA-tradfi/1.0)"},
        ) as client:
            while self._running:
                t0 = time.time()
                for sodex_sym, yahoo_sym in TRADFI_SYMBOLS.items():
                    if not self._running:
                        break
                    try:
                        await self._poll_one(client, sodex_sym, yahoo_sym)
                    except Exception as e:
                        self._fail_counts[sodex_sym] = self._fail_counts.get(sodex_sym, 0) + 1
                        _lw = self._last_warn.get(sodex_sym, 0.0)
                        if time.time() - _lw > 900:   # 1 warning / 15min / symbol
                            self._last_warn[sodex_sym] = time.time()
                            logger.warning("tradfi_poll_error",
                                           symbol=sodex_sym, error=str(e)[:120],
                                           consecutive=self._fail_counts[sodex_sym])
                    await asyncio.sleep(0.5)   # be polite — 17 symbols / minute
                # Record aligned SoDEX marks for the divergence check
                self._sample_sodex_marks()
                await asyncio.sleep(max(1.0, _POLL_S - (time.time() - t0)))

    async def _poll_one(self, client: httpx.AsyncClient, sodex_sym: str, yahoo_sym: str) -> None:
        resp = await client.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}",
            params={"interval": "1m", "range": "1d", "includePrePost": "false"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"http_{resp.status_code}")
        payload = resp.json()
        result = (payload.get("chart", {}).get("result") or [None])[0]
        if not result:
            raise RuntimeError("empty_chart")
        meta = result.get("meta", {})
        price = float(meta.get("regularMarketPrice", 0) or 0)
        if price <= 0:
            raise RuntimeError("no_price")

        now = time.time()
        self._underlying_px[sodex_sym] = price
        self._last_ok[sodex_sym] = now
        self._fail_counts[sodex_sym] = 0
        self._u_series.setdefault(sodex_sym, deque(maxlen=12)).append((now, price))

        if sodex_sym in _candle_yield:
            return   # candles owned by the execution-venue feed (aster kline_1m)

        # 1m bars → candle buffer. The FORMING bar is written too (buf.add
        # replaces it in place each poll) — same contract as the Bybit feed.
        # 2026-08-18: closed-bars-only left the tail one full bar + poll
        # interval behind, and the interpreter's 90s staleness guard (measured
        # from the tail's open_time) vetoed ~every equity signal all session
        # (META 911, ORCL 1427, TSLA 487 signal_stale_data in one Monday).
        timestamps = result.get("timestamp") or []
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        opens, highs = quote.get("open") or [], quote.get("high") or []
        lows, closes = quote.get("low") or [], quote.get("close") or []
        volumes = quote.get("volume") or []
        buf = self.candle_buffers.get(sodex_sym, {}).get("1m")
        if buf is None:
            return
        added = 0
        newest_closed_ot, newest_closed_close = 0, 0.0
        for i, ts in enumerate(timestamps):
            forming = ts + 60 > int(now)
            try:
                o, h, l, c = opens[i], highs[i], lows[i], closes[i]
                if o is None or h is None or l is None or c is None:
                    continue
                buf.add(Candle(
                    open_time=int(ts) * 1000,
                    open=float(o), high=float(h), low=float(l), close=float(c),
                    volume=float(volumes[i] or 0) if i < len(volumes) else 0.0,
                    close_time=(int(ts) + 60) * 1000,
                ))
                if forming:
                    continue
                added += 1
                if int(ts) * 1000 > newest_closed_ot:
                    newest_closed_ot = int(ts) * 1000
                    newest_closed_close = float(c)
            except (IndexError, TypeError, ValueError):
                continue
        if added and sodex_sym not in self._seeded:
            self._seeded.add(sodex_sym)
            logger.info("tradfi_candles_seeded", symbol=sodex_sym, candles=buf.count())

        # Wake the interpreter: its slow path is driven exclusively by
        # CANDLE_CLOSED, and sodex_feed stops publishing while we own the
        # symbol. One event per NEW CLOSED bar (the forming bar never
        # publishes); off-hours the tail never advances, so the interpreter
        # correctly stays silent.
        if newest_closed_ot > self._last_bar_ts.get(sodex_sym, 0):
            self._last_bar_ts[sodex_sym] = newest_closed_ot
            event_bus.publish(Event(
                event_type=EventType.CANDLE_CLOSED,
                symbol=sodex_sym,
                timestamp_ms=newest_closed_ot,
                data={"count": buf.count(), "close": newest_closed_close,
                      "confirmed": True},
            ))

    def _sample_sodex_marks(self) -> None:
        now = time.time()
        for sodex_sym in TRADFI_SYMBOLS:
            store = self.mark_price_stores.get(sodex_sym)
            if store is None:
                continue
            mark = float(getattr(store, "mark_price", 0.0) or 0.0)
            if mark <= 0:
                continue
            self._s_series.setdefault(sodex_sym, deque(maxlen=12)).append((now, mark))
