"""Daily EV digest — deterministic precompute for the watchdog's daily scan.

Runs standalone (works even when the bot is DOWN — diagnostics matter most
then). The watchdog's first cycle after 00:00 UTC runs this, reads
logs/daily_digest.json, and spends its turns on judgment, not arithmetic.

    .venv/bin/python tools/daily_digest.py [--date YYYY-MM-DD]

Reads:  newest logs/trade_journal_*.json (cumulative — filter by window),
        logs/aria.log (single pass, date-filtered, JSON-parsed only for
        position_closed lines), logs/gate_report.json, logs/shadow_scored.jsonl,
        logs/drawdown_state.json, logs/venue_comparison.json (weekly),
        public klines (Bybit / Aster / Yahoo) for entry-slippage + benchmark.
Writes: logs/daily_digest.json (full report) + one line appended to
        logs/daily_digest_history.jsonl (trend).

Best-effort doctrine: every section carries its own "error" field on failure;
one bad source never blanks the report. Exit code is always 0.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

# Run-as-script puts tools/ on sys.path, not the repo root — the lazy repo
# imports in venue_classifier() need the root (config, feeds, aster adapter).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
ARIA_LOG = os.path.join(LOG_DIR, "aria.log")
OUT_PATH = os.path.join(LOG_DIR, "daily_digest.json")
HISTORY_PATH = os.path.join(LOG_DIR, "daily_digest_history.jsonl")

BYBIT_KLINE = "https://api.bybit.com/v5/market/kline"
ASTER_KLINE = "https://fapi.asterdex.com/fapi/v1/klines"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}"

VETO_EVENTS = {
    "signal_stale_data", "insufficient_candles", "signal_rejected_c_tier",
    "signal_rejected_dispersion_gate", "coherence_tier_reject",
    "quant_filter_blocked", "recovery_mode_coherence_skip",
    "market_hours_gate_blocked", "signal_throttled",
}
PHANTOM_EVENTS = {
    "recovery_mode_coherence_skip", "recovery_mode_applied",
    "drawdown_size_reduced", "basket_harvest", "deposit_detection_vetoed",
    "deposit_anchors_adjusted", "campaign_loss_cooloff_armed",
    "direction_loss_block_armed",
}
MULT_FIELDS = ("allocation_mult", "coherence_mult", "calendar_mult",
               "calendar_size_mult", "freshness_mult", "size_multiplier")


# ── Pure analysis (unit-tested, no I/O) ──────────────────────────────────────

PHANTOM_DAYS = ("2026-08-21", "2026-08-22")


def is_phantom_record(r: dict) -> bool:
    """Delegates to memory.trade_journal.is_phantom_record (the shared
    any-date predicate, generalized 2026-08-29 after the bimodal census:
    561 real SPCX closes ALL under $5 vs 64 ghosts ALL over $100 — zero
    records between, so the threshold separates the clusters exactly on
    ANY date). Lazy import keeps the digest stdlib-only at module load
    (must run when the bot is down); the inline fallback is the same
    predicate so a broken venv can never silently unfilter.

    outcomes.db rows are NOT filtered here (different schema). The
    PHANTOM_DAYS constant stays for the history-tail recompute (history
    lines written on those days carry phantom-contaminated nets).
    """
    try:
        from memory.trade_journal import is_phantom_record as _shared
        return _shared(r)
    except Exception:
        if r.get("symbol") != "SPCX-USD":
            return False
        return abs(float(r.get("pnl_usd") or r.get("pnl_net_usd") or 0.0)) > 100.0


def pnl_net(r: dict) -> float:
    v = r.get("pnl_net_usd")
    if v is None:
        v = r.get("pnl_usd")
    return float(v or 0.0)


# ── #53 (CEO DIR 2026-09-11, widened): dust class + schedule-derived fees ──
# Dust = close whose notional is below the EXECUTION venue's minimum
# (issue #14: SoDEX $10 / Aster $1) — structurally unclosable remnants that
# net ~breakeven and pollute WR/expectancy (Sept: +$9.36 of fake "wins"
# to the cent in trade_db; +4.3pp phantom WR in the journal plane).
ASTER_MIN_NOTIONAL_USD = 1.0
SODEX_MIN_NOTIONAL_USD = 10.0

# Round-trip taker fee schedule by execution venue (fee x notional, never
# the pnl fields — DIR#53: directional_pnl - net_pnl is fee + exit-fill
# error, so published fee ratios derive from the schedule):
#   SoDEX taker 0.04%/side x 0.95 (SOSO_STAKED=168 5% discount) = 0.076% RT
#     (measured: 7.588bp median RT, s29 census n=26, p10 7.580/p90 7.622)
#   Aster crypto taker 0.04%/side = 0.080% RT (measured 7.647bp median;
#     maker-first entries model as taker — conservative high, matches median)
#   Aster stock/commodity perps taker 0.009%/side = 0.018% RT (docs fee table)
FEE_RT = {"sodex": 0.00076, "aster": 0.0008, "aster_tradfi": 0.00018}


def close_notional_usd(r: dict) -> float:
    """Entry-plane notional of the closed position (size class proxy)."""
    try:
        return abs(float(r.get("position_size") or 0.0)
                   * float(r.get("entry_price") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _exec_venue(r: dict, venue_of) -> str:
    """Execution venue for the min-notional/fee question: venue_of 'aster'
    routes to Aster ($1 min); everything else executed on SoDEX ($10 min).
    venue_of None (bot-down / tests) -> 'sodex' (the stricter threshold,
    fail-closed toward keeping records in the expectancy pool)."""
    if venue_of is None:
        return "sodex"
    try:
        v = venue_of(r.get("symbol", ""))
    except Exception:
        return "sodex"
    return "aster" if v == "aster" else "sodex"


def is_dust_close(r: dict, venue_of=None) -> bool:
    n = close_notional_usd(r)
    if n <= 0.0:
        return False  # missing fields: fail-open to legacy (kept in pool)
    floor = ASTER_MIN_NOTIONAL_USD if _exec_venue(r, venue_of) == "aster" \
        else SODEX_MIN_NOTIONAL_USD
    return n < floor


def dust_census(records: list[dict], venue_of=None) -> dict:
    """The distinct dust class: census reported, rows EXCLUDED from
    WR/expectancy at assembly (DIR#53: purge clears the position)."""
    dust = [r for r in records if r.get("outcome") in ("win", "loss")
            and is_dust_close(r, venue_of)]
    wins = sum(1 for r in dust if pnl_net(r) > 0)
    return {"n": len(dust), "fake_wins": wins,
            "net_pnl": round(sum(pnl_net(r) for r in dust), 4),
            "symbols": sorted({r.get("symbol", "?") for r in dust}),
            "excluded_from": ["expectancy", "wr"]}


def _tradfi_set() -> set:
    try:
        from data.tradfi_feed import TRADFI_SYMBOLS
        return set(TRADFI_SYMBOLS)
    except Exception:
        return set()


def modeled_fee_usd(r: dict, venue_of=None, tradfi: set | None = None) -> float:
    """Schedule x notional round-trip fee (never read from pnl fields)."""
    notional = close_notional_usd(r)
    if notional <= 0.0:
        return 0.0
    venue = _exec_venue(r, venue_of)
    if venue == "aster":
        if tradfi is None:
            tradfi = _tradfi_set()
        key = "aster_tradfi" if r.get("symbol", "") in tradfi else "aster"
    else:
        key = "sodex"
    return notional * FEE_RT[key]


def expectancy_by_symbol(records: list[dict]) -> dict:
    out = {}
    bysym = defaultdict(list)
    for r in records:
        bysym[r.get("symbol", "?")].append(r)
    for sym, rs in sorted(bysym.items()):
        closed = [r for r in rs if r.get("outcome") in ("win", "loss")]
        wins = [pnl_net(r) for r in closed if pnl_net(r) > 0]
        losses = [pnl_net(r) for r in closed if pnl_net(r) <= 0]
        n = len(closed)
        if n == 0:
            continue
        exp = sum(wins + losses) / n
        out[sym] = {
            "n": n,
            "abandoned": sum(1 for r in rs if r.get("outcome") == "abandoned"),
            "wr": round(len(wins) / n, 3),
            "avg_win": round(sum(wins) / len(wins), 4) if wins else 0.0,
            "avg_loss": round(sum(losses) / len(losses), 4) if losses else 0.0,
            "expectancy": round(exp, 4),
            "pnl_sum": round(sum(wins + losses), 3),
            "flag": "churn_leak" if (n >= 10 and exp < -0.02) else "",
        }
    return out


def size_chain(records: list[dict], balance: float,
               venue_of=None, venue_equity: dict | None = None) -> dict:
    """Where does size die? Mean of each multiplier field + median notional.
    The smallest mean mult names the chokepoint (e.g. size_multiplier 0.35
    avg = DD/recovery tax — the 2026-08-18 phantom pattern).

    Per-venue medians (2026-08-23): sizing doctrine is per-EXECUTING-venue
    equity (Vince — the venue's own capital), so the leak flag must be too.
    The combined-balance flag false-alarmed: median $65.5 vs 15% of $754
    combined while the Aster median is ~35% of the $188 Aster sleeve —
    healthy. venue_equity carries per-venue equity when the log offered it
    (aster_session_start_equity + combined minus aster for sodex)."""
    mults = {f: [] for f in MULT_FIELDS}
    notionals = []
    by_venue: dict[str, list[float]] = {}
    for r in records:
        if r.get("outcome") not in ("win", "loss"):
            continue
        for f in MULT_FIELDS:
            v = r.get(f)
            if isinstance(v, (int, float)) and v > 0:
                mults[f].append(float(v))
        sz, ep = r.get("position_size") or 0, r.get("entry_price") or 0
        if sz and ep:
            n = sz * ep
            notionals.append(n)
            if venue_of is not None:
                v = "aster" if venue_of(r.get("symbol", "")) == "aster" else "sodex"
                by_venue.setdefault(v, []).append(n)
    mean_mults = {f: round(sum(v) / len(v), 3) for f, v in mults.items() if v}
    notionals.sort()
    med_notional = round(notionals[len(notionals) // 2], 1) if notionals else 0.0
    choke = min(mean_mults, key=mean_mults.get) if mean_mults else ""
    flag = ""
    if balance > 400 and med_notional and med_notional < 0.15 * balance:
        flag = f"size_leak: median notional ${med_notional} < 15% of balance"
    out = {"mean_mults": mean_mults, "chokepoint": choke,
           "median_notional": med_notional, "flag": flag}
    if venue_of is not None:
        med_by_venue = {}
        venue_flags = []
        for v, xs in sorted(by_venue.items()):
            xs.sort()
            med = round(xs[len(xs) // 2], 1)
            med_by_venue[v] = med
            eq = float((venue_equity or {}).get(v, 0.0) or 0.0)
            if eq > 100 and med and med < 0.15 * eq:
                venue_flags.append(
                    f"size_leak[{v}]: median ${med} < 15% of {v} equity ${eq:.0f}")
        out["median_by_venue"] = med_by_venue
        if venue_equity:
            # Per-venue references replace the combined flag when available —
            # the combined flag mismeasures a two-sleeve book.
            out["flag"] = "; ".join(venue_flags)
            out["venue_equity"] = {k: round(v, 1) for k, v in venue_equity.items()}
    return out


def hold_asymmetry(records: list[dict]) -> dict:
    def med(xs):
        xs = sorted(xs)
        return round(xs[len(xs) // 2], 1) if xs else 0.0
    wins = [(r.get("hold_time_ms") or 0) / 60000 for r in records
            if r.get("outcome") == "win"]
    losses = [(r.get("hold_time_ms") or 0) / 60000 for r in records
              if r.get("outcome") == "loss"]
    out = {"median_win_min": med(wins), "median_loss_min": med(losses)}
    w, l = out["median_win_min"], out["median_loss_min"]
    out["flag"] = ("cut_winners_ride_losers" if w and l and l > 1.5 * w
                   else "")
    return out


def trend_capture(records: list[dict], day_pct, moves_4h: dict,
                  balance: float) -> dict:
    """Did ARIA capture the day's trend? Compares the majors' move (daily bar,
    or the biggest synchronized 4h thrust when the daily bar is ambiguous)
    against directional realized PnL. Trend is signed — a downtrend day is a
    trend day; the guard is direction-symmetric.

    Verdicts:
      quiet_day    — evidence present but below trend thresholds
      ok           — trend existed and trend-side realized PnL was positive
      MISSED_TREND — trend existed, trend-side PnL <= 0
                     (counter_traded=True when the opposed side also lost)
      unknown      — no market evidence (network section failed)
    """
    def _side(d) -> str:
        d = str(d or "").lower()
        if d.startswith(("l", "buy")):
            return "long"
        if d.startswith(("s", "sell")):
            return "short"
        return ""

    pnl_by_side = {"long": 0.0, "short": 0.0}
    n_by_side = {"long": 0, "short": 0}
    for r in records:
        if r.get("outcome") not in ("win", "loss"):
            continue
        s = _side(r.get("direction"))
        if not s:
            continue
        pnl_by_side[s] += pnl_net(r)
        n_by_side[s] += 1

    out = {"day_move_pct": day_pct, "max_4h_moves": moves_4h or {},
           "verdict": "unknown"}

    direction, mag = "", 0.0
    if day_pct is not None and abs(day_pct) >= 3.0:
        direction = "long" if day_pct > 0 else "short"
        mag = abs(day_pct)
    elif moves_4h:
        eq = sum(moves_4h.values()) / len(moves_4h)
        if abs(eq) >= 2.0:
            direction = "long" if eq > 0 else "short"
            mag = abs(eq)
    if not direction:
        if day_pct is not None or moves_4h:
            out["verdict"] = "quiet_day"
        return out

    opp = "short" if direction == "long" else "long"
    tp = round(pnl_by_side[direction], 3)
    cp = round(pnl_by_side[opp], 3)
    out.update({
        "trend_direction": direction,
        "trend_magnitude_pct": round(mag, 2),
        "trend_side_pnl_usd": tp,
        "counter_side_pnl_usd": cp,
        "trend_side_trades": n_by_side[direction],
        "counter_side_trades": n_by_side[opp],
    })
    if tp > 0:
        out["verdict"] = "ok"
    else:
        out["verdict"] = "MISSED_TREND"
        out["counter_traded"] = cp < 0
    return out


def fee_drag(records: list[dict], venue_of=None, tradfi: set | None = None) -> dict:
    gross = sum(float(r.get("pnl_usd") or 0.0) for r in records
                if r.get("outcome") in ("win", "loss"))
    net = sum(pnl_net(r) for r in records if r.get("outcome") in ("win", "loss"))
    drag = round(net - gross, 4)
    # Q1.5' (2026-09-05 CEO commission): the population + the composable form.
    # n = closed records measured; cost as % of notional per round trip is the
    # only form that composes with mover-class EV. NOTE: pnl_net_usd captures
    # fees; realized slippage is NOT in it (the slippage section stays a
    # separate dead wire).
    closed = [r for r in records if r.get("outcome") in ("win", "loss")]
    notionals = sorted((r.get("position_size") or 0) * (r.get("entry_price") or 0)
                       for r in closed)
    notional_total = sum(notionals)
    cost_usd = round(gross - net, 4)
    # #53 (CEO DIR 2026-09-11): the PUBLISHED fee ratio is schedule x notional.
    # The measured drag (gross - net) conflates fee with exit-fill error —
    # trade_db carries rows where net_pnl EXCEEDS directional_pnl (+$4.66 over
    # 28 non-dust rows), impossible for a fee-only reading. modeled_* is the
    # honest fee number; measured drag stays as a diagnostic tail indicator.
    modeled = round(sum(modeled_fee_usd(r, venue_of, tradfi) for r in closed), 4)
    return {"gross": round(gross, 3), "net": round(net, 3), "drag": drag,
            "drag_pct_of_gross": round(100 * drag / gross, 1) if gross else 0.0,
            "n": len(closed),
            "cost_usd": cost_usd,
            "notional_total_usd": round(notional_total, 1),
            "median_notional_usd": (round(notionals[len(notionals) // 2], 1)
                                    if notionals else 0.0),
            "cost_pct_notional_round_trip": (
                round(100 * cost_usd / notional_total, 4)
                if notional_total else 0.0),
            "modeled_fee_usd": modeled,
            "modeled_fee_pct_of_gross": (
                round(100 * modeled / abs(gross), 1) if gross else 0.0),
            "modeled_fee_pct_notional_rt": (
                round(100 * modeled / notional_total, 4)
                if notional_total else 0.0),
            "measured_drag_note": "fee + exit-fill error, not fee alone (#53)"}


def exit_pareto(closed_events: list[dict]) -> dict:
    """closed_events: parsed position_closed (__main__) log dicts."""
    agg = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for e in closed_events:
        reason = e.get("exit_reason") or "unknown"
        p = e.get("pnl")
        if isinstance(p, str):
            p = p.replace("$", "").replace("+", "")
        try:
            p = float(p or 0.0)
        except (TypeError, ValueError):
            p = 0.0
        agg[reason]["n"] += 1
        agg[reason]["pnl"] += p
    return {k: {"n": v["n"], "pnl": round(v["pnl"], 3)}
            for k, v in sorted(agg.items(), key=lambda kv: kv[1]["pnl"])}


def elite_override_census(events: list[dict], records: list[dict]) -> dict:
    """Census of elite overrides (coh≥8.5 firing through a 2-strike
    direction-loss lockout) and emerging-trend denials — each fired override
    joined to the next same-symbol/direction journal close so the watchdog
    reads one line: what did overriding the lockout earn or cost.
    (2026-09-04 directive: overrides after 2-strike lockouts, PnL scored.)"""
    fired = [e for e in events
             if e.get("event") == "direction_loss_block_elite_override"]
    denied = [e for e in events
              if e.get("event") == "direction_loss_block_elite_override_denied"]
    closes = sorted((r for r in records if r.get("outcome") in ("win", "loss")),
                    key=lambda r: r.get("timestamp_ms") or 0)
    rows, unscored = [], 0
    for e in fired:
        hit = next((r for r in closes
                    if r.get("symbol") == e.get("symbol")
                    and r.get("direction") == e.get("direction")
                    and (r.get("timestamp_ms") or 0)
                    >= (e.get("ts_ms") or 0) - 10000), None)
        if hit is None:
            unscored += 1
            continue
        rows.append({"symbol": e.get("symbol"), "direction": e.get("direction"),
                     "coherence": e.get("coherence"),
                     "exit_reason": hit.get("exit_reason"),
                     "pnl_usd": round(pnl_net(hit), 4)})
    return {"fired": len(fired), "denied": len(denied),
            "joined": len(rows), "unscored": unscored,
            "pnl_usd": round(sum(r["pnl_usd"] for r in rows), 4),
            "denied_symbols": sorted({e.get("symbol") for e in denied}),
            "rows": rows}


def operator_positions_section(events: list[dict]) -> dict:
    """Operator-trades observatory (2026-09-03 directive: manual trades run
    aside ARIA, observed — NEVER adopted/managed/journaled). Latest snapshot
    per symbol + event census; the digest is the surface that proves the
    firewall holds (observed, never in ARIA's own expectancy/fee sections)."""
    latest: dict[str, dict] = {}
    counts = Counter()
    for e in sorted(events, key=lambda e: e.get("ts_ms") or 0):
        counts[e.get("event")] += 1
        sym = e.get("symbol") or ""
        if not sym:
            continue
        if e.get("event") == "operator_position_closed":
            latest.pop(sym, None)
            continue
        latest[sym] = {"side": e.get("side"), "size": e.get("size"),
                       "entry": e.get("entry"), "upnl": e.get("upnl"),
                       "leverage": e.get("leverage"),
                       "last_seen_ms": e.get("ts_ms")}
    return {"events": dict(counts), "open_now": latest,
            "symbols_seen": sorted({e.get("symbol") for e in events
                                    if e.get("symbol")})}


STOP_CLOSE_MARKERS = ("stop", "trail", "ratchet")


def is_stop_close(r: dict) -> bool:
    """A loss whose exit was a stop-class trigger (software_stop, native stop,
    trailing, roe_ratchet) — the cohort the tight-stop regret study measures.
    Portfolio guards (portfolio_loss_cut) and time stops are NOT stop-class:
    they are exits of a different doctrine."""
    if r.get("outcome") != "loss":
        return False
    reason = str(r.get("exit_reason") or "").lower()
    if "time_stop" in reason or "portfolio" in reason:
        return False
    return any(m in reason for m in STOP_CLOSE_MARKERS)


def stop_regret_verdict(n: int, regret_rate_4h: float) -> str:
    """Aronson discipline: n<10 is a census, not a verdict. ≥40% of stopped
    losers recovering to breakeven within 4h = stops systematically tight;
    ≤15% = the stops are doing their job (the band between is regime noise)."""
    if n < 10:
        return "thin"
    if regret_rate_4h >= 0.40:
        return "stops_too_tight"
    if regret_rate_4h <= 0.15:
        return "stops_justified"
    return "mixed"


def silence_census(assets: list[str], signal_ready: Counter,
                   vetoes: Counter) -> list[dict]:
    out = []
    for sym in assets:
        if signal_ready.get(sym, 0) > 0:
            continue
        sym_vetoes = {ev: n for (s, ev), n in vetoes.items() if s == sym}
        if not sym_vetoes:
            out.append({"symbol": sym, "top_veto": "no_events_at_all",
                        "veto_count": 0})
            continue
        top = max(sym_vetoes, key=sym_vetoes.get)
        kind = "data" if top in ("signal_stale_data", "insufficient_candles") else "gate"
        out.append({"symbol": sym, "top_veto": top,
                    "veto_count": sym_vetoes[top], "kind": kind})
    return sorted(out, key=lambda d: -d["veto_count"])


def coherence_calibration(records: list[dict]) -> dict:
    buckets = {"5-6": [], "6-7": [], "7-8": [], "8+": []}
    for r in records:
        if r.get("outcome") not in ("win", "loss"):
            continue
        c = float(r.get("coherence_score") or 0.0)
        key = "8+" if c >= 8 else ("7-8" if c >= 7 else ("6-7" if c >= 6 else "5-6"))
        buckets[key].append(pnl_net(r))
    out = {}
    for k, v in buckets.items():
        if v:
            out[k] = {"n": len(v),
                      "wr": round(sum(1 for x in v if x > 0) / len(v), 3),
                      "expectancy": round(sum(v) / len(v), 4)}
    return out


def fundamental_law(records: list[dict]) -> dict:
    """Grinold & Kahn, *Active Portfolio Management* — the fundamental law:
    IR ≈ IC × √breadth. IC = Pearson corr(entry coherence, realized pnl_r);
    breadth = closed bets in the window (symbols traded again still count —
    each bet is a fresh forecast). n < 10 → not measured (Aronson)."""
    pairs = []
    for r in records:
        if r.get("outcome") not in ("win", "loss"):
            continue
        c = float(r.get("coherence_score") or 0.0)
        if c <= 0:
            continue
        p = r.get("pnl_r")
        try:
            p = float(p) if p is not None else pnl_net(r)
        except (TypeError, ValueError):
            p = pnl_net(r)
        pairs.append((c, p))
    n = len(pairs)
    if n < 10:
        return {"n": n, "note": "thin — IC not measured below 10 bets"}
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in pairs)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return {"n": n, "note": "degenerate variance"}
    ic = cov / (vx * vy) ** 0.5
    breadth = n
    return {"n": n, "ic": round(ic, 4), "breadth": breadth,
            "ir_weekly": round(ic * breadth ** 0.5, 3),
            "verdict": ("skill_positive" if ic > 0.05
                        else "skill_negative" if ic < -0.05 else "no_edge_measured")}


def recheck_yield(conviction_review: dict, closed_events: list[dict]) -> dict:
    """Raschke recheck economics: what the recheck mechanism saved vs cost.
    deferred = holds v1 would have fired (conviction_decay_deferred events);
    abandons = conviction_decay closes with their realized pnl by reason —
    a reason whose abandons net POSITIVE is cutting future losers, NEGATIVE
    is cutting would-be winners (the 2026-08-21 audit's −$10.9/day class)."""
    deferred = sum(v for k, v in conviction_review.items()
                   if k.startswith("conviction_decay_deferred"))
    abandons: dict = {}
    for e in closed_events:
        reason = e.get("exit_reason") or ""
        if not reason.startswith("conviction_decay"):
            continue
        p = e.get("pnl")
        if isinstance(p, str):
            p = p.replace("$", "").replace("+", "")
        try:
            p = float(p or 0.0)
        except (TypeError, ValueError):
            p = 0.0
        sub = reason.split(":", 1)[1] if ":" in reason else "v1"
        a = abandons.setdefault(sub, {"n": 0, "pnl": 0.0})
        a["n"] += 1
        a["pnl"] += p
    return {"deferred": deferred,
            "abandons": {k: {"n": v["n"], "pnl": round(v["pnl"], 3)}
                         for k, v in sorted(abandons.items())}}


def session_of(hour_utc: int) -> str:
    if hour_utc < 7:
        return "asia"
    if hour_utc < 12:
        return "london"
    if hour_utc < 21:
        return "us"
    return "off_hours"


def session_attribution(records: list[dict]) -> dict:
    agg = defaultdict(list)
    for r in records:
        if r.get("outcome") not in ("win", "loss"):
            continue
        ts = r.get("timestamp_ms") or 0
        hour = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).hour if ts else 12
        agg[session_of(hour)].append(pnl_net(r))
    return {k: {"n": len(v), "pnl": round(sum(v), 3),
                "wr": round(sum(1 for x in v if x > 0) / len(v), 3)}
            for k, v in sorted(agg.items())}


def slippage_bps(fills: list[dict], kline_close: dict[int, float]) -> list[float]:
    """fills: [{ts_ms, price}]; kline_close: minute_ms → close. Signed bps per
    fill (positive = paid above reference)."""
    out = []
    for f in fills:
        minute = int(f["ts_ms"] // 60000 * 60000)
        ref = kline_close.get(minute) or kline_close.get(minute - 60000)
        if ref and ref > 0:
            out.append((f["price"] - ref) / ref * 1e4)
    return out


def summarize_slippage(per_venue: dict[str, list[float]], skipped: dict) -> dict:
    out = {}
    for venue, vals in per_venue.items():
        if not vals:
            continue
        out[venue] = {
            "n_fills": len(vals),
            "avg_abs_bps": round(sum(abs(v) for v in vals) / len(vals), 1),
            "max_abs_bps": round(max(abs(v) for v in vals), 1),
            "mean_signed_bps": round(sum(vals) / len(vals), 1),
            "flag": "systematic_slippage" if sum(abs(v) for v in vals) / len(vals) > 10 else "",
        }
    if skipped:
        out["_skipped"] = skipped
    return out


# ── I/O helpers ──────────────────────────────────────────────────────────────

def load_outcome_records(day: str) -> list[dict]:
    """Primary source: outcomes.db (SQLite). Falls back to empty list."""
    db_path = os.path.join(LOG_DIR, "outcomes.db")
    if not os.path.exists(db_path):
        return []
    out = []
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT trade_id, symbol, direction, net_pnl_usd, exit_reason, "
            "entry_time_ms, exit_time_ms, hold_time_hours, coherence_mult "
            "FROM outcomes WHERE DATE(exit_time_ms/1000, 'unixepoch') = ?",
            (day,),
        )
        for row in cur.fetchall():
            tid, sym, direc, pnl, reason, entry_ms, exit_ms, hold_h, coh = row
            out.append({
                "outcome": "win" if (pnl or 0) > 0 else "loss",
                "symbol": sym,
                "direction": direc,
                "pnl_usd": pnl,
                "pnl_net_usd": pnl,
                "timestamp_ms": exit_ms,
                "entry_id": tid,
                "closed_at_ms": exit_ms,
                "hold_time_ms": int((hold_h or 0) * 3600000),
                "coherence_score": coh,
                "exit_reason": reason,
            })
        conn.close()
    except Exception:
        return []
    return out


def load_journal_records(day: str) -> list[dict]:
    files = sorted(glob.glob(os.path.join(LOG_DIR, "trade_journal_*.json")),
                   key=os.path.getmtime)
    seen, out = set(), []
    for path in files:
        try:
            records = json.load(open(path))
        except Exception:
            continue
        for r in records:
            ts = r.get("timestamp_ms") or 0
            day_str = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d") if ts else ""
            if day_str != day:
                continue
            if is_phantom_record(r):
                continue
            key = (r.get("entry_id"), r.get("closed_at_ms"))
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
    return out


def _iso_ms(ts: str) -> int:
    """ISO-8601 log timestamp → epoch ms (0 on parse failure)."""
    try:
        return int(datetime.fromisoformat(
            ts.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def scan_aria_log(day: str) -> dict:
    """Single pass over aria.log filtered to the target date. JSON-parse only
    position_closed lines from __main__ (they carry exit_reason)."""
    res = {"signal_ready": Counter(), "vetoes": Counter(),
           "phantom": Counter(), "closed_events": [],
           "conviction_review": Counter(),
           "elite_override": [], "operator_positions": [],
           "new_plane": Counter()}
    needle = f'"{day}T'
    if not os.path.exists(ARIA_LOG):
        res["error"] = "aria.log missing"
        return res
    with open(ARIA_LOG, errors="replace") as f:
        for line in f:
            if needle not in line:
                continue
            if '"event"' not in line:
                continue
            try:
                ev = line.split('"event": "', 1)[1].split('"', 1)[0]
            except IndexError:
                continue
            sym = ""
            if '"symbol": "' in line:
                try:
                    sym = line.split('"symbol": "', 1)[1].split('"', 1)[0]
                except IndexError:
                    pass
            if ev == "signal_ready" and sym:
                res["signal_ready"][sym] += 1
            if ev == "aster_session_start_equity" and '"equity": ' in line:
                try:
                    res["aster_equity"] = float(
                        line.split('"equity": ', 1)[1].split(",")[0].split("}")[0])
                except (IndexError, ValueError):
                    pass
            if ev in VETO_EVENTS and sym:
                res["vetoes"][(sym, ev)] += 1
            if ev in PHANTOM_EVENTS:
                res["phantom"][ev] += 1
            if ev in ("conviction_decay_closed", "conviction_decay_deferred"):
                key = ev
                if '"reason": "' in line:
                    try:
                        key = f"{ev}:{line.split(chr(34) + 'reason' + chr(34) + ': ' + chr(34), 1)[1].split(chr(34), 1)[0]}"
                    except IndexError:
                        pass
                res["conviction_review"][key] += 1
            if ev == "position_closed" and '"logger": "__main__"' in line:
                try:
                    res["closed_events"].append(json.loads(line))
                except Exception:
                    pass
            if ev in ("direction_loss_block_elite_override",
                      "direction_loss_block_elite_override_denied"):
                try:
                    row = json.loads(line[line.index("{"):])
                    res["elite_override"].append({
                        "event": ev, "symbol": row.get("symbol", sym),
                        "direction": row.get("direction", ""),
                        "coherence": row.get("coherence"),
                        "ts_ms": _iso_ms(row.get("timestamp", "")),
                    })
                except Exception:
                    pass
            if ev in ("operator_position_observed", "operator_position_update",
                      "operator_position_closed"):
                try:
                    row = json.loads(line[line.index("{"):])
                    res["operator_positions"].append({
                        "event": ev, "symbol": row.get("symbol", sym),
                        "side": row.get("side", ""), "size": row.get("size"),
                        "entry": row.get("entry"),
                        "upnl": row.get("upnl", row.get("last_upnl")),
                        "leverage": row.get("leverage"),
                        "ts_ms": _iso_ms(row.get("timestamp", "")),
                    })
                except Exception:
                    pass
            if ev in ("base_rate_veto_emerging_trend_exempted",
                      "emerging_trend_state", "emerging_trend_size_boost",
                      "roe_ratchet_stop_raised",
                      "roe_ratchet_native_stop_replaced",
                      "roe_ratchet_native_replace_failed"):
                res["new_plane"][ev] += 1
            if (ev == "signal_rejected_counter_trend"
                    and '"reason": "emerging_trend"' in line):
                res["new_plane"]["counter_trend:emerging_trend"] += 1
    return res


def load_json(path: str, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


# ── D25: threshold reachability (CEO commission 2026-09-08, due 09-10) ───────
# A threshold whose input never reaches it is a CONSTANT, not a gate. This
# block histograms the LIVE INPUT of exit-side thresholds from aria.log and
# reports how often each is reached. Incremental: byte-offset state so the
# daily run rescans only new bytes (aria.log is >1GB); rotation (file shrink)
# resets the offset. Exact event strings only (D24 lesson — never substrings).
# Thresholds are the CEO-commissioned values as of 2026-09-08 (treasury
# tp1=15/tp2=25/trail=15, quiet gate events_60s>=40, recovery floor 5.6,
# ratchet ATR floor 1.0); this tool does NOT propose values.

REACH_STATE_PATH = os.path.join(LOG_DIR, "threshold_reachability_state.json")

# series -> (bin_width, bin_offset); bin idx = floor((value - offset) / width)
REACH_SERIES = {
    "cluster_roe": (0.5, -30.0),      # treasury_heartbeat clusters.*.roe
    "cluster_peak": (0.5, -30.0),     # treasury_heartbeat clusters.*.peak
    "events_60s": (5.0, 0.0),         # vc activity at emit time (event-selected)
    "coherence": (0.25, 0.0),         # signal_ready coherence
    "nat_stop_dist_atr": (0.1, 0.0),  # ratchet: |mark - legacy_stop| / atr
    "atr_pct": (0.02, 0.0),           # dead-market gate atr_pct (PERCENT plane)
}

# (name, series, threshold, fire_reason, arms_logged) — fire_reason is the
# treasury_order_firing reason (or "suppressed" for the ratchet floor, or the
# quant_filter_blocked reason for dead_market_atr) whose last-seen date gives
# days_since_last_fire; None = no fire event exists. arms_logged declares
# whether the input series is observable on BOTH sides of the gate decision
# ("both"), only when it blocks ("block_only"), or — for dead_market_atr —
# derived from the T3b pass-side wire ("dynamic": "both" once
# dead_market_atr_pass events exist, else "block_only"). Reachability and
# gradability are different failures (CEO s21): a block_only series is
# censored by construction and the threshold is not tunable at any n.
REACH_THRESHOLDS = [
    ("treasury_tp1", "cluster_roe", 15.0, "treasury_tp1", "both"),
    ("treasury_tp2", "cluster_roe", 25.0, "treasury_tp2", "both"),
    ("treasury_trail_lock", "cluster_peak", 15.0, "treasury_trail_lock", "both"),
    ("quiet_gate_events_60s", "events_60s", 40.0, None, "both"),
    ("recovery_coherence_floor", "coherence", 5.6, None, "both"),
    ("roe_ratchet_atr_floor", "nat_stop_dist_atr", 1.0, "suppressed", "block_only"),
    ("dead_market_atr", "atr_pct", 0.2, "dead_market_atr_too_small", "dynamic"),
]

_REACH_NEEDLES = (
    '"event": "treasury_heartbeat"',
    '"event": "treasury_order_firing"',
    '"event": "signal_ready"',
    '"event": "cascade_detected"',
    '"event": "vc_liquidation_signal"',
    '"event": "quant_filter_blocked"',
    '"event": "roe_ratchet_atr_floor_suppressed"',
    '"event": "roe_ratchet_stop_raised"',
    '"event": "dead_market_atr_pass"',
)


def _reach_field_float(line: str, key: str):
    tok = f'"{key}": '
    i = line.find(tok)
    if i < 0:
        return None
    j = i + len(tok)
    k = j
    while k < len(line) and line[k] in "0123456789+-.eE":
        k += 1
    try:
        return float(line[j:k])
    except ValueError:
        return None


def _reach_field_str(line: str, key: str) -> str:
    tok = f'"{key}": "'
    i = line.find(tok)
    if i < 0:
        return ""
    j = i + len(tok)
    k = line.find('"', j)
    return line[j:k] if k > j else ""


def _reach_bin_add(sstate: dict, width: float, offset: float, value: float) -> None:
    idx = int((value - offset) // width)
    if idx < 0:
        idx = 0
    bins = sstate["bins"]
    while len(bins) <= idx:
        bins.append(0)
    bins[idx] += 1
    sstate["n"] += 1
    sstate["max"] = value if sstate["max"] is None else max(sstate["max"], value)
    sstate["min"] = value if sstate["min"] is None else min(sstate["min"], value)


def _reach_quantile(sstate: dict, width: float, offset: float, q: float):
    n = sstate["n"]
    if n <= 0:
        return None
    target = q * n
    cum = 0
    for i, c in enumerate(sstate["bins"]):
        cum += c
        if cum >= target:
            return round(offset + (i + 0.5) * width, 4)
    return round(offset + (len(sstate["bins"]) - 0.5) * width, 4)


def _reach_days_since(ts: str, day: str):
    """Whole days between an ISO timestamp and the digest day (None if no ts)."""
    if not ts:
        return None
    try:
        d0 = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
        d1 = datetime.fromisoformat(day).date()
        return (d1 - d0).days
    except Exception:
        return None


def threshold_reachability_update(log_path: str = ARIA_LOG,
                                  state_path: str = REACH_STATE_PATH) -> dict:
    """Incremental scan of aria.log for reachability series. Best-effort:
    a torn tail line is left for the next run; one bad line kills nothing."""
    st = load_json(state_path, None) or {}
    series = st.get("series") or {
        name: {"bins": [], "n": 0, "max": None, "min": None}
        for name in REACH_SERIES}
    for name in REACH_SERIES:           # tolerate partial/old state files
        series.setdefault(name, {"bins": [], "n": 0, "max": None, "min": None})
    fires = st.get("fires") or {}       # fire reason -> last ISO ts
    reaches = st.get("reaches") or {}   # threshold name -> last ISO ts
    ge_counts = st.get("ge_counts") or {}   # threshold name -> exact count >= thr
    sources = st.get("events_60s_sources") or {}
    dead_arms = st.get("dead_mkt_arms") or {"block": 0, "pass": 0}
    ratchet_raises = int(st.get("ratchet_stop_raised_n", 0))
    offset = int(st.get("offset", 0))

    if not os.path.exists(log_path):
        return {"error": "aria.log missing"}
    if os.path.getsize(log_path) < offset:
        offset = 0                      # rotation: rescan (fractions stay honest)

    def observe(series_name: str, value: float, ts: str) -> None:
        w, off = REACH_SERIES[series_name]
        _reach_bin_add(series[series_name], w, off, value)
        for name, sname, thr, _f, _a in REACH_THRESHOLDS:
            if sname == series_name and value >= thr:
                ge_counts[name] = ge_counts.get(name, 0) + 1
                reaches[name] = ts

    with open(log_path, "rb") as f:
        f.seek(offset)
        while True:
            raw = f.readline()
            if not raw or not raw.endswith(b"\n"):
                break                   # EOF or torn tail — leave for next run
            offset = f.tell()
            if not any(n.encode() in raw for n in _REACH_NEEDLES):
                continue
            line = raw.decode("utf-8", "replace")
            ts = _reach_field_str(line, "timestamp")
            if '"event": "treasury_heartbeat"' in line:
                try:
                    row = json.loads(line[line.index("{"):])
                    for c in (row.get("clusters") or {}).values():
                        if not isinstance(c, dict):
                            continue
                        if isinstance(c.get("roe"), (int, float)):
                            observe("cluster_roe", float(c["roe"]), ts)
                        if isinstance(c.get("peak"), (int, float)):
                            observe("cluster_peak", float(c["peak"]), ts)
                except Exception:
                    pass
            elif '"event": "treasury_order_firing"' in line:
                reason = _reach_field_str(line, "reason")
                if reason:
                    fires[reason] = ts
            elif '"event": "signal_ready"' in line:
                # signal_ready's "score" is the interpreter coherence pre-gates —
                # the unconditional carrier (sizing_chain doesn't carry it).
                v = _reach_field_float(line, "score")
                if v is not None:
                    observe("coherence", v, ts)
            elif '"event": "quant_filter_blocked"' in line:
                if '"reason": "quiet_market_pause"' in line:
                    v = _reach_field_float(line, "events_60s")
                    if v is not None:
                        observe("events_60s", v, ts)
                        sources["quiet_market_pause"] = sources.get("quiet_market_pause", 0) + 1
                elif '"reason": "dead_market_atr_too_small"' in line:
                    # BLOCK arm (pre-T3b the only logged arm — #41: censored
                    # by construction, max can never exceed the floor).
                    v = _reach_field_float(line, "atr_pct")
                    if v is not None:
                        observe("atr_pct", v, ts)
                        dead_arms["block"] += 1
                        fires["dead_market_atr_too_small"] = ts
            elif '"event": "dead_market_atr_pass"' in line:
                # PASS arm (T3b, 84f2dc7) — throttled 300s/symbol bot-side.
                v = _reach_field_float(line, "atr_pct")
                if v is not None:
                    observe("atr_pct", v, ts)
                    dead_arms["pass"] += 1
            elif '"event": "cascade_detected"' in line:
                v = _reach_field_float(line, "events_60s")
                if v is not None:
                    observe("events_60s", v, ts)
                    sources["cascade_detected"] = sources.get("cascade_detected", 0) + 1
            elif '"event": "vc_liquidation_signal"' in line:
                v = _reach_field_float(line, "events_60s")
                if v is not None:
                    observe("events_60s", v, ts)
                    sources["vc_liquidation_signal"] = sources.get("vc_liquidation_signal", 0) + 1
            elif '"event": "roe_ratchet_atr_floor_suppressed"' in line:
                try:
                    row = json.loads(line[line.index("{"):])
                    mark = float(row.get("mark") or 0)
                    legacy = float(row.get("legacy_stop") or 0)
                    atr = float(row.get("atr") or 0)
                    if atr > 0 and mark > 0 and legacy > 0:
                        observe("nat_stop_dist_atr", abs(mark - legacy) / atr, ts)
                    fires["suppressed"] = ts
                except Exception:
                    pass
            elif '"event": "roe_ratchet_stop_raised"' in line:
                ratchet_raises += 1

    out = {"offset": offset, "series": series, "fires": fires,
           "reaches": reaches, "ge_counts": ge_counts,
           "events_60s_sources": sources,
           "dead_mkt_arms": dead_arms,
           "ratchet_stop_raised_n": ratchet_raises}
    tmp = state_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(out, fh)
    os.replace(tmp, state_path)
    return out


def build_threshold_reachability(day: str, log_path: str = ARIA_LOG,
                                 state_path: str = REACH_STATE_PATH) -> list:
    """D25 block: per-threshold reachability over the full scanned history."""
    st = threshold_reachability_update(log_path, state_path)
    if st.get("error"):
        return [{"error": st["error"]}]
    rows = []
    dead_arms = st.get("dead_mkt_arms") or {"block": 0, "pass": 0}
    for name, sname, thr, fire_key, arms in REACH_THRESHOLDS:
        if arms == "dynamic":
            arms = "both" if dead_arms.get("pass", 0) > 0 else "block_only"
        s = st["series"][sname]
        w, off = REACH_SERIES[sname]
        n = s["n"]
        ge = st["ge_counts"].get(name, 0)
        frac = round(ge / n, 6) if n else None
        row = {"name": name, "field": sname, "threshold": thr, "n_obs": n,
               "p50": _reach_quantile(s, w, off, 0.50),
               "p99": _reach_quantile(s, w, off, 0.99),
               "max": (round(s["max"], 4) if s["max"] is not None else None),
               "frac_ge_threshold": frac,
               "arms_logged": arms,
               "days_since_last_reach": _reach_days_since(
                   st["reaches"].get(name, ""), day),
               "days_since_last_fire": (_reach_days_since(
                   st["fires"].get(fire_key, ""), day) if fire_key else None)}
        if frac == 0.0 and n >= 1000:
            row["verdict"] = ("CONSTANT — threshold outside the live support "
                              "of its own input (0.000% reach, n>=1000)")
        if name == "dead_market_atr":
            row["arms"] = dict(dead_arms)
            if arms == "block_only" and n >= 1000:
                # #41: block-side only — max is capped at the floor by
                # construction, so the band any correction would move is
                # unobservable. Reachability is measurable; GRADABILITY is
                # not. Say it in words (CEO s21).
                row["verdict"] = (
                    "CENSORED — block-side-only series (T3b pass wire not yet "
                    "observed); max<=floor by construction, the pass band is "
                    "unobservable and the floor is NOT tunable at any n")
        if name == "roe_ratchet_atr_floor":
            # CIRCULARITY GUARD: nat_stop_dist_atr is measured at suppressed
            # events, where the legacy stop is <1 ATR by construction — a 0%
            # reach here proves nothing (D10 lesson). The honest stat is the
            # floor-decline share of all legacy-desired tightens.
            raised = st.get("ratchet_stop_raised_n", 0)
            total = n + raised
            row["construction_note"] = (
                "series self-selected (<threshold by construction); "
                "frac_ge is NOT evidence of binding")
            row["floor_decline_share_of_tightens"] = (
                round(n / total, 4) if total else None)
            row["n_tighten_evaluations"] = total
            row.pop("verdict", None)
        rows.append(row)
    if st["series"]["events_60s"]["n"]:
        rows.append({"note": "events_60s sample is event-selected",
                     "sources": st.get("events_60s_sources", {})})
    rows.append({"note": "roe_ratchet context",
                 "ratchet_stop_raised_n": st.get("ratchet_stop_raised_n", 0)})
    return rows


# ── D24: regime duty cycle (CEO commission 2026-09-08) ───────────────────────
# Every performance number the firm publishes was measured under SOME regime
# mix; September's were silently conditioned on a 71%-recovery tape (#38).
# One log pass yields per-day recovery duty (activated→deactivated intervals)
# and regime distribution (regime_calculated is periodic, so event share ≈
# time share). D25's decay windows carry this field (CEO amendment 2).

def _duty_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def build_regime_duty(log_path: str = ARIA_LOG) -> dict:
    intervals = []          # (start_ts, end_ts) in recovery
    regime_counts = {}      # date -> Counter
    open_ts = None
    last_ts = None
    try:
        with open(log_path, errors="ignore") as f:
            for line in f:
                if "recovery_mode_a" not in line and "regime_calculated" not in line:
                    continue
                i = line.find("{")
                if i < 0:
                    continue
                try:
                    d = json.loads(line[i:])
                except Exception:
                    continue
                ev = d.get("event", "")
                try:
                    ts = datetime.fromisoformat(
                        str(d.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                last_ts = ts
                if ev == "recovery_mode_activated":
                    if open_ts is None:
                        open_ts = ts
                elif ev == "recovery_mode_deactivated":
                    if open_ts is not None:
                        intervals.append((open_ts, ts))
                        open_ts = None
                elif ev == "regime_calculated":
                    day = _duty_day(ts)
                    regime_counts.setdefault(day, Counter())[d.get("regime", "?")] += 1
    except Exception as e:
        return {"error": str(e)[:200]}
    if open_ts is not None and last_ts is not None:
        intervals.append((open_ts, last_ts))   # still in recovery at log tail

    days = {}
    all_dates = sorted(set(regime_counts) |
                       {_duty_day(s) for s, _ in intervals} |
                       {_duty_day(e) for _, e in intervals})
    for day in all_dates:
        d0 = datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()
        d1 = d0 + 86400
        rec_s = sum(max(0.0, min(e, d1) - max(s, d0)) for s, e in intervals
                    if s < d1 and e > d0)
        rc = regime_counts.get(day, Counter())
        total = sum(rc.values())
        days[day] = {
            "recovery_duty_pct": round(100.0 * rec_s / 86400.0, 1),
            "regime_share_pct": {k: round(100.0 * v / total, 1)
                                 for k, v in rc.most_common()} if total else {},
        }
    return {
        "declaration": {
            "field": "recovery_duty_pct = seconds between recovery_mode_activated/"
                     "deactivated overlapping the UTC day / 86400; regime_share = "
                     "regime_calculated event share (periodic -> time proxy)",
            "window_field": "event timestamp", "n_intervals": len(intervals),
        },
        "days": days,
    }


# ── D25: strategy decay watch (CEO-endorsed 2026-09-08, two amendments) ──────
# Rolling SR < 0.5x baseline for >=3 consecutive windows -> decay flag
# (persistence, not variance — Aronson). Amendment 1: baseline = the tag's
# DEPLOYMENT-WINDOW SR and every window prints n. Amendment 2: every window
# carries the D24 recovery duty it was measured under. Digest-only; the flag
# feeds CEO review, never auto-action.

DECAY_WINDOW_D = 7
DECAY_BASELINE_D = 14
DECAY_MIN_CONSEC = 3
DECAY_RATIO = 0.5


def _journal_by_day() -> dict:
    """All journal closes bucketed by UTC day in ONE pass (per-day loaders
    re-read every file per call — quadratic over 45 days)."""
    out = {}
    for path in glob.glob(os.path.join(LOG_DIR, "trade_journal_*.json")):
        try:
            records = json.load(open(path))
        except Exception:
            continue
        if not isinstance(records, list):
            continue
        for r in records:
            ts = r.get("closed_at_ms") or r.get("timestamp_ms") or 0
            if not ts:
                continue
            day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            out.setdefault(day, []).append(r)
    return out


def _sr(xs: list) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    return round(mean / (var ** 0.5), 3) if var > 0 else None


def build_decay_watch(day: str, duty: dict | None = None) -> list:
    by_day = _journal_by_day()
    tags = {}
    for d, recs in by_day.items():
        for r in recs:
            tag = r.get("strategy_tag") or "unknown"
            tags.setdefault(tag, {}).setdefault(d, 0.0)
            tags[tag][d] += pnl_net(r)
    duty_days = (duty or {}).get("days") or {}

    rows = []
    for tag, series in sorted(tags.items()):
        days_sorted = sorted(series)
        if len(days_sorted) < DECAY_BASELINE_D + DECAY_WINDOW_D:
            rows.append({"strategy_tag": tag, "verdict": "insufficient_history",
                         "n_days": len(days_sorted)})
            continue
        base_days = days_sorted[:DECAY_BASELINE_D]
        base = [series[d] for d in base_days]
        base_sr = _sr(base)
        windows = []
        consec = 0
        for i in range(DECAY_BASELINE_D, len(days_sorted) - DECAY_WINDOW_D + 1):
            wdays = days_sorted[i:i + DECAY_WINDOW_D]
            w = [series[d] for d in wdays]
            wsr = _sr(w)
            wduty = [duty_days[d]["recovery_duty_pct"]
                     for d in wdays if d in duty_days]
            flag = (base_sr is not None and base_sr > 0 and wsr is not None
                    and wsr < DECAY_RATIO * base_sr)
            consec = consec + 1 if flag else 0
            windows.append({"start": wdays[0], "n_days": len(wdays),
                            "net_usd": round(sum(w), 3), "sr": wsr,
                            "recovery_duty_pct": (round(sum(wduty) / len(wduty), 1)
                                                  if wduty else None),
                            "below_half_baseline": flag})
        if base_sr is not None and base_sr <= 0:
            verdict = ("baseline_nonpositive — born unprofitable; decay is "
                       "undefined against a losing baseline (viability "
                       "question, not a decay question)")
        else:
            verdict = ("DECAY — rolling SR < 0.5x deployment baseline for "
                       f">={DECAY_MIN_CONSEC} consecutive windows" if consec >= DECAY_MIN_CONSEC
                       else "ok")
        rows.append({"strategy_tag": tag,
                     "baseline": {"window": [base_days[0], base_days[-1]],
                                  "n_days": len(base_days), "sr": base_sr},
                     "latest_window": windows[-1] if windows else None,
                     "consecutive_below": consec, "verdict": verdict,
                     "windows": windows[-8:]})
    return rows


# ── Public-endpoint comparisons (network, best-effort) ───────────────────────

async def _fetch_klines(client, venue: str, symbol: str, start_ms: int,
                        yahoo_sym: str = "") -> dict[int, float]:
    """Return minute_ms → close for the symbol's fills window."""
    out = {}
    try:
        if venue == "bybit":
            r = await client.get(BYBIT_KLINE, params={
                "category": "linear", "symbol": f"{symbol.replace('-USD', '')}USDT",
                "interval": "1", "start": start_ms, "limit": 300})
            rows = (r.json().get("result") or {}).get("list") or []
            for k in rows:
                out[int(k[0])] = float(k[4])
        elif venue == "aster":
            r = await client.get(ASTER_KLINE, params={
                "symbol": symbol, "interval": "1m",
                "startTime": start_ms, "limit": 300})
            for k in r.json():
                out[int(k[0])] = float(k[4])
        elif venue == "yahoo":
            # 1m bars are retained ~7d — range=1d only covers TODAY, which can
            # never match yesterday's fills (the digest's default day).
            r = await client.get(YAHOO_CHART.format(yahoo_sym),
                                 params={"interval": "1m", "range": "5d"})
            result = (r.json().get("chart", {}).get("result") or [None])[0]
            if result:
                ts = result.get("timestamp") or []
                closes = ((result.get("indicators", {}).get("quote") or [{}])[0]
                          .get("close") or [])
                for t, c in zip(ts, closes):
                    if c is not None:
                        out[int(t) * 1000] = float(c)
    except Exception:
        pass
    return out


async def _fetch_klines_hl(client, venue: str, symbol: str, start_ms: int,
                           yahoo_sym: str = "") -> list[tuple[int, float, float]]:
    """Return [(minute_ms, high, low)] from start_ms — the post-stop window
    the tight-stop regret study measures. Same endpoints as _fetch_klines."""
    out: list[tuple[int, float, float]] = []
    try:
        if venue == "bybit":
            r = await client.get(BYBIT_KLINE, params={
                "category": "linear", "symbol": f"{symbol.replace('-USD', '')}USDT",
                "interval": "1", "start": start_ms, "limit": 300})
            rows = (r.json().get("result") or {}).get("list") or []
            out = [(int(k[0]), float(k[2]), float(k[3])) for k in rows]
        elif venue == "aster":
            r = await client.get(ASTER_KLINE, params={
                "symbol": symbol, "interval": "1m",
                "startTime": start_ms, "limit": 300})
            out = [(int(k[0]), float(k[2]), float(k[3])) for k in r.json()]
        elif venue == "yahoo":
            r = await client.get(YAHOO_CHART.format(yahoo_sym),
                                 params={"interval": "1m", "range": "5d"})
            result = (r.json().get("chart", {}).get("result") or [None])[0]
            if result:
                ts = result.get("timestamp") or []
                q = (result.get("indicators", {}).get("quote") or [{}])[0]
                for t, h, l in zip(ts, q.get("high") or [], q.get("low") or []):
                    if h is not None and l is not None and int(t) * 1000 >= start_ms:
                        out.append((int(t) * 1000, float(h), float(l)))
        out.sort(key=lambda x: x[0])
    except Exception:
        pass
    return out


async def stop_autopsy(records: list[dict], venue_of, yahoo_of,
                       aster_sym_of) -> dict:
    """Tight-stop regret study (2026-09-04 operator directive: "shadow when
    tight stops are bad"). For every stop-class losing close, measure the
    post-stop window: did price recover to BREAKEVEN (the entry) within
    1h / 4h, and what was the max favorable excursion from entry? A stop
    whose market promptly returns past the entry took us out of a trade
    that would have healed — that is the measurable definition of "too
    tight". Best-effort per close; kline gaps skip, never fabricate."""
    closes = [r for r in records
              if is_stop_close(r) and r.get("entry_price") and r.get("closed_at_ms")]
    out: dict = {"n_stop_closes": len(closes)}
    if not closes:
        out["note"] = "no stop-class losses this day"
        return out
    import httpx
    rows, skipped = [], 0
    async with httpx.AsyncClient(timeout=8.0,
                                 headers={"User-Agent": "Mozilla/5.0 (compatible; ARIA-digest/1.0)"}) as client:
        async def one(r):
            sym = r["symbol"]
            venue = venue_of(sym)
            if venue == "skip":
                return None
            entry = float(r["entry_price"])
            t0 = int(r["closed_at_ms"])
            bars = await _fetch_klines_hl(
                client, venue, aster_sym_of(sym) if venue == "aster" else sym,
                t0, yahoo_sym=yahoo_of(sym))
            if not bars or entry <= 0:
                return None
            long_side = r.get("direction") == "long"
            h1, h4 = t0 + 3_600_000, t0 + 4 * 3_600_000
            w1 = [b for b in bars if b[0] <= h1]
            w4 = [b for b in bars if b[0] <= h4]
            if not w4:
                return None
            if long_side:
                mfe4 = (max(b[1] for b in w4) / entry - 1.0) * 100.0
                rec1 = any(b[1] >= entry for b in w1)
                rec4 = any(b[1] >= entry for b in w4)
            else:
                mfe4 = (1.0 - min(b[2] for b in w4) / entry) * 100.0
                rec1 = any(b[2] <= entry for b in w1)
                rec4 = any(b[2] <= entry for b in w4)
            return {"symbol": sym, "direction": r.get("direction"),
                    "exit_reason": r.get("exit_reason"),
                    "pnl_usd": round(pnl_net(r), 4),
                    "recovered_1h": bool(rec1), "recovered_4h": bool(rec4),
                    "mfe_4h_pct": round(mfe4, 3)}
        rows = [x for x in await asyncio.gather(*(one(r) for r in closes[:25]))
                if x is not None]
    skipped = len(closes) - len(rows)
    n = len(rows)
    rec1 = sum(1 for x in rows if x["recovered_1h"])
    rec4 = sum(1 for x in rows if x["recovered_4h"])
    regret4 = rec4 / n if n else 0.0
    mfes = sorted(x["mfe_4h_pct"] for x in rows)
    out.update({
        "measured": n, "skipped_no_klines": skipped,
        "recovered_1h": rec1, "recovered_4h": rec4,
        "regret_rate_1h": round(rec1 / n, 3) if n else None,
        "regret_rate_4h": round(regret4, 3) if n else None,
        "median_mfe_4h_pct": mfes[n // 2] if n else None,
        "verdict": stop_regret_verdict(n, regret4),
        "worst_regrets": sorted((x for x in rows if x["recovered_4h"]),
                                key=lambda x: -x["mfe_4h_pct"])[:5],
    })
    return out


async def slippage_and_benchmark(records: list[dict], venue_of, yahoo_of,
                                 aster_sym_of, day: str) -> tuple[dict, dict]:
    import httpx
    per_venue: dict[str, list[float]] = defaultdict(list)
    skipped = Counter()
    fills_by_sym = defaultdict(list)
    for r in records:
        if r.get("outcome") not in ("win", "loss"):
            continue
        ep, ts = r.get("entry_price") or 0, r.get("timestamp_ms") or 0
        if ep and ts:
            fills_by_sym[r["symbol"]].append({"ts_ms": ts, "price": ep})
    bench = {}
    async with httpx.AsyncClient(timeout=8.0,
                                 headers={"User-Agent": "Mozilla/5.0 (compatible; ARIA-digest/1.0)"}) as client:
        async def one(sym, fills):
            venue = venue_of(sym)
            if venue == "skip":
                skipped[sym] = len(fills)
                return
            start = min(f["ts_ms"] for f in fills) - 120000
            kc = await _fetch_klines(client, venue,
                                     aster_sym_of(sym) if venue == "aster" else sym,
                                     start, yahoo_sym=yahoo_of(sym))
            if not kc:
                skipped[sym] = len(fills)
                return
            vals = slippage_bps(fills, kc)
            skipped[sym] = skipped.get(sym, 0) + (len(fills) - len(vals))
            per_venue[venue].extend(vals)
        await asyncio.gather(*(one(s, f) for s, f in fills_by_sym.items()))
        # Benchmark: equal-weight BTC/ETH/SOL hold return for the DIGEST day
        # (Bybit daily klines are newest-first — pick the bar whose open_time
        # date matches, then open→close of that bar, not prev-close→cur-close).
        try:
            rets = []
            for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
                r = await client.get(BYBIT_KLINE, params={
                    "category": "linear", "symbol": s, "interval": "D", "limit": 5})
                rows = (r.json().get("result") or {}).get("list") or []
                for k in rows:
                    bar_day = datetime.fromtimestamp(
                        int(k[0]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    if bar_day == day:
                        o, c = float(k[1]), float(k[4])
                        if o > 0:
                            rets.append((c - o) / o * 100)
                        break
            if rets:
                bench["hodl_pct"] = round(sum(rets) / len(rets), 2)
            # Biggest synchronized 4h thrust per major — the move ARIA should
            # have caught even when the full daily bar is ambiguous.
            moves_4h = {}
            for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
                r = await client.get(BYBIT_KLINE, params={
                    "category": "linear", "symbol": s, "interval": "240",
                    "limit": 12})
                rows = (r.json().get("result") or {}).get("list") or []
                best = None
                for k in rows:
                    bar_day = datetime.fromtimestamp(
                        int(k[0]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                    if bar_day != day:
                        continue
                    o, c = float(k[1]), float(k[4])
                    if o > 0:
                        pct = (c - o) / o * 100
                        if best is None or abs(pct) > abs(best):
                            best = pct
                if best is not None:
                    moves_4h[s.replace("USDT", "")] = round(best, 2)
            if moves_4h:
                bench["max_4h_moves"] = moves_4h
        except Exception:
            pass
    return summarize_slippage(per_venue, dict(skipped)), bench


# ── Main ─────────────────────────────────────────────────────────────────────

def venue_classifier():
    """Lazy repo imports — module stays importable without pydantic/env."""
    try:
        from core.config import Settings
        from data.tradfi_feed import TRADFI_SYMBOLS
        from execution.aster_client import to_aster_symbol
        cfg = Settings()
        aster = set(getattr(cfg, "aster_assets", []))
        assets = list(getattr(cfg, "assets", []))

        def venue_of(sym: str) -> str:
            if "SSI" in sym:
                return "skip"
            if sym in aster:
                return "aster"
            if sym in TRADFI_SYMBOLS:
                return "yahoo"
            return "bybit"

        def yahoo_of(sym: str) -> str:
            return TRADFI_SYMBOLS.get(sym, "")

        return assets, venue_of, yahoo_of, to_aster_symbol
    except Exception as e:
        return [], lambda s: "skip", lambda s: "", lambda s: s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()
    day = args.date or datetime.fromtimestamp(
        time.time() - 86400, tz=timezone.utc).strftime("%Y-%m-%d")
    run_wday = datetime.now(timezone.utc).weekday()

    assets, venue_of, yahoo_of, aster_sym_of = venue_classifier()
    records = load_outcome_records(day) or load_journal_records(day)
    logscan = scan_aria_log(day)
    dd_state = load_json(os.path.join(LOG_DIR, "drawdown_state.json"), {})
    gate_report = load_json(os.path.join(LOG_DIR, "gate_report.json"), {})
    balance = float(dd_state.get("current") or 0.0)

    _ms = sorted(int(r["closed_at_ms"]) for r in records if r.get("closed_at_ms"))
    digest: dict = {"date": day, "generated": datetime.now(timezone.utc).isoformat(),
                    "declaration": {
                        "pnl_field": "pnl_net_usd (fallback pnl_usd where absent; outcomes.db rows synthesize both from net_pnl_usd)",
                        "window_field": "closed_at_ms",
                        "window_bounds": ([datetime.fromtimestamp(_ms[0] / 1000, timezone.utc).isoformat(),
                                           datetime.fromtimestamp(_ms[-1] / 1000, timezone.utc).isoformat()]
                                          if _ms else None),
                        "dedup": "(entry_id, closed_at_ms)"},
                    "trades_closed": sum(1 for r in records if r.get("outcome") in ("win", "loss"))}

    # #53: dust class purged from WR/expectancy, censused separately
    _exp_records = [r for r in records if not is_dust_close(r, venue_of)]
    digest["expectancy"] = expectancy_by_symbol(_exp_records)
    digest["dust"] = dust_census(records, venue_of)
    _aster_eq = float(logscan.get("aster_equity") or 0.0)
    _venue_equity = ({"aster": _aster_eq, "sodex": balance - _aster_eq}
                     if _aster_eq > 0 and balance > _aster_eq else None)
    # outcomes.db rows lack position_size/entry_price/mult fields — size_chain
    # on them is silently empty. Journal records carry the full size chain, so
    # prefer them here whenever they exist for the day (observability fix
    # 2026-08-25: section dead since outcomes.db became the primary source).
    _journal_records = load_journal_records(day)
    _size_records = (_journal_records
                     if any(r.get("position_size") and r.get("entry_price")
                            for r in _journal_records) else records)
    digest["size_chain"] = size_chain(_size_records, balance, venue_of=venue_of,
                                      venue_equity=_venue_equity)
    digest["hold_asymmetry"] = hold_asymmetry(records)
    # Q1.5': fee_drag must read the JOURNAL records (day-filtered, phantom-
    # filtered, cross-file deduped) — outcomes.db synthesizes pnl_usd =
    # pnl_net_usd = net_pnl_usd, which pinned drag at 0.0 (dead wire). The
    # main figure keeps full-book semantics (net_pnl feeds the benchmark);
    # fee_drag_ex_spcx answers the CEO's SPCX-excluded cost question.
    _tradfi = _tradfi_set()
    digest["fee_drag"] = fee_drag(_journal_records or records,
                                  venue_of=venue_of, tradfi=_tradfi)
    digest["fee_drag_ex_spcx"] = fee_drag(
        [r for r in (_journal_records or records)
         if r.get("symbol") != "SPCX-USD"], venue_of=venue_of, tradfi=_tradfi)
    digest["net_pnl"] = digest["fee_drag"]["net"]
    digest["exit_pareto"] = exit_pareto(logscan["closed_events"])
    digest["conviction_review"] = dict(logscan["conviction_review"])
    digest["recheck_yield"] = recheck_yield(logscan["conviction_review"],
                                            logscan["closed_events"])
    digest["silence_census"] = silence_census(assets, logscan["signal_ready"], logscan["vetoes"])
    digest["elite_override"] = elite_override_census(logscan["elite_override"],
                                                     records)
    digest["operator_positions"] = operator_positions_section(
        logscan["operator_positions"])
    digest["new_plane_events"] = dict(logscan["new_plane"])

    peak = float(dd_state.get("peak_balance") or dd_state.get("peak") or 0.0)
    digest["phantom_sweep"] = {
        "dd_peak": peak, "dd_current": balance,
        "peak_ratio_suspect": bool(balance and peak > 1.3 * balance),
        "event_counts": dict(logscan["phantom"]),
    }

    ga = (gate_report or {}).get("gate_accuracy", {})
    gad = (gate_report or {}).get("gate_accuracy_by_day_type", {})
    digest["gates"] = {
        "overall": ga.get("_total", {}),
        "per_gate": {g: {"accuracy": v.get("accuracy"), "n": v.get("gated"),
                         "verdict": v.get("verdict")}
                     for g, v in ga.items() if not g.startswith("_")},
        # Season mismatches only — a gate strong globally but tight in one
        # day type is the row the watchdog should read (dispersion-on-trend
        # was the 2026-08-18 freeze-window finding).
        "day_type_mismatches": {
            dt: {g: v for g, v in gm.items() if v.get("verdict") != "strong"}
            for dt, gm in gad.items()
            if any(v.get("verdict") != "strong" for v in gm.values())
        },
    }
    scored = [json.loads(l) for l in open(os.path.join(LOG_DIR, "shadow_scored.jsonl"))
              if l.strip().startswith("{")] if os.path.exists(os.path.join(LOG_DIR, "shadow_scored.jsonl")) else []
    day_refusals = [r for r in scored
                    if datetime.fromtimestamp(r.get("ts", 0), tz=timezone.utc).strftime("%Y-%m-%d") == day]
    big_missed = sorted((r for r in day_refusals if r.get("won_24h")),
                        key=lambda r: -(r.get("pnl_24h") or 0))[:5]
    digest["gates"]["tail_cost_top5"] = [
        {"symbol": r["symbol"], "gate": r["gate"], "direction": r["direction"],
         "would_be_pnl_24h": round((r.get("pnl_24h") or 0) * 100, 2)}
        for r in big_missed]

    try:
        slip, bench = asyncio.run(slippage_and_benchmark(
            records, venue_of, yahoo_of, aster_sym_of, day))
        digest["slippage"] = slip
        realized = digest["fee_drag"]["net"]
        digest["benchmark"] = {
            "aria_realized_usd": realized,
            "aria_pct": round(100 * realized / balance, 2) if balance else 0.0,
            **bench,
        }
        if "hodl_pct" in bench:
            digest["benchmark"]["delta_pct"] = round(
                digest["benchmark"]["aria_pct"] - bench["hodl_pct"], 2)
        digest["trend_capture"] = trend_capture(
            records, bench.get("hodl_pct"), bench.get("max_4h_moves", {}),
            balance)
    except Exception as e:
        digest["slippage"] = {"error": str(e)[:200]}
        digest["benchmark"] = {"error": str(e)[:200]}
        digest["trend_capture"] = trend_capture(records, None, {}, balance)

    try:
        digest["stop_autopsy"] = asyncio.run(
            stop_autopsy(records, venue_of, yahoo_of, aster_sym_of))
    except Exception as e:
        digest["stop_autopsy"] = {"error": str(e)[:200]}

    # D25 (CEO commission, due 2026-09-10): threshold reachability — a
    # threshold whose input never reaches it is a CONSTANT, not a gate.
    try:
        digest["threshold_reachability"] = build_threshold_reachability(day)
    except Exception as e:
        digest["threshold_reachability"] = {"error": str(e)[:200]}

    # D24/D25 (CEO commissions): regime duty cycle + strategy decay watch —
    # every SR window declares the recovery duty it was measured under.
    try:
        digest["regime_duty"] = build_regime_duty()
    except Exception as e:
        digest["regime_duty"] = {"error": str(e)[:200]}
    try:
        digest["decay_watch"] = build_decay_watch(day, duty=digest.get("regime_duty"))
    except Exception as e:
        digest["decay_watch"] = {"error": str(e)[:200]}

    if run_wday == 0:   # Monday run → weekly sections over the trailing 7d
        week_records = []
        for back in range(7):
            d = datetime.fromtimestamp(time.time() - 86400 * (back + 1),
                                       tz=timezone.utc).strftime("%Y-%m-%d")
            week_records.extend(load_journal_records(d))
        digest["weekly"] = {
            "coherence_calibration": coherence_calibration(week_records),
            "session_attribution": session_attribution(week_records),
            "fundamental_law": fundamental_law(week_records),
            "venue_comparison": load_json(os.path.join(LOG_DIR, "venue_comparison.json"),
                                          {"note": "not generated yet"}),
        }

    # Trend — the compounding loop made visible. Reads the history JSONL the
    # digest itself appends to: is gate accuracy drifting, is the same symbol
    # churning day after day, what did the last 7 days actually net.
    try:
        hist_rows = []
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("{"):
                        hist_rows.append(json.loads(line))
        # Dedup by date (last wins) — same-day reruns used to append duplicate
        # lines, and tail(7 lines) then double-counted trades/net for the week.
        by_date = {}
        for h in hist_rows:
            by_date[h.get("date")] = h
        hist_rows = [h for h in hist_rows
                     if by_date.get(h.get("date")) is h]
        tail = hist_rows[-7:]
        if tail:
            churn_counts = Counter(s for h in tail for s in h.get("churn_flags", []))
            accs = [h["gate_accuracy"] for h in tail
                    if isinstance(h.get("gate_accuracy"), (int, float))]
            # History lines for the phantom days were written before the
            # filter existed — recompute those days' net from the (now
            # phantom-filtered) journals so net_pnl_7d stays honest.
            net7 = sum(h.get("net_pnl") or 0 for h in tail)
            phantom_days = sorted({h.get("date") for h in tail
                                   if h.get("date") in PHANTOM_DAYS})
            for pd_ in phantom_days:
                old = next((h.get("net_pnl") or 0 for h in tail
                            if h.get("date") == pd_), 0)
                new = sum(pnl_net(r) for r in load_journal_records(pd_)
                          if r.get("outcome") in ("win", "loss"))
                net7 += round(new, 3) - old
            digest["trend"] = {
                "days": len(tail),
                "net_pnl_7d": round(net7, 2),
                "phantom_days_adjusted": phantom_days,
                "trades_7d": sum(h.get("trades") or 0 for h in tail),
                "gate_accuracy_trajectory": accs,
                "chronic_churners": {s: n for s, n in churn_counts.items() if n >= 3},
            }
    except Exception as e:
        digest["trend"] = {"error": str(e)[:200]}

    tmp = args.out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(digest, f, indent=1)
    os.replace(tmp, args.out)

    hist = {"date": day, "trades": digest["trades_closed"],
            "net_pnl": digest["fee_drag"]["net"],
            "dust_n": digest["dust"]["n"],
            "modeled_fee_usd": digest["fee_drag"]["modeled_fee_usd"],
            "gate_accuracy": (digest["gates"]["overall"] or {}).get("accuracy"),
            "trend_capture": (digest.get("trend_capture") or {}).get("verdict"),
            "churn_flags": [s for s, v in digest["expectancy"].items() if v["flag"]],
            "size_flag": digest["size_chain"]["flag"],
            "slippage_flags": {v: s["flag"] for v, s in digest.get("slippage", {}).items()
                               if isinstance(s, dict) and s.get("flag")}}
    # Dedup by date on write: a same-day rerun REPLACES the prior line for
    # that date instead of appending a duplicate (one-bad-line doctrine kept —
    # unparseable lines are preserved verbatim).
    try:
        kept = []
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH) as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        if json.loads(stripped).get("date") == day:
                            continue
                    except Exception:
                        pass
                    kept.append(stripped)
        kept.append(json.dumps(hist))
        htmp = HISTORY_PATH + ".tmp"
        with open(htmp, "w") as f:
            f.write("\n".join(kept) + "\n")
        os.replace(htmp, HISTORY_PATH)
    except Exception:
        # best-effort doctrine: never fail the digest over history bookkeeping
        with open(HISTORY_PATH, "a") as f:
            f.write(json.dumps(hist) + "\n")
    print(f"digest written: {args.out} ({digest['trades_closed']} trades, "
          f"net {digest['fee_drag']['net']:+.2f}, "
          f"dust {digest['dust']['n']}, "
          f"fee_sched ${digest['fee_drag']['modeled_fee_usd']:.2f})")


if __name__ == "__main__":
    main()
