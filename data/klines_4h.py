"""Shared 4h kline plane (2026-09-15, Governor ATR/Hurst spec).

One measurement plane for 4h bars, consumed by the vol-stop cybernetics
(intelligence/vol_stop.py) and the Hurst/regime brain
(intelligence/hurst_regime.py). Those brains are zero-I/O — this module is
the splice layer's fetch/parse helper. It never decides anything.

Sources:
  - SoDEX REST: {sodex_rest_perps}/markets/{symbol}/klines?interval=4h
    — the stock-perp 4h plane (verified live 2026-09-15: code 0, data
    newest-first, string numerics, t=open ms / T=close ms).
  - Bybit public v5: /v5/market/kline?category=linear&interval=240
    — deep crypto history (Hurst needs >=100 bars; the WS 4h buffer holds
    only 50). Public endpoint, no auth.

Contracts:
  - fail-open []: a dark plane returns an empty list, never raises.
  - closed bars only: a bar counts when open_time + 4h_ms <= now_ms. The
    forming bar is excluded (same doctrine as stock_carry_plane).
"""

from __future__ import annotations

import time
from typing import Optional

import certifi

from data.candle_buffer import Candle

BAR_MS = 4 * 3_600_000

BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"


def _now_ms() -> int:
    return int(time.time() * 1000)


def closed_only(candles: list[Candle], now_ms: Optional[int] = None) -> list[Candle]:
    """Ascending candles minus the still-forming tail bar."""
    now = _now_ms() if now_ms is None else now_ms
    return [c for c in candles if c.open_time + BAR_MS <= now]


def closes_of(candles: list[Candle]) -> list[float]:
    return [c.close for c in candles]


def parse_sodex_4h(payload: dict, now_ms: Optional[int] = None) -> list[Candle]:
    """SoDEX kline payload → ascending closed Candles. One-bad-line: a
    malformed row is skipped, never fatal."""
    rows = payload.get("data") or []
    out: list[Candle] = []
    for row in reversed(rows):  # payload is newest-first
        try:
            out.append(Candle(
                open_time=int(row["t"]),
                open=float(row["o"]),
                high=float(row["h"]),
                low=float(row["l"]),
                close=float(row["c"]),
                volume=float(row.get("v", 0) or 0),
                close_time=int(row.get("T", 0) or 0),
            ))
        except Exception:
            continue
    return closed_only(out, now_ms)


def parse_bybit_240(payload: dict, now_ms: Optional[int] = None) -> list[Candle]:
    """Bybit v5 kline payload → ascending closed Candles."""
    rows = ((payload.get("result") or {}).get("list")) or []
    out: list[Candle] = []
    for row in reversed(rows):  # payload is newest-first
        try:
            start = int(row[0])
            out.append(Candle(
                open_time=start,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                close_time=start + BAR_MS - 1,
            ))
        except Exception:
            continue
    return closed_only(out, now_ms)


async def fetch_sodex_4h(rest_base: str, symbol: str, limit: int = 120,
                         now_ms: Optional[int] = None) -> list[Candle]:
    import httpx
    try:
        async with httpx.AsyncClient(verify=certifi.where(), timeout=10.0) as client:
            resp = await client.get(
                f"{rest_base}/markets/{symbol}/klines",
                params={"interval": "4h", "limit": limit},
            )
            if resp.status_code != 200:
                return []
            payload = resp.json()
            if payload.get("code") != 0:
                return []
            return parse_sodex_4h(payload, now_ms)
    except Exception:
        return []


async def fetch_bybit_240(bybit_symbol: str, limit: int = 200,
                          now_ms: Optional[int] = None) -> list[Candle]:
    import httpx
    try:
        async with httpx.AsyncClient(verify=certifi.where(), timeout=10.0) as client:
            resp = await client.get(
                BYBIT_KLINE_URL,
                params={"category": "linear", "symbol": bybit_symbol,
                        "interval": "240", "limit": limit},
            )
            if resp.status_code != 200:
                return []
            payload = resp.json()
            if payload.get("retCode") != 0:
                return []
            return parse_bybit_240(payload, now_ms)
    except Exception:
        return []


def realized_vol_rank(candles: list[Candle], hv_window: int = 20,
                      rank_window: int = 120) -> Optional[float]:
    """Parkinson realized-vol percentile, 0-100, or None when thin.

    Polemarch-stamped substitution (2026-09-15): the Governor's spec keys the
    ATR multiplier and the CHAOTIC kill on options IV rank; ARIA has no IV
    feed, so realized-vol rank stands in. Parkinson estimator per bar:
    sqrt(ln(h/l)^2 / (4 ln 2)); the current value is the mean over the last
    hv_window bars, ranked against the trailing rank_window of such values.

    Abstains (None) when fewer than hv_window + 30 bars — the vol-stop
    consumer treats None as "normal regime" (multiplier 2.0), the regime
    classifier treats None as unknown.
    """
    import math

    n = len(candles)
    if n < hv_window + 30:
        return None
    log_hl = []
    for c in candles:
        if c.high <= 0 or c.low <= 0 or c.high < c.low:
            return None
        log_hl.append(math.log(c.high / c.low) ** 2 / (4.0 * math.log(2.0)))
    hvs = []
    for i in range(hv_window, n + 1):
        hvs.append(math.sqrt(sum(log_hl[i - hv_window:i]) / hv_window))
    window = hvs[-rank_window:] if len(hvs) > rank_window else hvs
    current = hvs[-1]
    # Constant-HV window (dead-flat tape, h==l) ranks every tie at 100 — a
    # zero-vol tape is not a high-vol regime; abstain instead.
    if max(window) == min(window):
        return None
    below = sum(1 for v in window if v <= current)
    return 100.0 * below / len(window)
