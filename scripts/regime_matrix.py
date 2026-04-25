"""
scripts/regime_matrix.py — Move 2: Regime-Strategy Performance Matrix

Queries outcomes.db to show win rate per (regime, strategy_type) cell.
Run weekly to identify which strategy types work in which regimes.

Usage:
    cd /path/to/ARIA
    python3 scripts/regime_matrix.py [--min-n 3]
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "logs" / "outcomes.db"


def run(min_n: int = 3):
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT regime, strategy_type, net_pnl_usd, net_pnl_r,
               symbol, direction, exit_reason
        FROM   outcomes
        WHERE  regime IS NOT NULL AND regime != ''
    """)
    rows = cur.fetchall()

    if not rows:
        print("No tagged outcomes yet — need trades with regime/strategy_type fields.")
        print("These populate after the next position closes with the updated ARIA.")
        return

    # Build (regime, strategy) cell stats
    cells: dict = defaultdict(lambda: {"wins": 0, "losses": 0, "total_r": 0.0, "pnl": 0.0})
    regime_totals: dict = defaultdict(lambda: {"wins": 0, "losses": 0})

    for r in rows:
        reg = r["regime"] or "unknown"
        strat = r["strategy_type"] or "unknown"
        won = r["net_pnl_usd"] > 0
        cells[(reg, strat)]["wins"]    += int(won)
        cells[(reg, strat)]["losses"]  += int(not won)
        cells[(reg, strat)]["total_r"] += r["net_pnl_r"] or 0.0
        cells[(reg, strat)]["pnl"]     += r["net_pnl_usd"] or 0.0
        regime_totals[reg]["wins"]     += int(won)
        regime_totals[reg]["losses"]   += int(not won)

    print(f"\n{'─'*80}")
    print("  REGIME × STRATEGY PERFORMANCE MATRIX")
    print(f"  (min_n={min_n} trades to show | ✓ = WR≥55% | ✗ = WR<40% | ~ = marginal)")
    print(f"{'─'*80}")
    print(f"  {'Regime':<22} {'Strategy':<16} {'N':>4} {'WR%':>6} {'Avg-R':>7} {'PnL':>8}  {'Signal'}")
    print(f"{'─'*80}")

    for (reg, strat), s in sorted(cells.items(), key=lambda x: -(x[1]["wins"] + x[1]["losses"])):
        n = s["wins"] + s["losses"]
        if n < min_n:
            continue
        wr = 100.0 * s["wins"] / n
        avg_r = s["total_r"] / n
        pnl = s["pnl"]
        signal = "✓ EDGE" if wr >= 55 else ("✗ AVOID" if wr < 40 else "~ MARGINAL")
        print(f"  {reg:<22} {strat:<16} {n:>4} {wr:>5.1f}% {avg_r:>+7.2f} {pnl:>+8.3f}  {signal}")

    # Per-regime overall
    print(f"\n{'─'*80}")
    print("  REGIME OVERALL WIN RATES")
    print(f"{'─'*80}")
    for reg, s in sorted(regime_totals.items(), key=lambda x: -(x[1]["wins"] + x[1]["losses"])):
        n = s["wins"] + s["losses"]
        if n < min_n:
            continue
        wr = 100.0 * s["wins"] / n
        print(f"  {reg:<30} N={n:>4}  WR={wr:.1f}%")

    conn.close()
    print(f"{'─'*80}\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Regime-strategy performance matrix")
    p.add_argument("--min-n", type=int, default=3, help="Min trades per cell to display")
    args = p.parse_args()
    run(args.min_n)
