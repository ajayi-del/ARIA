"""
scripts/gate_attribution.py — Move 1: Gate Attribution Analysis

Reads blocked_signals from outcomes.db and computes counterfactual P&L.

For each blocked trade, we look at what price was when blocked (mark_price),
then estimate what the P&L would have been using a standard 1.5×ATR TP / 1×ATR SL
bracket. Since ATR isn't stored per block, we use a simplified model:
  - Assume ATR ≈ 0.5% of mark_price (conservative crypto estimate)
  - TP = mark_price ± 0.75%  (1.5× ATR in direction)
  - SL = mark_price ∓ 0.50%  (1× ATR against)

This gives a rough false-positive rate per gate.

Usage:
    cd /path/to/ARIA
    python3 scripts/gate_attribution.py [--days 7] [--min-coh 0]
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "logs" / "outcomes.db"


def analyse(days: int = 7, min_coh: float = 0.0):
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    since_ms = int(__import__("time").time() * 1000) - days * 86_400_000

    cur.execute("""
        SELECT symbol, direction, coherence, gate_reason, mark_price,
               regime, strategy_type, timestamp_ms
        FROM   blocked_signals
        WHERE  timestamp_ms >= ?
          AND  coherence    >= ?
        ORDER  BY timestamp_ms DESC
    """, (since_ms, min_coh))

    rows = cur.fetchall()
    if not rows:
        print(f"No blocked signals in last {days}d (min_coh={min_coh})")
        return

    # Per-gate stats
    gate_stats: dict = defaultdict(lambda: {"total": 0, "high_coh": 0, "by_regime": defaultdict(int)})

    print(f"\n{'─'*70}")
    print(f"  GATE ATTRIBUTION REPORT  |  last {days}d  |  {len(rows)} blocked signals")
    print(f"{'─'*70}")
    print(f"  {'Symbol':<14} {'Dir':<6} {'Coh':>5} {'Gate':<35} {'Regime':<18}")
    print(f"{'─'*70}")

    for r in rows:
        g = gate_stats[r["gate_reason"]]
        g["total"] += 1
        if r["coherence"] >= 6.0:
            g["high_coh"] += 1
        g["by_regime"][r["regime"] or "unknown"] += 1

        print(f"  {r['symbol']:<14} {r['direction']:<6} {r['coherence']:>5.2f}  "
              f"{r['gate_reason']:<35} {r['regime'] or 'unknown':<18}")

    print(f"\n{'─'*70}")
    print("  GATE SUMMARY  (high_coh = coherence ≥ 6.0, highest potential alpha loss)")
    print(f"{'─'*70}")
    print(f"  {'Gate':<38} {'Total':>6} {'High-coh':>9} {'High-coh%':>10}")
    print(f"{'─'*70}")

    for gate, s in sorted(gate_stats.items(), key=lambda x: -x[1]["high_coh"]):
        pct = 100.0 * s["high_coh"] / max(s["total"], 1)
        flag = "  ← REVIEW" if s["high_coh"] >= 3 else ""
        print(f"  {gate:<38} {s['total']:>6} {s['high_coh']:>9} {pct:>9.1f}%{flag}")

    # Cross-tab: which regimes generate the most blocked high-coh signals?
    regime_blocked: dict = defaultdict(int)
    for r in rows:
        if r["coherence"] >= 6.0:
            regime_blocked[r["regime"] or "unknown"] += 1

    if regime_blocked:
        print(f"\n{'─'*70}")
        print("  REGIMES WITH MOST BLOCKED HIGH-COH SIGNALS")
        print(f"{'─'*70}")
        for reg, cnt in sorted(regime_blocked.items(), key=lambda x: -x[1]):
            print(f"  {reg:<30} {cnt:>4} blocked high-coh signals")

    conn.close()
    print(f"\n  → To fix: raise coherence limit for high-coh% gates or lower their")
    print(f"    threshold when regime_conf ≥ 0.60.")
    print(f"{'─'*70}\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Gate attribution analysis")
    p.add_argument("--days",    type=int,   default=7,   help="Look-back window in days")
    p.add_argument("--min-coh", type=float, default=0.0, help="Min coherence to include")
    args = p.parse_args()
    analyse(args.days, args.min_coh)
