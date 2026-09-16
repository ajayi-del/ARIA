"""Gate realistic fill — the offline realistic estimand for gate economics.

Merged-schema Phase 0 (Governor-approved 2026-09-13; CEO COMMISSION2 design,
local build). The D10 same-clock estimand (tools/gate_economics.py) marks each
refusal as: stopped -> the stop distance, else -> the +24h mark. That mark is
blind in two places:

  1. HORIZON — the firm's realized median hold is ~30 min (trade_db), not 24h.
     A "+4.5% missed winner" at 24h may be +0.3% at the firm's true exit
     horizon; 91.6% of stopped trades went green first (September census), so
     path-dependent exits change the verdict.
  2. FILLS — refusals are marked at the mid/mark. Real entries pay taker on
     thin books plus half-spread plus impact. The haircut is material at the
     firm's 30-min scale (~8bps RT fee alone on $80-750 notional).

This tool replays every shadow-scored refusal through the firm's OWN exit
policy on the post-refusal price path (free Bybit 1m tape) and reports the
D10 net beside the realistic net per gate. The gap IS the overstatement.

Exit-policy replay (first trigger wins; within one bar stop outranks TP —
fail-pessimistic, mirrors live intrabar ambiguity):
  1. software_stop   — path touches hyp_stop (the record's own hypothetical)
  2. software_tp     — path touches hyp_tp = RR_MIN x stop distance
                       (intelligence/personality.py rr_min floors FLOW 2.0 /
                       SCOUT 2.5 / APEX 2.0, median 2.0 — 38b2c5 ladder)
  3. conviction_decay — at GRACE_S, if underwater beyond BLEED_BAND
                       (flat 0.4% leg of max(0.4%, 0.5xATR15); ATR is not
                       journaled on shadow records — documented model gap)
  4. horizon         — hold-matched (empirical p25/p50/p75 from trade_db)
                       and the 24h arm

Fill haircut (SCHEDULE doctrine, DIR#53 — never pnl-diffs):
  fee/side = FEE_RT[venue]/2 (mirror of tools/daily_digest.py:104-112:
  SoDEX 0.04%x0.95 SOSO_STAKED, Aster crypto 0.04%, Aster tradfi 0.009%)
  half-spread = cs_spread/2 (median over window from logs/exec_formulas.jsonl)
  impact      = kyle_lambda x median symbol notional (trade_db, in-window)

Validation (the simulator may not grade counterfactuals until it grades itself):
  ORACLE-1  join fidelity — the D10 arm is computed via
            tools.gate_economics.policy_pnl on the same records and compared
            against logs/gate_economics_all.json (tolerance +-0.2/gate).
  ORACLE-2  backtest — the simulator replays ACTUAL trade_db fills (known
            entry/stop/tp1) and its exit-reason mix + exit prices are compared
            against the realized rows.
  TEST CASE — dispersion + quant_filter rows side-by-side (the digest-vs-
            gate_economics sign disagreement the CEO found grading #54).

Coverage doctrine: only symbols with a Bybit perp tape are scored
(data/bybit_feed.BYBIT_SYMBOL_MAP); tradfi/SSI/unknown abstain and are
COUNTED — a gate's net is only as honest as its coverage line.

Runs standalone (bot up or down), stdlib+httpx, exit 0 always (best-effort
doctrine, same as tools/gate_economics.py). Klines cache to logs/rf_cache/
per symbol-day so re-runs only fetch new tape.

    .venv/bin/python tools/gate_realistic_fill.py [--window 3d|7d|all]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(_ROOT, "logs")
SHADOW_PATH = os.path.join(LOG_DIR, "shadow_scored.jsonl")
TRADE_DB_PATH = os.path.join(LOG_DIR, "trade_db.jsonl")
EXEC_FORMULAS_PATH = os.path.join(LOG_DIR, "exec_formulas.jsonl")
GE_ALL_PATH = os.path.join(LOG_DIR, "gate_economics_all.json")
CACHE_DIR = os.path.join(LOG_DIR, "rf_cache")

BYBIT_KLINE = "https://api.bybit.com/v5/market/kline"
WINDOWS_S = {"3d": 3 * 86400, "7d": 7 * 86400, "all": None}

# ── Model constants (declared, never silently tuned) ─────────────────────────
FEE_RT = {"sodex": 0.00076, "aster": 0.0008, "aster_tradfi": 0.00018}
RR_MIN = 2.0            # personality rr_min median (FLOW 2.0 / SCOUT 2.5 / APEX 2.0)
GRACE_S = 1800.0        # conviction-review base grace (config: ×4 only when aligned)
BLEED_BAND = 0.004      # flat 0.4% leg of max(0.4%, 0.5xATR15) — ATR not journaled
HORIZON_S = 86400.0
# DIR|PHASE-1-HORIZON (CEO s38): the sim exit horizon MUST equal the window D10
# marks in (+24h). The old p50-of-admitted-book hold was an emergent survivor
# statistic — it truncated 45% of stops and killed the conviction_decay leg by
# construction, manufacturing gate acquittals (quant_filter +105.1 artifact).
SIM_HORIZON_S = 86400.0
MIN_N_FLAG = 30         # gate_economics evidence bar, mirrored
FETCH_SLEEP_S = 0.12    # Bybit public kline cadence (well under 10/s)
KLINE_LIMIT = 1000


# ── Pure estimand math (unit-tested, no I/O) ─────────────────────────────────

def fee_side(venue_class: str) -> float:
    """One-way taker fee fraction from the SCHEDULE (DIR#53: never pnl-diff)."""
    return FEE_RT.get(venue_class, FEE_RT["sodex"]) / 2.0


def entry_fill(mark: float, direction: str, half_spread: float,
               impact: float, fee: float) -> float:
    """Realistic entry: takers cross — longs pay up, shorts receive less."""
    h = half_spread + impact + fee
    return mark * (1.0 + h) if direction == "long" else mark * (1.0 - h)


def exit_fill(price: float, direction: str, half_spread: float,
              fee: float) -> float:
    """Realistic exit: closing the side crosses the same costs back."""
    h = half_spread + fee
    return price * (1.0 - h) if direction == "long" else price * (1.0 + h)


def make_tp(entry_raw: float, stop: float, direction: str,
            rr: float = RR_MIN) -> float:
    """Hypothetical TP1 at rr x stop distance (personality ladder median)."""
    d = abs(entry_raw - stop)
    return entry_raw + rr * d if direction == "long" else entry_raw - rr * d


def simulate_exit(path: list, ts: float, direction: str, entry_raw: float,
                  stop: float, tp: float, grace_s: float = GRACE_S,
                  bleed_band: float = BLEED_BAND,
                  horizon_s: float = HORIZON_S) -> dict:
    """Replay the firm's exit stack over ascending 1m bars (t, o, h, l, c).

    First trigger wins. Within one bar, stop outranks TP (fail-pessimistic).
    Returns reason + raw exit price + hold seconds; censored when the tape
    ends before any trigger (exit at last close, flagged).
    """
    for (t, _o, hi, lo, c) in path:
        if direction == "long":
            if lo <= stop:
                return {"reason": "software_stop", "price": stop, "hold_s": t - ts}
            if hi >= tp:
                return {"reason": "software_tp", "price": tp, "hold_s": t - ts}
        else:
            if hi >= stop:
                return {"reason": "software_stop", "price": stop, "hold_s": t - ts}
            if lo <= tp:
                return {"reason": "software_tp", "price": tp, "hold_s": t - ts}
        if t - ts >= grace_s:
            unr = (c / entry_raw - 1.0) * (1.0 if direction == "long" else -1.0)
            if unr <= -bleed_band:
                return {"reason": "conviction_decay", "price": c, "hold_s": t - ts}
        if t - ts >= horizon_s:
            return {"reason": "horizon", "price": c, "hold_s": t - ts}
    if path:
        return {"reason": "censored", "price": path[-1][4],
                "hold_s": path[-1][0] - ts}
    return {"reason": "no_tape", "price": entry_raw, "hold_s": 0.0}


def sim_pnl_pct(entry_eff: float, exit_eff: float, direction: str) -> float:
    r = exit_eff / entry_eff - 1.0
    return (r if direction == "long" else -r) * 100.0


def gate_net(pnls: list) -> float:
    """Gate value = -(what the refused trades would have made), pct-points.

    Same convention as gate_economics: saved losers count positive, missed
    winners count negative. net > 0 -> the gate earns."""
    return round(-sum(pnls), 1)


def quantile(xs: list, q: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    i = min(len(ys) - 1, max(0, int(round(q * (len(ys) - 1)))))
    return ys[i]


def tape_class(symbol: str, bybit_map: dict, tradfi: set) -> str:
    if "SSI" in symbol:
        return "skip"
    if symbol in bybit_map:
        return "bybit"
    if symbol in tradfi:
        return "tradfi"
    return "unknown"


# ── I/O helpers (best-effort, one-bad-line doctrine) ─────────────────────────

def _load_jsonl(path: str) -> list:
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return out


def _atomic_write(path: str, payload: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _venue_maps() -> tuple:
    """Lazy repo imports — module stays importable without pydantic/env."""
    try:
        from core.config import Settings
        from data.bybit_feed import BYBIT_SYMBOL_MAP
        from data.tradfi_feed import TRADFI_SYMBOLS
        cfg = Settings()
        return (set(getattr(cfg, "aster_assets", [])), dict(BYBIT_SYMBOL_MAP),
                set(TRADFI_SYMBOLS))
    except Exception:
        return (set(), {}, set())


def _exec_context(window_start: float) -> dict:
    """Median impact plane + median notional per symbol, in-window."""
    lambdas, spreads = defaultdict(list), defaultdict(list)
    for r in _load_jsonl(EXEC_FORMULAS_PATH):
        try:
            if float(r.get("ts") or 0) < window_start:
                continue
            sym = r.get("symbol") or ""
            kl, cs = r.get("kyle_lambda"), r.get("cs_spread")
            if kl is not None:
                lambdas[sym].append(float(kl))
            if cs is not None:
                spreads[sym].append(float(cs))
        except (TypeError, ValueError):
            continue
    notionals = defaultdict(list)
    holds = []
    for r in _load_jsonl(TRADE_DB_PATH):
        try:
            if float(r.get("timestamp_close_ms") or 0) < window_start * 1000.0:
                continue
            n = float(r.get("notional_usd") or 0.0)
            if n > 0:
                notionals[r.get("symbol") or ""].append(n)
            h = float(r.get("hold_seconds") or 0.0)
            if h > 0:
                holds.append(h)
        except (TypeError, ValueError):
            continue
    all_l = [v for xs in lambdas.values() for v in xs]
    all_s = [v for xs in spreads.values() for v in xs]
    all_n = [v for xs in notionals.values() for v in xs]
    return {
        "kyle": {s: median(v) for s, v in lambdas.items()},
        "cs": {s: median(v) for s, v in spreads.items()},
        "notional": {s: median(v) for s, v in notionals.items()},
        "kyle_default": median(all_l) if all_l else 0.0,
        "cs_default": median(all_s) if all_s else 0.0,
        "notional_default": median(all_n) if all_n else 100.0,
        "holds": holds,
    }


def _fetch_day(bybit_symbol: str, day_start_s: float) -> list:
    """One UTC day of 1m bars, ascending [(t,o,h,l,c)], cache read-through."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    stamp = datetime.fromtimestamp(day_start_s, timezone.utc).strftime("%Y%m%d")
    path = os.path.join(CACHE_DIR, f"{bybit_symbol}_{stamp}.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        pass
    bars = []
    try:
        import httpx
        start_ms = int(day_start_s * 1000)
        end_ms = int((day_start_s + 86400) * 1000) - 1
        cursor = start_ms
        with httpx.Client(timeout=10.0) as client:
            while cursor < end_ms:
                resp = client.get(BYBIT_KLINE, params={
                    "category": "linear", "symbol": bybit_symbol,
                    "interval": "1", "start": cursor, "end": end_ms,
                    "limit": KLINE_LIMIT})
                rows = ((resp.json() or {}).get("result") or {}).get("list") or []
                if not rows:
                    break
                got = []
                for r in rows:
                    try:
                        got.append([int(r[0]) / 1000.0, float(r[1]), float(r[2]),
                                    float(r[3]), float(r[4])])
                    except (TypeError, ValueError, IndexError):
                        continue
                got.sort(key=lambda b: b[0])
                bars.extend(b for b in got if b[0] * 1000.0 >= cursor)
                if len(got) < 2:
                    break
                cursor = int(got[-1][0] * 1000.0) + 60_000
                time.sleep(FETCH_SLEEP_S)
    except Exception:
        return []
    if bars:
        try:
            _atomic_write(path, json.dumps(bars))
        except Exception:
            pass
    return bars


def _path_for(bybit_symbol: str, ts: float, end_s: float) -> list:
    """Ascending bars covering [ts, end_s] across ≤2 day-files."""
    day0 = int(ts // 86400) * 86400.0
    bars = _fetch_day(bybit_symbol, day0)
    if end_s >= day0 + 86400:
        bars = bars + _fetch_day(bybit_symbol, day0 + 86400)
    return [b for b in bars if ts <= b[0] <= end_s]


# ── Arms ──────────────────────────────────────────────────────────────────────

def d10_arm(records: list) -> dict:
    """ORACLE-1 arm: per-gate D10 nets via gate_economics' own policy_pnl."""
    from tools.gate_economics import policy_pnl
    pnls = defaultdict(list)
    for r in records:
        p = policy_pnl(r)
        if p is not None:
            pnls[r.get("gate") or "unknown"].append(p)
    return {g: gate_net(xs) for g, xs in pnls.items()}


def sim_arm(records: list, ctx: dict, aster: set, bybit_map: dict,
            tradfi: set, horizon_s: float) -> tuple:
    """Realistic arm: per-gate simulated nets + coverage + per-record detail."""
    pnls = defaultdict(list)
    coverage = defaultdict(lambda: [0, 0])   # gate -> [scored, abstained]
    details = []
    for r in records:
        g = r.get("gate") or "unknown"
        sym = r.get("symbol") or ""
        direction = r.get("direction") or "long"
        cls = tape_class(sym, bybit_map, tradfi)
        if cls != "bybit":
            coverage[g][1] += 1
            continue
        try:
            ts = float(r.get("ts") or 0.0)
            entry_raw = float(r.get("entry") or 0.0)
            stop = float(r.get("hyp_stop") or 0.0)
        except (TypeError, ValueError):
            coverage[g][1] += 1
            continue
        if ts <= 0 or entry_raw <= 0 or stop <= 0:
            coverage[g][1] += 1
            continue
        venue = "aster" if sym in aster else "sodex"
        fee = fee_side(venue)
        cs = ctx["cs"].get(sym, ctx["cs_default"])
        kl = ctx["kyle"].get(sym, ctx["kyle_default"])
        notional = ctx["notional"].get(sym, ctx["notional_default"])
        half_spread = max(cs, 0.0) / 2.0
        impact = max(kl, 0.0) * notional
        path = _path_for(bybit_map[sym], ts, ts + HORIZON_S)
        tp = make_tp(entry_raw, stop, direction)
        sim = simulate_exit(path, ts, direction, entry_raw, stop, tp,
                            horizon_s=horizon_s)
        if sim["reason"] == "no_tape":
            coverage[g][1] += 1
            continue
        e_in = entry_fill(entry_raw, direction, half_spread, impact, fee)
        e_out = exit_fill(sim["price"], direction, half_spread, fee)
        p = sim_pnl_pct(e_in, e_out, direction)
        pnls[g].append(p)
        coverage[g][0] += 1
        details.append({"id": r.get("id"), "gate": g, "symbol": sym,
                        "direction": direction, "ts": ts,
                        "reason": r.get("reason"),      # DIR|RECORD-DETAIL: the
                        "session": r.get("session"),    # census must be reproducible
                        "sim_reason": sim["reason"], "sim_hold_s": round(sim["hold_s"]),
                        "sim_pnl_pct": round(p, 4),
                        "haircut_bps": round((half_spread + impact + fee) * 1e4, 2)})
    nets = {g: gate_net(xs) for g, xs in pnls.items()}
    return nets, coverage, details


# ── Validation legs ───────────────────────────────────────────────────────────

# DIR|CONVICTION-DECAY-PREFIX (CEO A5): production exit_reason is PREFIXED —
# "conviction_decay:signal_abandoned" / "conviction_decay:signal_absent". An
# exact-string map orphaned 100/849 reference rows (11.8%) into "other".
REASON_MAP = {"software_stop": "software_stop", "software_tp": "software_tp",
              "conviction_decay": "conviction_decay"}


def reason_bucket(actual: str) -> str:
    """Map a production exit_reason onto the sim's exit vocabulary.

    Exact match first (REASON_MAP); the conviction_decay leg is a PREFIX
    match (conviction_decay:<subreason>); everything else is "other".
    """
    b = REASON_MAP.get(actual)
    if b is not None:
        return b
    if actual.startswith("conviction_decay:"):  # colon-bounded: bare form is in REASON_MAP; "conviction_decayed" must not misroute
        return "conviction_decay"
    return "other"


def fidelity_vs_gate_economics(my_nets: dict) -> dict:
    """ORACLE-1: my D10 arm vs the production gate_economics_all.json."""
    try:
        with open(GE_ALL_PATH) as f:
            ge = json.load(f)
    except Exception as e:
        return {"status": "skipped", "reason": f"gate_economics_all unreadable: {e}"}
    rows = {}
    worst = 0.0
    for row in ge.get("gates") or []:
        g = row.get("gate")
        theirs = row.get("net_value_pct")
        mine = my_nets.get(g)
        if theirs is None or mine is None:
            continue
        diff = abs(round(mine - theirs, 2))
        worst = max(worst, diff)
        rows[g] = {"theirs": theirs, "mine": mine, "abs_diff": diff}
    return {"status": "pass" if worst <= 0.2 else "fail",
            "max_abs_diff": round(worst, 2), "tolerance": 0.2, "gates": rows}


def backtest_actual_fills(ctx: dict, aster: set, bybit_map: dict,
                          tradfi: set, window_start: float,
                          horizon_s: float) -> dict:
    """ORACLE-2: replay real fills; exit-reason mix + exit-price realism."""
    n = matched = 0
    n_covered = matched_covered = 0
    price_err_bps = []
    reason_mix = defaultdict(lambda: [0, 0])   # actual -> [n, sim_matched]
    for r in _load_jsonl(TRADE_DB_PATH):
        try:
            t0 = float(r.get("timestamp_open_ms") or 0) / 1000.0
            if t0 < window_start:
                continue
            sym = r.get("symbol") or ""
            if tape_class(sym, bybit_map, tradfi) != "bybit":
                continue
            entry = float(r.get("entry_price") or 0.0)
            stop = float(r.get("stop_price") or 0.0)
            tp = float(r.get("tp1_price") or 0.0)
            exit_px = float(r.get("exit_price") or 0.0)
            side = "long" if (r.get("side") or "").lower() == "long" else "short"
            if min(entry, stop, exit_px) <= 0:
                continue
            if tp <= 0:
                tp = make_tp(entry, stop, side)
        except (TypeError, ValueError):
            continue
        path = _path_for(bybit_map[sym], t0, t0 + HORIZON_S)
        if not path:
            continue
        sim = simulate_exit(path, t0, side, entry, stop, tp,
                            horizon_s=horizon_s)
        n += 1
        actual_reason = r.get("exit_reason") or "other"
        bucket = reason_bucket(actual_reason)
        covered = bucket != "other"   # DIR|ORACLE-COVERAGE: the actual reason
        if covered:                   # is one the sim can emit
            n_covered += 1
        reason_mix[bucket][0] += 1
        if sim["reason"] == bucket or (bucket == "other"
                                       and sim["reason"] in ("horizon", "censored")):
            matched += 1
            reason_mix[bucket][1] += 1
            if covered:
                matched_covered += 1
        if sim["price"] > 0:
            price_err_bps.append(abs(sim["price"] / exit_px - 1.0) * 1e4)
    return {"n": n, "reason_match_rate": round(matched / n, 3) if n else None,
            "n_covered": n_covered,
            "reason_coverage": round(n_covered / n, 3) if n else None,
            "reason_match_rate_on_covered":
                round(matched_covered / n_covered, 3) if n_covered else None,
            "median_exit_price_err_bps": round(median(price_err_bps), 2)
            if price_err_bps else None,
            "mix": {k: {"n": v[0], "sim_matched": v[1]}
                    for k, v in sorted(reason_mix.items())}}


# DIR|ORACLE-3-PNL-FIDELITY (CEO A4): dust floor — the exchange min-notional.
# Sub-floor rows are bookkeeping remnants, not trades the sim could have made.
DUST_NOTIONAL_FLOOR = 10.0
ORACLE3_CAVEAT = "validated on ADMITTED, applied to REFUSED"


def pnl_fidelity_actual_fills(ctx: dict, aster: set, bybit_map: dict,
                              tradfi: set, window_start: float,
                              horizon_s: float) -> dict:
    """ORACLE-3: P&L fidelity of the sim on the ADMITTED book.

    Same population as ORACLE-2 (reference records that actually filled and
    closed), dust stripped (notional >= $10). Per row: sim P&L in pct-points
    under the SAME realistic-fill doctrine the sim arm grades refusals with
    (schedule taker fee + cs_spread/2 + kyle_lambda x notional on entry and
    exit) vs realized net_pnl / notional x 100. The aggregate adjudicates
    whether the sim's P&L — validated here on ADMITTED rows — may be
    trusted when applied to REFUSED counterfactuals.
    """
    n = agree = 0
    sim_sum = real_sum = 0.0
    per_class = defaultdict(lambda: [0, 0.0, 0.0])  # cls -> [n, sim, realized]
    for r in _load_jsonl(TRADE_DB_PATH):
        try:
            t0 = float(r.get("timestamp_open_ms") or 0) / 1000.0
            if t0 < window_start:
                continue
            sym = r.get("symbol") or ""
            if tape_class(sym, bybit_map, tradfi) != "bybit":
                continue
            notional = float(r.get("notional_usd") or 0.0)
            if notional < DUST_NOTIONAL_FLOOR:
                continue
            entry = float(r.get("entry_price") or 0.0)
            stop = float(r.get("stop_price") or 0.0)
            tp = float(r.get("tp1_price") or 0.0)
            exit_px = float(r.get("exit_price") or 0.0)
            side = "long" if (r.get("side") or "").lower() == "long" else "short"
            if min(entry, stop, exit_px) <= 0:
                continue
            if tp <= 0:
                tp = make_tp(entry, stop, side)
            net = r.get("net_pnl")
            if net is None:
                net = r.get("directional_pnl")
            if net is None:
                continue
            real_pct = float(net) / notional * 100.0
        except (TypeError, ValueError):
            continue
        path = _path_for(bybit_map[sym], t0, t0 + HORIZON_S)
        if not path:
            continue
        sim = simulate_exit(path, t0, side, entry, stop, tp,
                            horizon_s=horizon_s)
        if sim["price"] <= 0:
            continue
        venue = "aster" if sym in aster else "sodex"
        fee = fee_side(venue)
        half_spread = max(ctx["cs"].get(sym, ctx["cs_default"]), 0.0) / 2.0
        impact = (max(ctx["kyle"].get(sym, ctx["kyle_default"]), 0.0)
                  * ctx["notional"].get(sym, ctx["notional_default"]))
        e_in = entry_fill(entry, side, half_spread, impact, fee)
        e_out = exit_fill(sim["price"], side, half_spread, fee)
        sim_pct = sim_pnl_pct(e_in, e_out, side)
        n += 1
        sim_sum += sim_pct
        real_sum += real_pct
        if (sim_pct == 0.0 and real_pct == 0.0) or sim_pct * real_pct > 0:
            agree += 1
        raw_reason = r.get("exit_reason") or "other"
        cls = ("conviction_decay" if raw_reason.startswith("conviction_decay")
               else raw_reason)
        per_class[cls][0] += 1
        per_class[cls][1] += sim_pct
        per_class[cls][2] += real_pct
    bias = sim_sum - real_sum
    return {"n": n,
            "sim_pnl_total_pct": round(sim_sum, 1),
            "realized_pnl_total_pct": round(real_sum, 1),
            "bias_pct_points": round(bias, 1),
            "bias_bp_per_row": round(bias / n * 100.0, 2) if n else None,
            "sign_agreement": round(agree / n, 3) if n else None,
            "per_class": {c: {"n": v[0], "sim": round(v[1], 1),
                              "realized": round(v[2], 1),
                              "bias": round(v[1] - v[2], 1)}
                          for c, v in sorted(per_class.items())},
            "dust_floor_notional": DUST_NOTIONAL_FLOOR,
            "caveat": ORACLE3_CAVEAT}


# ── Report ────────────────────────────────────────────────────────────────────

def ascii_table(rows: list, window: str, horizon_label: str) -> str:
    lines = [f"Gate realistic fill — {window} (sim arm: firm's exit stack on "
             f"Bybit tape, horizon {horizon_label}, schedule fees + impact)",
             f"{'Gate':<22}{'n':>6}{'abst':>6}{'D10net':>9}{'simNet':>9}"
             f"{'gap':>8}{'flip':>6}"]
    for r in rows:
        flip = "FLIP" if r["sign_flip"] else ""
        lines.append(f"{r['gate']:<22}{r['n_scored']:>6}{r['n_abstained']:>6}"
                     f"{('+' if r['d10_net'] >= 0 else '') + str(r['d10_net']):>9}"
                     f"{('+' if r['sim_net'] >= 0 else '') + str(r['sim_net']):>9}"
                     f"{('+' if r['gap'] >= 0 else '') + str(r['gap']):>8}{flip:>6}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", choices=["3d", "7d", "all"], default=None,
                    help="single window; default emits 7d + all")
    args = ap.parse_args()
    windows = [args.window] if args.window else ["7d", "all"]

    recs_all = _load_jsonl(SHADOW_PATH)
    aster, bybit_map, tradfi = _venue_maps()
    now = time.time()

    for w in windows:
        try:
            horizon = WINDOWS_S[w]
            window_start = 0.0 if horizon is None else now - horizon
            subset = [r for r in recs_all
                      if horizon is None or float(r.get("ts") or 0) >= window_start]
            ctx = _exec_context(window_start)
            holds = ctx["holds"]
            hold_q = {"p25": quantile(holds, 0.25), "p50": quantile(holds, 0.50),
                      "p75": quantile(holds, 0.75)}
            sim_horizon = SIM_HORIZON_S  # DIR|PHASE-1-HORIZON: 24h, never p50

            d10_nets = d10_arm(subset)
            sim_nets, coverage, details = sim_arm(
                subset, ctx, aster, bybit_map, tradfi, sim_horizon)

            gates = sorted(set(d10_nets) | set(sim_nets),
                           key=lambda g: -(coverage.get(g, [0, 0])[0]))
            rows = []
            for g in gates:
                d10 = d10_nets.get(g, 0.0)
                sim = sim_nets.get(g, 0.0)
                rows.append({"gate": g,
                             "n_scored": coverage.get(g, [0, 0])[0],
                             "n_abstained": coverage.get(g, [0, 0])[1],
                             "d10_net": d10, "sim_net": sim,
                             "gap": round(d10 - sim, 1),
                             "sign_flip": (d10 < 0) != (sim < 0)})

            fidelity = fidelity_vs_gate_economics(d10_nets) if w == "all" \
                else {"status": "skipped", "reason": "oracle-1 runs on all"}
            backtest = backtest_actual_fills(ctx, aster, bybit_map, tradfi,
                                             window_start, sim_horizon)
            pnl_fid = pnl_fidelity_actual_fills(ctx, aster, bybit_map, tradfi,
                                                window_start, sim_horizon)
            test_case = [r for r in rows
                         if r["gate"] in ("dispersion", "quant_filter")]

            payload = {
                "window": w,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "declaration": {
                    "estimand_d10": "stopped -> stop distance %; else +24h mark % (D10/#24)",
                    "estimand_sim": "firm exit stack (stop > tp > conviction_decay > "
                                    "horizon) on Bybit 1m tape; entry/exit at schedule "
                                    "taker fee + cs_spread/2 + kyle_lambda x notional",
                    "constants": {"FEE_RT": FEE_RT, "RR_MIN": RR_MIN,
                                  "GRACE_S": GRACE_S, "BLEED_BAND": BLEED_BAND,
                                  "SIM_HORIZON_S": SIM_HORIZON_S},
                    "hold_quantiles_s": hold_q,
                    "sim_horizon_s": sim_horizon,
                    "staleness_contract_s": 23400,  # cron 4x/day; >6.5h = stale
                    "sign_convention": "gate net = -(sum of refused-trade pnl); >0 earns",
                },
                "n_records": len(subset),
                "gates": rows,
                "oracle1_d10_fidelity": fidelity,
                "oracle2_fill_backtest": backtest,
                "oracle3_pnl_fidelity": pnl_fid,
                "test_case_sign_disagreement": test_case,
                "record_detail": details[:20000],  # was 2000/8651 — full census
            }
            # 24h-horizon doctrine artifact; the p50-horizon 09-14 files stay
            # under the old name as the audit trail (CEO s38).
            _atomic_write(os.path.join(LOG_DIR, f"gate_realistic_fill_24h_{w}.json"),
                          json.dumps(payload, indent=1))
            print(ascii_table(rows, w, f"horizon={int(sim_horizon)}s"))
            print(f"  oracle-1 (D10 fidelity vs gate_economics_all): "
                  f"{fidelity.get('status')} max|diff|={fidelity.get('max_abs_diff')}")
            print(f"  oracle-2 (fill backtest): n={backtest.get('n')} "
                  f"reason_match={backtest.get('reason_match_rate')} "
                  f"coverage={backtest.get('reason_coverage')} "
                  f"match_on_covered="
                  f"{backtest.get('reason_match_rate_on_covered')} "
                  f"median_px_err_bps={backtest.get('median_exit_price_err_bps')}")
            print(f"  oracle-3 (P&L fidelity, ADMITTED book, dust "
                  f">=${pnl_fid.get('dust_floor_notional'):.0f} stripped): "
                  f"n={pnl_fid.get('n')} "
                  f"sim={pnl_fid.get('sim_pnl_total_pct')}pt "
                  f"realized={pnl_fid.get('realized_pnl_total_pct')}pt "
                  f"bias={pnl_fid.get('bias_pct_points')}pt "
                  f"({pnl_fid.get('bias_bp_per_row')}bp/row) "
                  f"sign_agree={pnl_fid.get('sign_agreement')} "
                  f"[{pnl_fid.get('caveat')}]")
            for cls, v in (pnl_fid.get("per_class") or {}).items():
                print(f"    {cls}: n={v['n']} sim={v['sim']} "
                      f"realized={v['realized']} bias={v['bias']}")
            for r in test_case:
                print(f"  TEST CASE {r['gate']}: D10 {r['d10_net']} vs sim "
                      f"{r['sim_net']} (n={r['n_scored']}, flip={r['sign_flip']})")
            print()
        except Exception as e:  # best-effort: one window's failure never blanks the rest
            print(f"gate_realistic_fill[{w}] error: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
