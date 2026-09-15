#!/usr/bin/env python3
"""prop_shadow_sim — HyroTrader rulebook overlay on the ARIA journal.

Ladder rung-5 gate instrument (CEO-endorsed 2026-09-05, review 2026-09-19):
"a challenge the journal would fail is a donation." Zero fee risk — the sim
answers "would we be passing?" from evidence before any purchase decision.
Dayo decides; nothing here buys anything.

RULEBOOK built from the OFFICIAL HyroTrader docs, retrieved 2026-09-05:
  targets/days : https://www.hyrotrader.com/how-to-start/
      Two-Step: Phase 1 = 10% target + 5 trading days, Phase 2 = 5% target
      + 5 trading days; $5,000 account = $59; unlimited time.
  daily loss   : https://www.hyrotrader.com/faq/rules/how-is-the-5-daily-drawdown-calculated/
      Two-Step 5% ($250 on $5K), TRAILING from the day's peak equity,
      unrealized PnL + fees count, resets daily UTC.
  consistency  : https://www.hyrotrader.com/faq/rules/what-are-the-risk-management-conditions-at-hyrotrader/
      40% profit-distribution rule (no single day > 40% of total net
      profit), evaluation phases only.
  per-trade    : same FAQ — max 3% realized loss per trade of initial
      balance (manual review, not auto-monitored).
  max loss     : NOT officially renderable (checkout table is JS) —
      third-party consensus 10% static from initial. verified=False;
      VERIFY AT CHECKOUT before any purchase.

Doctrine notes:
  - ARIA per-day realized net PnL is scaled by 5000/book_usd (--book arg)
    because ARIA's sizing doctrine scales linearly with equity.
  - Daily trailing DD is approximated from the CLOSE sequence only — the
    journal has no intraday unrealized equity path. Real accounts are
    stricter; sim breaches are a LOWER bound. Reported as such.
  - Deterministic, stdlib-only, exit-0 always, atomic output. No restart.

Usage: python3 tools/prop_shadow_sim.py [--book 844] [--out logs/prop_shadow_sim.json]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(REPO_ROOT, "logs")
OUT_PATH = os.path.join(LOG_DIR, "prop_shadow_sim.json")
HIST_PATH = os.path.join(LOG_DIR, "prop_shadow_sim_history.jsonl")

RULEBOOK = {
    "firm": "HyroTrader", "retrieved": "2026-09-05",
    "account_usd": 5000.0, "price_usd": 59.0, "steps": 2,
    "phase1": {"profit_target_pct": 10.0, "min_trading_days": 5},
    "phase2": {"profit_target_pct": 5.0, "min_trading_days": 5},
    "daily_loss": {"pct": 5.0, "type": "trailing_from_day_peak",
                   "includes_unrealized": True, "reset": "UTC"},
    "max_loss": {"pct": 10.0, "type": "static_from_initial", "verified": False,
                 "note": "official Two-Step table JS-only; third-party consensus — VERIFY AT CHECKOUT"},
    "max_loss_per_trade_pct": 3.0,
    "consistency_pct": 40.0,
    "prohibited": ["martingale", "cross_account_hedging"],
    "sources": {
        "targets_days": "hyrotrader.com/how-to-start/ (2026-09-05)",
        "daily_loss": "hyrotrader.com/faq/rules/how-is-the-5-daily-drawdown-calculated/ (2026-09-05)",
        "consistency": "hyrotrader.com/faq/rules/what-are-the-risk-management-conditions-at-hyrotrader/ (2026-09-05)",
        "per_trade_loss": "same risk-conditions FAQ (2026-09-05)",
    },
}

MAX_PHASE_DAYS = 90   # sim cap; real challenge has no time limit


def pnl_net(r: dict) -> float:
    v = r.get("pnl_net_usd")
    if isinstance(v, (int, float)):
        return float(v)
    v = r.get("pnl_usd")
    if isinstance(v, (int, float)):
        return float(v)
    v = r.get("realized_pnl_usd")
    return float(v) if isinstance(v, (int, float)) else 0.0


def load_closes() -> list[dict]:
    """All journaled closes across day-files, deduped (entry_id, closed_at_ms),
    phantom-filtered via the journal's own classifier when importable."""
    try:
        sys.path.insert(0, REPO_ROOT)
        from intelligence.trade_journal import is_phantom_record
    except Exception:
        def is_phantom_record(_r):  # noqa: ANN001
            return False
    seen: set = set()
    out: list[dict] = []
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "trade_journal_*.json"))):
        try:
            recs = json.load(open(path))
        except Exception:
            continue
        for r in recs:
            if r.get("outcome") not in ("win", "loss") or not r.get("closed_at_ms"):
                continue
            key = (r.get("entry_id"), r.get("closed_at_ms"))
            if key in seen or is_phantom_record(r):
                continue
            seen.add(key)
            out.append(r)
    out.sort(key=lambda r: int(r["closed_at_ms"]))
    return out


def check_daily_loss_trailing(day_eq_path: list[float], limit_usd: float) -> int | None:
    """Trailing from the day's running peak (incl. unrealized per the FAQ).
    day_eq_path = equity after each close in the day. Returns breach index|None."""
    peak = day_eq_path[0] if day_eq_path else 0.0
    for i, eq in enumerate(day_eq_path):
        peak = max(peak, eq)
        if peak - eq >= limit_usd - 1e-9:
            return i
    return None


def check_max_loss_static(equity: float, floor: float) -> bool:
    return equity <= floor + 1e-9


def check_per_trade_loss(pnl: float, limit_usd: float) -> bool:
    return pnl <= -limit_usd + 1e-9


def consistency_ok(day_pnls: list[float], pct: float) -> bool:
    total = sum(p for p in day_pnls if p > 0)
    if total <= 0:
        return False
    return all(p / total <= pct / 100.0 + 1e-9 for p in day_pnls if p > 0)


def phase_complete(profit: float, target: float, n_days: int,
                   min_days: int, day_pnls: list[float], cons_pct: float) -> bool:
    return (profit >= target - 1e-9 and n_days >= min_days
            and consistency_ok(day_pnls, cons_pct))


def simulate_phase(day_pnls: list[float], day_eq_paths: list[list[float]],
                   start_equity: float, target_usd: float, dd_limit_usd: float,
                   loss_floor: float, min_days: int, cons_pct: float,
                   per_trade_limit: float, closes_by_day: list[list[float]]) -> dict:
    """One phase over the day sequence. Returns result/killing_rule/days/equity."""
    equity = start_equity
    acc_pnls: list[float] = []
    n_days = 0
    for di, pnl in enumerate(day_pnls[:MAX_PHASE_DAYS]):
        if closes_by_day[di]:  # trading day = a day with >= 1 close
            n_days += 1
            for cp in closes_by_day[di]:
                if check_per_trade_loss(cp, per_trade_limit):
                    return {"result": "fail", "killing_rule": "per_trade_loss_3pct",
                            "days": n_days, "end_equity": round(equity, 2)}
        path = [equity]
        for cp in closes_by_day[di]:
            equity += cp
            path.append(equity)
        if not closes_by_day[di]:
            equity += pnl
            path.append(equity)
        acc_pnls.append(pnl)
        if check_daily_loss_trailing(path, dd_limit_usd) is not None:
            return {"result": "fail", "killing_rule": "daily_loss_5pct_trailing",
                    "days": n_days, "end_equity": round(equity, 2)}
        if check_max_loss_static(equity, loss_floor):
            return {"result": "fail", "killing_rule": "max_loss_10pct_static",
                    "days": n_days, "end_equity": round(equity, 2)}
        profit = equity - start_equity
        if phase_complete(profit, target_usd, n_days, min_days, acc_pnls, cons_pct):
            return {"result": "pass", "killing_rule": None,
                    "days": n_days, "end_equity": round(equity, 2)}
    return {"result": "timeout", "killing_rule": "no_pass_within_90d",
            "days": n_days, "end_equity": round(equity, 2)}


def day_sequence(closes: list[dict], scale: float) -> tuple[list, list, list, list[str]]:
    """Per-UTC-day scaled pnl, equity paths' close pnls, and day labels."""
    days: dict[str, list[float]] = {}
    order: list[str] = []
    for r in closes:
        d = datetime.fromtimestamp(int(r["closed_at_ms"]) / 1000,
                                   tz=timezone.utc).strftime("%Y-%m-%d")
        if d not in days:
            days[d] = []
            order.append(d)
        days[d].append(pnl_net(r) * scale)
    day_pnls = [sum(days[d]) for d in order]
    closes_by_day = [days[d] for d in order]
    eq_paths = []
    eq = RULEBOOK["account_usd"]
    for d in order:
        path = []
        cur = eq
        for cp in days[d]:
            cur += cp
            path.append(cur)
        eq_paths.append(path)
        eq = cur
    return day_pnls, closes_by_day, eq_paths, order


def run_sim(day_pnls, closes_by_day, start_i: int) -> dict:
    acct = RULEBOOK["account_usd"]
    dd_limit = acct * RULEBOOK["daily_loss"]["pct"] / 100.0
    floor = acct * (1 - RULEBOOK["max_loss"]["pct"] / 100.0)
    ptl = acct * RULEBOOK["max_loss_per_trade_pct"] / 100.0
    cons = RULEBOOK["consistency_pct"]
    dp = day_pnls[start_i:]
    cd = closes_by_day[start_i:]
    p1 = simulate_phase(dp, [None] * len(dp), acct,
                        acct * RULEBOOK["phase1"]["profit_target_pct"] / 100.0,
                        dd_limit, floor, RULEBOOK["phase1"]["min_trading_days"],
                        cons, ptl, cd)
    out = {"phase1": p1}
    if p1["result"] == "pass":
        used = p1["days"]
        p2 = simulate_phase(dp[used:], [None] * len(dp[used:]), p1["end_equity"],
                            acct * RULEBOOK["phase2"]["profit_target_pct"] / 100.0,
                            dd_limit, floor, RULEBOOK["phase2"]["min_trading_days"],
                            cons, ptl, cd[used:])
        out["phase2"] = p2
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", type=float, default=844.0)
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()
    try:
        closes = load_closes()
        scale = RULEBOOK["account_usd"] / args.book if args.book > 0 else 1.0
        day_pnls, closes_by_day, _, order = day_sequence(closes, scale)
        per_start = []
        for i, d in enumerate(order):
            r = run_sim(day_pnls, closes_by_day, i)
            per_start.append({"start_day": d, **r})
        months: dict[str, list[dict]] = {}
        for row in per_start:
            months.setdefault(row["start_day"][:7], []).append(row)
        per_month = []
        for m, rows in sorted(months.items()):
            p1p = sum(1 for r in rows if r["phase1"]["result"] == "pass")
            p2p = sum(1 for r in rows if r.get("phase2", {}).get("result") == "pass")
            per_month.append({"month": m, "n_starts": len(rows),
                              "phase1_pass_rate": round(p1p / len(rows), 3),
                              "phase2_pass_rate": round(p2p / len(rows), 3)})
        killers: dict[str, int] = {}
        for r in per_start:
            k = r["phase1"].get("killing_rule")
            if k and r["phase1"]["result"] == "fail":
                killers[k] = killers.get(k, 0) + 1
        n = len(per_start)
        result = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "rulebook": RULEBOOK,
            "book_usd": args.book, "scale_factor": round(scale, 4),
            "n_closes": len(closes), "n_journal_days": n,
            "note": ("Daily trailing DD estimated from CLOSE sequence only "
                     "(no intraday unrealized path in the journal) — breaches "
                     "are a lower bound; the real account is stricter. "
                     "max_loss rule unverified against official docs — verify "
                     "at checkout before any purchase. PnL scaled "
                     "5000/book; linear-sizing doctrine."),
            "per_month": per_month,
            "per_start_day": per_start,
            "summary": {
                "phase1_pass_rate": round(sum(1 for r in per_start if r["phase1"]["result"] == "pass") / n, 3) if n else None,
                "phase2_pass_rate": round(sum(1 for r in per_start if r.get("phase2", {}).get("result") == "pass") / n, 3) if n else None,
                "most_common_killer": max(killers, key=killers.get) if killers else None,
                "killer_census": killers,
            },
        }
    except Exception as e:  # exit-0 doctrine
        result = {"error": str(e)[:200],
                  "generated": datetime.now(timezone.utc).isoformat()}
    tmp = args.out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(result, f, indent=1)
    os.replace(tmp, args.out)
    try:
        with open(HIST_PATH, "a") as f:
            f.write(json.dumps({"generated": result.get("generated"),
                                "summary": result.get("summary")}) + "\n")
    except Exception:
        pass
    print(json.dumps(result.get("summary") or result))


if __name__ == "__main__":
    main()
