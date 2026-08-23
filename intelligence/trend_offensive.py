"""Trend Offensive — "Hugo" (2026-08-22).

The right-tail doctrine switch. The 3-day autopsy (08-20→08-22, −$37.8 vs
BTC +24%) proved the machine amputates the right tail by design: 6 early-exit
mechanisms (TP ladder 6-12%, treasury harvest 6% ROE, winner escape 7%,
runaway trim 7%, conviction_decay 30-min clock, time stops) against 1 ride
mechanism; winner hold median 0.30h, MAX 3.98h, on days the tape ran for 16.
122 base-rate vetoes fired on the rally day from a chop-era trailing WR.

The masters' answer (Livermore: "the big money is in the sitting";
Druckenmiller: "when you have conviction, bet the ranch"; Seykota: "ride
your winners"): when the evidence stack says TREND DAY, flip doctrine for
the aligned direction — size up, downgrade stale base-rate vetoes to size
discounts, suspend the fixed TP ladder and treasury harvest (the trail owns
the exit), grant eviction immunity and extended conviction grace, and let
winners pyramid on strength instead of only post-TP1.

Anti-flush doctrine (operator question 2026-08-22): a market-wide liquidation
is NOT a trend to this brain. Activation REQUIRES the day_move evidence
(move from the 00:00 UTC open, ≥ threshold) among the aligned votes — a
one-hour liquidation wick that retraces never sustains a 3% day move with
HTF alignment. A REAL crash (BTC −8% day, HTF bearish, funding fuel,
risk_off leadership) arms SHORT mode — the symmetric doctrine: size up
shorts, trail them, don't harvest the short cluster.

Department template (docs/DEPARTMENT_TEMPLATE.md):
  - Pure logic, zero I/O. Votes are gathered by the main.py executor and
    injected as a dict {name: -1|0|+1}; the brain counts and runs the state
    machine. Evidence names are open — a 7th vote (e.g. SSI sector spread)
    needs no brain change.
  - One splice point: the executor loop owns this instance and publishes
    its mode; consumers read modifiers through the published state.
  - Kill switch: with TREND_OFFENSIVE_ENABLED=false the executor never lets
    the brain leave "off" — modifiers are all neutral = pre-module system
    bit-for-bit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

EVIDENCE_NAMES = ("day_move", "htf", "cascade", "funding", "dispersion", "rally")


@dataclass
class OffensiveDecision:
    mode: str = "off"                    # "off" | "long" | "short"
    n_aligned: int = 0                   # votes aligned with the current mode
    votes: dict = field(default_factory=dict)
    changed: bool = False                # mode transitioned this evaluation
    previous_mode: str = "off"
    since: float = 0.0                   # ts the current mode activated


class TrendOffensive:
    """Vote-count state machine with confirmation streak and decay hysteresis.

    Entry: one direction strictly leads with ≥ entry_n votes AND day_move is
    among them, for confirm_evals consecutive evaluations (anti-flap — the
    2026-08-20 graduation cooloff storm came from single-tick transitions).
    Exit: aligned votes fall below exit_n and stay there for decay_s, or the
    opposite direction fully qualifies (immediate flip).
    """

    def __init__(self, *, entry_n: int = 4, exit_n: int = 3,
                 confirm_evals: int = 2, decay_s: float = 900.0,
                 size_boost: float = 2.0, veto_discount: float = 0.35,
                 grace_mult: float = 4.0, now_fn=time.time):
        self.entry_n = int(entry_n)
        self.exit_n = int(exit_n)
        self.confirm_evals = int(confirm_evals)
        self.decay_s = float(decay_s)
        self.size_boost = float(size_boost)
        self.veto_discount = float(veto_discount)
        self._grace_mult = float(grace_mult)
        self._now = now_fn
        self.mode: str = "off"
        self._since: float = 0.0
        self._qual_streak: int = 0
        self._decay_since: float = 0.0

    def reset(self) -> None:
        self.mode = "off"
        self._since = 0.0
        self._qual_streak = 0
        self._decay_since = 0.0

    def evaluate(self, votes: dict) -> OffensiveDecision:
        now = self._now()
        long_n = sum(1 for v in votes.values() if v > 0)
        short_n = sum(1 for v in votes.values() if v < 0)
        if long_n > short_n:
            cand_dir, cand_n = "long", long_n
        elif short_n > long_n:
            cand_dir, cand_n = "short", short_n
        else:
            cand_dir, cand_n = "off", max(long_n, short_n)

        # A trend day needs a trend: day_move must vote with the majority.
        dm = int(votes.get("day_move", 0) or 0)
        dm_ok = (cand_dir == "long" and dm > 0) or (cand_dir == "short" and dm < 0)
        qualifies = cand_dir != "off" and cand_n >= self.entry_n and dm_ok

        previous = self.mode
        if self.mode == "off":
            if qualifies:
                self._qual_streak += 1
                if self._qual_streak >= self.confirm_evals:
                    self.mode = cand_dir
                    self._since = now
                    self._decay_since = 0.0
            else:
                self._qual_streak = 0
        else:
            if qualifies and cand_dir != self.mode:
                # Thesis flip with a fully-qualified opposite stack — immediate.
                self.mode = cand_dir
                self._since = now
                self._decay_since = 0.0
                self._qual_streak = self.confirm_evals
            else:
                mode_n = long_n if self.mode == "long" else short_n
                if cand_dir == self.mode and mode_n >= self.exit_n:
                    self._decay_since = 0.0    # evidence healthy
                else:
                    if self._decay_since <= 0.0:
                        self._decay_since = now
                    elif now - self._decay_since >= self.decay_s:
                        self.reset()

        if self.mode == "long":
            aligned = long_n
        elif self.mode == "short":
            aligned = short_n
        else:
            aligned = cand_n
        return OffensiveDecision(
            mode=self.mode, n_aligned=aligned, votes=dict(votes),
            changed=(self.mode != previous), previous_mode=previous,
            since=self._since,
        )

    # ── Modifiers — neutral unless the position/signal rides the mode ────────

    def _aligned(self, direction: str) -> bool:
        return self.mode in ("long", "short") and direction == self.mode

    def size_mult(self, direction: str) -> float:
        return self.size_boost if self._aligned(direction) else 1.0

    def veto_discount_mult(self, direction: str) -> float:
        """Base-rate veto downgraded from size-ZERO to a size discount."""
        return self.veto_discount if self._aligned(direction) else 1.0

    def tp_suspended(self, direction: str) -> bool:
        """Fixed TP ladder replaced by the trail for aligned runners."""
        return self._aligned(direction)

    def harvest_suspended(self, direction: str) -> bool:
        """Treasury trail-lock/TP/recycle suspended for the aligned cluster."""
        return self._aligned(direction)

    def eviction_immune(self, direction: str) -> bool:
        """Aligned runners may not be weakest-evicted by new signals."""
        return self._aligned(direction)

    def grace_mult(self, direction: str) -> float:
        """Conviction-review grace extension for aligned positions."""
        return self._grace_mult if self._aligned(direction) else 1.0
