"""
intelligence/pair_spread.py — pair_meanrev shadow gate (pair pipeline step 2,
queue #66). SHADOW-ONLY: no live orders, no trade-path wiring. The module
scores screened cointegrated pairs counterfactually from birth so the gate
graduates (or dies) on evidence, not narrative — same doctrine as
whale_absorption: ZERO live orders until n≥50 AND EV>+0.15R AND CI>0.

Brain: zero-I/O, pure — callers inject pair rows (from logs/pair_screen.json),
prices, ages, and plane-open flags. Owns the shadow ledger shape
(logs/pair_shadow.jsonl, append-only, one-bad-line read).

Spread definition (matches tools/pair_screen.py exactly):
    spread = log(P_a) - intercept - hedge * log(P_b)
    z      = spread / spread_std          (OLS residuals are zero-mean)

Entry:  z >= +Z_ENTRY → short_spread (short A, long B)
        z <= -Z_ENTRY → long_spread  (long A, short B)
Exits:  mean_reverted     |z| <= Z_EXIT
        z_stop            adverse excursion to |z| >= Z_STOP
        time_stop         age > TIME_STOP_MULT x half-life
        kill_cointegration pair row status == "dead" (screen kill rule)
Guards: status must be "candidate"; plane_open False → abstain (tradfi
        off-hours — plane integrity, never price a pair on a dark leg);
        slot budget caps concurrent shadow positions; one open per pair.

Cost model (documented approximation): round-trip taker on both legs
≈ cost_bps x (1 + |hedge|) in log-spread terms — you trade one unit of A
against |hedge| units of B, both sides paying the spread+fee both ways.

Canon: Chan (cross-sectional mean reversion, trade the residual), Thorp
(only bet with edge — the edge is measured here, not asserted), Aronson
(bound every degree of freedom: slots, kill rules, fixed thresholds),
López de Prado (cointegration breakdown is a regime change — exit, don't
average), Carver (fixed cost model in bps, never free-parameter slippage).
"""
from __future__ import annotations

import json
import math
import os
import time
from typing import Dict, List, Optional

Z_ENTRY = 2.0
Z_EXIT = 0.5
Z_STOP = 3.5
TIME_STOP_MULT = 2.0        # x half-life
MAX_OPEN = 3
COST_BPS_RT = 16.0          # round-trip taker, both legs combined baseline


def spread_now(price_a: float, price_b: float, intercept: float,
               hedge: float) -> Optional[float]:
    """log-spread of the pair at current prices. None on degenerate input."""
    try:
        if price_a <= 0 or price_b <= 0:
            return None
        return math.log(price_a) - intercept - hedge * math.log(price_b)
    except (TypeError, ValueError):
        return None


def z_live(spread: float, spread_std: float) -> Optional[float]:
    """z of the current spread against the screen-window residual std."""
    if spread_std is None or spread_std <= 0:
        return None
    return spread / spread_std


def entry_verdict(z: Optional[float], status: str, plane_open: bool,
                  z_entry: float = Z_ENTRY) -> str:
    """"long_spread" | "short_spread" | "none" — fail-open abstain."""
    if not plane_open or status != "candidate":
        return "none"
    if z is None:
        return "none"
    if z >= z_entry:
        return "short_spread"
    if z <= -z_entry:
        return "long_spread"
    return "none"


def exit_verdict(direction: str, z: Optional[float], age_hours: float,
                 half_life_days: float, status: str,
                 z_exit: float = Z_EXIT, z_stop: float = Z_STOP,
                 time_stop_mult: float = TIME_STOP_MULT) -> str:
    """"mean_reverted" | "z_stop" | "time_stop" | "kill_cointegration" | "hold".

    Cointegration kill outranks everything (López de Prado: the trade's
    premise is gone — P&L is noise from that moment). z_stop binds only on
    ADVERSE excursion: short_spread entered at z>=+entry dies at z>=+stop.
    """
    if status == "dead":
        return "kill_cointegration"
    if age_hours > time_stop_mult * half_life_days * 24.0:
        return "time_stop"
    if z is None:
        return "hold"
    if abs(z) <= z_exit:
        return "mean_reverted"
    if direction == "short_spread" and z >= z_stop:
        return "z_stop"
    if direction == "long_spread" and z <= -z_stop:
        return "z_stop"
    return "hold"


def spread_pnl_pts(direction: str, entry_spread: float,
                   exit_spread: float) -> float:
    """Log-spread points earned. short_spread profits when spread falls."""
    if direction == "short_spread":
        return entry_spread - exit_spread
    return exit_spread - entry_spread


def cost_pts(hedge: float, cost_bps: float = COST_BPS_RT) -> float:
    """Round-trip cost in log-spread points (both legs, both ways)."""
    return (cost_bps / 1e4) * (1.0 + abs(hedge))


def slot_available(open_positions: List[dict], max_open: int = MAX_OPEN) -> bool:
    return len(open_positions) < max_open


def pair_already_open(open_positions: List[dict], sym_a: str, sym_b: str) -> bool:
    return any(p.get("sym_a") == sym_a and p.get("sym_b") == sym_b
               for p in open_positions)


# ── Shadow ledger (append-only JSONL, one-bad-line doctrine) ─────────────────

def open_record(pair: dict, direction: str, entry_spread: float, z: float,
                price_a: float, price_b: float, now: Optional[float] = None) -> dict:
    _now = time.time() if now is None else now
    return {
        "id": f"{pair['sym_a']}_{pair['sym_b']}_{int(_now * 1000)}",
        "event": "pair_shadow_open", "ts": _now,
        "sym_a": pair["sym_a"], "sym_b": pair["sym_b"],
        "plane": pair.get("plane", ""), "direction": direction,
        "hedge": pair["hedge_ratio"], "intercept": pair["intercept"],
        "spread_std": pair["spread_std"],
        "half_life_days": pair["half_life_days"],
        "entry_spread": round(entry_spread, 8), "entry_z": round(z, 3),
        "entry_px_a": price_a, "entry_px_b": price_b,
    }


def close_record(rec: dict, reason: str, exit_spread: float, z: float,
                 cost_bps: float = COST_BPS_RT,
                 now: Optional[float] = None) -> dict:
    _now = time.time() if now is None else now
    _pts = spread_pnl_pts(rec["direction"], rec["entry_spread"], exit_spread)
    _cost = cost_pts(rec["hedge"], cost_bps)
    return {
        "id": rec["id"], "event": "pair_shadow_close", "ts": _now,
        "sym_a": rec["sym_a"], "sym_b": rec["sym_b"],
        "direction": rec["direction"], "reason": reason,
        "exit_spread": round(exit_spread, 8), "exit_z": round(z, 3),
        "age_hours": round((_now - rec["ts"]) / 3600.0, 2),
        "pnl_pts": round(_pts, 6), "cost_pts": round(_cost, 6),
        "net_pts": round(_pts - _cost, 6),
        "expectancy_r": round((_pts - _cost) / max(_cost, 1e-9), 3),
    }


def append_ledger(path: str, row: dict) -> None:
    """Append one JSON line; tmp-free (single write, fsync) — the ledger is
    low-frequency (a few rows/day), atomic-rename buys nothing here."""
    with open(path, "a") as f:
        f.write(json.dumps(row) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_open_positions(path: str) -> List[dict]:
    """Open positions = opens minus closes, last-wins by id. One bad line
    kills one record, never the ledger."""
    opens: Dict[str, dict] = {}
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            for line in f:
                try:
                    row = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(row, dict):
                    continue
                _id, _ev = row.get("id"), row.get("event")
                if _ev == "pair_shadow_open" and _id:
                    opens[_id] = row
                elif _ev == "pair_shadow_close" and _id:
                    opens.pop(_id, None)
    except OSError:
        return []
    return list(opens.values())
