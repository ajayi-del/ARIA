"""
risk/streak_sizing.py — Streak-Aware Position Compounding
ARIA Execution Alpha Patch — Component 7

Tracks consecutive wins per symbol+direction.
Compounds size on winning streaks (listen to what the market is saying).
Resets fully on any loss — no carry-over.

Livermore rule (win side): when a trade works, the next one in the same direction
on the same symbol should be slightly larger (market is proving you right).
Caps at 1.30× to prevent runaway compounding.

Livermore rule (loss side): when a trade fails repeatedly in the same direction,
ARIA should stop fighting the tape. get_loss_strikes() exposes the loss streak
so the directional cooldown block in main.py can gate entries intelligently.
"""
from __future__ import annotations

from collections import defaultdict
import structlog

log = structlog.get_logger(__name__)

# streak_count → size multiplier
_MULT_TABLE = {0: 1.0, 1: 1.10, 2: 1.20}
_STREAK_CAP = 1.30


class StreakTracker:
    """Per symbol+direction consecutive-win size compounding + loss-side tracking."""

    def __init__(self) -> None:
        self._streaks: dict = defaultdict(int)       # f"{symbol}_{direction}" → int wins
        self._loss_streaks: dict = defaultdict(int)  # f"{symbol}_{direction}" → int losses

    # ── Called from _record_close() ──────────────────────────────────────────

    def on_trade_closed(self, symbol: str, direction: str, pnl: float) -> None:
        key = f"{symbol}_{direction}"
        if pnl > 0:
            self._streaks[key] += 1
            # Win resets the loss streak for this direction
            self._loss_streaks[key] = 0
        else:
            self._streaks[key] = 0
            self._loss_streaks[key] += 1

    # ── Called in sizing chain ────────────────────────────────────────────────

    def get_streak_multiplier(self, symbol: str, direction: str) -> float:
        key    = f"{symbol}_{direction}"
        streak = self._streaks[key]
        mult   = min(_MULT_TABLE.get(streak, _STREAK_CAP), _STREAK_CAP)
        if streak > 0:
            log.info("streak_sizing_applied",
                     symbol=symbol, direction=direction,
                     streak=streak, mult=round(mult, 2))
        return mult

    def get_loss_strikes(self, symbol: str, direction: str) -> int:
        """Returns consecutive loss count for this symbol+direction."""
        return self._loss_streaks.get(f"{symbol}_{direction}", 0)

    def current_streaks(self) -> dict:
        return {k: v for k, v in self._streaks.items() if v > 0}

    def current_loss_streaks(self) -> dict:
        return {k: v for k, v in self._loss_streaks.items() if v > 0}
