"""
AdaptiveCalibrator — v2.1 Three-loop adaptive recalibration for ARIA.

Three calibration loops at different time horizons:

  FAST loop  (last 5 trades):
    - Adaptive coherence degradation on consecutive losses (not a hard lock).
    - Each loss in recovery: coherence_min += 0.2, capped at 6.0.
    - Each win: decay 10% back toward config baseline.

  MEDIUM loop (last 10 trades):
    - Compute per-tier win rate vs portfolio win rate.
    - Adjust tier weight: ratio = tier_wr / portfolio_wr.
    - Clip weights to [0.5, 2.0].

  CASCADE AFTERMATH loop (last 20 cascade-context trades):
    - Track win rate of trades taken after cascade_primed=True.
    - Adjust cascade_min_coherence (raise if aftermath trades lose).

  MOMENTUM CASCADE loop (last 20 momentum trades):
    - Track win rate and P&L.
    - Raise momentum_velocity_threshold when momentum trades losing.

  PHASE LEARNING loop (v2.1 — per-phase, per-funding_alignment buckets):
    - For each (liq_phase, funding_aligned) combination, track win rate.
    - Adjust phase-specific size_mult and strategy_type_preference.
    - Requires MIN_PHASE_TRADES (5) per bucket before adjustment.

  RECOVERY MODE (v2.1 — replaces hard 7-loss lock):
    - Trigger: drawdown > RECOVERY_DD_THRESHOLD OR 10-trade win_rate < RECOVERY_WR_THRESHOLD.
    - Behaviors:
        * size_cap: 0.5 (reduce, never increase)
        * min_coherence_override: 5.6 (top percentile signals only)
        * max_duration_min: 20 (short-duration trades)
        * tp_sl_factor: 0.8 (tighter TP/SL)
    - Exit: 5 consecutive wins OR wr_10 > 0.50.
    - No hard position block — only size and threshold tightening.

v2.1 vs v1.x:
  - Removed: hard lock after 7 losses (_LOSS_LOCK_THRESHOLD).
  - Added: Recovery Mode with adaptive degradation per-loss.
  - Added: Phase-aware learning buckets keyed by (phase, funding_aligned).
  - Added: Recovery Mode exit condition based on consecutive wins.
  - Added: get_phase_params() for interpreter to read phase-specific params.
  - LOSS_STREAK_TRIGGER raised 4 → 5 (fewer false triggers in choppy markets).
"""

import time
import structlog
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Any, Tuple

log = structlog.get_logger(__name__)

FAST_WINDOW         = 5
MEDIUM_WINDOW       = 10
CASCADE_WINDOW      = 20
MOMENTUM_WINDOW     = 20
PHASE_WINDOW        = 30      # Larger window for phase learning (slower signal)

COHERENCE_STEP      = 0.2     # v2.1: smaller step (was 0.5) — less aggressive
COHERENCE_MIN_FLOOR = 1.0
COHERENCE_MIN_CEIL  = 6.0     # v2.1: raised ceiling matches recovery_min ceiling
TIER_WEIGHT_FLOOR   = 0.5
TIER_WEIGHT_CEIL    = 2.0
LOSS_STREAK_TRIGGER = 5       # v2.1: raised from 4 — fewer false triggers

# Recovery Mode thresholds
RECOVERY_DD_THRESHOLD = 0.03  # 3% drawdown from equity peak triggers recovery
RECOVERY_WR_THRESHOLD = 0.35  # < 35% win rate over 10 trades triggers recovery
RECOVERY_WR_EXIT      = 0.50  # > 50% win rate over 10 trades exits recovery
RECOVERY_WIN_STREAK   = 5     # 5 consecutive wins exits recovery
RECOVERY_COHERENCE    = 5.6   # Minimum score in recovery mode
RECOVERY_SIZE_CAP     = 0.5   # Maximum size multiplier in recovery
RECOVERY_TP_SL_FACTOR = 0.8   # Tighter TP/SL in recovery (0.8× ATR multiplier)
RECOVERY_MAX_DUR_MIN  = 20    # Short-duration trades in recovery

MIN_PHASE_TRADES    = 5       # Minimum trades before phase learning adjusts


@dataclass
class ClosedTrade:
    won: bool
    pnl: float
    strategy_tag: str = "unknown"
    cascade_phase: str = "none"     # "primed" | "momentum" | "none"
    liq_phase: str = "none"         # LiqPhase at trade entry
    funding_aligned: bool = False   # Was SFS aligned with trade direction?
    tier_scores: Dict[str, float] = field(default_factory=dict)
    closed_at: float = field(default_factory=time.time)


@dataclass
class RecoveryState:
    active: bool = False
    entered_at: float = 0.0
    consecutive_wins: int = 0
    reason: str = ""          # "drawdown" | "win_rate"

    def activate(self, reason: str) -> None:
        self.active = True
        self.entered_at = time.time()
        self.consecutive_wins = 0
        self.reason = reason

    def deactivate(self) -> None:
        self.active = False
        self.consecutive_wins = 0
        self.reason = ""


class AdaptiveCalibrator:
    """
    Short-window adaptive recalibration system with phase learning and recovery mode.

    Reads from:
      - on_trade_closed(...)          — feed every closed trade
      - update_drawdown(pct)          — feed current drawdown from equity peak

    Exposes:
      - get_coherence_minimum()       → float  (use instead of config.min_coherence)
      - get_tier_weights()            → Dict[str, float]
      - get_cascade_min_coherence()   → float
      - is_in_recovery()              → bool
      - get_recovery_params()         → Dict  (size_cap, coherence_min, tp_sl_factor, etc.)
      - get_phase_params(liq_phase, funding_aligned) → Dict  (size_mult_adj, strategy_pref)
      - get_calibration_summary()     → Dict
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
        self._win_streak: int = 0

        self._fast_window: deque = deque(maxlen=FAST_WINDOW)
        self._medium_window: deque = deque(maxlen=MEDIUM_WINDOW)
        self._cascade_window: deque = deque(maxlen=CASCADE_WINDOW)
        self._momentum_window: deque = deque(maxlen=MOMENTUM_WINDOW)

        # Context-segmented performance
        self._context_performance: Dict[str, Dict[str, Dict[str, int]]] = {}
        self._last_context_mode: str = "normal"

        # v2.1: Phase learning — keyed by (liq_phase, funding_aligned)
        # Each bucket: {"wins": int, "losses": int, "size_mult_adj": float, "strategy_pref": str}
        self._phase_learning: Dict[Tuple[str, bool], Dict] = {}
        self._phase_window: deque = deque(maxlen=PHASE_WINDOW)

        # v2.1: Recovery Mode
        self._recovery = RecoveryState()
        self._current_drawdown_pct: float = 0.0

    # ── Public API ──────────────────────────────────────────────────────────────

    def on_trade_closed(
        self,
        won: bool,
        pnl: float,
        strategy_tag: str = "unknown",
        cascade_phase: str = "none",
        liq_phase: str = "none",
        funding_aligned: bool = False,
        tier_scores: Optional[Dict[str, float]] = None,
        market_context: Any = None,
    ) -> None:
        """
        Feed every closed trade result.

        liq_phase: LiqPhase.value at trade entry ("quiet"|"trigger"|"expansion"|"exhaustion"|"aftermath")
        funding_aligned: True when SFS agreed with trade direction at entry.
        """
        trade = ClosedTrade(
            won=won,
            pnl=pnl,
            strategy_tag=strategy_tag,
            cascade_phase=cascade_phase,
            liq_phase=liq_phase,
            funding_aligned=funding_aligned,
            tier_scores=tier_scores or {},
        )
        self._fast_window.append(trade)
        self._medium_window.append(trade)
        self._phase_window.append(trade)

        # Streak tracking
        if won:
            self._loss_streak = 0
            self._win_streak += 1
        else:
            self._win_streak = 0
            self._loss_streak += 1

        # Context-segmented performance
        ctx_mode = "normal"
        if market_context is not None:
            ctx_mode = getattr(market_context, "market_mode", "normal")
        elif cascade_phase == "primed":
            ctx_mode = "cascade_primed"
        elif cascade_phase == "momentum":
            ctx_mode = "cascade_momentum"
        self._last_context_mode = ctx_mode

        if ctx_mode not in self._context_performance:
            self._context_performance[ctx_mode] = {}
        ctx = self._context_performance[ctx_mode]

        for tier, score in (tier_scores or {}).items():
            if score <= 0:
                continue
            if tier not in ctx:
                ctx[tier] = {"wins": 0, "losses": 0}
            if won: ctx[tier]["wins"] += 1
            else:   ctx[tier]["losses"] += 1

        if "_all" not in ctx:
            ctx["_all"] = {"wins": 0, "losses": 0}
        if won: ctx["_all"]["wins"] += 1
        else:   ctx["_all"]["losses"] += 1

        # Route cascade trades
        if cascade_phase == "primed":
            self._cascade_window.append(trade)
        elif cascade_phase == "momentum":
            self._momentum_window.append(trade)

        # Run calibration loops
        self._fast_loop_check()
        self._medium_loop_check()
        self._cascade_loop_check()
        self._phase_loop_check()
        self._check_recovery_transition(won)

    def update_drawdown(self, drawdown_pct: float) -> None:
        """
        Feed current drawdown from equity peak (e.g. 0.05 = 5%).
        Call from drawdown_guard on every balance update.
        Triggers recovery mode if drawdown exceeds threshold.
        """
        self._current_drawdown_pct = drawdown_pct
        if not self._recovery.active and drawdown_pct >= RECOVERY_DD_THRESHOLD:
            self._recovery.activate("drawdown")
            log.warning(
                "recovery_mode_activated",
                reason="drawdown",
                drawdown_pct=round(drawdown_pct * 100, 2),
                coherence_min=RECOVERY_COHERENCE,
                size_cap=RECOVERY_SIZE_CAP,
            )

    def get_coherence_minimum(self) -> float:
        """Current adaptive coherence minimum. Returns recovery override when active."""
        if self._recovery.active:
            return max(self._coherence_min, RECOVERY_COHERENCE)
        return self._coherence_min

    def get_cascade_min_coherence(self) -> float:
        return self._cascade_coherence_min

    def get_tier_weights(self) -> Dict[str, float]:
        return dict(self._tier_weights)

    def is_in_recovery(self) -> bool:
        return self._recovery.active

    def get_recovery_params(self) -> Dict:
        """
        Returns recovery mode execution parameters.
        Call from risk engine / candidate builder to apply recovery constraints.

        Returns empty dict when NOT in recovery (no overhead for normal path).
        """
        if not self._recovery.active:
            return {}
        return {
            "size_cap":        RECOVERY_SIZE_CAP,
            "coherence_min":   RECOVERY_COHERENCE,
            "tp_sl_factor":    RECOVERY_TP_SL_FACTOR,
            "max_duration_min": RECOVERY_MAX_DUR_MIN,
            "reason":          self._recovery.reason,
            "consecutive_wins": self._recovery.consecutive_wins,
        }

    def get_phase_params(
        self,
        liq_phase: str,
        funding_aligned: bool,
    ) -> Dict:
        """
        Return learned phase-specific parameters.

        liq_phase: LiqPhase.value ("quiet"|"trigger"|"expansion"|"exhaustion"|"aftermath")
        funding_aligned: True when SFS matches liq direction.

        Returns:
          size_mult_adj: multiplicative adjustment (default 1.0)
          strategy_pref: "momentum" | "reversal" | "any" (learned preference)
        """
        bucket = self._phase_learning.get((liq_phase, funding_aligned))
        if bucket is None:
            return {"size_mult_adj": 1.0, "strategy_pref": "any"}
        return {
            "size_mult_adj": bucket.get("size_mult_adj", 1.0),
            "strategy_pref": bucket.get("strategy_pref", "any"),
        }

    def get_calibration_summary(self) -> Dict:
        fast_trades     = list(self._fast_window)
        medium_trades   = list(self._medium_window)
        cascade_trades  = list(self._cascade_window)
        momentum_trades = list(self._momentum_window)

        def _wr(trades):
            if not trades: return 0.0
            return sum(1 for t in trades if t.won) / len(trades)

        context_summary: Dict[str, float] = {}
        for mode, tiers in self._context_performance.items():
            bucket = tiers.get("_all", {})
            total  = bucket.get("wins", 0) + bucket.get("losses", 0)
            if total >= self.MIN_CONTEXT_TRADES:
                context_summary[mode] = round(bucket["wins"] / total, 3)

        phase_summary = {
            f"{ph},{fa}": {
                "wins": b["wins"],
                "losses": b["losses"],
                "size_adj": round(b.get("size_mult_adj", 1.0), 3),
                "pref": b.get("strategy_pref", "any"),
            }
            for (ph, fa), b in self._phase_learning.items()
        }

        return {
            "coherence_min":           self._coherence_min,
            "coherence_effective":     self.get_coherence_minimum(),
            "cascade_coherence_min":   self._cascade_coherence_min,
            "momentum_vel_threshold":  self._momentum_velocity_threshold,
            "loss_streak":             self._loss_streak,
            "win_streak":              self._win_streak,
            "fast_wr":                 round(_wr(fast_trades), 3),
            "medium_wr":               round(_wr(medium_trades), 3),
            "cascade_wr":              round(_wr(cascade_trades), 3),
            "momentum_wr":             round(_wr(momentum_trades), 3),
            "fast_n":                  len(fast_trades),
            "medium_n":                len(medium_trades),
            "cascade_n":               len(cascade_trades),
            "momentum_n":              len(momentum_trades),
            "tier_weights":            dict(self._tier_weights),
            "context_wr":              context_summary,
            "last_context_mode":       self._last_context_mode,
            "recovery_active":         self._recovery.active,
            "recovery_reason":         self._recovery.reason,
            "recovery_wins":           self._recovery.consecutive_wins,
            "drawdown_pct":            round(self._current_drawdown_pct * 100, 2),
            "phase_learning":          phase_summary,
        }

    MIN_CONTEXT_TRADES = 3

    def get_context_win_rate(
        self,
        context_mode: str,
        tier: str = "_all",
    ) -> Optional[float]:
        ctx = self._context_performance.get(context_mode, {})
        bucket = ctx.get(tier)
        if bucket is None:
            return None
        total = bucket["wins"] + bucket["losses"]
        if total < self.MIN_CONTEXT_TRADES:
            return None
        return bucket["wins"] / total

    # ── Loop implementations ────────────────────────────────────────────────────

    def _fast_loop_check(self) -> None:
        """
        Fast loop (5-trade window) — adaptive coherence degradation.

        v2.1 replaces hard lock:
          - Each loss at >= LOSS_STREAK_TRIGGER: coherence_min += COHERENCE_STEP
          - Each win: decay 10% back toward config baseline
          - No hard block — only threshold adjustment
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

            # Also check win-rate for recovery activation
            trades = list(self._medium_window)
            if len(trades) >= MEDIUM_WINDOW:
                wr = sum(1 for t in trades if t.won) / len(trades)
                if not self._recovery.active and wr < RECOVERY_WR_THRESHOLD:
                    self._recovery.activate("win_rate")
                    log.warning(
                        "recovery_mode_activated",
                        reason="win_rate",
                        wr_10=round(wr, 3),
                        coherence_min=RECOVERY_COHERENCE,
                        size_cap=RECOVERY_SIZE_CAP,
                    )

        elif self._loss_streak == 0 and self._coherence_min > config_baseline:
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
        Medium loop (10-trade window) — per-tier weight adjustment.
        """
        trades = list(self._medium_window)
        if len(trades) < MEDIUM_WINDOW:
            return

        wins = [t for t in trades if t.won]
        portfolio_wr = len(wins) / len(trades) if trades else 0.5

        all_tiers: Dict[str, List[float]] = {}
        for trade in trades:
            for tier, score in trade.tier_scores.items():
                all_tiers.setdefault(tier, []).append(score)

        for tier, scores in all_tiers.items():
            active_trades = [t for t in trades if t.tier_scores.get(tier, 0.0) > 0]
            if len(active_trades) < 5:
                continue
            tier_wins = sum(1 for t in active_trades if t.won)
            tier_wr   = tier_wins / len(active_trades)

            ratio     = tier_wr / portfolio_wr if portfolio_wr > 0 else 1.0
            current_w = self._tier_weights.get(tier, 1.0)
            new_w     = current_w * 0.80 + ratio * 0.20
            new_w     = round(max(TIER_WEIGHT_FLOOR, min(TIER_WEIGHT_CEIL, new_w)), 4)
            self._tier_weights[tier] = new_w

        log.info("adaptive_medium_recalibrated",
                 portfolio_wr=round(portfolio_wr, 3),
                 tier_weights={k: f"{v:.3f}" for k, v in self._tier_weights.items()})

    def _cascade_loop_check(self) -> None:
        """
        Cascade aftermath loop — adjusts cascade coherence minimum.
        """
        cascade_trades = list(self._cascade_window)
        if len(cascade_trades) < 5:
            return

        wins = sum(1 for t in cascade_trades if t.won)
        wr   = wins / len(cascade_trades)
        config_baseline = float(getattr(self._config, "cascade_min_coherence", 3.0))

        if wr < 0.40:
            new_min = min(COHERENCE_MIN_CEIL, self._cascade_coherence_min + COHERENCE_STEP)
            if new_min != self._cascade_coherence_min:
                self._cascade_coherence_min = new_min
                log.warning("cascade_coherence_raised",
                             cascade_wr=round(wr, 3), cascade_min=new_min)
        elif wr > 0.60 and self._cascade_coherence_min > config_baseline:
            self._cascade_coherence_min = max(
                config_baseline,
                self._cascade_coherence_min - COHERENCE_STEP * 0.5,
            )
            log.info("cascade_coherence_lowered",
                     cascade_wr=round(wr, 3), cascade_min=self._cascade_coherence_min)

    def _phase_loop_check(self) -> None:
        """
        Phase learning loop — adjusts size_mult_adj and strategy_pref per phase bucket.

        Buckets: (liq_phase, funding_aligned)
        Requires MIN_PHASE_TRADES per bucket before any adjustment.

        size_mult_adj:
          wr ≥ 0.60 → 1.1× (phase+alignment is productive → trade larger)
          wr ≤ 0.35 → 0.85× (phase+alignment is losing → trade smaller)
          else → 1.0× (no adjustment)

        strategy_pref:
          Set to the winning strategy_tag for this bucket when wr ≥ 0.60
          and one strategy dominates (>60% of wins).
          Otherwise "any".
        """
        trades = list(self._phase_window)
        if len(trades) < MIN_PHASE_TRADES:
            return

        # Group by (liq_phase, funding_aligned)
        buckets: Dict[Tuple[str, bool], List[ClosedTrade]] = {}
        for t in trades:
            key = (t.liq_phase, t.funding_aligned)
            buckets.setdefault(key, []).append(t)

        for key, bucket_trades in buckets.items():
            if len(bucket_trades) < MIN_PHASE_TRADES:
                continue

            wins   = sum(1 for t in bucket_trades if t.won)
            wr     = wins / len(bucket_trades)
            liq_phase, funding_aligned = key

            # Size adjustment
            if wr >= 0.60:
                size_adj = 1.10
            elif wr <= 0.35:
                size_adj = 0.85
            else:
                size_adj = 1.0

            # Strategy preference (only set when clear winner emerges)
            strategy_counts: Dict[str, int] = {}
            for t in bucket_trades:
                if t.won:
                    strategy_counts[t.strategy_tag] = \
                        strategy_counts.get(t.strategy_tag, 0) + 1
            strategy_pref = "any"
            if wins > 0:
                best_strat = max(strategy_counts, key=lambda k: strategy_counts[k])
                if strategy_counts[best_strat] / wins >= 0.60:
                    strategy_pref = best_strat

            prev = self._phase_learning.get(key, {})
            prev_adj  = prev.get("size_mult_adj", 1.0)
            # Smooth: 80% old + 20% new
            smooth_adj = round(prev_adj * 0.80 + size_adj * 0.20, 4)

            self._phase_learning[key] = {
                "wins":          wins,
                "losses":        len(bucket_trades) - wins,
                "wr":            round(wr, 3),
                "size_mult_adj": smooth_adj,
                "strategy_pref": strategy_pref,
            }

            log.info(
                "phase_learning_updated",
                liq_phase=liq_phase,
                funding_aligned=funding_aligned,
                wr=round(wr, 3),
                size_mult_adj=smooth_adj,
                strategy_pref=strategy_pref,
                n=len(bucket_trades),
            )

    def _check_recovery_transition(self, won: bool) -> None:
        """Check for recovery mode entry/exit after each trade."""
        if self._recovery.active:
            if won:
                self._recovery.consecutive_wins += 1
            else:
                self._recovery.consecutive_wins = 0

            # Check exit conditions
            trades = list(self._medium_window)
            wr_10 = (sum(1 for t in trades if t.won) / len(trades)) if trades else 0.0
            streak_exit = self._recovery.consecutive_wins >= RECOVERY_WIN_STREAK
            wr_exit     = wr_10 > RECOVERY_WR_EXIT and len(trades) >= MEDIUM_WINDOW

            if streak_exit or wr_exit:
                self._recovery.deactivate()
                log.info(
                    "recovery_mode_deactivated",
                    reason="streak" if streak_exit else "win_rate",
                    consecutive_wins=self._recovery.consecutive_wins,
                    wr_10=round(wr_10, 3),
                )
