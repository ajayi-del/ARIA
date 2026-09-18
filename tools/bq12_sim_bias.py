"""bq12 sim bias — transfer error of the realistic-fill sim on REFUSED signals
that LATER FILLED (CEO s39 bq-12; the ONE assumption under every gate verdict).

The gate-verdict machinery (tools/gate_realistic_fill.py sim_arm) scores every
refusal counterfactually: "had it traded, it would have made X". oracle-3
validated that sim on the ADMITTED book (bias -0.87bp/row, n=745). But every
gate verdict applies the sim to the REFUSED population — a transfer. This tool
measures the transfer error on the cohort where the transfer can be checked:
shadow refusals whose (symbol, direction) actually filled in trade_db within
24h of the refusal. c113 census: n=4,695 all-time joins, 649 refused-then-
filled post-09-15 — 21x the n>=30 bar.

Two arms per joined row:
  ARM A (transfer error, the estimand) — sim the SHADOW record exactly as
    sim_arm does (same tape, same cost model, same exit stack) -> sim_pnl_pct.
    Realized = the actual fill's net_pnl / notional_usd x 100 (fee-inclusive,
    comparable to the sim's cost-inclusive pct). bias_row = sim - realized.
    Verdict: |median bias| <= 2x the admitted-book benchmark (0.87bp) ->
    transferable; else the gap is named per gate.
  ARM B (replay fidelity, oracle-3 style) — replay the ACTUAL fill
    (entry_price, stop_price, tp1_price) through simulate_exit on tape from
    the open. Exit-reason bucket match rate + exit-price error in bps.
    A fidelity failure here means the gap lives in the simulator, not the
    transfer.

Join doctrine: refusal -> NEAREST later fill (symbol, direction, open in
(ts, ts+86400]). Refusal-level rows are primary (the estimand is per-refusal);
fill-level dedup (each fill counted once, nearest preceding refusal) reported
as robustness. One-bad-line, exit-0 best-effort (same as gate_realistic_fill).

    .venv/bin/python tools/bq12_sim_bias.py [--window 7d|all]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.gate_realistic_fill import (  # noqa: E402
    HORIZON_S,
    LOG_DIR,
    SHADOW_PATH,
    TRADE_DB_PATH,
    _atomic_write,
    _exec_context,
    _load_jsonl,
    _path_for,
    _venue_maps,
    entry_fill,
    exit_fill,
    fee_side,
    make_tp,
    reason_bucket,
    sim_pnl_pct,
    simulate_exit,
    tape_class,
)

OUT_PATH = os.path.join(LOG_DIR, "bq12_sim_bias.json")
HISTORY_PATH = os.path.join(LOG_DIR, "bq12_sim_bias_history.jsonl")

JOIN_WINDOW_S = 86400.0          # refusal -> fill within 24h (CEO spec)
ADMITTED_BIAS_BP = -0.87         # oracle-3 admitted-book benchmark (CEO s39)
TRANSFER_TOL_MULT = 2.0          # verdict bar: |median bias| <= 2x benchmark
MIN_N = 30                       # CEO bar


# ── Pure join + bias math (unit-tested, no I/O) ─────────────────────────────

def join_refusals_to_fills(refusals: list, fills: list) -> list:
    """ refusal -> nearest later fill on (symbol, direction, open in
    (ts, ts+86400]). Returns [{'rec', 'fill'}] refusal-level rows."""
    by_key = defaultdict(list)
    for f in fills:
        try:
            sym = f.get("symbol") or ""
            side = f.get("side") or ""
            open_s = float(f.get("timestamp_open_ms") or 0) / 1000.0
        except (TypeError, ValueError):
            continue
        if sym and side and open_s > 0:
            by_key[(sym, side)].append((open_s, f))
    for xs in by_key.values():
        xs.sort(key=lambda t: t[0])
    rows = []
    for r in refusals:
        try:
            key = (r.get("symbol") or "", r.get("direction") or "")
            ts = float(r.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if ts <= 0:
            continue
        best = None
        for open_s, f in by_key.get(key, []):
            if open_s <= ts:
                continue
            if open_s > ts + JOIN_WINDOW_S:
                break
            best = (open_s, f)
            break  # sorted ascending: first later fill is nearest
        if best is not None:
            rows.append({"rec": r, "fill": best[1]})
    return rows


def realized_pnl_pct(fill: dict) -> float | None:
    """Fee-inclusive realized pct on the price plane (net/notional x 100)."""
    try:
        n = float(fill.get("notional_usd") or 0.0)
        p = float(fill.get("net_pnl"))
        if n <= 0:
            return None
        return p / n * 100.0
    except (TypeError, ValueError):
        return None


def bias_stats(biases_bp: list) -> dict:
    if not biases_bp:
        return {"n": 0}
    return {
        "n": len(biases_bp),
        "median_bp": round(median(biases_bp), 2),
        "mean_bp": round(sum(biases_bp) / len(biases_bp), 2),
        "p25_bp": round(sorted(biases_bp)[len(biases_bp) // 4], 2),
        "p75_bp": round(sorted(biases_bp)[3 * len(biases_bp) // 4], 2),
    }


def transfer_verdict(n: int, median_bias_bp: float | None) -> str:
    """transferable = median transfer bias within 2x the admitted-book
    benchmark on n>=30; thin below the bar; gap named otherwise."""
    if n < MIN_N or median_bias_bp is None:
        return "thin"
    if abs(median_bias_bp) <= TRANSFER_TOL_MULT * abs(ADMITTED_BIAS_BP):
        return "transferable"
    direction = "optimistic" if median_bias_bp > 0 else "pessimistic"
    return f"gap_{direction}"


# ── Arms ─────────────────────────────────────────────────────────────────────

def _sim_shadow_record(rec: dict, ctx: dict, aster: set, bybit_map: dict):
    """ARM A inner: replay the refusal exactly as sim_arm does. None = abstain."""
    sym = rec.get("symbol") or ""
    direction = rec.get("direction") or "long"
    try:
        ts = float(rec.get("ts") or 0.0)
        entry_raw = float(rec.get("entry") or 0.0)
        stop = float(rec.get("hyp_stop") or 0.0)
    except (TypeError, ValueError):
        return None
    if ts <= 0 or entry_raw <= 0 or stop <= 0:
        return None
    venue = "aster" if sym in aster else "sodex"
    fee = fee_side(venue)
    cs = ctx["cs"].get(sym, ctx["cs_default"])
    kl = ctx["kyle"].get(sym, ctx["kyle_default"])
    notional = ctx["notional"].get(sym, ctx["notional_default"])
    half_spread = max(cs, 0.0) / 2.0
    impact = max(kl, 0.0) * notional
    path = _path_for(bybit_map[sym], ts, ts + HORIZON_S)
    tp = make_tp(entry_raw, stop, direction)
    sim = simulate_exit(path, ts, direction, entry_raw, stop, tp)
    if sim["reason"] == "no_tape":
        return None
    e_in = entry_fill(entry_raw, direction, half_spread, impact, fee)
    e_out = exit_fill(sim["price"], direction, half_spread, fee)
    return {"sim_pnl_pct": sim_pnl_pct(e_in, e_out, direction),
            "sim_reason": sim["reason"]}


def _replay_actual_fill(fill: dict, bybit_map: dict):
    """ARM B inner: replay the realized trade's own bracket. None = abstain."""
    sym = fill.get("symbol") or ""
    direction = fill.get("side") or "long"
    try:
        ts = float(fill.get("timestamp_open_ms") or 0) / 1000.0
        entry = float(fill.get("entry_price") or 0.0)
        stop = float(fill.get("stop_price") or 0.0)
        tp = float(fill.get("tp1_price") or 0.0)
    except (TypeError, ValueError):
        return None
    if ts <= 0 or entry <= 0 or stop <= 0 or tp <= 0:
        return None
    path = _path_for(bybit_map[sym], ts, ts + HORIZON_S)
    sim = simulate_exit(path, ts, direction, entry, stop, tp)
    if sim["reason"] in ("no_tape", "censored"):
        return None
    return sim


def run(window_start: float) -> dict:
    aster, bybit_map, tradfi = _venue_maps()
    refusals = [r for r in _load_jsonl(SHADOW_PATH)
                if float(r.get("ts") or 0) >= window_start]
    fills = [f for f in _load_jsonl(TRADE_DB_PATH)
             if float(f.get("timestamp_open_ms") or 0) >= window_start * 1000.0]
    joined = join_refusals_to_fills(refusals, fills)

    ctx = _exec_context(window_start)
    arm_a, arm_a_by_gate = [], defaultdict(list)
    arm_b_match, arm_b_total, arm_b_err_bp = 0, 0, []
    abstain = {"tape_class": 0, "sim": 0, "realized": 0, "replay": 0}
    seen_fills = set()

    for row in joined:
        rec, fill = row["rec"], row["fill"]
        sym = rec.get("symbol") or ""
        gate = rec.get("gate") or "unknown"
        if tape_class(sym, bybit_map, tradfi) != "bybit":
            abstain["tape_class"] += 1
            continue
        real_pct = realized_pnl_pct(fill)
        if real_pct is None:
            abstain["realized"] += 1
            continue
        # ARM A — transfer error of the counterfactual sim
        a = _sim_shadow_record(rec, ctx, aster, bybit_map)
        if a is None:
            abstain["sim"] += 1
        else:
            bias_bp = (a["sim_pnl_pct"] - real_pct) * 100.0
            arm_a.append(bias_bp)
            arm_a_by_gate[gate].append(bias_bp)
        # ARM B — replay fidelity on the actual bracket (fill-deduped)
        fid = fill.get("trade_id")
        if fid and fid not in seen_fills:
            seen_fills.add(fid)
            b = _replay_actual_fill(fill, bybit_map)
            if b is None:
                abstain["replay"] += 1
            else:
                arm_b_total += 1
                actual_bucket = reason_bucket(fill.get("exit_reason") or "")
                if b["reason"] == actual_bucket:
                    arm_b_match += 1
                try:
                    err = (b["price"] / float(fill["exit_price"]) - 1.0) * 1e4
                    arm_b_err_bp.append(abs(err))
                except (TypeError, ValueError, ZeroDivisionError, KeyError):
                    pass

    stats = bias_stats(arm_a)
    verdict = transfer_verdict(stats.get("n", 0), stats.get("median_bp"))
    return {
        "ts": __import__("time").time(),
        "window_start": window_start,
        "joined_refusals": len(joined),
        "distinct_fills": len(seen_fills),
        "abstain": abstain,
        "arm_a_transfer": {
            **stats,
            "benchmark_admitted_bp": ADMITTED_BIAS_BP,
            "tol_mult": TRANSFER_TOL_MULT,
            "verdict": verdict,
            "by_gate": {g: bias_stats(xs) for g, xs in
                        sorted(arm_a_by_gate.items(),
                               key=lambda kv: -len(kv[1]))},
        },
        "arm_b_replay": {
            "n": arm_b_total,
            "reason_match_rate": round(arm_b_match / arm_b_total, 3)
            if arm_b_total else None,
            "exit_price_err_median_bp": round(median(arm_b_err_bp), 2)
            if arm_b_err_bp else None,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", choices=["7d", "all"], default="7d")
    args = ap.parse_args()
    import time
    now = time.time()
    window_start = 0.0 if args.window == "all" else now - 7 * 86400
    try:
        result = run(window_start)
    except Exception as exc:  # best-effort doctrine: a failed run is a finding
        result = {"ts": now, "error": str(exc)[:200]}
    try:
        _atomic_write(OUT_PATH, json.dumps(result, indent=1))
        with open(HISTORY_PATH, "a") as f:
            f.write(json.dumps({"ts": result.get("ts"),
                                "window": args.window,
                                "joined": result.get("joined_refusals"),
                                "arm_a": result.get("arm_a_transfer", {}),
                                "arm_b": result.get("arm_b_replay", {}),
                                "error": result.get("error")}) + "\n")
    except Exception:
        pass
    print(json.dumps(result, indent=1)[:3000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
