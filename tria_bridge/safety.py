"""
tria_bridge/safety.py — Safety gates: limits, kill switch, confirmation.

Quant principle: survival first. A missed trade is cheap; a runaway bot is existential.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Optional

from tria_bridge.config import BridgeConfig
from tria_bridge.logger import BridgeLogger
from tria_bridge.state_machine import TradeSignal


@dataclass
class DailyBudget:
    """Thread-safe (single-threaded bridge) daily counters."""
    trades: int = 0
    notional: float = 0.0
    last_trade_ts: float = 0.0
    day: int = field(default_factory=lambda: int(time.time() // 86400))

    def _rotate(self) -> None:
        today = int(time.time() // 86400)
        if today != self.day:
            self.day = today
            self.trades = 0
            self.notional = 0.0

    def can_trade(self, signal: TradeSignal, max_trades: int, max_notional: float, cooldown_s: float) -> Optional[str]:
        self._rotate()
        if self.trades >= max_trades:
            return f"daily_trade_limit_reached_{self.trades}/{max_trades}"
        notional = signal.notional_usd or (signal.size * 1000.0)  # fallback approx
        if self.notional + notional > max_notional:
            return f"daily_notional_limit_reached_{self.notional}/{max_notional}"
        if time.time() - self.last_trade_ts < cooldown_s:
            return f"cooldown_active_{round(time.time() - self.last_trade_ts, 1)}s_remaining"
        return None

    def record_trade(self, signal: TradeSignal) -> None:
        self._rotate()
        self.trades += 1
        self.notional += signal.notional_usd or (signal.size * 1000.0)
        self.last_trade_ts = time.time()


class SafetyEngine:
    """Composite safety: budget, kill switch, confirmation gate."""

    def __init__(self, config: BridgeConfig, logger: BridgeLogger):
        self.cfg = config
        self.log = logger
        self.budget = DailyBudget()
        self._halted = False
        self._halt_reason: Optional[str] = None

    def check_kill_switch(self) -> bool:
        """Returns True if bridge must halt immediately."""
        if self._halted:
            return True
        if os.path.exists(self.cfg.kill_switch_file):
            with open(self.cfg.kill_switch_file, "r", encoding="utf-8") as f:
                content = f.read().strip().upper()
            if content == "STOP":
                self._halted = True
                self._halt_reason = "kill_switch_file"
                self.log.error("kill_switch_activated", file=self.cfg.kill_switch_file)
                return True
        return False

    def halt(self, reason: str) -> None:
        self._halted = True
        self._halt_reason = reason
        self.log.error("safety_halt", reason=reason)
        try:
            with open(self.cfg.kill_switch_file, "w", encoding="utf-8") as f:
                f.write("STOP")
        except Exception:
            pass

    def preflight_check(self, signal: TradeSignal) -> Optional[str]:
        """Return error string if trade must not proceed. None = safe to proceed."""
        if self._halted:
            return f"bridge_halted:{self._halt_reason}"
        if self.check_kill_switch():
            return "kill_switch_active"
        return self.budget.can_trade(
            signal,
            max_trades=self.cfg.max_trades_day,
            max_notional=self.cfg.max_notional_day,
            cooldown_s=self.cfg.cooldown_s,
        )

    def confirm_with_user(self, signal: TradeSignal) -> bool:
        """If confirmation_required, prompt user. Returns True to proceed."""
        if not self.cfg.confirmation_required:
            return True
        prompt = (
            f"\n[TRIA BRIDGE] Execute {signal.direction} {signal.symbol} "
            f"size={signal.size} leverage={signal.leverage or 'default'}?\n"
            f"Type 'yes' to proceed, anything else to abort: "
        )
        try:
            response = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            response = "no"
        approved = response in ("yes", "y")
        self.log.info("user_confirmation", approved=approved, signal=signal.__dict__)
        return approved

    def record_executed(self, signal: TradeSignal) -> None:
        self.budget.record_trade(signal)

    def daily_stats(self) -> dict:
        return {
            "trades_today": self.budget.trades,
            "notional_today": round(self.budget.notional, 2),
            "halted": self._halted,
            "halt_reason": self._halt_reason,
        }
