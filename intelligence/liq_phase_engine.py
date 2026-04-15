"""
intelligence/liq_phase_engine.py — Adaptive Liquidation Phase Engine (ARIA v2.1)

Replaces static cascade thresholds with a rolling Z-score model that classifies
liquidation activity into four regimes: TRIGGER, EXPANSION, EXHAUSTION, AFTERMATH.

Design principles:
  - All thresholds are derived from rolling mean + std — nothing static.
  - Phase drives strategy type, size multiplier, and entry type selection.
  - The engine is read-only from downstream consumers — no mutation across calls.
  - Cross-venue lag is detected here and exposed for interpreter injection.

Phase model:
  QUIET      — Z < Z_TRIGGER, no structural bias.
  TRIGGER    — First elevated liq activity. Direction ambiguous. Reduce size, wait.
  EXPANSION  — Accelerating liq rate and elevated Z. Trade WITH momentum only.
  EXHAUSTION — Decelerating rate OR Z > Z_EXHAUSTION. Block momentum, enable reversal.
  AFTERMATH  — Silence >= AFTERMATH_SILENCE_S after EXHAUSTION. Recovery trades allowed.

Integration:
    engine = LiqPhaseEngine()

    # Feed each incoming liquidation event (call from process_liquidation):
    engine.on_event(symbol, notional_usd, direction, bybit_price, sodex_price)

    # Read phase state (call from interpreter._build_and_publish):
    snap = engine.get_snapshot(symbol)
    processed["liq_phase"]       = snap.phase.value
    processed["liq_zscore"]      = snap.zscore
    processed["liq_size_mult"]   = snap.size_mult
    processed["liq_entry_type"]  = snap.entry_type
    processed["liq_lag"]         = snap.cross_venue_lag
    processed["liq_lag_dir"]     = snap.cross_venue_dir

    # Execution guard (call from risk engine before size amplification):
    can_amp = engine.should_amplify(symbol, direction, coherence, funding_score)
"""

import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

import structlog

log = structlog.get_logger(__name__)

# ── Adaptive Z-score thresholds ───────────────────────────────────────────────
Z_TRIGGER    = 1.5   # Above this → TRIGGER phase
Z_EXPANSION  = 2.5   # Above this with positive velocity → EXPANSION
Z_EXHAUSTION = 4.0   # Above this → EXHAUSTION (cascade peaking)

# Event rate thresholds (events per 60s)
RATE_ELEVATED = 2.0
RATE_HOT      = 5.0

# Velocity: change in event rate over two 15s windows (events/s)
VELOCITY_EXPANSION_MIN  =  0.3   # must be accelerating to classify EXPANSION
VELOCITY_EXHAUSTION_MAX = -0.1   # decelerating → signal approaching exhaustion

# Aftermath: silence required after EXHAUSTION before recovery signal
AFTERMATH_SILENCE_S = 120.0   # 2 min quiet after exhaustion

# Rolling Z-score window
ZSCORE_WINDOW_EVENTS = 100
ZSCORE_WINDOW_AGE_S  = 3600   # 1h — old events dilute the baseline
ZSCORE_MIN_EVENTS    = 8      # Minimum events before Z-score is meaningful

# Cross-venue lag detection
XVENUE_PRICE_THRESHOLD = 0.0005   # 0.05% Bybit/SoDEX divergence = lag exists
XVENUE_BYBIT_STALE_S   = 30.0    # Bybit price older than this → no lag signal

# Amplification guard — coherence floor
AMPLIFY_COHERENCE_MIN  = 5.0
AMPLIFY_FUNDING_MIN    = 0.15   # Absolute SFS score required for funding alignment


class LiqPhase(str, Enum):
    QUIET      = "quiet"
    TRIGGER    = "trigger"
    EXPANSION  = "expansion"
    EXHAUSTION = "exhaustion"
    AFTERMATH  = "aftermath"


# Phase → execution parameters
PHASE_CONFIG: Dict[LiqPhase, Dict] = {
    LiqPhase.QUIET: {
        "allowed_strategies": ["any"],
        "size_mult": 1.0,
        "entry_type": "any",
    },
    LiqPhase.TRIGGER: {
        # Direction ambiguous — reduce size, no momentum entry
        "allowed_strategies": ["reversal_ready"],
        "size_mult": 0.6,
        "entry_type": "wait",
    },
    LiqPhase.EXPANSION: {
        # Momentum cascade live — only trade WITH the pressure
        "allowed_strategies": ["momentum"],
        "size_mult": 1.0,
        "entry_type": "breakout",
    },
    LiqPhase.EXHAUSTION: {
        # Cascade peaking / decelerating — BLOCK momentum, enable reversal
        "allowed_strategies": ["reversal"],
        "size_mult": 1.2,
        "entry_type": "reversal",
    },
    LiqPhase.AFTERMATH: {
        # Confirmed exhaustion + silence — highest-value recovery entries
        "allowed_strategies": ["reversal", "recovery"],
        "size_mult": 1.5,
        "entry_type": "reversal",
    },
}


@dataclass(frozen=True)
class LiqPhaseSnapshot:
    """Immutable phase state snapshot consumed by interpreter and risk engine."""
    symbol: str
    phase: LiqPhase
    zscore: float
    event_rate_per_min: float
    velocity: float
    last_direction: str        # "bearish" | "bullish" | "none"
    silence_s: float
    allowed_strategies: List[str]
    size_mult: float
    entry_type: str
    cross_venue_lag: bool
    cross_venue_dir: str       # "long" | "short" | "none"
    funding_aligned: bool
    computed_at: float


@dataclass
class _EventRecord:
    """Internal: single raw liquidation event for Z-score computation."""
    timestamp: float
    notional_usd: float
    direction: str


class LiqPhaseEngine:
    """
    Adaptive liquidation phase classifier.

    Designed for a single asyncio event loop — no locks needed.

    Phase transition rules (hysteretic):
      QUIET      → TRIGGER    : zscore >= Z_TRIGGER
      TRIGGER    → EXPANSION  : zscore >= Z_EXPANSION AND velocity >= VELOCITY_EXPANSION_MIN
      TRIGGER    → EXHAUSTION : zscore >= Z_EXHAUSTION (spike without acceleration)
      EXPANSION  → EXHAUSTION : velocity <= VELOCITY_EXHAUSTION_MAX OR zscore >= Z_EXHAUSTION
      EXHAUSTION → AFTERMATH  : silence >= AFTERMATH_SILENCE_S
      AFTERMATH  → QUIET      : silence >= AFTERMATH_SILENCE_S * 2 (signal expired)
      Any        → QUIET      : >10 min silence from non-AFTERMATH state
    """

    def __init__(self) -> None:
        self._events: Dict[str, deque]      = {}
        self._phase: Dict[str, LiqPhase]    = {}
        self._last_event_ts: Dict[str, float] = {}
        self._exhaustion_ts: Dict[str, float] = {}
        self._last_direction: Dict[str, str]  = {}
        self._funding_scores: Dict[str, float] = {}
        # {symbol: {bybit_price, bybit_ts, sodex_price, sodex_ts}}
        self._xvenue: Dict[str, Dict] = {}

    # ── Public API ─────────────────────────────────────────────────────────────

    def on_event(
        self,
        symbol: str,
        notional_usd: float,
        direction: str,
        bybit_price: float = 0.0,
        sodex_price: float = 0.0,
    ) -> None:
        """Feed a raw liquidation event from LiquidationSignalEngine.process_liquidation()."""
        now = time.time()
        if symbol not in self._events:
            self._events[symbol]   = deque(maxlen=ZSCORE_WINDOW_EVENTS)
            self._phase[symbol]    = LiqPhase.QUIET

        self._events[symbol].append(
            _EventRecord(timestamp=now, notional_usd=notional_usd, direction=direction)
        )
        # Only reset the silence timer for institutionally-sized liquidations (>= $60,000).
        # This is the quant-correct threshold: sub-$60k liquidations are continuous
        # background noise and carry no structural phase information.  True cascade
        # events (the ones that mark exhaustion) are $60k+ single events or clustered
        # bursts.  Resetting the 120s silence timer on $47 or $1k events would prevent
        # EXHAUSTION → AFTERMATH from ever firing.
        if notional_usd >= 60_000.0:
            self._last_event_ts[symbol] = now
        elif symbol not in self._last_event_ts:
            # First event ever — record it regardless so silence has a baseline
            self._last_event_ts[symbol] = now
        if direction in ("bearish", "bullish"):
            self._last_direction[symbol] = direction

        self._update_xvenue(symbol, bybit_price, sodex_price, now)
        self._advance_phase(symbol, now)

    def on_silence_tick(self, symbol: str) -> None:
        """Call from aftermath_loop (every 15s) to check AFTERMATH transition."""
        self._advance_phase(symbol, time.time())

    def get_snapshot(self, symbol: str) -> LiqPhaseSnapshot:
        """Returns current phase state. Safe to call at any time."""
        now    = time.time()
        phase  = self._phase.get(symbol, LiqPhase.QUIET)
        events = list(self._events.get(symbol, []))
        fresh  = [e for e in events if now - e.timestamp < ZSCORE_WINDOW_AGE_S]

        zscore   = self._compute_zscore(fresh)
        rate     = self._compute_rate(fresh, now)
        vel      = self._compute_velocity(fresh, now)
        last_dir = self._last_direction.get(symbol, "none")
        silence  = now - self._last_event_ts.get(symbol, now)
        lag, lag_dir = self._get_xvenue_lag(symbol, now)
        funding_aligned = self._check_funding_alignment(symbol, last_dir)

        cfg = PHASE_CONFIG[phase]
        return LiqPhaseSnapshot(
            symbol=symbol,
            phase=phase,
            zscore=round(zscore, 3),
            event_rate_per_min=round(rate, 2),
            velocity=round(vel, 3),
            last_direction=last_dir,
            silence_s=round(silence, 1),
            allowed_strategies=cfg["allowed_strategies"],
            size_mult=cfg["size_mult"],
            entry_type=cfg["entry_type"],
            cross_venue_lag=lag,
            cross_venue_dir=lag_dir,
            funding_aligned=funding_aligned,
            computed_at=now,
        )

    def should_amplify(
        self,
        symbol: str,
        direction: str,
        coherence: float,
        funding_score: float,
    ) -> bool:
        """
        Execution guard: True ONLY when all three conditions are satisfied.

        Condition 1 — Price confirmation:
          Bybit has moved in the trade direction and SoDEX hasn't caught up yet
          (cross-venue lag exists and lag direction matches trade direction).

        Condition 2 — Funding aligned:
          short + SFS > +0.15 (longs paying) = aligned
          long  + SFS < -0.15 (shorts paying) = aligned

        Condition 3 — Coherence >= AMPLIFY_COHERENCE_MIN (5.0)

        If False: caller uses base size_mult only. Trade is never blocked.
        """
        snap = self.get_snapshot(symbol)
        price_confirmed = snap.cross_venue_lag and snap.cross_venue_dir == direction
        funding_aligned = (
            (direction == "short" and funding_score >  AMPLIFY_FUNDING_MIN) or
            (direction == "long"  and funding_score < -AMPLIFY_FUNDING_MIN)
        )
        coherence_high = coherence >= AMPLIFY_COHERENCE_MIN

        result = price_confirmed and funding_aligned and coherence_high
        log.debug(
            "amplify_check",
            symbol=symbol,
            direction=direction,
            price_confirmed=price_confirmed,
            funding_aligned=funding_aligned,
            coherence_high=coherence_high,
            result=result,
        )
        return result

    def is_momentum_blocked(self, symbol: str) -> bool:
        """True during EXHAUSTION — momentum trades must be blocked."""
        return self._phase.get(symbol, LiqPhase.QUIET) == LiqPhase.EXHAUSTION

    def allows_strategy(self, symbol: str, strategy_type: str) -> bool:
        """
        Check if strategy_type is permitted in the current phase.
        strategy_type: "momentum" | "reversal" | "recovery"
        """
        phase   = self._phase.get(symbol, LiqPhase.QUIET)
        allowed = PHASE_CONFIG[phase]["allowed_strategies"]
        return "any" in allowed or strategy_type in allowed

    def update_bybit_price(self, symbol: str, price: float) -> None:
        """Update Bybit reference price for lag detection. Call on every Bybit tick."""
        if price > 0:
            xv = self._xvenue.setdefault(symbol, {})
            xv["bybit_price"] = price
            xv["bybit_ts"]    = time.time()

    def update_sodex_price(self, symbol: str, price: float) -> None:
        """Update SoDEX mark price. Call from mark_price_store updates."""
        if price > 0:
            xv = self._xvenue.setdefault(symbol, {})
            xv["sodex_price"] = price
            xv["sodex_ts"]    = time.time()

    def update_funding_score(self, symbol: str, sfs_score: float) -> None:
        """Update SFS score for funding alignment checks. Call from interpreter."""
        self._funding_scores[symbol] = sfs_score

    # ── Phase state machine ────────────────────────────────────────────────────

    def _advance_phase(self, symbol: str, now: float) -> None:
        events = list(self._events.get(symbol, []))
        fresh  = [e for e in events if now - e.timestamp < ZSCORE_WINDOW_AGE_S]
        zscore = self._compute_zscore(fresh)
        vel    = self._compute_velocity(fresh, now)
        silence = now - self._last_event_ts.get(symbol, now)
        cur    = self._phase.get(symbol, LiqPhase.QUIET)

        if cur == LiqPhase.QUIET:
            if zscore >= Z_TRIGGER:
                self._set_phase(symbol, LiqPhase.TRIGGER, zscore, vel)

        elif cur == LiqPhase.TRIGGER:
            if zscore >= Z_EXHAUSTION:
                # Spike without acceleration → EXHAUSTION (thin market fake pump)
                self._set_phase(symbol, LiqPhase.EXHAUSTION, zscore, vel)
                self._exhaustion_ts[symbol] = now
            elif zscore >= Z_EXPANSION and vel >= VELOCITY_EXPANSION_MIN:
                self._set_phase(symbol, LiqPhase.EXPANSION, zscore, vel)
            elif silence > 600.0:
                self._set_phase(symbol, LiqPhase.QUIET, zscore, vel)

        elif cur == LiqPhase.EXPANSION:
            if vel <= VELOCITY_EXHAUSTION_MAX or zscore >= Z_EXHAUSTION:
                self._set_phase(symbol, LiqPhase.EXHAUSTION, zscore, vel)
                self._exhaustion_ts[symbol] = now
            elif silence > 300.0:
                self._set_phase(symbol, LiqPhase.QUIET, zscore, vel)

        elif cur == LiqPhase.EXHAUSTION:
            exh_ts = self._exhaustion_ts.get(symbol, now)
            if now - exh_ts >= AFTERMATH_SILENCE_S and silence >= AFTERMATH_SILENCE_S:
                self._set_phase(symbol, LiqPhase.AFTERMATH, zscore, vel)

        elif cur == LiqPhase.AFTERMATH:
            exh_ts = self._exhaustion_ts.get(symbol, now)
            if now - exh_ts >= AFTERMATH_SILENCE_S * 2:
                self._set_phase(symbol, LiqPhase.QUIET, zscore, vel)

    def _set_phase(self, symbol: str, new: LiqPhase,
                   zscore: float, vel: float) -> None:
        old = self._phase.get(symbol, LiqPhase.QUIET)
        if old != new:
            self._phase[symbol] = new
            log.info(
                "liq_phase_transition",
                symbol=symbol,
                from_phase=old.value,
                to_phase=new.value,
                zscore=round(zscore, 2),
                velocity=round(vel, 3),
                allowed=PHASE_CONFIG[new]["allowed_strategies"],
                size_mult=PHASE_CONFIG[new]["size_mult"],
            )

    # ── Statistical helpers ────────────────────────────────────────────────────

    @staticmethod
    def _compute_zscore(events: List[_EventRecord]) -> float:
        """Z-score of the most recent notional vs the rolling window."""
        if len(events) < ZSCORE_MIN_EVENTS:
            return 0.0
        notionals = [e.notional_usd for e in events]
        mu  = sum(notionals) / len(notionals)
        var = sum((x - mu) ** 2 for x in notionals) / len(notionals)
        std = math.sqrt(var) + 1e-6
        return (notionals[-1] - mu) / std

    @staticmethod
    def _compute_rate(events: List[_EventRecord], now: float,
                      window_s: float = 60.0) -> float:
        """Events per minute over the last window_s seconds."""
        recent = [e for e in events if now - e.timestamp <= window_s]
        return len(recent) * (60.0 / window_s)

    @staticmethod
    def _compute_velocity(events: List[_EventRecord], now: float) -> float:
        """
        Second derivative of event rate.
        Δ(count in last 15s) − Δ(count in prior 15s), normalised by window.
        Positive = accelerating, negative = decelerating.
        """
        if len(events) < 3:
            return 0.0
        wa = sum(1 for e in events if now - 30.0 < e.timestamp <= now - 15.0)
        wb = sum(1 for e in events if now - 15.0 < e.timestamp <= now)
        return (wb - wa) / 15.0

    # ── Cross-venue lag ────────────────────────────────────────────────────────

    def _update_xvenue(self, symbol: str, bybit_price: float,
                       sodex_price: float, now: float) -> None:
        xv = self._xvenue.setdefault(symbol, {})
        if bybit_price > 0:
            xv["bybit_price"] = bybit_price
            xv["bybit_ts"]    = now
        if sodex_price > 0:
            xv["sodex_price"] = sodex_price
            xv["sodex_ts"]    = now

    def _get_xvenue_lag(self, symbol: str, now: float) -> Tuple[bool, str]:
        """
        Detect Bybit → SoDEX price lag.

        Lag exists when:
          |bybit − sodex| / bybit > XVENUE_PRICE_THRESHOLD (0.05%)
          AND Bybit price is fresh (< XVENUE_BYBIT_STALE_S old)

        lag_dir: "long"  = bybit above sodex → sodex will catch up upward
                 "short" = bybit below sodex → sodex will catch up downward
        """
        xv  = self._xvenue.get(symbol, {})
        bp  = xv.get("bybit_price", 0.0)
        bts = xv.get("bybit_ts",    0.0)
        sp  = xv.get("sodex_price", 0.0)

        if bp <= 0 or sp <= 0 or now - bts > XVENUE_BYBIT_STALE_S:
            return False, "none"

        div = (bp - sp) / bp
        if abs(div) < XVENUE_PRICE_THRESHOLD:
            return False, "none"

        return True, ("long" if div > 0 else "short")

    def _check_funding_alignment(self, symbol: str, last_liq_dir: str) -> bool:
        """
        Funding alignment: does the SFS score support the expected trade direction?
        bearish liq (longs liq'd → expect short entries) requires SFS > 0 (longs paying).
        bullish liq (shorts liq'd → expect long entries) requires SFS < 0 (shorts paying).
        """
        score = self._funding_scores.get(symbol, 0.0)
        if last_liq_dir == "bearish":
            return score > AMPLIFY_FUNDING_MIN
        if last_liq_dir == "bullish":
            return score < -AMPLIFY_FUNDING_MIN
        return False


# Module-level singleton — import and use directly
liq_phase_engine = LiqPhaseEngine()
