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

BASELINE_THRESHOLD  = 3.5   # lowered for small-account signal flow
MAX_ADJ_DOWN        = 0.20   # floor = 3.5 × (1 − 0.20) = 2.8
MAX_ADJ_UP          = 0.80   # ceiling = 3.5 × (1 + 0.80) = 6.3
THRESHOLD_FLOOR     = BASELINE_THRESHOLD * (1.0 - MAX_ADJ_DOWN)   # 2.8
THRESHOLD_CEILING   = BASELINE_THRESHOLD * (1.0 + MAX_ADJ_UP)     # 6.3
MAX_ADJUSTMENT      = MAX_ADJ_DOWN  # legacy alias — used by per-regime ceiling clamp
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
    strategy_tag: str = "unknown"  # v3: which strategy generated direction
    personality: str = "SCOUT"     # v4: personality assigned at entry
    session: str = "unknown"       # v4: trading session at entry
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

        # ── Agile learning: strategy-tag consecutive-loss fast-block ─────────
        # When the last N trades tagged with the same strategy all lose, block
        # that strategy for STRATEGY_BLOCK_S seconds by raising its threshold.
        # Resets when a win arrives in that strategy context.
        # Key: strategy_tag → [bool, bool, ...] (recent outcomes, newest last)
        self._strategy_streaks: Dict[str, deque] = {}  # strategy → deque(maxlen=5)
        self._strategy_blocked_until: Dict[str, float] = {}  # strategy → unix ts
        _STRATEGY_LOSS_TRIGGER = 3     # 3 consecutive losses → block
        _STRATEGY_BLOCK_S = 1800.0     # block for 30 min
        _STRATEGY_THRESHOLD_BOOST = 1.5  # raise threshold by 50% while blocked
        self._STRATEGY_LOSS_TRIGGER = _STRATEGY_LOSS_TRIGGER
        self._STRATEGY_BLOCK_S = _STRATEGY_BLOCK_S
        self._STRATEGY_THRESHOLD_BOOST = _STRATEGY_THRESHOLD_BOOST

        # Cross-product performance matrix: (personality, session, regime) -> stats
        self._cross_product: Dict[tuple, dict] = {}

    # ── Public API ──────────────────────────────────────────────────────────────

    def record_open(
        self,
        entry_id: int,
        symbol: str,
        direction: str,
        coherence: float,
        tier_scores: Dict[str, float],
        regime: str = "neutral",
        strategy_tag: str = "unknown",
        personality: str = "SCOUT",
        session: str = "unknown",
    ) -> None:
        """Register a new open position for tracking."""
        rec = TradeRecord(
            entry_id=entry_id,
            symbol=symbol,
            direction=direction,
            coherence=coherence,
            tier_scores=dict(tier_scores),
            regime=regime,
            strategy_tag=strategy_tag,
            personality=personality,
            session=session,
        )
        self._pending[entry_id] = rec
        logger.debug("feedback_open", entry_id=entry_id, symbol=symbol,
                     coherence=coherence, strategy=strategy_tag)

    def record_result(self, entry_id: int, won: bool, pnl: float = 0.0) -> None:
        """Settle a trade with its outcome. Triggers recalibration + agile strategy learning."""
        rec = self._pending.pop(entry_id, None)
        if rec is None:
            return

        # Feedback Optimization: Ignore trades with negligible negative PnL 
        # (e.g., fee-eaten Take Profits or break-even stops) mapping to losses.
        fee_threshold = 2.0  # USD threshold
        if not won and abs(pnl) <= fee_threshold:
            logger.info("feedback_ignored_negligible_loss", entry_id=entry_id, pnl=pnl)
            return

        rec.won = won
        rec.pnl = pnl
        rec.closed_at = time.time()
        self._records.append(rec)

        # ── Agile strategy learning: update streak + apply fast-block ──────────
        stag = rec.strategy_tag
        if stag and stag != "unknown":
            if stag not in self._strategy_streaks:
                self._strategy_streaks[stag] = deque(maxlen=5)
            self._strategy_streaks[stag].append(won)

            streak = list(self._strategy_streaks[stag])
            recent_losses = sum(1 for w in streak[-self._STRATEGY_LOSS_TRIGGER:] if not w)
            if recent_losses >= self._STRATEGY_LOSS_TRIGGER:
                _block_until = time.time() + self._STRATEGY_BLOCK_S
                self._strategy_blocked_until[stag] = _block_until
                logger.warning("strategy_fast_blocked",
                               strategy=stag,
                               consecutive_losses=recent_losses,
                               block_minutes=int(self._STRATEGY_BLOCK_S / 60))
            elif won:
                # Win: clear block for this strategy
                self._strategy_blocked_until.pop(stag, None)

        # ── Cross-product performance matrix (personality × session × regime) ────
        _cp_key = (rec.personality, rec.session, rec.regime)
        if _cp_key not in self._cross_product:
            self._cross_product[_cp_key] = {"wins": 0, "losses": 0}
        if won:
            self._cross_product[_cp_key]["wins"] += 1
        else:
            self._cross_product[_cp_key]["losses"] += 1

        self._recalibrate()
        logger.info(
            "feedback_result",
            entry_id=entry_id,
            won=won,
            pnl=f"{pnl:.4f}",
            strategy=rec.strategy_tag,
            total_settled=len(self._records),
        )

    def is_strategy_blocked(self, strategy_tag: str) -> bool:
        """True if this strategy has been fast-blocked due to consecutive losses."""
        blocked_until = self._strategy_blocked_until.get(strategy_tag, 0.0)
        return time.time() < blocked_until

    def get_strategy_threshold(self, strategy_tag: str) -> float:
        """
        Returns threshold multiplier for a strategy.
        Blocked strategy → 1.5× baseline (effectively suppressed).
        Normal strategy → 1.0× (no change).
        """
        if self.is_strategy_blocked(strategy_tag):
            return self._current_threshold * self._STRATEGY_THRESHOLD_BOOST
        return self._current_threshold

    def get_adjusted_threshold(
        self, symbol: str = None, regime: str = None, strategy_tag: str = None
    ) -> float:
        """
        Returns the adaptive min_coherence threshold.

        Priority:
          0. Strategy fast-block (agile learning — fires immediately on N losses)
          1. Per-symbol override (if ≥MIN_SYMBOL_TRADES for this symbol)
          2. Per-regime override (if ≥MIN_REGIME_TRADES for this regime)
          3. Global threshold
        """
        # Strategy fast-block overrides all per-symbol/regime thresholds
        if strategy_tag and self.is_strategy_blocked(strategy_tag):
            return self._current_threshold * self._STRATEGY_THRESHOLD_BOOST

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

    def get_cross_product_wr(
        self, personality: str, session: str, regime: str, min_trades: int = 5
    ) -> float:
        """
        Win rate for the (personality, session, regime) cross-product.
        Returns -1.0 if insufficient sample size.
        """
        stats = self._cross_product.get((personality, session, regime))
        if stats is None:
            return -1.0
        total = stats["wins"] + stats["losses"]
        if total < min_trades:
            return -1.0
        return stats["wins"] / total

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
        """
        Asymmetric threshold mapping.
        Bad  (WR < 0.40): raise threshold up to MAX_ADJ_UP above baseline  → ceiling 9.0
        Good (WR > 0.60): ease threshold down up to MAX_ADJ_DOWN below  → floor 4.0
        Healthy (0.40–0.60): decay slowly back to baseline.
        """
        if win_rate < 0.40:
            adj = MAX_ADJ_UP * (0.40 - win_rate) / 0.40
            return baseline * (1.0 + adj)
        elif win_rate > 0.60:
            adj = MAX_ADJ_DOWN * (win_rate - 0.60) / 0.40
            return baseline * (1.0 - adj)
        else:
            return self._current_threshold * DECAY + baseline * (1.0 - DECAY)

    def _recalibrate(self) -> None:
        """Recomputes all thresholds and weights from settled trades."""
        settled = list(self._records)
        n = len(settled)
        if n < MIN_TRADES:
            return

        wins = sum(1 for r in settled if r.won)
        win_rate = self._bayesian_win_rate(wins, n)

        # ── Global threshold — asymmetric range [4.0, 9.0] ───────────────────
        new_t = self._threshold_from_win_rate(win_rate, BASELINE_THRESHOLD)
        self._current_threshold = round(
            max(THRESHOLD_FLOOR, min(THRESHOLD_CEILING, new_t)), 3
        )

        # ── Per-symbol thresholds ─────────────────────────────────────────────
        symbols = {r.symbol for r in settled}
        for sym in symbols:
            sym_recs = [r for r in settled if r.symbol == sym]
            if len(sym_recs) < MIN_SYMBOL_TRADES:
                continue
            sym_wins = sum(1 for r in sym_recs if r.won)
            sym_wr = self._bayesian_win_rate(sym_wins, len(sym_recs), prior_n=5)
            new_sym_t = self._threshold_from_win_rate(sym_wr, BASELINE_THRESHOLD)
            self._symbol_thresholds[sym] = round(
                max(THRESHOLD_FLOOR, min(THRESHOLD_CEILING, new_sym_t)), 3
            )

        # ── Per-regime thresholds ─────────────────────────────────────────────
        for regime in ("risk_on", "risk_off", "rotational"):
            reg_recs = [r for r in settled if r.regime == regime]
            if len(reg_recs) < MIN_REGIME_TRADES:
                continue
            reg_wins = sum(1 for r in reg_recs if r.won)
            reg_wr = self._bayesian_win_rate(reg_wins, len(reg_recs), prior_n=8)
            reg_base = BASELINE_THRESHOLD * (1.10 if regime == "risk_off" else 1.0)
            new_reg_t = self._threshold_from_win_rate(reg_wr, reg_base)
            _reg_floor = THRESHOLD_FLOOR
            _reg_ceil  = THRESHOLD_CEILING * (1.10 if regime == "risk_off" else 1.0)
            self._regime_thresholds[regime] = round(
                max(_reg_floor, min(_reg_ceil, new_reg_t)), 3
            )

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
