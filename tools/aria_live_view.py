#!/usr/bin/env python3
"""aria_live_view.py — read-only pretty stream for logs/aria.log.

Reads structlog JSON lines on stdin, prints compact human rows on stdout.
Observer-class: never touches the bot, never opens a file, stdlib only.

Usage (on the server, usually via tools/aria_live.sh from a Mac):
    tail -F -n 40 ~/ARIA/logs/aria.log | .venv/bin/python tools/aria_live_view.py
"""
import datetime
import json
import os
import sys

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
_G = "\033[32m" if USE_COLOR else ""   # green — fills/wins/approvals
_R = "\033[31m" if USE_COLOR else ""   # red — failures/losses
_Y = "\033[33m" if USE_COLOR else ""   # yellow — standdowns/clamps/rejects
_C = "\033[36m" if USE_COLOR else ""   # cyan — signal flow
_D = "\033[2m" if USE_COLOR else ""    # dim — heartbeats
_B = "\033[1m" if USE_COLOR else ""    # bold
_X = "\033[0m" if USE_COLOR else ""

# High-frequency telemetry that buries the narrative. Hidden entirely.
NOISE = {
    "param_store_ai_set", "regime_classified", "tier6_signal_a",
    "tier6_signal_b_recovery", "liq_phase_transition", "spread_signal_elevated",
    "cascade_orchestrator_phase", "cascade_silence_transition",
    "bybit_subscribe_ack", "ensure_subscribed_waiting", "mover_radar_silent",
    "funding_arb_opportunities", "symbol_specs_synced_from_poller",
    "calendar_caution_active", "immune_order_purged", "immune_purge_failed",
    "aster_connection_lost", "ssi_spot_disconnected", "sodex_feed_disconnected",
    "bybit_feed_disconnected", "sodex_watchlist_subscribed",
    "bybit_watchlist_subscribed", "sodex_core_subscribed",
    "bybit_core_subscribed", "historical_loaded", "sodex_historical_loaded",
    "candle_event_received", "exec_formulas_published",
    "rotation_boost_applied", "emerging_trend_tick",
}

# Fields worth showing, in priority order, when no custom formatter hits.
PRIORITY_FIELDS = (
    "symbol", "side", "direction", "venue", "reason", "notional_usd",
    "final_notional", "floor", "leverage", "target", "actual", "margin",
    "pnl", "exit_reason", "coherence", "coherence_score", "daily_count",
    "open_positions", "balance_delta", "error", "note", "size",
)


def _kv(d, keys=PRIORITY_FIELDS, max_len=140):
    parts = []
    for k in keys:
        v = d.get(k)
        if v is None or v == "" or v == []:
            continue
        if isinstance(v, float):
            v = round(v, 4)
        parts.append(f"{k}={v}")
    out = " ".join(parts)
    return out[:max_len]


def _row(ts, tag, color, body):
    return f"{_D}{ts}{_X} {color}{tag:<14}{_X} {body}"


def _short_ts(raw):
    try:
        return datetime.datetime.fromisoformat(
            raw.replace("Z", "+00:00")).strftime("%H:%M:%S")
    except Exception:
        return raw[11:19] if len(raw) >= 19 else raw


def render(line):
    """Return the printable row for one JSON log line, or None to skip."""
    try:
        d = json.loads(line)
    except Exception:
        return None
    ev = d.get("event", "")
    if not ev or ev in NOISE:
        return None
    ts = _short_ts(d.get("timestamp", ""))
    lvl = d.get("level", "info")
    sym = d.get("symbol", "")

    # ── Money events ────────────────────────────────────────────────────
    if ev == "position_closed":
        pnl = d.get("pnl", 0.0) or 0.0
        color = _G if pnl >= 0 else _R
        return _row(ts, "CLOSED", color,
                    f"{sym} {d.get('side','')} pnl={_B}{pnl:+.4f}{_X} "
                    f"{d.get('exit_reason', d.get('reason', ''))}")
    if ev in ("bracket_placed", "fast_cycle_entry_approved"):
        return _row(ts, "ENTRY", _G + _B,
                    f"{sym} {d.get('side', d.get('direction',''))} "
                    f"notional={d.get('notional_usd', d.get('notional', '?'))} "
                    f"lev={d.get('leverage', '?')} {_kv(d, ('reason','margin'))}")
    if ev in ("bracket_failed", "place_rejected", "order_failed"):
        return _row(ts, "FAILED", _R + _B, _kv(d))
    if ev.startswith("signal_rejected_"):
        return _row(ts, ev.replace("signal_rejected_", "REJ:"), _Y, _kv(d))
    if ev.endswith("_standdown") or ev.endswith("_blocked"):
        return _row(ts, "STANDDOWN", _Y, f"{ev} {_kv(d)}")

    # ── Flow events ─────────────────────────────────────────────────────
    if ev == "signal_ready":
        return _row(ts, "SIGNAL", _C,
                    _kv(d, ("symbol", "direction", "side", "coherence",
                            "coherence_score", "session_type")))
    if ev == "sizing_chain":
        return _row(ts, "SIZING", _C,
                    _kv(d, ("symbol", "notional_usd", "final_notional",
                            "leverage", "dd_mult_effective", "dd_mult_sleeve")))
    if ev in ("leverage_set", "leverage_fallback_applied", "leverage_set_failed"):
        color = _R if ev.endswith("failed") else _C
        return _row(ts, "LEVERAGE", color, _kv(d))
    if ev == "venue_equity_clamped":
        return _row(ts, "CLAMPED", _Y, _kv(d))
    if ev.startswith("campaign_"):
        return _row(ts, "CAMPAIGN", _C, f"{ev.replace('campaign_','')} {_kv(d)}")
    if ev.startswith(("treasury_", "hedge_", "router_", "pair_engine_",
                      "pyramid_", "latefill_", "anticipator_")):
        return _row(ts, ev.split("_")[0].upper(), _C,
                    f"{ev} {_kv(d)}")

    # ── State / risk events ─────────────────────────────────────────────
    if ev == "pnl_attribution":
        return _row(ts, "BOOK", _D,
                    f"open={d.get('open_positions', '?')} "
                    f"delta={d.get('balance_delta', 0):+.3f} "
                    f"{d.get('breakdown', '')}")
    if ev == "treasury_heartbeat":
        return _row(ts, "TREASURY", _D, _kv(d, ("book_roe", "managed", "note")))
    if ev.startswith(("drawdown", "recovery_", "size_multiplier")):
        return _row(ts, "DD", _R if "recovery_mode_applied" in ev else _Y, _kv(d))
    if ev in ("startup_sync_complete", "performance_restored",
              "aster_session_start_equity", "fast_cycle_counters_restored",
              "agent_winrates_loaded"):
        return _row(ts, "BOOT", _B, f"{ev} {_kv(d)}")

    # ── Anything warning+ always shows; unknown info events hide ────────
    if lvl in ("warning", "error", "critical"):
        color = _R if lvl in ("error", "critical") else _Y
        return _row(ts, ev.upper()[:14], color, _kv(d))
    return None


def main():
    for line in sys.stdin:
        row = render(line)
        if row:
            print(row, flush=True)


if __name__ == "__main__":
    main()
