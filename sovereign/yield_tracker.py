"""
sovereign/yield_tracker.py — Sovereign yield attribution and cost tracking.

Architecture
────────────
Sovereign's yield comes from three sources:

  1. Index appreciation — price return on held SSI tokens (unrealised + realised)
  2. USSI carry yield  — dynamic APY from funding market conditions
                         Mapped from FundingRadar avg carry score:
                           carry ≥ 2.5  → 80% APY  (extreme bull funding)
                           carry ≥ 1.5  → 35% APY  (elevated carry)
                           carry ≥ 0.5  → 12% APY  (normal carry)
                           carry ≥ 0.0  → 5%  APY  (flat market)
                           carry <  0.0 → 1%  APY  (bear/negative funding floor)
  3. SLP vault yield  — from existing SLPVaultTracker (fed in from slp_tracker)

Costs:
  - Holding cost: 0.01% per day per token (management fee equivalent)

The tracker feeds net yield estimates to the terminal display each cycle.
It does NOT manage the yield budget (that's core/yield_tracker.py).
"""

from __future__ import annotations

import structlog
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from sovereign.portfolio import SovereignPortfolio

log = structlog.get_logger(__name__)

# ── USSI carry APY map ────────────────────────────────────────────────────────
# Carry score is the average funding carry across BTC/ETH/SOL/BNB from FundingRadar
_USSI_CARRY_APY_BRACKETS: list = [
    (2.5,  0.80),   # extreme bull funding → 80% APY
    (1.5,  0.35),   # elevated carry
    (0.5,  0.12),   # normal carry
    (0.0,  0.05),   # flat
    (-99,  0.01),   # bear / negative funding floor
]

# Annual days constant
_DAYS_PER_YEAR: float = 365.0

# Holding cost rate per token per day (0.01%)
_HOLDING_COST_DAILY: float = 0.0001


@dataclass
class YieldComponents:
    """Yield attribution for one Sovereign cycle."""
    cycle_hours:          float   # cycle duration in hours

    # Index appreciation (unrealised — based on price change since entry)
    index_gain_usd:       float   # sum across MAG7 + DEFI + MEME positions

    # USSI carry (accrued over cycle_hours at dynamic APY)
    ussi_notional_usd:    float
    ussi_apy:             float   # current APY derived from carry score
    ussi_carry_usd:       float   # cycle yield from USSI

    # SLP vault yield (passed in from SLPVaultTracker)
    slp_yield_usd:        float

    # Holding costs
    holding_cost_usd:     float   # negative (deducted)

    # Totals
    net_yield_usd:        float   # index_gain + ussi_carry + slp_yield - holding_cost
    annualised_apy:       float   # net_yield / total_portfolio × (365 / cycle_days)

    # Carry environment
    avg_carry_score:      float   # the carry score used for USSI APY


@dataclass
class YieldSummary:
    """Multi-cycle accumulation for display."""
    total_yield_30d_usd:  float
    holding_cost_30d_usd: float
    net_30d_usd:          float
    current_ussi_apy:     float
    avg_carry_score:      float


class SovereignYieldTracker:
    """
    Tracks and attributes yield for Sovereign's SSI portfolio each cycle.
    Stateless between cycles — just computes from current state.
    """

    def __init__(self) -> None:
        self._cycle_yields: list = []   # YieldComponents history (last 120 cycles = 30d)

    # ── Public API ────────────────────────────────────────────────────────────

    def compute_cycle_yield(
        self,
        portfolio: "SovereignPortfolio",
        avg_carry_score: float,
        slp_yield_usd: float = 0.0,
        cycle_hours: float = 6.0,
    ) -> YieldComponents:
        """
        Compute yield attribution for one 6-hour Sovereign cycle.

        Args:
            portfolio:       current SovereignPortfolio (prices must be updated)
            avg_carry_score: average carry score from FundingRadar (BTC/ETH/SOL/BNB)
            slp_yield_usd:   yield from SLP vault this cycle (from SLPVaultTracker)
            cycle_hours:     cycle duration (default 6h)

        Returns YieldComponents; also appended to internal history.
        """
        cycle_days = cycle_hours / 24.0

        # ── Index appreciation ────────────────────────────────────────────────
        # Unrealised gain = sum of (current_usd - entry_usd) for non-USSI positions
        index_gain = 0.0
        for sym, pos in portfolio.positions.items():
            if sym == "USSI-USD":
                continue   # USSI yield tracked separately via carry
            if pos.entry_usd > 0 and pos.current_usd > 0:
                index_gain += pos.current_usd - pos.entry_usd
        index_gain = max(index_gain, 0.0)  # unrealised; never negative here

        # ── USSI carry yield ──────────────────────────────────────────────────
        ussi_pos       = portfolio.positions.get("USSI-USD")
        ussi_notional  = ussi_pos.current_usd if ussi_pos else 0.0
        ussi_apy       = self._carry_to_apy(avg_carry_score)
        ussi_carry_usd = ussi_notional * ussi_apy * cycle_days / _DAYS_PER_YEAR

        # ── Holding cost ──────────────────────────────────────────────────────
        total_usd      = portfolio.total_value_usd()
        holding_cost   = total_usd * _HOLDING_COST_DAILY * cycle_days

        # ── Net yield ─────────────────────────────────────────────────────────
        net_yield = index_gain + ussi_carry_usd + slp_yield_usd - holding_cost

        # Annualised APY based on net yield over cycle
        ann_apy = 0.0
        if total_usd > 0 and cycle_days > 0:
            ann_apy = (net_yield / total_usd) * (_DAYS_PER_YEAR / cycle_days)

        components = YieldComponents(
            cycle_hours=cycle_hours,
            index_gain_usd=round(index_gain, 4),
            ussi_notional_usd=round(ussi_notional, 2),
            ussi_apy=round(ussi_apy, 4),
            ussi_carry_usd=round(ussi_carry_usd, 6),
            slp_yield_usd=round(slp_yield_usd, 6),
            holding_cost_usd=round(holding_cost, 6),
            net_yield_usd=round(net_yield, 4),
            annualised_apy=round(ann_apy, 4),
            avg_carry_score=round(avg_carry_score, 3),
        )

        # Append to history; keep last 120 cycles (30d at 6h)
        self._cycle_yields.append(components)
        if len(self._cycle_yields) > 120:
            self._cycle_yields.pop(0)

        log.info(
            "sovereign_yield_cycle",
            index_gain_usd=components.index_gain_usd,
            ussi_carry_usd=components.ussi_carry_usd,
            ussi_apy_pct=round(ussi_apy * 100, 1),
            slp_yield_usd=components.slp_yield_usd,
            holding_cost_usd=components.holding_cost_usd,
            net_yield_usd=components.net_yield_usd,
            avg_carry_score=avg_carry_score,
        )

        return components

    def get_summary_30d(self) -> YieldSummary:
        """Aggregate yield over up to 120 stored cycles (30-day window)."""
        if not self._cycle_yields:
            return YieldSummary(
                total_yield_30d_usd=0.0, holding_cost_30d_usd=0.0,
                net_30d_usd=0.0, current_ussi_apy=0.0, avg_carry_score=0.0,
            )

        total_yield   = sum(
            c.index_gain_usd + c.ussi_carry_usd + c.slp_yield_usd
            for c in self._cycle_yields
        )
        holding_cost  = sum(c.holding_cost_usd for c in self._cycle_yields)
        latest        = self._cycle_yields[-1]

        return YieldSummary(
            total_yield_30d_usd  = round(total_yield, 4),
            holding_cost_30d_usd = round(holding_cost, 4),
            net_30d_usd          = round(total_yield - holding_cost, 4),
            current_ussi_apy     = round(latest.ussi_apy, 4),
            avg_carry_score      = round(latest.avg_carry_score, 3),
        )

    # ── Utilities ─────────────────────────────────────────────────────────────

    @staticmethod
    def _carry_to_apy(carry_score: float) -> float:
        """Map FundingRadar avg carry score to USSI APY estimate."""
        for threshold, apy in _USSI_CARRY_APY_BRACKETS:
            if carry_score >= threshold:
                return apy
        return 0.01   # fallback floor
