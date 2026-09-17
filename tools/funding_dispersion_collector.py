#!/usr/bin/env python3
"""Cross-venue funding dispersion collector (SIG1 shadow, measure-only).

Hourly: joins SoDEX funding (1h interval, x8 normalized) against Aster funding
(8h interval) for the Aster-routed symbol list and appends one JSONL record per
symbol to logs/funding_dispersion.jsonl. Never raises; never touches execution.
"""
import json
import time
import urllib.request
from pathlib import Path

SODEX_URL = "https://mainnet-gw.sodex.dev/api/v1/perps/markets/mark-prices"
ASTER_URL = "https://fapi.asterdex.com/fapi/v3/premiumIndex"
OUT = Path(__file__).resolve().parent.parent / "logs" / "funding_dispersion.jsonl"

ASTER_SYMBOLS = (
    "HYPE ADA UNI ONDO TAO ENA KAITO WIF ZEC VIRTUAL AAVE 1000BONK SEI PENGU INJ "
    "TIA APT TRX BCH XLM FARTCOIN VELVET AKE CYS ASTER ACE MUBARAK DOS SNXX HEMI "
    "AIO ARIA XAUT CL TSM ORCL DOGE XRP 1000PEPE SUI AVAX LINK LTC NEAR WLD BOME "
    "ICP XMR ORDI WLFI LIT PAXG FLOCK FF"
).split()

SODEX_INTERVAL_H = 1.0   # verified 2026-09-17: nextFundingTime - ts ~= 20-60min
ASTER_INTERVAL_H = 8.0   # verified 2026-09-17: nextFundingTime - ts ~= 7.3h


def _get(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_sodex() -> dict:
    data = _get(SODEX_URL)
    out = {}
    for row in data.get("data", []):
        sym = row.get("symbol", "")
        base = sym[:-4] if sym.endswith("-USD") else sym
        try:
            out[base] = float(row["fundingRate"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def fetch_aster() -> dict:
    data = _get(ASTER_URL)
    rows = data if isinstance(data, list) else [data]
    out = {}
    for row in rows:
        sym = row.get("symbol", "")
        base = sym[:-4] if sym.endswith("USDT") else (sym[:-3] if sym.endswith("USD") else sym)
        try:
            out[base] = float(row["lastFundingRate"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def cycle() -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        sodex = fetch_sodex()
    except Exception as e:
        sodex = {}
        print(json.dumps({"ts": ts, "event": "sodex_funding_error", "error": str(e)[:120]}), flush=True)
    try:
        aster = fetch_aster()
    except Exception as e:
        aster = {}
        print(json.dumps({"ts": ts, "event": "aster_funding_error", "error": str(e)[:120]}), flush=True)

    n = 0
    with open(OUT, "a") as f:
        for sym in ASTER_SYMBOLS:
            if sym not in sodex or sym not in aster:
                continue
            sd_raw, as_raw = sodex[sym], aster[sym]
            sd_8h = sd_raw * (ASTER_INTERVAL_H / SODEX_INTERVAL_H)
            disp_bp = (as_raw - sd_8h) * 1e4
            rec = {
                "ts": ts, "symbol": sym,
                "sodex_rate_1h": sd_raw, "aster_rate_8h": as_raw,
                "sodex_rate_8h_equiv": sd_8h,
                "dispersion_bp": round(disp_bp, 4),
                "abs_gt_1_5bp": abs(disp_bp) > 1.5,
            }
            f.write(json.dumps(rec) + "\n")
            n += 1
            if abs(disp_bp) > 1.5:
                print(json.dumps({
                    "ts": ts, "event": "funding_dispersion_signal", "symbol": sym,
                    "dispersion_bp": round(disp_bp, 3),
                    "predicted_normalization": "aster_down" if disp_bp > 0 else "aster_up",
                }), flush=True)
    print(json.dumps({"ts": ts, "event": "dispersion_cycle", "symbols": n,
                      "sodex_n": len(sodex), "aster_n": len(aster)}), flush=True)


def main() -> None:
    while True:
        now = time.time()
        next_run = (int(now // 3600) + 1) * 3600 + 600  # hourly at :10
        time.sleep(max(next_run - now, 5))
        try:
            cycle()
        except Exception as e:
            print(json.dumps({"event": "cycle_error", "error": str(e)[:120]}), flush=True)


if __name__ == "__main__":
    main()
