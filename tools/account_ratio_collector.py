#!/usr/bin/env python3
"""account_ratio_collector.py — Bybit V5 global long/short account-ratio plane.

Standalone, daemon-less, cron-driven observer. Each invocation polls the
Bybit global long/short ACCOUNT ratio (all accounts, not top traders) for
BTCUSDT/ETHUSDT/SOLUSDT and appends NEW records to
logs/account_ratio.jsonl (append-only, dedup by (symbol, open_time)).
Crowd positioning is a sentiment input: a heavily net-long retail book into
resistance is fade fuel; today ARIA has no account-ratio history at all.

CRON (documented only — the local node installs it):
  */5 * * * * cd /home/dayodapper/ARIA && .venv/bin/python tools/account_ratio_collector.py --once >> logs/account_ratio.log 2>&1

SCHEMA-OF-RECORD (operator probe 2026-09-15, verbatim):
  GET https://api.bybit.com/v5/market/account-ratio
      ?category=linear&symbol=ETHUSDT&period=5min&limit=3
  -> HTTP 200
  {"retCode":0,"retMsg":"OK","result":{"list":[
     {"symbol":"ETHUSDT","buyRatio":"0.6784","sellRatio":"0.3216",
      "timestamp":"1789460400000"},
     {"symbol":"ETHUSDT","buyRatio":"0.6783","sellRatio":"0.3217",
      "timestamp":"1789460100000"},
     {"symbol":"ETHUSDT","buyRatio":"0.6772","sellRatio":"0.3228",
      "timestamp":"1789459800000"}],
     "nextPageCursor":"lastid%3D0%26lasttime%3D1789459800"},
   "retExtInfo":{},"time":1789460661467}

  result.list entries: {"symbol": str, "buyRatio": str fraction,
                        "sellRatio": str fraction, "timestamp": str ms epoch}.
  List is NEWEST-FIRST. period accepts "5min" and "1h". Cursor pagination
  exists (nextPageCursor, opaque urlencoded); we paginate with endTime
  windows instead — the cursor is opaque and endTime stepping is robust.
  NOTE: /v5/market/top-trader-ratio is 404 on this endpoint family (probed
  2026-09-15) — do NOT build anything for it.

Record shape appended to logs/account_ratio.jsonl (one JSON object per line):
  {"symbol":"ETHUSDT","open_time":<ms int>,"buy_ratio":<float>,
   "sell_ratio":<float>,"ls_ratio":<float>,"fetched_at":<epoch float>}
  ls_ratio = buy_ratio / sell_ratio. None-safe: a row with sell_ratio 0 or
  missing is OMITTED and counted as skipped (division by zero is a data
  problem, never a crash).

Meta line shape (written once per symbol after a backfill):
  {"_meta":true,"symbol":...,"earliest":<ms>,"resolution":"1h","note":...}

Doctrine: best-effort — per-symbol self-errors, exit code always 0,
stdlib + lazy httpx import, zero repo imports, ts<1e12 x1000 normalization,
<= 1 req/s rate politeness with a small sleep between symbols.
Modes: --once (default: one 5min poll per symbol, limit=2 — dedup makes
reruns idempotent), --backfill-days N (period=1h, endTime-stepped paging
back N days, same dedup), --loop S (repeat --once every S seconds,
default 300).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

BASE_URL = "https://api.bybit.com/v5/market/account-ratio"
CATEGORY = "linear"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

REPO_ROOT = Path(__file__).resolve().parent.parent
HISTORY_PATH = REPO_ROOT / "logs" / "account_ratio.jsonl"

FIVE_MIN_MS = 5 * 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000

ONCE_PERIOD = "5min"
ONCE_LIMIT = 2
BACKFILL_PERIOD = "1h"
BACKFILL_LIMIT = 200           # rows per call
MAX_CALLS_PER_SYMBOL = 60      # hard call cap per symbol per backfill
SLEEP_S = 1.1                  # rate politeness: <= 1 req/s between calls

_SECONDS_TS_THRESHOLD = 10**12  # anything below is seconds, not ms


# ── pure helpers ─────────────────────────────────────────────────────────────


def normalize_ts_ms(raw) -> int:
    """Bybit timestamps are ms-epoch strings, but a 10-digit SECONDS value can
    appear (probe quirk on this endpoint family). Normalize to ms."""
    ts = int(raw)
    if ts < _SECONDS_TS_THRESHOLD:
        ts *= 1000
    return ts


def parse_ratio_rows(payload: dict, symbol: str,
                     fetched_at: float) -> tuple[list[dict], int]:
    """Parse a verbatim Bybit payload into record dicts sorted ASCENDING by
    open_time (the API list is newest-first; we flip it). Bad rows are
    skipped, never fatal. Rows with sell_ratio 0 or missing are omitted
    (ls_ratio undefined) and counted. Returns (records, skipped)."""
    rows = (((payload or {}).get("result") or {}).get("list")) or []
    out = []
    skipped = 0
    for row in rows:
        try:
            sell = float(row["sellRatio"])
            buy = float(row["buyRatio"])
            open_time = normalize_ts_ms(row["timestamp"])
            if sell <= 0:
                skipped += 1
                continue
            out.append({
                "symbol": symbol,
                "open_time": open_time,
                "buy_ratio": buy,
                "sell_ratio": sell,
                "ls_ratio": buy / sell,
                "fetched_at": fetched_at,
            })
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
    out.sort(key=lambda r: r["open_time"])
    return out, skipped


def read_history_keys(path: Path) -> set:
    """Existing (symbol, open_time) keys from the append-only history.
    One-bad-line doctrine: an unparseable or malformed line is skipped,
    never fatal."""
    keys = set()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("_meta"):
                        continue
                    keys.add((rec["symbol"], int(rec["open_time"])))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    continue
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return keys


def append_new_records(path: Path, records: list[dict], known_keys: set) -> int:
    """Append records whose (symbol, open_time) is not already known.
    Updates known_keys in place. Returns the number of lines appended."""
    path.parent.mkdir(parents=True, exist_ok=True)
    appended = 0
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            key = (rec["symbol"], rec["open_time"])
            if key in known_keys:
                continue
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
            known_keys.add(key)
            appended += 1
    return appended


def make_meta_line(symbol: str, earliest_ms: int, note: str) -> dict:
    return {"_meta": True, "symbol": symbol, "earliest": earliest_ms,
            "resolution": "1h", "note": note}


def atomic_write_json(path: Path, obj) -> None:
    """tmp+replace so a reader never sees a torn file. Kept for any future
    JSON state file; the history plane itself is append-only JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── network (only touched in live runs, never in tests) ─────────────────────


def fetch_page(client, symbol: str, period: str, limit: int,
               end_ms: int | None = None) -> dict:
    params = {
        "category": CATEGORY,
        "symbol": symbol,
        "period": period,
        "limit": limit,
    }
    if end_ms is not None:
        params["endTime"] = end_ms
    resp = client.get(BASE_URL, params=params, timeout=15.0)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("retCode") != 0:
        raise RuntimeError(f"bybit retCode={payload.get('retCode')} "
                           f"msg={payload.get('retMsg')}")
    return payload


# ── orchestration ────────────────────────────────────────────────────────────


def run_once(client, symbols=SYMBOLS) -> list[str]:
    """One cron tick: one 5min poll per symbol (limit=2 — dedup makes reruns
    idempotent). Self-errors per symbol; the run never exits nonzero."""
    out = []
    fetched_at = time.time()
    known_keys = read_history_keys(HISTORY_PATH)
    total_appended = 0
    total_skipped = 0

    for i, symbol in enumerate(symbols):
        try:
            payload = fetch_page(client, symbol, ONCE_PERIOD, ONCE_LIMIT)
            recs, skipped = parse_ratio_rows(payload, symbol, fetched_at)
            n = append_new_records(HISTORY_PATH, recs, known_keys)
            total_appended += n
            total_skipped += skipped
            out.append(f"once {symbol}: +{n} rows skipped={skipped}")
        except Exception as exc:  # best-effort per symbol
            print(f"account_ratio_collector once error {symbol}: {exc}",
                  file=sys.stderr)
            out.append(f"once {symbol}: ERROR {exc}")
        if i < len(symbols) - 1:
            time.sleep(SLEEP_S)  # rate politeness between symbols

    out.append(f"summary once: +{total_appended} rows "
               f"skipped={total_skipped}")
    return out


def run_backfill(days: int, client, symbols=SYMBOLS) -> list[str]:
    """Backfill N days of 1h account-ratio history per symbol, paginating by
    endTime windows (the nextPageCursor is opaque; stepping endTime to one
    interval before the oldest observed row is robust, and dedup absorbs any
    overlap). Self-errors per symbol; meta line written per symbol."""
    out = []
    now_ms = int(time.time() * 1000)
    fetched_at = time.time()
    known_keys = read_history_keys(HISTORY_PATH)
    floor_ms = int(now_ms - float(days) * DAY_MS)

    for symbol in symbols:
        calls = 0
        appended = 0
        skipped = 0
        earliest_seen = None
        try:
            end = now_ms
            while end > floor_ms and calls < MAX_CALLS_PER_SYMBOL:
                payload = fetch_page(client, symbol, BACKFILL_PERIOD,
                                     BACKFILL_LIMIT, end_ms=end)
                calls += 1
                recs, sk = parse_ratio_rows(payload, symbol, fetched_at)
                skipped += sk
                if not recs:
                    break  # exchange history floor (or all-bad page)
                appended += append_new_records(HISTORY_PATH, recs, known_keys)
                oldest = recs[0]["open_time"]  # ascending: first is oldest
                earliest_seen = (oldest if earliest_seen is None
                                 else min(earliest_seen, oldest))
                if oldest <= floor_ms:
                    break  # reached the requested depth
                end = oldest - 1  # next page strictly older than this one
                time.sleep(SLEEP_S)  # rate politeness: <= 1 req/s
            meta = make_meta_line(
                symbol,
                earliest_seen if earliest_seen is not None else floor_ms,
                f"backfill {days}d at 1h; calls={calls} appended={appended} "
                f"skipped={skipped}")
            with open(HISTORY_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(meta, separators=(",", ":")) + "\n")
            out.append(f"backfill {symbol}: calls={calls} +{appended} rows "
                       f"skipped={skipped} earliest={earliest_seen}")
        except Exception as exc:  # best-effort per symbol
            print(f"account_ratio_collector backfill error {symbol}: {exc}",
                  file=sys.stderr)
            out.append(f"backfill {symbol}: ERROR {exc}")
        time.sleep(SLEEP_S)  # rate politeness between symbols
    return out


def run_loop(interval_s: float, client, symbols=SYMBOLS) -> None:
    """Repeat --once every interval_s seconds until killed. Each iteration
    self-errors; a dead iteration never kills the loop."""
    while True:
        try:
            for line in run_once(client, symbols=symbols):
                print(line, flush=True)
        except Exception as exc:
            print(f"account_ratio_collector loop error: {exc}",
                  file=sys.stderr)
        time.sleep(interval_s)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Bybit global long/short account-ratio collector")
    parser.add_argument("--once", action="store_true", default=True,
                        help="one cron tick (default)")
    parser.add_argument("--backfill-days", type=float, default=None,
                        metavar="N",
                        help="backfill N days of 1h history, then exit")
    parser.add_argument("--loop", type=float, default=None, metavar="S",
                        nargs="?", const=300.0,
                        help="repeat --once every S seconds (default 300)")
    args = parser.parse_args(argv)

    try:
        import httpx
    except ImportError:
        print("account_ratio_collector: httpx not installed", file=sys.stderr)
        return 0  # best-effort doctrine: exit 0 always

    try:
        with httpx.Client() as client:
            if args.loop is not None:
                run_loop(args.loop, client)
                return 0
            if args.backfill_days is not None:
                lines = run_backfill(args.backfill_days, client)
            else:
                lines = run_once(client)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"account_ratio_collector fatal: {exc}", file=sys.stderr)
        return 0
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
