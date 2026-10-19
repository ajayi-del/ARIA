"""
intelligence/c_tier_coldstart.py — c_tier cold-start abstain gate

Evidence: 12,612 equity signal_ready → 0 brackets since 2026-10-17. Equity
signals clear the 2.05 coherence floor but classify_signal lands them c_tier
because hist_wr defaults 0.5 on never-traded symbols (hist_wr=0.5 contributes
exactly 0 to the composite edge) — a permanent deadlock: no trades → no
evidence → c_tier forever.

Doctrine (NO-EVIDENCE ABSTAIN): a gate may not hard-reject on evidence it
does not have. When the c_tier classification stems from hist_wr having NO
journal evidence (n below the skeptic's VETO_MIN_N), the candidate passes at
a size haircut and the would-be rejection is shadow-scored under gate
"c_tier_coldstart" — the cold-start cohort is measured from birth. Real
negative evidence (n ≥ min_n, WR below breakeven) rejects exactly as before
(bit-for-bit legacy).

Equity-class only: crypto symbols have months of shadow evidence behind
their priors; the deadlock is specific to the never-traded equity cohort
(Governor's stocks-live order 2026-10-17).

Kill switch: C_TIER_COLDSTART_PASS_ENABLED (default true). False = legacy
hard-reject bit-for-bit (the legacy reject stays shadow-scored under gate
"c_tier" via REJECTION_EVENTS — no extra record needed in the OFF state).
"""
from __future__ import annotations

import os

from intelligence.skeptic import VETO_MIN_N

COLDSTART_SIZE_MULT = 0.5


def c_tier_coldstart_enabled() -> bool:
    return os.environ.get("C_TIER_COLDSTART_PASS_ENABLED", "true").strip().lower() != "false"


def coldstart_verdict(n_evidence, min_n: float = VETO_MIN_N) -> bool:
    """True = no measured basis to reject (evidence below the skeptic minimum).

    Fail-closed: unreadable evidence counts as REAL evidence → legacy reject.
    """
    try:
        return float(n_evidence) < float(min_n)
    except (TypeError, ValueError):
        return False
