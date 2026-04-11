"""
SignalFeedbackEngine — Adaptive coherence calibration from trade outcomes.

Learns win-rate per tier and adjusts:
  - min_coherence threshold (±20% of baseline 2.0)
  - per-tier score weights (0.50–2.00, decay toward 1.0 between trades)

Requires >= 10 settled trades before any adjustment activates.
Bayesian prior: 10-trade smoothing prevents overfit on small samples.
"""
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional
import structlog

logger = structlog.get_logger(__name__)

BASELINE_THRESHOLD = 2.0
MAX_ADJUSTMENT     = 0.20    # ±20% of baseline threshold
DECAY              = 0.95    # per-recalibration decay toward neutral (1.0) weights
MIN_TRADES         = 10      # minimum settled trades before adjustments activate
TIER_MIN_TRADES    = 5       # minimum tier-active trades for per-tier weight
WEIGHT_FLOOR       = 0.50
WEIGHT_CEIL        = 2.00
TIERS = ["microstructure", "regime", "structure", "funding", "institutional", "oi_momentum"]


@dataclass
class TradeRecord:
    entry_id: int
    symbol: str
    direction: str           # "long" | "short"
    coherence: float
    tier_scores: Dict[str, float]
    won: Optional[bool] = None
    pnl: float = 0.0
    opened_at: float = field(default_factory=time.time)
    closed_at: float = 0.0


class SignalFeedbackEngine:
    """
    Rolling 200-trade feedback window that adapts ARIA's coherence floor
    and per-tier score weights based on realized win rates.

    Flow:
      record_open()            — when bracket order confirmed placed
      record_result()          — when position closes (win/loss + pnl)
      get_adjusted_threshold() — read by risk gate before each validation
      get_tier_weights()       — read by coherence engine for weighted scoring
    """

    def __init__(self) -> None:
        self._records: deque = deque(maxlen=200)
        self._pending: Dict[int, TradeRecord] = {}   # unsettled entry_id → record
        self._current_threshold: float = BASELINE_THRESHOLD
        self._tier_weights: Dict[str, float] = {}    # empty = no override (all 1.0)

    # ── Public API ──────────────────────────────────────────────────────────────

    def record_open(
        self,
        entry_id: int,
        symbol: str,
        direction: str,
        coherence: float,
        tier_scores: Dict[str, float],
    ) -> None:
        """Register a new open position for tracking."""
        rec = TradeRecord(
            entry_id=entry_id,
            symbol=symbol,
            direction=direction,
            coherence=coherence,
            tier_scores=dict(tier_scores),
        )
        self._pending[entry_id] = rec
        logger.debug("feedback_open", entry_id=entry_id, symbol=symbol, coherence=coherence)

    def record_result(self, entry_id: int, won: bool, pnl: float = 0.0) -> None:
        """Settle a trade with its outcome. Triggers recalibration."""
        rec = self._pending.pop(entry_id, None)
        if rec is None:
            return  # entry not registered (pre-feedback startup trade)
        rec.won = won
        rec.pnl = pnl
        rec.closed_at = time.time()
        self._records.append(rec)
        self._recalibrate()
        logger.info(
            "feedback_result",
            entry_id=entry_id,
            won=won,
            pnl=f"{pnl:.4f}",
            total_settled=len(self._records),
        )

    def get_adjusted_threshold(self) -> float:
        """Returns the adaptive min_coherence threshold for the risk gate."""
        return self._current_threshold

    def get_tier_weights(self) -> Dict[str, float]:
        """Returns per-tier multipliers. Empty dict = all tiers at 1.0."""
        return dict(self._tier_weights)

    def get_summary(self) -> Dict:
        settled = list(self._records)
        n = len(settled)
        wins = sum(1 for r in settled if r.won)
        return {
            "total_settled": n,
            "wins": wins,
            "win_rate": round(wins / n, 3) if n > 0 else 0.0,
            "pending": len(self._pending),
            "threshold": self._current_threshold,
            "tier_weights": dict(self._tier_weights),
            "active": n >= MIN_TRADES,
        }

    # ── Internal ────────────────────────────────────────────────────────────────

    def _recalibrate(self) -> None:
        """Recomputes threshold and tier weights from all settled trades."""
        settled = list(self._records)
        n = len(settled)
        if n < MIN_TRADES:
            return  # Hold at baseline until enough history

        wins = sum(1 for r in settled if r.won)
        # Bayesian smoothing: flat prior of MIN_TRADES trades at 50% win rate
        win_rate = (wins + MIN_TRADES * 0.5) / (n + MIN_TRADES)

        # ── Threshold adjustment ──────────────────────────────────────────────
        if win_rate < 0.40:
            adj = MAX_ADJUSTMENT * (0.40 - win_rate) / 0.40
            new_t = BASELINE_THRESHOLD * (1.0 + adj)
        elif win_rate > 0.60:
            adj = MAX_ADJUSTMENT * (win_rate - 0.60) / 0.40
            new_t = BASELINE_THRESHOLD * (1.0 - adj)
        else:
            # In healthy range — decay toward baseline
            new_t = self._current_threshold * DECAY + BASELINE_THRESHOLD * (1.0 - DECAY)

        # Clamp to ±MAX_ADJUSTMENT of baseline
        lo = BASELINE_THRESHOLD * (1.0 - MAX_ADJUSTMENT)
        hi = BASELINE_THRESHOLD * (1.0 + MAX_ADJUSTMENT)
        self._current_threshold = round(max(lo, min(hi, new_t)), 3)

        # ── Per-tier weight adjustments ───────────────────────────────────────
        for tier in TIERS:
            tier_recs = [r for r in settled if r.tier_scores.get(tier, 0) > 0]
            current_w = self._tier_weights.get(tier, 1.0)

            if len(tier_recs) < TIER_MIN_TRADES:
                # Insufficient tier-specific history — decay toward neutral
                self._tier_weights[tier] = round(
                    current_w * DECAY + 1.0 * (1.0 - DECAY), 4
                )
                continue

            tier_wins = sum(1 for r in tier_recs if r.won)
            tier_win_rate = tier_wins / len(tier_recs)

            # Ratio: tier-active win rate vs overall win rate
            ratio = tier_win_rate / win_rate if win_rate > 0 else 1.0

            # Decay existing weight toward 1.0, then apply ratio signal
            decayed = current_w * DECAY + 1.0 * (1.0 - DECAY)
            new_w = max(WEIGHT_FLOOR, min(WEIGHT_CEIL, decayed * ratio))
            self._tier_weights[tier] = round(new_w, 4)

        logger.info(
            "feedback_recalibrated",
            n=n,
            win_rate=f"{win_rate:.3f}",
            threshold=self._current_threshold,
            tier_weights={k: f"{v:.3f}" for k, v in self._tier_weights.items()},
        )
