"""Mover relief v2 — two-stage per-symbol veto-relief detector (QUIET -> ALERT -> RELIEF).

Incident context (2026-09-17, UNI): the legacy mover radar flagged
"mover_radar_blocked" 43 times over a 7-hour block loop, but the veto-relief
armed at 15:14:10Z — AFTER the +17.49% move. Relief that arms post-move is
worthless. The root defect was a confirmation window anchored to a fixed
start; this module's confirmation window is ROLLING (always the LAST
RELIEF_WINDOW_S seconds, never fixed-start), so relief arms as soon as the
move's own signature appears inside the window.

Design (ALL thresholds are UNSOURCED starting hypotheses — module constants
below, to be calibrated against shadow data before any live wiring):

  Stage 1 ALERT: blocks_per_hour >= ALERT_BLOCKS_PER_HOUR over a rolling
      ALERT_WINDOW_S. Arms for ALERT_TTL_S; every poll where the condition
      still holds refreshes the TTL (re-arm refreshes).
  Stage 2 RELIEF: state == ALERT plus >= 2 of 3 confirmations over the
      rolling RELIEF_WINDOW_S:
        price   — abs(price_delta_pct) >= RELIEF_PRICE_MOVE_PCT
        volume  — mean(volume in window) / baseline_volume >= RELIEF_VOLUME_MULT
        oi      — abs(oi_delta_pct) >= RELIEF_OI_DELTA
      Arms for RELIEF_TTL_S; re-confirmation while active refreshes.
      On every FRESH arm a single JSON log line is printed
      (event "mover_relief_armed" with symbol, confirmations, window stats).

baseline_volume per symbol = median of retained volume samples OLDER than
the relief window (starter definition — document: a sustained 2x regime
shift will eventually inflate the baseline and dilute the confirmation;
acceptable for a starter, revisit with percentile-of-day baselines).

Fail-silent: no I/O except the arming log line; every public method swallows
exceptions. Stdlib only.
"""
from __future__ import annotations

import json
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from intelligence import kill_switch

# ---------------------------------------------------------------------------
# Thresholds — UNSOURCED STARTING HYPOTHESES (2026-09-17). Not calibrated.
# ---------------------------------------------------------------------------
ALERT_BLOCKS_PER_HOUR: int = 4        # blocks inside ALERT_WINDOW_S to arm ALERT
ALERT_WINDOW_S: float = 3600.0        # rolling block-count window
ALERT_TTL_S: float = 7200.0           # ALERT lifetime after last qualifying poll
RELIEF_WINDOW_S: float = 900.0        # ROLLING confirmation window (last 15 min)
RELIEF_PRICE_MOVE_PCT: float = 0.012  # |price delta| over the window
RELIEF_VOLUME_MULT: float = 1.8       # mean(window volume) / baseline volume
RELIEF_OI_DELTA: float = 0.005        # |OI delta| over the window
RELIEF_TTL_S: float = 3600.0          # RELIEF lifetime after last confirmation

# Retention: keep the longest window plus one relief window of margin so
# volume samples older than the relief window survive for the baseline.
_RETENTION_S: float = max(ALERT_WINDOW_S, RELIEF_WINDOW_S) + RELIEF_WINDOW_S

STATE_QUIET = "QUIET"
STATE_ALERT = "ALERT"
STATE_RELIEF = "RELIEF"


@dataclass
class _SymState:
    """Per-symbol rolling event buffers and stage TTLs (internal)."""

    blocks: Deque[float] = field(default_factory=deque)
    prices: Deque[Tuple[float, float]] = field(default_factory=deque)
    volumes: Deque[Tuple[float, float]] = field(default_factory=deque)
    ois: Deque[Tuple[float, float]] = field(default_factory=deque)
    alert_until: float = 0.0
    relief_until: float = 0.0


def relief_v2_enabled() -> bool:
    """Kill-switch helper: True when the Governor has armed mover_relief_v2.

    Fail-silent — any kill-switch read error returns False (v2 off).
    """
    try:
        return bool(kill_switch.enabled("mover_relief_v2"))
    except Exception:
        return False


class MoverReliefDetector:
    """Two-stage per-symbol state machine: QUIET -> ALERT -> RELIEF.

    Feed it via update(); query via state() / relief_active(). Pure brain,
    zero I/O except the JSON arming log. All methods fail-silent.
    """

    def __init__(self) -> None:
        self._symbols: Dict[str, _SymState] = {}

    # ------------------------------------------------------------------ API
    def update(
        self,
        symbol: str,
        now: float,
        *,
        blocked: bool,
        price: Optional[float] = None,
        volume: Optional[float] = None,
        oi: Optional[float] = None,
    ) -> None:
        """Ingest one poll tick for ``symbol`` and advance the state machine.

        ``blocked`` marks a mover_radar_blocked observation at ``now``;
        price/volume/oi are the current market samples (any may be None —
        missing legs simply cannot confirm). Never raises.
        """
        try:
            self._update_inner(str(symbol), float(now), bool(blocked),
                               price, volume, oi)
        except Exception:
            pass  # fail-silent

    def state(self, symbol: str, now: float) -> str:
        """Effective stage at ``now``: "QUIET" | "ALERT" | "RELIEF"."""
        try:
            st = self._symbols.get(str(symbol))
            if st is None:
                return STATE_QUIET
            t = float(now)
            if st.relief_until > t:
                return STATE_RELIEF
            if st.alert_until > t:
                return STATE_ALERT
            return STATE_QUIET
        except Exception:
            return STATE_QUIET

    def relief_active(self, symbol: str, now: float) -> bool:
        """True when the symbol is in RELIEF at ``now`` (the veto-relief gate)."""
        try:
            return self.state(symbol, now) == STATE_RELIEF
        except Exception:
            return False

    # -------------------------------------------------------------- internals
    def _update_inner(
        self,
        symbol: str,
        now: float,
        blocked: bool,
        price: Optional[float],
        volume: Optional[float],
        oi: Optional[float],
    ) -> None:
        st = self._symbols.setdefault(symbol, _SymState())
        self._prune(st, now)

        if blocked:
            st.blocks.append(now)
        if price is not None:
            p = float(price)
            if p > 0.0:
                st.prices.append((now, p))
        if volume is not None:
            v = float(volume)
            if v >= 0.0:
                st.volumes.append((now, v))
        if oi is not None:
            o = float(oi)
            if o > 0.0:
                st.ois.append((now, o))

        # Stage 1 — ALERT: rolling block count; re-arm refreshes the TTL.
        cutoff = now - ALERT_WINDOW_S
        recent_blocks = sum(1 for ts in st.blocks if ts > cutoff)
        if recent_blocks >= ALERT_BLOCKS_PER_HOUR:
            st.alert_until = now + ALERT_TTL_S

        # Stage 2 — RELIEF: requires ALERT plus >= 2 of 3 rolling confirmations.
        if st.alert_until > now:
            confirmations, stats = self._confirmations(st, now)
            if len(confirmations) >= 2:
                if st.relief_until <= now:
                    self._log_armed(symbol, now, confirmations, stats)
                st.relief_until = now + RELIEF_TTL_S

    @staticmethod
    def _prune(st: _SymState, now: float) -> None:
        cutoff = now - _RETENTION_S
        while st.blocks and st.blocks[0] <= cutoff:
            st.blocks.popleft()
        for buf in (st.prices, st.volumes, st.ois):
            while buf and buf[0][0] <= cutoff:
                buf.popleft()

    def _confirmations(
        self, st: _SymState, now: float
    ) -> Tuple[List[str], Dict[str, float]]:
        """Evaluate the 3 rolling-window confirmations. Returns (names, stats)."""
        wstart = now - RELIEF_WINDOW_S
        names: List[str] = []
        stats: Dict[str, float] = {}

        pts = [(t, p) for t, p in st.prices if t > wstart]
        if len(pts) >= 2 and pts[0][1] > 0.0:
            price_delta = (pts[-1][1] - pts[0][1]) / pts[0][1]
            stats["price_delta_pct"] = round(price_delta, 6)
            if abs(price_delta) >= RELIEF_PRICE_MOVE_PCT:
                names.append("price")

        vwin = [v for t, v in st.volumes if t > wstart]
        vbase = [v for t, v in st.volumes if t <= wstart]
        if vwin and vbase:
            baseline = statistics.median(vbase)
            if baseline > 0.0:
                vol_ratio = (sum(vwin) / len(vwin)) / baseline
                stats["volume_ratio"] = round(vol_ratio, 4)
                stats["baseline_volume"] = round(baseline, 4)
                if vol_ratio >= RELIEF_VOLUME_MULT:
                    names.append("volume")

        ots = [(t, o) for t, o in st.ois if t > wstart]
        if len(ots) >= 2 and ots[0][1] > 0.0:
            oi_delta = (ots[-1][1] - ots[0][1]) / ots[0][1]
            stats["oi_delta_pct"] = round(oi_delta, 6)
            if abs(oi_delta) >= RELIEF_OI_DELTA:
                names.append("oi")

        return names, stats

    @staticmethod
    def _log_armed(
        symbol: str,
        now: float,
        confirmations: List[str],
        stats: Dict[str, float],
    ) -> None:
        """The single permitted I/O: one JSON line per FRESH relief arm."""
        try:
            print(json.dumps({
                "event": "mover_relief_armed",
                "symbol": symbol,
                "ts": now,
                "confirmations": confirmations,
                "window_s": RELIEF_WINDOW_S,
                "stats": stats,
            }, sort_keys=True))
        except Exception:
            pass
