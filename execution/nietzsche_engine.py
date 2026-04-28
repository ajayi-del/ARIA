"""
execution/nietzsche_engine.py — Nietzsche Engine (ARIA v2.2)

Nietzsche = Sizing is Will to Power.
  "What does not kill me makes me stronger."

In practice: pure sizing logic. NEVER rejects. Only scales.
Receives a signal that has already passed KantGate.
Returns size multipliers, risk parameters, and bracket configs.

This class is STATELESS. All inputs are passed as arguments.
No daily counters. No flip history. Pure function.
"""

from __future__ import annotations

from typing import List, Optional, Tuple


# ── Coherence tier sizing table ─────────────────────────────────────────────
# (min_coherence, size_mult, risk_pct, max_concurrent)
COHERENCE_TIERS: List[Tuple[float, float, float, int]] = [
    (8.0, 1.50, 0.03, 2),
    (7.0, 1.00, 0.025, 2),
    (6.0, 0.50, 0.02,  1),
    (5.0, 0.25, 0.015, 1),
    (4.5, 0.20, 0.012, 1),
]


class NietzscheEngine:
    """
    Stateless sizing engine.

    Public API:
      - size_mult(coherence)       → (size_mult, risk_pct, max_concurrent)
      - brackets(coherence)        → [(fraction, r_multiple), ...]
    """

    def size_mult(self, coherence: float) -> Tuple[float, float, int]:
        """
        Returns (size_multiplier, risk_pct, max_concurrent) for a coherence level.
        Never rejects — returns (0.0, 0.0, 0) only if coherence < 4.5
        (which KantGate should have already rejected).
        """
        for tier in COHERENCE_TIERS:
            if coherence >= tier[0]:
                return tier[1], tier[2], tier[3]
        return 0.0, 0.0, 0

    def brackets(self, coherence: float) -> List[Tuple[float, float]]:
        """
        Bracket config for a given coherence level.
        Returns list of (fraction_of_position, r_multiple) tuples.

        coherence ≥ 8.0:  [(0.20, 2R), (0.30, 5R), (0.50, 7R)]  → weighted 5.4R  (elite)
        coherence ≥ 7.0:  [(0.30, 2R), (0.30, 4R), (0.40, 6R)]  → weighted 4.2R
        coherence  5-6.9: [(0.30, 3R), (0.70, 5R)]               → weighted 4.4R
        coherence  4.5-5: [(0.30, 3R), (0.70, 5R)]               → same as 5-6.9
        """
        if coherence >= 8.0:
            return [(0.20, 2.0), (0.30, 5.0), (0.50, 7.0)]
        if coherence >= 7.0:
            return [(0.30, 2.0), (0.30, 4.0), (0.40, 6.0)]
        return [(0.30, 3.0), (0.70, 5.0)]
