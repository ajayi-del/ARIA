#!/usr/bin/env python3
"""D19 unattributed-balance-delta census (CEO commission 2026-09-06, P0).

The firm could not tell a withdrawal from a loss: pnl_attribution.realized_est
is a residual (bal_delta - delta_unrealized) that jumps with ZERO closes and
ZERO classified external flows. This census:

  1. extracts every pnl_attribution row with |realized_est| >= $1.00 and no
     position_closed within +/-75s -> UNATTRIBUTED STEPS
  2. per day, reconciles: journal closes (day-files, deduped by
     (entry_id, closed_at_ms), phantom-filtered, dust rows stripped)
     + classified external flows (balance_adjustment_applied)
     vs equity delta (memory.performance drawdown_update current series)
  3. ORACLE (D19): journal + external must reconcile to equity delta within
     $2/day. Prints the one number: how much of the 7d equity decline is
     unattributed.

Observer-class. Reads logs only. Exit 0 always.
"""
import glob
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

ARIA = os.path.expanduser("~/ARIA")
LOG = f"{ARIA}/logs/aria.log"
DAYS = int(os.environ.get("CENSUS_DAYS", "7"))
MIN_STEP = 1.00
ORACLE_TOL = 2.00
DUST_NOTIONAL = 10.0  # sub-min-notional dust rows stripped pre-reconciliation (D16)


def _grep(event):
    out = subprocess.run(
        ["grep", f'"event": "{event}"', LOG], capture_output=True, text=True
    ).stdout
    rows = []
    for line in out.splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue  # one-bad-line doctrine
    return rows


def main():
    since = datetime.now(timezone.utc) - timedelta(days=DAYS)

    attrib = [r for r in _grep("pnl_attribution")
              if r.get("timestamp", "") >= since.strftime("%Y-%m-%dT%H:%M:%S")]
    closes_log = [r for r in _grep("position_closed")
                  if r.get("timestamp", "") >= since.strftime("%Y-%m-%dT%H:%M:%S")]
    flows = [r for r in _grep("balance_adjustment_applied")
             if r.get("timestamp", "") >= since.strftime("%Y-%m-%dT%H:%M:%S")]
    dd_updates = [r for r in _grep("drawdown_update")
                  if r.get("logger") == "memory.performance"
                  and r.get("timestamp", "") >= since.strftime("%Y-%m-%dT%H:%M:%S")]

    close_ts = sorted(r["timestamp"] for r in closes_log)

    def _close_near(ts, window_s=75):
        # binary search by string compare works: ISO timestamps sort
        import bisect
        i = bisect.bisect_left(close_ts, ts)
        for j in (i - 1, i):
            if 0 <= j < len(close_ts):
                try:
                    dt = abs((datetime.fromisoformat(close_ts[j].replace("Z", "+00:00"))
                              - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds())
                    if dt <= window_s:
                        return True
                except Exception:
                    pass
        return False

    # 1. unattributed steps
    steps = []
    prev_unreal = None
    for r in attrib:
        re_est = r.get("realized_est", 0.0) or 0.0
        if abs(re_est) >= MIN_STEP and not _close_near(r["timestamp"]):
            steps.append({
                "ts": r["timestamp"],
                "realized_est": round(re_est, 4),
                "bal_delta": r.get("balance_delta"),
                "unrealized_total": r.get("unrealized_total"),
                "open_positions": r.get("open_positions"),
                "day": r["timestamp"][:10],
            })

    steps_by_day = defaultdict(float)
    for s in steps:
        steps_by_day[s["day"]] += s["realized_est"]

    # 2. journal day-files, deduped + phantom-filtered + dust-stripped
    sys.path.insert(0, ARIA)
    try:
        from memory.trade_journal import is_phantom_record
    except Exception:
        def is_phantom_record(_):
            return False
    journal_by_day = defaultdict(float)
    journal_dust = defaultdict(float)
    seen = set()
    for f in sorted(glob.glob(f"{ARIA}/logs/trade_journal_*.json")):
        day = f[-15:-5] if f.endswith(".json") else ""
        if not re.match(r"\d{4}-\d{2}-\d{2}", day):
            continue
        if day < since.strftime("%Y-%m-%d"):
            continue
        try:
            entries = json.load(open(f))
        except Exception:
            continue
        if isinstance(entries, dict):
            entries = entries.get("entries", [])
        for e in entries:
            if e.get("outcome") not in ("win", "loss", "breakeven"):
                continue
            key = (e.get("entry_id"), e.get("closed_at_ms"))
            if key in seen:
                continue
            seen.add(key)
            if is_phantom_record(e):
                continue
            pnl = float(e.get("pnl_usd", e.get("pnl", 0.0)) or 0.0)
            notional = float(e.get("notional_usd", e.get("size_usd", 999.0)) or 999.0)
            if notional < DUST_NOTIONAL:
                journal_dust[day] += pnl  # D16: dust-purge wins stripped pre-reconciliation
                continue
            journal_by_day[day] += pnl

    # 3. classified external flows + equity series by day
    flows_by_day = defaultdict(float)
    for r in flows:
        flows_by_day[r["timestamp"][:10]] += float(r.get("adjustment", 0.0))

    equity_eod = {}
    for r in dd_updates:
        equity_eod[r["timestamp"][:10]] = r.get("current")  # last write wins = EOD

    # 4. reconciliation per day
    days = sorted(set(list(journal_by_day) + list(flows_by_day)
                      + list(steps_by_day) + list(equity_eod)))
    recon = {}
    total_unattrib = 0.0
    prev_eq = None
    for d in days:
        eq_delta = None
        if d in equity_eod:
            if prev_eq is not None:
                eq_delta = equity_eod[d] - prev_eq
            prev_eq = equity_eod[d]
        j = journal_by_day.get(d, 0.0)
        f = flows_by_day.get(d, 0.0)
        u = steps_by_day.get(d, 0.0)
        total_unattrib += u
        residual = None
        oracle = "no-equity-series"
        if eq_delta is not None:
            residual = eq_delta - j - f
            oracle = "PASS" if abs(residual) <= ORACLE_TOL else "FAIL"
        recon[d] = {
            "equity_delta": None if eq_delta is None else round(eq_delta, 2),
            "journal_real": round(j, 2),
            "journal_dust_stripped": round(journal_dust.get(d, 0.0), 2),
            "external_classified": round(f, 2),
            "unattributed_steps": round(u, 2),
            "residual": None if residual is None else round(residual, 2),
            "oracle": oracle,
        }

    out = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "window_days": DAYS,
        "min_step_usd": MIN_STEP,
        "total_unattributed_usd": round(total_unattrib, 2),
        "n_steps": len(steps),
        "steps": steps[-40:],
        "daily": recon,
    }
    tmp = f"{ARIA}/logs/unattributed_delta_census.json.tmp"
    with open(tmp, "w") as fh:
        json.dump(out, fh, indent=1)
    os.replace(tmp, f"{ARIA}/logs/unattributed_delta_census.json")

    print(f"D19 census {DAYS}d: total unattributed = ${total_unattrib:.2f} "
          f"across {len(steps)} steps")
    for d in days:
        r = recon[d]
        print(f"  {d}: eq={r['equity_delta']} journal={r['journal_real']} "
              f"dust={r['journal_dust_stripped']} ext={r['external_classified']} "
              f"unattrib={r['unattributed_steps']} residual={r['residual']} {r['oracle']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"census error: {e}")  # best-effort doctrine
    sys.exit(0)
