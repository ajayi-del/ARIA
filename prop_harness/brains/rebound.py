"""Rebound brain — underwater-recovery cycle tracker (2026-11-17).

Governor observation: some coins dip underwater and come back several
times before the ladder pays out. Each dip-then-recover cycle is the
market DEFENDING the entry level — a mean-reversion resilience signal
that the birth-R exit stack is blind to (it only sees peak and trough,
never the round trips between them).

Mechanism (ratchet idiom, mirror of the MFE/MAE trackers):
  * ARM when the mark moves to <= -arm_r (position meaningfully
    underwater — noise-band dips do not count).
  * COUNT when an armed position recovers to >= recover_r (breakeven by
    default) — the level held; re-arm for the next dip.
  * rebound_count accrues on the bracket, rides the registry row into
    the close journal, and feeds the cohort evidence planes
    (loss-as-signal / edge_boost read closed brackets — rebound_count
    becomes a cohort feature for free).

Live effect (bounded, safety-direction only): a rebound-qualified
bracket (count >= min_count) that is CURRENTLY underwater may not have
its stop sharpened past breakeven into the dip it historically recovers
from — the sharpen is DECLINED, never loosened. All other protection
(bank, trail, live-peak) is untouched.

Pure module: verdict math only. IO + journaling live in the executor.
"""

from __future__ import annotations

from typing import Any

from .eval_profile import mark_moved_r, stop_locks_entry, _f

DEFAULT_ARM_R = 0.5        # dip depth (birth R) that arms the rebound watch
DEFAULT_RECOVER_R = 0.0    # breakeven recovery counts the cycle
DEFAULT_MIN_COUNT = 2      # cycles before the bracket is rebound-qualified


def track_rebound(bracket: dict, mark: Any, *,
                  arm_r: float = DEFAULT_ARM_R,
                  recover_r: float = DEFAULT_RECOVER_R
                  ) -> tuple[bool, bool]:
    """Advance the rebound state machine. Returns (mutated, recovered).

    recovered=True exactly on the tick a cycle completes (armed ->
    recovered through breakeven) so the caller journals one event per
    cycle, not per tick. Bad geometry / unusable mark abstains.
    """
    moved = mark_moved_r(bracket, mark)
    if moved is None:
        return False, False
    mutated = False
    recovered = False
    armed = bool(bracket.get("rebound_armed"))
    if not armed and moved <= -float(arm_r):
        bracket["rebound_armed"] = True
        armed = True
        mutated = True
    if armed and moved >= float(recover_r):
        bracket["rebound_count"] = int(bracket.get("rebound_count") or 0) + 1
        bracket["rebound_armed"] = False
        mutated = True
        recovered = True
    return mutated, recovered


def rebound_qualified(bracket: dict, *,
                      min_count: int = DEFAULT_MIN_COUNT) -> bool:
    try:
        return int(bracket.get("rebound_count") or 0) >= int(min_count)
    except (TypeError, ValueError):
        return False


def underwater(bracket: dict, mark: Any) -> bool:
    """Mark adverse vs entry (buy: mark < entry; sell mirrored)."""
    moved = mark_moved_r(bracket, mark)
    return moved is not None and moved < 0.0


def sharpen_suppressed(bracket: dict, mark: Any, target: Any, *,
                       min_count: int = DEFAULT_MIN_COUNT) -> bool:
    """Decline a stop sharpen when ALL of:
      * the bracket is rebound-qualified (the level has held before),
      * the position is currently underwater (the dip in progress), and
      * the target would lock past entry (tightening INTO the dip band).
    Never loosens anything — returning True only skips a tighten vote.
    """
    if not rebound_qualified(bracket, min_count=min_count):
        return False
    if not underwater(bracket, mark):
        return False
    return bool(stop_locks_entry(bracket.get("side"), target,
                                 bracket.get("entry_ref")))
