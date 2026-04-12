"""
SignalFeedbackEngine v2 — Adaptive coherence calibration from trade outcomes.

Self-improvement architecture:
  Global level:
    - min_coherence threshold (±20% of baseline 2.0)
    - per-tier score weights (0.50–2.00, decay toward 1.0)

  Per-symbol level (new v2):
    - per-symbol coherence floor calibrated from that symbol's win/loss history
    - activates after MIN_SYMBOL_TRADES settled for that symbol

  Per-regime level (new v2):
    - separate thresholds for risk_on, risk_off, rotational regimes
    - risk_off markets typically require higher conviction

  Time-of-day level (new v2):
    - 6-hour bucket multiplier (0.5 × normal size during historically bad hours)
    - activates after MIN_HOUR_TRADES settled for that hour bucket

All adjustments use Bayesian smoothing (flat prior) to avoid overfitting on
small samples. Requires ≥10 settled trades for global, ≥5 for symbol/regime.
"""
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional
import structlog

logger = structlog.get_logger(__name__)

BASELINE_THRESHOLD  = 2.0
MAX_ADJUSTMENT      = 0.20   # ±20% of baseline
DECAY               = 0.95   # per-recalibration decay toward neutral (1.0)
MIN_TRADES          = 10     # minimum settled before global adjustments activate
MIN_SYMBOL_TRADES   = 5      # minimum settled per symbol before symbol-level adjust
MIN_REGIME_TRADES   = 8      # minimum per regime
MIN_HOUR_TRADES     = 4      # minimum per 6h bucket
TIER_MIN_TRADES     = 5
WEIGHT_FLOOR        = 0.50
WEIGHT_CEIL         = 2.00
TIERS = ["microstructure", "regime", "structure", "funding", "institutional", "oi_momentum"]

# Time-of-day bucket: UTC hours → 4 buckets (0-5, 6-11, 12-17, 18-23)
_HOUR_BUCKETS = 4


@dataclass
class TradeRecord:
    entry_id: int
    symbol: str
    direction: str           # "long" | "short"
    coherence: float
    tier_scores: Dict[str, float]
    regime: str = "neutral"  # v2: regime at entry time
    won: Optional[bool] = None
    pnl: float = 0.0
    opened_at: float = field(default_factory=time.time)
    closed_at: float = 0.0

    @property
    def hour_bucket(self) -> int:
        """0-3, one per 6h UTC block."""
        import datetime
        return datetime.datetime.utcfromtimestamp(self.opened_at).hour // 6


class SignalFeedbackEngine:
    """
    Rolling 200-trade feedback window that adapts ARIA's coherence floor
    and per-tier score weights based on realized win rates.

    v2 additions:
      - per-symbol thresholds (learn which symbols work best at what score)
      - regime-aware thresholds (risk_off needs higher conviction)
      - time-of-day size multipliers (reduce size in historically bad hours)
      - returns per-symbol threshold in get_symbol_threshold()
    """

    def __init__(self) -> None:
        self._records: deque = deque(maxlen=200)
        self._pending: Dict[int, TradeRecord] = {}

        # Global
        self._current_threshold: float = BASELINE_THRESHOLD
        self._tier_weights: Dict[str, float] = {}

        # Per-symbol thresholds
        self._symbol_thresholds: Dict[str, float] = {}

        # Per-regime thresholds
        self._regime_thresholds: Dict[str, float] = {
            "risk_on":    BASELINE_THRESHOLD,
            "risk_off":   BASELINE_THRESHOLD * 1.10,  # start 10% higher for risk_off
            "rotational": BASELINE_THRESHOLD,
        }

        # Time-of-day size multipliers (1.0 = normal)
        self._hour_multipliers: Dict[int, float] = {i: 1.0 for i in range(_HOUR_BUCKETS)}

    # ── Public API ──────────────────────────────────────────────────────────────

    def record_open(
        self,
        entry_id: int,
        symbol: str,
        direction: str,
        coherence: float,
        tier_scores: Dict[str, float],
        regime: str = "neutral",
    ) -> None:
        """Register a new open position for tracking."""
        rec = TradeRecord(
            entry_id=entry_id,
            symbol=symbol,
            direction=direction,
            coherence=coherence,
            tier_scores=dict(tier_scores),
            regime=regime,
        )
        self._pending[entry_id] = rec
        logger.debug("feedback_open", entry_id=entry_id, symbol=symbol, coherence=coherence)

    def record_result(self, entry_id: int, won: bool, pnl: float = 0.0) -> None:
        """Settle a trade with its outcome. Triggers recalibration."""
        rec = self._pending.pop(entry_id, None)
        if rec is None:
            return
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

    def get_adjusted_threshold(self, symbol: str = None, regime: str = None) -> float:
        """
        Returns the adaptive min_coherence threshold.

        Priority:
          1. Per-symbol override (if ≥MIN_SYMBOL_TRADES for this symbol)
          2. Per-regime override (if ≥MIN_REGIME_TRADES for this regime)
          3. Global threshold
        """
        settled = list(self._records)

        if symbol:
            sym_recs = [r for r in settled if r.symbol == symbol]
            if len(sym_recs) >= MIN_SYMBOL_TRADES:
                return self._symbol_thresholds.get(symbol, self._current_threshold)

        if regime and regime in self._regime_thresholds:
            regime_recs = [r for r in settled if r.regime == regime]
            if len(regime_recs) >= MIN_REGIME_TRADES:
                return self._regime_thresholds[regime]

        return self._current_threshold

    def get_tier_weights(self) -> Dict[str, float]:
        """Returns per-tier multipliers. Empty dict = all tiers at 1.0."""
        return dict(self._tier_weights)

    def get_hour_multiplier(self) -> float:
        """
        Returns the time-of-day size multiplier for the current UTC hour.
        Range [0.5, 1.2] — reduces size during historically losing hours.
        """
        bucket = time.gmtime().tm_hour // 6
        return self._hour_multipliers.get(bucket, 1.0)

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
            "symbol_thresholds": dict(self._symbol_thresholds),
            "regime_thresholds": dict(self._regime_thresholds),
            "hour_multipliers": dict(self._hour_multipliers),
            "active": n >= MIN_TRADES,
        }

    # ── Internal ────────────────────────────────────────────────────────────────

    def _bayesian_win_rate(self, wins: int, n: int, prior_n: int = 10) -> float:
        """
        Bayesian smoothed win rate with flat 50% prior.
        More robust than raw win rate with small samples.
        """
        return (wins + prior_n * 0.5) / (n + prior_n)

    def _threshold_from_win_rate(self, win_rate: float, baseline: float) -> float:
        """Convert win rate to threshold using the standard linear mapping."""
        if win_rate < 0.40:
            adj = MAX_ADJUSTMENT * (0.40 - win_rate) / 0.40
            return baseline * (1.0 + adj)
        elif win_rate > 0.60:
            adj = MAX_ADJUSTMENT * (win_rate - 0.60) / 0.40
            return baseline * (1.0 - adj)
        else:
            # Decay toward baseline — if win rate is healthy, ease off
            return self._current_threshold * DECAY + baseline * (1.0 - DECAY)

    def _recalibrate(self) -> None:
        """Recomputes all thresholds and weights from settled trades."""
        settled = list(self._records)
        n = len(settled)
        if n < MIN_TRADES:
            return

        wins = sum(1 for r in settled if r.won)
        win_rate = self._bayesian_win_rate(wins, n)

        # ── Global threshold ──────────────────────────────────────────────────
        new_t = self._threshold_from_win_rate(win_rate, BASELINE_THRESHOLD)
        lo = BASELINE_THRESHOLD * (1.0 - MAX_ADJUSTMENT)
        hi = BASELINE_THRESHOLD * (1.0 + MAX_ADJUSTMENT)
        self._current_threshold = round(max(lo, min(hi, new_t)), 3)

        # ── Per-symbol thresholds ─────────────────────────────────────────────
        symbols = {r.symbol for r in settled}
        for sym in symbols:
            sym_recs = [r for r in settled if r.symbol == sym]
            if len(sym_recs) < MIN_SYMBOL_TRADES:
                continue
            sym_wins = sum(1 for r in sym_recs if r.won)
            sym_wr = self._bayesian_win_rate(sym_wins, len(sym_recs), prior_n=5)
            new_sym_t = self._threshold_from_win_rate(sym_wr, BASELINE_THRESHOLD)
            self._symbol_thresholds[sym] = round(max(lo, min(hi, new_sym_t)), 3)

        # ── Per-regime thresholds ─────────────────────────────────────────────
        for regime in ("risk_on", "risk_off", "rotational"):
            reg_recs = [r for r in settled if r.regime == regime]
            if len(reg_recs) < MIN_REGIME_TRADES:
                continue
            reg_wins = sum(1 for r in reg_recs if r.won)
            reg_wr = self._bayesian_win_rate(reg_wins, len(reg_recs), prior_n=8)
            reg_base = BASELINE_THRESHOLD * (1.10 if regime == "risk_off" else 1.0)
            new_reg_t = self._threshold_from_win_rate(reg_wr, reg_base)
            self._regime_thresholds[regime] = round(max(lo, min(hi * 1.10, new_reg_t)), 3)

        # ── Time-of-day multipliers ───────────────────────────────────────────
        for bucket in range(_HOUR_BUCKETS):
            hour_recs = [r for r in settled if r.hour_bucket == bucket]
            if len(hour_recs) < MIN_HOUR_TRADES:
                continue
            hour_wins = sum(1 for r in hour_recs if r.won)
            hour_wr = self._bayesian_win_rate(hour_wins, len(hour_recs), prior_n=4)
            # Map win_rate to multiplier: <35% → 0.5x, 35-65% → linear 0.5-1.2x, >65% → 1.2x
            if hour_wr < 0.35:
                mult = 0.5
            elif hour_wr > 0.65:
                mult = 1.2
            else:
                mult = 0.5 + (hour_wr - 0.35) / 0.30 * 0.7
            prev = self._hour_multipliers.get(bucket, 1.0)
            # Smooth: 90% old + 10% new (very slow update — don't overcorrect)
            self._hour_multipliers[bucket] = round(prev * 0.90 + mult * 0.10, 4)

        # ── Per-tier weights ──────────────────────────────────────────────────
        for tier in TIERS:
            tier_recs = [r for r in settled if r.tier_scores.get(tier, 0) > 0]
            current_w = self._tier_weights.get(tier, 1.0)
            if len(tier_recs) < TIER_MIN_TRADES:
                self._tier_weights[tier] = round(
                    current_w * DECAY + 1.0 * (1.0 - DECAY), 4
                )
                continue
            tier_wins = sum(1 for r in tier_recs if r.won)
            tier_wr = tier_wins / len(tier_recs)
            ratio = tier_wr / win_rate if win_rate > 0 else 1.0
            decayed = current_w * DECAY + 1.0 * (1.0 - DECAY)
            new_w = max(WEIGHT_FLOOR, min(WEIGHT_CEIL, decayed * ratio))
            self._tier_weights[tier] = round(new_w, 4)

        logger.info(
            "feedback_recalibrated",
            n=n,
            win_rate=f"{win_rate:.3f}",
            threshold=self._current_threshold,
            symbol_thresholds={k: f"{v:.3f}" for k, v in self._symbol_thresholds.items()},
            regime_thresholds={k: f"{v:.3f}" for k, v in self._regime_thresholds.items()},
            tier_weights={k: f"{v:.3f}" for k, v in self._tier_weights.items()},
        )
