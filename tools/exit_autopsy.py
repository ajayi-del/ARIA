#!/usr/bin/env python3
"""exit_autopsy — counterfactual scoring for EVERY close, not just stop-class.

The shadow journal measures gate REFUSALS (entry side). Exits had no mirror:
a close is a refusal of the "continue holding" trade, and until now only
conviction_decay abandons carried that counterfactual (one wire, unproven).
This tool closes the gap deterministically — no model, no trade path.

For every journaled close on a UTC day:
  exit reference  = public-kline close of the 1m bar containing closed_at
                    (independent plane, same doctrine as digest slippage)
  hold leg        = kline path at 1h / 4h / 24h horizons (pending if the
                    close is younger than the horizon)
  stop realism    = if the journal carries stop_price AND the kline plane
                    matches (bybit/aster), the hold leg ends at the stop
                    when the window crosses it. Yahoo-underlying tradfi is
                    cross-plane (SPY vs rebased SPCX) — stop check skipped,
                    pct moves still plane-safe.
  USD conversion  = hold_ret_pct x journal-plane notional at exit
                    (size x implied exit from entry + gross pnl), so
                    cross-plane tradfi rows stay honest.

Aggregates per exit class with the Aronson bar: n>=10 before any verdict,
and a multiple-comparison note (classes x horizons cells are exploratory).
Best-effort doctrine: kline gaps skip a row, never fabricate; exit 0 always.

Output: logs/exit_autopsy.json (atomic) + one history line per run in
logs/exit_autopsy_history.jsonl. The watchdog reads this in the EV scan;
verdicts feed proposals with n + effect size. Observer-class: zero wiring.

Usage: python3 tools/exit_autopsy.py [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)  # lazy repo imports work when run as a script

LOG_DIR = os.path.join(REPO_ROOT, "logs")
OUT_PATH = os.path.join(LOG_DIR, "exit_autopsy.json")
HIST_PATH = os.path.join(LOG_DIR, "exit_autopsy_history.jsonl")

BYBIT_KLINE = "https://api.bybit.com/v5/market/kline"
ASTER_KLINE = "https://fapi.asterdex.com/fapi/v1/klines"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}"

HORIZONS_S = {"1h": 3600, "4h": 4 * 3600, "24h": 24 * 3600}
MAX_FETCH = 30           # closes scored per run, newest first
MIN_EFFECT_USD = 0.05    # |delta| below this is noise, not regret/saved


def pnl_gross(r: dict) -> float:
    v = r.get("pnl_usd")
    if isinstance(v, (int, float)):
        return float(v)
    v = r.get("realized_pnl_usd")
    return float(v) if isinstance(v, (int, float)) else 0.0


def pnl_net(r: dict) -> float:
    v = r.get("pnl_net_usd")
    if isinstance(v, (int, float)):
        return float(v)
    return pnl_gross(r)


def exit_class(reason: str) -> str:
    """Normalize exit_reason to a class bucket. Prefix before ':' kills
    parameter spam (time_stop_loser_momentum_cont_120min -> time_stop)."""
    r = (reason or "unknown").lower()
    for bucket in ("time_stop", "external_close", "exchange_close",
                   "software_stop", "conviction_decay", "portfolio_loss_cut",
                   "treasury", "trailing", "stop_hit", "native_stop",
                   "tp1", "tp2", "tp3", "native_tp", "roe_ratchet"):
        if bucket in r:
            return bucket
    return r.split(":")[0][:32] or "unknown"


def venue_classifier():
    """Lazy repo imports — module stays loadable without pydantic/env.
    Mirrors daily_digest.venue_classifier exactly (same planes, same skips)."""
    try:
        from core.config import Settings
        from data.tradfi_feed import TRADFI_SYMBOLS
        from execution.aster_client import to_aster_symbol
        cfg = Settings()
        aster = set(getattr(cfg, "aster_assets", []))

        def venue_of(sym: str) -> str:
            if "SSI" in sym:
                return "skip"
            if sym in aster:
                return "aster"
            if sym in TRADFI_SYMBOLS:
                return "yahoo"
            return "bybit"

        def yahoo_of(sym: str) -> str:
            return TRADFI_SYMBOLS.get(sym, "")

        return venue_of, yahoo_of, to_aster_symbol
    except Exception:
        return lambda s: "skip", lambda s: "", lambda s: s


def load_journal_closes(day: str) -> list[dict]:
    path = os.path.join(LOG_DIR, f"trade_journal_{day}.json")
    try:
        recs = json.load(open(path))
    except Exception:
        return []
    closes = [r for r in recs
              if r.get("outcome") in ("win", "loss")
              and r.get("closed_at_ms") and r.get("entry_price")
              and r.get("position_size")]
    # journal-integrity class: rolling-window day-file overlap dupes inflate
    # every census. Dedup by (entry_id, closed_at_ms) — same key as the digest.
    seen: set = set()
    deduped = []
    for r in closes:
        key = (r.get("entry_id"), r.get("closed_at_ms"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    deduped.sort(key=lambda r: int(r["closed_at_ms"]), reverse=True)
    return deduped


async def fetch_bars(client, venue: str, symbol: str, start_ms: int,
                     yahoo_sym: str = "") -> list[tuple[int, float, float, float]]:
    """[(open_ms, high, low, close)] from start_ms. Same endpoints as the
    digest's kline legs; sorted ascending; empty on any failure."""
    out: list[tuple[int, float, float, float]] = []
    try:
        if venue == "bybit":
            r = await client.get(BYBIT_KLINE, params={
                "category": "linear",
                "symbol": f"{symbol.replace('-USD', '')}USDT",
                "interval": "1", "start": start_ms, "limit": 1000})
            rows = (r.json().get("result") or {}).get("list") or []
            out = [(int(k[0]), float(k[2]), float(k[3]), float(k[4])) for k in rows]
        elif venue == "aster":
            r = await client.get(ASTER_KLINE, params={
                "symbol": symbol, "interval": "1m",
                "startTime": start_ms, "limit": 1000})
            out = [(int(k[0]), float(k[2]), float(k[3]), float(k[4]))
                   for k in r.json()]
        elif venue == "yahoo":
            r = await client.get(YAHOO_CHART.format(yahoo_sym),
                                 params={"interval": "1m", "range": "5d"})
            result = (r.json().get("chart", {}).get("result") or [None])[0]
            if result:
                ts = result.get("timestamp") or []
                q = (result.get("indicators", {}).get("quote") or [{}])[0]
                for t, h, l, c in zip(ts, q.get("high") or [],
                                      q.get("low") or [], q.get("close") or []):
                    if None not in (h, l, c) and int(t) * 1000 >= start_ms:
                        out.append((int(t) * 1000, float(h), float(l), float(c)))
        out.sort(key=lambda x: x[0])
    except Exception:
        pass
    return out


def bar_at_or_before(bars, t_ms: int):
    cand = [b for b in bars if b[0] <= t_ms]
    return cand[-1] if cand else None


def score_close(r: dict, bars, now_ms: int) -> dict | None:
    """Counterfactual hold-vs-close for one journal close. None = unmeasurable."""
    sym = r["symbol"]
    direction = r.get("direction")
    sign = 1.0 if direction == "long" else -1.0
    t0 = int(r["closed_at_ms"])
    entry = float(r["entry_price"])
    size = float(r["position_size"])
    if size <= 0 or entry <= 0 or not bars:
        return None

    exit_bar = bar_at_or_before(bars, t0)
    if not exit_bar or exit_bar[3] <= 0:
        return None
    exit_close = exit_bar[3]

    # Journal-plane implied exit (gross pnl back-out) for honest USD on
    # cross-plane tradfi rows; falls back to entry notional if degenerate.
    implied_exit = entry + sign * (pnl_gross(r) / size)
    exit_notional = size * (implied_exit if implied_exit > 0 else entry)

    stop = float(r.get("stop_price") or 0.0)
    stop_same_plane = stop > 0  # caller passes 0 for cross-plane venues

    row = {
        "symbol": sym, "direction": direction,
        "exit_reason": r.get("exit_reason"), "exit_class": exit_class(r.get("exit_reason", "")),
        "outcome": r.get("outcome"), "pnl_net_usd": round(pnl_net(r), 4),
        "hold_time_min": round((r.get("hold_time_ms") or 0) / 60000.0, 1),
        "exit_ref": round(exit_close, 6),
    }
    for hname, hsec in HORIZONS_S.items():
        if t0 + hsec * 1000 > now_ms:
            row[hname] = "pending"
            continue
        horizon_ms = t0 + hsec * 1000
        window = [b for b in bars if exit_bar[0] < b[0] <= horizon_ms]
        if not window:
            row[hname] = "no_klines"
            continue
        # Stop realism: the hold leg ends at the real stop if crossed.
        held_close = window[-1][3]
        stop_hit = False
        if stop_same_plane:
            for b in window:
                if (direction == "long" and b[2] <= stop) or \
                   (direction == "short" and b[1] >= stop):
                    held_close, stop_hit = stop, True
                    break
        mfe = max(b[1] for b in window)
        mae = min(b[2] for b in window)
        hold_ret = (held_close / exit_close - 1.0) * sign
        hold_usd = hold_ret * exit_notional
        mfe_ret = ((mfe / exit_close - 1.0) if direction == "long"
                   else (1.0 - mae / exit_close))
        delta = hold_usd - pnl_net(r)
        row[hname] = {
            "hold_usd": round(hold_usd, 4),
            "delta_usd": round(delta, 4),       # + = exit left money on the table
            "stop_would_hit": stop_hit,
            "mfe_pct_after_exit": round(mfe_ret * 100.0, 3),
        }
    return row


def aggregate(rows: list[dict]) -> dict:
    """Per exit-class stats per horizon. Verdicts obey the n>=10 census bar;
    cells are exploratory (classes x horizons comparisons — Aronson)."""
    classes: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        classes[r["exit_class"]].append(r)
    out = {}
    for cls, rs in sorted(classes.items()):
        c: dict = {"n": len(rs),
                   "realized_usd": round(sum(x["pnl_net_usd"] for x in rs), 3)}
        for hname in HORIZONS_S:
            scored = [x[hname] for x in rs
                      if isinstance(x.get(hname), dict)]
            if not scored:
                c[hname] = {"scored": 0}
                continue
            n = len(scored)
            deltas = [s["delta_usd"] for s in scored]
            regret = sum(1 for d in deltas if d > MIN_EFFECT_USD)
            saved = sum(1 for d in deltas if d < -MIN_EFFECT_USD)
            regret_rate = regret / n
            saved_rate = saved / n
            if n < 10:
                verdict = "thin"
            elif regret_rate >= 0.50:
                verdict = "exits_costly"
            elif saved_rate >= 0.60:
                verdict = "exits_justified"
            else:
                verdict = "mixed"
            c[hname] = {
                "scored": n,
                "sum_delta_usd": round(sum(deltas), 3),
                "mean_delta_usd": round(sum(deltas) / n, 4),
                "regret_rate": round(regret_rate, 3),
                "saved_rate": round(saved_rate, 3),
                "stops_would_hit": sum(1 for s in scored if s["stop_would_hit"]),
                "verdict": verdict,
            }
        out[cls] = c
    return out


async def run(day: str) -> dict:
    venue_of, yahoo_of, aster_sym_of = venue_classifier()
    closes = load_journal_closes(day)
    result: dict = {
        "date": day,
        "generated": datetime.now(timezone.utc).isoformat(),
        "n_closes": len(closes),
        "note": ("delta_usd > 0 = the exit left money on the table; "
                 "< 0 = the exit saved money. Verdicts need n>=10; "
                 "classes x horizons cells are exploratory "
                 "(multiple comparisons — confirm before proposing)."),
    }
    if not closes:
        result["rows"] = []
        result["by_class"] = {}
        return result

    import httpx
    now_ms = int(time.time() * 1000)
    rows, skipped = [], 0
    async with httpx.AsyncClient(
            timeout=10.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ARIA-exit-autopsy/1.0)"}) as client:
        async def one(r):
            sym = r["symbol"]
            venue = venue_of(sym)
            if venue == "skip":
                return None
            t0 = int(r["closed_at_ms"])
            bars = await fetch_bars(
                client, venue,
                aster_sym_of(sym) if venue == "aster" else sym,
                t0 - 120_000, yahoo_sym=yahoo_of(sym))
            if not bars:
                return None
            rr = dict(r)
            if venue == "yahoo":
                rr["stop_price"] = 0.0   # cross-plane: stop check not honest
            return score_close(rr, bars, now_ms)
        scored = await asyncio.gather(*(one(r) for r in closes[:MAX_FETCH]))
        rows = [x for x in scored if x is not None]
    skipped = min(len(closes), MAX_FETCH) - len(rows)

    result.update({
        "measured": len(rows),
        "skipped_unmeasurable": skipped,
        "truncated_to": MAX_FETCH if len(closes) > MAX_FETCH else None,
        "by_class": aggregate(rows),
        "rows": rows,
    })
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()
    day = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        result = asyncio.run(run(day))
    except Exception as e:  # exit-0 doctrine: a broken run writes the error
        result = {"date": day, "error": str(e)[:200],
                  "generated": datetime.now(timezone.utc).isoformat()}
    tmp = args.out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(result, f, indent=1)
    os.replace(tmp, args.out)
    try:
        hist = {"date": day, "generated": result.get("generated"),
                "n_closes": result.get("n_closes"),
                "measured": result.get("measured"),
                "by_class": {c: {h: v.get("sum_delta_usd")
                                 for h, v in s.items() if isinstance(v, dict)}
                             for c, s in (result.get("by_class") or {}).items()}}
        with open(HIST_PATH, "a") as f:
            f.write(json.dumps(hist) + "\n")
    except Exception:
        pass
    print(json.dumps({"date": day, "n_closes": result.get("n_closes"),
                      "measured": result.get("measured")}))


if __name__ == "__main__":
    main()
