"""
intelligence/cross_asset_lag.py — E2 cross-asset sector-rotation lag engine
(pure brain + thin publisher, 2026-09-26 E2 build).

Doctrine (E2): equity leaders move first; the crypto AI-complex follows with
a measurable lag. The equity_colony plane covers leader→follower SIZE boosts
out to a 30-minute horizon — E2 owns the longer arcs:
  NVDA move        → AMD follows,          lag 1–3d   (weak — same complex)
  NVDA/AMD complex → FET, RENDER follow,   lag 6–24h  (THE trade)
  COIN move        → BTC follows,          lag 1–12h
  GOOGL weak + COIN strong → SHORT GOOGL / LONG COIN rotation pair
  SOX gap-up >1.5% confirms semiconductor-complex crypto longs (boost only).

Propagation map (target, min_lag_s, max_lag_s):
  NVDA → AMD 1–3d, FET 6–24h, RENDER 6–24h
  AMD  → FET 6–24h, RENDER 6–24h
  COIN → BTC 1–12h
COIN→BTC DEDUPE NOTE: equity_flow_signals.coin_lead_signal already arms the
COIN→BTC lead at a 3–12h window with its own confidence. This engine still
tracks the COIN row of the map (state completeness), but the coordinator
MUST dedupe at consumption — the equity_flow_signals plane is the primary
COIN→BTC voice; this engine's BTC signal is the corroborating shadow.

Dark-target doctrine (verified 2026-09-26): NEITHER FET nor RENDER is in
ARIA's tradeable universe — both absent from core/config.py assets,
data/bybit_feed.BYBIT_SYMBOL_MAP / SUPPORTED_ASSETS, and
relative_strength.ASSET_CATEGORIES. FET was explicitly rejected 2026-07-30
("dead ticker data mid-migration"). Absent targets are DARK: the engine
tracks their windows internally (so a later listing lights them up with
history intact) but NEVER emits a LagSignal for them — abstain, not crash.
The coordinator injects `tradeable_targets` from the live universe at
wiring time; the module default reflects the verified 2026-09-26 state
(AMD, BTC tradeable; FET, RENDER dark).

SOX confirm plane: no SOX symbol is registered in ARIA. The confirm leg
reads the EXTERNAL param-store key `e2:sox_gap_pct` (written by an external
plane; value {"gap_pct": float, "ts": epoch} or a bare float read as
fresh-at-read). The publisher pulls it into the engine via ingest_sox_gap.
Armed when gap_pct > 1.5 and fresher than SOX_TTL_S. DARK/ABSENT = abstain
— the confirm NEVER blocks a signal, it only adds +0.15 confidence to the
semiconductor-complex crypto targets (FET, RENDER — never AMD, an equity
whose confirmation is its own tape).

Confidence curve: 0.5 at min_lag, ramping to 1.0 at the window midpoint,
decaying back to 0.5 at max_lag; +0.15 SOX boost (semi targets only),
hard-capped at 1.0. Signals are inactive before min_lag and pruned after
max_lag. Dedupe: ONE open signal per target — a fresher leader move
REFRESHES the target's window (latest leader state wins), never duplicates.

Persistence: atomic JSON mirror (tmp + os.replace) plus an append-only
.jsonl audit trail so 6–24h / 1–3d windows survive restarts. Corrupt state
fails closed to an empty book — never raises.

Department-template shape (docs/DEPARTMENT_TEMPLATE.md): all market reads
arrive as arguments; decisions leave as frozen verdicts. main.py owns the
I/O; the wiring is a later phase (coordinator-owned).

Kill switch: env CROSS_ASSET_LAG_ENABLED default "true"; false =
active_signals() returns [] and pair_signal() returns None. Ingest still
tracks state (flipping the switch mid-run loses nothing).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from intelligence.equity_flow_signals import rotation_signal as _efs_rotation_signal


# ── Kill switch ───────────────────────────────────────────────────────────────

def cross_asset_lag_enabled() -> bool:
    """Module-level kill switch (env CROSS_ASSET_LAG_ENABLED, default true).
    False = active_signals() [] / pair_signal() None; ingest still tracks."""
    return os.environ.get("CROSS_ASSET_LAG_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on")


# ── Shared helpers ────────────────────────────────────────────────────────────

def _f(x) -> Optional[float]:
    """Coerce to float; None on any garbage (fail-closed idiom)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def base_name(symbol) -> str:
    """'NVDA-USD' -> 'NVDA' (symbols may arrive venue-suffixed)."""
    try:
        s = str(symbol)
    except Exception:
        return ""
    if not s or s == "None":
        return ""
    return s.split("-")[0].strip().upper()


# ── Doctrine constants ────────────────────────────────────────────────────────

LEAD_MOVE_MIN_PCT = 2.0          # |move_pct| >= 2% arms a leader event
LEAD_LOOKBACK_S = 86400.0        # default LeadEvent window_s annotation
SOX_GAP_ARM_PCT = 1.5            # SOX gap-up > 1.5% confirms the complex
SOX_TTL_S = 86400.0              # a SOX print is confirm-fresh for 24h
SOX_CONF_BOOST = 0.15            # +0.15 confidence, semi targets only
CONF_FLOOR = 0.5                 # confidence at the window edges
CONF_PEAK = 1.0                  # confidence at the window midpoint

# target, min_lag_s, max_lag_s — THE trade is the 6–24h crypto AI basket leg.
PROPAGATION_MAP: Dict[str, List[Tuple[str, int, int]]] = {
    "NVDA": [("AMD", 86400, 259200), ("FET", 21600, 86400), ("RENDER", 21600, 86400)],
    "AMD": [("FET", 21600, 86400), ("RENDER", 21600, 86400)],
    "COIN": [("BTC", 3600, 43200)],
}

# Semiconductor-complex CRYPTO targets — the only legs the SOX confirm boosts
# (AMD is an equity; its confirmation is its own tape, never the SOX key).
SEMI_CONFIRM_TARGETS = frozenset({"FET", "RENDER"})

# Verified 2026-09-26: neither symbol is in the tradeable universe
# (core/config.py assets, data/bybit_feed.BYBIT_SYMBOL_MAP/SUPPORTED_ASSETS,
# relative_strength.ASSET_CATEGORIES all grep-clean). Dark = tracked but
# never emitted. The note is the audit trail; listing flips the default.
DARK_TARGET_NOTES: Dict[str, str] = {
    "FET":    "absent from tradeable universe 2026-09-26 (rejected "
              "2026-07-30: dead ticker data mid-migration) — dark/abstain",
    "RENDER": "absent from tradeable universe 2026-09-26 — dark/abstain",
}

_ALL_TARGETS = frozenset(t for rows in PROPAGATION_MAP.values() for t, _, _ in rows)
DEFAULT_TRADEABLE_TARGETS = frozenset(_ALL_TARGETS) - frozenset(DARK_TARGET_NOTES)

PAIR_NAME = "SHORT_GOOGL_LONG_COIN"
PAIR_LEG_RATIO = 1.0             # 1:1 notional legs


# ── Verdict dataclasses (frozen) ─────────────────────────────────────────────

@dataclass(frozen=True)
class LeadEvent:
    """A detected leader move (|move_pct| >= LEAD_MOVE_MIN_PCT over its
    lookback). window_s annotates the lookback the move was measured over."""
    source_symbol: str
    direction: str                 # "LONG" | "SHORT"
    move_pct: float
    ts: float
    window_s: float


@dataclass(frozen=True)
class LagSignal:
    """One armed propagation signal. confidence in [0.5, 1.0] (+SOX boost,
    capped 1.0); age_s = now − leader-event ts; expires_ts = ts + max_lag."""
    target_symbol: str
    direction: str                 # leader's direction — followers follow
    confidence: float
    source: str
    age_s: float
    expires_ts: float


# ── Confidence curve ──────────────────────────────────────────────────────────

def window_confidence(age_s, min_lag_s, max_lag_s) -> Optional[float]:
    """0.5 at min_lag → 1.0 at the midpoint → 0.5 at max_lag (linear both
    legs). None outside [min_lag, max_lag] — the signal is not active."""
    a, mn, mx = _f(age_s), _f(min_lag_s), _f(max_lag_s)
    if a is None or mn is None or mx is None or mx <= mn:
        return None
    if a < mn or a > mx:
        return None
    mid = (mn + mx) / 2.0
    if a <= mid:
        span = mid - mn
        frac = (a - mn) / span if span > 0 else 1.0
        return CONF_FLOOR + (CONF_PEAK - CONF_FLOOR) * frac
    span = mx - mid
    frac = (a - mid) / span if span > 0 else 1.0
    return CONF_PEAK - (CONF_PEAK - CONF_FLOOR) * frac


# ── Rotation pair verdict (wraps equity_flow_signals.rotation_signal) ────────

def pair_signal(googl_chg, coin_chg) -> Optional[Tuple[str, float]]:
    """SHORT GOOGL / LONG COIN rotation pair, legs 1:1 notional. Wraps
    equity_flow_signals.rotation_signal with GOOGL as the single big-tech
    leg this lens reads (AMZN leg unknown here — mean(GOOGL,GOOGL)=GOOGL,
    so the -0.5% big-tech bleed leg binds on GOOGL alone). Fires on
    (googl −1%, coin +1%); not on (googl +0.5%, coin +1%). Confidence =
    clamp((|googl| + |coin|) / 4, 0.5, 1.0) — bounded, monotone in the
    rotation evidence, never below the window floor once armed."""
    if not cross_asset_lag_enabled():
        return None
    g, c = _f(googl_chg), _f(coin_chg)
    if g is None or c is None:
        return None
    if not _efs_rotation_signal(g, g, c):
        return None
    conf = min(1.0, max(0.5, (abs(g) + abs(c)) / 4.0))
    return (PAIR_NAME, conf)


# ── Engine ────────────────────────────────────────────────────────────────────

class CrossAssetLagEngine:
    """Rolling leader-move detector + propagation-window book.

    time_fn: injectable clock. persist_path: atomic JSON mirror (None =
    in-memory only); the .jsonl audit trail rides alongside. State loads at
    construction — 6–24h / 1–3d windows survive restarts. Never raises."""

    def __init__(self, time_fn: Callable[[], float] = time.time,
                 persist_path: Optional[str] = "logs/cross_asset_lag.json",
                 tradeable_targets: Optional[Iterable[str]] = None,
                 lead_move_min_pct: float = LEAD_MOVE_MIN_PCT):
        self._time = time_fn
        self._persist_path = persist_path
        self._jsonl_path = self._jsonl_for(persist_path)
        self.lead_move_min_pct = _f(lead_move_min_pct) or LEAD_MOVE_MIN_PCT
        if tradeable_targets is None:
            self._tradeable = set(DEFAULT_TRADEABLE_TARGETS)
        else:
            self._tradeable = {base_name(t) for t in tradeable_targets}
        # target -> open propagation window (one open signal per target)
        self._open: Dict[str, dict] = {}
        self._last_moves: Dict[str, dict] = {}   # rolling detection state
        self._events: List[LeadEvent] = []       # bounded leader-event log
        self._sox: Optional[dict] = None         # {"gap_pct": f, "ts": f}
        self._load()

    # ── introspection ────────────────────────────────────────────────────

    @property
    def tradeable_targets(self) -> frozenset:
        return frozenset(self._tradeable)

    @property
    def dark_targets(self) -> frozenset:
        """Targets tracked but never emitted (absent from the universe)."""
        return frozenset(t for t in _ALL_TARGETS if t not in self._tradeable)

    def open_window_count(self) -> int:
        return len(self._open)

    # ── ingest ───────────────────────────────────────────────────────────

    def ingest_move(self, source_symbol, move_pct, ts) -> Optional[LeadEvent]:
        """Rolling leader-move detection. |move_pct| >= lead_move_min_pct
        arms a LeadEvent and refreshes every mapped target window (dedupe:
        one open signal per target — refresh, never duplicate). Sub-threshold
        moves update the rolling state only. Returns the event or None."""
        src = base_name(source_symbol)
        mv, t = _f(move_pct), _f(ts)
        if not src or mv is None or t is None:
            return None
        self._last_moves[src] = {"move_pct": mv, "ts": t}
        if abs(mv) < self.lead_move_min_pct:
            return None
        direction = "LONG" if mv > 0 else "SHORT"
        event = LeadEvent(source_symbol=src, direction=direction,
                          move_pct=mv, ts=t, window_s=LEAD_LOOKBACK_S)
        self._events.append(event)
        if len(self._events) > 200:
            self._events = self._events[-200:]
        refreshed = False
        for target, min_lag, max_lag in PROPAGATION_MAP.get(src, ()):
            prior = target in self._open
            self._open[target] = {
                "target": target, "source": src, "direction": direction,
                "move_pct": mv, "ts": t,
                "min_lag_s": float(min_lag), "max_lag_s": float(max_lag),
            }
            self._append_jsonl({
                "event": "propagation_refreshed" if prior else "propagation_armed",
                "target": target, "source": src, "direction": direction,
                "move_pct": mv, "ts": t,
                "min_lag_s": min_lag, "max_lag_s": max_lag,
            })
            refreshed = True
        if refreshed:
            self._save()
        return event

    def ingest_sox_gap(self, gap_pct, ts) -> bool:
        """Feed the external SOX confirm plane (param-store e2:sox_gap_pct).
        Returns True when the print ARMS the confirm (gap > 1.5%)."""
        g, t = _f(gap_pct), _f(ts)
        if g is None or t is None:
            return False
        self._sox = {"gap_pct": g, "ts": t}
        self._append_jsonl({"event": "sox_ingested", "gap_pct": g, "ts": t})
        self._save()
        return g > SOX_GAP_ARM_PCT

    def sox_confirm_armed(self, now=None) -> bool:
        """SOX gap-up > 1.5% and fresher than SOX_TTL_S. Absent/stale =
        abstain — the confirm never blocks, it only boosts."""
        n = _f(now) if now is not None else self._now()
        if self._sox is None or n is None:
            return False
        g, t = _f(self._sox.get("gap_pct")), _f(self._sox.get("ts"))
        if g is None or t is None:
            return False
        return g > SOX_GAP_ARM_PCT and (n - t) <= SOX_TTL_S

    # ── signals ──────────────────────────────────────────────────────────

    def active_signals(self, now=None) -> List[LagSignal]:
        """Open, in-window, tradeable-target signals. Dark targets are never
        emitted. Expired windows are pruned (and the mirror re-saved). Kill
        switch off = []."""
        if not cross_asset_lag_enabled():
            return []
        n = _f(now) if now is not None else self._now()
        if n is None:
            return []
        pruned = False
        out: List[LagSignal] = []
        sox = self.sox_confirm_armed(n)
        for target, w in list(self._open.items()):
            age = n - _f(w.get("ts"))
            conf = window_confidence(age, w.get("min_lag_s"), w.get("max_lag_s"))
            if conf is None:
                if age is not None and age > _f(w.get("max_lag_s")):
                    del self._open[target]
                    pruned = True
                continue
            if target not in self._tradeable:
                continue                    # dark target — abstain, not crash
            if sox and target in SEMI_CONFIRM_TARGETS:
                conf = min(1.0, conf + SOX_CONF_BOOST)
            out.append(LagSignal(
                target_symbol=target, direction=str(w.get("direction")),
                confidence=conf, source=str(w.get("source")), age_s=age,
                expires_ts=_f(w.get("ts")) + _f(w.get("max_lag_s")),
            ))
        if pruned:
            self._append_jsonl({"event": "propagation_expired", "now": n})
            self._save()
        return out

    def pair_signal(self, googl_chg, coin_chg) -> Optional[Tuple[str, float]]:
        """Engine-facing delegate of the module pair verdict."""
        return pair_signal(googl_chg, coin_chg)

    # ── persistence (atomic JSON mirror + append-only jsonl audit) ───────

    @staticmethod
    def _jsonl_for(persist_path: Optional[str]) -> Optional[str]:
        if not persist_path:
            return None
        p = str(persist_path)
        return p[:-5] + ".jsonl" if p.endswith(".json") else p + ".jsonl"

    def _now(self) -> Optional[float]:
        try:
            return _f(self._time())
        except Exception:
            return None

    def _state(self) -> dict:
        return {
            "version": 1,
            "saved_ts": self._now(),
            "open": list(self._open.values()),
            "last_moves": self._last_moves,
            "sox": self._sox,
        }

    def _save(self) -> None:
        if not self._persist_path:
            return
        try:
            d = os.path.dirname(self._persist_path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self._persist_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self._state(), fh)
            os.replace(tmp, self._persist_path)
        except Exception:
            pass                            # persistence must never raise

    def _append_jsonl(self, row: dict) -> None:
        if not self._jsonl_path:
            return
        try:
            d = os.path.dirname(self._jsonl_path)
            if d:
                os.makedirs(d, exist_ok=True)
            row = dict(row)
            row.setdefault("logged_ts", self._now())
            with open(self._jsonl_path, "a") as fh:
                fh.write(json.dumps(row) + "\n")
        except Exception:
            pass

    def _load(self) -> None:
        """Restore the mirror; in-window entries come back to life. Corrupt
        or missing state fails closed to an empty book — never raises."""
        if not self._persist_path:
            return
        try:
            with open(self._persist_path) as fh:
                st = json.load(fh)
        except Exception:
            return
        try:
            now = self._now()
            for w in st.get("open") or []:
                ts = _f(w.get("ts"))
                mx = _f(w.get("max_lag_s"))
                if ts is None or mx is None:
                    continue
                if now is not None and now > ts + mx:
                    continue                # expired while we were down
                target = base_name(w.get("target"))
                if not target:
                    continue
                self._open[target] = {
                    "target": target,
                    "source": base_name(w.get("source")),
                    "direction": str(w.get("direction")),
                    "move_pct": _f(w.get("move_pct")),
                    "ts": ts,
                    "min_lag_s": _f(w.get("min_lag_s")),
                    "max_lag_s": mx,
                }
            lm = st.get("last_moves")
            if isinstance(lm, dict):
                self._last_moves = {base_name(k): v for k, v in lm.items()
                                    if isinstance(v, dict)}
            sox = st.get("sox")
            if isinstance(sox, dict) and _f(sox.get("gap_pct")) is not None \
                    and _f(sox.get("ts")) is not None:
                self._sox = {"gap_pct": _f(sox.get("gap_pct")),
                             "ts": _f(sox.get("ts"))}
        except Exception:
            self._open, self._last_moves, self._sox = {}, {}, None


# ── Publisher (the only non-pure piece; all I/O injected) ────────────────────

SOX_PARAM_KEY = "e2:sox_gap_pct"
LAG_PARAM_PREFIX = "e2:lag:"
PAIR_PARAM_KEY = "e2:pair_googl_coin"


class CrossAssetLagPublisher:
    """Thin param_store publisher. set_param is the param_store-pattern
    callable (key, value, ttl); get_param reads the external SOX confirm
    key; clock is injectable for tests. Only armed signals are written —
    clearing is TTL expiry (param_store doctrine). Never raises."""

    def __init__(self, engine: CrossAssetLagEngine,
                 set_param: Callable[[str, object, float], object],
                 get_param: Optional[Callable[[str], object]] = None,
                 clock: Callable[[], float] = time.time):
        self._engine = engine
        self._set_param = set_param
        self._get_param = get_param
        self._clock = clock

    def pull_sox_confirm(self) -> bool:
        """Read e2:sox_gap_pct from the param store and feed the engine.
        Accepts {"gap_pct": f, "ts": f} or a bare float (fresh-at-read).
        Returns True when the confirm is armed after the pull."""
        if self._get_param is None:
            return False
        try:
            raw = self._get_param(SOX_PARAM_KEY)
        except Exception:
            return False
        try:
            now = self._clock()
        except Exception:
            now = None
        if isinstance(raw, dict):
            gap, ts = _f(raw.get("gap_pct")), _f(raw.get("ts"))
        else:
            gap, ts = _f(raw), now
        if gap is None or ts is None:
            return False
        return self._engine.ingest_sox_gap(gap, ts)

    def publish(self) -> int:
        """Refresh the SOX confirm, then write each armed signal to
        e2:lag:{target} with TTL = remaining window. Returns keys written.
        Kill switch off / any failure = best-effort, never raises."""
        if not cross_asset_lag_enabled():
            return 0
        written = 0
        try:
            now = self._clock()
        except Exception:
            now = None
        try:
            self.pull_sox_confirm()
        except Exception:
            pass
        try:
            for sig in self._engine.active_signals(now):
                ttl = sig.expires_ts - (now if now is not None else sig.expires_ts)
                if ttl <= 0:
                    continue
                self._set_param(LAG_PARAM_PREFIX + sig.target_symbol, {
                    "direction": sig.direction,
                    "confidence": sig.confidence,
                    "source": sig.source,
                    "age_s": sig.age_s,
                    "ts": now,
                }, ttl)
                written += 1
        except Exception:
            pass
        return written

    def publish_pair(self, googl_chg, coin_chg) -> int:
        """Write the rotation pair verdict when armed (TTL = 4h, the
        equity_flow rotation cadence). Returns keys written."""
        if not cross_asset_lag_enabled():
            return 0
        try:
            verdict = pair_signal(googl_chg, coin_chg)
            if verdict is None:
                return 0
            name, conf = verdict
            self._set_param(PAIR_PARAM_KEY, {
                "pair": name,
                "confidence": conf,
                "leg_ratio": PAIR_LEG_RATIO,   # 1:1 notional legs
                "ts": self._clock(),
            }, 4 * 3600.0)
            return 1
        except Exception:
            return 0
