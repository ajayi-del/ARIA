"""
intelligence/signal_tier.py — Signal Tier Classifier
ARIA Execution Alpha Patch — Component 1

Quantitative edge estimation. Tiers every signal by expected win rate.
Called before execution to skip C-tier trades and size S/A/B correctly.

Tiers:
  S — top 5% expected edge  (cascade aftermath on liquid asset)
  A — top 20%               (strong coherence + confirmed macro + optimal session)
  B — top 60%               (baseline signal, all gates passed)
  C — bottom 20%            (passes minimums but has warning flags — SKIP)
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, Optional
import structlog

log = structlog.get_logger(__name__)


class SignalTier(Enum):
    S = "s_tier"
    A = "a_tier"
    B = "b_tier"
    C = "c_tier"   # no trade


TIER_SIZE_MULT: Dict[SignalTier, float] = {
    SignalTier.S: 2.0,
    SignalTier.A: 1.3,
    SignalTier.B: 0.8,
    SignalTier.C: 0.0,
}

TIER_TP_STYLE: Dict[SignalTier, Optional[str]] = {
    SignalTier.S: "wide_runner",   # 34% runner allocation
    SignalTier.A: "standard",
    SignalTier.B: "tight_fast",    # 50% off at TP1
    SignalTier.C: None,
}


def classify_signal(
    coherence:           float,
    cascade_zscore:      float,
    agg_ratio:           float,    # 0–1: fraction buys vs total; extremes = edge
    regime_confidence:   float,
    hist_wr:             float,    # historical WR this symbol (0–1); 0 = unknown
    macro_confirmation:  float,    # macro_engine.state.macro_confirmation_score (0–0.9)
    session:             str,      # "asian"|"london"|"overlap"|"us"
    regime_bypass_elite: bool = False,  # True = elite signal bypassed regime suppression
) -> SignalTier:
    """
    Composite edge score → tier.
    All inputs documented above; defaults are neutral (0.5 agg_ratio, 0.5 hist_wr).
    """
    edge = 0.0

    # ── Coherence component (0–3) ─────────────────────────────────────────────
    edge += min(coherence / 3.0, 3.0)

    # ── Cascade strength (0–2) ────────────────────────────────────────────────
    if cascade_zscore > 3.0:
        edge += 2.0
    elif cascade_zscore > 2.0:
        edge += 1.0

    # ── Flow extreme (0–1.5): one-sided flow = edge ───────────────────────────
    if agg_ratio < 0.10 or agg_ratio > 0.90:
        edge += 1.5
    elif agg_ratio < 0.20 or agg_ratio > 0.80:
        edge += 0.8

    # ── Regime alignment (0–1) ───────────────────────────────────────────────
    edge += min(regime_confidence, 1.0)

    # ── Historical win rate (−1 to +1.5) ────────────────────────────────────
    if hist_wr > 0.60:
        edge += 1.5
    elif hist_wr > 0.50:
        edge += 0.8
    elif 0 < hist_wr < 0.35:
        edge -= 1.0   # known poor edge

    # ── Macro confirmation (0–1) ─────────────────────────────────────────────
    edge += min(macro_confirmation, 1.0)

    # ── Session quality ───────────────────────────────────────────────────────
    if session == "us":
        edge += 0.5
    elif session == "overlap":
        edge += 0.3
    elif session == "asian":
        edge -= 0.5   # thin liquidity

    # ── Elite bypass bonus ────────────────────────────────────────────────────
    if regime_bypass_elite:
        edge += 1.5   # signal beat regime suppression = stronger than average

    # ── Classify ─────────────────────────────────────────────────────────────
    if edge >= 7.0:
        tier = SignalTier.S
    elif edge >= 5.0:
        tier = SignalTier.A
    elif edge >= 2.5:
        tier = SignalTier.B
    else:
        tier = SignalTier.C

    return tier
