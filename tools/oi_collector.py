#!/usr/bin/env python3
"""oi_collector.py — Bybit V5 open-interest history plane for ARIA.

Standalone, daemon-less, cron-driven. Each invocation appends NEW open-interest
records for BTCUSDT/ETHUSDT/SOLUSDT to logs/oi_history.jsonl (append-only,
dedup by (symbol, open_time)) and rewrites logs/oi_latest.json atomically.
On first run (coverage < 30d) it backfills: recent ~2 days at 5min resolution,
deep history at 1h resolution (the mixed resolution is documented in a meta
line). Powers the OI-pullback swing strategy (entry requires OI 30d change
>= +5%); today OI exists only as an in-memory WS snapshot
(data/bybit_feed.py:462-467) with no history.

SCHEMA-OF-RECORD (operator probe 2026-09-15, verbatim):
  GET https://api.bybit.com/v5/market/open-interest
      ?category=linear&symbol=BTCUSDT&intervalTime=5min&limit=3
  -> HTTP 200
  {"retCode":0,"retMsg":"OK","result":{"symbol":"BTCUSDT","category":"linear",
   "list":[{"openInterest":"52664.64200000","singleOpenInterest":"26332.321",
            "timestamp":"1789458600000"},
           {"openInterest":"52673.34300000","singleOpenInterest":"26336.672",
            "timestamp":"1789458300000"},
           {"openInterest":"52652.41700000","singleOpenInterest":"26326.209",
            "timestamp":"1789458000"}],
   "nextPageCursor":"lastid%3D28782110%26lasttime%3D1789458000"},
   "retExtInfo":{},"time":1789458642339}

  result.list entries: {"openInterest": str (total OI, coin units),
                        "singleOpenInterest": str (one side ~= half),
                        "timestamp": str epoch}.
  List is NEWEST-FIRST. intervalTime accepts "5min" and "1h".
  A probe with startTime/endTime ~1 year back still served rows at 1h —
  history depth >= 1y, so a 30d backfill is safe. Paginate with
  startTime/endTime windows (nextPageCursor exists for deep paging but we
  pre-plan windows instead).
  PROBE QUIRK (both verbatim fixtures): the OLDEST row of a page can carry a
  10-digit SECONDS timestamp while the rest are 13-digit ms. We normalize any
  timestamp < 1e12 to ms by multiplying by 1000. Do not drop these rows.

Record shape appended to logs/oi_history.jsonl (one JSON object per line):
  {"symbol":"BTCUSDT","open_time":<ms int>,"oi":<float>,"oi_value":null,
   "fetched_at":<epoch float>}
  oi_value (USD-denominated OI) is NOT served by this endpoint — kept null;
  price-joining is a consumer's job.

Meta line shape (written once per symbol after a backfill):
  {"_meta":true,"symbol":...,"earliest":<ms>,"resolution":"5min|1h mixed",
   "note":...}

logs/oi_latest.json (atomic tmp+replace):
  {"BTCUSDT":{"oi":...,"open_time":...,"age_s":...}, ...}

Doctrine: best-effort — per-symbol self-errors, exit code always 0,
stdlib + httpx only, zero repo imports. Flags: --once (default),
--backfill-only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

BASE_URL = "https://api.bybit.com/v5/market/open-interest"
CATEGORY = "linear"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

REPO_ROOT = Path(__file__).resolve().parent.parent
HISTORY_PATH = REPO_ROOT / "logs" / "oi_history.jsonl"
LATEST_PATH = REPO_ROOT / "logs" / "oi_latest.json"

FIVE_MIN_MS = 5 * 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000

HISTORY_DAYS = 30          # total coverage target
RECENT_DAYS = 2            # recent slice kept at 5min resolution
PAGE_LIMIT = 200           # rows per call
MAX_CALLS_PER_SYMBOL = 40  # hard call cap per symbol per invocation
SLEEP_S = 0.25             # 250ms between backfill calls
ONCE_LOOKBACK_ROWS = 12    # ~1h of 5min rows for a symbol with no history yet

_SECONDS_TS_THRESHOLD = 10**12  # anything below is seconds, not ms


# ── pure helpers ─────────────────────────────────────────────────────────────


def normalize_ts_ms(raw) -> int:
    """Bybit timestamps are ms-epoch strings, but the oldest row of a page can
    arrive in SECONDS (probe quirk, both verbatim fixtures). Normalize to ms."""
    ts = int(raw)
    if ts < _SECONDS_TS_THRESHOLD:
        ts *= 1000
    return ts


def parse_oi_rows(payload: dict, symbol: str, fetched_at: float) -> list[dict]:
    """Parse a verbatim Bybit payload into record dicts sorted ASCENDING by
    open_time (the API list is newest-first; we flip it). Bad rows are
    skipped, never fatal."""
    rows = (((payload or {}).get("result") or {}).get("list")) or []
    out = []
    for row in rows:
        try:
            out.append({
                "symbol": symbol,
                "open_time": normalize_ts_ms(row["timestamp"]),
                "oi": float(row["openInterest"]),
                "oi_value": None,
                "fetched_at": fetched_at,
            })
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda r: r["open_time"])
    return out


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


def plan_backfill(now_ms: int, history_days: int = HISTORY_DAYS,
                  recent_days: int = RECENT_DAYS,
                  page_limit: int = PAGE_LIMIT) -> list[dict]:
    """Pre-planned startTime/endTime windows, NEWEST-FIRST, non-overlapping.

    Recent `recent_days` at 5min resolution; the remaining history back to
    `history_days` at 1h resolution (30d at 5min = 8,640 rows would need ~44
    calls at limit=200 — beyond the 40-call cap; at 1h it is 4 calls).
    Each window covers at most page_limit rows of its interval."""
    windows = []
    # recent slice, 5min
    end = now_ms
    floor = now_ms - recent_days * DAY_MS
    step = page_limit * FIVE_MIN_MS
    while end > floor:
        start = max(end - step + FIVE_MIN_MS, floor)
        windows.append({"interval": "5min", "start": start, "end": end})
        end = start - FIVE_MIN_MS
    # deep history, 1h
    floor = now_ms - history_days * DAY_MS
    step = page_limit * HOUR_MS
    while end > floor:
        start = max(end - step + HOUR_MS, floor)
        windows.append({"interval": "1h", "start": start, "end": end})
        end = start - HOUR_MS
    return windows


def make_meta_line(symbol: str, earliest_ms: int, note: str) -> dict:
    return {"_meta": True, "symbol": symbol, "earliest": earliest_ms,
            "resolution": "5min|1h mixed", "note": note}


def atomic_write_json(path: Path, obj) -> None:
    """tmp+replace so a reader never sees a torn file."""
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


def fetch_window(client, symbol: str, interval: str, start_ms: int,
                 end_ms: int, limit: int) -> dict:
    resp = client.get(BASE_URL, params={
        "category": CATEGORY,
        "symbol": symbol,
        "intervalTime": interval,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": limit,
    }, timeout=15.0)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("retCode") != 0:
        raise RuntimeError(f"bybit retCode={payload.get('retCode')} "
                           f"msg={payload.get('retMsg')}")
    return payload


# ── orchestration ────────────────────────────────────────────────────────────


def _needs_backfill(symbol: str, known_keys: set, now_ms: int) -> bool:
    earliest = min((ot for (sym, ot) in known_keys if sym == symbol),
                   default=None)
    if earliest is None:
        return True
    return (now_ms - earliest) < (HISTORY_DAYS - 1) * DAY_MS


def _backfill_symbol(client, symbol: str, known_keys: set, now_ms: int,
                     fetched_at: float, out: list) -> None:
    """Paginate the planned windows; append NEW records; write the meta line.
    Self-errors: any failure aborts this symbol's backfill, never the run."""
    calls = 0
    earliest_seen = None
    try:
        for win in plan_backfill(now_ms):
            if calls >= MAX_CALLS_PER_SYMBOL:
                break
            payload = fetch_window(client, symbol, win["interval"],
                                   win["start"], win["end"], PAGE_LIMIT)
            calls += 1
            recs = parse_oi_rows(payload, symbol, fetched_at)
            if recs:
                earliest_seen = (recs[0]["open_time"] if earliest_seen is None
                                 else min(earliest_seen,
                                          recs[0]["open_time"]))
            append_new_records(HISTORY_PATH, recs, known_keys)
            if not recs:
                break  # empty window = exchange history floor reached
            time.sleep(SLEEP_S)
        target_earliest = now_ms - HISTORY_DAYS * DAY_MS
        earliest = earliest_seen if earliest_seen is not None else target_earliest
        note = (f"backfill {HISTORY_DAYS}d: recent {RECENT_DAYS}d at 5min, "
                f"deep history at 1h (5min 30d = 8640 rows exceeds the "
                f"{MAX_CALLS_PER_SYMBOL}-call cap); calls={calls}")
        meta = make_meta_line(symbol, earliest, note)
        with open(HISTORY_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(meta, separators=(",", ":")) + "\n")
        out.append(f"backfill {symbol}: calls={calls} earliest={earliest}")
    except Exception as exc:  # best-effort per symbol
        print(f"oi_collector backfill error {symbol}: {exc}", file=sys.stderr)
        out.append(f"backfill {symbol}: ERROR {exc}")


def _read_latest(path: Path) -> dict:
    """Prior oi_latest.json, tolerantly. Carried forward so a per-symbol
    fetch failure never erases the last good reading (absence would look
    like 'no data' instead of 'stale data')."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def run_once(client, symbols=SYMBOLS) -> list[str]:
    """One cron tick: backfill any under-covered symbol, then append the
    latest 5min rows and rewrite oi_latest.json. Self-errors per symbol.

    Self-heal: the lookback is ADAPTIVE — it starts at the symbol's newest
    known open_time (dedup absorbs the overlap), so an outage of N hours is
    replayed, not silently stranded. Holes deeper than one page
    (PAGE_LIMIT rows ~= 16.7h at 5min) replay the planned backfill windows;
    dedup makes that idempotent. The old fixed 12-row (1h) lookback left
    any outage > 1h permanently missing."""
    out = []
    now_ms = int(time.time() * 1000)
    fetched_at = time.time()
    known_keys = read_history_keys(HISTORY_PATH)

    for symbol in symbols:
        try:
            if _needs_backfill(symbol, known_keys, now_ms):
                _backfill_symbol(client, symbol, known_keys, now_ms,
                                 fetched_at, out)
        except Exception as exc:
            print(f"oi_collector coverage error {symbol}: {exc}",
                  file=sys.stderr)

    latest = _read_latest(LATEST_PATH)
    for symbol in symbols:
        try:
            newest_known = max((ot for (sym, ot) in known_keys
                                if sym == symbol), default=None)
            if newest_known is None:
                start = now_ms - ONCE_LOOKBACK_ROWS * FIVE_MIN_MS
                limit = ONCE_LOOKBACK_ROWS
            else:
                gap_rows = (now_ms - newest_known) // FIVE_MIN_MS + 2
                if gap_rows > PAGE_LIMIT:
                    _backfill_symbol(client, symbol, known_keys, now_ms,
                                     fetched_at, out)
                start = newest_known  # dedup absorbs the overlap row
                limit = int(min(PAGE_LIMIT, max(ONCE_LOOKBACK_ROWS,
                                                gap_rows)))
            payload = fetch_window(client, symbol, "5min", start, now_ms,
                                   limit)
            recs = parse_oi_rows(payload, symbol, fetched_at)
            n = append_new_records(HISTORY_PATH, recs, known_keys)
            if recs:
                newest = recs[-1]
                latest[symbol] = {
                    "oi": newest["oi"],
                    "open_time": newest["open_time"],
                    "age_s": round(fetched_at - newest["open_time"] / 1000.0, 1),
                }
            out.append(f"once {symbol}: +{n} rows")
        except Exception as exc:  # best-effort per symbol
            print(f"oi_collector once error {symbol}: {exc}", file=sys.stderr)
            out.append(f"once {symbol}: ERROR {exc}")

    # Carried-forward entries keep their open_time but their age must keep
    # growing — recompute so a stale read is honestly stale.
    for ent in latest.values():
        try:
            ent["age_s"] = round(fetched_at - ent["open_time"] / 1000.0, 1)
        except Exception:
            pass
    try:
        atomic_write_json(LATEST_PATH, latest)
    except Exception as exc:
        print(f"oi_collector latest write error: {exc}", file=sys.stderr)
        out.append(f"latest write: ERROR {exc}")
    return out


def run_backfill_only(client, symbols=SYMBOLS) -> list[str]:
    out = []
    now_ms = int(time.time() * 1000)
    fetched_at = time.time()
    known_keys = read_history_keys(HISTORY_PATH)
    for symbol in symbols:
        if _needs_backfill(symbol, known_keys, now_ms):
            _backfill_symbol(client, symbol, known_keys, now_ms,
                             fetched_at, out)
        else:
            out.append(f"backfill {symbol}: coverage OK, skipped")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Bybit open-interest collector")
    parser.add_argument("--once", action="store_true", default=True,
                        help="one cron tick (default)")
    parser.add_argument("--backfill-only", action="store_true",
                        help="only run the 30d backfill, skip the latest fetch")
    args = parser.parse_args(argv)

    try:
        import httpx
    except ImportError:
        print("oi_collector: httpx not installed", file=sys.stderr)
        return 0  # best-effort doctrine: exit 0 always

    try:
        with httpx.Client() as client:
            lines = (run_backfill_only(client) if args.backfill_only
                     else run_once(client))
    except Exception as exc:
        print(f"oi_collector fatal: {exc}", file=sys.stderr)
        return 0
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
