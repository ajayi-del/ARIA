#!/usr/bin/env python3
"""Stock basis register — the equity-perp basis episode ledger (observer-class).

SoDEX lists ~21 equity/index perps that trade 24/7 while their Yahoo
underlyings close. The basis (perp mark vs underlying) is where the edge and
the danger live — the "ORCL pattern" is a persistent same-sign basis episode.
The bot computes a return-divergence guard in data/tradfi_feed.py but never
persists the LEVEL basis; this register is the missing plane. Funding rates
for equity perps are collected by funding/history.py (168 hourly records)
but episodes (>=3 consecutive same-sign prints) were never registered — a
receiving-side funding episode is a future CARRY-HARVEST candidate.

Rebased-synthetic caveat (tradfi_feed.py docstring): SoDEX equity perps are
rebased synthetics (SPCX ~140 vs SPX ~765), so LEVEL basis carries a
structural offset per symbol. We record the RAW basis AND a rolling z-score
of basis vs that symbol's OWN (symbol, segment) history — the episode
detector keys on persistence/sign of the z, never the absolute level.

Each invocation (designed for a 15-min cron):
  1. For each mapped symbol: SoDEX perp mark (public REST, same endpoint as
     execution/sodex_client.py:655-667 — probe-verified 2026-09-15:
     {"code":0,"timestamp":...,"data":[{"symbol","openInterest","markPrice",
     "indexPrice","fundingRate","nextFundingTime"}]}) + best-effort orderbook
     depth for spread_bps; Yahoo v8 chart meta.regularMarketPrice for the
     underlying (pattern per data/tradfi_feed.py:_poll_one).
  2. basis_bps = (perp/underlying - 1)*1e4; z vs trailing (symbol, segment)
     window; session tag RTH (09:30-16:00 America/New_York, DST-aware)
     vs OFF_HOURS. A rebase step (|Δbasis| > 2000bps between prints) resets
     that (symbol, segment) z window — structural break, not noise.
  3. Episode = >=3 consecutive same-sign z-significant (|z|>=1) prints in the
     same segment. Register in logs/stock_basis_episodes.json (atomic rewrite;
     open episodes + last 90d closed). Prints to logs/stock_basis.jsonl
     (append-only, one-bad-line tolerant, dedup by (symbol, ts_minute)).
  4. Funding episodes from logs/funding_history.json (funding/history.py
     schema {sym: [{symbol, rate, timestamp_ms, source}]}): >=3 consecutive
     same-sign prints -> kind="funding" episode carrying annualized carry and
     the receiving side. Prints are hourly, so a funding episode is >=3h of
     same-sign persistence by construction. Missing/stale funding file
     self-errors that section.

Best-effort doctrine (tools/macro_posture.py pattern): per-symbol and
per-section self-errors, exit code always 0, stdlib + httpx, zero trade-path
wiring. Lazy repo imports inside functions — the tool survives repo import
breakage (hardcoded snapshot fallback below).

Usage: .venv/bin/python tools/stock_basis_register.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

LOG_DIR = os.path.join(_ROOT, "logs")
PRINTS_PATH = os.path.join(LOG_DIR, "stock_basis.jsonl")
EPISODES_PATH = os.path.join(LOG_DIR, "stock_basis_episodes.json")
FUNDING_PATH = os.path.join(LOG_DIR, "funding_history.json")

SODEX_BASE = "https://mainnet-gw.sodex.dev/api/v1/perps"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
UA = {"User-Agent": "Mozilla/5.0 (compatible; ARIA-stockbasis/1.0)"}

# SoDEX equity/index perp universe (task canon 2026-09-15; cross-checked vs
# core/config.py TRADFI_ASSETS + TIER_B_ASSETS — XAUT/CL excluded, COIN/CRCL
# included). UNITREE is in-universe but deliberately UNMAPPED (no Yahoo
# underlying, 2026-09-11 probe) — skipped with a note, funding still tracked.
EQUITY_PERPS = [
    "TSM-USD", "ORCL-USD", "NVDA-USD", "MSFT-USD", "AAPL-USD", "AMZN-USD",
    "GOOGL-USD", "META-USD", "TSLA-USD", "USTECH100-USD", "SPCX-USD",
    "COIN-USD", "CRCL-USD", "HOOD-USD", "LITE-USD", "SMCI-USD",
    "SAMSUNG-USD", "SKHX-USD", "UNITREE-USD", "SILVER-USD", "COPPER-USD",
]
UNMAPPED = {"UNITREE-USD": "no Yahoo equity underlying (2026-09-11 probe)"}

# Hardcoded symbol-map snapshot 2026-09-15 (mirror of data/tradfi_feed.py
# TRADFI_SYMBOLS at that date) — fallback when repo imports are broken.
_SYMBOL_MAP_SNAPSHOT = {
    "SPCX-USD": "SPY", "USTECH100-USD": "QQQ", "NVDA-USD": "NVDA",
    "MSFT-USD": "MSFT", "AAPL-USD": "AAPL", "AMZN-USD": "AMZN",
    "GOOGL-USD": "GOOGL", "META-USD": "META", "TSLA-USD": "TSLA",
    "TSM-USD": "TSM", "ORCL-USD": "ORCL", "CRCL-USD": "CRCL",
    "COIN-USD": "COIN", "HOOD-USD": "HOOD", "LITE-USD": "LITE",
    "SMCI-USD": "SMCI", "SAMSUNG-USD": "005930.KS", "SKHX-USD": "000660.KS",
    "COPPER-USD": "HG=F", "SILVER-USD": "SI=F",
}

RTH_START_HHMM = (13, 30)    # FALLBACK ONLY: fixed EDT hours in UTC, used
RTH_END_HHMM = (20, 0)       # when the tz database is unavailable
try:                         # DST-aware RTH: 09:30-16:00 America/New_York.
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:            # (EST winter shifts true RTH to 14:30-21:00 UTC;
    _ET = None               #  the hardcoded UTC window was wrong half the year)
Z_WINDOW = 200                 # trailing prints per (symbol, segment)
Z_MIN_N = 8                    # min history before z is meaningful
Z_SIGNIFICANT = 1.0
EPISODE_MIN_PRINTS = 3
RUN_MAX_GAP_S = 6 * 3600       # consecutive = prints of a segment ≤6h apart
FUNDING_RUN_MAX_GAP_S = 3 * 3600
FUNDING_STALE_S = 3 * 3600     # newest funding record older than this = stale
CLOSED_RETENTION_S = 90 * 86400
REBASE_STEP_BPS = 2000.0       # |Δbasis| between consecutive prints beyond
# this is a synthetic-rebase step (structural break), never organic 15-min
# noise — the pre-rebase levels would poison the z window for Z_WINDOW
# prints and fabricate a same-sign episode. SPCX 08-2026 rebase: Δ≈8200bps.


# ── Pure helpers (unit-tested, no I/O) ───────────────────────────────────────

def segment_for(ts: float) -> str:
    """RTH = US equity regular hours 09:30-16:00 America/New_York Mon-Fri
    (DST-aware — true UTC window is 13:30-20:00 in EDT, 14:30-21:00 in EST),
    else OFF_HOURS. Falls back to fixed EDT hours if the tz db is missing.
    US exchange holidays are NOT modeled (proposal class): a holiday weekday
    still tags RTH and the underlying reads the prior close."""
    if _ET is not None:
        dt = datetime.fromtimestamp(ts, tz=_ET)
        if dt.weekday() >= 5:
            return "OFF_HOURS"
        mins = dt.hour * 60 + dt.minute
        return "RTH" if 9 * 60 + 30 <= mins < 16 * 60 else "OFF_HOURS"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    if dt.weekday() >= 5:
        return "OFF_HOURS"
    mins = dt.hour * 60 + dt.minute
    if RTH_START_HHMM[0] * 60 + RTH_START_HHMM[1] <= mins < RTH_END_HHMM[0] * 60 + RTH_END_HHMM[1]:
        return "RTH"
    return "OFF_HOURS"


def basis_bps(perp: float, underlying: float) -> float:
    if not perp or not underlying or underlying <= 0 or perp <= 0:
        raise ValueError("invalid prices")
    return (perp / underlying - 1.0) * 1e4


def zscore(value: float, history: list[float]) -> float | None:
    """Population z of value vs trailing window; None below Z_MIN_N or zero stdev."""
    window = history[-Z_WINDOW:]
    if len(window) < Z_MIN_N:
        return None
    mean = sum(window) / len(window)
    var = sum((x - mean) ** 2 for x in window) / len(window)
    if var <= 0:
        return None
    return (value - mean) / math.sqrt(var)


def parse_sodex_mark(payload: dict) -> dict:
    """Shape pinned to the 2026-09-15 probe of /markets/mark-prices."""
    items = payload.get("data") or []
    if not items or not isinstance(items[0], dict):
        raise ValueError("empty_mark_data")
    it = items[0]
    mark = float(it.get("markPrice") or 0)
    if mark <= 0:
        raise ValueError("no_mark")
    out = {"mark": mark}
    try:
        idx = float(it.get("indexPrice") or 0)
        if idx > 0:
            out["index"] = idx
    except (TypeError, ValueError):
        pass
    try:
        out["funding_rate"] = float(it.get("fundingRate") or 0.0)
    except (TypeError, ValueError):
        pass
    return out


def parse_yahoo_price(payload: dict) -> float:
    """Shape pinned to data/tradfi_feed.py:_poll_one (v8 chart meta)."""
    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        raise ValueError("empty_chart")
    price = float(result.get("meta", {}).get("regularMarketPrice", 0) or 0)
    if price <= 0:
        raise ValueError("no_price")
    return price


def parse_spread_bps(payload: dict) -> float | None:
    """Best-effort spread from /markets/{symbol}/orderbook (bids/asks rows may
    be [price, qty] lists or dicts)."""
    ob = payload.get("data", payload)
    if not isinstance(ob, dict):
        return None
    bids, asks = ob.get("bids") or [], ob.get("asks") or []
    if not bids or not asks:
        return None

    def _px(row):
        if isinstance(row, (list, tuple)) and row:
            return float(row[0])
        if isinstance(row, dict):
            return float(row.get("price") or row.get("px") or 0)
        return 0.0

    try:
        bid, ask = _px(bids[0]), _px(asks[0])
    except (TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    return (ask - bid) / mid * 1e4


def trailing_run(prints: list[dict], max_gap_s: float, key: str = "z",
                 threshold: float = Z_SIGNIFICANT) -> list[dict]:
    """Trailing consecutive same-sign significant prints (list sorted by ts).

    A print is significant when prints[key] is not None and |v| > threshold
    (threshold=0 for funding: any nonzero rate counts). The run breaks on a
    sign flip, an insignificant/missing value, or a gap > max_gap_s.
    """
    run: list[dict] = []
    sign = 0
    prev_ts = None
    for rec in reversed(prints):
        v = rec.get(key)
        if v is None:
            break
        s = 1 if v > threshold else (-1 if v < -threshold else 0)
        if s == 0 or (sign and s != sign):
            break
        if prev_ts is not None and prev_ts - rec["ts"] > max_gap_s:
            break
        sign = s
        run.append(rec)
        prev_ts = rec["ts"]
    run.reverse()
    return run


def reconcile_episode(register: dict, key: str, run: list[dict], now: float,
                      make_record) -> str | None:
    """Open/extend/close one episode. Returns 'opened'|'extended'|'closed'|None."""
    open_eps = register.setdefault("open", {})
    closed = register.setdefault("closed", [])
    ep = open_eps.get(key)
    if len(run) >= EPISODE_MIN_PRINTS:
        rec = make_record(run)
        rec.update({"key": key, "status": "open"})
        if (ep is not None and ep.get("sign") is not None
                and rec.get("sign") is not None
                and rec["sign"] != ep["sign"]):
            # Sign flipped between invocations (missed cron ticks hid the
            # break): close the old leg on its own last print and open the
            # new one — never let an episode silently change sign in place
            # while keeping its original opened_ts.
            ep["status"] = "closed"
            ep["end_ts"] = ep.get("last_ts", now)
            ep["closed_at"] = now
            ep["duration_min"] = round(
                (ep["end_ts"] - ep.get("opened_ts", ep["end_ts"])) / 60.0, 1)
            closed.append(ep)
            del open_eps[key]
            ep = None
        if ep is None:
            rec["opened_ts"] = run[0]["ts"]
            rec["registered_at"] = now
            open_eps[key] = rec
            return "opened"
        ep.update(rec)
        ep["opened_ts"] = min(ep.get("opened_ts", run[0]["ts"]), run[0]["ts"])
        return "extended"
    if ep is not None:
        ep["status"] = "closed"
        ep["end_ts"] = ep.get("last_ts", now)
        ep["closed_at"] = now
        ep["duration_min"] = round((ep["end_ts"] - ep.get("opened_ts", ep["end_ts"])) / 60.0, 1)
        closed.append(ep)
        del open_eps[key]
        return "closed"
    return None


def prune_closed(register: dict, now: float) -> None:
    cutoff = now - CLOSED_RETENTION_S
    register["closed"] = [e for e in register.get("closed", [])
                          if e.get("end_ts", 0) >= cutoff]


def read_jsonl_tolerant(path: str) -> list[dict]:
    rows = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue   # one-bad-line doctrine
    except Exception:
        pass
    return rows


def atomic_write_json(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


# ── Fetchers (network; injected in tests) ────────────────────────────────────

def fetch_sodex_mark(client, symbol: str) -> dict:
    r = client.get(f"{SODEX_BASE}/markets/mark-prices", params={"symbol": symbol})
    if r.status_code != 200:
        raise RuntimeError(f"http_{r.status_code}")
    return parse_sodex_mark(r.json())


def fetch_sodex_spread(client, symbol: str) -> float | None:
    try:
        r = client.get(f"{SODEX_BASE}/markets/{symbol}/orderbook", params={"depth": 2})
        if r.status_code != 200:
            return None
        return parse_spread_bps(r.json())
    except Exception:
        return None


def fetch_yahoo_price(client, yahoo_sym: str) -> float:
    r = client.get(YAHOO_CHART.format(yahoo_sym),
                   params={"interval": "1m", "range": "1d", "includePrePost": "false"})
    if r.status_code != 200:
        raise RuntimeError(f"http_{r.status_code}")
    return parse_yahoo_price(r.json())


def load_symbol_map() -> tuple[dict, str]:
    """Lazy import of the repo map; snapshot fallback on any breakage."""
    try:
        from data.tradfi_feed import TRADFI_SYMBOLS  # lazy: survive repo breakage
        return dict(TRADFI_SYMBOLS), "repo"
    except Exception:
        return dict(_SYMBOL_MAP_SNAPSHOT), "snapshot_2026-09-15"


# ── Record builders ──────────────────────────────────────────────────────────

def basis_episode_record(symbol: str, segment: str, run: list[dict]) -> dict:
    zs = [abs(r["z"]) for r in run if r.get("z") is not None]
    bs = [r["basis_bps"] for r in run]
    return {
        "symbol": symbol, "segment": segment, "kind": "basis",
        # sign keys on the z (deviation vs own history) — the rebased level
        # offset lives in mean_basis_bps, not in the episode sign.
        "sign": 1 if run[-1]["z"] > 0 else -1,
        "last_ts": run[-1]["ts"], "n_prints": len(run),
        "max_abs_z": round(max(zs), 3) if zs else None,
        "mean_basis_bps": round(sum(bs) / len(bs), 2),
        "min_basis_bps": round(min(bs), 2), "max_basis_bps": round(max(bs), 2),
    }


def funding_episode_record(symbol: str, run: list[dict]) -> dict:
    rates = [r["rate"] for r in run]
    mean_rate = sum(rates) / len(rates)
    return {
        "symbol": symbol, "segment": "24_7", "kind": "funding",
        "sign": 1 if mean_rate > 0 else -1,
        "last_ts": run[-1]["ts"], "n_prints": len(run),
        "mean_rate": mean_rate,
        # FundingRecord.rate is an hourly decimal (funding/history.py) —
        # annualized = hourly × 24 × 365.
        "annualized_carry": round(mean_rate * 24 * 365, 6),
        "receiving_side": "shorts" if mean_rate > 0 else "longs",
    }


# ── Sections ─────────────────────────────────────────────────────────────────

def basis_section(client, sym_map: dict, now: float, prints_path: str,
                  register: dict, errors: dict) -> tuple[list[dict], list[str]]:
    """Fetch, print, z-score, episode-reconcile. Returns new print records."""
    history: dict[tuple[str, str], list[dict]] = {}
    seen_minutes: set[tuple[str, int]] = set()
    rows = read_jsonl_tolerant(prints_path)
    # Rebase reset markers persist: rows older than the latest marker for
    # their (symbol, segment) are pre-break levels, not history.
    resets: dict[tuple[str, str], int] = {}
    for rec in rows:
        try:
            if rec.get("rebase_reset"):
                k = (rec["symbol"], rec["segment"])
                resets[k] = max(resets.get(k, 0), int(rec["ts"]))
        except Exception:
            continue
    for rec in rows:
        try:
            key = (rec["symbol"], rec["segment"])
            if int(rec["ts"]) < resets.get(key, 0):
                continue
            history.setdefault(key, []).append(rec)
            seen_minutes.add((rec["symbol"], int(rec["ts"] // 60)))
        except Exception:
            continue
    for v in history.values():
        v.sort(key=lambda r: r["ts"])

    new_prints: list[dict] = []
    ts_minute = int(now // 60)
    for symbol in sorted(EQUITY_PERPS):
        if symbol in UNMAPPED:
            continue
        yahoo_sym = sym_map.get(symbol)
        if not yahoo_sym:
            errors[symbol] = "no_map"
            continue
        try:
            sm = fetch_sodex_mark(client, symbol)
            underlying = fetch_yahoo_price(client, yahoo_sym)
            bps = basis_bps(sm["mark"], underlying)
        except Exception as e:
            errors[symbol] = str(e)[:120]
            continue
        segment = segment_for(now)
        key = (symbol, segment)
        prior = history.get(key, [])
        rebase = bool(prior) and abs(bps - prior[-1]["basis_bps"]) > REBASE_STEP_BPS
        if rebase:
            # Synthetic rebase = structural break in the level offset: the
            # pre-rebase basis levels would poison the z window for up to
            # Z_WINDOW prints and fabricate a persistent same-sign episode
            # (USTECH100 carries a +398,133bps standing offset — a rebase
            # moves that offset discontinuously). Reset the window; the
            # rebase_reset flag on the print persists the reset across runs.
            history[key] = []
        hist = [r["basis_bps"] for r in history.get(key, [])]
        z = zscore(bps, hist)
        rec = {
            "ts": int(now), "symbol": symbol, "segment": segment,
            "perp_mark": sm["mark"], "underlying": underlying,
            "basis_bps": round(bps, 3),
            "z": round(z, 3) if z is not None else None,
            "index_px": sm.get("index"), "funding_rate": sm.get("funding_rate"),
            "spread_bps": None,
        }
        if rebase:
            rec["rebase_reset"] = True
        spread = fetch_sodex_spread(client, symbol)
        if spread is not None:
            rec["spread_bps"] = round(spread, 2)
        if (symbol, ts_minute) not in seen_minutes:
            new_prints.append(rec)
            history.setdefault(key, []).append(rec)
            seen_minutes.add((symbol, ts_minute))

    # Episode state machine — per (symbol, segment) over full history.
    events = []
    for symbol in sorted(EQUITY_PERPS):
        if symbol in UNMAPPED:
            continue
        for segment in ("RTH", "OFF_HOURS"):
            key = (symbol, segment)
            prints = history.get(key, [])
            if not prints:
                continue
            run = trailing_run(prints, RUN_MAX_GAP_S)
            ep_key = f"{symbol}|{segment}|basis"
            ev = reconcile_episode(
                register, ep_key, run, now,
                lambda r, s=symbol, g=segment: basis_episode_record(s, g, r))
            if ev:
                events.append(f"{ep_key}:{ev}")
    return new_prints, events


def funding_section(now: float, funding_path: str, register: dict,
                    errors: dict) -> list[str]:
    events = []
    try:
        with open(funding_path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("bad_shape")
    except Exception as e:
        errors["funding"] = f"read:{str(e)[:100]}"
        return events
    newest = 0
    for recs in data.values():
        try:
            newest = max(newest, max(int(r.get("timestamp_ms", 0)) for r in recs))
        except Exception:
            continue
    if newest < (now - FUNDING_STALE_S) * 1000:
        errors["funding"] = f"stale:newest_age_s={int(now - newest / 1000)}"
        return events

    for symbol in sorted(EQUITY_PERPS):
        recs = data.get(symbol) or []
        prints = []
        for r in recs:
            try:
                rate = float(r["rate"])
                prints.append({"ts": int(r["timestamp_ms"]) // 1000, "rate": rate})
            except Exception:
                continue
        if not prints:
            continue
        prints.sort(key=lambda r: r["ts"])
        # Significant = nonzero for funding (the register keys on same-sign
        # persistence of the rate itself, not a z — history is only 168 records).
        run = trailing_run(prints, FUNDING_RUN_MAX_GAP_S, key="rate", threshold=0.0)
        ep_key = f"{symbol}|funding"
        ev = reconcile_episode(
            register, ep_key, run, now,
            lambda r, s=symbol: funding_episode_record(s, r))
        if ev:
            events.append(f"{ep_key}:{ev}")
    return events


# ── Main ─────────────────────────────────────────────────────────────────────

def run(now: float | None = None, client=None, prints_path: str = PRINTS_PATH,
        episodes_path: str = EPISODES_PATH, funding_path: str = FUNDING_PATH,
        verbose: bool = True) -> dict:
    now = time.time() if now is None else now
    errors: dict = {}
    summary: dict = {"ts": int(now), "prints": 0, "episode_events": [], "errors": {}}

    sym_map, map_source = load_symbol_map()
    summary["map_source"] = map_source

    try:
        register = json.load(open(episodes_path))
        if not isinstance(register, dict) or "open" not in register:
            raise ValueError("bad_shape")
    except Exception:
        register = {"open": {}, "closed": []}

    if client is None:
        import httpx
        client = httpx.Client(timeout=10.0, headers=UA)

    new_prints, basis_events = basis_section(
        client, sym_map, now, prints_path, register, errors)
    funding_events = funding_section(now, funding_path, register, errors)

    if new_prints:
        os.makedirs(os.path.dirname(prints_path), exist_ok=True)
        with open(prints_path, "a") as f:
            for rec in new_prints:
                f.write(json.dumps(rec) + "\n")
    prune_closed(register, now)
    register["updated_ts"] = int(now)
    try:
        atomic_write_json(episodes_path, register)
    except Exception as e:
        errors["episodes_write"] = str(e)[:120]

    summary["prints"] = len(new_prints)
    summary["episode_events"] = basis_events + funding_events
    summary["errors"] = errors
    summary["open_episodes"] = sorted(register.get("open", {}).keys())
    summary["unmapped_skipped"] = sorted(UNMAPPED)

    if verbose:
        for rec in new_prints:
            print(f"{rec['symbol']:15s} {rec['segment']:9s} "
                  f"mark={rec['perp_mark']:<12} underlying={rec['underlying']:<12} "
                  f"basis={rec['basis_bps']:+8.2f}bps z={rec['z']} "
                  f"spread={rec['spread_bps']}")
        for ev in summary["episode_events"]:
            print(f"EPISODE {ev}")
        for k, v in errors.items():
            print(f"SELF-ERROR {k}: {v}")
        print(json.dumps({k: v for k, v in summary.items() if k != "errors"},
                         indent=1))
    return summary


if __name__ == "__main__":
    try:
        run()
    except Exception as e:          # best-effort doctrine: never nonzero
        print(f"FATAL-SELF-ERROR {str(e)[:200]}")
    sys.exit(0)
