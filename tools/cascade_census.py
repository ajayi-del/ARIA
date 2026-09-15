#!/usr/bin/env python3
"""cascade_census.py — event-counted conditional cascade probability (Governor
spec 2026-09-15, verbatim). Observer-class: zero trade-path wiring, zero new
gates. Answers, per symbol and pooled:

    P(down-cascade within 7d | pre-cascade score >= SCORE_THRESHOLD)

for BTCUSDT/ETHUSDT/SOLUSDT on Bybit public REST (no auth).

ESTIMAND DEFINITIONS (spec ambiguities resolved, documented for the Governor):
  * "pre-cascade score" is evaluated at the bar IMMEDIATELY BEFORE the cascade
    bar (no look-ahead — the cascade bar's own OI drop would contaminate the
    +OI-6h leg if scored at the trigger bar itself).
  * "down-cascade within 7d" = the forward-label further_down flag: any later
    1h close pct change <= -2% inside the 168h horizon (the only boolean the
    spec's forward-labeling block defines).
  * CI is the Wilson 95% SCORE interval (n will be 5-25; normal approximation
    banned). NOTE: the spec's hand-check "n=10 k=5 -> 0.299-0.701" is the
    Wilson CI for n=20 k=10; the correct n=10 k=5 interval is (0.2366, 0.7634).
    Both are pinned in tests/test_cascade_census.py.

DATA (paginate ~--days back each, 1h grid from the klines):
  kline         /v5/market/kline?category=linear&symbol=&interval=60&limit=200&end=
  OI            /v5/market/open-interest?category=linear&symbol=&intervalTime=1h&limit=200&endTime=
  funding       /v5/market/funding/history?category=linear&symbol=&limit=200&endTime=
                (8h cadence, settlements used directly for the funding legs)
  account-ratio /v5/market/account-ratio?category=linear&symbol=&period=1h&limit=200
                (L/S = buyRatio/sellRatio, ffill onto the 1h grid)

VERBATIM FIXTURES-OF-RECORD (live-probed 2026-09-15):
  account-ratio: {"retCode":0,"result":{"list":[{"symbol":"ETHUSDT",
    "buyRatio":"0.6784","sellRatio":"0.3216","timestamp":"1789460400000"},...],
    "nextPageCursor":"..."}} — newest-first, strings.
  funding: {"retCode":0,"result":{"category":"linear","list":[{"symbol":
    "ETHUSDT","fundingRate":"-0.00006352","fundingRateTimestamp":
    "1789459200000"},...]}} — newest-first, strings, 8h cadence.
  OI: {"retCode":0,"result":{"list":[{"openInterest":"52664.64200000",
    "singleOpenInterest":"26332.321","timestamp":"1789458600000"},...],
    "nextPageCursor":...}} — newest-first, strings.
  kline: {"retCode":0,"result":{"list":[["1789459200000","2481.5","2490.2",
    "2477.0","2488.1","1234.5","3067890.1"],...]}} — [start_ms, open, high,
    low, close, volume, turnover] strings, newest-first.

PRE-CASCADE SCORE (0-4) at each 1h timestamp t (using data at or before t):
  +1 latest funding settlement < 0 (exactly 0 is NOT negative)
  +1 the two settlements PRIOR to the latest are both < 0
  +1 OI 6h pct change > 0 (exactly 0 is not > 0)
  +1 L/S > 1.8 (STRICTLY greater — exactly 1.8 fails)

CASCADE EVENT at bar i: 1h OI pct change <= -3% AND 1h close pct change
  <= -2% in the same bar (both boundaries INCLUSIVE).
QUALIFYING TRIGGER: cascade bar whose PRE-cascade score >= threshold.
FORWARD LABEL per trigger over the next 168h: min/max/end close, further_down,
  drawdown_7d = (min_close - entry_close)/entry_close, return_7d likewise with
  the end close. Triggers with < 84h (half horizon) of forward data are
  skipped (counted, not scored).

OUTPUTS: logs/cascade_census.json (atomic tmp+replace) + one history line to
logs/cascade_census_history.jsonl + a compact summary table on stdout.
Per symbol: n cascade events, n qualifying triggers, n labeled, conditional P
with Wilson 95% CI, avg/median/p75 drawdown_7d, trigger hour-UTC histogram,
thin_sample flag (n_labeled < 50) with the honest read. Plus a pooled
all-symbols section. Best-effort doctrine: per-symbol self-errors, exit 0.

Cron (install is the local node's job, NOT this tool's):
  13 2 * * * cd /home/dayodapper/ARIA && .venv/bin/python tools/cascade_census.py --once >> logs/cascade_census.log 2>&1

CLI: --days (90) --threshold (4) --symbols (BTCUSDT,ETHUSDT,SOLUSDT) --once.
Pure computation is separated from network fetchers; everything except HTTP
takes plain lists of (ts_ms, value) tuples / bar tuples. Stdlib + lazy httpx.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

BASE = "https://api.bybit.com/v5/market"
KLINE_URL = BASE + "/kline"
OI_URL = BASE + "/open-interest"
FUNDING_URL = BASE + "/funding/history"
ACCOUNT_RATIO_URL = BASE + "/account-ratio"
CATEGORY = "linear"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

REPO_ROOT = Path(__file__).resolve().parent.parent
CENSUS_PATH = REPO_ROOT / "logs" / "cascade_census.json"
HISTORY_PATH = REPO_ROOT / "logs" / "cascade_census_history.jsonl"

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
PAGE_LIMIT = 200
MAX_CALLS_PER_SERIES = 40
SLEEP_S = 0.25
HORIZON_H = 168            # 7d forward window
MIN_FORWARD_H = HORIZON_H // 2
OI_LOOKBACK_H = 6
OI_DROP = -0.03            # cascade OI leg (inclusive)
PX_DROP = -0.02            # cascade price leg (inclusive) + further-down flag
CASCADE_LAG_BARS = 1       # OI flush window: bars [T, T+LAG] after the price bar
# POLEMARCH STAMP (measured 2026-09-15 on the live ETH 1h grid, 30d): the
# spec's "same 1h window" definition counted ZERO events in 180d because the
# cascade is a 2-bar event — price prints the dump in bar T, liquidations
# settle into the OI series at the NEXT bar close (all 10 OI<=-3% bars in
# the probe had a benign same-bar price print; every PX<=-2% bar was
# followed by an OI flush within 1h: 08-22 05:00 px -2.96% -> 06:00 oi
# -3.94%; 08-30 23:00 px -2.19% -> 00:00 oi -3.49%; 09-04 12:00 px -2.85%
# -> 13:00 oi -5.44%). LAG=1 is the physically-honest joint window;
# LAG=0 reproduces the spec's same-bar reading bit-for-bit.
LS_THRESHOLD = 1.8         # strictly greater
THIN_FLOOR = 50
THIN_NOTE = ("CI too wide to trade on — extend lookback or pool symbols")
_SECONDS_TS_THRESHOLD = 10**12


# ── pure helpers: parse (verbatim fixtures are the spec) ─────────────────────


def normalize_ts_ms(raw) -> int:
    """Bybit ts strings are ms-epoch, but a page's oldest row can arrive in
    SECONDS (oi_collector probe quirk). Normalize to ms; never drop the row."""
    ts = int(raw)
    if ts < _SECONDS_TS_THRESHOLD:
        ts *= 1000
    return ts


def parse_kline_rows(payload: dict) -> list[tuple]:
    """[(ts_ms, open, high, low, close)] ascending. Bad rows skipped."""
    rows = (((payload or {}).get("result") or {}).get("list")) or []
    out = []
    for r in rows:
        try:
            out.append((normalize_ts_ms(r[0]), float(r[1]), float(r[2]),
                        float(r[3]), float(r[4])))
        except (IndexError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x[0])
    return out


def parse_oi_rows(payload: dict) -> list[tuple]:
    """[(ts_ms, oi)] ascending."""
    rows = (((payload or {}).get("result") or {}).get("list")) or []
    out = []
    for r in rows:
        try:
            out.append((normalize_ts_ms(r["timestamp"]),
                        float(r["openInterest"])))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x[0])
    return out


def parse_funding_rows(payload: dict) -> list[tuple]:
    """[(ts_ms, rate)] ascending (8h cadence)."""
    rows = (((payload or {}).get("result") or {}).get("list")) or []
    out = []
    for r in rows:
        try:
            out.append((normalize_ts_ms(r["fundingRateTimestamp"]),
                        float(r["fundingRate"])))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x[0])
    return out


def parse_account_ratio_rows(payload: dict) -> list[tuple]:
    """[(ts_ms, long_short)] ascending; L/S = buyRatio/sellRatio. A zero
    sellRatio row is skipped (infinite ratio carries no numeric value)."""
    rows = (((payload or {}).get("result") or {}).get("list")) or []
    out = []
    for r in rows:
        try:
            sell = float(r["sellRatio"])
            if sell == 0.0:
                continue
            out.append((normalize_ts_ms(r["timestamp"]),
                        float(r["buyRatio"]) / sell))
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x[0])
    return out


# ── pure helpers: alignment, score, events, labels, stats ────────────────────


def ffill_to_grid(grid_ts: list[int], points: list[tuple]) -> list:
    """Forward-fill (ts, value) points onto the grid: for each grid ts the
    value of the latest point with point.ts <= grid.ts; None before the
    first point. Ragged inputs are fine; points must be ascending."""
    out = []
    j = 0
    last = None
    for t in grid_ts:
        while j < len(points) and points[j][0] <= t:
            last = points[j][1]
            j += 1
        out.append(last)
    return out


def compute_scores(grid_ts: list[int], funding_pts: list[tuple],
                   oi_vals: list, ls_vals: list,
                   oi_lookback: int = OI_LOOKBACK_H,
                   ls_threshold: float = LS_THRESHOLD) -> list[int]:
    """The 0-4 pre-cascade score at each grid index. oi_vals/ls_vals are
    already ffilled onto the grid; funding_pts are raw (ts, rate) settlements
    (a settlement applies from its own timestamp forward). Missing data legs
    are False, never fatal."""
    funding_pts = sorted(funding_pts, key=lambda x: x[0])
    scores = []
    f_idx = 0
    for i, t in enumerate(grid_ts):
        while f_idx < len(funding_pts) and funding_pts[f_idx][0] <= t:
            f_idx += 1
        # settlements at or before t are funding_pts[:f_idx]
        leg1 = f_idx >= 1 and funding_pts[f_idx - 1][1] < 0
        leg2 = (f_idx >= 3 and funding_pts[f_idx - 2][1] < 0
                and funding_pts[f_idx - 3][1] < 0)
        leg3 = False
        if i >= oi_lookback:
            prev = oi_vals[i - oi_lookback]
            if prev not in (None, 0) and oi_vals[i] is not None:
                leg3 = (oi_vals[i] - prev) / prev > 0
        leg4 = ls_vals[i] is not None and ls_vals[i] > ls_threshold
        scores.append(int(leg1) + int(leg2) + int(leg3) + int(leg4))
    return scores


def find_cascade_events(closes: list[float], oi_vals: list,
                        oi_drop: float = OI_DROP,
                        px_drop: float = PX_DROP,
                        lag_bars: int = CASCADE_LAG_BARS) -> list[int]:
    """Grid indices i (the PRICE bar) where the 1h close pct change <= px_drop
    AND the 1h OI pct change <= oi_drop in any bar of [i, i+lag_bars].
    Boundaries INCLUSIVE. lag_bars=0 reproduces the spec's same-bar reading;
    lag_bars=1 (default) matches settlement mechanics — the OI flush prints
    one bar after the price dump (see the constants-block stamp)."""
    events = []
    n = len(closes)
    for i in range(1, n):
        if closes[i - 1] == 0:
            continue
        px_chg = (closes[i] - closes[i - 1]) / closes[i - 1]
        if px_chg > px_drop:
            continue
        hit = False
        for j in range(i, min(i + lag_bars + 1, n)):
            if oi_vals[j] is None or oi_vals[j - 1] in (None, 0):
                continue
            if (oi_vals[j] - oi_vals[j - 1]) / oi_vals[j - 1] <= oi_drop:
                hit = True
                break
        if hit:
            events.append(i)
    return events


def forward_label(i: int, closes: list[float], horizon: int = HORIZON_H,
                  min_forward: int = MIN_FORWARD_H,
                  further_drop: float = PX_DROP):
    """Forward label for a trigger at grid index i over the next `horizon`
    bars. None when fewer than `min_forward` bars of forward data exist.
    further_down = any later 1h close pct change <= further_drop."""
    available = len(closes) - 1 - i
    if available < min_forward:
        return None
    end = min(len(closes) - 1, i + horizon)
    window = closes[i + 1:end + 1]
    entry = closes[i]
    min_c, max_c, end_c = min(window), max(window), window[-1]
    further = False
    for j in range(i + 1, end + 1):
        if closes[j - 1] and (closes[j] - closes[j - 1]) / closes[j - 1] \
                <= further_drop:
            further = True
            break
    return {"min_close": min_c, "max_close": max_c, "end_close": end_c,
            "drawdown_7d": (min_c - entry) / entry if entry else None,
            "return_7d": (end_c - entry) / entry if entry else None,
            "further_down": further, "forward_bars": end - i}


def wilson_interval(k: int, n: int, z: float = 1.96):
    """Wilson 95% score interval. n=0 -> None (never nan)."""
    if n <= 0:
        return None
    phat = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (phat + z2 / (2.0 * n)) / denom
    half = z * math.sqrt(phat * (1.0 - phat) / n + z2 / (4.0 * n * n)) / denom
    return (center - half, center + half)


def percentile_nearest_rank(vals: list[float], q: float = 0.75):
    """Nearest-rank percentile: sorted[ceil(q*n)-1]. Deterministic and
    hand-computable; None on empty input."""
    if not vals:
        return None
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, math.ceil(q * len(s)) - 1))
    return s[idx]


def drawdown_stats(dds: list[float]):
    if not dds:
        return None
    return {"n": len(dds), "avg": sum(dds) / len(dds),
            "median": statistics.median(dds),
            "p75": percentile_nearest_rank(dds, 0.75)}


def thin_sample(n: int, floor: int = THIN_FLOOR):
    """(flag, note). n >= floor -> (False, None)."""
    if n < floor:
        return True, THIN_NOTE
    return False, None


def hour_histogram(ts_ms_list: list[int]) -> dict:
    """Trigger hour-UTC histogram; all 24 hours present for a stable schema."""
    h = {str(hh): 0 for hh in range(24)}
    for t in ts_ms_list:
        h[str((t // HOUR_MS) % 24)] += 1
    return h


def build_symbol_report(grid_ts: list[int], closes: list[float],
                        funding_pts: list[tuple], oi_vals: list,
                        ls_vals: list, threshold: int,
                        horizon: int = HORIZON_H) -> dict:
    """Full census for one symbol over one aligned grid. The trigger's score
    is the PRE-cascade score at index i-1 (no look-ahead)."""
    scores = compute_scores(grid_ts, funding_pts, oi_vals, ls_vals)
    events = find_cascade_events(closes, oi_vals)
    triggers = [i for i in events if i >= 1 and scores[i - 1] >= threshold]
    labels, skipped = [], 0
    for i in triggers:
        lab = forward_label(i, closes, horizon=horizon)
        if lab is None:
            skipped += 1
            continue
        lab = dict(lab)
        lab["ts"] = grid_ts[i]
        lab["score"] = scores[i - 1]
        labels.append(lab)
    n = len(labels)
    k = sum(1 for l in labels if l["further_down"])
    ci = wilson_interval(k, n)
    flag, note = thin_sample(n)
    dds = [l["drawdown_7d"] for l in labels if l["drawdown_7d"] is not None]
    return {"n_cascade_events": len(events),
            "n_qualifying_triggers": len(triggers),
            "n_skipped_forward": skipped,
            "n_labeled": n,
            "n_further_down": k,
            "conditional_p": (k / n) if n else None,
            "wilson95": [round(ci[0], 4), round(ci[1], 4)] if ci else None,
            "drawdown_7d": drawdown_stats(dds),
            "trigger_hour_utc_hist": hour_histogram([l["ts"] for l in labels]),
            "thin_sample": flag, "note": note,
            "threshold": threshold,
            "triggers": labels}


def pool_reports(reps: dict) -> dict:
    """Pooled all-symbols section. Exact pooling from the per-symbol trigger
    lists (counts, P, Wilson, drawdown stats, histogram all recomputed)."""
    labels = []
    n_events = n_qual = n_skip = 0
    for rep in reps.values():
        if not isinstance(rep, dict) or "triggers" not in rep:
            continue
        n_events += rep.get("n_cascade_events", 0)
        n_qual += rep.get("n_qualifying_triggers", 0)
        n_skip += rep.get("n_skipped_forward", 0)
        labels.extend(rep["triggers"])
    n = len(labels)
    k = sum(1 for l in labels if l["further_down"])
    ci = wilson_interval(k, n)
    flag, note = thin_sample(n)
    dds = [l["drawdown_7d"] for l in labels if l["drawdown_7d"] is not None]
    hist = {str(hh): 0 for hh in range(24)}
    for rep in reps.values():
        if not isinstance(rep, dict):
            continue
        for hh, c in (rep.get("trigger_hour_utc_hist") or {}).items():
            hist[hh] = hist.get(hh, 0) + c
    return {"n_cascade_events": n_events,
            "n_qualifying_triggers": n_qual,
            "n_skipped_forward": n_skip,
            "n_labeled": n,
            "n_further_down": k,
            "conditional_p": (k / n) if n else None,
            "wilson95": [round(ci[0], 4), round(ci[1], 4)] if ci else None,
            "drawdown_7d": drawdown_stats(dds),
            "trigger_hour_utc_hist": hist,
            "thin_sample": flag, "note": note,
            "symbols": sorted(reps.keys())}


# ── io helpers ────────────────────────────────────────────────────────────────


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


def append_history(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")


# ── network (only touched in live runs, never in tests) ──────────────────────


def get_json(client, url: str, params: dict) -> dict:
    resp = client.get(url, params=params, timeout=15.0)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("retCode") != 0:
        raise RuntimeError(f"bybit retCode={payload.get('retCode')} "
                           f"msg={payload.get('retMsg')}")
    return payload


def fetch_paginated(client, url: str, base_params: dict, end_key: str,
                    floor_ms: int, parser, max_calls: int = MAX_CALLS_PER_SERIES,
                    sleep_s: float = SLEEP_S):
    """Walk backwards page by page: each call sets end_key = oldest_seen - 1ms
    until the floor is reached. No-progress guard: if a page's oldest ts fails
    to move backwards (endpoint ignores the end param), stop after the repeat
    — the rows fetched are still served, honestly. Returns (rows, calls)."""
    rows_by_ts = {}
    end = None
    prev_oldest = None
    calls = 0
    while calls < max_calls:
        params = dict(base_params)
        if end is not None:
            params[end_key] = end
        payload = get_json(client, url, params)
        calls += 1
        rows = parser(payload)
        if not rows:
            break
        for r in rows:
            rows_by_ts[r[0]] = r
        oldest = rows[0][0]
        if prev_oldest is not None and oldest >= prev_oldest:
            break
        prev_oldest = oldest
        if oldest <= floor_ms:
            break
        end = oldest - 1
        time.sleep(sleep_s)
    return sorted(rows_by_ts.values(), key=lambda r: r[0]), calls


def fetch_symbol_series(client, symbol: str, days: int, now_ms: int) -> dict:
    floor = now_ms - days * DAY_MS
    klines, c1 = fetch_paginated(
        client, KLINE_URL,
        {"category": CATEGORY, "symbol": symbol, "interval": "60",
         "limit": PAGE_LIMIT}, "end", floor, parse_kline_rows)
    if len(klines) < 10:
        raise RuntimeError(f"insufficient klines ({len(klines)})")
    oi_pts, c2 = fetch_paginated(
        client, OI_URL,
        {"category": CATEGORY, "symbol": symbol, "intervalTime": "1h",
         "limit": PAGE_LIMIT}, "endTime", floor, parse_oi_rows)
    f_pts, c3 = fetch_paginated(
        client, FUNDING_URL,
        {"category": CATEGORY, "symbol": symbol, "limit": PAGE_LIMIT},
        "endTime", floor, parse_funding_rows)
    ls_pts, c4 = fetch_paginated(
        client, ACCOUNT_RATIO_URL,
        {"category": CATEGORY, "symbol": symbol, "period": "1h",
         "limit": PAGE_LIMIT}, "endTime", floor, parse_account_ratio_rows)
    return {"klines": klines, "oi": oi_pts, "funding": f_pts, "ls": ls_pts,
            "calls": c1 + c2 + c3 + c4}


# ── orchestration ─────────────────────────────────────────────────────────────


def run_symbol(client, symbol: str, days: int, threshold: int,
               now_ms: int) -> dict:
    series = fetch_symbol_series(client, symbol, days, now_ms)
    klines = series["klines"]
    grid_ts = [k[0] for k in klines]
    closes = [k[4] for k in klines]
    oi_vals = ffill_to_grid(grid_ts, series["oi"])
    ls_vals = ffill_to_grid(grid_ts, series["ls"])
    rep = build_symbol_report(grid_ts, closes, series["funding"],
                              oi_vals, ls_vals, threshold)
    rep["coverage"] = {"kline_rows": len(klines), "oi_rows": len(series["oi"]),
                       "funding_rows": len(series["funding"]),
                       "ls_rows": len(series["ls"]),
                       "calls": series["calls"],
                       "grid_earliest": grid_ts[0], "grid_latest": grid_ts[-1]}
    return rep


def _fmt_p(p):
    return f"{p:.3f}" if p is not None else "  —  "


def _fmt_ci(ci):
    return f"[{ci[0]:.3f},{ci[1]:.3f}]" if ci else "     —      "


def _row(tag, rep):
    if "error" in rep:
        return f"{tag:<9} ERROR {rep['error'][:60]}"
    dd = rep.get("drawdown_7d") or {}
    avg = dd.get("avg")
    return (f"{tag:<9} ev={rep['n_cascade_events']:<4} "
            f"trg={rep['n_qualifying_triggers']:<4} "
            f"lab={rep['n_labeled']:<4} "
            f"P={_fmt_p(rep['conditional_p']):<6} "
            f"CI95={_fmt_ci(rep['wilson95']):<15} "
            f"avgDD7d={(f'{avg*100:.2f}%' if avg is not None else '  — '):<8} "
            f"{'THIN' if rep['thin_sample'] else 'ok'}")


def format_table(out: dict) -> list[str]:
    lines = [f"cascade_census days={out['days']} threshold={out['threshold']} "
             f"utc={out['utc']}"]
    for sym in sorted(out["symbols"]):
        lines.append(_row(sym, out["symbols"][sym]))
    lines.append(_row("POOLED", out["pooled"]))
    return lines


def run_once(client, symbols, days: int, threshold: int) -> dict:
    now = time.time()
    now_ms = int(now * 1000)
    out = {"ts": now,
           "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
           "days": days, "threshold": threshold, "horizon_h": HORIZON_H,
           "symbols": {}}
    for symbol in symbols:
        try:
            out["symbols"][symbol] = run_symbol(client, symbol, days,
                                                threshold, now_ms)
        except Exception as exc:  # best-effort per symbol
            print(f"cascade_census error {symbol}: {exc}", file=sys.stderr)
            out["symbols"][symbol] = {"error": str(exc)[:200]}
    out["pooled"] = pool_reports({s: r for s, r in out["symbols"].items()
                                  if "error" not in r})
    try:
        atomic_write_json(CENSUS_PATH, out)
    except Exception as exc:
        print(f"cascade_census write error: {exc}", file=sys.stderr)
    try:
        hist = {"ts": now, "days": days, "threshold": threshold,
                "symbols": {s: {"n_ev": r.get("n_cascade_events"),
                                "n_trg": r.get("n_qualifying_triggers"),
                                "n_lab": r.get("n_labeled"),
                                "p": r.get("conditional_p"),
                                "ci": r.get("wilson95")}
                            for s, r in out["symbols"].items()
                            if "error" not in r},
                "pooled": {"n_lab": out["pooled"]["n_labeled"],
                           "p": out["pooled"]["conditional_p"],
                           "ci": out["pooled"]["wilson95"]}}
        append_history(HISTORY_PATH, hist)
    except Exception as exc:
        print(f"cascade_census history error: {exc}", file=sys.stderr)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Event-counted conditional cascade probability census")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--threshold", type=int, default=4)
    parser.add_argument("--symbols", type=str, default=",".join(SYMBOLS),
                        help="comma-separated (default all three)")
    parser.add_argument("--once", action="store_true", default=True,
                        help="one cron tick (default; census is one-shot)")
    args = parser.parse_args(argv)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    try:
        import httpx
    except ImportError:
        print("cascade_census: httpx not installed", file=sys.stderr)
        return 0  # best-effort doctrine: exit 0 always

    try:
        with httpx.Client() as client:
            out = run_once(client, symbols, args.days, args.threshold)
    except Exception as exc:
        print(f"cascade_census fatal: {exc}", file=sys.stderr)
        return 0
    for line in format_table(out):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
