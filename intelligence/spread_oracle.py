"""
intelligence/spread_oracle.py — Persistent spread-baseline oracle with the
3× pre-cascade warning (zero-I/O brain, 2026-09-26 Governor doctrine).

Doctrine (Governor, locked 2026-09-26): "The spread can also tell directions
and the next decision." Kyle/Glosten-Milgrom: the quoted spread is the
market maker's uncertainty price. A spread at ≥3× its own baseline is the
book pulling liquidity — a spike is imminent (pre-cascade warning). The
mid-drift over the last 60s votes direction: positive drift + widening =
longs trapped → DOWN vote (the chase is about to unwind); mirror for UP;
flat mids → None vote.

Sibling modules: intelligence/spread_signal.py (30-min rolling z-score —
THIS oracle PERSISTS the EWM baseline across restarts and adds the 3×
warning); intelligence/exec_formulas.py:127-158 corwin_schultz_spread (we
CONSUME spread estimates; we never recompute them).

Department shape (docs/DEPARTMENT_TEMPLATE.md): zero I/O beyond the
injected persist path; the caller owns cadence and telemetry.

Kill switch: config spread_oracle_enabled=False → cascade_warning returns
None and direction_vote returns None — the pre-module system bit-for-bit.
update() still tracks state.

Persistence: atomic JSON snapshot (tmp + os.replace), written at most once
per 5 minutes (throttle). Corrupt file = fresh start, never a crash.
Abstain-not-fabricate: < spread_oracle_min_obs (30) observations →
spread_multiple None; baseline 0 → None (no division).

Telemetry events (emitted by the coordinator): spread_oracle_cascade_warning,
spread_oracle_direction_vote.
"""
from __future__ import annotations

import json
import math
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

_PERSIST_MIN_INTERVAL_S = 300.0   # write at most once per 5 min
_MID_WINDOW_S = 60.0              # direction-vote drift window
_DEFAULT_HALFLIFE_S = 14400.0     # 4h EWM baseline halflife
_DEFAULT_MIN_OBS = 30
_DEFAULT_WARN_MULT = 3.0


def _f(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (math.inf, -math.inf):
        return None
    return v


@dataclass(frozen=True)
class SpreadCascadeWarning:
    """multiple ≥ warn threshold → the book is pulling liquidity."""
    symbol: str
    multiple: float                # current ÷ baseline
    spread_bps: float
    baseline_bps: float
    direction_vote: Optional[str]  # "up" | "down" | None
    ts: float


class SpreadOracle:
    """Per-symbol EWM spread baseline + persisted snapshot + 3× warning.

    persist_path: injected JSON snapshot path (None = in-memory only).
    clock: injectable for tests. Never raises."""

    def __init__(self, persist_path: Optional[str] = "logs/spread_oracle.json",
                 clock=time.time):
        self._persist_path = persist_path
        self._clock = clock
        # symbol -> {"baseline": f, "n": int, "last_ts": f}
        self._state: Dict[str, Dict[str, float]] = {}
        # symbol -> deque[(ts, mid)] for the 60s drift vote
        self._mids: Dict[str, Deque[Tuple[float, float]]] = {}
        self._last_write_ts: float = 0.0
        self._load()

    # ── ingest ───────────────────────────────────────────────────────────

    def update(self, symbol: str, spread_bps: float, mid_price: float,
               ts: float, *, halflife_s: float = _DEFAULT_HALFLIFE_S) -> None:
        """Feed one observation. EWM baseline with time-decayed alpha
        (alpha = 1 − 0.5^(dt/halflife)); mid ring kept for the drift vote.
        Persist throttled to one write per 5 min. Garbage fails closed."""
        s, m, t = _f(spread_bps), _f(mid_price), _f(ts)
        hl = _f(halflife_s) or _DEFAULT_HALFLIFE_S
        if not symbol or s is None or s < 0 or m is None or m <= 0 or t is None:
            return
        st = self._state.get(symbol)
        if st is None:
            st = {"baseline": s, "n": 1, "last_ts": t}
            self._state[symbol] = st
        else:
            dt = max(0.0, t - _f(st.get("last_ts")) or 0.0)
            alpha = 1.0 - 0.5 ** (dt / hl) if hl > 0 else 1.0
            st["baseline"] = (1.0 - alpha) * st["baseline"] + alpha * s
            st["n"] = int(st.get("n", 0)) + 1
            st["last_ts"] = t
        dq = self._mids.setdefault(symbol, deque())
        dq.append((t, m))
        cutoff = t - _MID_WINDOW_S
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        self._maybe_persist(t)

    # ── reads ────────────────────────────────────────────────────────────

    def spread_multiple(self, symbol: str, spread_bps: float,
                        *, min_obs: int = _DEFAULT_MIN_OBS) -> Optional[float]:
        """current ÷ baseline. None before min_obs observations (abstain,
        no fake precision); None on zero/negative baseline (no division)."""
        s = _f(spread_bps)
        if s is None:
            return None
        st = self._state.get(symbol)
        if st is None or int(st.get("n", 0)) < int(min_obs):
            return None
        base = _f(st.get("baseline"))
        if base is None or base <= 0:
            return None
        return s / base

    def direction_vote(self, symbol: str,
                       now_ts: Optional[float] = None) -> Optional[str]:
        """Mid-drift sign over the last 60s of mids. Positive drift → "up",
        negative → "down", flat/thin → None. (The warning flips the vote:
        positive drift + widening = longs trapped → DOWN — see
        cascade_warning.)"""
        dq = self._mids.get(symbol)
        if not dq:
            return None
        n = _f(now_ts) if now_ts is not None else _f(self._clock())
        if n is None:
            return None
        cutoff = n - _MID_WINDOW_S
        rows = [(t, m) for (t, m) in dq if t >= cutoff]
        if len(rows) < 2:
            return None
        drift = rows[-1][1] - rows[0][1]
        if drift > 0:
            return "up"
        if drift < 0:
            return "down"
        return None

    def cascade_warning(self, cfg, *, symbol: str, spread_bps: float,
                        now_ts: float) -> Optional[SpreadCascadeWarning]:
        """multiple ≥ spread_oracle_warn_multiple (default 3.0) → pre-cascade
        warning. Direction vote from mid drift: positive drift + widening =
        longs trapped → DOWN; negative drift + widening = shorts trapped →
        UP; flat → None vote. Kill switch off / thin data → None."""
        if not getattr(cfg, "spread_oracle_enabled", False):
            return None
        t = _f(now_ts)
        if t is None:
            return None
        min_obs = int(getattr(cfg, "spread_oracle_min_obs", _DEFAULT_MIN_OBS))
        mult = self.spread_multiple(symbol, spread_bps, min_obs=min_obs)
        if mult is None:
            return None
        warn_at = _f(getattr(cfg, "spread_oracle_warn_multiple", _DEFAULT_WARN_MULT))
        if warn_at is None or warn_at <= 0:
            warn_at = _DEFAULT_WARN_MULT
        if mult < warn_at:
            return None
        drift = self.direction_vote(symbol, t)
        # widening against the drift = the chased side is trapped
        if drift == "up":
            vote = "down"
        elif drift == "down":
            vote = "up"
        else:
            vote = None
        st = self._state.get(symbol) or {}
        return SpreadCascadeWarning(
            symbol=symbol, multiple=mult, spread_bps=float(spread_bps),
            baseline_bps=_f(st.get("baseline")) or 0.0,
            direction_vote=vote, ts=t,
        )

    # ── persistence (atomic tmp+replace, throttled 5 min) ────────────────

    def snapshot(self) -> Dict[str, Dict[str, float]]:
        return {k: dict(v) for k, v in self._state.items()}

    def _maybe_persist(self, now_ts: float) -> None:
        if not self._persist_path:
            return
        if now_ts - self._last_write_ts < _PERSIST_MIN_INTERVAL_S:
            return
        try:
            d = os.path.dirname(self._persist_path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self._persist_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"version": 1, "saved_ts": now_ts,
                           "state": self._state}, fh)
            os.replace(tmp, self._persist_path)
            self._last_write_ts = now_ts
        except Exception:
            pass                            # persistence must never raise

    def _load(self) -> None:
        """Restore the baseline snapshot. Corrupt/missing file = fresh
        start, never a crash. Observation counts ride along so a warm
        baseline stays warm across restarts."""
        if not self._persist_path:
            return
        try:
            with open(self._persist_path) as fh:
                doc = json.load(fh)
            state = doc.get("state")
            if not isinstance(state, dict):
                return
            for sym, row in state.items():
                if not isinstance(row, dict):
                    continue
                base, n, lts = _f(row.get("baseline")), row.get("n"), _f(row.get("last_ts"))
                if base is None or base < 0 or lts is None:
                    continue
                try:
                    n = int(n)
                except (TypeError, ValueError):
                    continue
                self._state[str(sym)] = {"baseline": base, "n": n, "last_ts": lts}
        except Exception:
            self._state = {}                # corrupt → fresh start
