"""
intelligence/staking_monitor.py — SoDEX Staking Position Monitor.

Tracks which SSI tokens the user has staked, accrued yield, and component
weights of staked indexes. This data feeds the SOVEREIGN personality budget.

Default: MAG7_DEFAULT_STAKE_USD = $200 (user confirmed standing stake).
Can be overridden by live SoDEX API data when wired in main.py.

Component weights match MAG7_COMPONENTS in ssi_component_monitor.py.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from intelligence.ssi_component_monitor import MAG7_COMPONENTS

# Default stake balance — user has $200 MAG7 staked by default
MAG7_DEFAULT_STAKE_USD = 200.0

# Approximate annual staking yield for MAG7 SSI on SoDEX (basis points)
# 5% APY → daily accrual = 5% / 365 ≈ 0.0137% per day
MAG7_ANNUAL_YIELD_PCT = 0.05


@dataclass
class StakingPosition:
    """Represents a single staked SSI token position."""
    token:           str    # e.g. "MAG7-SSI"
    balance_usd:     float  # current USD value of staked amount
    component_weights: Dict[str, float]  # {component_symbol: weight}
    entry_ts:        float = field(default_factory=time.time)
    last_yield_ts:   float = field(default_factory=time.time)
    accrued_yield_usd: float = 0.0
    annual_yield_pct:  float = 0.05


class StakingMonitor:
    """
    Monitors staked SSI positions and tracks yield accrual.

    Designed for background update every 60 minutes.
    Falls back to default $200 MAG7 stake if no live API data available.

    Usage:
        monitor = StakingMonitor()
        monitor.initialise()         # called at startup

        # Every hour:
        yield_usd = monitor.accrue_yield()

        # Query:
        balance = monitor.get_stake_balance("MAG7-SSI")   # $200.0
        weights = monitor.get_component_weights()          # {"NVDA-USD": 0.25, ...}
        hedge   = monitor.get_hedge_notional("NVDA-USD")  # $200 × 0.25 = $50
    """

    def __init__(self, default_stake_usd: float = MAG7_DEFAULT_STAKE_USD) -> None:
        self._default_stake_usd = default_stake_usd
        self._positions: Dict[str, StakingPosition] = {}
        self._initialised = False

    def initialise(self) -> None:
        """Load default positions. Called at startup."""
        mag7_pos = StakingPosition(
            token="MAG7-SSI",
            balance_usd=self._default_stake_usd,
            component_weights=dict(MAG7_COMPONENTS),
            annual_yield_pct=MAG7_ANNUAL_YIELD_PCT,
        )
        self._positions["MAG7-SSI"] = mag7_pos
        self._initialised = True

    def update_balance(self, token: str, balance_usd: float) -> None:
        """Update stake balance from live API data."""
        pos = self._positions.get(token)
        if pos is not None:
            pos.balance_usd = max(0.0, balance_usd)

    def accrue_yield(self) -> float:
        """
        Compute and add accrued yield since last call.
        Returns total USD yield accrued this call.
        Returns 0.0 if no positions.
        """
        now = time.time()
        total_yield = 0.0
        for pos in self._positions.values():
            elapsed_s = now - pos.last_yield_ts
            daily_yield_frac = pos.annual_yield_pct / 365.0
            period_yield_frac = daily_yield_frac * (elapsed_s / 86400.0)
            yield_usd = pos.balance_usd * period_yield_frac
            pos.accrued_yield_usd += yield_usd
            pos.last_yield_ts = now
            total_yield += yield_usd
        return total_yield

    def get_stake_balance(self, token: str = "MAG7-SSI") -> float:
        """Total USD balance staked in the given token."""
        pos = self._positions.get(token)
        return pos.balance_usd if pos else 0.0

    def get_total_stake_balance(self) -> float:
        """Sum of all staked positions in USD."""
        return sum(p.balance_usd for p in self._positions.values())

    def get_accrued_yield(self, token: str = "MAG7-SSI") -> float:
        """Accrued (unclaimed) yield for a token."""
        pos = self._positions.get(token)
        return pos.accrued_yield_usd if pos else 0.0

    def get_total_accrued_yield(self) -> float:
        """Total accrued yield across all staked tokens."""
        return sum(p.accrued_yield_usd for p in self._positions.values())

    def consume_yield(self, amount_usd: float) -> float:
        """
        Consume yield (move to SOVEREIGN budget pool).
        Returns actual amount consumed (may be less if insufficient yield).
        """
        total_accrued = self.get_total_accrued_yield()
        consumed = min(amount_usd, total_accrued)
        if consumed <= 0:
            return 0.0
        # Deduct proportionally across positions
        remaining = consumed
        for pos in self._positions.values():
            take = min(remaining, pos.accrued_yield_usd)
            pos.accrued_yield_usd -= take
            remaining -= take
            if remaining <= 0:
                break
        return consumed

    def get_component_weights(self, token: str = "MAG7-SSI") -> Dict[str, float]:
        """Return component weights for a staked token."""
        pos = self._positions.get(token)
        return dict(pos.component_weights) if pos else {}

    def get_hedge_notional(self, component_symbol: str, token: str = "MAG7-SSI") -> float:
        """
        Maximum short notional for a component = stake_balance × component_weight.
        This is the structurally matched hedge size.
        e.g. NVDA weight=0.25, stake=$200 → hedge = $50
        """
        pos = self._positions.get(token)
        if pos is None:
            return 0.0
        weight = pos.component_weights.get(component_symbol, 0.0)
        return pos.balance_usd * weight

    def is_component_in_stake(self, symbol: str) -> bool:
        """True if symbol is a component of any staked index."""
        for pos in self._positions.values():
            if symbol in pos.component_weights:
                return True
        return False