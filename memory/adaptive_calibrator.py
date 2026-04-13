"""
AdaptiveCalibrator — Three-loop adaptive recalibration for ARIA.

Three calibration loops at different time horizons:

  FAST loop  (last 5 trades):
    - If loss_streak ≥ 4: raise coherence_min by +0.5 (clamp [1.0, 5.0])
    - Resets when streak breaks (win arrives)
    - Purpose: immediate protection from a deteriorating market condition

  MEDIUM loop (last 10 trades):
    - Compute per-tier win rate vs portfolio win rate
    - Adjust tier weight: ratio = tier_wr / portfolio_wr
    - Clip weights to [0.5, 2.0]
    - Purpose: down-weight tiers that consistently underperform

  CASCADE AFTERMATH tracking (last 20 cascade-context trades):
    - Track win rate of trades taken after cascade_primed=True
    - Adjust cascade_min_coherence (raise if cascade aftermath trades lose)

  MOMENTUM CASCADE tracking (last 20 momentum trades):
    - Track win rate and P&L of momentum cascade entries
    - Adjust momentum_velocity_threshold upward if momentum trades losing

Compared to SignalFeedbackEngine (which does global + per-symbol + per-regime
recalibration on 200-trade window), this calibrator operates on shorter windows
for faster adaptation to regime shifts.
"""

import time
import structlog
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional, List

log = structlog.get_logger(__name__)

FAST_WINDOW        = 5
MEDIUM_WINDOW      = 10
CASCADE_WINDOW     = 20
COHERENCE_STEP     = 0.5      # Raise coherence min by this amount on loss streak
COHERENCE_MIN_FLOOR = 1.0
COHERENCE_MIN_CEIL  = 5.0
TIER_WEIGHT_FLOOR   = 0.5
TIER_WEIGHT_CEIL    = 2.0
LOSS_STREAK_TRIGGER = 4       # Fast-block threshold for adaptive calibrator


@dataclass
class ClosedTrade:
    won: bool
    pnl: float
    strategy_tag: str = "unknown"
    cascade_phase: str = "none"     # "primed" | "momentum" | "none"
    tier_scores: Dict[str, float] = field(default_factory=dict)
    closed_at: float = field(default_factory=time.time)


class AdaptiveCalibrator:
    """
    Short-window adaptive recalibration system.

    Reads from:
      - on_trade_closed(trade) — feed every closed trade

    Exposes:
      - get_coherence_minimum() → float  (use instead of config.min_coherence)
      - get_tier_weights() → Dict[str, float]
      - get_cascade_min_coherence() → float
      - get_calibration_summary() → Dict

    Integration:
        calibrator = AdaptiveCalibrator(config)
        # In _record_close():
        calibrator.on_trade_closed(ClosedTrade(won=..., pnl=..., strategy_tag=...,
                                                cascade_phase=..., tier_scores=...))
        # In risk_engine / coherence gate:
        min_coh = calibrator.get_coherence_minimum()
    """

    def __init__(self, config):
        self._config = config
        self._coherence_min: float = float(
            getattr(config, "min_coherence", getattr(config, "live_min_coherence", 2.0))
        )
        self._cascade_coherence_min: float = float(
            getattr(config, "cascade_min_coherence", 3.0)
        )
        self._momentum_velocity_threshold: float = float(
            getattr(config, "momentum_velocity_threshold", 3.0)
        )

        self._tier_weights: Dict[str, float] = {}
        self._loss_streak: int = 0

        self._fast_window: deque = deque(maxlen=FAST_WINDOW)
        self._medium_window: deque = deque(maxlen=MEDIUM_WINDOW)
        self._cascade_window: deque = deque(maxlen=CASCADE_WINDOW)
        self._momentum_window: deque = deque(maxlen=CASCADE_WINDOW)

    # ── Public API ──────────────────────────────────────────────────────────────

    def on_trade_closed(
        self,
        won: bool,
        pnl: float,
        strategy_tag: str = "unknown",
        cascade_phase: str = "none",
        tier_scores: Optional[Dict[str, float]] = None,
    ) -> None:
        """
        Feed every closed trade result.
        Triggers fast / medium / cascade loop checks.
        """
        trade = ClosedTrade(
            won=won,
            pnl=pnl,
            strategy_tag=strategy_tag,
            cascade_phase=cascade_phase,
            tier_scores=tier_scores or {},
        )
        self._fast_window.append(trade)
        self._medium_window.append(trade)

        # Route cascade trades to their own windows
        if cascade_phase == "primed":
            self._cascade_window.append(trade)
        elif cascade_phase == "momentum":
            self._momentum_window.append(trade)

        # Track global loss streak
        if won:
            self._loss_streak = 0
        else:
            self._loss_streak += 1

        self._fast_loop_check()
        self._medium_loop_check()
        self._cascade_loop_check()

    def get_coherence_minimum(self) -> float:
        """Current adaptive coherence minimum (use instead of config.min_coherence)."""
        return self._coherence_min

    def get_cascade_min_coherence(self) -> float:
        """Adaptive coherence minimum for cascade-primed entries."""
        return self._cascade_coherence_min

    def get_tier_weights(self) -> Dict[str, float]:
        """Current per-tier weight multipliers (empty = all 1.0)."""
        return dict(self._tier_weights)

    def get_calibration_summary(self) -> Dict:
        fast_trades = list(self._fast_window)
        medium_trades = list(self._medium_window)
        cascade_trades = list(self._cascade_window)
        momentum_trades = list(self._momentum_window)

        def _wr(trades):
            if not trades:
                return 0.0
            return sum(1 for t in trades if t.won) / len(trades)

        return {
            "coherence_min":           self._coherence_min,
            "cascade_coherence_min":   self._cascade_coherence_min,
            "momentum_vel_threshold":  self._momentum_velocity_threshold,
            "loss_streak":             self._loss_streak,
            "fast_wr":                 round(_wr(fast_trades), 3),
            "medium_wr":               round(_wr(medium_trades), 3),
            "cascade_wr":              round(_wr(cascade_trades), 3),
            "momentum_wr":             round(_wr(momentum_trades), 3),
            "fast_n":                  len(fast_trades),
            "medium_n":                len(medium_trades),
            "cascade_n":               len(cascade_trades),
            "momentum_n":              len(momentum_trades),
            "tier_weights":            dict(self._tier_weights),
        }

    # ── Loop implementations ────────────────────────────────────────────────────

    def _fast_loop_check(self) -> None:
        """
        Fast loop (5-trade window).
        Raises coherence minimum by COHERENCE_STEP when loss_streak ≥ LOSS_STREAK_TRIGGER.
        Decays back toward config baseline when streak breaks.
        """
        config_baseline = float(
            getattr(self._config, "min_coherence",
                    getattr(self._config, "live_min_coherence", 2.0))
        )

        if self._loss_streak >= LOSS_STREAK_TRIGGER:
            new_min = min(COHERENCE_MIN_CEIL,
                         self._coherence_min + COHERENCE_STEP)
            if new_min != self._coherence_min:
                self._coherence_min = new_min
                log.warning("adaptive_coherence_raised",
                            loss_streak=self._loss_streak,
                            coherence_min=new_min)
        elif self._loss_streak == 0 and self._coherence_min > config_baseline:
            # Win broke the streak: decay 10% back toward baseline per win
            decay = 0.10
            self._coherence_min = round(
                max(config_baseline,
                    self._coherence_min * (1 - decay) + config_baseline * decay),
                3,
            )
            log.info("adaptive_coherence_decayed",
                     coherence_min=self._coherence_min,
                     baseline=config_baseline)

    def _medium_loop_check(self) -> None:
        """
        Medium loop (10-trade window).
        Adjusts per-tier weights based on tier win rate vs portfolio win rate.
        Only triggers when window is full.
        """
        trades = list(self._medium_window)
        if len(trades) < MEDIUM_WINDOW:
            return

        wins = [t for t in trades if t.won]
        portfolio_wr = len(wins) / len(trades) if trades else 0.5

        # Collect tier scores from all trades
        all_tiers: Dict[str, List[float]] = {}
        for trade in trades:
            for tier, score in trade.tier_scores.items():
                all_tiers.setdefault(tier, []).append(score)

        for tier, scores in all_tiers.items():
            # Only adjust tiers that were active (score > 0) in ≥5 trades
            active_trades = [t for t in trades if t.tier_scores.get(tier, 0.0) > 0]
            if len(active_trades) < 5:
                continue
            tier_wins = sum(1 for t in active_trades if t.won)
            tier_wr = tier_wins / len(active_trades)

            ratio = tier_wr / portfolio_wr if portfolio_wr > 0 else 1.0
            current_w = self._tier_weights.get(tier, 1.0)
            # Smooth: 80% old + 20% new
            new_w = current_w * 0.80 + ratio * 0.20
            new_w = round(max(TIER_WEIGHT_FLOOR, min(TIER_WEIGHT_CEIL, new_w)), 4)
            self._tier_weights[tier] = new_w

        log.info("adaptive_medium_recalibrated",
                 portfolio_wr=round(portfolio_wr, 3),
                 tier_weights={k: f"{v:.3f}" for k, v in self._tier_weights.items()})

    def _cascade_loop_check(self) -> None:
        """
        Cascade aftermath loop (20-trade cascade-context window).
        Raises cascade_coherence_min when aftermath trades are losing.
        """
        cascade_trades = list(self._cascade_window)
        if len(cascade_trades) < 5:
            return

        wins = sum(1 for t in cascade_trades if t.won)
        wr = wins / len(cascade_trades)

        config_cascade_baseline = float(
            getattr(self._config, "cascade_min_coherence", 3.0)
        )

        if wr < 0.40:
            new_min = min(COHERENCE_MIN_CEIL,
                         self._cascade_coherence_min + COHERENCE_STEP)
            if new_min != self._cascade_coherence_min:
                self._cascade_coherence_min = new_min
                log.warning("cascade_coherence_raised",
                            cascade_wr=round(wr, 3),
                            cascade_min=new_min)
        elif wr > 0.60 and self._cascade_coherence_min > config_cascade_baseline:
            self._cascade_coherence_min = max(
                config_cascade_baseline,
                self._cascade_coherence_min - COHERENCE_STEP * 0.5,
            )
            log.info("cascade_coherence_lowered",
                     cascade_wr=round(wr, 3),
                     cascade_min=self._cascade_coherence_min)
