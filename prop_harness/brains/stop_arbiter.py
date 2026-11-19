"""Stop arbiter — per-position per-tick stop coalescing (prop harness).

The wound (CEO audit S4-C16, 2026-11-17): SIX exit-stack writers each
mutate bracket["current_stop"] per tick — protect_realized_bank,
arm_breakeven, arm_breakeven_from_mark, trail_bracket,
sharpen_mfe_bracket, lock_live_peak, harvest_runner_bank_floor. Every
one fired its OWN venue resize the moment its verdict tightened, so a
runner crossing three rungs in one tick paid three place+cancel storms
(and three 429 lottery tickets) for one net stop move.

The arbiter turns the tick into a ballot: inside the reconcile window
(begin_stop_tick ... flush_stop_arbiter) each writer VOTES its floor
instead of touching the venue; the flush picks the tightest vote
(max-floor — highest protective stop for longs, lowest for shorts),
checks it moved past the dust epsilon vs the last issued floor, and
fires AT MOST one venue resize + one current_stop write.

Pure zero-I/O register: ballots are plain lists of proposal dicts;
the executor owns all I/O. Kill switch PROP_STOP_ARBITER=0 → the
writers call resize_protection directly, legacy bit-for-bit. Size-sync
(qty-change) resizes never route here — they are qty-driven, not
floor-driven, and bypass the dust epsilon by design.
"""

from __future__ import annotations

from typing import Any

# Stage names riding proposals / journal rows.
STAGES = ("bank", "breakeven", "breakeven_mark", "trail",
          "mfe_sharpen", "live_peak", "runner_harvest")

COMMIT = "stop_arbiter_commit"
FAILED = "stop_arbiter_failed"
SUPPRESSED = "stop_arbiter_suppressed"


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def is_tighter(side: str | None, a: Any, b: Any) -> bool:
    """True when stop a protects more than stop b (strict).

    Long (buy): the stop is a sell BELOW — tighter = HIGHER price.
    Short (sell): mirrored — tighter = LOWER price.
    Unparseable side/values abstain False (never loosen on doubt)."""
    fa, fb = _f(a), _f(b)
    if fa is None or fb is None:
        return False
    if (side or "").lower() == "buy":
        return fa > fb
    if (side or "").lower() == "sell":
        return fa < fb
    return False


def propose(ballot: list[dict], *, stage: str, target: Any) -> list[dict]:
    """Append one stage's vote. A stage re-voting inside one tick
    replaces its own vote (it recomputed with fresher state).
    Unparseable targets never enter the ballot."""
    ft = _f(target)
    if ft is None:
        return ballot
    for i, p in enumerate(ballot):
        if p.get("stage") == stage:
            ballot[i] = {"stage": stage, "target": str(target)}
            return ballot
    ballot.append({"stage": stage, "target": str(target)})
    return ballot


def winner(ballot: list[dict], side: str | None) -> dict | None:
    """The max-floor vote: tightest proposal wins; ties keep the FIRST
    stage (deterministic journal attribution)."""
    best: dict | None = None
    for p in ballot or []:
        if best is None or is_tighter(side, p.get("target"),
                                      best.get("target")):
            best = p
    return best


def past_dust(side: str | None, target: Any, prev: Any,
              frac: float, eps_abs: float | None = None) -> bool:
    """True when the winning floor moved past the dust epsilon vs the
    last issued floor — sub-epsilon moves are not worth a venue
    round-trip. prev unparseable → True (a first issue always fires).
    frac <= 0 → any strictly-tighter move fires (legacy sensitivity).

    eps_abs (audit 2026-11-17): the frac path computes eps = |prev|*frac —
    PRICE-relative, so a $20k-stop asset at 0.0005 swallows ~0.5R of the
    early tighten band. When eps_abs is given (price units, computed by
    the caller as dust_r × stop distance) it REPLACES the frac epsilon.
    None = legacy bit-for-bit."""
    ft, fp = _f(target), _f(prev)
    if ft is None:
        return False
    if fp is None:
        return True
    if eps_abs is not None:
        try:
            eps = max(0.0, float(eps_abs))
        except (TypeError, ValueError):
            eps = 0.0
    else:
        try:
            f = float(frac or 0.0)
        except (TypeError, ValueError):
            f = 0.0
        f = max(0.0, f)
        eps = abs(fp) * f
    if (side or "").lower() == "buy":
        return ft > fp + eps
    if (side or "").lower() == "sell":
        return ft < fp - eps
    return False
