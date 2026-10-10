#!/usr/bin/env python3
"""outlier_census — right-tail PnL census over logs/outcomes.db (observer-class).

Wave-2 quant instrument (Governor order 2026-10-10; math spec
/tmp/aria_wave1_quant.md §3). Nightly-cronable. Answers: "where does the
gross positive PnL actually live?" — the Iglewicz–Hoaglin modified-z right
tail, its share of gross positive PnL, its hold-time profile, and per-symbol
attribution. The separation TEST (entry features vs the fence indicator)
stays unanswerable until the wave-2 entry-feature stamps accrue — this tool
is the fence + base-rate half of that framework.

Method (Iglewicz–Hoaglin):
  M_i = 0.6745 · (x_i − p50) / MAD ;  right-tail fence: M > 3.5
MAD = median(|x − p50|). Distribution measured on net_pnl_usd (USD plane);
net_pnl_r percentiles ride along for reference.

Phantom class: SPCX-USD rows with |net_pnl_usd| > 100 are the scale-mismatch
ghost cohort (beliefs-layer is_phantom_record idiom, 2026-08-24) — excluded
from every statistic, counted separately. --keep-phantoms disables.

Read-only: sqlite opened with uri mode=ro, journal files never touched.
Output: atomic logs/outlier_census.json (tmp + os.replace) + one-line stdout
summary. Exit code always 0 (best-effort observer doctrine).

Usage:
  python3 tools/outlier_census.py                 # census, write
  python3 tools/outlier_census.py --no-write      # print only
  python3 tools/outlier_census.py --db PATH       # alternate outcomes.db
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

LOG_DIR = Path(REPO_ROOT) / "logs"
DB_PATH = LOG_DIR / "outcomes.db"
OUT_PATH = LOG_DIR / "outlier_census.json"

FENCE_Z = 3.5                 # Iglewicz–Hoaglin right-tail fence
PHANTOM_SYMBOL = "SPCX-USD"   # scale-mismatch ghost cohort (beliefs idiom)
PHANTOM_ABS_PNL = 100.0


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else 0.5 * (s[mid - 1] + s[mid])


def _percentile(xs: list[float], q: float) -> float | None:
    """Linear-interpolation percentile, q ∈ [0,100]. None on empty input."""
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    pos = (q / 100.0) * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] + frac * (s[hi] - s[lo])


# ── data read (read-only, fail-closed) ───────────────────────────────────────

def read_outcomes(db_path: Path) -> tuple[list[dict], bool]:
    """(rows, db_present). Missing/corrupt DB = empty honest zero, NOT an
    error — flagged in the record. Read-only uri connection."""
    rows: list[dict] = []
    if not db_path.exists():
        return rows, False
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cur = con.cursor()
            cols = {r[1] for r in cur.execute("PRAGMA table_info(outcomes)")}
            need = {"symbol", "net_pnl_usd", "net_pnl_r", "hold_time_hours"}
            if not need.issubset(cols):
                return rows, True
            for sym, pnl, pnl_r, hold in cur.execute(
                    "SELECT symbol, net_pnl_usd, net_pnl_r, hold_time_hours "
                    "FROM outcomes"):
                try:
                    rows.append({
                        "symbol": str(sym or ""),
                        "net_pnl_usd": float(pnl),
                        "net_pnl_r": (float(pnl_r) if pnl_r is not None else None),
                        "hold_time_hours": (float(hold) if hold is not None else None),
                    })
                except (TypeError, ValueError):
                    continue  # one-bad-line doctrine, row granularity
        finally:
            con.close()
    except Exception:
        return rows, False
    return rows, True


def is_phantom(row: dict) -> bool:
    """Scale-mismatch ghost cohort (is_phantom_record idiom)."""
    return (row["symbol"] == PHANTOM_SYMBOL
            and abs(row["net_pnl_usd"]) > PHANTOM_ABS_PNL)


# ── census (pure) ────────────────────────────────────────────────────────────

def compute_census(rows: list[dict], computed_at: float,
                   db_present: bool = True) -> dict:
    pnls = [r["net_pnl_usd"] for r in rows]
    n = len(pnls)
    rec: dict = {
        "computed_at": _iso(computed_at),
        "n": n,
        "db_present": bool(db_present),
        "fence": {"method": "iglewicz_hoaglin", "z": FENCE_Z},
    }
    if n == 0:
        rec["notes"] = "empty_outcomes_db"
        return rec

    p50 = _median(pnls)
    mad = _median([abs(x - p50) for x in pnls])
    rec["distribution"] = {
        "p50": round(p50, 4),
        "p75": round(_percentile(pnls, 75), 4),
        "p90": round(_percentile(pnls, 90), 4),
        "p95": round(_percentile(pnls, 95), 4),
        "p99": round(_percentile(pnls, 99), 4),
        "mad": round(mad, 4),
        "max": round(max(pnls), 4),
        "min": round(min(pnls), 4),
    }
    pnls_r = [r["net_pnl_r"] for r in rows if r["net_pnl_r"] is not None]
    if pnls_r:
        rec["distribution_r"] = {
            "p50": round(_median(pnls_r), 4),
            "p90": round(_percentile(pnls_r, 90), 4),
            "p99": round(_percentile(pnls_r, 99), 4),
        }

    if mad <= 0.0:
        rec["notes"] = "mad_zero_fence_unreachable"
        return rec

    fence_usd = p50 + FENCE_Z * mad / 0.6745
    rec["fence"]["fence_usd"] = round(fence_usd, 4)

    gross_pos = sum(x for x in pnls if x > 0.0)
    tail = [r for r in rows if 0.6745 * (r["net_pnl_usd"] - p50) / mad > FENCE_Z]
    tail_pnl = sum(r["net_pnl_usd"] for r in tail)
    rec["right_tail"] = {
        "n": len(tail),
        "share_of_trades": round(len(tail) / n, 4),
        "pnl_usd": round(tail_pnl, 4),
        "gross_positive_pnl_usd": round(gross_pos, 4),
        "share_of_gross_positive": (round(tail_pnl / gross_pos, 4)
                                    if gross_pos > 0 else None),
    }

    holds = [r["hold_time_hours"] for r in tail if r["hold_time_hours"] is not None]
    if holds:
        rec["right_tail"]["hold_time_hours"] = {
            "p50": round(_median(holds), 3),
            "p75": round(_percentile(holds, 75), 3),
            "p90": round(_percentile(holds, 90), 3),
            "mean": round(sum(holds) / len(holds), 3),
        }
    all_holds = [r["hold_time_hours"] for r in rows if r["hold_time_hours"] is not None]
    if all_holds:
        rec["hold_time_hours_all"] = {"p50": round(_median(all_holds), 3)}

    # per-symbol tail attribution (sorted by tail pnl desc)
    by_sym: dict[str, dict] = {}
    for r in tail:
        b = by_sym.setdefault(r["symbol"] or "UNKNOWN", {"n": 0, "pnl_usd": 0.0})
        b["n"] += 1
        b["pnl_usd"] += r["net_pnl_usd"]
    rec["tail_by_symbol"] = [
        {"symbol": s, "n": b["n"], "pnl_usd": round(b["pnl_usd"], 4),
         "share_of_tail_pnl": (round(b["pnl_usd"] / tail_pnl, 4)
                               if tail_pnl > 0 else None)}
        for s, b in sorted(by_sym.items(), key=lambda kv: -kv[1]["pnl_usd"])
    ]
    return rec


# ── output ───────────────────────────────────────────────────────────────────

def write_census(out_path: Path, rec: dict) -> bool:
    tmp = str(out_path) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, indent=2, default=str))
        os.replace(tmp, out_path)
        return True
    except Exception as _e:
        print(f"outlier_census_write_failed: {str(_e)[:120]}", file=sys.stderr)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def one_liner(rec: dict, phantoms: int) -> str:
    rt = rec.get("right_tail") or {}
    dist = rec.get("distribution") or {}
    fence = (rec.get("fence") or {}).get("fence_usd")
    share = rt.get("share_of_gross_positive")
    return (
        f"outlier_census {rec.get('computed_at')}: n={rec.get('n')} "
        f"p50={dist.get('p50')} mad={dist.get('mad')} fence=+{fence} "
        f"tail_n={rt.get('n')} ({(rt.get('share_of_trades') or 0) * 100:.1f}%) "
        f"tail_pnl={rt.get('pnl_usd')} "
        f"({(share * 100 if share is not None else 0):.0f}% of gross+) "
        f"phantoms_excluded={phantoms}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Right-tail PnL census (outcomes.db)")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--out", default=str(OUT_PATH))
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--keep-phantoms", action="store_true",
                    help="do not exclude the SPCX scale-mismatch ghost cohort")
    args = ap.parse_args(argv)

    now = time.time()
    rows, db_present = read_outcomes(Path(args.db))
    phantoms = 0
    if not args.keep_phantoms:
        kept = []
        for r in rows:
            if is_phantom(r):
                phantoms += 1
            else:
                kept.append(r)
        rows = kept

    rec = compute_census(rows, now, db_present=db_present)
    rec["phantoms_excluded"] = phantoms
    print(one_liner(rec, phantoms))
    if not args.no_write:
        ok = write_census(Path(args.out), rec)
        print(f"outlier_census.json: {'written' if ok else 'WRITE FAILED'} "
              f"({args.out})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
