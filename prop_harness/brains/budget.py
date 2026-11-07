"""Budget brain — B(d) = max(floor, headroom/10), never bankrupt.

Quant register (founding review 2026-11-06):
  B(0) = $15/day at full $150 headroom; hard zero at headroom <= $30.
  r_final = min(r_base * mult, budget_remaining, headroom/10, r_max).
  15 straight full-loss days leave $30.9 headroom -> 3-week survival holds.

Budget rolls on the UTC day (venue daily-loss window resets 00:00 UTC);
display timestamps are Berlin's business, not the risk window's.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from ..io.journal import atomic_write_json, read_json


@dataclass
class BudgetState:
    day: str
    headroom: float
    budget: float
    committed: float
    realized: float
    ts: float


class BudgetBrain:
    def __init__(self, register_path: Path, hard_zero: float = 30.0,
                 divisor: float = 10.0, floor_budget: float = 5.0,
                 r_base: float = 5.0, r_max: float = 7.5):
        self.path = register_path
        self.hard_zero = hard_zero
        self.divisor = divisor
        self.floor_budget = floor_budget
        self.r_base = r_base
        self.r_max = r_max
        self.state: BudgetState | None = None
        raw = read_json(self.path)
        if raw:
            self.state = BudgetState(**{k: raw[k] for k in
                                        ("day", "headroom", "budget", "committed", "realized", "ts")})

    @staticmethod
    def utc_day() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def daily_budget(self, headroom: float) -> float:
        if headroom <= self.hard_zero:
            return 0.0
        return max(self.floor_budget, headroom / self.divisor)

    def roll(self, headroom: float) -> bool:
        """Roll to the current UTC day if needed. Returns True on roll."""
        day = self.utc_day()
        if self.state and self.state.day == day:
            self.state.headroom = headroom
            self.state.ts = time.time()
            atomic_write_json(self.path, asdict(self.state))
            return False
        self.state = BudgetState(
            day=day, headroom=headroom, budget=self.daily_budget(headroom),
            committed=0.0, realized=0.0, ts=time.time(),
        )
        atomic_write_json(self.path, asdict(self.state))
        return True

    @property
    def remaining(self) -> float:
        if not self.state:
            return 0.0
        return max(0.0, self.state.budget - self.state.committed)

    def risk_cap(self, mult: float = 1.0) -> float:
        if not self.state or self.state.headroom <= self.hard_zero:
            return 0.0
        if self.remaining <= 0:
            return 0.0
        return min(self.r_base * mult, self.remaining,
                   self.state.headroom / self.divisor, self.r_max)

    def commit(self, risk_usd: float) -> None:
        assert self.state is not None
        self.state.committed += risk_usd
        self.state.ts = time.time()
        atomic_write_json(self.path, asdict(self.state))

    def release(self, risk_usd: float, realized_pnl: float) -> None:
        assert self.state is not None
        self.state.committed = max(0.0, self.state.committed - risk_usd)
        self.state.realized += realized_pnl
        self.state.ts = time.time()
        atomic_write_json(self.path, asdict(self.state))
