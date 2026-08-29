"""Gate economics — deterministic gate-value precompute for the watchdog.

Operator directive 2026-08-29: the watchdog produces gate-economics
spreadsheets every 3 days and 7 days to decide whether a gate needs
recalibration. Judgment is the LLM's job; arithmetic is not — this tool
does the arithmetic and emits the table, the CSVs, and the recalibration
candidate flags. Runs standalone (bot up or down), stdlib-only, exit 0
always (best-effort doctrine, same as tools/daily_digest.py).

    .venv/bin/python tools/gate_economics.py [--window 3d|7d|all]

Reads:  logs/shadow_scored.jsonl (append-only counterfactual verdicts;
        ts = epoch seconds of the refusal, pnl_24h = % move to +24h).
Writes: logs/gate_economics_{3d,7d,all}.json (atomic),
        logs/gate_economics/gate_economics_{window}_{date}.csv (spreadsheet),
        one history line per window in logs/gate_economics_history.jsonl.
Stdout: the ASCII gate table the watchdog pastes into report.md / Telegram.

Verdict doctrine (identical to the 2026-08-29 refused-trades audit):
  stopped OR pnl_24h < 0  → the refusal SAVED a loser (value = -pnl)
  pnl_24h > 0             → the refusal MISSED a winner (cost = pnl)
  accuracy = saved / (saved + missed); net = avoided − missed (pct points,
  unweighted — a tail metric, not capital-weighted).

Recalibration flags (evidence bar, Aronson):
  recalibrate_candidate: n≥30 in BOTH 3d and 7d windows AND net < 0 in both
                         AND avg missed per winner > 2× avg avoided per loser.
  disable_candidate:     accuracy < 60% with net < 0 on the 7d window (n≥30).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
SHADOW_PATH = os.path.join(LOG_DIR, "shadow_scored.jsonl")
CSV_DIR = os.path.join(LOG_DIR, "gate_economics")
HISTORY_PATH = os.path.join(LOG_DIR, "gate_economics_history.jsonl")

WINDOWS_S = {"3d": 3 * 86400, "7d": 7 * 86400, "all": None}
MIN_N_FLAG = 30
TAIL_ASYMMETRY = 2.0
DISABLE_ACC = 0.60


# ── Pure analysis (unit-tested, no I/O) ──────────────────────────────────────

def verdict_of(rec: dict) -> str:
    """saved_loser | missed_winner | scratch | unscored."""
    if rec.get("stopped"):
        return "saved_loser"
    p = rec.get("pnl_24h")
    if p is None:
        return "unscored"
    try:
        p = float(p)
    except (TypeError, ValueError):
        return "unscored"
    if p < 0:
        return "saved_loser"
    if p > 0:
        return "missed_winner"
    return "scratch"


def gate_rollup(records: list) -> list:
    """Per-gate economics rows, sorted by n_refused desc."""
    gates = defaultdict(list)
    for r in records:
        gates[r.get("gate") or "unknown"].append(r)
    rows = []
    for g, xs in gates.items():
        verdicts = [(verdict_of(x), x) for x in xs]
        saved = [x for v, x in verdicts if v == "saved_loser"]
        missed = [x for v, x in verdicts if v == "missed_winner"]

        def _p(x):
            try:
                return float(x.get("pnl_24h") or 0.0)
            except (TypeError, ValueError):
                return 0.0

        avoided = sum(-_p(x) for x in saved)
        missed_sum = sum(_p(x) for x in missed)
        denom = len(saved) + len(missed)
        big = sorted((x for x in missed if _p(x) > 2.0), key=_p, reverse=True)
        rows.append({
            "gate": g,
            "n_refused": len(xs),
            "saved_losers": len(saved),
            "missed_winners": len(missed),
            "accuracy_pct": round(100.0 * len(saved) / denom, 1) if denom else None,
            "losses_avoided_pct": round(avoided, 1),
            "gains_missed_pct": round(missed_sum, 1),
            "net_value_pct": round(avoided - missed_sum, 1),
            "avg_avoided_per_loser": round(avoided / len(saved), 3) if saved else None,
            "avg_missed_per_winner": round(missed_sum / len(missed), 3) if missed else None,
            "big_missed_gt2pct": len(big),
            "big_missed_detail": "; ".join(
                f"{x.get('symbol')} {x.get('direction')} +{_p(x):.1f}%" for x in big[:5]),
        })
    rows.sort(key=lambda r: -r["n_refused"])
    return rows


def missed_cohorts(records: list, top: int = 5) -> dict:
    """The leverage map: where refused WINNERS concentrate, by regime cohort.

    Gate totals say HOW MUCH a gate costs; cohorts say WHERE the tail lives
    (gate × day_type, gate × session). The 2026-08-29 discovery — 199/200
    recovery-skip missed winners on trend days — is invisible without this
    slice. Sums pnl_24h of missed winners per cohort, desc.
    """
    def _p(x):
        try:
            return float(x.get("pnl_24h") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    missed = [r for r in records if verdict_of(r) == "missed_winner"]
    out = {}
    for label, key_fn in (
        ("by_day_type", lambda r: (r.get("gate") or "unknown", r.get("day_type") or "unknown")),
        ("by_session", lambda r: (r.get("gate") or "unknown", r.get("session") or "unknown")),
        ("by_direction", lambda r: (r.get("gate") or "unknown", r.get("direction") or "unknown")),
    ):
        agg = defaultdict(lambda: [0, 0.0])
        for r in missed:
            k = key_fn(r)
            agg[k][0] += 1
            agg[k][1] += _p(r)
        out[label] = [{"cohort": f"{k[0]} × {k[1]}", "n": v[0],
                       "missed_pct": round(v[1], 1)}
                      for k, v in sorted(agg.items(), key=lambda kv: -kv[1][1])[:top]]
    return out


def recalibration_flags(rows_3d: list, rows_7d: list) -> list:
    """Evidence-bar flags. Loosening needs BOTH windows negative with n≥30
    and a ≥2× tail asymmetry; disable needs an accuracy collapse on 7d."""
    by3 = {r["gate"]: r for r in rows_3d}
    flags = []
    for r7 in rows_7d:
        g = r7["gate"]
        r3 = by3.get(g)
        if (r7["n_refused"] >= MIN_N_FLAG and r7["net_value_pct"] < 0
                and r7["accuracy_pct"] is not None
                and r7["accuracy_pct"] < 100.0 * DISABLE_ACC):
            flags.append({"gate": g, "flag": "disable_candidate",
                          "evidence": {"net_7d": r7["net_value_pct"],
                                       "acc_7d": r7["accuracy_pct"],
                                       "n_7d": r7["n_refused"]}})
            continue
        if (r3 and r3["n_refused"] >= MIN_N_FLAG and r7["n_refused"] >= MIN_N_FLAG
                and r3["net_value_pct"] < 0 and r7["net_value_pct"] < 0
                and r3["avg_missed_per_winner"] and r3["avg_avoided_per_loser"]
                and r3["avg_missed_per_winner"] > TAIL_ASYMMETRY * r3["avg_avoided_per_loser"]):
            flags.append({"gate": g, "flag": "recalibrate_candidate",
                          "evidence": {"net_3d": r3["net_value_pct"],
                                       "net_7d": r7["net_value_pct"],
                                       "n_3d": r3["n_refused"], "n_7d": r7["n_refused"],
                                       "tail_asymmetry": round(
                                           r3["avg_missed_per_winner"]
                                           / max(r3["avg_avoided_per_loser"], 1e-9), 2)}})
    return flags


def ascii_table(rows: list, title: str) -> str:
    lines = [f"Gate economics — {title} (shadow-scored to +24h, unweighted %-points)",
             f"{'Gate':<22}{'Refused':>8}{'Acc':>7}{'Avoided':>10}{'Missed':>9}{'Net':>9}"]
    for r in rows:
        acc = f"{r['accuracy_pct']}%" if r["accuracy_pct"] is not None else "n/a"
        lines.append(f"{r['gate']:<22}{r['n_refused']:>8}{acc:>7}"
                     f"{('+' + str(r['losses_avoided_pct']) + '%'):>10}"
                     f"{(str(-r['gains_missed_pct']) + '%'):>9}"
                     f"{('+' if r['net_value_pct'] >= 0 else '') + str(r['net_value_pct']) + '%':>9}")
    tot_a = sum(r["losses_avoided_pct"] for r in rows)
    tot_m = sum(r["gains_missed_pct"] for r in rows)
    lines.append(f"Stack: +{round(tot_a, 1)}% avoided vs -{round(tot_m, 1)}% missed "
                 f"= net {('+' if tot_a - tot_m >= 0 else '')}{round(tot_a - tot_m, 1)}%")
    return "\n".join(lines)


# ── I/O shell (best-effort) ──────────────────────────────────────────────────

def _load_records() -> list:
    out = []
    try:
        with open(SHADOW_PATH) as f:
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


def _atomic_write(path: str, payload: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", choices=["3d", "7d", "all"], default=None,
                    help="single window; default emits all three")
    args = ap.parse_args()
    windows = [args.window] if args.window else ["3d", "7d", "all"]

    recs = _load_records()
    now = time.time()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        os.makedirs(CSV_DIR, exist_ok=True)
    except Exception:
        pass

    results = {}
    for w in windows:
        horizon = WINDOWS_S[w]
        try:
            subset = [r for r in recs
                      if horizon is None or float(r.get("ts") or 0) >= now - horizon]
            rows = gate_rollup(subset)
            results[w] = rows
            cohorts = missed_cohorts(subset)
            payload = {"window": w, "generated_at": datetime.now(timezone.utc).isoformat(),
                       "n_records": len(subset), "gates": rows,
                       "missed_cohorts": cohorts}
            _atomic_write(os.path.join(LOG_DIR, f"gate_economics_{w}.json"),
                          json.dumps(payload, indent=1))
            try:
                csv_path = os.path.join(CSV_DIR, f"gate_economics_{w}_{stamp}.csv")
                with open(csv_path, "w", newline="") as f:
                    if rows:
                        wcsv = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                        wcsv.writeheader()
                        wcsv.writerows(rows)
            except Exception:
                pass
            try:
                with open(HISTORY_PATH, "a") as f:
                    f.write(json.dumps({"ts": now, "window": w, "n": len(subset),
                                        "net_stack_pct": round(
                                            sum(r["net_value_pct"] for r in rows), 1),
                                        "gates_negative": [r["gate"] for r in rows
                                                           if r["net_value_pct"] < 0
                                                           and r["n_refused"] >= MIN_N_FLAG]}) + "\n")
            except Exception:
                pass
            print(ascii_table(rows, w))
            for label, cs in cohorts.items():
                if cs:
                    print(f"  missed-alpha {label}: " + "; ".join(
                        f"{c['cohort']} n={c['n']} +{c['missed_pct']}%" for c in cs[:3]))
            print()
        except Exception as e:  # best-effort: one window's failure never blanks the rest
            print(f"gate_economics[{w}] error: {e}")

    if "3d" in results and "7d" in results:
        flags = recalibration_flags(results["3d"], results["7d"])
        try:
            for w in ("3d", "7d", "all"):
                p = os.path.join(LOG_DIR, f"gate_economics_{w}.json")
                if os.path.exists(p):
                    with open(p) as f:
                        d = json.load(f)
                    d["recalibration_flags"] = flags
                    _atomic_write(p, json.dumps(d, indent=1))
        except Exception:
            pass
        if flags:
            print("RECALIBRATION FLAGS:")
            for fl in flags:
                print(f"  {fl['flag']:<24} {fl['gate']:<22} {json.dumps(fl['evidence'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
