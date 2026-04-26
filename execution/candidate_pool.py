"""
Trade Candidate Pool — deterministic strategy selection.

Problem: signals arrive asynchronously from 8+ symbols. The first signal
to pass all gates fires regardless of quality. In a downtrend every symbol
sends "short" simultaneously; ARIA fills the position cap with the weakest
signals and misses the strongest.

Solution: buffer incoming candidates for a short window (SELECTION_WINDOW_S).
On each selection tick, pick the top-N by score where N = remaining capacity.
Tag each candidate with the STRATEGY that generated its direction so the journal
can attribute wins/losses to the correct edge.

Strategy taxonomy (derived from signal_generator fallback chain):
  sweep_reversal  — Fallback 1: liquidity sweep set direction
  divergence      — Fallback 1.5: OB divergence set direction
  trend_macro     — Fallback 2: trend + macro + regime agreement
  score_macro     — Fallback 3: score ≥ 3.0 + macro bias
  ob_imbalance    — Fallback 4: OB imbalance ≥ ±0.25
  regime_struct   — Fallback 5: regime (risk_on/off) structural
  funding_fade    — Fallback 6: extreme funding crowd fade
  mag_lead        — Primary: MAG lead signal active
  xaut_riskoff    — Gold risk-off hedge assignment

Usage:
    pool = CandidatePool(max_age_s=30, max_slots=5)
    pool.add(symbol, state, strategy_tag, score)   # called on SIGNAL_READY
    best = pool.select(n=remaining_capacity)        # called by selection loop
    pool.evict_stale()                              # purge old candidates
"""

import time
import structlog
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

logger = structlog.get_logger(__name__)


@dataclass
class Candidate:
    symbol: str
    state: Any                      # MarketState
    strategy_tag: str               # which strategy generated direction
    score: float                    # coherence score at submission time
    direction: str                  # "long" | "short"
    arrived_at: float = field(default_factory=time.time)

    def age_s(self) -> float:
        return time.time() - self.arrived_at

    def is_stale(self, max_age_s: float) -> bool:
        return self.age_s() > max_age_s


class CandidatePool:
    """
    Priority pool of trade candidates.

    One slot per symbol — a newer signal for the same symbol replaces the old one
    if the score is higher (or the old one is stale). This prevents duplicate
    entries and ensures we always have the most recent high-quality signal.

    select(n) returns the top-n candidates sorted by score descending. Callers
    are responsible for checking position limits and executing only what fits.
    """

    STRATEGY_PRIORITY: Dict[str, int] = {
        # Higher = tried first when scores are equal
        "sweep_reversal":  10,
        "ob_imbalance":     9,
        "divergence":       8,
        "mag_lead":         7,
        "xaut_riskoff":     6,
        "funding_fade":     5,
        "trend_macro":      4,
        "score_macro":      3,
        "regime_struct":    2,
        "unknown":          1,
    }

    def __init__(self, max_age_s: float = 30.0, max_slots: int = 8):
        self._pool: Dict[str, Candidate] = {}   # symbol → best candidate
        self.max_age_s = max_age_s
        self.max_slots = max_slots

    def add(self, symbol: str, state: Any, strategy_tag: str, score: float, direction: str) -> None:
        """
        Add or replace candidate for symbol.
        Replace if: pool is empty for symbol, OR new score > existing, OR existing is stale.
        """
        existing = self._pool.get(symbol)
        if (existing is None
                or existing.is_stale(self.max_age_s)
                or score > existing.score):
            self._pool[symbol] = Candidate(
                symbol=symbol,
                state=state,
                strategy_tag=strategy_tag,
                score=score,
                direction=direction,
            )
            logger.debug("candidate_pool_add",
                         symbol=symbol, strategy=strategy_tag,
                         score=round(score, 3), direction=direction,
                         replaced=existing is not None)

    def evict_stale(self) -> List[str]:
        """Remove candidates older than max_age_s. Returns evicted symbols."""
        evicted = [sym for sym, c in self._pool.items() if c.is_stale(self.max_age_s)]
        for sym in evicted:
            del self._pool[sym]
        if evicted:
            logger.debug("candidates_evicted", symbols=evicted)
        return evicted

    def select(self, n: int = 1) -> List[Candidate]:
        """
        Return top-n candidates by (score DESC, strategy_priority DESC).
        Does NOT remove them from pool — caller must call discard() after execution.
        """
        self.evict_stale()
        ranked = sorted(
            self._pool.values(),
            key=lambda c: (
                c.score,
                self.STRATEGY_PRIORITY.get(c.strategy_tag, 1),
                -c.arrived_at,  # oldest among equals = more confirmed
            ),
            reverse=True,
        )
        return ranked[:n]

    def discard(self, symbol: str) -> None:
        """Remove candidate after execution or rejection."""
        self._pool.pop(symbol, None)

    def pending_symbols(self) -> List[str]:
        return list(self._pool.keys())

    def best_score(self) -> float:
        if not self._pool:
            return 0.0
        return max(c.score for c in self._pool.values())

    def size(self) -> int:
        return len(self._pool)

    def summary(self) -> List[Dict]:
        return [
            {
                "symbol": c.symbol,
                "direction": c.direction,
                "strategy": c.strategy_tag,
                "score": round(c.score, 3),
                "age_s": round(c.age_s(), 1),
            }
            for c in sorted(self._pool.values(), key=lambda x: x.score, reverse=True)
        ]


def tag_strategy(
    state: Any,
    cascade_phase: str = "idle",
    cascade_direction: str = "",
    signal_direction: str = "",
) -> str:
    """
    Infer which strategy generated the trade direction from the MarketState fields.
    Called in main.py after a signal passes all gates, before adding to pool.

    Priority mirrors signal_generator.py fallback order.
    Cascade-aware: returns cascade tags when liquidation state machine is active
    and the signal direction aligns with the cascade intent.
    """
    # Spartan priority: exogenous mechanical shocks outrank organic signals.
    if cascade_phase == "momentum":
        return "cascade_momentum"
    if cascade_phase == "primed":
        return "cascade_aftermath"
    if cascade_phase == "blocked":
        # During blocked phase, signals trading WITH the cascade are suppressed
        # by risk_engine; signals that make it through are fades.
        return "cascade_fade"

    sweep      = getattr(state, "sweep",       "none")
    divergence = getattr(state, "divergence",  "none")
    mag_active = getattr(state, "mag_active",  False)
    funding    = getattr(state, "funding_class", "neutral")
    symbol     = getattr(state, "symbol",      "")

    if symbol == "XAUT-USD" and getattr(state, "macro_bias", "") in ("risk_off", "confused"):
        return "xaut_riskoff"
    if mag_active:
        return "mag_lead"
    if sweep in ("buy_side", "sell_side"):
        return "sweep_reversal"
    if divergence not in ("none", "neutral"):
        return "divergence"
    if "extreme" in str(funding):
        return "funding_fade"
    # Macro/regime fallbacks — use regime and macro_bias to distinguish
    regime     = getattr(state, "regime",      "rotational")
    macro_bias = getattr(state, "macro_bias",  "neutral")
    if macro_bias != "neutral" and regime in ("risk_on", "risk_off"):
        return "trend_macro"
    if macro_bias != "neutral":
        return "score_macro"
    if regime in ("risk_on", "risk_off"):
        return "regime_struct"
    # Imbalance is not stored directly on MarketState — default
    return "ob_imbalance"
