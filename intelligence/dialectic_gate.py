"""
intelligence/dialectic_gate.py — Hegelian Dialectic Engine

Signal agents use this as a skill/tool to resolve macro/micro conflicts.
Thesis (macro: T1/T2) + Antithesis (micro: T3/T4) → Synthesis (trade/reduce/abstain)

Integration with ARIA's cross-agent betting system:
  - DialecticGate.evaluate() returns a verdict + confidence
  - The verdict is logged as a PredictionRecord with personality="HEGEL"
  - CrossAgentBetEngine treats HEGEL as an independent agent
  - If HEGEL says "abstain" (confidence ≥ 0.85), the bet engine blocks the trade
    even if other agents agree — HEGEL's veto overrides weak consensus.
"""

from __future__ import annotations

import structlog
from typing import Optional

log = structlog.get_logger(__name__)

# Conflict strength thresholds
# Lowered from 2.0 → 1.0 so L4-confirmed microstructure signals are not vetoed
# by macro headwinds.  Gate should attenuate, not silence.
_STRONG_CONFLICT = 1.0
_WEAK_CONFLICT = 0.5


class DialecticGate:
    """
    Hegelian conflict resolution for signal agents.

    Agents call evaluate() before committing capital.
    The gate tracks its own accuracy and reports to the prediction market.
    """

    def __init__(self) -> None:
        # (predicted_action, was_win) tuples for self-calibration
        self._resolutions: list[tuple[str, bool]] = []
        self._abstain_wins = 0   # abstain predicted, trade would have lost
        self._abstain_misses = 0 # abstain predicted, trade would have won
        self._trade_wins = 0
        self._trade_misses = 0

    # ── Public API ──────────────────────────────────────────────────────────

    def evaluate(
        self,
        symbol: str,
        direction: str,
        tier_scores: dict,
        macro_bias: str = "neutral",
    ) -> tuple[str, str, float]:
        """
        Hegelian synthesis of macro vs micro signals.

        Returns (action, reason, confidence):
          action:     "trade" | "reduce" | "abstain"
          reason:     human-readable conflict description
          confidence: 0.0–1.0 — certainty of the verdict
        """
        if not tier_scores:
            return "trade", "no_tier_data", 0.50

        # Macro = Tier 1 (regime/SSI) + Tier 2 (structure)
        # Micro = Tier 3 (microstructure) + Tier 4 (liquidation)
        _macro = (
            tier_scores.get("regime", 0.0)
            + tier_scores.get("structure", 0.0)
        )
        _micro = (
            tier_scores.get("microstructure", 0.0)
            + tier_scores.get("liquidation", 0.0)
        )

        # Same sign or one is zero → alignment, no conflict
        if _macro * _micro >= 0:
            _alignment_strength = abs(_macro) + abs(_micro)
            if _alignment_strength > 2.0:
                return (
                    "trade",
                    f"strong_alignment_strength={_alignment_strength:.2f}",
                    0.92,
                )
            return "trade", "macro_micro_aligned", 0.80

        # Opposite signs — conflict detected. Strength = min of the two magnitudes.
        _conflict_strength = min(abs(_macro), abs(_micro))

        if _conflict_strength > _STRONG_CONFLICT:
            return (
                "abstain",
                f"strong_conflict_macro={_macro:.2f}_micro={_micro:.2f}",
                0.90,
            )

        if _conflict_strength > _WEAK_CONFLICT:
            return (
                "reduce",
                f"weak_conflict_macro={_macro:.2f}_micro={_micro:.2f}",
                0.65,
            )

        # Very weak conflict — borderline, allow but with reduced confidence
        return (
            "trade",
            f"borderline_conflict_macro={_macro:.2f}_micro={_micro:.2f}",
            0.55,
        )

    def record_outcome(self, predicted_action: str, was_win: bool) -> None:
        """
        Feed back the trade outcome to calibrate gate confidence.

        predicted_action: what the gate recommended
        was_win:          whether the trade was profitable
        """
        self._resolutions.append((predicted_action, was_win))
        if len(self._resolutions) > 200:
            self._resolutions = self._resolutions[-200:]

        if predicted_action == "abstain":
            if was_win:
                self._abstain_misses += 1
            else:
                self._abstain_wins += 1
        else:
            if was_win:
                self._trade_wins += 1
            else:
                self._trade_misses += 1

        log.debug("dialectic_outcome_recorded",
                  predicted=predicted_action, was_win=was_win,
                  abstain_wr=self.abstain_win_rate,
                  trade_wr=self.trade_win_rate)

    # ── Introspection ───────────────────────────────────────────────────────

    @property
    def accuracy(self) -> float:
        """Overall accuracy of the gate (fraction of correct verdicts)."""
        if not self._resolutions:
            return 0.5
        correct = sum(
            1
            for pred, actual in self._resolutions
            if (pred in ("abstain", "reduce") and not actual)
            or (pred == "trade" and actual)
        )
        return correct / len(self._resolutions)

    @property
    def abstain_win_rate(self) -> float:
        """When gate says 'abstain', what fraction of trades would have lost?"""
        total = self._abstain_wins + self._abstain_misses
        if total == 0:
            return 0.5
        return self._abstain_wins / total

    @property
    def trade_win_rate(self) -> float:
        """When gate says 'trade', what fraction actually won?"""
        total = self._trade_wins + self._trade_misses
        if total == 0:
            return 0.5
        return self._trade_wins / total

    def get_summary(self) -> dict:
        return {
            "accuracy": round(self.accuracy, 3),
            "abstain_wr": round(self.abstain_win_rate, 3),
            "trade_wr": round(self.trade_win_rate, 3),
            "total_verdicts": len(self._resolutions),
        }


# ── Standalone helper (stateless, for quick checks) ───────────────────────

def hegelian_gate(
    symbol: str, direction: str, tier_scores: dict
) -> tuple[str, str]:
    """
    Stateless Hegelian check — same logic as DialecticGate.evaluate()
    but returns (action, reason) without confidence.

    Use this when you don't need tracking (e.g., hot-path pre-filters).
    """
    if not tier_scores:
        return "ok", ""

    _macro = tier_scores.get("regime", 0.0) + tier_scores.get("structure", 0.0)
    _micro = tier_scores.get("microstructure", 0.0) + tier_scores.get("liquidation", 0.0)

    if _macro * _micro >= 0:
        return "ok", ""

    if abs(_macro) > _STRONG_CONFLICT and abs(_micro) > _STRONG_CONFLICT:
        return "abstain", f"strong_conflict_macro={_macro:.2f}_micro={_micro:.2f}"

    return "reduce", f"weak_conflict_macro={_macro:.2f}_micro={_micro:.2f}"
