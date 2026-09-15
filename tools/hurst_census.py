#!/usr/bin/env python3
"""hurst_census — per-symbol rolling Hurst persistence census (observer-class).

Governor doctrine 2026-09-15 ("signal bar"): "Hurst says the asset is
tradeable" is one of eight filters a real signal must clear. This tool is
the measurement plane: for each crypto major + liquid alt in ARIA's
universe it fetches the last 1000 Bybit public 15m klines (~10.4 days),
computes the rolling Hurst exponent with TWO independent estimators
(intelligence/hurst.py: R/S primary, DFA order-1 cross-check), and
classifies the CURRENT regime: trendable / mean_reverting / random_walk /
unknown.

REGIME-AWARENESS: Hurst is regime-conditional, not an asset property. The
output is the current rolling estimate over the stated window — never a
permanent label. Re-run the census; don't memorize verdicts.

Data plane: GET https://api.bybit.com/v5/market/kline (public, no key).
Documented response shape (Bybit V5): {"retCode":0,"retMsg":"OK",
"result":{"category":"linear","symbol":"BTCUSDT","list":[[startTime,open,
high,low,close,volume,turnover], ... newest-first, all fields strings]}}.
NOTE: the endpoint could not be probed from the build host (network
denied), so the parser is built from the documented shape — the output
carries "api_shape_verified": false until a live run confirms it.

ADVISORY ONLY. Zero trade-path wiring; the shadow gate proposal rides
proposals.jsonl separately (Aronson: measurement before gates).
Best-effort doctrine: every symbol self-errors, exit code always 0.
Writes logs/hurst_census.json (atomic tmp+replace) + one append line to
logs/hurst_census_history.jsonl.

Usage: python3 tools/hurst_census.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)  # lazy repo imports work when run as a script

LOG_DIR = os.path.join(REPO_ROOT, "logs")
OUT_PATH = os.path.join(LOG_DIR, "hurst_census.json")
HIST_PATH = os.path.join(LOG_DIR, "hurst_census_history.jsonl")

BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"
KLINE_INTERVAL = "15"          # 15m bars
KLINE_LIMIT = 1000             # ~10.4 days of 15m bars
HTTP_TIMEOUT_S = 15.0

# Symbols that are not crypto perps on Bybit — skipped without a fetch when
# the config import works (equities/indices/commodities self-error anyway,
# but there is no reason to spend the HTTP call).
FALLBACK_ASSETS = ["BTC-USD", "ETH-USD", "SOL-USD", "XAUT-USD"]


# ── Pure helpers (unit-tested, no I/O) ───────────────────────────────────────

def parse_klines(payload: dict) -> list:
    """Parse a Bybit V5 kline payload into closes, OLDEST-first floats.

    Accepts the documented shape: {"retCode": 0, "result": {"list":
    [[startTime, open, high, low, close, volume, turnover], ...]}} with
    list rows newest-first and all fields strings. Raises ValueError on
    any contract deviation — callers self-error the symbol, never guess.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload not a dict")
    if payload.get("retCode") != 0:
        raise ValueError(f"retCode={payload.get('retCode')!r} "
                         f"retMsg={payload.get('retMsg')!r}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError("result missing")
    rows = result.get("list")
    if not isinstance(rows, list) or not rows:
        raise ValueError("kline list missing/empty")
    closes = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            raise ValueError("kline row shape")
        closes.append(float(row[4]))
    if any(not math.isfinite(c) or c <= 0 for c in closes):
        raise ValueError("non-positive close")
    closes.reverse()  # API is newest-first; estimators want oldest-first
    return closes


def bybit_symbol(asset: str) -> str:
    """ARIA 'BTC-USD' -> Bybit linear perp 'BTCUSDT'."""
    base = asset.split("-")[0].strip().upper()
    return f"{base}USDT"


def census_row(asset: str, closes: list) -> dict:
    """One symbol's census row from its closes (oldest-first)."""
    try:
        from intelligence.hurst import classify, hurst_dfa, hurst_rs
    except Exception as e:  # brain import failure must not kill the census
        return {"symbol": asset, "error": f"brain_import: {e}"}
    h_rs = hurst_rs(closes, min_window=100)
    h_dfa = hurst_dfa(closes, min_window=200)
    verdict = classify(h_rs, h_dfa)
    return {
        "symbol": asset,
        "bars": len(closes),
        "window_days": round(len(closes) * 15 / 60 / 24, 1),
        "h_rs": round(h_rs, 4) if h_rs is not None else None,
        "h_dfa": round(h_dfa, 4) if h_dfa is not None else None,
        "estimator_gap": (round(abs(h_rs - h_dfa), 4)
                          if h_rs is not None and h_dfa is not None else None),
        "class": verdict["class"],
        "confidence": verdict["confidence"],
        "error": None,
    }


def load_universe() -> tuple:
    """(assets, source). Lazy config read; FALLBACK_ASSETS on any failure.
    Crypto-only: TRADFI/signal assets have no Bybit linear perp plane."""
    try:
        from core.config import Settings
        cfg = Settings()
        assets = list(cfg.assets)
        tradfi = set(getattr(cfg, "TRADFI_ASSETS", []) or [])
        sig = set(getattr(cfg, "signal_assets", []) or [])
        crypto = [a for a in assets if a not in tradfi and a not in sig]
        if crypto:
            return crypto, "config"
    except Exception:
        pass
    return list(FALLBACK_ASSETS), "fallback"


# ── I/O spine (best-effort, exit-0) ──────────────────────────────────────────

def _fetch_closes(client, symbol: str) -> list:
    resp = client.get(BYBIT_KLINE_URL, params={
        "category": "linear", "symbol": bybit_symbol(symbol),
        "interval": KLINE_INTERVAL, "limit": KLINE_LIMIT,
    }, timeout=HTTP_TIMEOUT_S)
    resp.raise_for_status()
    return parse_klines(resp.json())


def _atomic_write(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def main() -> int:
    ts_utc = datetime.now(timezone.utc)
    try:  # Governor-visible timestamps in Europe/Berlin (+2)
        from zoneinfo import ZoneInfo
        ts_berlin = ts_utc.astimezone(ZoneInfo("Europe/Berlin")).isoformat()
    except Exception:
        ts_berlin = None
    out = {
        "ts_utc": ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_berlin": ts_berlin,
        "tool": "hurst_census",
        "interval_min": int(KLINE_INTERVAL),
        "bars_requested": KLINE_LIMIT,
        # Parser built from the documented Bybit V5 shape — flip to true
        # after a live run confirms retCode/result/list contract.
        "api_shape_verified": False,
        "universe_source": None,
        "symbols": [],
        "errors": [],
    }
    assets, source = load_universe()
    out["universe_source"] = source
    try:
        import httpx
    except Exception as e:
        out["errors"].append({"symbol": "*", "error": f"httpx_import: {e}"})
        _finish(out)
        return 0
    try:
        with httpx.Client(http2=False) as client:
            for asset in assets:
                try:
                    closes = _fetch_closes(client, asset)
                    out["symbols"].append(census_row(asset, closes))
                except Exception as e:
                    err = {"symbol": asset, "error": str(e)[:300]}
                    out["symbols"].append(err)
                    out["errors"].append(err)
                time.sleep(0.15)  # public-endpoint courtesy
    except Exception as e:
        out["errors"].append({"symbol": "*", "error": f"client: {e}"})
    _finish(out)
    return 0


def _finish(out: dict) -> None:
    ok = [s for s in out["symbols"] if not s.get("error")]
    tally = {}
    for s in ok:
        tally[s["class"]] = tally.get(s["class"], 0) + 1
    out["summary"] = {"classified": len(ok), "errored": len(out["errors"]),
                      "classes": tally}
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        _atomic_write(OUT_PATH, out)
        hist = {"ts_utc": out["ts_utc"], "summary": out["summary"],
                "symbols": {s["symbol"]: {"h_rs": s.get("h_rs"),
                                          "h_dfa": s.get("h_dfa"),
                                          "class": s.get("class"),
                                          "error": s.get("error")}
                            for s in out["symbols"]}}
        with open(HIST_PATH, "a") as f:
            f.write(json.dumps(hist, sort_keys=True) + "\n")
    except Exception as e:
        print(f"hurst_census: write failed: {e}", file=sys.stderr)
    print(json.dumps(out["summary"], sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
