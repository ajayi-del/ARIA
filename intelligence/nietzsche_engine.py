"""
intelligence/nietzsche_engine.py — Will-to-Size Engine.

"How much force should I apply to this belief given my current state?"

Nietzsche asks the question that Kelly answers mathematically but
misses psychologically: the market doesn't care about your edge;
it cares about your ability to sustain pressure during drawdown.

This is a CONTINUOUS sizing function — NOT a gate.
  - It never blocks trades (DORMANT is the only full stop, and it
    mirrors the existing dd_tracker halt semantics).
  - It scales size between 0.10 and 1.50 based on:
      (a) drawdown band      — how deep in a hole are we?
      (b) win/loss streak    — are we in flow or friction?
      (c) conviction score   — how strong is this specific signal?
      (d) Kant size_cap      — hard ceiling from structure layer

The Will Table maps (drawdown_band, streak_band) → (WillState, base_mult).
Conviction score then modulates the base multiplier ±50%.
Kant's size_cap applies as a hard ceiling after all modulation.

Persistent memory: win/loss streaks are computed from the trade journal
(which survives restarts) — not from in-memory counters. This is how
Nietzsche "remembers" across sessions.

Latency: O(1) table lookup + float arithmetic, ~0.1ms.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from core.config import SYMBOL_MIN_QUANTITY

if TYPE_CHECKING:
    from intelligence.kant_engine import KantFrame


class WillState(Enum):
    AGGRESSIVE   = "aggressive"    # hot streak — lean harder
    NEUTRAL      = "neutral"       # baseline — follow signal
    CONSERVATIVE = "conservative"  # mild caution — reduce size
    DEFENSIVE    = "defensive"     # deep drawdown — survive mode
    DORMANT      = "dormant"       # near halt — stand down


@dataclass(frozen=True)
class NietzscheOutput:
    will_state:      WillState
    size_multiplier: float    # effective multiplier applied to base size
    order_type:      str      # "limit" | "market" | "probe" | "none"
    min_notional_ok: bool     # True = adjusted size meets venue minimum
    adjusted_size:   float    # final position units after all modulation
    reason:          str      # structured log string
    basket_cap_pct:  float = 1.0  # win-rate cap applied (1.0 = no cap)


# Will Table: (drawdown_band, streak_band) → (WillState, base_multiplier)
#
# Drawdown bands (as decimal fraction, NOT percentage):
#   "0-1%"    0.00–0.01
#   "1-2%"    0.01–0.02
#   "2-3%"    0.02–0.03
#   "3-5%"    0.03–0.05
#   "5-10%"   0.05–0.10
#   "10-20%"  0.10–0.20
#   "20-35%"  0.20–0.35
#   ">35%"    ≥ 0.35   ← DORMANT — only catastrophic halt
#
# Streak bands (effective streak = wins - losses×0.5):
#   "0-2"  effective < 3
#   "3-5"  3 ≤ effective < 6
#   "6+"   effective ≥ 6
#
# Design: system is counter-cyclical.
#   - Draws reduce size early (not binary) — survive the hole
#   - Win streaks earn the right to be aggressive — compound the edge
#   - DORMANT only at >35% — aligned with DrawdownManager 70% catastrophic halt
#     so Nietzsche softens long before DM ever hits the kill switch.

_WILL_TABLE: dict[str, dict[str, tuple[WillState, float]]] = {
    "0-1%": {
        "0-2": (WillState.NEUTRAL,      1.00),
        "3-5": (WillState.AGGRESSIVE,   1.25),
        "6+":  (WillState.AGGRESSIVE,   1.50),
    },
    "1-2%": {
        "0-2": (WillState.NEUTRAL,      1.00),
        "3-5": (WillState.NEUTRAL,      1.10),
        "6+":  (WillState.AGGRESSIVE,   1.25),
    },
    "2-3%": {
        "0-2": (WillState.CONSERVATIVE, 0.80),
        "3-5": (WillState.NEUTRAL,      0.90),
        "6+":  (WillState.NEUTRAL,      1.00),
    },
    "3-5%": {
        "0-2": (WillState.CONSERVATIVE, 0.50),
        "3-5": (WillState.CONSERVATIVE, 0.65),
        "6+":  (WillState.NEUTRAL,      0.80),
    },
    "5-10%": {
        "0-2": (WillState.DEFENSIVE,    0.25),
        "3-5": (WillState.DEFENSIVE,    0.35),
        "6+":  (WillState.CONSERVATIVE, 0.50),
    },
    "10-20%": {
        "0-2": (WillState.DEFENSIVE,    0.15),
        "3-5": (WillState.DEFENSIVE,    0.20),
        "6+":  (WillState.DEFENSIVE,    0.25),
    },
    "20-35%": {
        "0-2": (WillState.DEFENSIVE,    0.05),
        "3-5": (WillState.DEFENSIVE,    0.10),
        "6+":  (WillState.DEFENSIVE,    0.15),
    },
    ">35%": {
        "0-2": (WillState.DORMANT,      0.00),
        "3-5": (WillState.DORMANT,      0.00),
        "6+":  (WillState.DORMANT,      0.00),
    },
}

# Elite signal bypass — coherence above this overrides will state and
# always applies full Kant-capped size. "Current edge cannot be vetoed
# by past losses." Only truly exceptional signals qualify.
# Raised to 8.5 now that ceiling can reach 9.0 — elite requires top-decile conviction.
_ELITE_THRESHOLD = 8.5


class NietzscheEngine:
    """
    Continuous sizing engine — call compute() on every approved signal.

    Instantiate once at startup, shared across all symbols.
    Thread-safe: all state is read-only after construction.
    """

    def __init__(self, config) -> None:
        self._config = config
        self._current_will = WillState.NEUTRAL
        # Cybernetic feedback: empirical Kelly multipliers per (dd_band, streak_band)
        self._empirical_mults: dict[str, dict[str, float]] = {}
        self._empirical_states: dict[str, dict[str, WillState]] = {}

    def adapt(self, analytics) -> None:
        """
        Cybernetic feedback — called periodically with JournalAnalytics output.

        Replaces static Will Table cells with Kelly-optimal values when
        empirical sample size is sufficient.
        """
        from intelligence.journal_analytics import CyberneticAdjustments
        if not isinstance(analytics, CyberneticAdjustments):
            return
        for dd_band, sb_map in analytics.kelly_multipliers.items():
            self._empirical_mults.setdefault(dd_band, {})
            self._empirical_states.setdefault(dd_band, {})
            for sb, mult in sb_map.items():
                # Dampen: blend 50% old static + 50% new empirical
                old = self._empirical_mults[dd_band].get(sb, mult)
                self._empirical_mults[dd_band][sb] = round(old * 0.5 + mult * 0.5, 3)
                # Map string state back to WillState enum
                state_str = analytics.kelly_states.get(dd_band, {}).get(sb, "neutral")
                try:
                    self._empirical_states[dd_band][sb] = WillState(state_str)
                except ValueError:
                    self._empirical_states[dd_band][sb] = WillState.NEUTRAL

    # ── Public API ────────────────────────────────────────────────────────────

    def compute(
        self,
        drawdown_pct:     float,     # 0.03 = 3% drawdown (decimal, not %)
        win_streak:       int,        # consecutive wins from journal
        loss_streak:      int,        # consecutive losses from journal
        conviction_score: float,      # 0.0–1.0 from conviction_engine
        coherence:        float,      # raw coherence score
        kant_frame:       "KantFrame",
        base_size_units:  float,      # candidate.size before Nietzsche
        min_notional_usd: float,      # config.min_trade_notional_usd
        mark_price:       float,      # current mark price for notional calc
        balance:          float,      # current account balance
        symbol:           str = "",   # symbol for min_qty enforcement
        win_rate:         float = 0.5, # historical win rate [0,1]
    ) -> NietzscheOutput:
        """
        Compute the will-adjusted position size.

        Returns NietzscheOutput with adjusted_size ready to write
        back to candidate.size.
        """
        # ── Per-symbol minimum quantity floor ────────────────────────────────
        _min_qty = SYMBOL_MIN_QUANTITY.get(symbol, 0.0)

        # ── Elite signal bypass ───────────────────────────────────────────────
        # Exceptional coherence overrides all will state considerations.
        if coherence >= _ELITE_THRESHOLD:
            mult     = min(kant_frame.size_cap, 1.50)
            adjusted = self._enforce_min_notional(
                base_size_units * mult, min_notional_usd, mark_price
            )
            if _min_qty > 0 and adjusted < _min_qty:
                adjusted = _min_qty
            return NietzscheOutput(
                will_state      = WillState.AGGRESSIVE,
                size_multiplier = mult,
                order_type      = kant_frame.order_type,
                min_notional_ok = True,
                adjusted_size   = round(adjusted, 6),
                reason          = f"elite_coherence={coherence:.1f}",
                basket_cap_pct  = 1.0,
            )

        # ── Will Table lookup ─────────────────────────────────────────────────
        dd_band     = _dd_band(drawdown_pct, balance)
        streak_band = _streak_band(win_streak, loss_streak)
        # Cybernetic override: use empirical Kelly multipliers if available
        if dd_band in self._empirical_mults and streak_band in self._empirical_mults[dd_band]:
            base_mult = self._empirical_mults[dd_band][streak_band]
            state = self._empirical_states[dd_band].get(streak_band, WillState.NEUTRAL)
        else:
            state, base_mult = _WILL_TABLE[dd_band][streak_band]
        self._current_will = state

        # ── Dormant → full stop ───────────────────────────────────────────────
        if state == WillState.DORMANT:
            return NietzscheOutput(
                will_state      = state,
                size_multiplier = 0.0,
                order_type      = "none",
                min_notional_ok = False,
                adjusted_size   = 0.0,
                reason          = f"dormant dd={dd_band} streak={streak_band}",
                basket_cap_pct  = 1.0,
            )

        # ── Conviction modulation ─────────────────────────────────────────────
        # conviction 0.5 = neutral (no change to base_mult)
        # conviction 1.0 = +50% boost
        # conviction 0.0 = -50% reduction
        conviction_mult = 0.50 + conviction_score
        conviction_mult = max(0.10, min(1.50, conviction_mult))

        # ── Final multiplier ──────────────────────────────────────────────────
        final_mult = min(base_mult * conviction_mult, kant_frame.size_cap)
        final_mult = max(0.10, final_mult)

        # ── Compute adjusted units ────────────────────────────────────────────
        adjusted = base_size_units * final_mult

        # ── Win-rate basket cap ───────────────────────────────────────────────
        # When win rate is low, cap total basket exposure as % of balance.
        # Applied BEFORE min_notional so sub-floor sizes skip gracefully.
        basket_cap_pct = _win_rate_band(win_rate)
        _cap_applied = False
        if basket_cap_pct < 1.0 and balance > 0 and mark_price > 0:
            cap_usd = balance * basket_cap_pct
            cap_units = cap_usd / mark_price
            if adjusted > cap_units:
                adjusted = cap_units
                _cap_applied = True

        # ── Min notional guard ────────────────────────────────────────────────
        actual_notional = adjusted * mark_price if mark_price > 0 else 0.0
        min_notional_ok = actual_notional >= min_notional_usd

        # Auto-bump only when basket cap is NOT active — never violate the cap
        if kant_frame.min_notional_adjust and basket_cap_pct >= 1.0:
            adjusted = self._enforce_min_notional(adjusted, min_notional_usd, mark_price)
            actual_notional = adjusted * mark_price
            min_notional_ok = actual_notional >= min_notional_usd

        # ── Per-symbol minimum quantity floor ─────────────────────────────────
        if _min_qty > 0 and adjusted < _min_qty:
            adjusted = _min_qty

        _reason = (
            f"dd={dd_band} streak={streak_band} "
            f"conv={conviction_score:.2f} will={state.value}"
        )
        if _cap_applied:
            _reason += f" basket_cap={basket_cap_pct:.1%}"

        return NietzscheOutput(
            will_state      = state,
            size_multiplier = round(final_mult, 3),
            order_type      = kant_frame.order_type,
            min_notional_ok = min_notional_ok,
            adjusted_size   = round(adjusted, 6),
            reason          = _reason,
            basket_cap_pct  = basket_cap_pct,
        )

    @property
    def will_state(self) -> WillState:
        return self._current_will

    # ── Private ───────────────────────────────────────────────────────────────

    @staticmethod
    def _enforce_min_notional(
        units: float, min_notional: float, mark_price: float
    ) -> float:
        """Auto-bump units to meet venue minimum (e.g. OP-USD $45 → $50)."""
        if mark_price <= 0 or min_notional <= 0:
            return units
        min_units = min_notional / mark_price
        return max(units, min_units)


# ── Module-level helpers (used by engine and unit tests) ──────────────────────

def _dd_band(dd: float, balance: float = 0.0) -> str:
    """Map decimal drawdown fraction to Will Table band key.

    Small accounts (<$150) use wider bands so they can still trade through
    deeper drawdowns — a $67 account at 30% DD needs to trade, not hibernate.

    Aligned with softened DrawdownManager: DORMANT only at catastrophic (>35%).
    """
    if balance < 150.0:
        # Wider thresholds for small accounts — dormant only at total wipeout
        if dd < 0.15: return "0-1%"
        if dd < 0.25: return "1-2%"
        if dd < 0.35: return "2-3%"
        if dd < 0.50: return "3-5%"
        if dd < 0.70: return "5-10%"
        if dd < 1.00: return "10-20%"
        return ">35%"
    # Standard thresholds for $150+ accounts
    if dd < 0.01: return "0-1%"
    if dd < 0.02: return "1-2%"
    if dd < 0.03: return "2-3%"
    if dd < 0.05: return "3-5%"
    if dd < 0.10: return "5-10%"
    if dd < 0.20: return "10-20%"
    if dd < 0.35: return "20-35%"
    return ">35%"


def _streak_band(wins: int, losses: int) -> str:
    """
    Map win/loss counts to Will Table streak band.

    Effective streak = wins - losses × 0.5
    A loss is only half as damaging as a win is rewarding.
    """
    effective = wins - (losses * 0.5)
    if effective >= 6: return "6+"
    if effective >= 3: return "3-5"
    return "0-2"


def _win_rate_band(win_rate: float) -> float:
    """Map historical win rate to basket cap as fraction of balance.

    win_rate >= 0.50  → 1.00  (no cap)
    win_rate 0.40-0.50 → 0.10  (10% of balance)
    win_rate 0.30-0.40 → 0.05  (5% of balance)
    win_rate < 0.30    → 0.025 (2.5% of balance)
    """
    if win_rate >= 0.50:
        return 1.0
    if win_rate >= 0.40:
        return 0.10
    if win_rate >= 0.30:
        return 0.05
    return 0.025
