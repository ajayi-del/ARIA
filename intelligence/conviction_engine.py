"""
intelligence/conviction_engine.py — Conviction score aggregator.

Reduces all live signal evidence to a single float in [0.0, 1.0].
This is the prediction market output distilled for the Nietzsche engine.

Pure function — no state, no I/O. Latency: ~0.01ms.
"""

from __future__ import annotations


def compute_conviction(
    coherence:          float,
    max_coherence:      float = 9.0,
    regime_aligned:     bool  = True,
    order_flow_ratio:   float = 0.5,
    cascade_active:     bool  = False,
    cascade_zscore:     float = 0.0,
    historical_wr:      float = 0.50,
    kant_confidence:    float = 0.70,
    agent_alignment:    float = 0.5,  # [0,1] — 0.5=neutral, 1.0=all agents agree, 0.0=all oppose
) -> float:
    """
    Aggregate signal evidence into conviction score [0.0, 1.0].

    Weights:
      Coherence        40%  — primary quality signal
      Regime alignment 25%  — structural tailwind/headwind
      Order flow       20%  — real-money aggressor pressure
      Cascade boost    15%  — institutional cascade confirmation

    Adjustments (additive on top of weighted base):
      Historical WR: ±0.04 — recent performance calibrates trust
      Kant confidence: ×0.80 gate when structure is ambiguous
      Agent alignment: ±0.05 — macro/micro/structure/funding/ssi consensus

    Agent alignment 0.5 = neutral (no agent data). >0.5 = agents corroborate.
    <0.5 = agents contradict (e.g. macro bearish but signal is long).

    Latency: O(1), pure math, zero I/O.

    Weights sum to 1.0: 0.40 + 0.25 + 0.20 + 0.15 = 1.00
    Agent adjustment is additive: ±0.10 max (6 agents × 60-80% accuracy ensemble).
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

    # ── Agent alignment ────────────────────────────────────────────────────────
    # 6 independent agents (macro/regime/micro/structure/funding/ssi) each vote.
    # Full consensus adds +0.10; full opposition subtracts -0.10.
    # 0.5 = neutral (no data or split) — no effect.
    # Multiplier 0.20 gives ±0.10 range — commensurate with 6 agents at 60-80% accuracy.
    agent_adjust = (agent_alignment - 0.5) * 0.20
    raw = max(0.0, min(1.0, raw + agent_adjust))

    return round(raw, 3)
