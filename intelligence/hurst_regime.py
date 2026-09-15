"""intelligence/hurst_regime.py — Hurst/regime classification brain.

Governor spec 2026-09-15: "no strategy should fire until Hurst is computed" —
shipped SHADOW-from-birth (house doctrine: measurement precedes enforcement,
n>=20 per cell before any live gate). The classification plane and the shadow
scoring ship ON; live gating stays OFF (config.regime_gate_live_enabled=False
must remain False until the shadow census argues otherwise).

Estimator choice: classic Hurst (1951) rescaled range (R/S) computed directly
on the LOG-PRICE series — NOT on differenced log returns. Windows start at 16
and grow x1.5 (16, 25, 38, ...); per window, chunks slide with 50% overlap
(min 3 chunks or the window is skipped); per chunk
R = range(cumsum(x - chunk_mean)), S = population std of the chunk values.
Each window's mean R/S is bias-corrected by the Anis & Lloyd (1976) expected
R/S of an iid series (small-window bias is large at w=16 and would otherwise
inflate every slope); H is the OLS slope of log(corrected E[R/S]) vs log(w).
WHY the raw-series convention: the Governor's classification contract
requires a strongly DRIFTING random walk to read persistent (H > 0.55). An
increments-based R/S (the existing intelligence/hurst.py hurst_rs, built for
the signal-bar filter) demeans the increments and cancels drift, reading the
same series H ~= 0.5. Raw-series R/S prices trend as persistence, which is
exactly what the regime gate vets. Consequence (documented, accepted): a
DRIFTLESS random walk also reads high — this estimator separates
range-expanding paths (trend) from range-contracting paths (mean reversion)
and pure noise; it does not separate drift from integration.
Pinned in tests/test_hurst_regime.py on fixed seeds (verified stable across
seeds 1-10): strong-drift walk > 0.55, AR(1) negative-autocorr < 0.45,
iid white noise centered ~0.48 (observed band [0.41, 0.60] — the load-bearing
claim is centering near 0.5, not the band width).

Vol rank: realized_vol_rank (data/klines_4h.py) — Parkinson realized-vol
percentile 0-100, the Polemarch-stamped IV-rank substitute (ARIA has no
options feed).

Zero-I/O brain: injected data only, never raises, fail-open None / "unknown"
= dark/abstain. The single env touch is the house kill-switch idiom
(measurement_enabled), read per-call.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Optional, Sequence

from data.klines_4h import closes_of, realized_vol_rank

MIN_BARS = 100          # Governor floor: Hurst abstains below 100 4h closes
_TREND_H = 0.55         # H above = trend-persistent
_MR_H = 0.45            # H below = anti-persistent
_CHAOTIC_VOL = 75.0     # vol_rank at/above = KILL (all flat)
_CALM_VOL = 65.0        # trend/mr classes require vol_rank below this


@dataclass
class RegimeState:
    symbol: str
    regime: str                 # chaotic/trending/mean_reverting/random/unknown
    hurst: Optional[float]
    vol_rank: Optional[float]
    computed_at_ms: int
    n_bars: int


def measurement_enabled() -> bool:
    """House kill-switch idiom, read per-call: REGIME_CLASSIFY_ENABLED=false
    stands the measurement plane down (no loop writes)."""
    return os.environ.get("REGIME_CLASSIFY_ENABLED", "true").strip().lower() != "false"


_AL_CACHE: dict = {}


def _anis_lloyd(n: int) -> float:
    """Anis & Lloyd (1976) expected R/S of an iid series of length n —
    the small-window bias correction normalizing white noise to H = 0.5."""
    v = _AL_CACHE.get(n)
    if v is None:
        if n <= 1:
            v = 1.0
        else:
            s = sum(math.sqrt((n - r) / r) for r in range(1, n))
            v = ((n - 0.5) / n) * (n * math.pi / 2.0) ** -0.5 * s
        _AL_CACHE[n] = v
    return v


def _windows(n: int):
    """Window sizes: 16 then x1.5 growth; a window needs >=3 half-overlapped
    chunks inside n or the grid stops (its lone-chunk noise would dominate
    the slope)."""
    ws = []
    w = 16
    while True:
        step = max(1, w // 2)
        if len(range(0, n - w + 1, step)) < 3:
            break
        ws.append(w)
        w = int(w * 1.5) + 1
    return ws


def hurst_exponent(closes: Sequence[float], min_bars: int = MIN_BARS) -> Optional[float]:
    """Raw-series, Anis-Lloyd-corrected R/S Hurst estimate (the module
    docstring documents the convention and its accepted consequence).

    None on thin (<min_bars closes), non-positive, or degenerate input
    (zero variance everywhere, <3 regression points, out-of-range slope).
    """
    try:
        px = [float(p) for p in closes]
    except (TypeError, ValueError):
        return None
    if len(px) < max(int(min_bars), 33) or any(p <= 0 for p in px):
        return None
    logp = [math.log(p) for p in px]
    n = len(logp)
    points = []
    for w in _windows(n):
        step = max(1, w // 2)
        rs_vals = []
        for start in range(0, n - w + 1, step):
            chunk = logp[start:start + w]
            m = sum(chunk) / w
            dev = 0.0
            lo = hi = 0.0
            ss = 0.0
            for x in chunk:
                d = x - m
                dev += d
                ss += d * d
                if dev < lo:
                    lo = dev
                if dev > hi:
                    hi = dev
            s = math.sqrt(ss / w)
            # 1e-12: float dust on a constant series is not structure
            if s > 1e-12:
                rs_vals.append((hi - lo) / s)
        if rs_vals:
            avg = sum(rs_vals) / len(rs_vals)
            if avg > 0:
                corr = avg / _anis_lloyd(w) * math.sqrt(math.pi * w / 2.0)
                points.append((math.log(w), math.log(corr)))
    if len(points) < 3:
        return None
    np_ = len(points)
    mx = sum(p[0] for p in points) / np_
    my = sum(p[1] for p in points) / np_
    num = sum((p[0] - mx) * (p[1] - my) for p in points)
    den = sum((p[0] - mx) ** 2 for p in points)
    if den <= 0:
        return None
    slope = num / den
    if not (0.0 < slope < 1.5):
        return None
    return slope


def classify_regime(hurst: Optional[float], vol_rank: Optional[float]) -> str:
    """The Governor's five-regime map. None on either axis = unknown
    (fail-open abstain — never blocks)."""
    if hurst is None or vol_rank is None:
        return "unknown"
    try:
        h = float(hurst)
        v = float(vol_rank)
    except (TypeError, ValueError):
        return "unknown"
    if v >= _CHAOTIC_VOL:
        return "chaotic"
    if v < _CALM_VOL and h > _TREND_H:
        return "trending"
    if v < _CALM_VOL and h < _MR_H:
        return "mean_reverting"
    return "random"


# The Governor's eligibility matrix (shadow-scored, NOT enforced).
_FIRE_MATRIX = {
    "trending":       {"momentum": True,  "swing": True,  "meanrev": False, "carry": True},
    "mean_reverting": {"momentum": False, "swing": False, "meanrev": True,  "carry": True},
    "random":         {"momentum": False, "swing": False, "meanrev": False, "carry": True},
    "chaotic":        {"momentum": False, "swing": False, "meanrev": False, "carry": False},
    "unknown":        {"momentum": True,  "swing": True,  "meanrev": True,  "carry": True},
}


def should_fire(regime: Optional[str], strategy_class: Optional[str]) -> bool:
    """True = eligible. Unknown regime or unknown strategy class = fail-open
    abstain (True) — the shadow gate only ever VETOES on a positive read."""
    row = _FIRE_MATRIX.get(str(regime or "unknown"))
    if row is None:
        return True
    return row.get(str(strategy_class or ""), True)


# ARIA's personality/strategy-tag vocabulary -> the Governor's four classes.
# Conservative: only confident mappings are listed; everything else abstains.
_STRATEGY_CLASS = {
    # momentum / cascade
    "apex": "momentum", "cascade_momentum": "momentum", "mag_lead": "momentum",
    "trend_macro": "momentum", "score_macro": "momentum",
    "regime_struct": "momentum", "ob_imbalance": "momentum",
    "sc23_breakout": "momentum", "explosive": "momentum",
    # mean-reversion / aftermath
    "aftermath": "meanrev", "cascade_aftermath": "meanrev",
    "cascade_fade": "meanrev", "sweep_reversal": "meanrev",
    "divergence": "meanrev", "convergence": "meanrev",
    "s4_cascade_fade": "meanrev", "sc1_eth_bb_bounce": "meanrev",
    "pair_meanrev": "meanrev",
    # carry / funding
    "funding_fade": "carry", "carry": "carry", "funding": "carry",
    "stock_carry": "carry",
    # swing
    "swing": "swing", "aster_swing": "swing", "s1_oi_pullback": "swing",
}


def strategy_class_of(tag: str = "", personality: str = "") -> Optional[str]:
    """Map the candidate's strategy tag / personality to
    {momentum, meanrev, carry, swing}. None = unmapped (abstain)."""
    for raw in (tag, personality):
        key = str(raw or "").strip().lower()
        if key and key in _STRATEGY_CLASS:
            return _STRATEGY_CLASS[key]
    return None


def compute_state(symbol: str, candles, now_ms: int,
                  min_bars: int = MIN_BARS) -> RegimeState:
    """Assemble hurst + vol_rank + classify into a RegimeState. Thin or dark
    input -> regime "unknown" (fail-open); never raises."""
    try:
        bars = list(candles or [])
        h = hurst_exponent(closes_of(bars), min_bars=min_bars)
        vr = realized_vol_rank(bars)
        return RegimeState(symbol=str(symbol), regime=classify_regime(h, vr),
                           hurst=h, vol_rank=vr,
                           computed_at_ms=int(now_ms), n_bars=len(bars))
    except Exception:
        return RegimeState(symbol=str(symbol or ""), regime="unknown",
                           hurst=None, vol_rank=None,
                           computed_at_ms=int(now_ms or 0), n_bars=0)
