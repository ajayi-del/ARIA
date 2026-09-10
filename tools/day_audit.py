"""Day audit — deterministic night-lane precompute (day_loop.md, Governor 2026-09-08).

The 00:41Z night audit judges; this tool does the arithmetic. One JSON over
the day's closes: summary, per-exit-class, per-tag, per-plane, fee drag,
dust/WR honesty, and compact per-close rows the prover swarm reads instead
of crawling raw logs on LLM tokens. Stdlib + the digest's loaders; atomic
write; exit 0 always (best-effort doctrine).

    .venv/bin/python tools/day_audit.py [--date YYYY-MM-DD] [--out PATH]

Reads:  outcomes.db / trade_journal (via tools.daily_digest loaders),
        logs/trade_db.jsonl (MFE/MAE/hold/entry_plane/venue/notional),
        logs/execution_plane_ledger.jsonl (SCH-1 plane labels).
Writes: logs/day_audit.json (atomic).
Doctrine (#48/R19): prints the PnL field it read; window computed on
closed_at_ms programmatically, never by filename glob. Plane labels come
from trade_db.entry_plane -> SCH-1 ledger join -> "unknown" — NEVER from
strategy_tag (#49: the tag is not a plane; cascade_aftermath runs the
gated path 13/13).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.daily_digest import (  # noqa: E402
    load_journal_records, load_outcome_records, pnl_net,
)

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
TRADE_DB = os.path.join(LOG_DIR, "trade_db.jsonl")
PLANE_LEDGER = os.path.join(LOG_DIR, "execution_plane_ledger.jsonl")
OUT_PATH = os.path.join(LOG_DIR, "day_audit.json")

DUST_CLASSES = ("stop_dust_purged", "sync_dust_purged")


def _load_jsonl(path: str) -> list:
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue  # one-bad-line doctrine
    except Exception:
        pass
    return out


def _trade_db_by_symbol() -> dict:
    # outcomes/journal ids share no namespace with trade_db; the reliable
    # join is (symbol, nearest timestamp_close_ms) — observed dt <= 2ms.
    out = {}
    for r in _load_jsonl(TRADE_DB):
        if r.get("symbol") and r.get("timestamp_close_ms"):
            out.setdefault(r["symbol"], []).append(r)
    for xs in out.values():
        xs.sort(key=lambda r: r["timestamp_close_ms"])
    return out


def _match_tdb(rec: dict, by_symbol: dict, tol_ms: int = 60_000) -> dict:
    cands = by_symbol.get(rec.get("symbol") or "")
    if not cands:
        return {}
    best = min(cands, key=lambda t: abs(t["timestamp_close_ms"] - rec["closed_at_ms"]))
    return best if abs(best["timestamp_close_ms"] - rec["closed_at_ms"]) <= tol_ms else {}


def _plane_by_attempt() -> dict:
    out = {}
    for r in _load_jsonl(PLANE_LEDGER):
        ident = r.get("identity") or {}
        plane = r.get("plane") or {}
        aid = ident.get("attempt_id")
        if not aid:
            continue
        p = plane.get("plane")
        if not p:
            p = "fastpath" if plane.get("entry_path_site") == "post_fill" else "gated"
        out[str(aid)] = p
    return out


def _plane_of(t: dict, ledger: dict) -> str:
    if t.get("entry_plane"):
        return str(t["entry_plane"])
    if str(t.get("trade_id") or "") in ledger:
        return ledger[str(t["trade_id"])]
    return "unknown" if t else "no_trade_db_match"


def _median(xs: list) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return round(xs[n // 2], 3) if n % 2 else round((xs[n // 2 - 1] + xs[n // 2]) / 2, 3)


def _wr(rows: list) -> float | None:
    if not rows:
        return None
    return round(100.0 * sum(1 for r in rows if r["_net"] > 0) / len(rows), 1)


def _rollup(rows: list, key_fn) -> list:
    groups = {}
    for r in rows:
        groups.setdefault(key_fn(r), []).append(r)
    out = []
    for k, xs in groups.items():
        mfes = [x["_mfe"] for x in xs if x["_mfe"] is not None]
        out.append({"cohort": k, "n": len(xs),
                    "net_usd": round(sum(x["_net"] for x in xs), 4),
                    "wr_pct": _wr(xs),
                    "avg_mfe_pct": round(sum(mfes) / len(mfes), 3) if mfes else None})
    out.sort(key=lambda d: -d["n"])
    return out


def _journal_tags(day: str) -> list:
    # outcomes carry no strategy_tag; journal does. Same (symbol, close ts)
    # join as trade_db — ids share no namespace there either.
    return [(r.get("symbol"), r.get("closed_at_ms"), r.get("strategy_tag"))
            for r in load_journal_records(day)
            if r.get("closed_at_ms") and r.get("strategy_tag")]


def _tag_of(rec: dict, tags: list, tol_ms: int = 60_000) -> str:
    for sym, ms, tag in tags:
        if sym == rec.get("symbol") and abs(ms - rec["closed_at_ms"]) <= tol_ms:
            return str(tag)
    return "unknown"


def run(day: str) -> dict:
    records = load_outcome_records(day) or load_journal_records(day)
    by_symbol = _trade_db_by_symbol()
    ledger = _plane_by_attempt()
    tags = _journal_tags(day)

    rows = []
    for r in records:
        if not r.get("closed_at_ms"):
            continue
        t = _match_tdb(r, by_symbol)
        rows.append({
            "symbol": r.get("symbol"),
            "side": r.get("direction") or r.get("side"),
            "exit_reason": r.get("exit_reason") or t.get("exit_reason") or "unknown",
            "strategy_tag": r.get("strategy_tag") or _tag_of(r, tags),
            "venue": t.get("venue") or "unknown",
            "hold_min": round(float(t.get("hold_seconds") or 0) / 60.0, 1) if t.get("hold_seconds") else None,
            "_net": pnl_net(r),
            "_gross": (float(r["pnl_usd"]) if r.get("pnl_usd") is not None else None),
            "_mfe": (float(t["mfe_pct"]) if t.get("mfe_pct") is not None else None),
            "plane": _plane_of(t, ledger),
        })

    ms = sorted(int(r["closed_at_ms"]) for r in records if r.get("closed_at_ms"))
    declaration = {
        "pnl_field": "pnl_net_usd (fallback pnl_usd where absent; outcomes.db synthesizes both from net_pnl_usd)",
        "window_field": "closed_at_ms",
        "window_bounds": ([datetime.fromtimestamp(ms[0] / 1000, timezone.utc).isoformat(),
                           datetime.fromtimestamp(ms[-1] / 1000, timezone.utc).isoformat()]
                          if ms else None),
        "dedup": "(entry_id, closed_at_ms)",
        "plane_source": "trade_db.entry_plane -> execution_plane_ledger join -> unknown; never strategy_tag (#49)",
    }

    net = sum(r["_net"] for r in rows)
    gross_rows = [r for r in rows if r["_gross"] is not None]
    gross = sum(r["_gross"] for r in gross_rows)
    fees = gross - sum(r["_net"] for r in gross_rows)
    fees_separable = gross_rows and abs(fees) > 1e-9
    dust = [r for r in rows if r["exit_reason"] in DUST_CLASSES]
    nondust = [r for r in rows if r["exit_reason"] not in DUST_CLASSES]

    return {
        "date": day,
        "generated": datetime.now(timezone.utc).isoformat(),
        "declaration": declaration,
        "summary": {
            "n_closes": len(rows),
            "net_usd": round(net, 4),
            "wr_pct": _wr(rows),
            "median_hold_min": _median([r["hold_min"] for r in rows]),
            "fee_drag_usd": round(fees, 4) if fees_separable else None,
            "fee_drag_pct_of_gross": (round(100.0 * fees / abs(gross), 1)
                                      if fees_separable and gross else None),
            "fee_basis": (f"{len(gross_rows)} rows carrying both pnl fields" if fees_separable
                          else "pnl_usd == pnl_net_usd in source; fees not separable" if gross_rows
                          else None),
        },
        "dust_honesty": {
            "n_dust": len(dust),
            "dust_net_usd": round(sum(r["_net"] for r in dust), 4),
            "wr_all_pct": _wr(rows),
            "wr_ex_dust_pct": _wr(nondust),
        },
        "by_exit_class": _rollup(rows, lambda r: r["exit_reason"]),
        "by_strategy_tag": _rollup(rows, lambda r: r["strategy_tag"]),
        "by_plane": _rollup(rows, lambda r: r["plane"]),
        "rows": [{k: v for k, v in r.items() if not k.startswith("_")}
                 | {"net_usd": round(r["_net"], 4), "mfe_pct": r["_mfe"]}
                 for r in rows],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()
    day = args.date or datetime.fromtimestamp(
        __import__("time").time() - 86400, tz=timezone.utc).strftime("%Y-%m-%d")
    try:
        result = run(day)
    except Exception as e:  # exit-0 doctrine: a broken run writes the error
        result = {"date": day, "error": str(e)[:200],
                  "generated": datetime.now(timezone.utc).isoformat()}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(args.out), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(result, f, indent=1)
        os.replace(tmp, args.out)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    s = result.get("summary") or {}
    print(json.dumps({"date": day, "n_closes": s.get("n_closes"),
                      "net_usd": s.get("net_usd"), "wr_pct": s.get("wr_pct"),
                      "error": result.get("error")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
