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
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.daily_digest import (  # noqa: E402
    load_journal_records, load_outcome_records, pnl_net,
)
from intelligence.plane_ledger import ledger_plane_map, plane_of  # noqa: E402

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
    return ledger_plane_map(_load_jsonl(PLANE_LEDGER))


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


def source_split_nets(outcome_records: list, journal_records: list) -> dict:
    """CEO DIR (day-audit-source-split): both planes' nets side by side.
    Canonical net = digest-canonical JOURNAL plane when it has any closed
    records (daily_digest reads the journal); outcomes otherwise. Neither
    source is mutated. 10-24 pin: journal -5.9409 vs outcomes +6.3127 —
    side-mismatch rows made the outcomes plane unspeakable for reset/EV
    reporting."""
    oc = [r for r in outcome_records if r.get("closed_at_ms")]
    jc = [r for r in journal_records if r.get("closed_at_ms")]
    net_o = sum(pnl_net(r) for r in oc)
    net_j = sum(pnl_net(r) for r in jc)
    source = "journal" if jc else "outcomes"
    return {
        "net_usd_outcomes": round(net_o, 4),
        "net_usd_journal": round(net_j, 4),
        "pnl_source": source,
        "net_usd_canonical": round(net_j if jc else net_o, 4),
        "n_journal": len(jc),
        "n_outcomes": len(oc),
        "source_disagreement_usd": (round(net_j - net_o, 4)
                                    if (jc and oc) else None),
    }


# ── Arcus scalper fills plane (O7 KR2, 2026-11-03) ──────────────────────────
# The scalper is the firm's dominant executor but arcus_scalp_exited carries
# outcome only — no pnl. The venue's public /v1/fills (address-scoped GET,
# no key) carries closedPnl + fee + role per fill: venue truth, no inference.
# Attribution: clientId "scalpe*" = scalp entries; empty clientId + CLOSE_*
# = scalp exits (close_position_market sets no clientId; the account is
# scalper-dedicated). Anything else is reported as unattributed, never
# guessed. Paginates backwards via to=<oldest createdAt us - 1>.
ARCUS_FILLS_URL = "https://api.arcus.xyz/v1/fills"
ARCUS_MAX_PAGES = 16


def _arcus_address() -> str:
    env_path = os.path.join(os.path.dirname(LOG_DIR), ".env")
    try:
        with open(env_path) as f:
            for line in f:
                if line.startswith("ARCUS_ADDRESS="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _arcus_fetch_page(address: str, to_us: int | None) -> list:
    qs = f"address={address}&accountIndex=0"
    if to_us is not None:
        qs += f"&to={to_us}"
    req = urllib.request.Request(f"{ARCUS_FILLS_URL}?{qs}")
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode())
    fills = data.get("fills")
    return fills if isinstance(fills, list) else []


def arcus_fills_section(day: str) -> dict:
    """Per-UTC-day realized PnL for the Arcus scalper, from venue fills."""
    if os.environ.get("ARCUS_DAY_PNL_ENABLED", "1") in ("0", "false", "False"):
        return {"enabled": False}
    address = _arcus_address()
    if not address:
        return {"enabled": True, "error": "ARCUS_ADDRESS not in .env"}
    day_start = int(datetime.strptime(day, "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc).timestamp() * 1_000_000)
    day_end = day_start + 86_400_000_000
    try:
        fills: list = []
        to_us: int | None = None
        complete = False
        for _ in range(ARCUS_MAX_PAGES):
            page = _arcus_fetch_page(address, to_us)
            if not page:
                complete = True
                break
            fills.extend(page)
            oldest = min(int(x.get("createdAt") or 0) for x in page)
            if oldest <= day_start:
                complete = True
                break
            to_us = oldest - 1
        day_fills = [x for x in fills
                     if day_start <= int(x.get("createdAt") or 0) < day_end]
    except Exception as e:
        return {"enabled": True, "error": str(e)[:200]}

    def _cls(x: dict) -> str:
        cid = str(x.get("clientId") or "")
        eff = str(x.get("positionEffect") or "")
        if cid.startswith("scalpe"):
            return "entry"
        if not cid and eff.startswith("CLOSE_"):
            return "exit"
        return "unattributed"

    entries = [x for x in day_fills if _cls(x) == "entry"]
    exits = [x for x in day_fills if _cls(x) == "exit"]
    unattr = [x for x in day_fills if _cls(x) == "unattributed"]
    realized = sum(float(x.get("closedPnl") or 0) for x in exits)
    fees = sum(float(x.get("fee") or 0) for x in entries + exits)
    roles = {}
    for x in entries + exits:
        roles[x.get("role") or "?"] = roles.get(x.get("role") or "?", 0) + 1
    by_sym: dict = {}
    for x in exits:
        s = by_sym.setdefault(x.get("marketDisplayName") or "?",
                              {"n": 0, "realized_usd": 0.0})
        s["n"] += 1
        s["realized_usd"] += float(x.get("closedPnl") or 0)
    coverage = None
    if day_fills:
        ts = [int(x["createdAt"]) for x in day_fills]
        coverage = [datetime.fromtimestamp(min(ts) / 1e6, timezone.utc).isoformat(),
                    datetime.fromtimestamp(max(ts) / 1e6, timezone.utc).isoformat()]
    return {
        "enabled": True,
        "attribution": ("clientId scalpe* = entries; empty clientId + CLOSE_* "
                        "= exits (bot market closes carry no clientId; account "
                        "is scalper-dedicated); other clientIds unattributed"),
        "coverage_utc": coverage,
        "coverage_complete": complete,
        "n_entry_fills": len(entries),
        "n_exit_fills": len(exits),
        "n_unattributed": len(unattr),
        "realized_usd": round(realized, 4),
        "fees_usd": round(fees, 4),
        "net_usd": round(realized - fees, 4),
        "fill_roles": roles,
        "by_symbol": {k: {"n": v["n"], "realized_usd": round(v["realized_usd"], 4)}
                      for k, v in sorted(by_sym.items(),
                                         key=lambda kv: kv[1]["realized_usd"])},
    }


def run(day: str) -> dict:
    outcome_records = load_outcome_records(day)
    journal_records = load_journal_records(day)
    records = outcome_records or journal_records
    split = source_split_nets(outcome_records, journal_records)
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
            "plane": plane_of(t, ledger),
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
        "summary_net_source": "journal when it has closed records (digest-canonical), outcomes otherwise; both nets reported side by side",
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
            "net_usd": split["net_usd_canonical"],
            "net_usd_journal": split["net_usd_journal"],
            "net_usd_outcomes": split["net_usd_outcomes"],
            "pnl_source": split["pnl_source"],
            "source_disagreement_usd": split["source_disagreement_usd"],
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
        "arcus": arcus_fills_section(day),
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
