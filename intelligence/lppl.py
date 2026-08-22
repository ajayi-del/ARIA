"""LPPL dragon-king precursor — Sornette, Johansen & Bouchaud (1996).

A super-exponential run-up decorated by accelerating log-periodic oscillations
is the signature of a bubble approaching its critical time tc — the "dragon-
king" regime where the break is endogenous, not exogenous. The model:

    ln p(t) = A + B·(tc−t)^m + (tc−t)^m · [C1·cos(ω ln(tc−t)) + C2·sin(ω ln(tc−t))]

Fitted by the standard slaving trick: grid the three nonlinear parameters
(tc, m, ω); conditioned on them the model is LINEAR in (A, B, C1, C2) — one
least-squares solve per grid point, keep the best R². A fit only counts as a
bubble signature when the Sornette filter conditions hold:

  - B < 0           (price accelerates UP into tc for 0 < m < 1)
  - 0.1 ≤ m ≤ 0.9   (sub/super-linear singularity, not linear drift)
  - 6 ≤ ω ≤ 13      (empirical log-periodic band across asset classes)
  - damping = m·|B| / (ω·√(C1²+C2²)) ≥ 1  (oscillations don't explode)

Polymathic use in ARIA: the Dreamer's four precursors read the SPRING
(compression, OI loading, funding, volume). LPPL reads the WAVE building
inside the spring — a compressed market that is ALSO tracing a log-periodic
run-up is a dragon-king candidate, so readiness gets an additive boost
(NOT a fifth precursor: a 5th gate would dilute 3/4 scores and starve the
pilot). Kill switch: LPPL_ENABLED=false → readiness identical to pre-module.

Pure module: callers inject aligned close arrays (any cadence, same clock).
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np

_TC_GRID = np.linspace(1.02, 1.30, 8)        # tc as a multiple of window length T
_M_GRID = np.linspace(0.15, 0.85, 8)
_W_GRID = np.linspace(6.0, 13.0, 8)
_MIN_FIT_R2 = 0.5                            # below this the wave isn't there
_DAMPING_MIN = 1.0                           # Sornette damping condition


def lppl_confidence(closes: Sequence[float], window: int = 120) -> Optional[float]:
    """Dragon-king confidence 0..1 over the tail `window` closes (newest last).

    0 = no LPPL signature (or a fit that fails the filter conditions);
    higher = cleaner super-exponential log-periodic run-up. None on thin or
    bad data — callers treat None as 0."""
    try:
        px = np.asarray([float(p) for p in closes][-window:], dtype=float)
        if len(px) < 60 or (px <= 0).any():
            return None
        y = np.log(px)
        T = len(y)
        t = np.arange(1, T + 1, dtype=float)
        sse0 = float(np.sum((y - y.mean()) ** 2))
        if sse0 <= 0:
            return None

        best_r2 = 0.0
        for tc_mult in _TC_GRID:
            tc = tc_mult * T
            dt = tc - t                                # > 0 everywhere (tc > T)
            ln_dt = np.log(dt)
            for m in _M_GRID:
                dt_m = dt ** m
                for w in _W_GRID:
                    X = np.column_stack([
                        np.ones(T),
                        dt_m,
                        dt_m * np.cos(w * ln_dt),
                        dt_m * np.sin(w * ln_dt),
                    ])
                    beta, res, *_ = np.linalg.lstsq(X, y, rcond=None)
                    A, B, C1, C2 = (float(b) for b in beta)
                    if B >= 0:
                        continue                       # not an accelerating run-up
                    fit = X @ beta
                    sse = float(np.sum((y - fit) ** 2))
                    r2 = 1.0 - sse / sse0
                    if r2 < _MIN_FIT_R2:
                        continue
                    osc_amp = math.hypot(C1, C2)
                    damping = (m * abs(B)) / (w * osc_amp) if osc_amp > 0 else 0.0
                    if damping < _DAMPING_MIN:
                        continue
                    best_r2 = max(best_r2, r2)
        return float(best_r2)
    except (ValueError, TypeError, FloatingPointError, np.linalg.LinAlgError):
        return None
