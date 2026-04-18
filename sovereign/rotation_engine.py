"""
sovereign/rotation_engine.py — Market phase detection and allocation engine.

Architecture
────────────
Three-timeframe momentum framework:
  Short  (1h proxy)  — drift_1h snapshot from signal_price_stores
  Medium (~24h eff.) — rolling avg of last 4 short scores (4×6h cycles)
  Long   (~48h eff.) — rolling avg of last 8 medium scores (8×6h cycles)
  NOTE: doc originally claimed 48h/168h/720h; actual effective lookbacks are
  much shorter because we use drift_1h, not multi-day candle returns.

Phase state machine:
  BULL       → high conviction, max SSI exposure
  CAUTION    → moderate conviction, reduce MEME, hold MAG7/DEFI
  TRANSITION → conflicting signals, MEME = 0, increase USSI, open proxy hedge
  BEAR       → confirmed down, full hedge, USSI = dominant position
  RECOVERY   → turning up, rebuild MAG7/DEFI, MEME = last to re-enter

Churn prevention:
  - Minimum 6h phase duration
  - Maximum 2 phase transitions per 12h window

MEME rule:
  - MEME exits FIRST in any rotation down (BULL→CAUTION, CAUTION→TRANSITION, etc.)
  - MEME enters LAST in any rotation up (RECOVERY→BULL is the re-entry gate)
"""

from __future__ import annotations

import time
import structlog
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

log = structlog.get_logger(__name__)

# ── Phase constants ────────────────────────────────────────────────────────────

class MarketPhase(str, Enum):
    BULL       = "BULL"
    CAUTION    = "CAUTION"
    TRANSITION = "TRANSITION"
    BEAR       = "BEAR"
    RECOVERY   = "RECOVERY"


# Target allocation per phase: {symbol → weight (0–1, sums to 1.0)}
# MEME enters/exits in the BULL phase only — it's the last buy and first sell.
PHASE_ALLOCATIONS: Dict[MarketPhase, Dict[str, float]] = {
    MarketPhase.BULL: {
        "MAG7SSI-USD": 0.45,
        "DEFISSI-USD": 0.30,
        "MEMESSI-USD": 0.15,
        "USSI-USD":    0.10,
    },
    MarketPhase.CAUTION: {
        "MAG7SSI-USD": 0.40,
        "DEFISSI-USD": 0.25,
        "MEMESSI-USD": 0.05,
        "USSI-USD":    0.30,
    },
    MarketPhase.TRANSITION: {
        "MAG7SSI-USD": 0.35,
        "DEFISSI-USD": 0.20,
        "MEMESSI-USD": 0.00,
        "USSI-USD":    0.45,
    },
    MarketPhase.BEAR: {
        "MAG7SSI-USD": 0.20,
        "DEFISSI-USD": 0.10,
        "MEMESSI-USD": 0.00,
        "USSI-USD":    0.70,
    },
    MarketPhase.RECOVERY: {
        "MAG7SSI-USD": 0.35,
        "DEFISSI-USD": 0.25,
        "MEMESSI-USD": 0.05,
        "USSI-USD":    0.35,
    },
}

# Phase hedge flag: which phases should activate proxy hedge
PHASE_HEDGE_ACTIVE: Dict[MarketPhase, bool] = {
    MarketPhase.BULL:       False,
    MarketPhase.CAUTION:    False,
    MarketPhase.TRANSITION: True,
    MarketPhase.BEAR:       True,
    MarketPhase.RECOVERY:   False,
}

# Churn guards
MIN_PHASE_DURATION_S: float = 6 * 3600   # 6 hours minimum dwell
MAX_TRANSITIONS_PER_12H: int = 2


@dataclass
class PhaseTransition:
    timestamp: float
    from_phase: MarketPhase
    to_phase:   MarketPhase


@dataclass
class PhaseDecision:
    phase:       MarketPhase
    allocations: Dict[str, float]
    hedge_active: bool
    confidence:   float   # 0.0–1.0
    short_score:  float   # short-term momentum
    medium_score: float
    long_score:   float
    reason:       str


class RotationEngine:
    """
    Determines target portfolio phase from multi-timeframe SSI momentum signals.

    Signal inputs (all from signal_price_stores):
      - MAG7SSI drift_1h  → tech-sector trend
      - DEFISSI drift_1h  → DeFi-sector trend
      - MEMESSI drift_1h  → risk-appetite / meme signal (volatile, noisy)
      - USSI    drift_1h  → broad TradFi correlation signal

    Combined into a unified momentum score; mapped to phase via thresholds.
    """

    def __init__(self) -> None:
        self._current_phase:   MarketPhase = MarketPhase.CAUTION
        self._phase_entered_at: float      = time.time() - MIN_PHASE_DURATION_S  # allow immediate first transition
        self._transition_history: List[PhaseTransition] = []
        self._short_scores: List[float]   = []   # rolling short-term scores
        self._medium_scores: List[float]  = []   # rolling medium-term scores

    # ── Public API ────────────────────────────────────────────────────────────

    def evaluate(self, signal_price_stores: dict, carry_score: float = 0.0) -> PhaseDecision:
        """
        Evaluate current market phase from live signal data.

        Args:
            signal_price_stores: {symbol: {"price": float, "drift_1h": float, "ts_ms": int}}
            carry_score: average funding carry score from FundingRadar (-3 to +3)

        Returns PhaseDecision with target allocations and hedge flag.
        """
        short_score  = self._compute_short_score(signal_price_stores, carry_score)
        medium_score = self._compute_medium_score(short_score)
        long_score   = self._compute_long_score(signal_price_stores)

        candidate_phase, confidence = self._score_to_phase(short_score, medium_score, long_score)

        # Apply churn guards — may keep current phase despite new signal
        final_phase = self._apply_churn_guard(candidate_phase)

        allocations = PHASE_ALLOCATIONS[final_phase]
        hedge_active = PHASE_HEDGE_ACTIVE[final_phase]

        return PhaseDecision(
            phase=final_phase,
            allocations=dict(allocations),
            hedge_active=hedge_active,
            confidence=confidence,
            short_score=short_score,
            medium_score=medium_score,
            long_score=long_score,
            reason=f"short={short_score:.2f} med={medium_score:.2f} long={long_score:.2f} carry={carry_score:.2f}",
        )

    def current_phase(self) -> MarketPhase:
        return self._current_phase

    def phase_age_hours(self) -> float:
        return (time.time() - self._phase_entered_at) / 3600

    def transitions_in_last_12h(self) -> int:
        cutoff = time.time() - 12 * 3600
        return sum(1 for t in self._transition_history if t.timestamp >= cutoff)

    # ── Scoring ───────────────────────────────────────────────────────────────

    def _compute_short_score(self, stores: dict, carry_score: float) -> float:
        """
        Short-term (48h proxy) momentum score: -3 to +3.

        Uses drift_1h from SSI tokens with weights:
          MAG7: 0.35 (most reliable index)
          DEFI: 0.30
          MEME: 0.15 (noisy — low weight)
          USSI: 0.10 (TradFi correlation)
          carry: 0.10 (funding market signal)
        """
        weights = {
            "MAG7SSI-USD": 0.35,
            "DEFISSI-USD": 0.30,
            "MEMESSI-USD": 0.15,
            "USSI-USD":    0.10,
        }

        raw = 0.0
        data_count = 0
        for sym, w in weights.items():
            drift = float((stores.get(sym) or {}).get("drift_1h", 0.0) or 0.0)
            if drift != 0.0:
                data_count += 1
            raw += drift * w

        # Funding carry component
        raw += carry_score * 0.10

        # Normalise: scale drift (typically ±5%) to ±3 score range
        score = raw * 60.0   # 5% drift × 0.90 total weight × 60 ≈ 2.7
        score = max(-3.0, min(3.0, score))

        # Maintain rolling window for medium-term
        self._short_scores.append(score)
        if len(self._short_scores) > 24:   # keep last 24 cycles (144h at 6h cycle)
            self._short_scores.pop(0)

        return round(score, 3)

    def _compute_medium_score(self, latest_short: float) -> float:
        """
        Medium-term (168h proxy) score: rolling average of last 4 short scores.
        Smooths out noise; requires sustained momentum for score to shift.
        """
        if len(self._short_scores) < 2:
            return latest_short

        window = self._short_scores[-min(4, len(self._short_scores)):]
        score = sum(window) / len(window)

        self._medium_scores.append(score)
        if len(self._medium_scores) > 48:
            self._medium_scores.pop(0)

        return round(score, 3)

    def _compute_long_score(self, stores: dict) -> float:
        """
        Long-term (720h proxy) structural signal.

        Uses MAG7SSI and DEFISSI 1h drift as a proxy since we don't have
        30-day candle history in signal_price_stores. Long score is the
        average of the last 8 medium scores (if available) or the current
        medium score decayed toward zero.
        """
        if len(self._medium_scores) >= 4:
            window = self._medium_scores[-8:]
            return round(sum(window) / len(window), 3)

        # Fallback: use drift but halve its influence (long-term = less reactive)
        mag7_drift = float((stores.get("MAG7SSI-USD") or {}).get("drift_1h", 0.0) or 0.0)
        defi_drift = float((stores.get("DEFISSI-USD") or {}).get("drift_1h", 0.0) or 0.0)
        composite  = (mag7_drift * 0.6 + defi_drift * 0.4) * 30.0  # lighter scaling
        return round(max(-3.0, min(3.0, composite)), 3)

    def _score_to_phase(
        self,
        short: float,
        medium: float,
        long: float,
    ) -> Tuple[MarketPhase, float]:
        """
        Map three-timeframe scores to a market phase.

        Timeframe alignment:
          All agree positive (≥ threshold) → strong bull/recovery signal
          Mixed signals → caution/transition
          All agree negative → bear
        """
        # Weighted composite: short most reactive, long most structural
        composite = short * 0.40 + medium * 0.35 + long * 0.25

        # Alignment bonus: all three timeframes agree direction → higher confidence
        signs = [
            1 if s > 0.2 else (-1 if s < -0.2 else 0)
            for s in (short, medium, long)
        ]
        aligned = len(set(signs)) == 1 and signs[0] != 0
        confidence = min(1.0, abs(composite) / 2.5 * (1.2 if aligned else 1.0))

        # Phase thresholds
        if composite >= 1.5:
            # MEME enters LAST — must pass through RECOVERY before returning to BULL.
            # A v-bounce that skips RECOVERY would allocate 15% MEME immediately
            # from BEAR, which violates the MEME-last structural rule.
            if self._current_phase in (MarketPhase.BEAR, MarketPhase.TRANSITION):
                phase = MarketPhase.RECOVERY
            else:
                phase = MarketPhase.BULL
        elif composite >= 0.5:
            # Coming from BEAR/TRANSITION → RECOVERY first
            if self._current_phase in (MarketPhase.BEAR, MarketPhase.TRANSITION):
                phase = MarketPhase.RECOVERY
            else:
                phase = MarketPhase.CAUTION
        elif composite >= -0.3:
            if self._current_phase == MarketPhase.RECOVERY and composite >= 0.0:
                phase = MarketPhase.CAUTION
            else:
                phase = MarketPhase.TRANSITION
        elif composite >= -1.5:
            phase = MarketPhase.BEAR
        else:
            phase = MarketPhase.BEAR

        return phase, round(confidence, 3)

    # ── Churn guard ───────────────────────────────────────────────────────────

    def _apply_churn_guard(self, candidate: MarketPhase) -> MarketPhase:
        """
        Enforce phase stability rules:
          1. Min 6h dwell before transitioning
          2. Max 2 transitions in any 12h window
        Returns current phase if guard blocks, otherwise updates to candidate.
        """
        if candidate == self._current_phase:
            return self._current_phase

        # Guard 1: minimum phase duration
        age_s = time.time() - self._phase_entered_at
        if age_s < MIN_PHASE_DURATION_S:
            log.debug(
                "phase_transition_blocked_dwell",
                current=self._current_phase,
                candidate=candidate,
                age_h=round(age_s / 3600, 1),
                required_h=MIN_PHASE_DURATION_S / 3600,
            )
            return self._current_phase

        # Guard 2: max transitions per 12h
        if self.transitions_in_last_12h() >= MAX_TRANSITIONS_PER_12H:
            log.debug(
                "phase_transition_blocked_churn",
                current=self._current_phase,
                candidate=candidate,
                transitions_12h=self.transitions_in_last_12h(),
            )
            return self._current_phase

        # Transition allowed — record it
        old_phase = self._current_phase
        self._current_phase    = candidate
        self._phase_entered_at = time.time()
        self._transition_history.append(PhaseTransition(
            timestamp=time.time(),
            from_phase=old_phase,
            to_phase=candidate,
        ))
        # Prune old history
        cutoff = time.time() - 24 * 3600
        self._transition_history = [
            t for t in self._transition_history if t.timestamp >= cutoff
        ]

        log.info(
            "sovereign_phase_transition",
            from_phase=old_phase.value,
            to_phase=candidate.value,
            dwell_h=round(age_s / 3600, 1),
        )

        return candidate
