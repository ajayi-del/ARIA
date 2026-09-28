"""
data/session_vwap.py — E3 session-VWAP accumulator for the SoDEX equity
volume generator (2026-09-26 E3 build, Governor directive).

The candle buffer is maxlen 200 (~3.3h of 1m bars) — INSUFFICIENT for a
full 6.5h session VWAP. This accumulator persists cumulative typical-price
x volume / volume state across buffer eviction and across restarts
(logs/session_vwap.json atomic + logs/session_vwap.jsonl snapshots).

Doctrine:
  1. SESSION — 13:30-20:00 UTC (the E3 core window). The VWAP resets at
     each session open; it is NOT a rolling window. Only bars inside
     [anchor, anchor + 6.5h) accumulate.
  2. PURE CORE — the update math is a module-level pure function
     (vwap_accumulate); tests never touch disk. The SessionVwap object is
     a stateful shell around it; SessionVwapStore is the only piece that
     does I/O (atomic JSON + JSONL append, injected clock, fail-closed —
     never raises into the caller's loop).
  3. STALE — no bar for > 5 min during the session => is_stale() true =>
     callers abstain (a stale VWAP is a lying anchor).
  4. KILL SWITCH — env SESSION_VWAP_ENABLED default "true"; false =>
     store.update() no-ops (pre-module system bit-for-bit).

Department-template shape (docs/DEPARTMENT_TEMPLATE.md): all market reads
arrive as arguments; state leaves as plain dicts. main.py owns the wiring;
the splice is coordinator-owned.
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable, Dict, Iterable, Optional, Tuple


# ── Kill switch ───────────────────────────────────────────────────────────────

def session_vwap_enabled() -> bool:
    """Module-level kill switch (env SESSION_VWAP_ENABLED, default true).
    False = SessionVwapStore.update() no-ops."""
    return os.environ.get("SESSION_VWAP_ENABLED", "true").strip().lower() in (
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


# ── Session geometry ──────────────────────────────────────────────────────────

SESSION_OPEN_UTC_S = 13 * 3600 + 30 * 60      # 13:30 UTC
SESSION_LEN_S = 6.5 * 3600                    # 13:30 -> 20:00 UTC (23400s)
DAY_S = 86400.0
STALE_S = 300.0                               # no bar > 5 min => stale


def session_anchor_for(ts) -> Optional[float]:
    """Epoch of the 13:30 UTC anchor of the session containing/most-recent
    to ts. A ts before today's 13:30 belongs to yesterday's anchor."""
    t = _f(ts)
    if t is None:
        return None
    day_floor = t - (t % DAY_S)
    anchor = day_floor + SESSION_OPEN_UTC_S
    if t < anchor:
        anchor -= DAY_S
    return anchor


def in_session(ts) -> bool:
    """True strictly inside [13:30, 20:00) UTC."""
    t = _f(ts)
    if t is None:
        return False
    anchor = session_anchor_for(t)
    if anchor is None:
        return False
    return anchor <= t < anchor + SESSION_LEN_S


# ── Pure update math ──────────────────────────────────────────────────────────

def vwap_accumulate(cum_pv, cum_vol, high, low, close, volume
                    ) -> Tuple[float, float]:
    """The pure core: typical price x volume accumulation.
    typical = (high + low + close) / 3; returns (cum_pv', cum_vol').
    Non-positive volume contributes nothing to either leg (fail-closed:
    garbage numerics coerce to 0 contribution, never raise)."""
    pv, vol = _f(cum_pv) or 0.0, _f(cum_vol) or 0.0
    h, l, c, v = _f(high), _f(low), _f(close), _f(volume)
    if h is None or l is None or c is None or v is None or v <= 0:
        return (pv, vol)
    typical = (h + l + c) / 3.0
    return (pv + typical * v, vol + v)


# ── Per-symbol accumulator (stateful shell, still zero-I/O) ──────────────────

class SessionVwap:
    """One symbol's session VWAP. Resets at the session anchor; accumulates
    only bars strictly inside [anchor, anchor + SESSION_LEN_S)."""

    def __init__(self, symbol: str, session_anchor: float):
        self.symbol = str(symbol)
        self.anchor = float(session_anchor)
        self.cum_pv = 0.0
        self.cum_vol = 0.0
        self.bars = 0
        self.last_bar_ts: Optional[float] = None

    def update(self, bar_ts, high, low, close, volume) -> bool:
        """Accept a 1m bar. Returns True when the bar was inside the session
        window and counted. Out-of-window bars are ignored (the VWAP is a
        session anchor, not a rolling window); garbage timestamps fail
        closed (ignored)."""
        t = _f(bar_ts)
        if t is None:
            return False
        if not (self.anchor <= t < self.anchor + SESSION_LEN_S):
            return False
        self.bars += 1
        self.last_bar_ts = t if self.last_bar_ts is None else max(self.last_bar_ts, t)
        self.cum_pv, self.cum_vol = vwap_accumulate(
            self.cum_pv, self.cum_vol, high, low, close, volume)
        return True

    def vwap(self) -> Optional[float]:
        """Cumulative session VWAP; None before any volume accumulates."""
        if self.cum_vol <= 0:
            return None
        return self.cum_pv / self.cum_vol

    def deviation_pct(self, price) -> Optional[float]:
        """(price - vwap) / vwap x 100, signed. None when either side is
        missing (abstain — never invent an anchor)."""
        p = _f(price)
        v = self.vwap()
        if p is None or v is None or v <= 0:
            return None
        return (p - v) / v * 100.0

    def bars_seen(self) -> int:
        return self.bars

    def is_stale(self, now=None) -> bool:
        """No bar for > STALE_S (or no bar ever) => stale => callers
        abstain. A stale VWAP is a lying anchor."""
        n = _f(now) if now is not None else time.time()
        if n is None:
            return True
        if self.last_bar_ts is None:
            return True
        return (n - self.last_bar_ts) > STALE_S

    # ── persistence shape ──
    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "anchor": self.anchor,
                "cum_pv": self.cum_pv, "cum_vol": self.cum_vol,
                "bars": self.bars, "last_bar_ts": self.last_bar_ts}

    @classmethod
    def from_dict(cls, row: dict) -> Optional["SessionVwap"]:
        """Tolerant restore; garbage rows -> None (skipped, never raise)."""
        try:
            sym = str(row["symbol"])
            anchor = float(row["anchor"])
            acc = cls(sym, anchor)
            acc.cum_pv = float(row.get("cum_pv") or 0.0)
            acc.cum_vol = float(row.get("cum_vol") or 0.0)
            acc.bars = int(row.get("bars") or 0)
            lb = row.get("last_bar_ts")
            acc.last_bar_ts = None if lb is None else float(lb)
            return acc
        except Exception:
            return None


# ── Store (the only piece that does I/O; all of it injected/fail-closed) ─────

class SessionVwapStore:
    """Owns one SessionVwap per symbol, auto-rolls at session boundaries,
    persists cumulative state so the VWAP survives buffer eviction and
    restarts. log_dir holds session_vwap.json (atomic state) +
    session_vwap.jsonl (snapshot rows). time_fn is the injectable clock."""

    def __init__(self, symbols: Iterable[str], log_dir: str = "logs",
                 time_fn: Callable[[], float] = time.time):
        self._symbols = [str(s) for s in symbols]
        self._log_dir = str(log_dir)
        self._time_fn = time_fn
        self._accs: Dict[str, SessionVwap] = {}

    # ── paths ──
    def _state_path(self) -> str:
        return os.path.join(self._log_dir, "session_vwap.json")

    def _jsonl_path(self) -> str:
        return os.path.join(self._log_dir, "session_vwap.jsonl")

    # ── clock ──
    def _now(self) -> float:
        try:
            t = float(self._time_fn())
            return t
        except Exception:
            return time.time()

    # ── public API ──
    def get(self, symbol) -> Optional[SessionVwap]:
        return self._accs.get(str(symbol))

    def update(self, symbol, bar_ts, high, low, close, volume) -> bool:
        """Route a bar into the symbol's accumulator, rolling the session
        when the bar's anchor differs from the held one. Kill switch off /
        unknown symbol / out-of-session bar / any I/O failure => False,
        never raises."""
        if not session_vwap_enabled():
            return False
        sym = str(symbol)
        if sym not in self._symbols:
            return False
        anchor = session_anchor_for(bar_ts)
        if anchor is None:
            return False
        acc = self._accs.get(sym)
        if acc is None or acc.anchor != anchor:
            acc = SessionVwap(sym, anchor)      # session boundary: fresh reset
            self._accs[sym] = acc
        accepted = acc.update(bar_ts, high, low, close, volume)
        if not accepted:
            return False
        self._persist(sym, acc)
        return True

    def load(self) -> int:
        """Restore cumulative state from disk at boot. Returns the number of
        symbols restored. Missing/corrupt file -> 0 (fail-closed, never
        raises)."""
        try:
            with open(self._state_path()) as f:
                doc = json.load(f)
            rows = doc.get("symbols") if isinstance(doc, dict) else None
            if not isinstance(rows, dict):
                return 0
            n = 0
            for sym, row in rows.items():
                if sym not in self._symbols:
                    continue
                acc = SessionVwap.from_dict(row)
                if acc is not None:
                    self._accs[sym] = acc
                    n += 1
            return n
        except Exception:
            return 0

    # ── persistence (tmp+os.replace idiom; one-bad-line JSONL append) ──
    def _persist(self, symbol: str, acc: SessionVwap) -> None:
        """Atomic JSON state + one JSONL snapshot row. All failures swallowed
        — telemetry must never break the bar path."""
        try:
            os.makedirs(self._log_dir, exist_ok=True)
        except Exception:
            pass
        now = self._now()
        try:
            payload = {"ts": now,
                       "symbols": {s: a.to_dict() for s, a in self._accs.items()}}
            tmp = self._state_path() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, self._state_path())   # atomic
        except Exception:
            pass
        try:
            row = acc.to_dict()
            row["ts"] = now
            row["vwap"] = acc.vwap()
            with open(self._jsonl_path(), "a") as f:
                f.write(json.dumps(row) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            pass
