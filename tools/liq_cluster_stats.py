#!/usr/bin/env python3
"""Liquidation-cluster aftermath analyzer (Agent E, 2026-09-15).

Standalone (stdlib + httpx), exit-0 best-effort, works when the bot is down.
Reads logs/liq_clusters.jsonl (the capture plane's ground truth) and, for
each fully-matured cluster, fetches Bybit public 1m klines around ts_start:

  GET https://api.bybit.com/v5/market/kline?category=linear&symbol=<BYBIT>&interval=1&start=&end=

Symbol map: ARIA "SOL-USD" → Bybit "SOLUSDT"; symbols with no Bybit perp
(empty result list) are marked skipped and never retried. Computes the
forward price path at T+2/4/8/15/30/45/120min vs price_at_print, the
continuation rate (>=0.5% move in the liq direction) per tier per minute,
and the coin-flip crossover minute (first minute continuation rate < 0.50;
n>=5 clusters in the tier else "thin").

Cache discipline: <=20 clusters analyzed per run, oldest-unanalyzed first;
per-cluster analyzed/skipped marker + results in
logs/liq_cluster_stats_done.json (pruned to the newest 2000). Only clusters
whose ts_start is >=130 min old are analyzed (the +120min leg must be
mature) — younger ones stay pending for the next run (date discipline per
the tools/macro_posture.py house pattern).

Output: logs/liq_cluster_stats.json (atomic tmp+os.replace) + one history
line to logs/liq_cluster_stats_history.jsonl + the full JSON on stdout.
Kill switch: LIQ_CLUSTER_ENABLED=false = inert (writes a disabled payload,
exits 0). Observer-class: zero trade-path wiring, zero gate changes.

Usage: .venv/bin/python tools/liq_cluster_stats.py
"""
import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_KLINE_URL = "https://api.bybit.com/v5/market/kline"
_HORIZONS_MIN = (2, 4, 8, 15, 30, 45, 120)
_CONTINUATION_PCT = 0.005        # >=0.5% move in the liq direction
_MIN_TIER_N = 5                  # crossover needs >=5 clusters in the tier
_MAX_PER_RUN = 20                # polite: <=20 kline fetches per run
_MATURE_AGE_MS = 130 * 60_000    # only analyze clusters with the +120m leg done
_DONE_CAP = 2000                 # marker/result store prune bound
_RECENT_IN_REPORT = 50


def _enabled() -> bool:
    return os.environ.get("LIQ_CLUSTER_ENABLED", "true").strip().lower() != "false"


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _load_jsonl(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    if isinstance(rec, dict):
                        rows.append(rec)
                except Exception:
                    continue   # one-bad-line doctrine
    except Exception:
        pass
    return rows


def _atomic_write(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


# ── Pure math (pinned in tests/test_liq_clusters.py) ─────────────────────────

def aria_to_bybit_symbol(symbol: str):
    """'SOL-USD' -> 'SOLUSDT'. None when unmappable."""
    try:
        s = str(symbol or "").strip().upper()
        if not s.endswith("-USD"):
            return None
        base = s[:-4]
        return base + "USDT" if base else None
    except Exception:
        return None


def signed_move_pct(direction: str, p0: float, p_t: float):
    """Signed % move in the LIQ direction. LiquidationSignal direction is
    'bearish' (from long liqs — continuation = price falls) or 'bullish'
    (from short liqs — continuation = price rises). None on bad inputs."""
    try:
        p0, p_t = float(p0), float(p_t)
        if p0 <= 0 or p_t <= 0:
            return None
        d = str(direction or "").strip().lower()
        if d == "bearish":
            return (p0 - p_t) / p0
        if d == "bullish":
            return (p_t - p0) / p0
        return None
    except Exception:
        return None


def is_continuation(signed_pct, threshold: float = _CONTINUATION_PCT) -> bool:
    try:
        return signed_pct is not None and float(signed_pct) >= threshold
    except Exception:
        return False


def crossover_minute(per_minute: dict):
    """First horizon minute whose continuation rate < 0.50. per_minute maps
    minute -> {"n": int, "rate": float}. 'thin' when the tier's total n is
    below the >=5 bar; None when the rate never crosses (rare tail)."""
    total_n = 0
    try:
        for m in _HORIZONS_MIN:
            st = per_minute.get(str(m)) or per_minute.get(m) or {}
            total_n = max(total_n, int(st.get("n") or 0))
        if total_n < _MIN_TIER_N:
            return "thin"
        for m in _HORIZONS_MIN:
            st = per_minute.get(str(m)) or per_minute.get(m) or {}
            if int(st.get("n") or 0) >= _MIN_TIER_N and \
                    float(st.get("rate")) < 0.50:
                return m
        return None
    except Exception:
        return "thin"


def cluster_key(rec: dict) -> str:
    return "%s|%s|%s" % (rec.get("symbol"), rec.get("direction"),
                         rec.get("ts_start_ms"))


# ── Bybit kline fetch + path computation ─────────────────────────────────────

def fetch_klines(bybit_symbol: str, start_ms: int, end_ms: int):
    """One public kline GET. Returns list of (start_ms, close) ascending,
    [] when the symbol has no Bybit perp or no data, None on fetch error."""
    import httpx
    r = httpx.get(_KLINE_URL,
                  params={"category": "linear", "symbol": bybit_symbol,
                          "interval": "1", "start": str(int(start_ms)),
                          "end": str(int(end_ms)), "limit": "1000"},
                  timeout=15.0)
    d = r.json()
    if int(d.get("retCode", -1)) != 0:
        return None
    raw = ((d.get("result") or {}).get("list")) or []
    out = []
    for bar in raw:
        try:
            out.append((int(bar[0]), float(bar[4])))
        except Exception:
            continue
    out.sort()
    return out


def _close_at(bars, ts_ms):
    """Close of the bar containing ts_ms, else the newest earlier close."""
    best = None
    for start, close in bars:
        if start <= ts_ms:
            best = close
        else:
            break
    return best


def analyze_cluster(rec: dict, bars) -> dict:
    """Forward path for one cluster over its kline bars."""
    ts0 = int(rec.get("ts_start_ms") or 0)
    direction = rec.get("direction")
    p0 = _close_at(bars, ts0)
    path = {}
    for m in _HORIZONS_MIN:
        p_t = _close_at(bars, ts0 + m * 60_000)
        sm = signed_move_pct(direction, p0, p_t)
        path[str(m)] = None if sm is None else round(sm, 6)
    return {"symbol": rec.get("symbol"), "direction": direction,
            "ts_start_ms": ts0, "tier": rec.get("tier"),
            "total_notional": rec.get("total_notional"),
            "event_count": rec.get("event_count"),
            "price_at_print": p0, "signed_moves": path}


def aggregate_tiers(analyses) -> dict:
    """Continuation rate per tier per minute + coin-flip crossover."""
    tiers = {}
    for a in analyses:
        tier = a.get("tier") or "unknown"
        t = tiers.setdefault(tier, {"n": 0, "per_minute": {}})
        t["n"] += 1
        for m in _HORIZONS_MIN:
            sm = (a.get("signed_moves") or {}).get(str(m))
            if sm is None:
                continue
            st = t["per_minute"].setdefault(str(m), {"n": 0, "continuations": 0})
            st["n"] += 1
            if is_continuation(sm):
                st["continuations"] += 1
    for tier, t in tiers.items():
        for st in t["per_minute"].values():
            st["rate"] = round(st["continuations"] / st["n"], 4) if st["n"] else None
        t["coin_flip_crossover_minute"] = crossover_minute(t["per_minute"])
    return tiers


# ── Main pass ────────────────────────────────────────────────────────────────

def main() -> int:
    log_dir = os.path.join(_ROOT, "logs")
    now = time.time()
    now_ms = int(now * 1000)
    out = {"ts": now, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
           "kill_switch_enabled": _enabled(),
           "horizons_min": list(_HORIZONS_MIN),
           "continuation_threshold_pct": _CONTINUATION_PCT,
           "min_tier_n": _MIN_TIER_N}
    if not _enabled():
        out["disabled"] = True
        try:
            _atomic_write(os.path.join(log_dir, "liq_cluster_stats.json"), out)
        except Exception:
            pass
        print(json.dumps(out, indent=2))
        return 0

    clusters = _load_jsonl(os.path.join(log_dir, "liq_clusters.jsonl"))
    done_path = os.path.join(log_dir, "liq_cluster_stats_done.json")
    done = _load(done_path, {})
    if not isinstance(done, dict):
        done = {}
    out["clusters_total"] = len(clusters)
    out["analyzed_total"] = sum(1 for v in done.values()
                                if isinstance(v, dict)
                                and v.get("status") == "analyzed")
    out["skipped_total"] = sum(1 for v in done.values()
                               if isinstance(v, dict)
                               and v.get("status") == "skipped")

    # Oldest-unanalyzed first; only fully-matured clusters; <=20 per run.
    pending = [c for c in clusters
               if cluster_key(c) not in done
               and int(c.get("ts_start_ms") or 0) <= now_ms - _MATURE_AGE_MS]
    pending.sort(key=lambda c: int(c.get("ts_start_ms") or 0))
    batch = pending[:_MAX_PER_RUN]
    out["pending_mature"] = len(pending)
    analyzed_this_run, errors = 0, 0

    for rec in batch:
        key = cluster_key(rec)
        try:
            bybit = aria_to_bybit_symbol(rec.get("symbol"))
            if bybit is None:
                done[key] = {"status": "skipped", "reason": "symbol_unmappable",
                             "tier": rec.get("tier"),
                             "ts_start_ms": rec.get("ts_start_ms")}
                continue
            ts0 = int(rec.get("ts_start_ms") or 0)
            bars = fetch_klines(bybit, ts0 - 300_000, ts0 + 125 * 60_000)
            if bars is None:
                errors += 1
                continue          # fetch error: retry next run
            if not bars:
                done[key] = {"status": "skipped", "reason": "no_bybit_perp",
                             "bybit_symbol": bybit, "tier": rec.get("tier"),
                             "ts_start_ms": rec.get("ts_start_ms")}
                continue
            a = analyze_cluster(rec, bars)
            a["status"] = "analyzed"
            a["bybit_symbol"] = bybit
            done[key] = a
            analyzed_this_run += 1
            time.sleep(0.1)       # polite fetch cadence
        except Exception as e:
            errors += 1
            done[key] = {"status": "error", "reason": str(e)[:120],
                         "ts_start_ms": rec.get("ts_start_ms")}

    out["analyzed_this_run"] = analyzed_this_run
    out["fetch_errors"] = errors

    # Prune the marker store to the newest _DONE_CAP by ts_start_ms.
    try:
        if len(done) > _DONE_CAP:
            keys = sorted(done.keys(),
                          key=lambda k: int((done[k] or {}).get("ts_start_ms") or 0))
            for k in keys[:len(done) - _DONE_CAP]:
                done.pop(k, None)
        _atomic_write(done_path, done)
    except Exception:
        pass

    analyses = [v for v in done.values()
                if isinstance(v, dict) and v.get("status") == "analyzed"]
    try:
        out["tiers"] = aggregate_tiers(analyses)
    except Exception as e:
        out["tiers"] = {"error": str(e)[:120]}
    out["per_cluster_recent"] = sorted(
        analyses, key=lambda a: int(a.get("ts_start_ms") or 0))[-_RECENT_IN_REPORT:]

    try:
        _atomic_write(os.path.join(log_dir, "liq_cluster_stats.json"), out)
        hist = {"ts": now, "clusters_total": out["clusters_total"],
                "analyzed_total": out["analyzed_total"] + analyzed_this_run,
                "analyzed_this_run": analyzed_this_run,
                "crossover": {t: (v or {}).get("coin_flip_crossover_minute")
                              for t, v in (out.get("tiers") or {}).items()
                              if isinstance(v, dict)}}
        with open(os.path.join(log_dir, "liq_cluster_stats_history.jsonl"), "a") as f:
            f.write(json.dumps(hist) + "\n")
    except Exception:
        pass
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
