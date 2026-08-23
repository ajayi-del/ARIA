"""Capacity governor — the per-symbol daily trade cap's evidence layer.

The cap is a CHURN guard (ETH: 35 trades in 5 days). The 2026-08-23 HYPE
autopsy showed it cannot distinguish churn from trend-riding: HYPE +41% in
7d fired 1011 signal_ready, hit the cap of 4 by 02:54 UTC, then 403 blocks
while the rally ran without us. MUBARAK was the same miss one pipe earlier
(silence, fixed in 8fa1855). This module is the cap's brain — pure, zero-I/O,
one splice in main.py.

Book grounding:
  Livermore    — re-entering a proven trend is the core play, not churn.
  Taleb        — count caps truncate the right tail; the trades a trend-day
                 symbol makes past the cap are exactly the convexity that
                 pays for the churn the cap exists to stop.
  Carver       — constrain RISK, never trade count. Exempted symbols live
                 inside a per-symbol daily risk budget (R = stop distance x
                 size), not a count.
  Thorp/Kelly  — capacity follows DEMONSTRATED edge: the symbol's own day
                 move, no BTC dependency (Hugo needs BTC >=3% — dead leg for
                 alt-specific rallies), no graduation-slot contention.
  Raschke      — day-type governs trade-frequency rules; the same day-move
                 evidence that forbids counter-trend entries (trend_day veto)
                 permits trend-direction re-entries. Symmetric thresholds.
  Steenbarger  — overtrading vs opportunity-taking is a process distinction:
                 direction ALTERNATION within the day is the churn signature;
                 monotone same-direction repetition is process. Flips kill
                 the evidence legs (graduated/Hugo survive — their own
                 machinery is already direction-locked).
  Aronson      — every exemption measured from birth: blocks open shadow
                 records (gate "daily_cap"), exempted trades are real journal
                 entries; the journal_evidence leg closes the loop by reading
                 the cap's OWN measured accuracy per symbol.
"""
from __future__ import annotations

from typing import Optional


def evaluate_cap(*, count: int, cap: int, direction: str,
                 recovery_active: bool = False,
                 graduated: bool = False, hugo_aligned: bool = False,
                 day_move_pct: Optional[float] = None,
                 day_move_threshold: float = 3.0,
                 day_move_enabled: bool = True,
                 dirs_today: Optional[dict] = None,
                 risk_consumed_usd: float = 0.0,
                 risk_budget_usd: float = 0.0,
                 journal_verdict: Optional[dict] = None,
                 journal_min_n: int = 10,
                 journal_max_accuracy: float = 0.35,
                 journal_enabled: bool = True,
                 mover_relief: Optional[dict] = None,
                 ) -> tuple[str, str]:
    """(decision, reason). decision in {"pass","exempt","block"}.

    "pass"   — under the cap, no exemption consulted.
    "exempt" — over the cap but evidence-gated trend participation; reason
               names the evidence leg.
    "block"  — reason in {"recovery","cap","r_budget_exhausted"}.
    """
    if count < cap:
        return "pass", ""
    # Recovery = capital preservation; no exemption adds capacity in recovery.
    if recovery_active:
        return "block", "recovery"

    reason = ""
    if graduated:
        reason = "graduated"
    elif hugo_aligned:
        reason = "hugo_aligned"
    else:
        dirs = dirs_today or {}
        churned = bool(dirs.get("long")) and bool(dirs.get("short"))
        if not churned:
            if (day_move_enabled and day_move_pct is not None
                    and abs(day_move_pct) >= day_move_threshold
                    and ((day_move_pct > 0) == (direction == "long"))):
                reason = "day_move_aligned"
            elif (isinstance(mover_relief, dict)
                    and mover_relief.get("direction") == direction):
                reason = "mover_relief"
            elif (journal_enabled and journal_verdict
                    and int(journal_verdict.get("n", 0)) >= journal_min_n
                    and float(journal_verdict.get("accuracy", 1.0))
                    <= journal_max_accuracy):
                reason = "journal_evidence"

    if not reason:
        return "block", "cap"

    # Carver uniformity: ALL exemption legs (graduated/Hugo included) live
    # inside the day's per-symbol risk budget. Budget 0 = unconfigured →
    # fail open on this check only (the count evidence already fired).
    if risk_budget_usd > 0 and risk_consumed_usd >= risk_budget_usd:
        return "block", "r_budget_exhausted"
    return "exempt", reason
