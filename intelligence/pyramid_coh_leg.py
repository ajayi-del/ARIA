"""Pyramid coh warmup-leg repair (2026-09-22, Governor-commissioned).

WHY: the pyramid add path gates on warmup_frac >= 0.80 over three
venue-scoped legs (fr, rvr, coh) — main.py ``_pyramid_warmup_legs``.
Live telemetry since the 6fa9ba1 venue-scope repair shows coh_ok=false
on EVERY pyramid_add_blocked event. Two stacked defects, both proven
from the code:

  1. The pyramid loop's ``compute_coherence`` call site passes
     ``movers_3pct=None`` — a field that does NOT exist on
     ``breakout_coherence.CoherenceInputs`` (the real field is
     ``sector_movers_up``). Dataclass construction raises TypeError,
     swallowed by the site's bare ``except Exception: pass``, so
     ``_coh`` is None on every tick for every symbol — even a fully-lit
     input set would die at the constructor.
  2. Even with the kwarg fixed, the site injects a near-all-None
     CoherenceInputs: oi_delta_24h_pct is None for SoDEX symbols (no
     venue OI plane), whale_ls/narrative/macro/movers are hardwired
     None, and rv_rank is starved by the thin ``_BC_HV_HIST`` ring
     (needs >=10 prints at 900s cadence ~ 2.5h; the rvr warmup leg is a
     SIBLING repair, not this module's). All four pillars abstain, the
     composite renormalizes over zero present pillars, and the site's
     pillar-presence check keeps ``_coh`` None.

REPAIR DOCTRINE: assemble CoherenceInputs from the planes the pyramid
loop ALREADY has (venue funding + 7d avg, Parkinson HV, ring rv_rank)
PLUS the measured-state cache (``_measured_state_cache`` — the last real
MarketState per symbol, stamped by the standard signal path, SCH-3
2026-09-09). A fresh cached MarketState carries ``volatility_percentile``
(0-1, current ATR vs its own baseline, written by the interpreter) —
that IS a realized-vol rank, the exact quantity breakout_coherence's
docstring names as the documented IVR proxy. It fills rv_rank ONLY when
the primary ring is starved; it never overrides a live ring read.

HONESTY LAW: this module lights the leg when real planes exist and
stays dark (None) when they do not. It never fabricates a pillar:
positioning still abstains without OI, narrative/cross-asset stay None
until their planes exist, a stale or missing measured state abstains
(fail-closed on unknown age), and an out-of-range percentile abstains.

Zero-I/O department template: no network/file/logging imports; every
external value injected; module-level knobs are STARTER thresholds
(UNSOURCED — the calibration loop tightens them from
pyramid_add_blocked / breakout_coherence_score telemetry). Fail-silent:
helpers never raise; malformed input degrades to abstention.
"""
from __future__ import annotations

from typing import Optional

from intelligence import breakout_coherence as _bc

# ── Starter knobs (UNSOURCED — calibrate from shadow telemetry) ──────────────

MEASURED_STATE_MAX_AGE_S = 1800.0  # cached MarketState staleness ceiling
COH_LEG_MIN_SCORE = 5.0            # mirrors config.pyramid_add_coherence_min


# ── Measured-state fallback (the SCH-3 cache reader) ─────────────────────────

def measured_rv_rank(
    measured_state,
    age_s: Optional[float],
    max_age_s: float = MEASURED_STATE_MAX_AGE_S,
) -> Optional[float]:
    """Realized-vol rank (0-100) from a cached MarketState's
    ``volatility_percentile`` (0-1, ATR vs own baseline — the interpreter's
    realized-vol rank, the documented IVR-proxy class).

    Fail-closed: unknown/negative/over-age age, missing attribute, or an
    out-of-[0,1] percentile all abstain (None). Boundary ages exactly at
    ``max_age_s`` and percentiles exactly 0.0 / 1.0 are valid (inclusive,
    mirroring the S4 boundary doctrine). Never raises.
    """
    try:
        if measured_state is None or age_s is None:
            return None
        age = float(age_s)
        if age < 0.0 or age > float(max_age_s):
            return None
        pct = float(getattr(measured_state, "volatility_percentile", None))
        if pct < 0.0 or pct > 1.0:
            return None
        return pct * 100.0
    except (TypeError, ValueError):
        return None


# ── CoherenceInputs assembly ──────────────────────────────────────────────────

def build_inputs(
    symbol: str,
    *,
    funding_rate: Optional[float] = None,
    funding_avg: Optional[float] = None,
    oi_delta_24h_pct: Optional[float] = None,
    parkinson_hv: Optional[float] = None,
    rv_rank: Optional[float] = None,
    measured_state=None,
    measured_state_age_s: Optional[float] = None,
) -> "_bc.CoherenceInputs":
    """Assemble a CoherenceInputs from real planes only.

    Pass-through for every plane the loop already has; the single repair
    leg is the rv_rank fallback to the measured-state cache when (and only
    when) the primary ``_BC_HV_HIST`` ring read is None. whale_ls /
    narrative_score / macro_regime / sector_movers_up are NOT injected —
    their data planes do not exist and this module does not fake them
    (breakout_coherence renormalizes over present pillars by design).

    The field names here are the REAL CoherenceInputs fields — this
    constructor path also retires the call-site ``movers_3pct`` TypeError.
    """
    rvr = rv_rank
    if rvr is None:
        rvr = measured_rv_rank(measured_state, measured_state_age_s)
    return _bc.CoherenceInputs(
        funding_rate=funding_rate,
        funding_avg=funding_avg,
        oi_delta_24h_pct=oi_delta_24h_pct,
        whale_ls=None,
        narrative_score=None,
        parkinson_hv=parkinson_hv,
        rv_rank=rvr,
        macro_regime=None,
        sector_movers_up=None,
    )


# ── Leg score + verdict ───────────────────────────────────────────────────────

def coh_leg_score(symbol: str, inputs: "_bc.CoherenceInputs") -> Optional[float]:
    """The ``coh`` leg value for ``_pyramid_warmup_legs``.

    float(score) when at least one pillar lit (honest renormalized
    composite); None when every pillar abstained — the leg stays dark and
    the warmup counter reads it as missing. Never raises.
    """
    try:
        res = _bc.compute_coherence(symbol, inputs)
        if not any(v is not None for v in res.pillars.values()):
            return None
        return float(res.score)
    except Exception:
        return None


def coh_leg_verdict(
    coh: Optional[float],
    min_score: float = COH_LEG_MIN_SCORE,
) -> str:
    """pass/fail/dark semantics for the flight recorder.

    "dark"  — no real plane lit (coh is None); the warmup leg reads missing.
    "fail"  — lit but below the pyramid add threshold (matches
              ``add_verdict``'s strict ``coherence < min`` reject).
    "pass"  — lit at or above the threshold (inclusive boundary).

    NOTE: ``_pyramid_warmup_legs`` itself only tests ``coh is not None`` —
    dark vs lit is what moves warmup_frac; pass/fail is recorder semantics
    for the downstream add_verdict coherence gate.
    """
    if coh is None:
        return "dark"
    try:
        return "pass" if float(coh) >= float(min_score) else "fail"
    except (TypeError, ValueError):
        return "dark"
