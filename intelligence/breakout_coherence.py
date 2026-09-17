"""Breakout Coherence scorer — 4-pillar 0-10 composite (2026-09-17).

Origin: a stale ``quiet_market_pause`` veto blocked UNI longs while UNI ran
+17.49%. External review proposed a coherence score where HIGH coherence
(>= 6.5) overrides LOW-SIGNAL vetoes (quiet market, low-winrate regime,
coherence decay). Hard risk limits are NEVER overridden — they are simply
absent from ``OVERRIDE_THRESHOLDS``.

Zero-I/O brain doctrine: every function here is pure. The wiring layer
(main.py ``on_signal_ready`` quant-filter chain) gathers inputs from the
real data planes and injects them via ``CoherenceInputs``.

PROXIED / MISSING PLANES (documented, not faked):
  * funding_avg — funding_history.json holds ~7d of hourly prints
    post-compaction (max_records=168). FUNDING_AVG_WINDOW=168 is the
    documented 7d proxy for the 30d window the doctrine asks for.
  * oi_delta_24h_pct — only SHORT-window Bybit OI deltas exist
    (bybit_ticker_stores[sym]["open_interest"]). 24h delta is the
    provider's problem; this module just consumes the float.
  * whale_ls — NO market-wide L/S plane exists. Wire the optional
    registry_net_dir float from ARIA's whale_positions/WPP registry.
    None → that sub-signal abstains (pillar continues without it).
  * narrative_score — NO NEWS FEED exists. The pillar is an interface
    only; the SoSoValue news layer is the standing candidate. Pass
    None → pillar abstains and weights renormalize.
  * rv_rank — NO options/IVR plane exists. Realized-vol rank (current
    Parkinson HV vs its own history) is the documented proxy.

UNSOURCED THRESHOLDS: all numeric constants below are STARTER thresholds
from the external review, not measured on ARIA trade history. They are
module-level constants precisely so the calibration loop can tighten them
from shadow-journal counterfactuals (event: breakout_coherence_score).

Fail-silent: helpers never raise; malformed input degrades to abstention.
Stdlib only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from intelligence import kill_switch

# ── Starter thresholds (UNSOURCED — calibrate from shadow journal) ────────────

FUNDING_AVG_WINDOW = 168          # 7d hourly — documented proxy for 30d window

# Pillar 1 — positioning structure
FUNDING_SQUEEZE_MULT = 0.7        # funding < 0.7*avg  → crowded-short squeeze
OI_SQUEEZE_PCT = 10.0             # oi_delta_24h > +10% with the squeeze
FUNDING_MOMENTUM_MULT = 1.3       # funding > 1.3*avg  → momentum longs paying
OI_MOMENTUM_PCT = 3.0
WHALE_LS_MOMENTUM_MIN = 1.2
WHALE_LS_STRONG_MIN = 1.5

# Pillar 3 — volatility regime (parkinson_hv annualized; rv_rank 0-100)
HV_COILED_MAX = 0.70
RV_COILED_MAX = 35.0
HV_MID_MAX = 0.85                 # UNSOURCED interpolation band (see vol pillar)
RV_MID_MAX = 45.0
HV_COMPRESS_MAX = 1.00
RV_COMPRESS_MAX = 55.0
HV_EXHAUSTED_MIN = 1.20
RV_EXHAUSTED_MIN = 75.0

# Pillar 4 — cross-asset
BIFURCATED_MOVERS_MIN = 3         # same-complex symbols up >= 3%
RISK_ON_MOVERS_MIN = 5
RISK_OFF_MOVERS_MAX = 2
SECTOR_MOVER_PCT = 3.0            # "up" bar for sector_movers_up counting

# Macro regime classifier (starter thresholds, UNSOURCED)
MAJORS_WEAK_PCT = -0.5            # btc/eth day move below this = majors weak
BTC_RISK_ON_PCT = 0.5
ALT_BREADTH_MIN = 5               # alts up on the day (decoupling quorum)

# Regime bands for the composite 0-10 score
REGIME_BREAKOUT_IMMINENT = 8.0
REGIME_HIGH_COHERENCE_ENTRY = 6.5
REGIME_MODERATE_WATCH = 5.0
REGIME_LOW_SIGNAL = 3.5

PILLAR_MAX = 2.5                  # each pillar tops at 2.5; 4 pillars → 10

# Veto override map. HARD RISK LIMITS ARE NEVER LISTED — absence means the
# veto can never be overridden by coherence, at any score.
OVERRIDE_THRESHOLDS: Dict[str, float] = {
    "quiet_market_pause": 6.5,
    "low_winrate_regime": 7.0,
    "coherence_decay": 7.5,
}

_PILLAR_ORDER = ("positioning", "narrative", "volatility", "cross_asset")


# ── Inputs / result ───────────────────────────────────────────────────────────

@dataclass
class CoherenceInputs:
    """Injected by the wiring layer. None on any field → that pillar (or
    sub-signal) abstains; the composite renormalizes over present pillars."""
    # Pillar 1 — positioning
    funding_rate: Optional[float] = None       # live Bybit/venue rate
    funding_avg: Optional[float] = None        # FUNDING_AVG_WINDOW proxy avg
    oi_delta_24h_pct: Optional[float] = None   # short-window OI delta, %
    whale_ls: Optional[float] = None           # WPP registry_net_dir (optional)
    # Pillar 2 — narrative (DATA PLANE UNBUILT — SoSoValue news layer is the
    # standing candidate; pass None until it exists. Do NOT fake.)
    narrative_score: Optional[float] = None    # pre-scored 0-2.5
    # Pillar 3 — volatility
    parkinson_hv: Optional[float] = None       # annualized, from klines
    rv_rank: Optional[float] = None            # 0-100 percentile (IVR proxy)
    # Pillar 4 — cross-asset
    macro_regime: Optional[str] = None         # from classify_macro_regime
    sector_movers_up: Optional[int] = None     # same-complex syms up >= 3%


@dataclass
class CoherenceResult:
    score: float                     # 0-10, renormalized over present pillars
    pillars: Dict[str, Optional[float]] = field(default_factory=dict)
    regime: str = "NOISE"
    dominant_driver: Optional[str] = None


# ── Kill-switch helpers ───────────────────────────────────────────────────────

def shadow_enabled() -> bool:
    """Measure-only shadow: compute + log, never act."""
    try:
        return kill_switch.enabled("breakout_coherence_shadow")
    except Exception:
        return False


def override_enabled() -> bool:
    """Live veto-override arm. Default OFF until shadow evidence accrues."""
    try:
        return kill_switch.enabled("coherence_veto_override")
    except Exception:
        return False


# ── Volatility helpers (pure math) ────────────────────────────────────────────

def parkinson_hv(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    periods_per_year: int = 8760,
) -> Optional[float]:
    """Annualized Parkinson (high-low) volatility.

    var = (1 / (4 ln2 * n)) * sum( ln(h_i / l_i)^2 ), annualized by
    sqrt(periods_per_year). Default 8760 = hourly klines; pass 2190 for 4h.

    ``closes`` is accepted for interface parity with the wiring layer's
    kline extraction (and length validation); the estimator itself uses
    only the high-low range. Returns None on <2 usable bars, non-positive
    prices, or length mismatch — fail-silent.
    """
    try:
        n = min(len(highs), len(lows), len(closes))
        if n < 2:
            return None
        acc = 0.0
        used = 0
        for i in range(n):
            h, l = float(highs[i]), float(lows[i])
            if h <= 0.0 or l <= 0.0 or h < l:
                continue
            r = math.log(h / l)
            acc += r * r
            used += 1
        if used < 2:
            return None
        var = acc / (4.0 * math.log(2.0) * used)
        return math.sqrt(var * periods_per_year)
    except (TypeError, ValueError, OverflowError):
        return None


def rv_rank(hv_series: Sequence[float], current_hv: Optional[float]) -> Optional[float]:
    """Percentile (0-100) of current_hv within its own history.

    ARIA has no options/IVR plane — realized-vol rank is the documented
    IVR proxy. Fraction of history <= current, scaled to 0-100. Returns
    None when history is too thin (<10 prints) or inputs are malformed.
    """
    try:
        if current_hv is None:
            return None
        cur = float(current_hv)
        series = [float(v) for v in hv_series if v is not None]
        if len(series) < 10:
            return None
        return 100.0 * sum(1 for v in series if v <= cur) / len(series)
    except (TypeError, ValueError):
        return None


# ── Macro regime classifier ───────────────────────────────────────────────────

def classify_macro_regime(
    btc_day_move_pct: Optional[float],
    eth_day_move_pct: Optional[float],
    alt_breadth_up: Optional[int],
    etf_tide: Optional[str],
) -> str:
    """Classify the tape: "RISK_ON" | "RISK_OFF" | "BIFURCATED".

    BIFURCATED = majors weak ((btc AND eth day moves < -0.5%) OR
    etf_tide == "opposed") AND alt_breadth_up >= 5 — macro risk-off with
    narrative alts decoupling (the UNI-day regime).

    RISK_ON  = btc day move > +0.5% AND breadth >= 5.
    RISK_OFF = btc < -0.5% AND breadth < 5.

    Ambiguous states resolve RISK_OFF (conservative: never grants the
    RISK_ON cross-asset bonus without evidence). Missing inputs degrade
    toward RISK_OFF — fail-safe, never fail-silent-upward.
    """
    try:
        btc = float(btc_day_move_pct) if btc_day_move_pct is not None else 0.0
        eth = float(eth_day_move_pct) if eth_day_move_pct is not None else 0.0
        breadth = int(alt_breadth_up) if alt_breadth_up is not None else 0
    except (TypeError, ValueError):
        return "RISK_OFF"

    majors_weak = (btc < MAJORS_WEAK_PCT and eth < MAJORS_WEAK_PCT) or etf_tide == "opposed"
    if majors_weak and breadth >= ALT_BREADTH_MIN:
        return "BIFURCATED"
    if btc > BTC_RISK_ON_PCT and breadth >= ALT_BREADTH_MIN:
        return "RISK_ON"
    if btc < MAJORS_WEAK_PCT and breadth < ALT_BREADTH_MIN:
        return "RISK_OFF"
    return "RISK_OFF"


# ── Pillars (each returns 0-2.5, or None to abstain) ─────────────────────────

def _pillar_positioning(inp: CoherenceInputs) -> Optional[float]:
    """Pillar 1 — positioning structure. Abstains if any of the three core
    planes (funding, funding_avg, oi_delta) is missing; whale_ls is an
    optional sub-signal (None → momentum/whale branches simply can't fire)."""
    if inp.funding_rate is None or inp.funding_avg is None or inp.oi_delta_24h_pct is None:
        return None
    try:
        fr, favg, oi = float(inp.funding_rate), float(inp.funding_avg), float(inp.oi_delta_24h_pct)
        wls = float(inp.whale_ls) if inp.whale_ls is not None else None
    except (TypeError, ValueError):
        return None
    if fr < FUNDING_SQUEEZE_MULT * favg and oi > OI_SQUEEZE_PCT:
        return 2.5                                  # squeeze_setup
    if fr > FUNDING_MOMENTUM_MULT * favg and oi > OI_MOMENTUM_PCT \
            and wls is not None and wls > WHALE_LS_MOMENTUM_MIN:
        return 2.0                                  # momentum_setup
    if wls is not None and wls > WHALE_LS_STRONG_MIN and oi > 0.0:
        return 1.5                                  # whale-led positioning
    return 0.5


def _pillar_narrative(inp: CoherenceInputs) -> Optional[float]:
    """Pillar 2 — narrative catalyst. INTERFACE ONLY: ARIA has no news feed
    (SoSoValue news layer is the standing candidate). The wiring layer passes
    a pre-scored 0-2.5 once that plane exists; None → abstain + renormalize."""
    if inp.narrative_score is None:
        return None
    try:
        return max(0.0, min(PILLAR_MAX, float(inp.narrative_score)))
    except (TypeError, ValueError):
        return None


def _pillar_volatility(inp: CoherenceInputs) -> Optional[float]:
    """Pillar 3 — volatility regime (Parkinson HV + realized-vol rank proxy).
    Includes one UNSOURCED interpolation band (hv<0.85 & rvr<45 → 2.0) so
    the coiled-but-not-extreme middle is scored; reconstructs the UNI day."""
    if inp.parkinson_hv is None or inp.rv_rank is None:
        return None
    try:
        hv, rvr = float(inp.parkinson_hv), float(inp.rv_rank)
    except (TypeError, ValueError):
        return None
    if hv < HV_COILED_MAX and rvr < RV_COILED_MAX:
        return 2.5                                  # coiled
    if hv < HV_MID_MAX and rvr < RV_MID_MAX:
        return 2.0                                  # coiling (interpolated band)
    if hv < HV_COMPRESS_MAX and rvr < RV_COMPRESS_MAX:
        return 1.8
    if hv > HV_EXHAUSTED_MIN and rvr > RV_EXHAUSTED_MIN:
        return 0.5                                  # exhausted
    return 1.2


def _pillar_cross_asset(inp: CoherenceInputs) -> Optional[float]:
    """Pillar 4 — cross-asset confirmation from macro regime + same-complex
    breadth (count of complex peers up >= 3% on the day)."""
    if inp.macro_regime is None or inp.sector_movers_up is None:
        return None
    try:
        movers = int(inp.sector_movers_up)
    except (TypeError, ValueError):
        return None
    regime = inp.macro_regime
    if regime == "BIFURCATED" and movers >= BIFURCATED_MOVERS_MIN:
        return 2.5
    if regime == "RISK_ON" and movers >= RISK_ON_MOVERS_MIN:
        return 2.0
    if regime == "RISK_OFF" and movers < RISK_OFF_MOVERS_MAX:
        return 0.5
    return 1.2


# ── Composite scorer ──────────────────────────────────────────────────────────

def _regime_band(score: float) -> str:
    if score >= REGIME_BREAKOUT_IMMINENT:
        return "BREAKOUT_IMMINENT"
    if score >= REGIME_HIGH_COHERENCE_ENTRY:
        return "HIGH_COHERENCE_ENTRY"
    if score >= REGIME_MODERATE_WATCH:
        return "MODERATE_WATCH"
    if score >= REGIME_LOW_SIGNAL:
        return "LOW_SIGNAL"
    return "NOISE"


def compute_coherence(symbol: str, inputs: CoherenceInputs) -> CoherenceResult:
    """Composite 0-10 breakout-coherence score.

    Pillars that abstain (None) drop out and the score RENORMALIZES over
    present pillars: score = 10 * sum(present) / (2.5 * n_present). All-abstain
    → 0.0 / NOISE. dominant_driver = highest-scoring present pillar (ties
    resolve to the first in canonical order). Never raises.
    """
    pillars: Dict[str, Optional[float]] = {
        "positioning": _pillar_positioning(inputs),
        "narrative": _pillar_narrative(inputs),
        "volatility": _pillar_volatility(inputs),
        "cross_asset": _pillar_cross_asset(inputs),
    }
    present = {k: v for k, v in pillars.items() if v is not None}
    if not present:
        return CoherenceResult(score=0.0, pillars=pillars, regime="NOISE",
                               dominant_driver=None)
    score = 10.0 * sum(present.values()) / (PILLAR_MAX * len(present))
    dominant = next(k for k in _PILLAR_ORDER
                    if k in present and present[k] == max(present.values()))
    return CoherenceResult(score=round(score, 4), pillars=pillars,
                           regime=_regime_band(score), dominant_driver=dominant)


# ── Veto override ─────────────────────────────────────────────────────────────

def veto_override(veto_type: Optional[str], score: Optional[float]) -> bool:
    """True when coherence >= the override threshold for a LISTED veto.

    Vetoes absent from OVERRIDE_THRESHOLDS (hard risk limits) return False
    at any score — they can never be overridden by coherence.
    """
    if veto_type is None or score is None:
        return False
    threshold = OVERRIDE_THRESHOLDS.get(str(veto_type))
    if threshold is None:
        return False
    try:
        return float(score) >= threshold
    except (TypeError, ValueError):
        return False
