"""
core/budget_manager.py — ARIA BudgetManager with two-level Kelly sizing.

Architecture:
  Level 1 — Agent pool allocation: AGENT_RATIOS × total balance
  Level 2 — Per-(agent, personality) slot: equal split within each pool,
             dynamically adjusted via record_pnl history and rebalance().

Kelly Formula (half-Kelly, clamped):
  raw_kelly = W - (1 - W) / b
  half_kelly = raw_kelly / 2
  clamped    = max(0.01, min(0.15, half_kelly))

  where W = win_rate, b = avg_win_r (average R-multiple on winners)

AGENT_RATIOS must sum to 1.0.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Deque, Dict, Optional, Tuple

log = logging.getLogger(__name__)

# ── Agent Ratios ──────────────────────────────────────────────────────────────

AGENT_RATIOS: Dict[str, float] = {
    "perp":   0.60,
    "gold":   0.25,
    "equity": 0.15,
}

# Sanity check at import time
_ratio_sum = sum(AGENT_RATIOS.values())
assert abs(_ratio_sum - 1.0) < 1e-9, (
    f"AGENT_RATIOS must sum to 1.0, got {_ratio_sum}"
)

# ── Constants ─────────────────────────────────────────────────────────────────

_PERSONALITIES = ("SHIELD", "AFTERMATH", "APEX", "COIL", "FLOW", "SCOUT")
_NUM_PERSONALITIES = len(_PERSONALITIES)
_HISTORY_LEN = 30          # rolling window of R-multiples per slot
_BUDGET_FLOOR = 5.0        # minimum USD per personality slot
_MAX_POSITION_PCT = 0.15   # max 15% of total balance per trade
_REBALANCE_MAX_STEP = 0.10 # max 10% reduction per rebalance call
_JOINT_THRESHOLD = 0.70    # minimum joint probability for can_bet
_MIN_KELLY = 0.01
_MAX_KELLY = 0.15
_FALLBACK_WIN_RATE = 0.50  # assumed win rate before history accumulates
_FALLBACK_AVG_WIN_R = 1.5  # assumed avg R-multiple before history accumulates
_MIN_HISTORY_FOR_KELLY = 5 # need at least this many trades to use live Kelly


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class SlotState:
    """Tracks budget and trade history for a single (agent, personality) slot."""
    budget_usd: float
    history: Deque[float] = field(default_factory=lambda: deque(maxlen=_HISTORY_LEN))

    @property
    def win_rate(self) -> float:
        if len(self.history) < _MIN_HISTORY_FOR_KELLY:
            return _FALLBACK_WIN_RATE
        return sum(1 for r in self.history if r > 0) / len(self.history)

    @property
    def avg_win_r(self) -> float:
        winners = [r for r in self.history if r > 0]
        if not winners:
            return _FALLBACK_AVG_WIN_R
        return sum(winners) / len(winners)


# ── BudgetManager ─────────────────────────────────────────────────────────────

class BudgetManager:
    """
    Two-level Kelly budget allocator for ARIA institutional trading bot.

    Initialise once at startup with ``initialise()``.  All mutation of
    per-slot state goes through ``record_pnl`` and ``rebalance``, both
    protected by an asyncio.Lock for safe concurrent access.

    Parameters
    ----------
    config : Settings
        ARIA settings object (passed through; not used internally but kept
        for consistency with the rest of the codebase).
    balance : float
        Total account equity in USD at construction time.
    """

    def __init__(self, config, balance: float) -> None:
        self._config = config
        self._total_balance = balance
        self._lock = asyncio.Lock()
        # {agent: {personality: SlotState}}
        self._slots: Dict[str, Dict[str, SlotState]] = {}
        self._initialised = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def initialise(self) -> None:
        """
        Partition balance into agent pools, then split each pool equally
        across the 6 personalities.  Safe to call multiple times (idempotent
        in terms of not raising, but will reset state if called again).
        """
        self._slots.clear()
        for agent, ratio in AGENT_RATIOS.items():
            agent_pool = self._total_balance * ratio
            per_slot = agent_pool / _NUM_PERSONALITIES
            self._slots[agent] = {
                p: SlotState(budget_usd=per_slot)
                for p in _PERSONALITIES
            }
        self._initialised = True
        log.info(
            "budget_manager_initialised",
            extra={
                "total_balance": self._total_balance,
                "agents": {
                    a: {p: round(s.budget_usd, 4) for p, s in slots.items()}
                    for a, slots in self._slots.items()
                },
            },
        )

    # ── Kelly Formula ─────────────────────────────────────────────────────────

    @staticmethod
    def kelly_fraction(win_rate: float, avg_win_r: float) -> float:
        """
        Half-Kelly fraction, clamped to [0.01, 0.15].

        Parameters
        ----------
        win_rate : float
            Historical win rate in (0, 1).
        avg_win_r : float
            Average R-multiple on winning trades (must be > 0).

        Returns
        -------
        float
            Position size as fraction of budgeted capital.
        """
        if avg_win_r <= 0.0:
            return _MIN_KELLY
        raw = win_rate - (1.0 - win_rate) / avg_win_r
        half = raw / 2.0
        return max(_MIN_KELLY, min(_MAX_KELLY, half))

    # ── Budget Accessors ──────────────────────────────────────────────────────

    def get_budget(self, agent: str, personality: str) -> float:
        """
        Return current USD budget for the (agent, personality) slot.

        Parameters
        ----------
        agent : str
            One of the keys in AGENT_RATIOS (e.g. "perp").
        personality : str
            One of the 6 personality names (e.g. "FLOW").

        Returns
        -------
        float
            USD budget allocated to this slot.
        """
        self._check_initialised()
        slot = self._get_slot(agent, personality)
        return slot.budget_usd

    def get_trade_size(
        self,
        agent: str,
        personality: str,
        ml_prob: float,
        balance: float,
    ) -> float:
        """
        Compute a Kelly-sized trade amount in USD for this slot.

        Sizing logic:
          1. Compute Kelly fraction from slot's trade history.
          2. Apply fraction to slot budget.
          3. Clamp to MAX_POSITION_PCT of total balance.
          4. Never exceed the slot's current budget.
          5. Never go below zero.

        Parameters
        ----------
        agent : str
            Agent pool identifier.
        personality : str
            Active personality name.
        ml_prob : float
            ML model probability (0–1); reserved for future use — currently
            the Kelly fraction is derived from historical R-multiples, but
            ml_prob can modulate it in future extensions.
        balance : float
            Current total account balance (may differ from construction-time
            balance if PnL has accumulated).

        Returns
        -------
        float
            USD trade size.
        """
        self._check_initialised()
        slot = self._get_slot(agent, personality)

        kelly = self.kelly_fraction(slot.win_rate, slot.avg_win_r)
        raw_size = slot.budget_usd * kelly

        # Hard cap: never risk more than MAX_POSITION_PCT of total balance
        max_size = balance * _MAX_POSITION_PCT
        size = min(raw_size, max_size, slot.budget_usd)
        size = max(0.0, size)

        log.debug(
            "get_trade_size",
            extra={
                "agent": agent,
                "personality": personality,
                "kelly": round(kelly, 4),
                "raw_size": round(raw_size, 4),
                "max_size": round(max_size, 4),
                "final_size": round(size, 4),
            },
        )
        return size

    # ── Joint Betting ─────────────────────────────────────────────────────────

    def can_bet(
        self,
        agent1: str,
        agent2: str,
        pers1: str,
        pers2: str,
    ) -> Tuple[bool, float]:
        """
        Determine whether two slots can place a joint bet.

        Joint probability is computed as the product of the two slot win rates.
        If joint_prob > JOINT_THRESHOLD (0.70) and both slots have budget,
        returns (True, combined_usd) where combined_usd is capped at 15% of
        total balance.

        Returns
        -------
        tuple[bool, float]
            (allowed, combined_usd_amount)
        """
        self._check_initialised()
        slot1 = self._get_slot(agent1, pers1)
        slot2 = self._get_slot(agent2, pers2)

        p1 = slot1.win_rate
        p2 = slot2.win_rate
        p_joint = p1 * p2

        if p_joint <= _JOINT_THRESHOLD:
            log.debug(
                "can_bet_rejected",
                extra={
                    "agent1": agent1, "pers1": pers1, "p1": round(p1, 4),
                    "agent2": agent2, "pers2": pers2, "p2": round(p2, 4),
                    "p_joint": round(p_joint, 4),
                    "threshold": _JOINT_THRESHOLD,
                },
            )
            return False, 0.0

        # Size each leg using Kelly, combine, cap at global max
        k1 = self.kelly_fraction(slot1.win_rate, slot1.avg_win_r)
        k2 = self.kelly_fraction(slot2.win_rate, slot2.avg_win_r)
        size1 = min(slot1.budget_usd * k1, slot1.budget_usd)
        size2 = min(slot2.budget_usd * k2, slot2.budget_usd)
        combined = size1 + size2

        max_combined = self._total_balance * _MAX_POSITION_PCT
        combined = min(combined, max_combined)

        log.info(
            "can_bet_approved",
            extra={
                "agent1": agent1, "pers1": pers1,
                "agent2": agent2, "pers2": pers2,
                "p_joint": round(p_joint, 4),
                "combined_usd": round(combined, 4),
            },
        )
        return True, combined

    # ── PnL Recording ─────────────────────────────────────────────────────────

    async def record_pnl(
        self,
        agent: str,
        personality: str,
        pnl_usd: float,
        r_multiple: float,
    ) -> None:
        """
        Record trade outcome for a slot.  Updates rolling history used by
        kelly_fraction and adjusts the slot's budget by pnl_usd.

        Budget is floored at _BUDGET_FLOOR after adjustment.

        Parameters
        ----------
        agent : str
            Agent pool identifier.
        personality : str
            Personality name.
        pnl_usd : float
            Realised PnL in USD (positive = win, negative = loss).
        r_multiple : float
            R-multiple of the trade (e.g. +2.0 = 2R win, -1.0 = 1R loss).
        """
        async with self._lock:
            self._check_initialised()
            slot = self._get_slot(agent, personality)
            slot.history.append(r_multiple)
            slot.budget_usd = max(_BUDGET_FLOOR, slot.budget_usd + pnl_usd)

            log.info(
                "budget_pnl_recorded",
                extra={
                    "agent": agent,
                    "personality": personality,
                    "pnl_usd": round(pnl_usd, 4),
                    "r_multiple": round(r_multiple, 4),
                    "new_budget": round(slot.budget_usd, 4),
                    "win_rate": round(slot.win_rate, 4),
                    "avg_win_r": round(slot.avg_win_r, 4),
                    "history_len": len(slot.history),
                },
            )

    # ── Rebalance ─────────────────────────────────────────────────────────────

    async def rebalance(self, calibration: dict) -> None:
        """
        Reduce personality budgets based on calibration error.

        For each personality in ``calibration``, applies the CalibrationResult's
        ``budget_multiplier``.  Reduction is capped at MAX_REBALANCE_STEP (10%)
        per call so no single rebalance can slash budgets aggressively.
        Budget floor (_BUDGET_FLOOR) is enforced after adjustment.

        Parameters
        ----------
        calibration : dict
            Maps personality_name (str) → CalibrationResult.
            CalibrationResult must have a ``budget_multiplier`` float attribute
            (e.g. 0.90 = reduce by 10%, 1.0 = no change).
            Imported lazily to avoid circular imports.
        """
        # Lazy import to avoid circular dependency
        try:
            from intelligence.prediction_market import CalibrationResult  # noqa: F401
        except ImportError:
            CalibrationResult = None  # type: ignore[assignment,misc]

        async with self._lock:
            self._check_initialised()
            for personality, cal_result in calibration.items():
                multiplier: float = getattr(cal_result, "budget_multiplier", 1.0)

                # Clamp multiplier: allow only up to MAX_STEP reduction
                # (multiplier < 1 = reduction; floor at 1 - MAX_STEP)
                min_allowed_mult = 1.0 - _REBALANCE_MAX_STEP  # 0.90
                effective_mult = max(min_allowed_mult, min(1.0, multiplier))

                for agent, slots in self._slots.items():
                    if personality not in slots:
                        continue
                    slot = slots[personality]
                    new_budget = slot.budget_usd * effective_mult
                    new_budget = max(_BUDGET_FLOOR, new_budget)

                    log.info(
                        "budget_rebalanced",
                        extra={
                            "agent": agent,
                            "personality": personality,
                            "old_budget": round(slot.budget_usd, 4),
                            "multiplier_requested": round(multiplier, 4),
                            "multiplier_applied": round(effective_mult, 4),
                            "new_budget": round(new_budget, 4),
                        },
                    )
                    slot.budget_usd = new_budget

    # ── Internal Helpers ──────────────────────────────────────────────────────

    def _check_initialised(self) -> None:
        if not self._initialised:
            raise RuntimeError(
                "BudgetManager.initialise() must be called before use."
            )

    def _get_slot(self, agent: str, personality: str) -> SlotState:
        """Retrieve slot, raising KeyError with a clear message if missing."""
        if agent not in self._slots:
            raise KeyError(
                f"Unknown agent '{agent}'. Known agents: {list(self._slots)}"
            )
        slots = self._slots[agent]
        # Normalize: accept lowercase or uppercase personality names
        key = personality.upper()
        if key not in slots:
            raise KeyError(
                f"Unknown personality '{personality}' for agent '{agent}'. "
                f"Known: {list(slots)}"
            )
        return slots[key]

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Dict[str, Dict]]:
        """
        Return a nested dict summarising current budget state.
        Useful for health endpoints and dashboard display.
        """
        self._check_initialised()
        out: Dict[str, Dict[str, Dict]] = {}
        for agent, slots in self._slots.items():
            out[agent] = {}
            for personality, slot in slots.items():
                out[agent][personality] = {
                    "budget_usd": round(slot.budget_usd, 4),
                    "win_rate": round(slot.win_rate, 4),
                    "avg_win_r": round(slot.avg_win_r, 4),
                    "kelly_fraction": round(
                        self.kelly_fraction(slot.win_rate, slot.avg_win_r), 4
                    ),
                    "history_len": len(slot.history),
                }
        return out

    def total_deployed_budget(self) -> float:
        """Sum of all slot budgets across all agents."""
        self._check_initialised()
        return sum(
            slot.budget_usd
            for slots in self._slots.values()
            for slot in slots.values()
        )
