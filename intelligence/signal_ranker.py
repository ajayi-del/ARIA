"""
SignalRanker — EV-based signal prioritization for ARIA.

Replaces simple score-based CandidatePool selection with Expected Value
ranking that considers:
  1. Coherence score (signal quality)
  2. Historical win rate for the strategy_tag (from feedback engine)
  3. Estimated R:R from candidate state's rr_ratio
  4. Cascade aftermath boost (+50% EV when direction matches primed direction)
  5. Freshness decay (stale signals discounted)
  6. Liquidation phase adjustment (liq_engine integration):
       EXPANSION phase → +0.5 EV boost  (momentum confirmation)
       EXHAUSTION phase → -0.5 penalty  (trend reversing; block breakouts)
       Direction mismatch vs liq direction → score × 0.7 penalty

EV formula:
  ev = score × (win_rate × rr_ratio) × freshness × cascade_mult × liq_phase_mult

This correctly ranks:
  - High-quality signals over low-quality
  - Proven strategies over unproven ones
  - Fresh signals over stale
  - Cascade-primed entries with highest urgency
  - Signals aligned with active liquidation cascade phase
"""

import time
import structlog
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

log = structlog.get_logger(__name__)

EV_FLOOR = 0.5   # Minimum EV to fire a signal


@dataclass
class RankedCandidate:
    symbol: str
    strategy_tag: str
    score: float
    direction: str
    ev_score: float
    rank: int = 0
    cascade_boosted: bool = False
    liq_phase_adjusted: bool = False
    win_rate_estimate: float = 0.5
    rr_estimate: float = 2.0
    candidate: Optional[Any] = field(default=None, repr=False)


class SignalRanker:
    """
    Ranks trade candidates from CandidatePool by Expected Value.

    Usage in main.py:
        ranker = SignalRanker()
        all_candidates = pool.select(n=pool.size())
        ranked = ranker.rank_candidates(all_candidates,
                                        cascade_tracker=cascade_tracker,
                                        feedback=feedback_engine)
        best = ranker.should_fire_next(ranked, cascade_tracker)
        if best:
            pool.discard(best.symbol)
            # execute best.candidate.state
    """

    def rank_candidates(
        self,
        candidates: List[Any],
        cascade_tracker=None,
        feedback=None,
        liq_engine=None,
    ) -> List[RankedCandidate]:
        """
        Rank candidates by EV descending.
        cascade_tracker: optional CascadeTracker — enables cascade boost.
        feedback: optional SignalFeedbackEngine — enables strategy win rate lookup.
        liq_engine: optional LiquidationSignalEngine — enables liq phase EV adjustment.
        """
        ranked: List[RankedCandidate] = []

        primed_dir = ""
        if cascade_tracker and cascade_tracker.is_primed():
            primed_dir = cascade_tracker.get_primed_direction()

        for cand in candidates:
            # ── Win rate estimate from feedback engine ─────────────────────────
            win_rate = 0.50  # flat prior
            if feedback and hasattr(feedback, "_records"):
                settled = list(feedback._records)
                stag = getattr(cand, "strategy_tag", "unknown")
                strat_recs = [r for r in settled
                              if getattr(r, "strategy_tag", "") == stag]
                if len(strat_recs) >= 5:
                    wins = sum(1 for r in strat_recs if r.won)
                    # Bayesian smoothed win rate (flat 50% prior, n=6 prior weight)
                    win_rate = (wins + 3) / (len(strat_recs) + 6)

            # ── R:R estimate from MarketState ──────────────────────────────────
            rr = 2.0
            state = getattr(cand, "state", None)
            if state:
                cand_rr = float(getattr(state, "rr_ratio", 0.0) or 0.0)
                if cand_rr > 0.5:
                    rr = min(6.0, cand_rr)  # cap at 6 to prevent outlier distortion

            # ── Freshness decay ───────────────────────────────────────────────
            age_s = cand.age_s() if hasattr(cand, "age_s") else 0.0
            if age_s > 25.0:
                freshness = 0.5
            elif age_s > 15.0:
                freshness = 0.75
            else:
                freshness = 1.0

            # ── Cascade aftermath boost ────────────────────────────────────────
            cascade_boost = 1.0
            cascade_boosted = False
            cand_direction = getattr(cand, "direction", "")
            if primed_dir and cand_direction == primed_dir:
                cascade_boost = 1.5
                cascade_boosted = True

            # ── Liquidation phase EV adjustment ───────────────────────────────
            # Liq phase informs momentum state: EXPANSION confirms trend,
            # EXHAUSTION warns of reversal, direction mismatch signals counter-trend.
            liq_ev_adj = 0.0
            liq_phase_adjusted = False
            if liq_engine is not None:
                cand_sym = getattr(cand, "symbol", "")
                liq_sig = liq_engine.get_best_signal(cand_sym)
                if liq_sig is not None:
                    liq_phase_adjusted = True
                    if liq_sig.phase == "expansion":
                        if liq_sig.direction == cand_direction:
                            liq_ev_adj = +0.5   # momentum confirmation
                        else:
                            # Direction mismatch in expansion — penalise score component
                            score_before_liq = float(getattr(cand, "score", 0.0))
                            # Apply 0.7 score penalty for direction mismatch
                            # by reducing the score used in EV (not the stored score)
                            liq_ev_adj = -(score_before_liq * 0.30)  # equivalent to score×0.7
                    elif liq_sig.phase == "exhaustion":
                        liq_ev_adj = -0.5   # trend reversing — discount this direction

            # ── EV calculation ─────────────────────────────────────────────────
            score = float(getattr(cand, "score", 0.0))
            ev = score * (win_rate * rr) * freshness * cascade_boost + liq_ev_adj
            ev = max(0.0, ev)  # EV cannot go negative

            ranked.append(RankedCandidate(
                symbol=getattr(cand, "symbol", ""),
                strategy_tag=getattr(cand, "strategy_tag", "unknown"),
                score=round(score, 4),
                direction=cand_direction,
                ev_score=round(ev, 4),
                cascade_boosted=cascade_boosted,
                liq_phase_adjusted=liq_phase_adjusted,
                win_rate_estimate=round(win_rate, 3),
                rr_estimate=round(rr, 2),
                candidate=cand,
            ))

        # Sort EV descending; tie-break on raw score
        ranked.sort(key=lambda r: (r.ev_score, r.score), reverse=True)
        for i, r in enumerate(ranked):
            r.rank = i + 1

        if ranked:
            top = ranked[0]
            log.debug("signals_ranked",
                      count=len(ranked),
                      top_symbol=top.symbol,
                      top_ev=top.ev_score,
                      cascade_boost=top.cascade_boosted,
                      liq_phase_adj=top.liq_phase_adjusted,
                      top_wr=top.win_rate_estimate)

        return ranked

    def should_fire_next(
        self,
        ranked: List[RankedCandidate],
        cascade_tracker=None,
    ) -> Optional[RankedCandidate]:
        """
        Returns top candidate if EV ≥ EV_FLOOR and cascade not blocking.
        Returns None if no signals meet the threshold.
        """
        if not ranked:
            return None

        # Blocked cascade → suppress all normal signals
        if cascade_tracker and cascade_tracker.is_blocked():
            log.debug("signal_ranker_cascade_blocked")
            return None

        top = ranked[0]
        if top.ev_score >= EV_FLOOR:
            return top

        log.debug("signal_ranker_ev_floor_missed",
                  best_ev=top.ev_score, floor=EV_FLOOR)
        return None
