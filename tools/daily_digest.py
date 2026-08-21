"""Daily EV digest — deterministic precompute for the watchdog's daily scan.

Runs standalone (works even when the bot is DOWN — diagnostics matter most
then). The watchdog's first cycle after 00:00 UTC runs this, reads
logs/daily_digest.json, and spends its turns on judgment, not arithmetic.

    .venv/bin/python tools/daily_digest.py [--date YYYY-MM-DD]

Reads:  newest logs/trade_journal_*.json (cumulative — filter by window),
        logs/aria.log (single pass, date-filtered, JSON-parsed only for
        position_closed lines), logs/gate_report.json, logs/shadow_scored.jsonl,
        logs/drawdown_state.json, logs/venue_comparison.json (weekly),
        public klines (Bybit / Aster / Yahoo) for entry-slippage + benchmark.
Writes: logs/daily_digest.json (full report) + one line appended to
        logs/daily_digest_history.jsonl (trend).

Best-effort doctrine: every section carries its own "error" field on failure;
one bad source never blanks the report. Exit code is always 0.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

# Run-as-script puts tools/ on sys.path, not the repo root — the lazy repo
# imports in venue_classifier() need the root (config, feeds, aster adapter).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
ARIA_LOG = os.path.join(LOG_DIR, "aria.log")
OUT_PATH = os.path.join(LOG_DIR, "daily_digest.json")
HISTORY_PATH = os.path.join(LOG_DIR, "daily_digest_history.jsonl")

BYBIT_KLINE = "https://api.bybit.com/v5/market/kline"
ASTER_KLINE = "https://fapi.asterdex.com/fapi/v1/klines"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}"

VETO_EVENTS = {
    "signal_stale_data", "insufficient_candles", "signal_rejected_c_tier",
    "signal_rejected_dispersion_gate", "coherence_tier_reject",
    "quant_filter_blocked", "recovery_mode_coherence_skip",
    "market_hours_gate_blocked", "signal_throttled",
}
PHANTOM_EVENTS = {
    "recovery_mode_coherence_skip", "recovery_mode_applied",
    "drawdown_size_reduced", "basket_harvest", "deposit_detection_vetoed",
    "deposit_anchors_adjusted", "campaign_loss_cooloff_armed",
    "direction_loss_block_armed",
}
MULT_FIELDS = ("allocation_mult", "coherence_mult", "calendar_mult",
               "calendar_size_mult", "freshness_mult", "size_multiplier")


# ── Pure analysis (unit-tested, no I/O) ──────────────────────────────────────

def pnl_net(r: dict) -> float:
    v = r.get("pnl_net_usd")
    if v is None:
        v = r.get("pnl_usd")
    return float(v or 0.0)


def expectancy_by_symbol(records: list[dict]) -> dict:
    out = {}
    bysym = defaultdict(list)
    for r in records:
        bysym[r.get("symbol", "?")].append(r)
    for sym, rs in sorted(bysym.items()):
        closed = [r for r in rs if r.get("outcome") in ("win", "loss")]
        wins = [pnl_net(r) for r in closed if pnl_net(r) > 0]
        losses = [pnl_net(r) for r in closed if pnl_net(r) <= 0]
        n = len(closed)
        if n == 0:
            continue
        exp = sum(wins + losses) / n
        out[sym] = {
            "n": n,
            "abandoned": sum(1 for r in rs if r.get("outcome") == "abandoned"),
            "wr": round(len(wins) / n, 3),
            "avg_win": round(sum(wins) / len(wins), 4) if wins else 0.0,
            "avg_loss": round(sum(losses) / len(losses), 4) if losses else 0.0,
            "expectancy": round(exp, 4),
            "pnl_sum": round(sum(wins + losses), 3),
            "flag": "churn_leak" if (n >= 10 and exp < -0.02) else "",
        }
    return out


def size_chain(records: list[dict], balance: float) -> dict:
    """Where does size die? Mean of each multiplier field + median notional.
    The smallest mean mult names the chokepoint (e.g. size_multiplier 0.35
    avg = DD/recovery tax — the 2026-08-18 phantom pattern)."""
    mults = {f: [] for f in MULT_FIELDS}
    notionals = []
    for r in records:
        if r.get("outcome") not in ("win", "loss"):
            continue
        for f in MULT_FIELDS:
            v = r.get(f)
            if isinstance(v, (int, float)) and v > 0:
                mults[f].append(float(v))
        sz, ep = r.get("position_size") or 0, r.get("entry_price") or 0
        if sz and ep:
            notionals.append(sz * ep)
    mean_mults = {f: round(sum(v) / len(v), 3) for f, v in mults.items() if v}
    notionals.sort()
    med_notional = round(notionals[len(notionals) // 2], 1) if notionals else 0.0
    choke = min(mean_mults, key=mean_mults.get) if mean_mults else ""
    flag = ""
    if balance > 400 and med_notional and med_notional < 0.15 * balance:
        flag = f"size_leak: median notional ${med_notional} < 15% of balance"
    return {"mean_mults": mean_mults, "chokepoint": choke,
            "median_notional": med_notional, "flag": flag}


def hold_asymmetry(records: list[dict]) -> dict:
    def med(xs):
        xs = sorted(xs)
        return round(xs[len(xs) // 2], 1) if xs else 0.0
    wins = [(r.get("hold_time_ms") or 0) / 60000 for r in records
            if r.get("outcome") == "win"]
    losses = [(r.get("hold_time_ms") or 0) / 60000 for r in records
              if r.get("outcome") == "loss"]
    out = {"median_win_min": med(wins), "median_loss_min": med(losses)}
    w, l = out["median_win_min"], out["median_loss_min"]
    out["flag"] = ("cut_winners_ride_losers" if w and l and l > 1.5 * w
                   else "")
    return out


def trend_capture(records: list[dict], day_pct, moves_4h: dict,
                  balance: float) -> dict:
    """Did ARIA capture the day's trend? Compares the majors' move (daily bar,
    or the biggest synchronized 4h thrust when the daily bar is ambiguous)
    against directional realized PnL. Trend is signed — a downtrend day is a
    trend day; the guard is direction-symmetric.

    Verdicts:
      quiet_day    — evidence present but below trend thresholds
      ok           — trend existed and trend-side realized PnL was positive
      MISSED_TREND — trend existed, trend-side PnL <= 0
                     (counter_traded=True when the opposed side also lost)
      unknown      — no market evidence (network section failed)
    """
    def _side(d) -> str:
        d = str(d or "").lower()
        if d.startswith(("l", "buy")):
            return "long"
        if d.startswith(("s", "sell")):
            return "short"
        return ""

    pnl_by_side = {"long": 0.0, "short": 0.0}
    n_by_side = {"long": 0, "short": 0}
    for r in records:
        if r.get("outcome") not in ("win", "loss"):
            continue
        s = _side(r.get("direction"))
        if not s:
            continue
        pnl_by_side[s] += pnl_net(r)
        n_by_side[s] += 1

    out = {"day_move_pct": day_pct, "max_4h_moves": moves_4h or {},
           "verdict": "unknown"}

    direction, mag = "", 0.0
    if day_pct is not None and abs(day_pct) >= 3.0:
        direction = "long" if day_pct > 0 else "short"
        mag = abs(day_pct)
    elif moves_4h:
        eq = sum(moves_4h.values()) / len(moves_4h)
        if abs(eq) >= 2.0:
            direction = "long" if eq > 0 else "short"
            mag = abs(eq)
    if not direction:
        if day_pct is not None or moves_4h:
            out["verdict"] = "quiet_day"
        return out

    opp = "short" if direction == "long" else "long"
    tp = round(pnl_by_side[direction], 3)
    cp = round(pnl_by_side[opp], 3)
    out.update({
        "trend_direction": direction,
        "trend_magnitude_pct": round(mag, 2),
        "trend_side_pnl_usd": tp,
        "counter_side_pnl_usd": cp,
        "trend_side_trades": n_by_side[direction],
        "counter_side_trades": n_by_side[opp],
    })
    if tp > 0:
        out["verdict"] = "ok"
    else:
        out["verdict"] = "MISSED_TREND"
        out["counter_traded"] = cp < 0
    return out


def fee_drag(records: list[dict]) -> dict:
    gross = sum(float(r.get("pnl_usd") or 0.0) for r in records
                if r.get("outcome") in ("win", "loss"))
    net = sum(pnl_net(r) for r in records if r.get("outcome") in ("win", "loss"))
    drag = round(net - gross, 4)
    return {"gross": round(gross, 3), "net": round(net, 3), "drag": drag,
            "drag_pct_of_gross": round(100 * drag / gross, 1) if gross else 0.0}


def exit_pareto(closed_events: list[dict]) -> dict:
    """closed_events: parsed position_closed (__main__) log dicts."""
    agg = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for e in closed_events:
        reason = e.get("exit_reason") or "unknown"
        p = e.get("pnl")
        if isinstance(p, str):
            p = p.replace("$", "").replace("+", "")
        try:
            p = float(p or 0.0)
        except (TypeError, ValueError):
            p = 0.0
        agg[reason]["n"] += 1
        agg[reason]["pnl"] += p
    return {k: {"n": v["n"], "pnl": round(v["pnl"], 3)}
            for k, v in sorted(agg.items(), key=lambda kv: kv[1]["pnl"])}


def silence_census(assets: list[str], signal_ready: Counter,
                   vetoes: Counter) -> list[dict]:
    out = []
    for sym in assets:
        if signal_ready.get(sym, 0) > 0:
            continue
        sym_vetoes = {ev: n for (s, ev), n in vetoes.items() if s == sym}
        if not sym_vetoes:
            out.append({"symbol": sym, "top_veto": "no_events_at_all",
                        "veto_count": 0})
            continue
        top = max(sym_vetoes, key=sym_vetoes.get)
        kind = "data" if top in ("signal_stale_data", "insufficient_candles") else "gate"
        out.append({"symbol": sym, "top_veto": top,
                    "veto_count": sym_vetoes[top], "kind": kind})
    return sorted(out, key=lambda d: -d["veto_count"])


def coherence_calibration(records: list[dict]) -> dict:
    buckets = {"5-6": [], "6-7": [], "7-8": [], "8+": []}
    for r in records:
        if r.get("outcome") not in ("win", "loss"):
            continue
        c = float(r.get("coherence_score") or 0.0)
        key = "8+" if c >= 8 else ("7-8" if c >= 7 else ("6-7" if c >= 6 else "5-6"))
        buckets[key].append(pnl_net(r))
    out = {}
    for k, v in buckets.items():
        if v:
            out[k] = {"n": len(v),
                      "wr": round(sum(1 for x in v if x > 0) / len(v), 3),
                      "expectancy": round(sum(v) / len(v), 4)}
    return out


def session_of(hour_utc: int) -> str:
    if hour_utc < 7:
        return "asia"
    if hour_utc < 12:
        return "london"
    if hour_utc < 21:
        return "us"
    return "off_hours"


def session_attribution(records: list[dict]) -> dict:
    agg = defaultdict(list)
    for r in records:
        if r.get("outcome") not in ("win", "loss"):
            continue
        ts = r.get("timestamp_ms") or 0
        hour = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).hour if ts else 12
        agg[session_of(hour)].append(pnl_net(r))
    return {k: {"n": len(v), "pnl": round(sum(v), 3),
                "wr": round(sum(1 for x in v if x > 0) / len(v), 3)}
            for k, v in sorted(agg.items())}


def slippage_bps(fills: list[dict], kline_close: dict[int, float]) -> list[float]:
    """fills: [{ts_ms, price}]; kline_close: minute_ms → close. Signed bps per
    fill (positive = paid above reference)."""
    out = []
    for f in fills:
        minute = int(f["ts_ms"] // 60000 * 60000)
        ref = kline_close.get(minute) or kline_close.get(minute - 60000)
        if ref and ref > 0:
            out.append((f["price"] - ref) / ref * 1e4)
    return out


def summarize_slippage(per_venue: dict[str, list[float]], skipped: dict) -> dict:
    out = {}
    for venue, vals in per_venue.items():
        if not vals:
            continue
        out[venue] = {
            "n_fills": len(vals),
            "avg_abs_bps": round(sum(abs(v) for v in vals) / len(vals), 1),
            "max_abs_bps": round(max(abs(v) for v in vals), 1),
            "mean_signed_bps": round(sum(vals) / len(vals), 1),
            "flag": "systematic_slippage" if sum(abs(v) for v in vals) / len(vals) > 10 else "",
        }
    if skipped:
        out["_skipped"] = skipped
    return out


# ── I/O helpers ──────────────────────────────────────────────────────────────

def load_journal_records(day: str) -> list[dict]:
    files = sorted(glob.glob(os.path.join(LOG_DIR, "trade_journal_*.json")),
                   key=os.path.getmtime)
    if not files:
        return []
    try:
        records = json.load(open(files[-1]))
    except Exception:
        return []
    seen, out = set(), []
    for r in records:
        ts = r.get("timestamp_ms") or 0
        day_str = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if ts else ""
        if day_str != day:
            continue
        key = (r.get("entry_id"), r.get("closed_at_ms"))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def scan_aria_log(day: str) -> dict:
    """Single pass over aria.log filtered to the target date. JSON-parse only
    position_closed lines from __main__ (they carry exit_reason)."""
    res = {"signal_ready": Counter(), "vetoes": Counter(),
           "phantom": Counter(), "closed_events": [],
           "conviction_review": Counter()}
    needle = f'"{day}T'
    if not os.path.exists(ARIA_LOG):
        res["error"] = "aria.log missing"
        return res
    with open(ARIA_LOG, errors="replace") as f:
        for line in f:
            if needle not in line:
                continue
            if '"event"' not in line:
                continue
            try:
                ev = line.split('"event": "', 1)[1].split('"', 1)[0]
            except IndexError:
                continue
            sym = ""
            if '"symbol": "' in line:
                try:
                    sym = line.split('"symbol": "', 1)[1].split('"', 1)[0]
                except IndexError:
                    pass
            if ev == "signal_ready" and sym:
                res["signal_ready"][sym] += 1
            if ev in VETO_EVENTS and sym:
                res["vetoes"][(sym, ev)] += 1
            if ev in PHANTOM_EVENTS:
                res["phantom"][ev] += 1
            if ev in ("conviction_decay_closed", "conviction_decay_deferred"):
                key = ev
                if '"reason": "' in line:
                    try:
                        key = f"{ev}:{line.split(chr(34) + 'reason' + chr(34) + ': ' + chr(34), 1)[1].split(chr(34), 1)[0]}"
                    except IndexError:
                        pass
                res["conviction_review"][key] += 1
            if ev == "position_closed" and '"logger": "__main__"' in line:
                try:
                    res["closed_events"].append(json.loads(line))
                except Exception:
                    pass
    return res


def load_json(path: str, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


# ── Public-endpoint comparisons (network, best-effort) ───────────────────────

async def _fetch_klines(client, venue: str, symbol: str, start_ms: int,
                        yahoo_sym: str = "") -> dict[int, float]:
    """Return minute_ms → close for the symbol's fills window."""
    out = {}
    try:
        if venue == "bybit":
            r = await client.get(BYBIT_KLINE, params={
                "category": "linear", "symbol": f"{symbol.replace('-USD', '')}USDT",
                "interval": "1", "start": start_ms, "limit": 300})
            rows = (r.json().get("result") or {}).get("list") or []
            for k in rows:
                out[int(k[0])] = float(k[4])
        elif venue == "aster":
            r = await client.get(ASTER_KLINE, params={
                "symbol": symbol, "interval": "1m",
                "startTime": start_ms, "limit": 300})
            for k in r.json():
                out[int(k[0])] = float(k[4])
        elif venue == "yahoo":
            # 1m bars are retained ~7d — range=1d only covers TODAY, which can
            # never match yesterday's fills (the digest's default day).
            r = await client.get(YAHOO_CHART.format(yahoo_sym),
                                 params={"interval": "1m", "range": "5d"})
            result = (r.json().get("chart", {}).get("result") or [None])[0]
            if result:
                ts = result.get("timestamp") or []
                closes = ((result.get("indicators", {}).get("quote") or [{}])[0]
                          .get("close") or [])
                for t, c in zip(ts, closes):
                    if c is not None:
                        out[int(t) * 1000] = float(c)
    except Exception:
        pass
    return out


async def slippage_and_benchmark(records: list[dict], venue_of, yahoo_of,
                                 aster_sym_of, day: str) -> tuple[dict, dict]:
    import httpx
    per_venue: dict[str, list[float]] = defaultdict(list)
    skipped = Counter()
    fills_by_sym = defaultdict(list)
    for r in records:
        if r.get("outcome") not in ("win", "loss"):
            continue
        ep, ts = r.get("entry_price") or 0, r.get("timestamp_ms") or 0
        if ep and ts:
            fills_by_sym[r["symbol"]].append({"ts_ms": ts, "price": ep})
    bench = {}
    async with httpx.AsyncClient(timeout=8.0,
                                 headers={"User-Agent": "Mozilla/5.0 (compatible; ARIA-digest/1.0)"}) as client:
        async def one(sym, fills):
            venue = venue_of(sym)
            if venue == "skip":
                skipped[sym] = len(fills)
                return
            start = min(f["ts_ms"] for f in fills) - 120000
            kc = await _fetch_klines(client, venue,
                                     aster_sym_of(sym) if venue == "aster" else sym,
                                     start, yahoo_sym=yahoo_of(sym))
            if not kc:
                skipped[sym] = len(fills)
                return
            vals = slippage_bps(fills, kc)
            skipped[sym] = skipped.get(sym, 0) + (len(fills) - len(vals))
            per_venue[venue].extend(vals)
        await asyncio.gather(*(one(s, f) for s, f in fills_by_sym.items()))
        # Benchmark: equal-weight BTC/ETH/SOL hold return for the DIGEST day
        # (Bybit daily klines are newest-first — pick the bar whose open_time
        # date matches, then open→close of that bar, not prev-close→cur-close).
        try:
            rets = []
            for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
                r = await client.get(BYBIT_KLINE, params={
                    "category": "linear", "symbol": s, "interval": "D", "limit": 5})
                rows = (r.json().get("result") or {}).get("list") or []
                for k in rows:
                    bar_day = datetime.fromtimestamp(
                        int(k[0]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    if bar_day == day:
                        o, c = float(k[1]), float(k[4])
                        if o > 0:
                            rets.append((c - o) / o * 100)
                        break
            if rets:
                bench["hodl_pct"] = round(sum(rets) / len(rets), 2)
            # Biggest synchronized 4h thrust per major — the move ARIA should
            # have caught even when the full daily bar is ambiguous.
            moves_4h = {}
            for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
                r = await client.get(BYBIT_KLINE, params={
                    "category": "linear", "symbol": s, "interval": "240",
                    "limit": 12})
                rows = (r.json().get("result") or {}).get("list") or []
                best = None
                for k in rows:
                    bar_day = datetime.fromtimestamp(
                        int(k[0]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    if bar_day != day:
                        continue
                    o, c = float(k[1]), float(k[4])
                    if o > 0:
                        pct = (c - o) / o * 100
                        if best is None or abs(pct) > abs(best):
                            best = pct
                if best is not None:
                    moves_4h[s.replace("USDT", "")] = round(best, 2)
            if moves_4h:
                bench["max_4h_moves"] = moves_4h
        except Exception:
            pass
    return summarize_slippage(per_venue, dict(skipped)), bench


# ── Main ─────────────────────────────────────────────────────────────────────

def venue_classifier():
    """Lazy repo imports — module stays importable without pydantic/env."""
    try:
        from core.config import Settings
        from data.tradfi_feed import TRADFI_SYMBOLS
        from execution.aster_client import to_aster_symbol
        cfg = Settings()
        aster = set(getattr(cfg, "aster_assets", []))
        assets = list(getattr(cfg, "assets", []))

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

        return assets, venue_of, yahoo_of, to_aster_symbol
    except Exception as e:
        return [], lambda s: "skip", lambda s: "", lambda s: s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()
    day = args.date or datetime.fromtimestamp(
        time.time() - 86400, tz=timezone.utc).strftime("%Y-%m-%d")
    run_wday = datetime.now(timezone.utc).weekday()

    assets, venue_of, yahoo_of, aster_sym_of = venue_classifier()
    records = load_journal_records(day)
    logscan = scan_aria_log(day)
    dd_state = load_json(os.path.join(LOG_DIR, "drawdown_state.json"), {})
    gate_report = load_json(os.path.join(LOG_DIR, "gate_report.json"), {})
    balance = float(dd_state.get("current") or 0.0)

    digest: dict = {"date": day, "generated": datetime.now(timezone.utc).isoformat(),
                    "trades_closed": sum(1 for r in records if r.get("outcome") in ("win", "loss"))}

    digest["expectancy"] = expectancy_by_symbol(records)
    digest["size_chain"] = size_chain(records, balance)
    digest["hold_asymmetry"] = hold_asymmetry(records)
    digest["fee_drag"] = fee_drag(records)
    digest["exit_pareto"] = exit_pareto(logscan["closed_events"])
    digest["conviction_review"] = dict(logscan["conviction_review"])
    digest["silence_census"] = silence_census(assets, logscan["signal_ready"], logscan["vetoes"])

    peak = float(dd_state.get("peak_balance") or dd_state.get("peak") or 0.0)
    digest["phantom_sweep"] = {
        "dd_peak": peak, "dd_current": balance,
        "peak_ratio_suspect": bool(balance and peak > 1.3 * balance),
        "event_counts": dict(logscan["phantom"]),
    }

    ga = (gate_report or {}).get("gate_accuracy", {})
    gad = (gate_report or {}).get("gate_accuracy_by_day_type", {})
    digest["gates"] = {
        "overall": ga.get("_total", {}),
        "per_gate": {g: {"accuracy": v.get("accuracy"), "n": v.get("gated"),
                         "verdict": v.get("verdict")}
                     for g, v in ga.items() if not g.startswith("_")},
        # Season mismatches only — a gate strong globally but tight in one
        # day type is the row the watchdog should read (dispersion-on-trend
        # was the 2026-08-18 freeze-window finding).
        "day_type_mismatches": {
            dt: {g: v for g, v in gm.items() if v.get("verdict") != "strong"}
            for dt, gm in gad.items()
            if any(v.get("verdict") != "strong" for v in gm.values())
        },
    }
    scored = [json.loads(l) for l in open(os.path.join(LOG_DIR, "shadow_scored.jsonl"))
              if l.strip().startswith("{")] if os.path.exists(os.path.join(LOG_DIR, "shadow_scored.jsonl")) else []
    day_refusals = [r for r in scored
                    if datetime.fromtimestamp(r.get("ts", 0), tz=timezone.utc).strftime("%Y-%m-%d") == day]
    big_missed = sorted((r for r in day_refusals if r.get("won_24h")),
                        key=lambda r: -(r.get("pnl_24h") or 0))[:5]
    digest["gates"]["tail_cost_top5"] = [
        {"symbol": r["symbol"], "gate": r["gate"], "direction": r["direction"],
         "would_be_pnl_24h": round((r.get("pnl_24h") or 0) * 100, 2)}
        for r in big_missed]

    try:
        slip, bench = asyncio.run(slippage_and_benchmark(
            records, venue_of, yahoo_of, aster_sym_of, day))
        digest["slippage"] = slip
        realized = digest["fee_drag"]["net"]
        digest["benchmark"] = {
            "aria_realized_usd": realized,
            "aria_pct": round(100 * realized / balance, 2) if balance else 0.0,
            **bench,
        }
        if "hodl_pct" in bench:
            digest["benchmark"]["delta_pct"] = round(
                digest["benchmark"]["aria_pct"] - bench["hodl_pct"], 2)
        digest["trend_capture"] = trend_capture(
            records, bench.get("hodl_pct"), bench.get("max_4h_moves", {}),
            balance)
    except Exception as e:
        digest["slippage"] = {"error": str(e)[:200]}
        digest["benchmark"] = {"error": str(e)[:200]}
        digest["trend_capture"] = trend_capture(records, None, {}, balance)

    if run_wday == 0:   # Monday run → weekly sections over the trailing 7d
        week_records = []
        for back in range(7):
            d = datetime.fromtimestamp(time.time() - 86400 * (back + 1),
                                       tz=timezone.utc).strftime("%Y-%m-%d")
            week_records.extend(load_journal_records(d))
        digest["weekly"] = {
            "coherence_calibration": coherence_calibration(week_records),
            "session_attribution": session_attribution(week_records),
            "venue_comparison": load_json(os.path.join(LOG_DIR, "venue_comparison.json"),
                                          {"note": "not generated yet"}),
        }

    # Trend — the compounding loop made visible. Reads the history JSONL the
    # digest itself appends to: is gate accuracy drifting, is the same symbol
    # churning day after day, what did the last 7 days actually net.
    try:
        hist_rows = []
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("{"):
                        hist_rows.append(json.loads(line))
        tail = hist_rows[-7:]
        if tail:
            churn_counts = Counter(s for h in tail for s in h.get("churn_flags", []))
            accs = [h["gate_accuracy"] for h in tail
                    if isinstance(h.get("gate_accuracy"), (int, float))]
            digest["trend"] = {
                "days": len(tail),
                "net_pnl_7d": round(sum(h.get("net_pnl") or 0 for h in tail), 2),
                "trades_7d": sum(h.get("trades") or 0 for h in tail),
                "gate_accuracy_trajectory": accs,
                "chronic_churners": {s: n for s, n in churn_counts.items() if n >= 3},
            }
    except Exception as e:
        digest["trend"] = {"error": str(e)[:200]}

    tmp = args.out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(digest, f, indent=1)
    os.replace(tmp, args.out)

    hist = {"date": day, "trades": digest["trades_closed"],
            "net_pnl": digest["fee_drag"]["net"],
            "gate_accuracy": (digest["gates"]["overall"] or {}).get("accuracy"),
            "trend_capture": (digest.get("trend_capture") or {}).get("verdict"),
            "churn_flags": [s for s, v in digest["expectancy"].items() if v["flag"]],
            "size_flag": digest["size_chain"]["flag"],
            "slippage_flags": {v: s["flag"] for v, s in digest.get("slippage", {}).items()
                               if isinstance(s, dict) and s.get("flag")}}
    with open(HISTORY_PATH, "a") as f:
        f.write(json.dumps(hist) + "\n")
    print(f"digest written: {args.out} ({digest['trades_closed']} trades, "
          f"net {digest['fee_drag']['net']:+.2f})")


if __name__ == "__main__":
    main()
