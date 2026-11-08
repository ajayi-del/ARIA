"""
Hyperliquid HIP-3 ("xyz:") REST feed — 24/7 candle plane for ARIA's
equity/commodity perps.

The venue lists xyz: perps (xyz:NVDA, xyz:GOLD, xyz:SP500, ...) that trade
24/7, fixing the chronic off-hours starvation of session-bound underlying
feeds. Verified wire shapes (2026-11-07 probes, no auth):

  POST https://api.hyperliquid.xyz/info
    {"type":"candleSnapshot","req":{"coin":"xyz:NVDA","interval":"1m",
     "startTime":<ms>,"endTime":<ms>}}
    → [{"t":1794103740000,"T":1794103799999,"s":"xyz:NVDA","i":"1m",
        "o":"186.65","c":"186.64","h":"186.65","l":"186.64","v":"7.065","n":8}]
    (oldest-first; the FINAL row is the still-forming bar)

  {"type":"metaAndAssetCtxs","dex":"xyz"}  ← the "dex" field is load-bearing:
    xyz: assets do NOT appear in the default (no-dex) response.
    → [{"universe":[{"name":"xyz:NVDA",...}, ...]},
       [{"funding":"0.00000625","openInterest":"10422.3516",
         "markPx":"26923.0","midPx":"26922.5",...}, ...]]

Ownership (dark-plane pin, same doctrine as sodex_kline_assets): while this
feed is HEALTHY for a symbol it owns the 1m candle buffer; sodex_feed's WS
candle handler and reseed gate yield via hl_owns(). The yield is
health-GATED — HL dark → SoDEX kline resumes writing, so the plane never
goes dark. tradfi_owns() likewise yields (belt; the static yield set already
covers every current overlap).

SCALE DOCTRINE (why marks are NOT written by default): xyz: marks print the
REAL underlying scale (xyz:SP500 ~7367) while ARIA's execution venues serve
rebased synthetics (SPCX ~140). Writing HL marks into mark_price_stores
would be the 2026-08-22 phantom-PnL class at birth (mark-scale sentinel
false-quarantines, mark-derived PnL corruption). Candles are safe — the
candle plane was ALREADY underlying-scale under Yahoo ownership, and
_sentinel_venue_ref_symbol / venue_plane_atr treat HL-owned symbols with
that same cross-scale routing. HL_MARKS_ENABLED=true opts in deliberately.
"""

import asyncio
import os
import time

import httpx
import structlog

from core.event_bus import event_bus, Event, EventType
from data.candle_buffer import Candle
from data.sodex_feed import _aggregate_higher_tf, _merge_closed_bars, _replay_higher_tf

logger = structlog.get_logger(__name__)

HL_REST = "https://api.hyperliquid.xyz/info"


def _load_hl_symbols() -> dict[str, str]:
    """ARIA symbol → xyz: coin. Canonical source: prop_harness/io/signal_tap.py
    SYMBOL_MAP (Governor 2026-11-07). xyz:SMCI is NOT listed on the HL xyz dex
    (2026-11-07 universe probe — 143 names, no SMCI) so SMCI-USD stays
    SoDEX-kline-owned; SILVER-USD is universe-removed (2026-10-10).
    Fail-closed: any import defect → empty map → the feed stands down."""
    try:
        from prop_harness.io.signal_tap import SYMBOL_MAP
        return {aria: coin for aria, coin in SYMBOL_MAP.items()
                if isinstance(coin, str) and coin.startswith("xyz:")}
    except Exception:
        return {}


HL_SYMBOLS: dict[str, str] = _load_hl_symbols()

_POLL_S = 45.0            # REST cadence — 1m bars, generous public rate limits
_HEALTH_S = 150.0         # ~3 poll cycles: one slow poll never false-stales
_SEED_BARS = 55           # mirror of the SODEX_SUPPORTED boot-seed idiom
_SEED_WINDOW_MS = _SEED_BARS * 60_000

_feed: "HyperliquidFeed | None" = None


def hl_marks_enabled() -> bool:
    """Separate kill switch for mark writes (scale doctrine in the module
    docstring — default OFF, candles-only)."""
    return os.environ.get("HL_MARKS_ENABLED", "false").lower() == "true"


def hl_owns(symbol: str) -> bool:
    """True when this feed is the symbol's candle WRITER right now.

    Health-gated: a dark HL feed owns nothing, so the SoDEX kline plane
    resumes (the 2026-08-24 naive-yield dark-plane pin)."""
    return (
        _feed is not None
        and _feed.enabled
        and _feed.healthy(symbol)
    )


def hl_health(symbol: str) -> bool:
    return _feed is not None and _feed.enabled and _feed.healthy(symbol)


def hl_mark(symbol: str) -> float | None:
    """Latest xyz: mark (midPx) for the symbol — SIGNAL-plane read only.
    Never wired into mark_price_stores unless HL_MARKS_ENABLED=true."""
    if _feed is None:
        return None
    return _feed.marks.get(symbol)


def parse_candle_rows(rows) -> list[Candle]:
    """HL candleSnapshot rows → Candle list. Skips malformed rows; never
    raises (fail-closed data plane)."""
    out: list[Candle] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            ot = int(row["t"])
            c = Candle(
                open_time=ot,
                open=float(row["o"]),
                high=float(row["h"]),
                low=float(row["l"]),
                close=float(row["c"]),
                volume=float(row.get("v") or 0.0),
                close_time=ot + 60_000,
            )
        except (KeyError, TypeError, ValueError):
            continue
        if c.open <= 0 or c.close <= 0:
            continue
        out.append(c)
    return out


class HyperliquidFeed:
    def __init__(self, candle_buffers: dict, mark_price_stores: dict,
                 symbols: list[str]):
        self.candle_buffers = candle_buffers
        self.mark_price_stores = mark_price_stores
        # Only symbols with an xyz: listing AND a candle buffer can be served.
        self.symbols: list[str] = [
            s for s in (symbols or [])
            if s in HL_SYMBOLS and "1m" in (candle_buffers.get(s) or {})
        ]
        self.enabled = False          # flips True only after boot self-verify
        self.marks: dict[str, float] = {}     # signal-plane xyz marks
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_ok: dict[str, float] = {}
        self._fail_counts: dict[str, int] = {}
        self._last_warn: dict[str, float] = {}
        self._last_bar_ts: dict[str, int] = {}   # last published closed 1m open_time

    # ── Queries ──────────────────────────────────────────────────────────────

    def healthy(self, symbol: str) -> bool:
        ts = self._last_ok.get(symbol, 0.0)
        return ts > 0 and (time.time() - ts) <= _HEALTH_S

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Boot self-verify gates the whole feed (HL_FEED_ENABLED default-true
        is only safe because a failed verify stands the feed down and the
        health-gated yield falls back to the SoDEX kline plane)."""
        global _feed
        _feed = self
        if not self.symbols:
            logger.warning("hl_feed_standdown", reason="no_symbols")
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                verified = await self._boot_verify(client)
                if not verified:
                    logger.warning("hl_boot_verify_failed",
                                   symbols=self.symbols)
                    return
                await self._seed_all(client)
        except Exception as e:
            logger.warning("hl_boot_verify_failed", error=str(e)[:160])
            return
        self.enabled = True
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("hl_feed_started", symbols=len(self.symbols))

    async def stop(self) -> None:
        global _feed
        self._running = False
        self.enabled = False
        _feed = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _boot_verify(self, client: httpx.AsyncClient) -> bool:
        """The xyz dex is addressed via the "dex" field — verify our coins
        exist in ITS universe (the default metaAndAssetCtxs omits them)."""
        resp = await client.post(HL_REST, json={
            "type": "metaAndAssetCtxs", "dex": "xyz"})
        if resp.status_code != 200:
            logger.warning("hl_boot_verify_failed",
                           reason=f"http_{resp.status_code}")
            return False
        payload = resp.json()
        if not (isinstance(payload, list) and len(payload) == 2
                and isinstance(payload[0], dict)
                and isinstance(payload[1], list)):
            logger.warning("hl_boot_verify_failed", reason="bad_shape")
            return False
        universe = {a.get("name") for a in payload[0].get("universe") or []
                    if isinstance(a, dict)}
        ok = []
        for sym in self.symbols:
            coin = HL_SYMBOLS[sym]
            if coin in universe:
                ok.append(sym)
            else:
                logger.warning("hl_coin_missing", symbol=sym, coin=coin)
        self.symbols = ok
        return bool(ok)

    async def _seed_all(self, client: httpx.AsyncClient) -> None:
        """Backfill ~55 1m bars per symbol (SODEX_SUPPORTED seed idiom)."""
        now_ms = int(time.time() * 1000)
        for sym in self.symbols:
            try:
                rows = await self._fetch_candles(
                    client, sym, now_ms - _SEED_WINDOW_MS, now_ms)
                candles = parse_candle_rows(rows)
                buf = self.candle_buffers.get(sym, {}).get("1m")
                if not candles or buf is None:
                    continue
                _merge_closed_bars(buf, candles)
                _replay_higher_tf(self.candle_buffers.get(sym, {}),
                                  candles[0].open_time)
                # The seed defines the publish watermark — only bars closed
                # AFTER boot publish CANDLE_CLOSED from the poll loop.
                closed = [c for c in candles
                          if c.open_time + 60_000 <= now_ms]
                if closed:
                    self._last_bar_ts[sym] = max(c.open_time for c in closed)
                self._last_ok[sym] = time.time()
                logger.info("hl_historical_loaded", symbol=sym,
                            candles=buf.count())
            except Exception as e:
                logger.warning("hl_historical_failed", symbol=sym,
                               error=str(e)[:120])
            await asyncio.sleep(0.3)   # polite — public endpoint

    # ── Poll loop ────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        await asyncio.sleep(5.0)   # stagger past boot (tradfi idiom)
        async with httpx.AsyncClient(timeout=10.0) as client:
            while self._running:
                t0 = time.time()
                for sym in self.symbols:
                    if not self._running:
                        break
                    try:
                        await self._poll_one(client, sym)
                    except Exception as e:
                        self._fail_counts[sym] = self._fail_counts.get(sym, 0) + 1
                        lw = self._last_warn.get(sym, 0.0)
                        if time.time() - lw > 900:   # 1 warning / 15min / symbol
                            self._last_warn[sym] = time.time()
                            logger.warning("hl_poll_error", symbol=sym,
                                           error=str(e)[:120],
                                           consecutive=self._fail_counts[sym])
                    await asyncio.sleep(0.3)
                await asyncio.sleep(max(1.0, _POLL_S - (time.time() - t0)))

    async def _fetch_candles(self, client: httpx.AsyncClient, symbol: str,
                             start_ms: int, end_ms: int):
        resp = await client.post(HL_REST, json={
            "type": "candleSnapshot",
            "req": {"coin": HL_SYMBOLS[symbol], "interval": "1m",
                    "startTime": int(start_ms), "endTime": int(end_ms)},
        })
        if resp.status_code != 200:
            raise RuntimeError(f"http_{resp.status_code}")
        return resp.json()

    async def _poll_one(self, client: httpx.AsyncClient, symbol: str) -> None:
        now_ms = int(time.time() * 1000)
        rows = await self._fetch_candles(client, symbol,
                                         now_ms - 5 * 60_000, now_ms)
        candles = parse_candle_rows(rows)
        if not candles:
            raise RuntimeError("empty_snapshot")
        buf = self.candle_buffers.get(symbol, {}).get("1m")
        if buf is None:
            return

        now = time.time()
        self._last_ok[symbol] = now
        self._fail_counts[symbol] = 0

        # Closed + FORMING bars both written (buf.add replaces the forming
        # bar in place) — the 2026-08-14 starvation fix contract: closed-only
        # leaves the tail a full bar behind and the interpreter's 90s
        # staleness guard vetoes every signal.
        newest_closed: Candle | None = None
        for c in candles:
            buf.add(c)
            if c.open_time + 60_000 <= now_ms:
                if newest_closed is None or c.open_time > newest_closed.open_time:
                    newest_closed = c

        # Marks: signal-plane dict always; mark_price_stores only behind the
        # scale-doctrine kill switch (module docstring).
        self.marks[symbol] = candles[-1].close
        if hl_marks_enabled():
            store = self.mark_price_stores.get(symbol)
            if store is not None:
                store.update(candles[-1].close, candles[-1].close, now_ms)

        # One CANDLE_CLOSED per NEWLY closed bar (the forming bar never
        # publishes) + higher-TF aggregation — the sodex WS handler contract.
        if (newest_closed is not None
                and newest_closed.open_time > self._last_bar_ts.get(symbol, 0)):
            self._last_bar_ts[symbol] = newest_closed.open_time
            _aggregate_higher_tf(self.candle_buffers.get(symbol, {}),
                                 newest_closed, symbol=symbol)
            event_bus.publish(Event(
                event_type=EventType.CANDLE_CLOSED,
                symbol=symbol,
                timestamp_ms=newest_closed.open_time,
                data={"count": buf.count(), "close": newest_closed.close,
                      "confirmed": True},
            ))
