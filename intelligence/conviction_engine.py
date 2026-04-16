"""
intelligence/conviction_engine.py — Conviction score aggregator.

Reduces all live signal evidence to a single float in [0.0, 1.0].
This is the prediction market output distilled for the Nietzsche engine.

Pure function — no state, no I/O. Latency: ~0.01ms.
"""

from __future__ import annotations


def compute_conviction(
    coherence:          float,
    max_coherence:      float = 8.0,
    regime_aligned:     bool  = True,
    order_flow_ratio:   float = 0.5,
    cascade_active:     bool  = False,
    cascade_zscore:     float = 0.0,
    historical_wr:      float = 0.50,
    kant_confidence:    float = 0.70,
) -> float:
    """
    Aggregate signal evidence into conviction score [0.0, 1.0].

    Weights:
      Coherence        40%  — primary quality signal
      Regime alignment 25%  — structural tailwind/headwind
      Order flow       20%  — real-money aggressor pressure
      Cascade boost    15%  — institutional cascade confirmation

    Historical WR adjustment: if ARIA has been winning, trust signals more.
    Kant confidence gate: if Kant is uncertain about structure, dampen.

    Latency: O(1), pure math, zero I/O.
    """
    # ── Coherence contribution (40%) ─────────────────────────────────────────
    coh_score  = min(1.0, coherence / max_coherence)
    coh_weight = 0.40

    # ── Regime alignment contribution (25%) ──────────────────────────────────
    reg_score  = 1.0 if regime_aligned else 0.3
    reg_weight = 0.25

    # ── Order flow contribution (20%) ─────────────────────────────────────────
    flow_score  = min(1.0, max(0.0, order_flow_ratio))
    flow_weight = 0.20

    # ── Cascade boost contribution (15%) ─────────────────────────────────────
    cascade_score = 0.0
    if cascade_active:
        cascade_score = min(1.0, cascade_zscore / 4.0)
    cascade_weight = 0.15

    raw = (
        coh_score    * coh_weight  +
        reg_score    * reg_weight  +
        flow_score   * flow_weight +
        cascade_score * cascade_weight
    )

    # ── Historical calibration ────────────────────────────────────────────────
    # WR 50% → neutral (+0). WR 70% → +0.04. WR 30% → -0.04.
    wr_adjust = (historical_wr - 0.50) * 0.20
    raw = max(0.0, min(1.0, raw + wr_adjust))

    # ── Kant confidence gate ──────────────────────────────────────────────────
    # If Kant is uncertain about market structure, dampen conviction.
    if kant_confidence < 0.60:
        raw *= 0.80

    return round(raw, 3)
