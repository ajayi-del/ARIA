"""
intelligence/lancer.py — LANCER trade-class SHADOW GATE (2026-10-04).

Governor-approved, SHADOW-ONLY build (spec: memory
project_20261004_lancer_spec.md). LANCER is a measurement instrument, not a
trading path: it answers the counterfactual "what would a wide-stop /
no-TP1 / trail-only / high-leverage doctrine have captured on our best
signal classes?" The September census shows 91.6% of stopped trades went
green first — entries find edge, exits (TP1 crush + tight stops) amputate
the right tail. LANCER inverts the geometry and measures the result.

ZERO live trading. ZERO changes to live exits. ZERO capital paths. The
live envelope knobs (lancer_enabled, sleeve margin, concurrency) are
pre-locked here but DARK (Amendment 3): lancer_enabled=False from birth,
Governor-gated at n=50 (EV > +0.15R, CI excl 0).

Department shape (docs/DEPARTMENT_TEMPLATE.md):
  - Zero-I/O brain (LancerShadow): all market reads (price, ATR15 fraction,
    venue, coherence components, time) arrive as arguments; decisions leave
    as verdict dicts + telemetry events. The main.py splice owns all I/O:
    the shadow_journal would-block record, the jsonl append, the logging.
  - Kill switch: lancer_shadow_enabled=False reproduces the pre-module
    system bit-for-bit — the splice skips every call and the brain itself
    fails closed to a disabled verdict with zero state mutation.
  - One-bad-line doctrine on the writer: a serialization failure costs
    that row, never the file, never the trade path.

Candidate detection (measure the SIGNAL, not the fill):
  INCLUDE  rotation family  — coherence component rotation_laggard > 0
                              (intelligence/coherence.py:323) or the Tier-1
                              SSI institutional component > 0 at the same
                              point (coherence.py:150).
  INCLUDE  QNT-class structural — symbol in the LEVERAGE_CAPS 10x+ tier
                              (intelligence/fast_cycle_engine.py:72-101)
                              with structural engine provenance (Kant
                              structure tier component > 0).
  EXCLUDE  cascade class    — census verdict: score>=4 never precedes
                              events, coin flip. Hard-excluded on APEX/
                              AFTERMATH personality or cascade_zscore > 0.
                              (The gated-path hook point never sees the
                              cascade fast paths at all; this is the
                              defense-in-depth leg.)
  DEDUP    one open shadow per symbol; a second candidate for the same
           symbol while open is skipped_duplicate.

Shadow position params:
  entry    = signal price. SLIPPAGE ASSUMPTION: NONE — the shadow fills at
             the signal mark, so shadow results OVERSTATE fills versus any
             live path (flagged per spec; the graduation bar at n=50 must
             clear EV with this bias in mind).
  leverage = venue argmax over the spec's venue table (choose_venue):
             SoDEX per LEVERAGE_CAPS (ETH 40 / BTC 38 / XAUT-USTECH100 25 /
             20x tier / mid 10, unmapped SoDEX symbols default mid 10),
             Aster 15 (post-uplift). Venue+leverage journaled on every open.
  stop     = max(lancer_stop_atr_mult x ATR15/mark, lancer_stop_floor_pct)
             of entry. The ATR15 fraction is INJECTED — computed by the
             splice via the existing accessor
             intelligence/conviction_review.atr_pct_from_candles on the
             15m buffer (same read as main.py:5095); this module never
             recomputes its own ATR. Dark ATR -> the floor binds
             (fail-closed wide), mirroring _latefill_stop_geometry
             (main.py:27660).
  NO TP1.  Exit = trail-only, reusing the EXISTING trailing-stop ratchet
             semantics of main.py:16345-16380 (activation after
             trail_activation_atr x ATR favorable, trail at
             trail_distance_atr x ATR behind best, ratchet-only, never
             backward — same config knobs the live loop reads). Mirrored
             BY CONSTRUCTION below with the source cited; the live loop is
             an inline main.py closure and cannot be imported by a
             department brain (template rule: intelligence/ imports nothing
             from main.py). ATR-dark positions use the live loop's own
             synthetic fallback (1.0% of entry, main.py:16308).
  timeout  = flat-close at lancer_timeout_h (default 72h).

Excursion journaling (Amendment 1 — the load-bearing feature): every open
shadow updates MFE/MAE in R multiples of the stop distance AND in % on
every tick. On close the splice writes one dense line to
logs/lancer_shadow.jsonl plus a gate="lancer" record in the shadow
journal idiom (record_would_block with the REAL shadow stop) so the
standard census tooling scores the cohort.

Config knobs (core/config.py, injected cfg via getattr):
  lancer_shadow_enabled        True   — the measurement; safe from birth
  lancer_enabled               False  — LIVE path; dark, Governor-gated n=50
  lancer_sleeve_max_margin_pct 0.05   — pre-locked live envelope (dark)
  lancer_max_concurrent        1      — live envelope AND shadow admission
  lancer_max_per_day           3      — live envelope AND shadow admission
  lancer_no_reload_same_day    True   — no same-day reload after stop-out
  lancer_stop_atr_mult         1.0
  lancer_stop_floor_pct        0.04
  lancer_timeout_h             72.0
"""
from __future__ import annotations

import json
import time
from typing import Dict, List, Optional, Tuple

from intelligence.fast_cycle_engine import LEVERAGE_CAPS

SCHEMA_VERSION = 1
LANCER_JOURNAL_PATH = "logs/lancer_shadow.jsonl"

# Venue table (spec 2026-10-04): Aster post-uplift flat cap. SoDEX per
# LEVERAGE_CAPS; SoDEX symbols absent from the table default to the mid-cap
# 10x tier (documented simplification — LEVERAGE_CAPS is the exchange-tier
# source of truth for mapped symbols).
ASTER_LEVERAGE: int = 15
SODEX_DEFAULT_LEVERAGE: int = 10

# Structural-class leverage floor: the QNT-class is the 10x+ tier.
STRUCTURAL_MIN_CAP: int = 10

# Cascade-class exclusion: census verdict (project_cascade_census_verdict) —
# score>=4 never precedes events; squeeze-long = coin flip. Hard-excluded.
_CASCADE_PERSONALITIES = frozenset({"APEX", "AFTERMATH"})

# Live-loop synthetic ATR fallback (main.py:16308): 1.0% of price when the
# measured ATR is dark. Mirrored so the trail ruler matches the machinery
# it shadows.
_SYNTHETIC_ATR_FRAC = 0.010

EXIT_CLASSES = frozenset({"trail", "stop", "timeout"})


def _f(x) -> Optional[float]:
    """Coerce to float; None on any garbage (fail-closed idiom)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def base_name(symbol: str) -> str:
    """BTC-USD / btc / BTC-USD.P → BTC (venue suffixes stripped)."""
    try:
        s = str(symbol)
    except Exception:
        return ""
    if not s or s == "None":
        return ""
    return s.split("-")[0].split(".")[0].strip().upper()


def choose_venue(symbol: str, routed_venue: str) -> Tuple[str, int, dict]:
    """Venue argmax (leverage_cap x liquidity_class)/fee per the spec's
    venue table.

    The table already folds liquidity class and fee into its numbers
    (SoDEX majors carry the staking-discounted deep-book tiers; Aster-only
    alts carry the post-uplift 15x), so the argmax reduces to max leverage
    over the venues where the symbol can actually trade:
      - SoDEX is a candidate venue for every signal symbol (routed or
        SoDEX-tradable); leverage = LEVERAGE_CAPS[base] else the mid-10
        default.
      - Aster is a candidate venue only when the symbol is Aster-routed
        (Aster-only alts are not SoDEX-tradable; SoDEX-routed symbols
        trade SoDEX per the 2026-09-21 venue doctrine).
    Ties break to the routed venue. Returns (venue, leverage, table)."""
    base = base_name(symbol)
    routed = str(routed_venue or "sodex").strip().lower() or "sodex"
    table: Dict[str, int] = {"sodex": int(LEVERAGE_CAPS.get(
        base, SODEX_DEFAULT_LEVERAGE))}
    if routed == "aster":
        table["aster"] = ASTER_LEVERAGE
    best = routed if routed in table else "sodex"
    for v, lev in table.items():
        if lev > table[best]:
            best = v
    return best, int(table[best]), dict(table)


def classify_signal(symbol: str, components: Optional[dict],
                    personality: str = "",
                    cascade_zscore: float = 0.0) -> Tuple[str, str]:
    """(signal_class, exclude_reason).

    signal_class ∈ {"rotation", "structural", "excluded"}; exclude_reason
    names the exclusion when excluded ("" otherwise). Pure predicates over
    injected values."""
    if str(personality or "").upper() in _CASCADE_PERSONALITIES:
        return "excluded", "cascade_class"
    try:
        if float(cascade_zscore or 0.0) > 0.0:
            return "excluded", "cascade_class"
    except (TypeError, ValueError):
        pass
    comps = components if isinstance(components, dict) else {}
    rot = _f(comps.get("rotation_laggard")) or 0.0
    ssi = _f(comps.get("institutional")) or 0.0
    if rot > 0.0 or ssi > 0.0:
        return "rotation", ""
    cap = LEVERAGE_CAPS.get(base_name(symbol))
    struct = _f(comps.get("structure")) or 0.0
    if cap is not None and int(cap) >= STRUCTURAL_MIN_CAP and struct > 0.0:
        return "structural", ""
    return "excluded", "not_lancer_class"


def stop_geometry(side: str, entry: float, atr_pct: Optional[float],
                  atr_mult: float, floor_pct: float) -> Tuple[float, float]:
    """(stop_price, stop_dist_frac) — max(atr_mult x ATR15/mark, floor).

    Mirror of _latefill_stop_geometry (main.py:27660) semantics: dark ATR
    → the floor binds (fail-closed wide). Returns (0.0, 0.0) on degenerate
    input — the caller refuses the candidate (fail-closed)."""
    entry_f = _f(entry)
    if entry_f is None or entry_f <= 0 or side not in ("long", "short"):
        return 0.0, 0.0
    dist = max(0.0, _f(floor_pct) or 0.0)
    atr_f = _f(atr_pct)
    if atr_f is not None and atr_f > 0:
        dist = max(dist, (_f(atr_mult) or 1.0) * atr_f)
    if dist <= 0:
        return 0.0, 0.0
    if side == "long":
        return entry_f * (1.0 - dist), dist
    return entry_f * (1.0 + dist), dist


class LancerShadow:
    """The shadow book: open shadow positions, admission counters, and the
    trail/timeout machinery. In-memory only; the splice owns persistence.
    Time is injected (now_ts) — the brain never reads the clock."""

    def __init__(self) -> None:
        self._open: Dict[str, dict] = {}          # symbol → shadow position
        self._day_key: int = -1                   # UTC tm_yday of counters
        self._day_count: int = 0                  # admissions today
        self._stopped_day: Dict[str, int] = {}    # symbol → yday of last stop-out

    # ── introspection (the splice's register/deregister surface) ─────────

    def has_open(self) -> bool:
        return bool(self._open)

    def open_symbols(self) -> List[str]:
        return list(self._open.keys())

    # ── admission ─────────────────────────────────────────────────────────

    def _roll_day(self, now_ts: float) -> int:
        try:
            yday = time.gmtime(float(now_ts)).tm_yday
        except Exception:
            yday = -1
        if yday != self._day_key:
            self._day_key = yday
            self._day_count = 0
            self._stopped_day = {s: d for s, d in self._stopped_day.items()
                                 if d == yday}
        return yday

    def evaluate_candidate(self, *, cfg, symbol: str, direction: str,
                           entry_price, atr_pct, venue: str, now_ts: float,
                           components: Optional[dict] = None,
                           personality: str = "",
                           cascade_zscore: float = 0.0) -> dict:
        """Full would-be verdict for one signal-stream candidate.

        Returns {"action": "opened"|"skipped", "reason": str,
                 "events": [(event, kwargs)], "opened": record|None}.
        Never raises: any internal fault → skipped/internal_error with zero
        state mutation (the hot path must never feel this module)."""
        try:
            if not bool(getattr(cfg, "lancer_shadow_enabled", True)):
                return {"action": "skipped", "reason": "disabled",
                        "events": [], "opened": None}
            now = _f(now_ts)
            if now is None or now <= 0:
                return {"action": "skipped", "reason": "bad_time",
                        "events": [], "opened": None}
            yday = self._roll_day(now)

            if direction not in ("long", "short"):
                return {"action": "skipped", "reason": "no_direction",
                        "events": [], "opened": None}

            sclass, excl = classify_signal(symbol, components,
                                           personality, cascade_zscore)
            if sclass == "excluded":
                return {"action": "skipped", "reason": excl,
                        "events": [("lancer_candidate_skipped",
                                    {"symbol": symbol, "reason": excl})],
                        "opened": None}

            if symbol in self._open:
                return {"action": "skipped", "reason": "skipped_duplicate",
                        "events": [("lancer_candidate_skipped",
                                    {"symbol": symbol,
                                     "reason": "skipped_duplicate"})],
                        "opened": None}

            # Live-envelope admission caps (Amendment 3): the shadow cohort
            # must match what the live path could actually take.
            max_conc = int(getattr(cfg, "lancer_max_concurrent", 1) or 1)
            if len(self._open) >= max(1, max_conc):
                return {"action": "skipped", "reason": "skipped_concurrency",
                        "events": [("lancer_candidate_skipped",
                                    {"symbol": symbol,
                                     "reason": "skipped_concurrency",
                                     "open": len(self._open)})],
                        "opened": None}
            max_day = int(getattr(cfg, "lancer_max_per_day", 3) or 3)
            if self._day_count >= max(1, max_day):
                return {"action": "skipped", "reason": "skipped_daily_cap",
                        "events": [("lancer_candidate_skipped",
                                    {"symbol": symbol,
                                     "reason": "skipped_daily_cap",
                                     "day_count": self._day_count})],
                        "opened": None}
            if (bool(getattr(cfg, "lancer_no_reload_same_day", True))
                    and self._stopped_day.get(symbol) == yday):
                return {"action": "skipped", "reason": "skipped_no_reload",
                        "events": [("lancer_candidate_skipped",
                                    {"symbol": symbol,
                                     "reason": "skipped_no_reload"})],
                        "opened": None}

            entry = _f(entry_price)
            if entry is None or entry <= 0:
                return {"action": "skipped", "reason": "bad_geometry",
                        "events": [], "opened": None}

            v_name, lev, v_table = choose_venue(symbol, venue)
            stop, dist = stop_geometry(
                direction, entry, atr_pct,
                float(getattr(cfg, "lancer_stop_atr_mult", 1.0) or 1.0),
                float(getattr(cfg, "lancer_stop_floor_pct", 0.04) or 0.04))
            if stop <= 0 or dist <= 0:
                return {"action": "skipped", "reason": "bad_geometry",
                        "events": [], "opened": None}

            # Trail ruler — same knobs the live loop reads (main.py:16279).
            atr_f = _f(atr_pct)
            atr_abs = (atr_f * entry) if (atr_f is not None and atr_f > 0) \
                else entry * _SYNTHETIC_ATR_FRAC
            atr_source = "atr15" if (atr_f is not None and atr_f > 0) \
                else "fallback_1pct"
            act_atr = float(getattr(cfg, "trail_activation_atr", 2.0) or 2.0)
            dist_atr = float(getattr(cfg, "trail_distance_atr", 1.0) or 1.0)

            rec = {
                "schema": SCHEMA_VERSION,
                "id": f"lancer_{int(now)}_{symbol}_{direction}",
                "symbol": symbol,
                "side": direction,
                "signal_class": sclass,
                "venue": v_name,
                "leverage": lev,
                "venue_table": v_table,
                "entry": entry,
                "stop": stop,
                "stop_dist_pct": round(dist, 6),
                "atr_pct": round(atr_f, 6) if atr_f is not None else None,
                "atr_abs": atr_abs,
                "atr_source": atr_source,
                "trail_act_atr": act_atr,
                "trail_dist_atr": dist_atr,
                "timeout_h": float(getattr(cfg, "lancer_timeout_h", 72.0)
                                   or 72.0),
                "opened_ts": now,
                "best_price": entry,
                "current_stop": stop,
                "trail_active": False,
                "mfe_pct": 0.0,
                "mae_pct": 0.0,
                "mfe_R": 0.0,
                "mae_R": 0.0,
            }
            self._open[symbol] = rec
            self._day_count += 1
            return {
                "action": "opened", "reason": "",
                "events": [("lancer_candidate_opened", {
                    "symbol": symbol, "side": direction,
                    "signal_class": sclass, "venue": v_name,
                    "leverage": lev, "entry": round(entry, 6),
                    "stop": round(stop, 6),
                    "stop_dist_pct": round(dist * 100.0, 3),
                    "atr_source": atr_source})],
                "opened": rec,
            }
        except Exception:
            # Fail-closed: the shadow must NEVER break the hot path.
            return {"action": "skipped", "reason": "internal_error",
                    "events": [], "opened": None}

    # ── tick machinery ─────────────────────────────────────────────────────

    def on_tick(self, symbol: str, price, ts) -> List[dict]:
        """Advance the open shadow on `symbol` one tick. Returns the close
        records finalized this tick ([] when nothing closed). Never raises;
        a tick fault costs the tick, never the position, never the path.

        Exit precedence (conservative): stop/trail touch first, then the
        72h timeout — a tick that crosses the stop IS the stop fill."""
        closes: List[dict] = []
        try:
            rec = self._open.get(symbol)
            if rec is None:
                return closes
            px = _f(price)
            now = _f(ts)
            if px is None or px <= 0 or now is None or now <= 0:
                return closes

            entry = float(rec["entry"])
            dist = float(rec["stop_dist_pct"])
            side = rec["side"]
            sign = 1.0 if side == "long" else -1.0

            # Amendment 1: excursion update on EVERY tick, in % and in R.
            ret = (px / entry - 1.0) * sign
            rec["mfe_pct"] = max(float(rec["mfe_pct"]), ret)
            rec["mae_pct"] = min(float(rec["mae_pct"]), ret)
            rec["mfe_R"] = rec["mfe_pct"] / dist if dist > 0 else 0.0
            rec["mae_R"] = rec["mae_pct"] / dist if dist > 0 else 0.0

            # Trail ratchet — mirrored BY CONSTRUCTION from the live
            # trailing-stop loop (main.py:16345-16380): activation after
            # act_atr x ATR favorable; trail at dist_atr x ATR behind best;
            # ratchet-only, never backward; same sub-mark clamp.
            atr_abs = float(rec["atr_abs"])
            act = float(rec["trail_act_atr"]) * atr_abs
            tdist = float(rec["trail_dist_atr"]) * atr_abs
            best = float(rec["best_price"])
            cur_stop = float(rec["current_stop"])
            if side == "long":
                if px > best:
                    best = px
                if best >= entry + act:
                    rec["trail_active"] = True
                    new_stop = min(best - tdist, px * 0.9999)
                    if new_stop > cur_stop:
                        cur_stop = new_stop
                hit = px <= cur_stop
            else:
                if px < best:
                    best = px
                if best <= entry - act:
                    rec["trail_active"] = True
                    new_stop = max(best + tdist, px * 1.0001)
                    if new_stop < cur_stop:
                        cur_stop = new_stop
                hit = px >= cur_stop
            rec["best_price"] = best
            rec["current_stop"] = cur_stop

            if hit:
                # Shadow fills AT the stop level (no slippage modeled —
                # same overstatement class as the entry; stop-out R = -1
                # by construction).
                exit_px = cur_stop
                eclass = "trail" if rec["trail_active"] else "stop"
                closes.append(self._finalize(rec, exit_px, now, eclass))
                return closes

            if now - float(rec["opened_ts"]) >= float(rec["timeout_h"]) * 3600.0:
                closes.append(self._finalize(rec, px, now, "timeout"))
            return closes
        except Exception:
            return closes

    def _finalize(self, rec: dict, exit_px: float, now: float,
                  eclass: str) -> dict:
        """Close the shadow: stamp the result, drop the registration, mark
        the no-reload ledger on stop-outs, and return the dense journal
        row (one line per close — density law)."""
        entry = float(rec["entry"])
        dist = float(rec["stop_dist_pct"])
        sign = 1.0 if rec["side"] == "long" else -1.0
        ret = (exit_px / entry - 1.0) * sign
        r_mult = ret / dist if dist > 0 else 0.0
        row = {
            "schema": SCHEMA_VERSION,
            "id": rec["id"],
            "gate": "lancer",
            "ts_open": rec["opened_ts"],
            "ts_close": now,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "symbol": rec["symbol"],
            "side": rec["side"],
            "signal_class": rec["signal_class"],
            "venue": rec["venue"],
            "leverage": rec["leverage"],
            "entry": round(entry, 8),
            "stop": round(float(rec["stop"]), 8),
            "stop_dist_pct": round(dist * 100.0, 4),
            "atr_pct": rec["atr_pct"],
            "atr_source": rec["atr_source"],
            "exit_class": eclass,
            "exit_price": round(exit_px, 8),
            "r_multiple": round(r_mult, 4),
            "mfe_R": round(float(rec["mfe_R"]), 4),
            "mae_R": round(float(rec["mae_R"]), 4),
            "mfe_pct": round(float(rec["mfe_pct"]) * 100.0, 4),
            "mae_pct": round(float(rec["mae_pct"]) * 100.0, 4),
            "trail_activated": bool(rec["trail_active"]),
            "time_in_trade_min": round(
                (now - float(rec["opened_ts"])) / 60.0, 2),
        }
        self._open.pop(rec["symbol"], None)
        if eclass == "stop":
            # No-reload ledger keys on the STOP-OUT day, not the admission
            # day (a position opened yesterday and stopped today blocks
            # today's reload).
            try:
                self._stopped_day[rec["symbol"]] = time.gmtime(now).tm_yday
            except Exception:
                self._stopped_day[rec["symbol"]] = self._day_key
        return row


def append_close(path: str, row: dict) -> bool:
    """Append one close row as a JSON line. Never raises, never blocks the
    trade path. One-bad-line doctrine: a serialization failure costs this
    row only. The kill switch lives at the splice (lancer_shadow_enabled)
    — False = this writer is never called (pre-module system bit-for-bit)."""
    try:
        line = json.dumps(row, default=str)
    except Exception:
        return False
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except Exception:
        return False
