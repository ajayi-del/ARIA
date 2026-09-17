#!/usr/bin/env python3
"""oracle_freshness_probe.py — MEASURE-ONLY probe.

Every 60s, appends ONE JSON line per tracked symbol to
logs/oracle_freshness.jsonl recording the SoDEX equity-perp mark, how long it
has been unchanged, its change vs the previous sample, the ET session bucket,
and (during RTH only) a Yahoo Finance reference price with basis.

Purpose: settle whether the "stale pre-market oracle" premise behind the Kant
RTH gate (main.py ~4771-4793) still holds — do SoDEX equity perp marks update
off-hours, and do they track anything real?

Standalone: no repo imports, never raises, append-only output.
"""

import asyncio
import json
import time
from datetime import datetime, timezone, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

BASE = "https://mainnet-gw.sodex.dev/api/v1/perps"
MARK_URL = f"{BASE}/markets/mark-prices"  # batch: no params returns all symbols
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

OUT_PATH = Path(__file__).resolve().parent.parent / "logs" / "oracle_freshness.jsonl"

SYMBOLS = [
    "TSLA-USD", "AMZN-USD", "NVDA-USD", "META-USD", "MSFT-USD",
    "AAPL-USD", "GOOGL-USD", "SPCX-USD", "USTECH100-USD",
]
# SoDEX symbol -> Yahoo reference ticker. SPCX (~$153 mark) has no verifiable
# public reference — left unmapped so ref fields stay null rather than wrong.
YAHOO_MAP = {
    "TSLA-USD": "TSLA", "AMZN-USD": "AMZN", "NVDA-USD": "NVDA",
    "META-USD": "META", "MSFT-USD": "MSFT", "AAPL-USD": "AAPL",
    "GOOGL-USD": "GOOGL", "USTECH100-USD": "^NDX",
}

LOOP_S = 60
BACKOFF_S = 300          # per-symbol backoff cadence after ERROR_THRESHOLD
ERROR_THRESHOLD = 10     # consecutive errors before backing off + logging once

ET = ZoneInfo("America/New_York")
RTH_OPEN = dtime(9, 30)
RTH_CLOSE = dtime(16, 0)


def et_session(now_utc: float) -> str:
    dt = datetime.fromtimestamp(now_utc, tz=ET)
    if dt.weekday() >= 5:  # Sat/Sun in ET
        return "weekend"
    return "rth" if RTH_OPEN <= dt.time() < RTH_CLOSE else "off_hours"


async def fetch_marks(client: httpx.AsyncClient):
    """Return (venue_ts_s or None, {symbol: mark}) from one batch call."""
    resp = await client.get(MARK_URL, timeout=15.0)
    resp.raise_for_status()
    payload = resp.json()
    venue_ts = payload.get("timestamp")
    venue_ts_s = (venue_ts / 1000.0) if isinstance(venue_ts, (int, float)) else None
    marks = {}
    for item in payload.get("data", []):
        try:
            marks[item["symbol"]] = float(item["markPrice"])
        except (KeyError, TypeError, ValueError):
            continue
    return venue_ts_s, marks


async def fetch_yahoo_ref(client: httpx.AsyncClient, ticker: str):
    """Return (ref_price, ref_age_s) or raise. Meta-only; no intraday series."""
    resp = await client.get(
        YAHOO_URL.format(ticker=ticker),
        params={"interval": "1m", "range": "1d"},
        headers=YAHOO_HEADERS,
        timeout=15.0,
    )
    resp.raise_for_status()
    meta = resp.json()["chart"]["result"][0]["meta"]
    price = float(meta["regularMarketPrice"])
    mtime = float(meta["regularMarketTime"])
    return price, max(0.0, time.time() - mtime)


async def main() -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = {
        s: {"prev": None, "last_change": None, "errors": 0,
            "backoff_until": 0.0, "backoff_logged": False}
        for s in SYMBOLS
    }

    async with httpx.AsyncClient() as client:
        while True:
            loop_start = time.time()
            records = []
            try:
                now = time.time()
                session = et_session(now)
                venue_ts_s, marks = None, {}
                batch_err = None
                try:
                    venue_ts_s, marks = await fetch_marks(client)
                except Exception as e:  # noqa: BLE001 — never raise
                    batch_err = f"{type(e).__name__}: {e}"

                for sym in SYMBOLS:
                    st = state[sym]
                    if now < st["backoff_until"]:
                        continue  # per-symbol 5-min backoff cadence
                    rec = {
                        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "symbol": sym,
                        "sodex_mark": None,
                        "sodex_mark_age_s": None,
                        "unchanged_streak_s": None,
                        "changed_bp": None,
                        "session": session,
                    }
                    if batch_err is not None or sym not in marks:
                        st["errors"] += 1
                        rec["error"] = batch_err or "symbol_missing_from_payload"
                        if st["errors"] >= ERROR_THRESHOLD and not st["backoff_logged"]:
                            rec["backoff"] = (f"{st['errors']} consecutive errors; "
                                              f"backing off to {BACKOFF_S}s cadence")
                            st["backoff_logged"] = True
                        if st["errors"] >= ERROR_THRESHOLD:
                            st["backoff_until"] = now + BACKOFF_S
                        records.append(rec)
                        continue

                    # success path
                    st["errors"] = 0
                    st["backoff_logged"] = False
                    st["backoff_until"] = 0.0
                    mark = marks[sym]
                    rec["sodex_mark"] = mark
                    if venue_ts_s is not None:
                        rec["sodex_mark_age_s"] = round(max(0.0, now - venue_ts_s), 1)

                    prev = st["prev"]
                    if prev is None:
                        rec["unchanged_streak_s"] = 0
                        rec["changed_bp"] = None
                        st["prev"] = mark
                        st["last_change"] = now
                    elif mark != prev:
                        rec["unchanged_streak_s"] = 0
                        rec["changed_bp"] = round((mark - prev) / prev * 1e4, 4) if prev else None
                        st["prev"] = mark
                        st["last_change"] = now
                    else:
                        rec["unchanged_streak_s"] = round(now - (st["last_change"] or now), 1)
                        rec["changed_bp"] = 0.0

                    # Yahoo reference — only meaningful during RTH.
                    if session == "rth":
                        try:
                            ref_price, ref_age = await fetch_yahoo_ref(client, YAHOO_MAP[sym])
                            rec["ref_price"] = ref_price
                            rec["ref_source"] = "yahoo_chart"
                            rec["ref_age_s"] = round(ref_age, 1)
                            rec["basis_bp"] = round((mark - ref_price) / ref_price * 1e4, 4) \
                                if ref_price else None
                        except Exception as e:  # noqa: BLE001
                            rec["ref_error"] = f"{type(e).__name__}: {e}"
                    records.append(rec)
            except Exception as e:  # noqa: BLE001 — outer guard, never raise
                records.append({
                    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "symbol": "__probe__",
                    "error": f"loop_guard: {type(e).__name__}: {e}",
                })

            try:
                with OUT_PATH.open("a") as f:
                    for rec in records:
                        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
            except Exception:  # noqa: BLE001 — disk hiccup must not kill the loop
                pass

            elapsed = time.time() - loop_start
            await asyncio.sleep(max(1.0, LOOP_S - elapsed))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
