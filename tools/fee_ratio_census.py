#!/usr/bin/env python3
"""fee_ratio_census — retrospective per-class fee-drag census (observer-class).

Doctrine: a trade class whose round-trip cost eats >30% of its average gross
take-profit is structurally unprofitable and should be retired. ARIA has
entry-side fee gates (main.py:7052-7080, 18010-18023) but no RETROSPECTIVE
per-class census. This tool is that census.

Cell = (symbol, personality, venue). Only cells with n>=10 realized closes
are verdicted; small cells are reported n_thin, never verdicted. Gross-per-
win is the REALIZED average gross pnl of winning closes in the cell — never
nominal TP distances. Round-trip cost uses the live SoDEX fee tier from
core/fee_engine.py (lazy import — the tool still runs if repo imports
break) plus a spread-estimate leg when the record carries one.

The SHADOW-REFUSED population (logs/shadow_scored.jsonl — counterfactual
outcomes of gate-refused candidates) is censused separately: are we refusing
things that would have been profitable net of fees?

ADVISORY ONLY. The tool changes nothing live. Verdict "retire_candidate" =
cost_ratio > 0.30 with n>=10. Writes logs/fee_ratio_census.json (atomic
tmp+replace) + one append line to logs/fee_ratio_census_history.jsonl.
Best-effort doctrine: every section self-errors, exit code always 0.

Usage: python3 tools/fee_ratio_census.py
"""
from __future__ import annotations

import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)  # lazy repo imports work when run as a script

LOG_DIR = os.path.join(REPO_ROOT, "logs")
OUT_PATH = os.path.join(LOG_DIR, "fee_ratio_census.json")
HIST_PATH = os.path.join(LOG_DIR, "fee_ratio_census_history.jsonl")
SHADOW_PATH = os.path.join(LOG_DIR, "shadow_scored.jsonl")

MIN_N = 10                 # census bar (Aronson): no verdicts below n=10
RETIRE_COST_RATIO = 0.30   # >30% of avg gross win eaten by round-trip cost

# Fallback Tier-0 rates with the live 5% SOSO-staking discount applied
# (CLAUDE.md 2026-09: SOSO_STAKED=168 → 5%). Used only when the lazy
# core.fee_engine import fails — matches the digest's measured schedule
# (7.588bp median RT taker-taker, s29 census).
FALLBACK_SODEX_TAKER = 0.00040 * 0.95
FALLBACK_SODEX_MAKER = 0.00012 * 0.95
# Aster schedule (docs fee table, no staking token wired): crypto taker
# 0.04%/side, maker 0%; stock/commodity perps taker 0.009%/side.
ASTER_TAKER_CRYPTO = 0.00040
ASTER_TAKER_TRADFI = 0.00009
ASTER_MAKER = 0.0

MAKER_ORDER_TYPES = {"limit", "gtx", "post_only", "post-only", "maker",
                     "limit_gtx", "limit_gtc"}


# ── Pure helpers (unit-tested, no I/O) ───────────────────────────────────────

def pnl_gross(r: dict) -> float | None:
    v = r.get("pnl_usd")
    if isinstance(v, (int, float)):
        return float(v)
    v = r.get("pnl_net_usd")
    return float(v) if isinstance(v, (int, float)) else None


def is_phantom_record(r: dict) -> bool:
    """Delegates to memory.trade_journal.is_phantom_record (the shared
    any-date predicate); the inline fallback is the same predicate so a
    broken venv can never silently unfilter. Same idiom as daily_digest."""
    try:
        from memory.trade_journal import is_phantom_record as _shared
        return _shared(r)
    except Exception:
        if r.get("symbol") != "SPCX-USD":
            return False
        return abs(float(r.get("pnl_usd") or r.get("pnl_net_usd") or 0.0)) > 100.0


def norm_venue(r: dict) -> str:
    """Venue for the cell key. Pre-2026-08 journal rows carry no venue field —
    that era is SoDEX-only by construction, so the default is honest."""
    v = r.get("venue")
    return str(v).lower() if isinstance(v, str) and v else "sodex"


def norm_personality(r: dict) -> str:
    p = r.get("personality")
    return str(p) if isinstance(p, str) and p else "unknown"


def is_realized_close(r: dict) -> bool:
    return bool(r.get("closed_at_ms")) and pnl_gross(r) is not None


def dedup_closes(records: list[dict]) -> tuple[list[dict], int]:
    """Journal-integrity class: rolling-window day-file overlap dupes inflate
    every census (a past audit found 4x duplication). Dedup by
    (entry_id, closed_at_ms) — same key as the digest and exit_autopsy."""
    seen: set = set()
    out = []
    dupes = 0
    for r in records:
        key = (r.get("entry_id"), r.get("closed_at_ms"))
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        out.append(r)
    return out, dupes


def maker_entry(order_type: str | None) -> bool:
    return str(order_type or "").lower() in MAKER_ORDER_TYPES


def round_trip_rate(venue: str, symbol: str, order_type: str | None,
                    sodex_taker: float, sodex_maker: float,
                    aster_tradfi_symbols: set | None = None) -> float:
    """Round-trip fee fraction. Entry leg honors the journaled order type;
    the exit leg is always taker (house doctrine: maker-first binds entries,
    exits cross the spread)."""
    if venue == "aster":
        taker = (ASTER_TAKER_TRADFI
                 if aster_tradfi_symbols and symbol in aster_tradfi_symbols
                 else ASTER_TAKER_CRYPTO)
        entry = ASTER_MAKER if maker_entry(order_type) else taker
        return entry + taker
    entry = sodex_maker if maker_entry(order_type) else sodex_taker
    return entry + sodex_taker


def close_notional_usd(r: dict) -> float:
    """Entry-plane notional of the closed position (size class proxy)."""
    try:
        return abs(float(r.get("position_size") or 0.0)
                   * float(r.get("entry_price") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def spread_leg_usd(r: dict) -> float | None:
    """Spread-estimate leg, if derivable from the record. The journal carries
    slippage_expected_usd at intent time; None = not derivable (caller notes
    the skip, never fabricates)."""
    v = r.get("slippage_expected_usd")
    if isinstance(v, (int, float)) and v > 0:
        return float(v)
    return None


def close_cost_usd(r: dict, rt_rate: float) -> tuple[float, bool]:
    """(round-trip cost in USD, spread_leg_applied)."""
    notional = close_notional_usd(r)
    cost = notional * rt_rate
    leg = spread_leg_usd(r)
    if leg is not None:
        cost += leg
    return cost, leg is not None


def cell_key(r: dict) -> str:
    return f"{r.get('symbol', '?')}|{norm_personality(r)}|{norm_venue(r)}"


def verdict_for(n: int, cost_ratio: float | None) -> str:
    """ADVISORY ONLY. n<MIN_N cells are never verdicted (n_thin)."""
    if n < MIN_N:
        return "n_thin"
    if cost_ratio is None:
        return "no_gross_wins"
    if cost_ratio > RETIRE_COST_RATIO:
        return "retire_candidate"
    return "viable"


def census_cells(closes: list[dict], sodex_taker: float, sodex_maker: float,
                 aster_tradfi_symbols: set | None = None) -> dict:
    """Group realized closes into (symbol, personality, venue) cells and grade
    each: avg realized gross-per-win vs avg round-trip cost."""
    cells: dict[str, list[dict]] = defaultdict(list)
    for r in closes:
        cells[cell_key(r)].append(r)

    out: dict[str, dict] = {}
    for key, rs in sorted(cells.items()):
        n = len(rs)
        wins = [x for x in rs if (pnl_gross(x) or 0.0) > 0]
        avg_gross_win = (sum(pnl_gross(x) for x in wins) / len(wins)
                         if wins else None)
        costs, spread_applied, no_notional = [], 0, 0
        for x in rs:
            if close_notional_usd(x) <= 0:
                no_notional += 1
                continue
            rt = round_trip_rate(norm_venue(x), str(x.get("symbol", "")),
                                 x.get("order_type_used"),
                                 sodex_taker, sodex_maker,
                                 aster_tradfi_symbols)
            c, applied = close_cost_usd(x, rt)
            costs.append(c)
            spread_applied += 1 if applied else 0
        avg_cost = sum(costs) / len(costs) if costs else None
        cost_ratio = (avg_cost / avg_gross_win
                      if avg_cost is not None and avg_gross_win
                      else None)
        out[key] = {
            "n": n,
            "n_wins": len(wins),
            # explicit: a low cost_ratio with a low win_rate can still bleed —
            # read pnl_net_sum_usd alongside the verdict before acting
            "win_rate": round(len(wins) / n, 3),
            "avg_gross_win_usd": (round(avg_gross_win, 6)
                                  if avg_gross_win is not None else None),
            "avg_round_trip_cost_usd": (round(avg_cost, 6)
                                        if avg_cost is not None else None),
            "cost_ratio": (round(cost_ratio, 4)
                           if cost_ratio is not None else None),
            "pnl_net_sum_usd": round(sum(
                float(x.get("pnl_net_usd") if isinstance(x.get("pnl_net_usd"), (int, float))
                      else pnl_gross(x) or 0.0) for x in rs), 4),
            "spread_leg_records": spread_applied,
            "skipped_no_notional": no_notional,
            "verdict": verdict_for(n, cost_ratio),
        }
    return out


def read_jsonl(path: str) -> list[dict]:
    """One-bad-line doctrine: a corrupt line kills one record, not the read."""
    out = []
    try:
        with open(path, errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except Exception:
        return []
    return out


def shadow_pnl_pct(rec: dict) -> float | None:
    """24h counterfactual return (%) of a refused candidate, if finalized."""
    v = rec.get("pnl_24h")
    if isinstance(v, (int, float)):
        return float(v)
    scored = rec.get("scored")
    if isinstance(scored, dict):
        v = scored.get("24h")
        if isinstance(v, (int, float)):
            return float(v)
    return None


def shadow_census(records: list[dict], rt_bps: float) -> dict:
    """Fee-ratio census of the SHADOW-REFUSED population, per gate + pooled.
    Shadow records carry % returns, not USD — the ratio is computed in bps:
    cost_ratio = round_trip_cost_bps / avg_winning_return_bps. A winning
    shadow = pnl_24h > 0 and the hypothetical stop was never hit. Stopped
    records can NEVER count as net-of-fees profitable: their counterfactual
    realized the stop loss, not the 24h mark."""
    cohorts: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        if shadow_pnl_pct(r) is None:
            continue
        cohorts[str(r.get("gate") or "unknown")].append(r)
        cohorts["_all"].append(r)

    out: dict[str, dict] = {}
    for gate, rs in sorted(cohorts.items()):
        n = len(rs)
        pnls = [shadow_pnl_pct(r) for r in rs]  # percent
        wins = [p for r, p in zip(rs, pnls)
                if p > 0 and not r.get("stopped")]
        avg_win_bps = (sum(wins) / len(wins)) * 100.0 if wins else None
        cost_ratio = (rt_bps / avg_win_bps if avg_win_bps else None)
        net_profitable = sum(1 for r, p in zip(rs, pnls)
                             if p * 100.0 > rt_bps and not r.get("stopped"))
        net_share = net_profitable / n if n else None
        out[gate] = {
            "n": n,
            "n_wins": len(wins),
            "avg_win_bps": round(avg_win_bps, 2) if avg_win_bps else None,
            "round_trip_cost_bps": round(rt_bps, 2),
            "cost_ratio": round(cost_ratio, 4) if cost_ratio is not None else None,
            "net_of_fees_profitable_share": (round(net_share, 3)
                                             if net_share is not None else None),
            "verdict": verdict_for(n, cost_ratio),
        }
    return out


def atomic_write_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


# ── I/O sections (self-erroring; one bad source never blanks the report) ─────

def effective_sodex_rates() -> dict:
    """Live SoDEX perp rates from core.fee_engine (lazy import — the tool
    still runs if repo imports break). SOSO_STAKED from env; the live value
    (168 → 5% discount) is the documented default."""
    soso = 168.0
    try:
        soso = float(os.getenv("SOSO_STAKED", "168"))
    except Exception:
        pass
    try:
        from core.fee_engine import SoDEXFeeEngine
        eng = SoDEXFeeEngine(soso_staked=soso, weighted_14d_volume=0.0)
        return {"taker": eng.perps_taker_fee(), "maker": eng.perps_maker_fee(),
                "tier": eng.current_tier(), "soso_staked": soso,
                "source": "core.fee_engine.SoDEXFeeEngine"}
    except Exception as e:
        return {"taker": FALLBACK_SODEX_TAKER, "maker": FALLBACK_SODEX_MAKER,
                "tier": 0, "soso_staked": soso,
                "source": f"fallback constants (fee_engine import failed: "
                          f"{str(e)[:80]})"}


def aster_tradfi_symbols() -> set:
    """Tradfi symbols for the Aster 0.009% taker schedule. Lazy import;
    empty set (all-aster-crypto, conservative high) on failure."""
    try:
        from data.tradfi_feed import TRADFI_SYMBOLS
        return set(TRADFI_SYMBOLS)
    except Exception:
        return set()


def load_journal_closes() -> dict:
    """All trade_journal_*.json under logs/ (the rolling window), phantom-
    filtered, cross-file deduped by (entry_id, closed_at_ms)."""
    files = sorted(glob.glob(os.path.join(LOG_DIR, "trade_journal_*.json")))
    records, phantoms, read_errors = [], 0, 0
    first_ms = last_ms = None
    for path in files:
        try:
            recs = json.load(open(path))
        except Exception:
            read_errors += 1
            continue
        if not isinstance(recs, list):
            read_errors += 1
            continue
        for r in recs:
            if not isinstance(r, dict) or not is_realized_close(r):
                continue
            if is_phantom_record(r):
                phantoms += 1
                continue
            records.append(r)
            ms = int(r.get("closed_at_ms") or 0)
            if ms:
                first_ms = ms if first_ms is None else min(first_ms, ms)
                last_ms = ms if last_ms is None else max(last_ms, ms)
    deduped, dupes = dedup_closes(records)
    window = None
    if first_ms and last_ms:
        window = [datetime.fromtimestamp(first_ms / 1000, timezone.utc).isoformat(),
                  datetime.fromtimestamp(last_ms / 1000, timezone.utc).isoformat()]
    return {"files": len(files), "file_read_errors": read_errors,
            "phantoms_skipped": phantoms, "dupes_skipped": dupes,
            "closes": deduped, "n_closes": len(deduped), "window": window}


# ── Report ───────────────────────────────────────────────────────────────────

def build_report() -> dict:
    report: dict = {
        "tool": "fee_ratio_census",
        "generated": datetime.now(timezone.utc).isoformat(),
        "advisory_only": True,
        "doctrine": ("cost_ratio = avg round-trip cost USD / avg REALIZED gross "
                     "win USD per (symbol, personality, venue) cell; "
                     f"retire_candidate = cost_ratio > {RETIRE_COST_RATIO} "
                     f"with n>={MIN_N}. Verdicts change nothing live."),
        "errors": {},
    }

    # Section 1: fee model
    try:
        fees = effective_sodex_rates()
        report["fee_model"] = {
            "sodex_taker_frac": fees["taker"], "sodex_maker_frac": fees["maker"],
            "sodex_taker_bps": round(fees["taker"] * 1e4, 3),
            "sodex_maker_bps": round(fees["maker"] * 1e4, 3),
            "sodex_tier": fees["tier"], "soso_staked": fees["soso_staked"],
            "source": fees["source"],
            "aster_taker_crypto_frac": ASTER_TAKER_CRYPTO,
            "aster_taker_tradfi_frac": ASTER_TAKER_TRADFI,
            "exit_leg": "always taker (maker-first binds entries; exits cross)",
        }
    except Exception as e:
        report["errors"]["fee_model"] = str(e)[:200]
        fees = {"taker": FALLBACK_SODEX_TAKER, "maker": FALLBACK_SODEX_MAKER}

    # Section 2: journal census
    try:
        j = load_journal_closes()
        tradfi = aster_tradfi_symbols()
        cells = census_cells(j.pop("closes"), fees["taker"], fees["maker"], tradfi)
        verdicted = [k for k, c in cells.items()
                     if c["verdict"] in ("retire_candidate", "viable")]
        retirees = sorted((k for k in verdicted
                           if cells[k]["verdict"] == "retire_candidate"),
                          key=lambda k: -(cells[k]["cost_ratio"] or 0))
        worst = sorted((k for k in verdicted
                        if cells[k]["cost_ratio"] is not None),
                       key=lambda k: -(cells[k]["cost_ratio"] or 0))[:10]
        spread_records = sum(c["spread_leg_records"] for c in cells.values())
        report["journal"] = j
        report["cells"] = cells
        report["summary"] = {
            "cells_total": len(cells),
            "cells_verdicted": len(verdicted),   # multiple-comparison count
            "cells_n_thin": sum(1 for c in cells.values()
                                if c["verdict"] == "n_thin"),
            "retire_candidates": retirees,
            "worst_cost_ratio_cells": [
                {"cell": k, "n": cells[k]["n"],
                 "cost_ratio": cells[k]["cost_ratio"]} for k in worst],
            "multiple_comparison_note": (
                f"{len(verdicted)} cells were verdicted this run — a "
                "retire_candidate flag is one cell of many; confirm before "
                "acting (Aronson)."),
            "spread_leg": ("applied on %d records (slippage_expected_usd)"
                           % spread_records if spread_records else
                           "skipped — slippage_expected_usd absent/zero in "
                           "the journal plane"),
        }
    except Exception as e:
        report["errors"]["journal"] = str(e)[:200]
        report.setdefault("cells", {})
        report.setdefault("summary", {})

    # Section 3: shadow-refused census
    try:
        rt_bps = (fees["taker"] * 2.0) * 1e4  # sodex taker-taker (NOT cross-
        # venue worst case: aster crypto taker-taker is 8.0bp — see fee_model)
        records = read_jsonl(SHADOW_PATH)
        # dedup by id, last wins (same doctrine as shadow_journal._load_scored)
        by_id: dict[str, dict] = {}
        for r in records:
            if r.get("id"):
                by_id[r["id"]] = r
        report["shadow_refused"] = {
            "path": SHADOW_PATH,
            "records": len(records),
            "unique_ids": len(by_id),
            "note": ("Counterfactual outcomes of gate-refused candidates, in "
                     "bps of notional (no USD sizing on refusals). cost_ratio "
                     "= taker-taker round-trip bps / avg winning 24h return "
                     "bps. High net_of_fees_profitable_share = we refuse "
                     "things that would have been profitable net of fees."),
            "by_gate": shadow_census(list(by_id.values()), rt_bps),
        }
    except Exception as e:
        report["errors"]["shadow_refused"] = str(e)[:200]

    return report


def main() -> None:
    try:
        report = build_report()
    except Exception as e:  # exit-0 doctrine: a broken run writes the error
        report = {"tool": "fee_ratio_census", "error": str(e)[:200],
                  "generated": datetime.now(timezone.utc).isoformat()}
    try:
        atomic_write_json(OUT_PATH, report)
    except Exception:
        pass
    try:
        s = report.get("summary") or {}
        hist = {"generated": report.get("generated"),
                "n_closes": (report.get("journal") or {}).get("n_closes"),
                "dupes_skipped": (report.get("journal") or {}).get("dupes_skipped"),
                "cells_total": s.get("cells_total"),
                "cells_verdicted": s.get("cells_verdicted"),
                "retire_candidates": s.get("retire_candidates"),
                "worst": s.get("worst_cost_ratio_cells", [])[:3],
                "errors": sorted((report.get("errors") or {}).keys())}
        with open(HIST_PATH, "a") as f:
            f.write(json.dumps(hist) + "\n")
    except Exception:
        pass
    s = report.get("summary") or {}
    print(json.dumps({
        "generated": report.get("generated"),
        "n_closes": (report.get("journal") or {}).get("n_closes"),
        "cells_total": s.get("cells_total"),
        "cells_verdicted": s.get("cells_verdicted"),
        "retire_candidates": s.get("retire_candidates"),
        "errors": report.get("errors") or {},
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
