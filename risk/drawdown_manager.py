"""
DrawdownManager — ARIA v1.6

Tracks equity curve and enforces drawdown-based position size reduction and
system halts across three dimensions: daily, weekly, and total (peak-to-trough).

Four levels:
  NORMAL  (0–10% total DD)  → 1.00× size
  REDUCED (10–20% total DD) → 0.75× size
  MINIMAL (20–25% total DD) → 0.50× size
  HALTED  (25%+ total DD)   → 0.00× directional trades; arb only

Kelly fraction under drawdown:
  Effective Kelly = full_Kelly × (1 − dd_pct / max_dd)
  At 25% halt with 1% risk and 52% win-rate: P(ruin before recovery) < 0.1%.

Recovery requires 10% gain above the low watermark — NOT above the current
balance or the peak. This prevents premature resumption after a shallow bounce.

State is persisted to logs/drawdown_state.json across sessions.
"""

import json
import time
import structlog
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

log = structlog.get_logger(__name__)

_STATE_FILE = Path("logs/drawdown_state.json")


@dataclass
class DrawdownStatus:
    halted: bool
    halt_reason: str
    size_multiplier: float
    total_drawdown_pct: float
    daily_pnl: float
    weekly_pnl: float
    peak_balance: float
    current_balance: float
    low_watermark: float
    can_directional: bool
    can_arb: bool


class DrawdownManager:
    """
    Session-level and cross-session drawdown circuit breaker.

    Typical integration:
      1. Call update_balance(balance) on every equity refresh.
      2. Call can_trade_directional() before any directional entry.
      3. Call get_size_multiplier() and apply to candidate.size AFTER all
         other multipliers (coherence, freshness, calendar, funding).
      4. Call reset_daily() at UTC midnight; reset_weekly() on Monday midnight.
    """

    MAX_DAILY_DD    = 0.05    # 5%  → halt today
    MAX_WEEKLY_DD   = 0.15    # 15% → reduce + weekly review
    MAX_TOTAL_DD    = 0.25    # 25% → full halt, arb only
    RECOVERY_THRESHOLD = 0.10 # 10% gain from low watermark to resume

    def __init__(self, starting_balance: float):
        self._peak_balance    = starting_balance
        self._low_watermark   = starting_balance
        self._session_start   = starting_balance
        self._week_start      = starting_balance
        self._current_balance = starting_balance

        self._daily_pnl  = 0.0
        self._weekly_pnl = 0.0
        self._total_pnl  = 0.0

        self._halted       = False
        self._halt_reason  = ""
        self._size_multiplier = 1.0

        self._load_state()

    # ── Public interface ──────────────────────────────────────────────────────

    def update_balance(self, balance: float) -> None:
        """
        Call on every equity refresh. Recalculates all drawdown metrics and
        updates size_multiplier. Ignores zero or negative balances.
        """
        if balance <= 0:
            return

        self._current_balance = balance
        _ath_recovered = False   # set True if ATH clears a halt; skips tier chain below

        # Update peak and low watermark
        if balance > self._peak_balance:
            # New all-time high: auto-recover if currently halted.
            # A fresh ATH definitively proves full recovery — no need for
            # the 10% threshold (which becomes 0% when low_watermark = new_peak).
            if self._halted:
                self._halted          = False
                self._halt_reason     = ""
                self._size_multiplier = 0.50   # resume at MINIMAL, not NORMAL
                _ath_recovered        = True   # skip tier chain — size already set
                log.info(
                    "drawdown_recovery_new_ath",
                    balance=round(balance, 2),
                    prior_peak=round(self._peak_balance, 2),
                    new_multiplier=0.50,
                )
            self._peak_balance  = balance
            self._low_watermark = balance   # reset low when new high is set

        if balance < self._low_watermark:
            self._low_watermark = balance

        # PnL tracking
        self._daily_pnl  = balance - self._session_start
        self._weekly_pnl = balance - self._week_start
        self._total_pnl  = balance - self._peak_balance

        # Drawdown fractions
        total_dd  = self._total_dd_pct()
        daily_dd  = (
            (self._session_start - balance) / self._session_start
            if self._session_start > 0 else 0.0
        )
        weekly_dd = (
            (self._week_start - balance) / self._week_start
            if self._week_start > 0 else 0.0
        )

        prev_mult = self._size_multiplier

        if self._halted:
            # Check recovery: need 10% above low watermark
            recovery_pct = (
                (balance - self._low_watermark) / self._low_watermark
                if self._low_watermark > 0 else 0.0
            )
            if recovery_pct >= self.RECOVERY_THRESHOLD:
                self._halted      = False
                self._halt_reason = ""
                self._size_multiplier = 0.50   # return at MINIMAL, not NORMAL
                log.info(
                    "drawdown_recovery",
                    balance=round(balance, 2),
                    low_watermark=round(self._low_watermark, 2),
                    recovery_pct=round(recovery_pct * 100, 2),
                    new_multiplier=0.50,
                )

        elif _ath_recovered:
            pass   # size already set to 0.50 by ATH recovery — do not override

        elif total_dd >= self.MAX_TOTAL_DD:
            self._halted          = True
            self._size_multiplier = 0.0
            self._halt_reason     = f"total_dd_{total_dd:.1%}"
            log.warning(
                "drawdown_halt",
                reason="total_drawdown_exceeded",
                total_dd=f"{total_dd * 100:.1f}%",
                max_dd=f"{self.MAX_TOTAL_DD * 100:.0f}%",
                balance=round(balance, 2),
                peak=round(self._peak_balance, 2),
            )

        elif daily_dd >= self.MAX_DAILY_DD:
            self._halted          = True
            self._size_multiplier = 0.0
            self._halt_reason     = f"daily_dd_{daily_dd:.1%}"
            log.warning(
                "drawdown_halt",
                reason="daily_loss_limit_exceeded",
                daily_dd=f"{daily_dd * 100:.1f}%",
                max=f"{self.MAX_DAILY_DD * 100:.0f}%",
            )

        elif weekly_dd >= self.MAX_WEEKLY_DD:
            self._size_multiplier = 0.50
            if prev_mult != 0.50:
                log.warning(
                    "drawdown_size_reduced",
                    reason="weekly_drawdown",
                    weekly_dd=f"{weekly_dd * 100:.1f}%",
                    multiplier=0.50,
                )

        elif total_dd >= 0.20:
            self._size_multiplier = 0.50
            if prev_mult > 0.50:
                log.info("drawdown_size_reduced",
                         reason="total_20pct", total_dd=f"{total_dd*100:.1f}%",
                         multiplier=0.50)

        elif total_dd >= 0.10:
            self._size_multiplier = 0.75
            if prev_mult > 0.75:
                log.info("drawdown_size_reduced",
                         reason="total_10pct", total_dd=f"{total_dd*100:.1f}%",
                         multiplier=0.75)

        else:
            if prev_mult < 1.0 and not self._halted:
                log.info("drawdown_size_restored", multiplier=1.0,
                         total_dd=f"{total_dd*100:.1f}%")
            self._size_multiplier = 1.0

        if self._size_multiplier != prev_mult:
            log.info(
                "size_multiplier_changed",
                prev=prev_mult,
                new=self._size_multiplier,
                total_dd=round(total_dd * 100, 2),
                daily_dd=round(daily_dd * 100, 2),
            )

        self._save_state()

    def can_trade_directional(self) -> bool:
        """False when halted — NO directional trades regardless of coherence."""
        return not self._halted

    def can_trade_arb(self) -> bool:
        """
        Arb always allowed even when halted.
        Delta-neutral = zero directional exposure = no drawdown contribution.
        """
        return True

    def get_size_multiplier(self) -> float:
        """
        Returns 1.0 / 0.75 / 0.50 / 0.0.
        Apply LAST in the multiplier chain:
          coherence × freshness × calendar × funding × drawdown = final_size
        This multiplier can only reduce size — never increase.
        """
        return self._size_multiplier

    def reset_daily(self) -> None:
        """Called at UTC midnight. Resets daily P&L tracking."""
        self._session_start = self._current_balance
        self._daily_pnl     = 0.0
        # Clear daily halt if it was daily-only (not total)
        if self._halted and "daily" in self._halt_reason:
            self._halted      = False
            self._halt_reason = ""
            self._size_multiplier = 1.0
        log.info("daily_reset", balance=round(self._current_balance, 2))
        self._save_state()

    def reset_weekly(self) -> None:
        """Called Monday UTC midnight. Resets weekly P&L tracking."""
        self._week_start = self._current_balance
        self._weekly_pnl = 0.0
        log.info("weekly_reset", balance=round(self._current_balance, 2))
        self._save_state()

    def status(self) -> DrawdownStatus:
        """Returns a complete snapshot. Used by terminal and balance_monitor_loop."""
        return DrawdownStatus(
            halted=self._halted,
            halt_reason=self._halt_reason,
            size_multiplier=self._size_multiplier,
            total_drawdown_pct=round(self._total_dd_pct() * 100, 2),
            daily_pnl=round(self._daily_pnl, 2),
            weekly_pnl=round(self._weekly_pnl, 2),
            peak_balance=round(self._peak_balance, 2),
            current_balance=round(self._current_balance, 2),
            low_watermark=round(self._low_watermark, 2),
            can_directional=self.can_trade_directional(),
            can_arb=self.can_trade_arb(),
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _total_dd_pct(self) -> float:
        if self._peak_balance <= 0:
            return 0.0
        return max(0.0, (self._peak_balance - self._current_balance) / self._peak_balance)

    def _load_state(self) -> None:
        """Restore persisted peak/low from previous session."""
        try:
            if _STATE_FILE.exists():
                data = json.loads(_STATE_FILE.read_text())
                saved_peak = float(data.get("peak", self._peak_balance))
                saved_low  = float(data.get("low",  self._low_watermark))
                saved_week = float(data.get("week_start", self._week_start))
                # Only restore peak if it's higher than starting_balance
                # (prevents stale high-water marks from a different session)
                if saved_peak >= self._peak_balance:
                    self._peak_balance  = saved_peak
                    self._low_watermark = min(saved_low, self._peak_balance)
                if saved_week > 0:
                    self._week_start = saved_week
        except Exception as e:
            log.debug("drawdown_state_load_skipped", error=str(e))

    def _save_state(self) -> None:
        """Persist current state so recovery logic survives restarts."""
        try:
            _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _STATE_FILE.write_text(json.dumps({
                "peak":           self._peak_balance,
                "low":            self._low_watermark,
                "week_start":     self._week_start,
                "current":        self._current_balance,
                "halted":         self._halted,
                "halt_reason":    self._halt_reason,
                "size_multiplier": self._size_multiplier,
                "saved_at":       time.time(),
            }, indent=2))
        except Exception as e:
            log.debug("drawdown_state_save_failed", error=str(e))
