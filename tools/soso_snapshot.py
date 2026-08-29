#!/usr/bin/env python3
"""SoSoValue snapshot reader for the watchdog (2026-08-29).

ONE fetcher, MANY readers doctrine: ARIA's _sosovalue_loop owns the API
budget (≤7 calls/day + 2/day LLM reserve ≈ 2.7% of the 10k/mo demo plan).
This tool READS the cache ARIA writes (logs/sosovalue_flows.json +
logs/sosovalue_macro.json) and prints one JSON snapshot to stdout for the
watchdog cycle. It only spends API calls itself when the cache is genuinely
stale (>30h — e.g. the bot is down) AND SOSOVALUE_API_KEY is present in the
environment; then it performs a single top-up pass (same window discipline
does not apply — a dead bot is the exceptional case, fail-closed on error).

Usage: .venv/bin/python tools/soso_snapshot.py
Exit code always 0 (best-effort doctrine, same as daily_digest).
"""
import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_STALE_AFTER_H = 30.0


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def main() -> int:
    from data.sosovalue_feed import (SoSoValueFeed, flow_verdict, flow_poll,
                                     tide_aligned, macro_due_today)

    log_dir = os.path.join(_ROOT, "logs")
    rows = _load(os.path.join(log_dir, "sosovalue_flows.json"), {})
    macro = _load(os.path.join(log_dir, "sosovalue_macro.json"), [])
    now = time.time()

    # Cache age from the newest flow date across symbols.
    newest = ""
    for r in rows.values():
        if r and str(r[0].get("date") or "") > newest:
            newest = str(r[0]["date"])
    age_h = 999.0
    if newest:
        try:
            import calendar as _cal
            y, m, d = int(newest[:4]), int(newest[5:7]), int(newest[8:10])
            age_h = max(0.0, (now - _cal.timegm((y, m, d, 0, 0, 0, 0, 0, 0))) / 3600.0)
        except Exception:
            pass

    fetched = False
    if age_h > _STALE_AFTER_H:
        key = os.environ.get("SOSOVALUE_API_KEY", "")
        if key:
            try:
                import asyncio
                feed = SoSoValueFeed(key, log_dir=log_dir)
                fetched = asyncio.run(feed.fetch_due())
                if fetched:
                    rows = _load(os.path.join(log_dir, "sosovalue_flows.json"), {})
                    macro = _load(os.path.join(log_dir, "sosovalue_macro.json"), [])
            except Exception as e:
                print(json.dumps({"error": f"topup_failed:{str(e)[:120]}"}))
                return 0

    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    flows = {}
    for sym, r in rows.items():
        v = flow_verdict(sym, r or [])
        if v.get("rows"):
            flows[sym] = {**v,
                          "tide_long": tide_aligned(v, "long", age_hours=age_h),
                          "tide_short": tide_aligned(v, "short", age_hours=age_h)}
    out = {
        "ts": now,
        "cache_age_hours": round(age_h, 1),
        "fresh": age_h <= _STALE_AFTER_H,
        "topup_fetched": fetched,
        "flows": flows,
        "macro_today": macro_due_today(macro, today),
        "macro_calendar": macro[:8],
        "budget_note": ("ARIA owns the budget (<=7 calls/day + 2/day LLM "
                        "reserve); this tool spends only on stale-cache "
                        "top-up when the bot is down."),
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
