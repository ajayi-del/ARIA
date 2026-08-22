"""Volatility & path-efficiency estimators — Yang-Zhang (2000), Lo-MacKinlay (1988).

Two pure estimators from the same candle data ARIA already owns:

- **yang_zhang_pct(candles)**: the YZ OHLC volatility estimator — drift-
  independent, handles open gaps (overnight/jump risk ATR systematically
  misses on 24/7 crypto session boundaries). Used by Conviction Review as the
  noise-band scale when available: band = max(floor, mult × yz_pct). Rogers-
  Satchell term uses the full OHLC path, so it prices wick noise ATR's
  close-anchored TR cannot see.

- **variance_ratio / vr_class**: Lo-MacKinlay VR(q) = Var(q-period log
  returns) / (q × Var(1-period)). VR > 1 → positively autocorrelated
  (trending — give positions room); VR < 1 → negatively autocorrelated
  (mean-reverting — recoveries come fast or not at all, so a long grace is
  wasted margin). The classifier converts the paper's statistic into the
  per-symbol grace doctrine Conviction Review consumes.

No I/O, no globals — callers inject candles.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple


def _log(x: float) -> float:
    return math.log(x) if x > 0 else 0.0


def yang_zhang_pct(candles: Sequence, period: int = 20) -> Optional[float]:
    """YZ per-bar volatility as a fraction of price. Needs period+1 candles
    (newest last). Returns None on insufficient/bad data — callers fall back
    to ATR or the flat floor."""
    try:
        cs = list(candles)[-(period + 1):]
        if len(cs) < period + 1:
            return None
        o = [float(c.open) for c in cs]
        h = [float(c.high) for c in cs]
        l = [float(c.low) for c in cs]
        c = [float(c.close) for c in cs]
        if min(o + h + l + c) <= 0:
            return None
        n = len(cs) - 1
        r_o = [_log(o[i] / c[i - 1]) for i in range(1, len(cs))]       # overnight
        r_c = [_log(c[i] / o[i]) for i in range(1, len(cs))]           # open→close
        mu_o = sum(r_o) / n
        mu_c = sum(r_c) / n
        var_o = sum((r - mu_o) ** 2 for r in r_o) / (n - 1) if n > 1 else 0.0
        var_c = sum((r - mu_c) ** 2 for r in r_c) / (n - 1) if n > 1 else 0.0
        rs = [(_log(h[i] / c[i]) * _log(h[i] / o[i])
               + _log(l[i] / c[i]) * _log(l[i] / o[i])) for i in range(1, len(cs))]
        var_rs = sum(rs) / n
        k = 0.34 / (1.34 + (n + 1) / (n - 1)) if n > 1 else 0.5
        var_yz = var_o + k * var_c + (1.0 - k) * var_rs
        return math.sqrt(var_yz) if var_yz > 0 else None
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return None


def variance_ratio(closes: Sequence[float], q: int = 8) -> Optional[float]:
    """Lo-MacKinlay VR(q) over the tail of the close series (newest last).
    Overlapping q-period log returns. None when the sample is too thin."""
    try:
        px = [float(p) for p in closes if float(p) > 0]
    except (TypeError, ValueError):
        return None
    n1 = 5 * q                       # minimum sample for a stable estimate
    if len(px) < n1 + 1:
        return None
    px = px[-(12 * q + 1):]          # tail window: recent path efficiency
    r1 = [_log(px[i] / px[i - 1]) for i in range(1, len(px))]
    n = len(r1)
    if n < n1:
        return None
    mu = sum(r1) / n
    var1 = sum((r - mu) ** 2 for r in r1) / (n - 1)
    if var1 <= 0:
        return None
    rq = [_log(px[i] / px[i - q]) for i in range(q, len(px))]
    m = len(rq)
    mu_q = sum(rq) / m
    var_q = sum((r - mu_q) ** 2 for r in rq) / (m - 1)
    return var_q / (q * var1)   # VR = 0 is valid: perfect oscillation


VR_TREND_THRESHOLD = 1.15          # VR(q) above → positively autocorrelated path
VR_MR_THRESHOLD = 0.85             # below → mean-reverting path


def vr_class(closes: Sequence[float], q: int = 8) -> Tuple[str, Optional[float]]:
    """("trend" | "mr" | "neutral", vr). "neutral" also covers thin data —
    callers must treat neutral as the pre-classifier behavior."""
    vr = variance_ratio(closes, q)
    if vr is None:
        return "neutral", None
    if vr >= VR_TREND_THRESHOLD:
        return "trend", vr
    if vr <= VR_MR_THRESHOLD:
        return "mr", vr
    return "neutral", vr
