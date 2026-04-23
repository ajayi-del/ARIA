"""
intelligence/signal_guard.py — Signal Quality Guards v1.0

Eliminates 7 profit-killing gaps identified from live logs (2026-04-22):

  Gap 1 — Kingdom contamination:      Kingdom publish moved post-sizing in main.py
  Gap 2 — Directional alpha bias:     rolling 20-trade win-rate veto per (symbol, dir)
  Gap 3 — Large-cap alpha floor:      per-symbol win rate → elevated coherence floor
  Gap 4 — Funding carry headwind:     veto longs when funding < -1.5%, shorts > +1.5%
  Gap 5 — Post-block signal queue:    accumulate conviction during earnings blocks,
                                       fire with 1.15× size boost when block lifts
  Gap 6 — Regime stability:           suppress signal generation when stuck in
                                       transitioning + conf≤0.3 for > 3 min
  Gap 7 — (position trailing):        flagged for future executor integration

SignalGuard is the single façade instantiated once in main().
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)


# ── DirectionalAlphaTracker ────────────────────────────────────────────────────

class DirectionalAlphaTracker:
    """
    Rolling win-rate tracker per (symbol, direction) and per symbol.

    Directional veto:  rolling_win_rate(sym, dir, n=20) < 0.38 and n >= 10
    Alpha floor:       rolling_win_rate(sym, n=20) maps to extra coherence required:
                         ≥ 0.50 → +0.0 (Tier A: normal)
                         38-50% → +1.5 (Tier B: elevated)
                         < 38%  → +3.0 (Tier C: near-suspend)
    """

    WIN_RATE_VETO_THRESHOLD  = 0.38
    ALPHA_FLOOR_TIER_B       = 0.50
    ALPHA_FLOOR_TIER_C       = 0.38
    MIN_TRADES               = 10   # minimum sample before any veto activates
    WINDOW                   = 20   # rolling window

    def __init__(self) -> None:
        self._dir_history: Dict[Tuple[str, str], Deque[bool]] = {}
        self._sym_history: Dict[str, Deque[bool]] = {}

    def record_trade(self, symbol: str, direction: str, pnl: float) -> None:
        won = pnl > 0
        key = (symbol, direction)
        if key not in self._dir_history:
            self._dir_history[key] = deque(maxlen=self.WINDOW)
        self._dir_history[key].append(won)

        if symbol not in self._sym_history:
            self._sym_history[symbol] = deque(maxlen=self.WINDOW)
        self._sym_history[symbol].append(won)

    def _win_rate(self, history: Deque[bool]) -> Optional[float]:
        if len(history) < self.MIN_TRADES:
            return None
        return sum(history) / len(history)

    def should_veto_direction(self, symbol: str, direction: str) -> bool:
        key = (symbol, direction)
        hist = self._dir_history.get(key)
        if hist is None:
            return False
        wr = self._win_rate(hist)
        if wr is None:
            return False
        if wr < self.WIN_RATE_VETO_THRESHOLD:
            logger.info("directional_veto_active",
                        symbol=symbol, direction=direction,
                        win_rate=round(wr, 3),
                        threshold=self.WIN_RATE_VETO_THRESHOLD,
                        trades=len(hist))
            return True
        return False

    def get_coherence_floor_add(self, symbol: str) -> float:
        """Extra coherence required based on per-symbol rolling win rate."""
        hist = self._sym_history.get(symbol)
        if hist is None:
            return 0.0
        wr = self._win_rate(hist)
        if wr is None:
            return 0.0
        if wr < self.ALPHA_FLOOR_TIER_C:
            return 3.0
        if wr < self.ALPHA_FLOOR_TIER_B:
            return 1.5
        return 0.0

    def stats(self, symbol: str) -> dict:
        out: dict = {}
        for d in ("long", "short"):
            h = self._dir_history.get((symbol, d))
            if h:
                wr = self._win_rate(h)
                out[d] = {"win_rate": round(wr, 3) if wr is not None else None, "n": len(h)}
        sh = self._sym_history.get(symbol)
        if sh:
            wr = self._win_rate(sh)
            out["combined"] = {"win_rate": round(wr, 3) if wr is not None else None, "n": len(sh)}
        return out


# ── FundingDirectionalVeto ─────────────────────────────────────────────────────

class FundingDirectionalVeto:
    """
    Vetoes trades that go against the carry direction.

      funding_rate < -THRESHOLD → shorts earn → veto longs
      funding_rate > +THRESHOLD → longs earn  → veto shorts
    """

    THRESHOLD = 1.5  # minimum |funding_rate|% to activate

    def should_veto(self, direction: str, funding_rate: float) -> bool:
        if abs(funding_rate) < self.THRESHOLD:
            return False
        if direction == "long" and funding_rate < -self.THRESHOLD:
            logger.info("funding_carry_veto",
                        direction=direction, funding_rate=round(funding_rate, 3),
                        reason="long_against_negative_carry")
            return True
        if direction == "short" and funding_rate > self.THRESHOLD:
            logger.info("funding_carry_veto",
                        direction=direction, funding_rate=round(funding_rate, 3),
                        reason="short_against_positive_carry")
            return True
        return False


# ── RegimeStabilityTracker ─────────────────────────────────────────────────────

class RegimeStabilityTracker:
    """
    Suppresses signal generation when regime is stuck in 'transitioning'
    with confidence ≤ 0.3 for > SUPPRESS_AFTER_S seconds.

    From logs (21:44–21:52): 20+ regime_calculated all returning
    transitioning/unknown/conf=0.3 for 8 minutes — signal churn with zero alpha.
    Suppression saves compute and cleans Kingdom feed.
    """

    SUPPRESS_AFTER_S = 180.0   # 3 minutes stuck
    CONF_THRESHOLD   = 0.3

    def __init__(self) -> None:
        self._transitioning_since: float = 0.0
        self._active: bool = False

    def update(self, regime: str, confidence: float) -> None:
        now = time.time()
        if regime == "transitioning" and confidence <= self.CONF_THRESHOLD:
            if self._transitioning_since == 0.0:
                self._transitioning_since = now
            elif now - self._transitioning_since >= self.SUPPRESS_AFTER_S and not self._active:
                self._active = True
                logger.warning("regime_suppression_activated",
                               stuck_seconds=round(now - self._transitioning_since),
                               confidence=confidence,
                               note="transitioning_conf_0.3_for_3min_signal_generation_halted")
        else:
            if self._active:
                logger.info("regime_suppression_cleared", regime=regime, confidence=confidence)
            self._active = False
            self._transitioning_since = 0.0

    @property
    def is_suppressed(self) -> bool:
        return self._active


# ── PostBlockSignalQueue ───────────────────────────────────────────────────────

class PostBlockSignalQueue:
    """
    During hard calendar blocks (earnings, FOMC) accumulates the best signal
    per symbol: highest coherence × consistent direction fire count.

    On block lift, the top-3 qualifying symbols get a 1.15× size boost on their
    first matching entry — "pent-up conviction" rather than a cold start.

    Qualifies when:
      - signal is still fresh  (< 5 min since last fire)
      - ≥ 3 consistent-direction fires during block
    """

    FRESHNESS_S = 300.0   # 5 min freshness
    MIN_FIRES   = 3
    BOOST_MULT  = 1.15
    MAX_ENTRIES = 3

    @dataclass
    class _Entry:
        direction:     str
        max_coherence: float
        fire_count:    int
        last_ts:       float

    def __init__(self) -> None:
        self._queue:       Dict[str, PostBlockSignalQueue._Entry] = {}
        self._boosts:      Dict[str, Tuple[str, float]] = {}   # sym → (dir, mult)
        self._was_blocked: bool = False

    def accumulate(self, symbol: str, direction: str, coherence: float) -> None:
        now   = time.time()
        entry = self._queue.get(symbol)
        if entry is None or entry.direction != direction:
            self._queue[symbol] = self._Entry(
                direction=direction,
                max_coherence=coherence,
                fire_count=1,
                last_ts=now,
            )
        else:
            entry.max_coherence = max(entry.max_coherence, coherence)
            entry.fire_count   += 1
            entry.last_ts       = now

    def on_block_state(self, blocked: bool) -> None:
        if self._was_blocked and not blocked:
            self._flush_on_lift()
        self._was_blocked = blocked

    def _flush_on_lift(self) -> None:
        now = time.time()
        candidates = [
            (sym, e)
            for sym, e in self._queue.items()
            if (now - e.last_ts) < self.FRESHNESS_S and e.fire_count >= self.MIN_FIRES
        ]
        candidates.sort(key=lambda x: x[1].max_coherence * x[1].fire_count, reverse=True)
        for sym, entry in candidates[: self.MAX_ENTRIES]:
            self._boosts[sym] = (entry.direction, self.BOOST_MULT)
            logger.info("post_block_boost_queued",
                        symbol=sym,
                        direction=entry.direction,
                        max_coherence=round(entry.max_coherence, 2),
                        fire_count=entry.fire_count,
                        boost=self.BOOST_MULT)
        self._queue.clear()

    def consume_boost(self, symbol: str, direction: str) -> float:
        """One-shot: returns boost multiplier if queued, else 1.0. Removes the entry."""
        entry = self._boosts.get(symbol)
        if entry is None:
            return 1.0
        boost_dir, mult = entry
        if boost_dir == direction:
            del self._boosts[symbol]
            logger.info("post_block_boost_applied", symbol=symbol,
                        direction=direction, boost=mult)
            return mult
        return 1.0


# ── SignalGuard (façade) ───────────────────────────────────────────────────────

class SignalGuard:
    """
    Single façade used by main().

    Instantiation:
        _signal_guard = SignalGuard()

    In on_signal_ready (in order):
        1. _signal_guard.update_regime(regime_engine.last_state())
        2. if _signal_guard.is_regime_suppressed: return
        3. if _signal_guard.should_reject_direction(sym, dir, funding_rate): return
        4. coherence_floor_add = _signal_guard.get_coherence_floor_add(sym)
        5. inside notional-rejected block (earnings): _signal_guard.accumulate_blocked_signal(...)
        6. on block lift: handled via on_block_state()
        7. _pb_boost = _signal_guard.consume_post_block_boost(sym, dir)

    On trade close (_record_close):
        _signal_guard.record_trade(sym, direction, pnl)

    In funding loop (after _last_known_rates.update):
        _live_funding_rates.update(real_rates)  # shared dict; read via should_reject_direction
    """

    def __init__(self) -> None:
        self._alpha  = DirectionalAlphaTracker()
        self._carry  = FundingDirectionalVeto()
        self._regime = RegimeStabilityTracker()
        self._queue  = PostBlockSignalQueue()

    # ── Regime stability ──────────────────────────────────────────────────────

    def update_regime(self, rs) -> None:
        if rs is None:
            return
        self._regime.update(
            regime=getattr(rs, "regime", "unknown"),
            confidence=getattr(rs, "confidence", 0.0),
        )

    @property
    def is_regime_suppressed(self) -> bool:
        return self._regime.is_suppressed

    # ── Directional veto ─────────────────────────────────────────────────────

    def should_reject_direction(
        self,
        symbol: str,
        direction: str,
        funding_rate: float = 0.0,
    ) -> bool:
        if direction not in ("long", "short"):
            return False
        if self._carry.should_veto(direction, funding_rate):
            return True
        if self._alpha.should_veto_direction(symbol, direction):
            return True
        return False

    # ── Alpha coherence floor ─────────────────────────────────────────────────

    def get_coherence_floor_add(self, symbol: str) -> float:
        return self._alpha.get_coherence_floor_add(symbol)

    # ── Trade recording ───────────────────────────────────────────────────────

    def record_trade(self, symbol: str, direction: str, pnl: float) -> None:
        self._alpha.record_trade(symbol, direction, pnl)

    # ── Post-block queue ──────────────────────────────────────────────────────

    def on_block_state(self, blocked: bool) -> None:
        self._queue.on_block_state(blocked)

    def accumulate_blocked_signal(
        self, symbol: str, direction: str, coherence: float
    ) -> None:
        self._queue.accumulate(symbol, direction, coherence)

    def consume_post_block_boost(self, symbol: str, direction: str) -> float:
        return self._queue.consume_boost(symbol, direction)
