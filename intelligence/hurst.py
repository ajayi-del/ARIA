"""Hurst persistence estimators — rescaled range (R/S) + DFA cross-check.

Governor doctrine 2026-09-15 ("signal bar"): "Hurst says the asset is
tradeable" is one of eight filters a real signal must clear. This module is
the measurement plane for that filter — it answers, per asset, is this
series persistent enough to trend-trade, mean-reverting enough to fade, or
a random walk to refuse?

- **hurst_rs(closes)**: classic Hurst (1951) rescaled-range estimate on
  log-price differences. E[R/S] ~ c * w^H over power-of-two subwindows;
  H is the OLS slope of log(E[R/S]) vs log(w).

- **hurst_dfa(closes)**: Peng (1994) detrended fluctuation analysis,
  order-1, on the integrated log-return profile. Independent estimator of
  the same exponent — disagreement between R/S and DFA is itself a signal
  (estimator uncertainty), and classify() widens its dead zone for it.

- **classify(h, h2, ci)**: H > 0.55 trend-persistent ("trendable"),
  H < 0.45 anti-persistent ("mean_reverting"), else "random_walk". The
  dead zone WIDENS to "unknown" under estimator disagreement or a CI that
  straddles a band edge — Aronson: state uncertainty, never fake precision.
  A 0.53 ± 0.06 is "unknown", not "trendable".

REGIME-AWARENESS: the Hurst exponent is REGIME-CONDITIONAL, not an asset
property. Persistence drifts with vol regime, liquidity, and session
structure. Every consumer must treat the output as the CURRENT rolling
estimate over a stated window — never a permanent label for the symbol.

No I/O, no globals, stdlib only — callers inject closes (newest last).
"""
from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple


def _log_returns(closes: Sequence[float]) -> Optional[list]:
    """Log differences of a positive close series. None on bad input."""
    try:
        px = [float(p) for p in closes]
    except (TypeError, ValueError):
        return None
    if len(px) < 3 or any(p <= 0 for p in px):
        return None
    return [math.log(px[i] / px[i - 1]) for i in range(1, len(px))]


def _ols_slope(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 0:
        return None
    return num / den


def _pow2_windows(n: int, min_w: int = 16) -> list:
    """Power-of-two window sizes with at least 2 full chunks in n points."""
    ws = []
    w = min_w
    while 2 * w <= n:
        ws.append(w)
        w *= 2
    return ws


def hurst_rs(closes: Sequence[float], min_window: int = 100) -> Optional[float]:
    """Rescaled-range Hurst estimate over power-of-two subwindows.

    Splits the log-return series into non-overlapping chunks of size w
    (w = 16, 32, ... while >=2 chunks fit), computes R/S per chunk
    (population std, Hurst's convention), averages, and regresses
    log(E[R/S]) on log(w). Returns None on degenerate input: fewer than
    min_window returns, zero variance, or fewer than 3 regression points.
    """
    r = _log_returns(closes)
    if r is None or len(r) < min_window:
        return None
    n = len(r)
    points = []
    for w in _pow2_windows(n):
        # Anchor the partition at the NEWEST tail: the n % w remainder that
        # cannot fill a chunk is dropped from the OLDEST end. Head-anchored
        # chunking would exclude up to w-1 of the most recent returns at the
        # largest window — backwards for a regime-conditional estimator whose
        # entire purpose is the CURRENT window.
        off = n % w
        rs_vals = []
        for start in range(off, n - w + 1, w):
            chunk = r[start:start + w]
            mu = sum(chunk) / w
            dev = 0.0
            lo = hi = 0.0
            ss = 0.0
            for x in chunk:
                d = x - mu
                dev += d
                ss += d * d
                if dev < lo:
                    lo = dev
                if dev > hi:
                    hi = dev
            s = math.sqrt(ss / w)
            if s > 0:
                rs_vals.append((hi - lo) / s)
        if rs_vals:
            avg = sum(rs_vals) / len(rs_vals)
            if avg > 0:
                points.append((math.log(w), math.log(avg)))
    if len(points) < 3:
        return None
    slope = _ols_slope([p[0] for p in points], [p[1] for p in points])
    return slope


def hurst_dfa(closes: Sequence[float], min_window: int = 200) -> Optional[float]:
    """Detrended fluctuation analysis (order-1) Hurst estimate.

    Integrates the demeaned log returns into a profile, then for each
    power-of-two window w fits a per-chunk linear trend and measures the
    RMS of the detrended residuals F(w). H is the OLS slope of log F(w)
    vs log(w). Input is the increment series, so alpha ~= H directly
    (fGn convention — a random walk's white increments give alpha ~= 0.5).
    Returns None on degenerate input (<min_window returns, zero variance,
    or fewer than 3 regression points).
    """
    r = _log_returns(closes)
    if r is None or len(r) < min_window:
        return None
    n = len(r)
    mu = sum(r) / n
    profile = []
    acc = 0.0
    for x in r:
        acc += x - mu
        profile.append(acc)
    points = []
    for w in _pow2_windows(n):
        off = n % w  # newest-tail anchored, same doctrine as hurst_rs
        ss_total = 0.0
        cnt = 0
        for start in range(off, n - w + 1, w):
            seg = profile[start:start + w]
            # closed-form linear fit y = a + b*i over the chunk
            sx = w * (w - 1) / 2.0
            sxx = (w - 1) * w * (2 * w - 1) / 6.0
            sy = sum(seg)
            sxy = sum(i * y for i, y in enumerate(seg))
            den = w * sxx - sx * sx
            if den <= 0:
                continue
            b = (w * sxy - sx * sy) / den
            a = (sy - b * sx) / w
            for i, y in enumerate(seg):
                res = y - (a + b * i)
                ss_total += res * res
                cnt += 1
        if cnt > 0:
            f = math.sqrt(ss_total / cnt)
            if f > 0:
                points.append((math.log(w), math.log(f)))
    if len(points) < 3:
        return None
    return _ols_slope([p[0] for p in points], [p[1] for p in points])


# Classification bands (Governor "signal bar" doctrine).
_DEAD_HALF = 0.05          # base dead zone: H in [0.45, 0.55] = random walk
_DISAGREE_FREE = 0.05      # estimator disagreement below this adds no widening
_CI_STRADDLE = True        # CI straddling a band edge = unknown (Aronson)
_CI_MAX_WIDTH = 0.20       # CI wider than this = unknown outright


def classify(h: Optional[float], h2: Optional[float] = None,
             ci: Optional[Tuple[float, float]] = None) -> dict:
    """Classify a Hurst estimate into the signal-bar persistence classes.

    h   — primary estimate (hurst_rs). None → unknown.
    h2  — optional cross-check estimate (hurst_dfa). |h - h2| beyond 0.05
          widens the dead zone by the excess — estimator disagreement is
          state uncertainty, and the widened region classifies "unknown",
          never "random_walk".
    ci  — optional (lo, hi) confidence interval around h. A CI straddling
          either band edge, or wider than 0.20, classifies "unknown"
          (a 0.53 ± 0.06 is unknown, not trendable).

    Regime-conditional: the class describes the CURRENT window only.
    """
    if h is None or not (0.0 < h < 1.5):
        return {"class": "unknown", "confidence": 0.0}
    dead = _DEAD_HALF
    disagreement = 0.0
    if h2 is not None and 0.0 < h2 < 1.5:
        disagreement = abs(h - h2)
        dead += max(0.0, disagreement - _DISAGREE_FREE)
    zone = h - 0.5
    if ci is not None:
        try:
            lo, hi = float(ci[0]), float(ci[1])
        except (TypeError, ValueError, IndexError):
            lo, hi = h, h
        if hi - lo > _CI_MAX_WIDTH:
            return {"class": "unknown", "confidence": 0.2}
        if _CI_STRADDLE and (lo < 0.5 + dead < hi or lo < 0.5 - dead < hi):
            return {"class": "unknown", "confidence": 0.2}
    if zone > dead:
        conf = 0.3 + 0.7 * min(1.0, (zone - dead) / 0.15)
        conf *= max(0.4, 1.0 - disagreement)
        return {"class": "trendable", "confidence": round(conf, 3)}
    if zone < -dead:
        conf = 0.3 + 0.7 * min(1.0, (-zone - dead) / 0.15)
        conf *= max(0.4, 1.0 - disagreement)
        return {"class": "mean_reverting", "confidence": round(conf, 3)}
    if dead > _DEAD_HALF:
        # Inside the WIDENED dead zone — uncertainty, not evidence of a walk.
        return {"class": "unknown", "confidence": 0.2}
    conf = 0.3 + 0.5 * (1.0 - abs(zone) / _DEAD_HALF)
    return {"class": "random_walk", "confidence": round(conf, 3)}
