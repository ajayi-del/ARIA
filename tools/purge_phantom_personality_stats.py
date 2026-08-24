#!/usr/bin/env python3
"""
tools/purge_phantom_personality_stats.py — one-time phantom purge for agent_winrates.json.

The 2026-08-21/22 SPCX scale-mismatch ghosts (4 closes, |pnl| $635–$800 on a
~$760 book) booked into AFTERMATH's live-tracked record. Journals are
permanent (rule #14) — this script subtracts exactly those records'
contribution from the DERIVED store logs/agent_winrates.json, with backup.

memory/performance.py rebuilds its own personality stats from the journals at
boot and already filters these records via is_phantom_record — after this
purge both stores agree.

Usage: python3 tools/purge_phantom_personality_stats.py [--dry-run]
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

WINRATES_PATH = REPO_ROOT / "logs" / "agent_winrates.json"
BACKUP_SUFFIX = ".bak-phantom-purge-20260824"


def collect_phantoms(log_dir: Path) -> dict:
    """Sum phantom contributions per personality from all journal files."""
    from memory.performance import is_phantom_record

    seen: set = set()
    phantoms: dict = {}
    for fpath in sorted(log_dir.glob("trade_journal_*.json")):
        try:
            records = json.loads(fpath.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for entry in records:
            if entry.get("outcome") not in ("win", "loss"):
                continue
            key = (entry.get("entry_id"), entry.get("closed_at_ms"))
            if key in seen:
                continue
            seen.add(key)
            if not is_phantom_record(entry):
                continue
            p = (entry.get("personality") or "SCOUT").upper()
            agg = phantoms.setdefault(p, {"wins": 0, "losses": 0, "pnl": 0.0, "n": 0})
            if entry["outcome"] == "win":
                agg["wins"] += 1
            else:
                agg["losses"] += 1
            agg["pnl"] += entry.get("pnl_usd") or 0.0
            agg["n"] += 1
    return phantoms


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    phantoms = collect_phantoms(REPO_ROOT / "logs")
    if not phantoms:
        print("no phantom records found — nothing to purge")
        return 0

    data = json.loads(WINRATES_PATH.read_text())
    print(f"phantoms found: {sum(a['n'] for a in phantoms.values())}")
    for agent, agg in phantoms.items():
        rec = data.get(agent)
        if rec is None:
            print(f"  {agent}: WARNING not present in agent_winrates.json, skipped")
            continue
        before = dict(rec)
        rec["wins"] = rec.get("wins", 0) - agg["wins"]
        rec["losses"] = rec.get("losses", 0) - agg["losses"]
        rec["total_pnl"] = round(rec.get("total_pnl", 0.0) - agg["pnl"], 4)
        if rec["wins"] < 0 or rec["losses"] < 0:
            print(f"  {agent}: ERROR subtraction would go negative (already purged?) — aborted")
            return 1
        print(f"  {agent}: {before['wins']}W/{before['losses']}L ${before['total_pnl']:.2f}"
              f" -> {rec['wins']}W/{rec['losses']}L ${rec['total_pnl']:.2f}"
              f"  (removed {agg['wins']}W/{agg['losses']}L ${agg['pnl']:.2f})")

    if dry_run:
        print("dry-run — no files written")
        return 0

    backup = WINRATES_PATH.with_suffix(WINRATES_PATH.suffix + BACKUP_SUFFIX)
    backup.write_text(WINRATES_PATH.read_text())
    WINRATES_PATH.write_text(json.dumps(data, indent=2))
    print(f"written {WINRATES_PATH} (backup: {backup.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
