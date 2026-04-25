"""
DrawdownGuard — ARIA v1.4

Drawdown-aware position sizing.  Scales size DOWN during losing streaks and
restores it after consecutive wins.  Prevents a bad patch from compounding
losses before the bot adapts.

Algorithm:
  • Peak-to-trough drawdown ratio drives a size multiplier in [MIN_MULT, 1.0].
  • After RECOVERY_WINS consecutive profitable closes the multiplier resets to 1.0.
  • State persists across execution_cleanup_loop iterations (in-memory only —
    resets on bot restart, which is acceptable for session-level protection).

Usage:
    guard = DrawdownGuard(config)
    guard.update_balance(current_balance)    # call every equity update
    guard.record_close(pnl_usd)             # call on every trade close
    mult = guard.size_multiplier()           # 0.25 .. 1.0; apply to candidate.size
"""

import time
import structlog
from dataclasses import dataclass
from typing import List

log = structlog.get_logger(__name__)

# ── Tunable constants ─────────────────────────────────────────────────────────
_MIN_MULT = 0.60          # Never size below 60% of normal — preserves min notional
_STEP_PER_WIN = 0.10      # Restore 10% of normal per consecutive win
_DRAWDOWN_TIERS = [       # (drawdown_threshold, size_multiplier)
    (0.00, 1.00),         # 0–5%  drawdown → full size
    (0.05, 0.80),         # 5–10% drawdown → 80%
    (0.10, 0.60),         # 10–15% drawdown → 60%
    (0.15, 0.60),         # 15–20% drawdown → 60% (clamped to min)
    (0.20, 0.60),         # 20%+  drawdown → 60% (survival mode, preserves notional)
]
_RECOVERY_WINS = 3        # Consecutive profitable closes to fully restore size


@dataclass
class DrawdownState:
    peak_balance: float
    current_balance: float
    drawdown_pct: float
    size_multiplier: float
    consecutive_wins: int
    consecutive_losses: int
    session_pnl: float


class DrawdownGuard:
    """
    Session-level drawdown circuit with linear recovery.

    Typical integration:
      1. Call update_balance() every time equity refreshes (~1s cadence).
      2. Call record_close(pnl) on each trade_closed event.
      3. Read size_multiplier() inside build_candidate() or on_signal_ready()
         and apply: candidate.size *= guard.size_multiplier()
    """

    def __init__(self, config=None):
        self._config = config
        self._peak: float = 0.0
        self._current: float = 0.0
        self._consecutive_wins: int = 0
        self._consecutive_losses: int = 0
        self._session_pnl: float = 0.0
        self._history: List[dict] = []    # lightweight trade log
        self._mult: float = 1.0           # cached multiplier (recomputed on balance/close)

    # ── Public interface ──────────────────────────────────────────────────────

    def update_balance(self, balance: float) -> None:
        """
        Call on every equity refresh.  Updates peak-equity and recomputes multiplier.
        Ignores zero or clearly-invalid balances.
        """
        if balance <= 0:
            return
        self._current = balance
        if balance > self._peak:
            self._peak = balance
        self._recompute()

    def record_close(self, pnl_usd: float) -> None:
        """
        Call when a trade closes.  Tracks win/loss streaks and adjusts multiplier.
        """
        self._session_pnl += pnl_usd
        self._history.append({"pnl": pnl_usd, "ts": time.time()})

        if pnl_usd > 0:
            self._consecutive_losses = 0
            self._consecutive_wins += 1

            # After RECOVERY_WINS consecutive wins, restore to normal
            if self._consecutive_wins >= _RECOVERY_WINS:
                if self._mult < 1.0:
                    log.info(
                        "drawdown_recovery",
                        consecutive_wins=self._consecutive_wins,
                        prev_mult=round(self._mult, 2),
                        new_mult=1.0,
                    )
                self._mult = 1.0
                self._consecutive_wins = 0
            else:
                # Partial restore per win (floor at current tier minimum)
                _tier_min = self._tier_multiplier()
                _restored = min(1.0, self._mult + _STEP_PER_WIN)
                self._mult = max(_tier_min, _restored)
        else:
            self._consecutive_wins = 0
            self._consecutive_losses += 1
            self._recompute()

        log.debug(
            "drawdown_close_recorded",
            pnl=round(pnl_usd, 2),
            session_pnl=round(self._session_pnl, 2),
            mult=round(self._mult, 2),
            consecutive_wins=self._consecutive_wins,
            consecutive_losses=self._consecutive_losses,
        )

    def size_multiplier(self) -> float:
        """
        Returns the current size multiplier in [MIN_MULT, 1.0].
        Multiply candidate.size by this value before risk validation.
        """
        return max(_MIN_MULT, min(1.0, self._mult))

    def get_state(self) -> DrawdownState:
        """Returns a snapshot of the current drawdown state for display/logging."""
        return DrawdownState(
            peak_balance=round(self._peak, 2),
            current_balance=round(self._current, 2),
            drawdown_pct=round(self._drawdown_pct(), 4),
            size_multiplier=round(self.size_multiplier(), 4),
            consecutive_wins=self._consecutive_wins,
            consecutive_losses=self._consecutive_losses,
            session_pnl=round(self._session_pnl, 2),
        )

    def is_survival_mode(self) -> bool:
        """True when drawdown ≥ 15% and size is at minimum (0.60)."""
        return self._drawdown_pct() >= 0.15

    def sync_peak(self, peak: float) -> None:
        """Ensure our peak is at least `peak` — used to align with DrawdownManager."""
        if peak > self._peak:
            self._peak = peak
            self._recompute()

    # ── Internal ─────────────────────────────────────────────────────────────

    def _drawdown_pct(self) -> float:
        """Peak-to-trough drawdown as a fraction."""
        if self._peak <= 0:
            return 0.0
        return max(0.0, (self._peak - self._current) / self._peak)

    def _tier_multiplier(self) -> float:
        """Lookup the multiplier tier for current drawdown."""
        dd = self._drawdown_pct()
        mult = _MIN_MULT
        for threshold, m in _DRAWDOWN_TIERS:
            if dd >= threshold:
                mult = m
            else:
                break
        return max(_MIN_MULT, mult)

    def _recompute(self) -> None:
        """Recompute multiplier from current drawdown.  Never raises mult above tier."""
        tier = self._tier_multiplier()
        # Only lower the multiplier — raising happens via record_close wins
        if tier < self._mult:
            prev = self._mult
            self._mult = tier
            dd = self._drawdown_pct()
            log.warning(
                "drawdown_size_reduced",
                drawdown_pct=f"{dd*100:.1f}%",
                prev_mult=round(prev, 2),
                new_mult=round(self._mult, 2),
                peak=round(self._peak, 2),
                current=round(self._current, 2),
            )
