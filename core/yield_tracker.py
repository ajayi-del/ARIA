"""
core/yield_tracker.py — SOVEREIGN Yield-Funded Budget Tracker.

Architecture:
  Staking yield (passive income) funds the SOVEREIGN budget separately
  from main trading capital. SOVEREIGN never touches the main capital.

  sovereign_budget  = yield_accrued × 0.80
  sovereign_reserve = yield_accrued × 0.20

  Win cycle:   profits accumulate in sovereign_budget
  Loss cycle:  losses deducted from sovereign_budget only
  Overflow:    when sovereign_budget >= 2× initial_yield:
               50% transferred to main capital, 50% stays as reserve

  Depletion:   when sovereign_budget <= 0 → SOVEREIGN enters COIL
               (waits for next yield accrual cycle)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

_YIELD_TO_BUDGET_RATIO = 0.80    # 80% of yield goes to SOVEREIGN budget
_BUDGET_FLOOR = 0.0              # When at 0 → COIL mode
_OVERFLOW_TRIGGER = 2.0          # budget ≥ 2× seed → trigger overflow
_OVERFLOW_TRANSFER_PCT = 0.50    # transfer 50% to main on overflow


@dataclass
class YieldSnapshot:
    """Point-in-time snapshot of SOVEREIGN budget state."""
    sovereign_budget:   float
    sovereign_reserve:  float
    total_yield_earned: float
    total_pnl:          float
    transferred_to_main: float
    is_active:          bool   # True = budget > 0, can trade


class YieldTracker:
    """
    Manages the SOVEREIGN personality's yield-funded budget pool.

    Call flow:
      startup:         initialise(initial_yield_usd)
      every 60 min:    add_yield(new_yield_usd)
      on SOVEREIGN win/loss:  record_pnl(pnl_usd)
      periodically:    check_overflow() → optional transfer to main
      hot path query:  can_trade() / available_budget

    Thread safety: asyncio.Lock on mutating methods.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sovereign_budget:   float = 0.0
        self._sovereign_reserve:  float = 0.0
        self._total_yield_earned: float = 0.0
        self._total_pnl:          float = 0.0
        self._transferred_to_main: float = 0.0
        self._seed_yield:         float = 0.0   # initial yield that seeded this cycle

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def initialise(self, initial_yield_usd: float = 0.0) -> None:
        """
        Seed the SOVEREIGN budget from initial accrued yield.
        Safe to call at startup with 0.0 (bot will wait for yield to accrue).
        """
        budget = initial_yield_usd * _YIELD_TO_BUDGET_RATIO
        reserve = initial_yield_usd * (1.0 - _YIELD_TO_BUDGET_RATIO)
        self._sovereign_budget  = budget
        self._sovereign_reserve = reserve
        self._seed_yield        = initial_yield_usd
        self._total_yield_earned += initial_yield_usd
        log.info("yield_tracker_initialised",
                 extra={"initial_yield": initial_yield_usd, "budget": budget, "reserve": reserve})

    # ── Mutating methods (async, lock-protected) ──────────────────────────────

    async def add_yield(self, yield_usd: float) -> float:
        """
        Add newly accrued staking yield to the SOVEREIGN pool.
        Returns the amount added to the budget.
        """
        async with self._lock:
            if yield_usd <= 0:
                return 0.0
            budget_add  = yield_usd * _YIELD_TO_BUDGET_RATIO
            reserve_add = yield_usd * (1.0 - _YIELD_TO_BUDGET_RATIO)
            self._sovereign_budget   += budget_add
            self._sovereign_reserve  += reserve_add
            self._total_yield_earned += yield_usd
            self._seed_yield         += yield_usd
            log.info("yield_added",
                     extra={"yield_usd": round(yield_usd, 4),
                             "budget_add": round(budget_add, 4),
                             "budget_total": round(self._sovereign_budget, 4)})
            return budget_add

    async def record_pnl(self, pnl_usd: float) -> None:
        """
        Record SOVEREIGN trade outcome. Deducted from / added to sovereign_budget.
        Never touches main trading capital.
        """
        async with self._lock:
            self._sovereign_budget = max(0.0, self._sovereign_budget + pnl_usd)
            self._total_pnl       += pnl_usd
            log.info("sovereign_pnl_recorded",
                     extra={"pnl": round(pnl_usd, 4),
                             "budget_after": round(self._sovereign_budget, 4)})

    async def check_overflow(self) -> Optional[float]:
        """
        If budget ≥ 2× seed_yield, transfer 50% to main capital.
        Returns the transfer amount, or None if no overflow.
        """
        async with self._lock:
            if self._seed_yield <= 0:
                return None
            trigger = self._seed_yield * _OVERFLOW_TRIGGER
            if self._sovereign_budget < trigger:
                return None
            transfer = self._sovereign_budget * _OVERFLOW_TRANSFER_PCT
            self._sovereign_budget      -= transfer
            self._transferred_to_main   += transfer
            # Reset seed for next cycle
            self._seed_yield = self._sovereign_budget
            log.info("sovereign_overflow_transfer",
                     extra={"transfer": round(transfer, 4),
                             "budget_after": round(self._sovereign_budget, 4)})
            return transfer

    # ── Read-only queries (synchronous, no lock needed for hot path) ──────────

    @property
    def available_budget(self) -> float:
        """Current SOVEREIGN trading budget in USD."""
        return self._sovereign_budget

    def can_trade(self) -> bool:
        """True if budget > 0 and SOVEREIGN can take new positions."""
        return self._sovereign_budget > 0.0

    def get_snapshot(self) -> YieldSnapshot:
        """Return a point-in-time snapshot for display/diagnostics."""
        return YieldSnapshot(
            sovereign_budget    = round(self._sovereign_budget, 4),
            sovereign_reserve   = round(self._sovereign_reserve, 4),
            total_yield_earned  = round(self._total_yield_earned, 4),
            total_pnl           = round(self._total_pnl, 4),
            transferred_to_main = round(self._transferred_to_main, 4),
            is_active           = self.can_trade(),
        )
