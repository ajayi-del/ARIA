"""Whale Evidence Layer — SignalEvidence + calibrated EV primitives.

2026-08-30 spec audit amendments baked in:
  - Evidence is NOT a multiplier. The pipeline is
    evidence → estimated conditional distribution of outcomes → expected
    value → risk-adjusted size. Until the distribution is learned from the
    shadow journal, every estimator here returns None and consumers abstain
    (shadow-only). No fake precision before data (Aronson).
  - Confidence is FOUR-DIMENSIONAL (signal / edge / execution / regime),
    combined by GEOMETRIC mean — one zero leg zeroes the whole (a great
    signal with no measured edge is not a trade).
  - Breadth is EFFECTIVE breadth, not wallet count: correlated whales are
    one risk factor. Leviathan cap 40% (no single wallet dominates the
    weight pool); venue-cluster deflation sqrt(n) (same-venue same-direction
    whales share flow, funding and liquidation physics).
  - EV estimator: k=20 empirical-Bayes shrinkage toward prior 0R (same
    doctrine as the Skeptic base rate). n=0 → None (abstain).

Zero-I/O brain: pure functions + a dataclass. Clocks injected where needed.
"""
import math
from dataclasses import dataclass, field, asdict


# ── SignalEvidence ────────────────────────────────────────────────────────

@dataclass
class SignalEvidence:
    """One evidence packet handed from a detector (TAC/WAS/WPP) to the
    sizing layer. Pre-data fields stay None — a None is an honest abstain,
    not a zero."""
    event_type: str                 # "tide_consensus" | "whale_absorption" | ...
    symbol: str
    direction: str                  # "long" | "short"
    p_win: float | None = None      # learned — None until shadow n accrues
    expected_R: float | None = None  # learned
    mae_p95: float | None = None    # learned — stop placement input
    mfe_median: float | None = None  # learned — target placement input
    confidence: float | None = None  # geometric mean of the four legs
    freshness_s: float | None = None
    effective_breadth: float = 0.0
    regime_compatibility: float | None = None  # 0..1, None = unmeasured
    liquidity_score: float | None = None       # 0..1, None = unmeasured
    sample_size: int = 0
    features: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ── Four-confidence geometric mean ────────────────────────────────────────

def combined_confidence(signal: float | None, edge: float | None,
                        execution: float | None,
                        regime: float | None) -> float | None:
    """Geometric mean of the confidence legs present. Any leg exactly 0
    zeroes the product (a great signal with zero measured edge confidence
    is not a trade). All-None → None (abstain, never invent 1.0)."""
    legs = [v for v in (signal, edge, execution, regime) if v is not None]
    if not legs:
        return None
    if any(v <= 0 for v in legs):
        return 0.0
    return math.exp(sum(math.log(min(v, 1.0)) for v in legs) / len(legs))


# ── Effective breadth ─────────────────────────────────────────────────────

def effective_breadth(flows: list, leviathan_cap_frac: float = 0.40) -> float:
    """Independent-bet equivalents of a whale flow set.

    flows: list of dicts with venue + optional capital_usd (None → weight 1).
    Step 1 — leviathan cap: no single wallet may carry more than
    `leviathan_cap_frac` of the total weight pool.
    Step 2 — within each venue cluster, inverse-Herfindahl breadth
    b = (Σw)² / Σw² (a dominant wallet deflates the cluster toward 1).
    Step 3 — correlation deflation: a venue cluster of effective breadth b
    contributes √b independent bets (same venue = shared funding, shared
    liquidation physics, often shared signal source). Clusters sum.
    One average-weight whale alone = 1.0; n equal whales on one venue =
    √n; n equal whales on n venues = n.
    """
    if not flows:
        return 0.0
    weights = []
    for f in flows:
        w = f.get("capital_usd") if isinstance(f, dict) else None
        try:
            w = float(w)
            if w != w or w <= 0:  # NaN / nonpositive guard
                w = 1.0
        except (TypeError, ValueError):
            w = 1.0
        weights.append(w)
    total = sum(weights)
    cap = leviathan_cap_frac * total
    weights = [min(w, cap) for w in weights]
    clusters: dict = {}
    for f, w in zip(flows, weights):
        venue = (f.get("venue") if isinstance(f, dict) else None) or "unknown"
        clusters.setdefault(venue, []).append(w)
    eff = 0.0
    for ws in clusters.values():
        s1 = sum(ws)
        s2 = sum(w * w for w in ws)
        if s2 <= 0:
            continue
        eff += math.sqrt((s1 * s1) / s2)
    return eff


# ── EV-from-samples (k=20 shrinkage) ─────────────────────────────────────

def ev_from_samples(outcomes_R: list, k: float = 20.0,
                    prior_R: float = 0.0) -> dict | None:
    """Shrunk expectancy estimate from realized R multiples.

    None before ANY data — the abstain is the answer. p_win shrunk toward
    0.5 with the same k. lower_ci_95 = shrunk mean − 1.96·SE (normal
    approx; None when n < 2 — the graduation gate requires CI > 0, so a
    thin sample can never graduate)."""
    if not outcomes_R:
        return None
    rs = [float(r) for r in outcomes_R]
    n = len(rs)
    mean = sum(rs) / n
    ev = (n * mean + k * prior_R) / (n + k)
    wins = sum(1 for r in rs if r > 0)
    p_win = (wins + k * 0.5) / (n + k)
    se = None
    lower = None
    if n >= 2:
        var = sum((r - mean) ** 2 for r in rs) / (n - 1)
        se = math.sqrt(var / n)
        lower = ev - 1.96 * se
    return {"n": n, "mean_R": round(mean, 4), "ev_R": round(ev, 4),
            "p_win": round(p_win, 4), "se_R": round(se, 4) if se else None,
            "lower_ci_95": round(lower, 4) if lower is not None else None}
