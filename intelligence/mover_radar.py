"""Mover radar — the cross-pipe missed-move detector.

HYPE (+41%/7d, 2026-08-23 autopsy) and MUBARAK (2026-08-21 silence) are the
SAME failure one pipe apart: a symbol makes a violent move while ARIA holds
zero exposure. HYPE's pipe broke downstream (signals flowed, the daily cap
choked execution); MUBARAK's broke upstream (mark store never written, zero
signals). Gate-by-gate telemetry cannot see either — each gate only sees its
own victims. The observable that cuts across every pipe is market-side:
symbol moving, ARIA flat.

The radar reads PUBLIC 24h ticker moves (feed-independent — a dead internal
feed cannot blind it) and crosses them with participation (trades today,
signals today). Pure brain, zero-I/O; the loop in main.py does the fetching.

  blocked  — signals flowed, no trades: capacity/gate failure downstream.
             Actionable: mover_relief param arms a cap-exemption leg.
  silent   — no signals at all: data-plane failure upstream. Fail-CLOSED:
             never auto-trades on broken data; the warning is the product
             (watchdog/operator escalation with diagnosis fields).
"""
from __future__ import annotations


def evaluate(moves: dict, trades_today: dict, signals_today: dict,
             threshold_pct: float = 10.0) -> list[dict]:
    """moves: {symbol: 24h change pct}. Returns verdicts for symbols whose
    |move| >= threshold, sorted by |move| descending:
      {"symbol", "move_pct", "direction", "cls"}
    cls in {"participating", "blocked", "silent"}.
    """
    out = []
    for sym, mv in moves.items():
        try:
            mv = float(mv)
        except (TypeError, ValueError):
            continue
        if abs(mv) < threshold_pct:
            continue
        direction = "long" if mv > 0 else "short"
        if int(trades_today.get(sym, 0)) > 0:
            cls = "participating"
        elif int(signals_today.get(sym, 0)) > 0:
            cls = "blocked"
        else:
            cls = "silent"
        out.append({"symbol": sym, "move_pct": round(mv, 2),
                    "direction": direction, "cls": cls})
    out.sort(key=lambda v: -abs(v["move_pct"]))
    return out
