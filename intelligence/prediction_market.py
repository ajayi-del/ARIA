"""
intelligence/prediction_market.py — ARIA Prediction Market Layer.

Tracks agent predictions, resolves outcomes, and runs cross-agent bet
combination logic. Designed for zero-latency on the hot path:

  add_pending()   — synchronous, queue.put_nowait(), never blocks
  _drain_once()   — async, called by background loop every 1 s
  resolve()       — synchronous, O(n) scan of deque, fills outcome fields
  check_bet()     — synchronous Bayesian joint-probability gate

Memory: circular deque maxlen=500. No file I/O. No database. stdlib only.
"""

from __future__ import annotations

import asyncio
import collections
import time
from dataclasses import dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PredictionRecord:
    id: str
    agent: str              # "perp" | "gold" | "equity"
    personality: str        # "FLOW" | "APEX" | "SCOUT" | "COIL" | "AFTERMATH" | "SHIELD"
    symbol: str
    direction: str          # "long" | "short"
    confidence: float       # 0.0 – 1.0
    ml_probability: float   # from ML classifier cache
    coherence: float
    entry_price: float
    predicted_exit: float
    timestamp_ms: int
    # Filled on resolution:
    outcome: Optional[str] = None         # "correct" | "incorrect"
    actual_r: Optional[float] = None      # realized R-multiple (legacy)
    actual_pnl_r: Optional[float] = None  # realized R-multiple (canonical)
    resolved_ms: Optional[int] = None
    resolved: bool = False                # True once outcome is set
    # Nietzsche will_state at trade time (stamped after Nietzsche runs)
    will_state: Optional[str] = None


@dataclass
class BetResult:
    p_joint: float
    agent_a: str
    agent_b: str
    combined_size_mult: float   # 1.5×
    combined_budget: float = 0.0  # USD budget contributed by both sides


@dataclass
class CalibrationResult:
    personality: str
    n_trades: int
    calibration_error: float
    is_overconfident: bool
    budget_multiplier: float    # e.g. 0.60 → reduce budget to 60 %


# ---------------------------------------------------------------------------
# PredictionStore
# ---------------------------------------------------------------------------

class PredictionStore:
    """
    Thread/task-safe prediction ledger.

    Hot-path contract
    -----------------
    * add_pending() is SYNCHRONOUS — queue.put_nowait() only, returns None.
    * _drain_once() is ASYNC — moves items from queue into _records deque.
    * Neither method touches disk or a database.
    """

    def __init__(self) -> None:
        self._records: collections.deque[PredictionRecord] = collections.deque(maxlen=500)
        self._queue: asyncio.Queue[PredictionRecord] = asyncio.Queue()

    # ------------------------------------------------------------------
    # Hot-path write (synchronous, never blocks)
    # ------------------------------------------------------------------

    def add_pending(self, record: PredictionRecord) -> None:
        """Add a prediction to the queue. Synchronous. Hot-path safe."""
        self._queue.put_nowait(record)

    # ------------------------------------------------------------------
    # Background drain (async, called every 1 s by event loop)
    # ------------------------------------------------------------------

    async def _drain_once(self) -> int:
        """
        Drain all queued PredictionRecords into _records.
        Returns the number of records moved.
        """
        moved = 0
        while not self._queue.empty():
            try:
                record = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._records.append(record)
            moved += 1
        return moved

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(self, symbol: str, outcome: str, actual_r: float) -> Optional[PredictionRecord]:
        """
        Find the most recent *pending* record for symbol, fill resolution
        fields in-place, and return it.  Returns None if no match found.
        """
        # Scan deque in reverse to find the most recent pending record.
        for record in reversed(self._records):
            if record.symbol == symbol and record.outcome is None:
                record.outcome = outcome
                record.actual_r = actual_r
                record.resolved_ms = _now_ms()
                return record
        return None

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    def calibration_error(self, personality: str, n: int = 50) -> float:
        """
        Mean squared error between predicted confidence and binary outcome
        for the last *n* resolved records matching *personality*.

        "correct"  → actual = 1.0
        "incorrect" → actual = 0.0
        MSE = mean((confidence - actual)^2)
        Returns 0.0 if no resolved records exist for this personality.
        """
        resolved = [
            r for r in self._records
            if r.personality == personality
            and r.outcome is not None
        ]
        if not resolved:
            return 0.0

        sample = resolved[-n:]  # last n
        total = 0.0
        for r in sample:
            actual = 1.0 if r.outcome == "correct" else 0.0
            total += (r.confidence - actual) ** 2
        return total / len(sample)

    def accuracy_today(self) -> float:
        """
        Fraction of today's resolved records that are "correct".
        Returns 0.5 if no resolved records exist for today.
        """
        today_start_ms = _today_start_ms()
        resolved_today = [
            r for r in self._records
            if r.outcome is not None
            and r.resolved_ms is not None
            and r.resolved_ms >= today_start_ms
        ]
        if not resolved_today:
            return 0.5
        correct = sum(1 for r in resolved_today if r.outcome == "correct")
        return correct / len(resolved_today)


# ---------------------------------------------------------------------------
# CrossAgentBetEngine
# ---------------------------------------------------------------------------

_BET_THRESHOLD = 0.70
_BET_SIZE_MULT  = 1.5


class CrossAgentBetEngine:
    """
    Combines independent agent predictions into a joint Bayesian probability.

    Rules
    -----
    * Must NOT bet if same (agent + personality) appears in existing_preds.
    * new_pred and the matching existing_pred must share symbol + direction.
    * Returns BetResult only when P_joint >= 0.70.
    """

    # ------------------------------------------------------------------
    # Core Bayesian combiner
    # ------------------------------------------------------------------

    def _joint_probability(self, P_A: float, P_B: float) -> float:
        """
        Bayesian combination of two independent binary probabilities:

            P_joint = P_A·P_B / (P_A·P_B + (1 - P_A)·(1 - P_B))
        """
        numerator = P_A * P_B
        denominator = numerator + (1.0 - P_A) * (1.0 - P_B)
        if denominator == 0.0:
            return 0.0
        return numerator / denominator

    # ------------------------------------------------------------------
    # Bet gate
    # ------------------------------------------------------------------

    def check_bet(
        self,
        new_pred: PredictionRecord,
        existing_preds: List[PredictionRecord],
        budget_manager,          # duck-typed; not called internally
    ) -> Optional[BetResult]:
        """
        Evaluate whether new_pred + any existing pred qualifies for a
        combined-size bet.

        Returns BetResult on the first qualifying pair, else None.

        Exclusion rules
        ---------------
        * Skip any existing pred with same agent AND same personality as
          new_pred (would be the same model reposting).
        * Skip any existing pred that doesn't share symbol + direction.
        """
        for existing in existing_preds:
            # Exclusion: same agent + personality → skip (not independent)
            if existing.agent == new_pred.agent and existing.personality == new_pred.personality:
                continue

            # Must agree on symbol and direction
            if existing.symbol != new_pred.symbol:
                continue
            if existing.direction != new_pred.direction:
                continue

            p_joint = self._joint_probability(
                new_pred.confidence,
                existing.confidence,
            )

            if p_joint >= _BET_THRESHOLD:
                # Compute combined budget from the budget manager if available
                combined_budget = 0.0
                try:
                    ba = budget_manager.get_budget(new_pred.agent, new_pred.personality)
                    bb = budget_manager.get_budget(existing.agent, existing.personality)
                    combined_budget = ba + bb
                    # Cap at 15% of total balance
                    try:
                        max_combined = budget_manager._total_balance * 0.15
                        combined_budget = min(combined_budget, max_combined)
                    except AttributeError:
                        pass
                except Exception:
                    pass

                return BetResult(
                    p_joint=p_joint,
                    agent_a=new_pred.agent,
                    agent_b=existing.agent,
                    combined_size_mult=_BET_SIZE_MULT,
                    combined_budget=combined_budget,
                )

        return None


# ---------------------------------------------------------------------------
# Calibration helper (standalone function + CalibrationResult builder)
# ---------------------------------------------------------------------------

def build_calibration_result(
    store: PredictionStore,
    personality: str,
    n: int = 50,
    overconfidence_threshold: float = 0.05,
) -> CalibrationResult:
    """
    Derive a CalibrationResult from the store for *personality*.

    is_overconfident  — True when calibration_error > overconfidence_threshold
    budget_multiplier — 0.60 when overconfident, 1.0 otherwise
    """
    resolved = [
        r for r in store._records
        if r.personality == personality and r.outcome is not None
    ]
    n_trades = len(resolved)
    cal_err = store.calibration_error(personality, n=n)
    is_overconfident = cal_err > overconfidence_threshold
    budget_multiplier = 0.60 if is_overconfident else 1.0

    return CalibrationResult(
        personality=personality,
        n_trades=n_trades,
        calibration_error=cal_err,
        is_overconfident=is_overconfident,
        budget_multiplier=budget_multiplier,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _today_start_ms() -> int:
    """Millisecond timestamp for 00:00:00 UTC today."""
    import datetime
    today = datetime.datetime.utcnow().date()
    dt = datetime.datetime(today.year, today.month, today.day,
                           tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)
