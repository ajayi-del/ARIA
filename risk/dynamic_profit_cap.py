"""
risk/dynamic_profit_cap.py — Dynamic ROE-Based Profit Cap

Monitors open positions and triggers market-close when ROE hits the
regime-specific cap:
  TREND   → 10% ROE
  SCALP   → 4%  ROE
  DEFAULT → 6%  ROE

ROE calculation (perps):
  ROE % = (mark - entry) / entry * leverage * 100   (longs)
  ROE % = (entry - mark) / entry * leverage * 100   (shorts)

Design:
  • Pure function for ROE calc — testable, no side effects.
  • Loop cadence: 5 s (fast enough to catch moves, slow enough to not spam).
  • Market-order close — guarantees fill at cap.
  • Logs: cap_hit, regime, roe, symbol — essential for post-trade review.

Integration:
  Called from main.py _dynamic_profit_cap_loop().
  Requires: position_manager, mark_price_stores, client, _close_with_retry.
"""

from __future__ import annotations

import structlog
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from execution.schemas import Position

logger = structlog.get_logger(__name__)


# Direct mapping — avoids circular imports and enum gymnastics.
_ROE_CAP_MAP: dict[str, float] = {
    "trend":   12.0,
    "scalp":    6.0,
    "default":  8.0,
}


def compute_roe_pct(position: "Position", mark: float) -> float:
    """
    Compute Return-on-Equity % for a position at current mark price.
    Returns 0.0 if entry is invalid.
    """
    entry = float(getattr(position, "entry_price", 0) or 0)
    if entry <= 0 or mark <= 0:
        return 0.0

    lev = int(getattr(position, "leverage", 5) or 5)
    side = getattr(position, "side", "long")

    if side == "long":
        pnl_pct = (mark - entry) / entry
    else:
        pnl_pct = (entry - mark) / entry

    return pnl_pct * lev * 100.0


def should_cap(position: "Position", mark: float) -> tuple[bool, float, float]:
    """
    Check if position has hit its dynamic profit cap.

    Returns:
        (hit: bool, roe_pct: float, cap_pct: float)
    """
    roe = compute_roe_pct(position, mark)
    regime = getattr(position, "trade_regime", "default") or "default"
    cap = _ROE_CAP_MAP.get(regime, 6.0)

    hit = roe >= cap
    return hit, roe, cap
