"""
intelligence/shadow_journal.py — ARIA's Counterfactual Mind.

The cemetery of rejected signals, exhumed. Every gate refusal is recorded
with the market context at refusal time; a scorer loop revisits each ghost
at +1h / +4h / +24h and grades what WOULD have happened; an aggregator
answers the Nine Deep Questions nightly, decay-reweighed (14d half-life)
and shrunk toward zero for small samples.

Three faculties (books):
  Kahneman — the Epistemologist: base rates vs gate confidence (Q1-Q3)
  Taleb    — the Convexity auditor: silent evidence, right-tail destruction (Q4-Q6)
  Sun Tzu  — the Strategist: was silence wisdom or fear (Q7-Q9)

Wiring is a single structlog processor — the trade path is never touched.
Processor contract: never raise, never block, never mutate the event.

Storage (all under logs/):
  shadow_journal.jsonl   — raw refusals (append-only)
  shadow_registry.json   — open shadows + last-trade ts (restart-safe)
  shadow_report.md       — nightly human report (the emperor's review)
  gate_report.json       — same data, machine-readable (param_store phase 2)
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)

# ── Rejection events → gate names ────────────────────────────────────────────
REJECTION_EVENTS: Dict[str, str] = {
    "signal_rejected_dispersion_gate": "dispersion",
    "signal_rejected_c_tier":          "c_tier",
    "signal_rejected_dust_notional":   "dust_notional",
    "signal_stale_data":               "stale_data",
    "coherence_tier_reject":           "coherence_floor",
    "balance_floor_halt":              "balance_floor",
    "global_daily_limit_reached":      "daily_limit",
    "flip_blocked":                    "flip_cooldown",
    "risk_reward_reject":              "rr_minimum",
    "regime_alignment_reject":         "regime_alignment",
    "quant_filter_blocked":            "quant_filter",
    "signal_throttled":                "throttle",
    "meta_reflex_entry_blocked":       "meta_reflex",
    "build_candidate_turnover_reject": "turnover",
    # Incubation gate: a fully-approved signal on a venue-routed symbol (no
    # SoDEX ID) dies here. Shadow-scoring it measures the expansion universe's
    # counterfactual edge BEFORE capital commits (Q8 symbol-edge = graduation
    # report). Self-disables at activation — venue_for() != "sodex" bypasses
    # the guard, so the event stops firing once the symbol trades on Aster.
    "order_blocked_no_symbol_id":      "no_venue",
}

# Trade events — watched for silence detection (Q7) and fragility trend (Q6).
TRADE_EVENTS = frozenset({
    "order_submitted", "order_filled", "bracket_placed", "position_closed",
})

_HORIZONS_S = {"1h": 3600, "4h": 4 * 3600, "24h": 24 * 3600}
_DEDUP_WINDOW_S = 1800          # one open shadow per (symbol, side, gate) / 30min
_MAX_OPEN = 2000
_MAX_RECORD_PER_DAY = 6000
_HALF_LIFE_D = 14.0             # evidence decay — the journal tracks the season
_SHRINK_K = 20                  # n/(n+k) shrinkage — small samples can't move policy
_TICK_S = 300.0                 # scorer cadence: 5 min
_NEAR_MISS_PCT = 10.0           # |margin| ≤ 10% of threshold = near miss (Q3)
_MIN_STOP_PCT = 0.003           # hyp stop floor: 0.3% of price


def _session_of(ts: float) -> str:
    h = time.gmtime(ts).tm_hour
    if h < 7:
        return "asia"
    if h < 12:
        return "london"
    if h < 21:
        return "us"
    return "off_hours"


def _skew(xs: List[float]) -> float:
    n = len(xs)
    if n < 8:
        return 0.0
    m = sum(xs) / n
    m2 = sum((x - m) ** 2 for x in xs) / n
    if m2 <= 0:
        return 0.0
    m3 = sum((x - m) ** 3 for x in xs) / n
    return m3 / (m2 ** 1.5)


class ShadowJournal:
    def __init__(self) -> None:
        self._wired = False
        self._config: Any = None
        self._candle_buffers: Dict = {}
        self._mark_stores: Dict = {}
        self._bybit_tickers: Dict = {}
        self._open: Dict[str, Dict] = {}          # shadow_id → entry
        self._dedup: Dict[tuple, float] = {}      # (sym, dir, gate) → last ts
        self._last_trade_ts: float = 0.0
        self._trade_ts: List[float] = []          # trailing trade timestamps
        self._recorded_today: int = 0
        self._record_day: int = 0
        self._fh = None                            # persistent JSONL handle
        self._scored: List[Dict] = []             # in-memory scored window (35d cap)
        self._gate_series: Dict[str, List] = defaultdict(list)  # gate → [(ts, value)]
        self._context_fn: Any = None              # symbol → {market_energy, day_type}

    # ── Wiring ────────────────────────────────────────────────────────────

    def wire(self, config: Any, candle_buffers: Dict, mark_stores: Dict,
             bybit_tickers: Dict, context_fn: Any = None) -> None:
        self._config = config
        self._candle_buffers = candle_buffers
        self._mark_stores = mark_stores
        self._bybit_tickers = bybit_tickers
        self._context_fn = context_fn
        self._wired = bool(getattr(config, "shadow_journal_enabled", True))
        if self._wired:
            self._load_registry()
            logger.info("shadow_journal_wired", open_shadows=len(self._open))

    def _path(self, name: str) -> str:
        log_dir = getattr(self._config, "log_dir", "logs") if self._config else "logs"
        return os.path.join(log_dir, name)

    # ── structlog processor (sync, never raises) ─────────────────────────

    def processor(self, _logger, _method: str, event_dict: Dict) -> Dict:
        try:
            ev = event_dict.get("event", "")
            if ev in REJECTION_EVENTS:
                self._record(ev, event_dict)
            elif ev in TRADE_EVENTS:
                ts = time.time()
                self._last_trade_ts = ts
                self._trade_ts.append(ts)
                if len(self._trade_ts) > 5000:
                    self._trade_ts = self._trade_ts[-2500:]
        except Exception:
            pass
        return event_dict

    # ── Recording ─────────────────────────────────────────────────────────

    def _price_of(self, symbol: str) -> float:
        store = self._mark_stores.get(symbol)
        px = float(getattr(store, "mark_price", 0.0) or 0.0) if store else 0.0
        if px > 0:
            return px
        tick = self._bybit_tickers.get(symbol) or {}
        px = float(tick.get("mark_price", 0.0) or 0.0)
        if px > 0:
            return px
        buf = (self._candle_buffers.get(symbol) or {}).get("1m")
        if buf is not None:
            tail = buf.latest(1)
            if tail:
                return float(tail[0].close)
        return 0.0

    def _hyp_stop(self, symbol: str, entry: float, direction: str) -> float:
        buf = (self._candle_buffers.get(symbol) or {}).get("1m")
        atr = 0.0
        if buf is not None:
            cs = buf.latest(15)
            if len(cs) >= 5:
                atr = sum(float(c.high) - float(c.low) for c in cs) / len(cs)
        dist = max(2.0 * atr, _MIN_STOP_PCT * entry)
        return entry - dist if direction == "long" else entry + dist

    def _context(self, symbol: str) -> Dict:
        fn = self._context_fn
        if fn is None:
            return {}
        try:
            d = fn(symbol)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _record(self, event: str, kw: Dict) -> None:
        if not self._wired:
            return
        symbol = kw.get("symbol")
        direction = kw.get("direction") or kw.get("dir") or "none"
        if not symbol or direction not in ("long", "short"):
            return
        gate = REJECTION_EVENTS[event]
        gate_value = kw.get("dispersion", kw.get("value"))
        if gate_value is None and gate in ("coherence_floor", "c_tier"):
            gate_value = kw.get("coherence")
        self._commit(symbol, direction, gate, event,
                     reason=str(kw.get("reason", ""))[:80],
                     coherence=float(kw.get("coherence", 0.0) or 0.0),
                     gate_value=gate_value,
                     gate_threshold=kw.get("threshold"),
                     regime=str(kw.get("regime", "") or ""))

    def record_candidate(self, symbol: str, direction: str, source: str,
                         score: float, details: str = "") -> None:
        """Phase A incubation channel — the Dreamer's voice to the Historian.

        Non-executable candidates (explosive scanner precursors) are
        shadow-scored exactly like gate refusals: entry now, MFE/MAE at
        1h/4h/24h, wise/lucky quadrants in the nightly report. The gate
        name carries the score (explosive_s3) so Q8 ranks visions by tier.
        """
        if not self._wired:
            return
        if not symbol or direction not in ("long", "short"):
            return
        gate = f"{source}_s{int(score)}"
        self._commit(symbol, direction, gate, f"{source}_candidate",
                     reason=str(details)[:80], coherence=float(score),
                     gate_value=float(score))

    def _commit(self, symbol: str, direction: str, gate: str, event: str,
                reason: str = "", coherence: float = 0.0,
                gate_value: Any = None, gate_threshold: Any = None,
                regime: str = "") -> None:
        now = time.time()
        day = time.gmtime(now).tm_yday
        if day != self._record_day:
            self._record_day, self._recorded_today = day, 0
        if self._recorded_today >= _MAX_RECORD_PER_DAY:
            return
        dkey = (symbol, direction, gate)
        if now - self._dedup.get(dkey, 0.0) < _DEDUP_WINDOW_S:
            return
        self._dedup[dkey] = now

        entry = self._price_of(symbol)
        if entry <= 0:
            return
        if isinstance(gate_value, (int, float)):
            series = self._gate_series[gate]
            series.append((now, float(gate_value)))
            if len(series) > 500:
                del series[:250]
        ctx = self._context(symbol)
        sid = f"{int(now)}_{symbol}_{direction}_{gate}"
        btc_px = self._price_of("BTC-USD")
        rec = {
            "id": sid, "ts": now, "symbol": symbol, "direction": direction,
            "gate": gate, "event": event,
            "reason": reason,
            "coherence": coherence,
            "entry": entry,
            "hyp_stop": self._hyp_stop(symbol, entry, direction),
            "btc_price": btc_px,
            "session": _session_of(now),
            "regime": regime,
            "market_energy": ctx.get("market_energy"),
            "day_type": str(ctx.get("day_type", "") or ""),
            "gate_value": gate_value,
            "gate_threshold": gate_threshold,
            "marks": {}, "mfe": 0.0, "mae": 0.0,
            "stopped": False, "scored": {}, "info_axis": None,
        }
        self._open[sid] = rec
        if len(self._open) > _MAX_OPEN:
            oldest = sorted(self._open, key=lambda k: self._open[k]["ts"])
            for k in oldest[: len(self._open) - _MAX_OPEN]:
                self._open.pop(k, None)
        self._recorded_today += 1
        try:
            if self._fh is None:
                self._fh = open(self._path("shadow_journal.jsonl"), "a", buffering=1)
            self._fh.write(json.dumps({k: v for k, v in rec.items()
                                       if k not in ("marks", "scored")}) + "\n")
        except Exception:
            self._fh = None

    # ── Registry persistence (restart-safe) ───────────────────────────────

    def _save_registry(self) -> None:
        try:
            tmp = self._path("shadow_registry.json.tmp")
            with open(tmp, "w") as f:
                json.dump({"open": self._open,
                           "last_trade_ts": self._last_trade_ts,
                           "trade_ts": self._trade_ts[-500:]}, f)
            os.replace(tmp, self._path("shadow_registry.json"))
        except Exception as e:
            logger.debug("shadow_registry_save_failed", error=str(e)[:80])

    def _load_registry(self) -> None:
        try:
            with open(self._path("shadow_registry.json")) as f:
                d = json.load(f)
            now = time.time()
            self._open = {k: v for k, v in (d.get("open") or {}).items()
                          if now - float(v.get("ts", 0)) < 26 * 3600}
            self._last_trade_ts = float(d.get("last_trade_ts", 0.0) or 0.0)
            self._trade_ts = list(d.get("trade_ts") or [])
        except Exception:
            self._open = {}

    # ── Scorer loop (5-min cadence) ───────────────────────────────────────

    def _ret_pct(self, rec: Dict, px: float) -> float:
        if rec["entry"] <= 0 or px <= 0:
            return 0.0
        r = px / rec["entry"] - 1.0
        return r if rec["direction"] == "long" else -r

    def _score_tick(self) -> None:
        now = time.time()
        done: List[str] = []
        for sid, rec in self._open.items():
            px = self._price_of(rec["symbol"])
            if px <= 0:
                continue
            r = self._ret_pct(rec, px)
            rec["mfe"] = max(rec["mfe"], r)
            rec["mae"] = min(rec["mae"], r)
            stop = rec.get("hyp_stop", 0.0)
            if stop > 0 and not rec["stopped"]:
                hit = (px <= stop) if rec["direction"] == "long" else (px >= stop)
                if hit:
                    rec["stopped"] = True
            age = now - rec["ts"]
            # Persistence test (lucky-gate detection): re-read the gate's value
            # ~30min after refusal. TRANSIENT information = gate measured a
            # snapshot that didn't persist; PERSISTENT = condition was durable.
            if age >= 1500 and rec["info_axis"] is None:
                v1 = rec.get("gate_value")
                thr = rec.get("gate_threshold")
                if isinstance(v1, (int, float)) and isinstance(thr, (int, float)) and thr:
                    later = next((v for t, v in self._gate_series.get(rec["gate"], [])
                                  if t >= rec["ts"] + 1500), None)
                    if later is not None:
                        delta = abs(later - v1) / abs(thr)
                        rec["info_axis"] = ("TRANSIENT" if delta > 0.5
                                            else "PERSISTENT" if delta < 0.2
                                            else "AMBIGUOUS")
            for name, horizon in _HORIZONS_S.items():
                if age >= horizon and name not in rec["scored"]:
                    rec["scored"][name] = round(r * 100.0, 4)   # % return
            if age >= _HORIZONS_S["24h"]:
                rec["pnl_4h"] = rec["scored"].get("4h")
                rec["pnl_24h"] = rec["scored"].get("24h")
                rec["won_4h"] = (rec["pnl_4h"] or 0.0) > 0 and not rec["stopped"]
                rec["won_24h"] = (rec["pnl_24h"] or 0.0) > 0 and not rec["stopped"]
                saved = (rec["pnl_24h"] or 0.0) <= 0
                rec["quadrant"] = {
                    ("PERSISTENT", True): "wise",
                    ("PERSISTENT", False): "correct_unlucky",
                    ("TRANSIENT", True): "lucky",
                    ("TRANSIENT", False): "broken",
                }.get((rec["info_axis"], saved), "unknown")
                self._scored.append(rec)
                done.append(sid)
        for sid in done:
            self._open.pop(sid, None)
        if len(self._scored) > 20000:
            self._scored = self._scored[-10000:]

    async def scorer_loop(self) -> None:
        while True:
            try:
                if self._wired:
                    self._score_tick()
                    self._save_registry()
            except Exception as e:
                logger.warning("shadow_scorer_error", error=str(e)[:120])
            await asyncio.sleep(_TICK_S)

    # ── Aggregation: the Nine Deep Questions ──────────────────────────────

    def scored_records(self) -> List[Dict]:
        """Read-only snapshot of the scored window — the Skeptic's Phase-B
        base-rate query surface (35d cap, won_24h verdicts attached)."""
        return list(self._scored)

    def _window(self, days: float) -> List[Dict]:
        cutoff = time.time() - days * 86400
        return [r for r in self._scored if r["ts"] >= cutoff]

    @staticmethod
    def _w(ts: float) -> float:
        age_d = max(0.0, (time.time() - ts) / 86400.0)
        return 0.5 ** (age_d / _HALF_LIFE_D)

    def _fnr(self, rows: List[Dict], key: str = "won_4h") -> Optional[float]:
        """False-negative rate: share of refusals that would have won.
        Decay-weighted, shrunk toward 0.5-prior-neutral by small n."""
        if not rows:
            return None
        num = sum(self._w(r["ts"]) for r in rows if r.get(key))
        den = sum(self._w(r["ts"]) for r in rows)
        if den <= 0:
            return None
        raw = num / den
        n = len(rows)
        return round((n / (n + _SHRINK_K)) * raw + (_SHRINK_K / (n + _SHRINK_K)) * 0.5, 3)

    def _aggregate(self) -> Dict:
        rows7 = self._window(7)
        by_gate: Dict[str, List[Dict]] = defaultdict(list)
        by_sym: Dict[str, List[Dict]] = defaultdict(list)
        for r in rows7:
            by_gate[r["gate"]].append(r)
            by_sym[r["symbol"]].append(r)

        # Q1 — base rate of profitability per gate (Kahneman's outside view)
        q1 = {g: {"n": len(rs), "fnr_4h": self._fnr(rs),
                  "fnr_24h": self._fnr(rs, "won_24h")}
              for g, rs in sorted(by_gate.items())}

        # Q2 — anchoring: FNR trailing 3d vs prior 3d
        now = time.time()
        t3 = [r for r in self._scored if now - 3 * 86400 <= r["ts"] < now]
        p3 = [r for r in self._scored if now - 6 * 86400 <= r["ts"] < now - 3 * 86400]
        q2 = {"fnr_trailing_3d": self._fnr(t3), "fnr_prior_3d": self._fnr(p3),
              "anchoring_suspected": bool(
                  self._fnr(t3) is not None and self._fnr(p3) is not None
                  and self._fnr(t3) - self._fnr(p3) > 0.10
                  and len(t3) > len(p3))}

        # Q3 — near-miss profitability (within 10% of threshold)
        near = [r for r in rows7
                if isinstance(r.get("gate_value"), (int, float))
                and isinstance(r.get("gate_threshold"), (int, float))
                and r["gate_threshold"]
                and abs(r["gate_value"] - r["gate_threshold"])
                / abs(r["gate_threshold"]) * 100.0 <= _NEAR_MISS_PCT]
        q3 = {"n": len(near), "near_miss_fnr_4h": self._fnr(near)}

        # Q4 — skewness of shadow 24h outcomes (Taleb's right tail)
        pnls = [r["pnl_24h"] for r in rows7 if r.get("pnl_24h") is not None]
        q4 = {"n": len(pnls), "skew_24h": round(_skew(pnls), 3),
              "verdict": ("right_tail_blocked" if _skew(pnls) > 0.5
                          else "left_tail_blocked" if _skew(pnls) < -0.5
                          else "symmetric")}

        # Q5 — Gate Value Ratio: losses prevented / profits blocked
        q5 = {}
        for g, rs in sorted(by_gate.items()):
            blocked = sum(self._w(r["ts"]) * max(0.0, r.get("pnl_24h") or 0.0) for r in rs)
            saved = sum(self._w(r["ts"]) * abs(min(0.0, r.get("pnl_24h") or 0.0)) for r in rs)
            q5[g] = {"gvr": round(saved / blocked, 2) if blocked > 0 else None,
                     "profit_blocked_pct": round(blocked, 2),
                     "loss_prevented_pct": round(saved, 2)}

        # Q6 — fragility trajectory (approximation: reject + trade trends)
        rej7 = len(rows7)
        rej_prev = len([r for r in self._scored
                        if now - 14 * 86400 <= r["ts"] < now - 7 * 86400])
        tr7 = len([t for t in self._trade_ts if t >= now - 7 * 86400])
        tr_prev = len([t for t in self._trade_ts
                       if now - 14 * 86400 <= t < now - 7 * 86400])
        q6 = {"rejections_7d": rej7, "rejections_prior_7d": rej_prev,
              "trades_7d": tr7, "trades_prior_7d": tr_prev,
              "contracting": bool(rej7 > rej_prev * 1.2 and tr7 < tr_prev * 0.8)}

        # Q7 — silence: gaps >4h without trades, scored by shadow PnL inside
        silences = []
        bounds = sorted(t for t in self._trade_ts if t >= now - 7 * 86400)
        for a, b in zip(bounds, bounds[1:]):
            if b - a >= 4 * 3600:
                inside = [r for r in rows7 if a <= r["ts"] < b
                          and r.get("pnl_24h") is not None]
                pnl = sum(r["pnl_24h"] for r in inside)
                silences.append({
                    "start": time.strftime("%m-%dT%H:%MZ", time.gmtime(a)),
                    "hours": round((b - a) / 3600, 1),
                    "shadow_pnl_pct": round(pnl, 2),
                    "verdict": "fear" if pnl > 0 else "wisdom"})
        q7 = silences[-5:]

        # Q8 — symbol shadow edge (terrain map)
        q8 = {s: {"n": len(rs), "fnr_4h": self._fnr(rs),
                  "avg_pnl_24h": round(sum(r.get("pnl_24h") or 0.0 for r in rs)
                                       / max(1, len(rs)), 3)}
              for s, rs in sorted(by_sym.items(),
                                  key=lambda kv: -len(kv[1]))[:15]}

        # Q9 — self-knowledge map: outcomes partitioned by session
        by_sess: Dict[str, List[Dict]] = defaultdict(list)
        for r in rows7:
            by_sess[r.get("session") or "unknown"].append(r)
        q9 = {s: {"n": len(rs), "fnr_4h": self._fnr(rs),
                  "avg_pnl_24h": round(sum(r.get("pnl_24h") or 0.0 for r in rs)
                                       / max(1, len(rs)), 3)}
              for s, rs in sorted(by_sess.items())}

        # Q10 — the lucky-gate census (four quadrants, persistence test)
        q10: Dict[str, Dict] = {}
        quad_by_gate: Dict[str, List[str]] = defaultdict(list)
        for r in rows7:
            quad_by_gate[r["gate"]].append(r.get("quadrant", "unknown"))
        for g, qs in sorted(quad_by_gate.items()):
            classified = [q for q in qs if q != "unknown"]
            lucky = qs.count("lucky")
            q10[g] = {"wise": qs.count("wise"), "lucky": lucky,
                      "broken": qs.count("broken"),
                      "correct_unlucky": qs.count("correct_unlucky"),
                      "unclassified": qs.count("unknown"),
                      "lucky_share": (round(lucky / len(classified), 3)
                                      if classified else None),
                      "luck_dominated": bool(classified
                                             and lucky / len(classified) > 0.30)}

        return {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "window_days": 7, "shadows_scored": len(rows7),
                "half_life_days": _HALF_LIFE_D, "shrink_k": _SHRINK_K,
                "q1_gate_fnr": q1, "q2_anchoring": q2, "q3_near_miss": q3,
                "q4_skew": q4, "q5_gate_value_ratio": q5, "q6_fragility": q6,
                "q7_silence": q7, "q8_symbol_edge": q8, "q9_session_map": q9,
                "q10_lucky_gates": q10}

    def _render_md(self, rep: Dict) -> str:
        L = [f"# Shadow Journal — {rep['generated']}",
             f"Window {rep['window_days']}d · {rep['shadows_scored']} scored ghosts · "
             f"half-life {rep['half_life_days']}d · shrink k={rep['shrink_k']}", ""]
        L.append("## Q1 · Gate false-negative rates (Kahneman — base rates)")
        L.append("| gate | n | FNR 4h | FNR 24h | verdict |")
        L.append("|---|---|---|---|---|")
        for g, d in rep["q1_gate_fnr"].items():
            f4 = d["fnr_4h"]
            v = ("DESTRUCTIVE" if f4 and f4 > 0.50 else
                 "concerning" if f4 and f4 > 0.35 else "healthy")
            L.append(f"| {g} | {d['n']} | {f4} | {d['fnr_24h']} | {v} |")
        q2 = rep["q2_anchoring"]
        L += ["", "## Q2 · Anchoring on recent trauma",
              f"FNR 3d: {q2['fnr_trailing_3d']} vs prior 3d: {q2['fnr_prior_3d']} "
              f"→ anchoring_suspected={q2['anchoring_suspected']}",
              "", "## Q3 · Near-miss (within 10% of threshold)",
              f"n={rep['q3_near_miss']['n']} · FNR 4h = {rep['q3_near_miss']['near_miss_fnr_4h']}",
              "", "## Q4 · Shadow PnL skew (Taleb)",
              f"n={rep['q4_skew']['n']} · skew={rep['q4_skew']['skew_24h']} "
              f"→ {rep['q4_skew']['verdict']}",
              "", "## Q5 · Gate Value Ratio (loss prevented / profit blocked)",
              "| gate | GVR | profit blocked % | loss prevented % |",
              "|---|---|---|---|"]
        for g, d in rep["q5_gate_value_ratio"].items():
            L.append(f"| {g} | {d['gvr']} | {d['profit_blocked_pct']} | {d['loss_prevented_pct']} |")
        q6 = rep["q6_fragility"]
        L += ["", "## Q6 · Fragility trajectory",
              f"rejections 7d {q6['rejections_7d']} (prior {q6['rejections_prior_7d']}) · "
              f"trades 7d {q6['trades_7d']} (prior {q6['trades_prior_7d']}) "
              f"→ contracting={q6['contracting']}",
              "", "## Q7 · Silence — wisdom or fear (Sun Tzu)"]
        for s in rep["q7_silence"]:
            L.append(f"- {s['start']} · {s['hours']}h · shadow {s['shadow_pnl_pct']}% → **{s['verdict']}**")
        L += ["", "## Q8 · Symbol shadow edge",
              "| symbol | n | FNR 4h | avg pnl 24h % |", "|---|---|---|---|"]
        for s, d in rep["q8_symbol_edge"].items():
            L.append(f"| {s} | {d['n']} | {d['fnr_4h']} | {d['avg_pnl_24h']} |")
        L += ["", "## Q9 · Session self-knowledge", "| session | n | FNR 4h | avg pnl 24h % |",
              "|---|---|---|---|"]
        for s, d in rep["q9_session_map"].items():
            L.append(f"| {s} | {d['n']} | {d['fnr_4h']} | {d['avg_pnl_24h']} |")
        L += ["", "## Q10 · Lucky-gate census (persistence test)",
              "| gate | wise | lucky | broken | correct-unlucky | lucky share |",
              "|---|---|---|---|---|---|"]
        for g, d in rep["q10_lucky_gates"].items():
            flag = " ⚠ LUCK-DOMINATED" if d["luck_dominated"] else ""
            L.append(f"| {g} | {d['wise']} | {d['lucky']} | {d['broken']} "
                     f"| {d['correct_unlucky']} | {d['lucky_share']}{flag} |")
        L.append("")
        return "\n".join(L)

    async def aggregator_loop(self) -> None:
        """Nightly consolidation — the emperor's review. Writes md + json."""
        last_day = 0
        while True:
            try:
                await asyncio.sleep(600)
                if not self._wired:
                    continue
                now = time.time()
                day = time.gmtime(now).tm_yday
                hour = time.gmtime(now).tm_hour
                if day != last_day and hour >= 0:
                    last_day = day
                    rep = self._aggregate()
                    with open(self._path("gate_report.json"), "w") as f:
                        json.dump(rep, f, indent=1)
                    with open(self._path("shadow_report.md"), "w") as f:
                        f.write(self._render_md(rep))
                    logger.info("shadow_report_written",
                                scored=rep["shadows_scored"],
                                gates=len(rep["q1_gate_fnr"]))
            except Exception as e:
                logger.warning("shadow_aggregator_error", error=str(e)[:120])


shadow_journal = ShadowJournal()
