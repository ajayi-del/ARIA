"""
execution/execution_guardian.py — ARIA Execution Guardian (v2.2 façade)

BACKWARD COMPATIBILITY NOTICE:
  This class is now a façade over two specialist engines:
    - KantGate:       hard YES/NO rejection (structure, survival, rationality)
    - NietzscheEngine: pure sizing (coherence → size_mult, brackets)

  All public APIs (check, record_execution, update_regime_confidence,
  reset_day, get_brackets, daily_stats) retain identical signatures.
  Internal state has migrated into KantGate.

Philosophy:
  Kant (Structure First):    No valid structure = halt.
  Nietzsche (Will to Power): Coherence × cascade × recovery × regime = conviction.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import structlog

from execution.kant_gate import KantGate, KantVerdict
from execution.nietzsche_engine import NietzscheEngine

log = structlog.get_logger(__name__)

# Re-export for backward compatibility
GuardianVerdict = KantVerdict


class ExecutionGuardian:
    """
    Backward-compatible façade.

    Internally delegates:
      - All hard gates → KantGate
      - All sizing     → NietzscheEngine
    """

    def __init__(self) -> None:
        self._kant = KantGate()
        self._nietzsche = NietzscheEngine()

    # ── Public gate (backward-compatible) ─────────────────────────────────

    def check(
        self,
        symbol:           str,
        direction:        str,
        coherence:        float,
        rr_ratio:         float,
        balance:          float,
        regime_state,
        cascade_zscore:   float = 0.0,
        regime_conf:      float = 0.0,
    ) -> KantVerdict:
        """
        Backward-compatible single call.

        1. Delegates to KantGate for hard rejection.
        2. If Kant approves, asks NietzscheEngine for size_mult.
        3. Returns unified verdict (allowed + size_mult).
        """
        # Compute Spartan flag here so KantGate doesn't need to know the rule
        spartan = (
            (regime_conf >= 0.70 and coherence >= 7.0) or
            (regime_conf >= 0.60 and coherence >= 8.0) or
            (coherence >= 8.0)
        )

        # ── Phase 1: Kant (hard gates) ────────────────────────────────
        kant = self._kant.check(
            symbol=symbol,
            direction=direction,
            coherence=coherence,
            rr_ratio=rr_ratio,
            balance=balance,
            regime_state=regime_state,
            cascade_zscore=cascade_zscore,
            regime_conf=regime_conf,
            spartan=spartan,
        )
        if not kant.allowed:
            # Kant rejected — size_mult defaults to 0.0 for safety
            return KantVerdict(
                allowed=False,
                reason=kant.reason,
                size_mult=0.0,
                log_event=kant.log_event,
            )

        # ── Phase 2: Nietzsche (sizing) ───────────────────────────────
        size_mult, risk_pct, max_conc = self._nietzsche.size_mult(coherence)

        # Flip cooldown elite exception: Nietzsche caps size at 0.5×
        if symbol in self._kant._last_exec:
            prev_dir, prev_ts = self._kant._last_exec[symbol]
            age_s = __import__('time').time() - prev_ts
            if prev_dir != direction and age_s < 60*60 and coherence >= 8.0:
                size_mult = min(size_mult, 0.50)

        return KantVerdict(
            allowed=True,
            reason="all_guardian_gates_passed",
            size_mult=size_mult,
            log_event="guardian_approved",
        )

    def record_execution(self, symbol: str, direction: str) -> None:
        """Call immediately after an order is placed."""
        self._kant.record_execution(symbol, direction)

    def update_regime_confidence(self, confidence: float) -> None:
        """Called by regime engine on every update to track confidence drift."""
        self._kant.update_regime_confidence(confidence)

    def reset_day(self) -> None:
        """Call at UTC midnight to reset daily counters."""
        self._kant.reset_day()

    def get_brackets(self, coherence: float) -> List[Tuple[float, float]]:
        """
        Bracket config for a given coherence level.
        Delegates to NietzscheEngine.
        """
        return self._nietzsche.brackets(coherence)

    def daily_stats(self) -> dict:
        """For dashboard / logging."""
        return self._kant.daily_stats()
