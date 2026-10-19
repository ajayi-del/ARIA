"""
intelligence/c_tier_coldstart.py — c_tier cold-start abstain gate

Evidence: 12,612 equity signal_ready → 0 brackets since 2026-10-17. Equity
signals clear the 2.05 coherence floor but classify_signal lands them c_tier
because hist_wr defaults 0.5 on never-traded symbols (hist_wr=0.5 contributes
exactly 0 to the composite edge) — a permanent deadlock: no trades → no
evidence → c_tier forever.

Doctrine (NO-EVIDENCE ABSTAIN): a gate may not hard-reject on evidence it
does not have. When the c_tier classification stems from hist_wr having NO
journal evidence (real closes below the skeptic's VETO_MIN_N), the candidate
passes at a size haircut and the would-be rejection is shadow-scored under
gate "c_tier_coldstart" — the cold-start cohort is measured from birth. Real
negative evidence (n ≥ min_n, WR below breakeven) rejects exactly as before
(bit-for-bit legacy).

Evidence base = REAL journal closes (count_real_closes over day files),
NEVER the skeptic's shadow-record n: shadow scored_records count gate
refusals, and starved equities carry thousands of them (2,077 c_tier kills
in one week) — a shadow-keyed verdict reads n >> min_n on never-traded
symbols and the pass never fires (dead-register finding, 2026-10-19).

Equity-class only: crypto symbols have months of shadow evidence behind
their priors; the deadlock is specific to the never-traded equity cohort
(Governor's stocks-live order 2026-10-17).

Kill switch: C_TIER_COLDSTART_PASS_ENABLED (default true). False = legacy
hard-reject bit-for-bit (the legacy reject stays shadow-scored under gate
"c_tier" via REJECTION_EVENTS — no extra record needed in the OFF state).
"""
from __future__ import annotations

import os
from typing import Optional

from intelligence.skeptic import VETO_MIN_N
from memory.trade_journal import is_operator_record, is_phantom_record

COLDSTART_SIZE_MULT = 0.5
COLDSTART_EVIDENCE_DAYS = 30       # real-close evidence horizon (day files)

_CLOSED_OUTCOMES = frozenset({"win", "loss"})


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


def count_real_closes(rows, symbol) -> Optional[int]:
    """Count REAL closes (outcome win/loss) for symbol in injected journal
    day-file rows, with the same read-path filters as journal.get_closed()
    (phantom scale-ghosts + operator orphan closes never count as evidence).

    The evidence base is REAL CLOSES ONLY — never skeptic shadow records:
    starved equities carry thousands of shadow c_tier refusals, so a shadow
    count reads n >> min_n on symbols with zero real history and the pass
    would never fire (2026-10-19 dead-register finding).

    Fail-closed: None on any defect → caller treats as real evidence →
    legacy reject.
    """
    try:
        if not isinstance(rows, (list, tuple)) or not symbol:
            return None
        n = 0
        for e in rows:
            if not isinstance(e, dict):
                continue
            if e.get("symbol") != symbol:
                continue
            if e.get("outcome") not in _CLOSED_OUTCOMES:
                continue
            if is_phantom_record(e) or is_operator_record(e):
                continue
            n += 1
        return n
    except Exception:
        return None
