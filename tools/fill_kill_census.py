"""D13 fill-kill census — every stage between shortlist and fill, counted.

CEO s27 queue #12. One pass over aria.log for a UTC day: funnel stage counts
overall + per-symbol, so "why no X trades" is a number, not a theory. Stage
map is a static event-name classification; per-candidate joins don't exist
(no shared id across stages), so this is a CENSUS of stage flows — a kill
that eats 90% of a symbol's flow is visible without the join.

    .venv/bin/python tools/fill_kill_census.py [--date YYYY-MM-DD]

Reads:  logs/aria.log
Writes: logs/fill_kill_census.json (atomic). Exit 0 always.
Declaration (#48): window on event timestamp, never filename glob.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
ARIA_LOG = os.path.join(LOG_DIR, "aria.log")
OUT_PATH = os.path.join(LOG_DIR, "fill_kill_census.json")

STAGES = {
    "shortlist": {"signal_ready", "symbol_ready"},
    "sized": {"sizing_chain", "risk_parity_resized", "notional_floor_resized"},
    "decision": {"execution_decision"},
    "order": {"order_submitted", "aster_maker_entry_unfilled",
              "aster_maker_entry_unfilled_taker_fallback",
              "aster_max_notional_clamped"},
    "fill": {"order_filled", "bracket_placed", "position_opened",
             "position_fill_confirmed", "fastpath_entry_journaled",
             "bracket_failed"},
}
KILL_EVENTS = {
    "quant_filter_blocked", "signal_throttled", "signal_stale_data",
    "coherence_tier_reject", "session_coherence_floor", "signal_rejected_c_tier",
    "signal_rejected_dispersion_gate", "signal_rejected_regime_alignment",
    "signal_rejected_etf_tide", "signal_rejected_base_rate",
    "signal_rejected_rotation_filter", "signal_rejected_counter_trend",
    "equity_off_hours_blocked", "entry_blocked_mark_scale",
    "balance_floor_halt", "daily_trade_cap_reached",
    "nietzsche_min_notional_fail", "flip_blocked", "risk_reward_reject",
    "signal_rejected_calendar_block", "build_candidate_turnover_reject",
    "build_candidate_turnover_underlying_bypass",
    "signal_rejected_dust_notional", "cascade_aftermath_low_coherence_skip",
    "rally_graduation_blocked_counter_trend", "mark_entry_scale_mismatch",
    "campaign_balance_precondition_failed", "build_candidate_balance_too_low",
    "aftermath_entry_blocked_timing", "circuit_breaker_opened",
}
STAGE_OF = {ev: stage for stage, evs in STAGES.items() for ev in evs}
TRADFI_HINTS = ("SPCX", "AAPL", "TSLA", "NVDA", "SPY", "QQQ", "MSTR", "HOOD",
                "COIN", "META", "AMZN", "MSFT", "GOOGL", "AMD", "PLTR", "NFLX")


def _is_tradfi(symbol: str) -> bool:
    return any(symbol.startswith(s) for s in TRADFI_HINTS)


def run(day: str) -> dict:
    stage_counts = Counter()
    kill_counts = Counter()
    by_symbol = {}
    n_lines = 0
    try:
        with open(ARIA_LOG, errors="ignore") as f:
            for line in f:
                if f'"2026' not in line[:60] and '"timestamp"' not in line:
                    continue
                i = line.find("{")
                if i < 0:
                    continue
                try:
                    d = json.loads(line[i:])
                except Exception:
                    continue
                ts = str(d.get("timestamp", ""))
                if not ts.startswith(day):
                    continue
                n_lines += 1
                ev = d.get("event", "")
                sym = d.get("symbol") or ""
                stage = STAGE_OF.get(ev)
                if stage:
                    stage_counts[stage] += 1
                    if ev == "execution_decision" and d.get("approved") is False:
                        kill_counts["execution_decision_rejected"] += 1
                elif ev in KILL_EVENTS:
                    kill_counts[ev] += 1
                    stage = "kill"
                else:
                    continue
                if sym:
                    s = by_symbol.setdefault(sym, Counter())
                    s[f"{stage}:{ev}"] += 1
    except Exception as e:
        return {"date": day, "error": str(e)[:200]}

    fills = stage_counts.get("fill", 0)
    funnel = {s: stage_counts.get(s, 0)
              for s in ("shortlist", "sized", "decision", "order", "fill")}
    funnel["fill_rate_pct_of_shortlist"] = (
        round(100.0 * fills / funnel["shortlist"], 2)
        if funnel["shortlist"] else None)

    tradfi = {s: dict(c) for s, c in by_symbol.items() if _is_tradfi(s)}
    top_kills = sorted(((_is_tradfi(s), s, sum(v2 for k2, v2 in c.items()
                                             if k2.startswith("kill:")), c)
                        for s, c in by_symbol.items()),
                       key=lambda x: -x[2])[:15]

    return {
        "date": day,
        "generated": datetime.now(timezone.utc).isoformat(),
        "declaration": {
            "window_field": "event timestamp prefix", "window": day,
            "unit": "event counts (no per-candidate join exists across stages)",
        },
        "funnel": funnel,
        "kills": dict(kill_counts.most_common()),
        "top_kill_symbols": [
            {"symbol": s, "tradfi": tf, "kill_events": n,
             "top": dict(c.most_common(5))}
            for tf, s, n, c in top_kills],
        "tradfi_symbols_seen": sorted(tradfi),
        "log_lines_in_window": n_lines,
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
    except Exception as e:
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
    f_ = result.get("funnel") or {}
    print(json.dumps({"date": day, "funnel": f_,
                      "error": result.get("error")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
