"""
intelligence/exec_formulas.py — canon execution-formula measurement plane
(Governor directive 2026-09-11: "tune this live" = the INSTRUMENTS go live
now as shadow telemetry; no live gate/sizing changes until >=200
counterfactuals + Bonferroni alpha=0.01 screen). SHADOW-ONLY: this module
measures, never decides — zero trade-path wiring.

Brain: zero-I/O, pure — callers inject OHLCV bars (1m candle closes/
volumes), inventory, and vol estimates. All estimators are rolling-window
and fail-open: None on insufficient or degenerate input, never raise on
bad input.

Estimators:
    kyle_lambda        — price impact per dollar of signed flow
    amihud_illiq       — |return| per dollar volume (illiquidity)
    corwin_schultz_spread — high-low 2-period spread estimator (fraction)
    realized_skew      — sample skewness of 1m log returns
    avellaneda_stoikov_reservation — inventory-skewed reservation price

Canon: Kyle (1985 — lambda as the price of informed flow), Amihud (2002 —
illiquidity as return-per-dollar), Corwin & Schultz (2012 — spread from
high/low ratios), Avellaneda & Stoikov (2008 — reservation price skewed
against inventory), Aronson (bound every degree of freedom: fixed windows,
clamped ratios, documented None guards), Carver (fixed measurement, never
free-parameter fits).
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

MIN_BARS = 30           # fail-open floor for the windowed estimators
CS_BARS = 20            # Corwin-Schultz lookback (bars -> CS_BARS-1 pairs)
_CS_DENOM = 3.0 - 2.0 * math.sqrt(2.0)


def _clean(values: Sequence[Any], lo: float = 0.0) -> List[float]:
    """Float-coerce, drop non-finite and non-positive entries pairwise-safe."""
    out: List[float] = []
    for v in values or []:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f) and f > lo:
            out.append(f)
        else:
            out.append(float("nan"))   # keep index alignment; callers filter
    return out


def _paired(*series: List[float]) -> List[tuple]:
    """Zip series, dropping any index where any leg is non-finite."""
    return [t for t in zip(*series) if all(math.isfinite(x) for x in t)]


def kyle_lambda(closes: Sequence[float], volumes: Sequence[float],
                window: int = 60,
                opens: Optional[Sequence[float]] = None) -> Optional[float]:
    """OLS slope of dPrice on signed dollar volume over the last `window`
    bars. Units: price-impact per dollar. Sign of flow = sign(close-open)
    when `opens` is injected, else sign(dClose) (the close-to-close proxy).
    None if <30 valid bars or zero variance in the signed-volume leg."""
    try:
        window = max(int(window), 2)
        _c = _clean(list(closes)[-(window + 1):])
        _v = _clean(list(volumes)[-(window + 1):])
        _o = _clean(list(opens)[-(window + 1):]) if opens is not None else None
        n = min(len(_c), len(_v))
        if _o is not None:
            n = min(n, len(_o))
        _c, _v = _c[-n:], _v[-n:]
        if _o is not None:
            _o = _o[-n:]
        xs: List[float] = []
        ys: List[float] = []
        for i in range(1, n):
            c0, c1, v1 = _c[i - 1], _c[i], _v[i]
            if not all(map(math.isfinite, (c0, c1, v1))):
                continue
            dp = c1 - c0
            if _o is not None and math.isfinite(_o[i]):
                sgn = 1.0 if c1 > _o[i] else (-1.0 if c1 < _o[i] else 0.0)
            else:
                sgn = 1.0 if dp > 0 else (-1.0 if dp < 0 else 0.0)
            xs.append(sgn * v1 * c1)     # signed dollar volume
            ys.append(dp)
        if len(xs) < MIN_BARS:
            return None
        mx = sum(xs) / len(xs)
        my = sum(ys) / len(ys)
        var_x = sum((x - mx) ** 2 for x in xs)
        if var_x <= 0:
            return None
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        return cov / var_x
    except Exception:
        return None


def amihud_illiq(closes: Sequence[float], volumes: Sequence[float],
                 window: int = 60) -> Optional[float]:
    """Mean(|ret_i| / dollar_vol_i) over the last `window` bars. None if
    <30 valid bars or every dollar-volume leg is zero."""
    try:
        window = max(int(window), 2)
        _c = _clean(list(closes)[-(window + 1):])
        _v = _clean(list(volumes)[-(window + 1):])
        n = min(len(_c), len(_v))
        _c, _v = _c[-n:], _v[-n:]
        terms: List[float] = []
        for i in range(1, n):
            c0, c1, v1 = _c[i - 1], _c[i], _v[i]
            if not all(map(math.isfinite, (c0, c1, v1))):
                continue
            dv = v1 * c1
            if dv <= 0:
                continue
            terms.append(abs(c1 / c0 - 1.0) / dv)
        if len(terms) < MIN_BARS:
            return None
        return sum(terms) / len(terms)
    except Exception:
        return None


def corwin_schultz_spread(highs: Sequence[float],
                          lows: Sequence[float]) -> Optional[float]:
    """2-period high-low spread estimator over the last ~20 bars. Returns
    the estimated spread as a FRACTION (0.0005 = 5bps). Per-bar alpha from
    ln(H_t/L_t)^2 + ln(H_{t-1}/L_{t-1})^2 (beta) and the 2-bar range
    (gamma); negative alpha clamped to 0. None on degenerate input."""
    try:
        _h = _clean(list(highs)[-CS_BARS:])
        _l = _clean(list(lows)[-CS_BARS:])
        n = min(len(_h), len(_l))
        _h, _l = _h[-n:], _l[-n:]
        alphas: List[float] = []
        for i in range(1, n):
            h0, l0, h1, l1 = _h[i - 1], _l[i - 1], _h[i], _l[i]
            if not all(map(math.isfinite, (h0, l0, h1, l1))):
                continue
            if h1 <= l1 or h0 <= l0:
                continue
            hh, ll = max(h0, h1), min(l0, l1)
            if hh <= ll:
                continue
            beta = math.log(h1 / l1) ** 2 + math.log(h0 / l0) ** 2
            gamma = math.log(hh / ll) ** 2
            alpha = ((math.sqrt(2.0 * beta) - math.sqrt(beta)) / _CS_DENOM
                     - math.sqrt(gamma / _CS_DENOM))
            alphas.append(max(alpha, 0.0))
        if len(alphas) < (CS_BARS - 1) // 2:
            return None
        a = sum(alphas) / len(alphas)
        return 2.0 * (math.exp(a) - 1.0) / (1.0 + math.exp(a))
    except Exception:
        return None


def realized_skew(closes: Sequence[float], window: int = 60) -> Optional[float]:
    """Sample skewness of 1m log returns over the last `window` bars.
    None if <30 bars or zero variance."""
    try:
        window = max(int(window), 2)
        _c = _clean(list(closes)[-(window + 1):])
        rets: List[float] = []
        for i in range(1, len(_c)):
            c0, c1 = _c[i - 1], _c[i]
            if math.isfinite(c0) and math.isfinite(c1):
                rets.append(math.log(c1 / c0))
        if len(rets) < MIN_BARS:
            return None
        m = sum(rets) / len(rets)
        m2 = sum((r - m) ** 2 for r in rets) / len(rets)
        if m2 <= 0:
            return None
        m3 = sum((r - m) ** 3 for r in rets) / len(rets)
        return m3 / (m2 ** 1.5)
    except Exception:
        return None


def avellaneda_stoikov_reservation(mid: float, inventory_usd: float,
                                   sigma: float, gamma: float = 0.1,
                                   horizon_s: float = 60.0,
                                   max_inventory_usd: float = 1000.0
                                   ) -> float:
    """Reservation price = mid - q_norm x gamma x sigma^2 x horizon, with
    q_norm = inventory_usd / max_inventory_usd clamped to [-1, 1]. Long
    inventory pushes the reservation BELOW mid (quote to sell). Pure
    arithmetic; degenerate mid/sigma/max_inventory returns mid unchanged."""
    try:
        mid = float(mid)
        sigma = float(sigma)
        max_inv = float(max_inventory_usd)
        if mid <= 0 or sigma <= 0 or max_inv <= 0:
            return mid
        q = float(inventory_usd) / max_inv
        q = max(-1.0, min(1.0, q))
        return mid - q * float(gamma) * sigma * sigma * float(horizon_s)
    except (TypeError, ValueError):
        return mid if isinstance(mid, (int, float)) else 0.0


def _field(bar: Any, name: str) -> Any:
    if isinstance(bar, dict):
        return bar.get(name)
    return getattr(bar, name, None)


def estimate_symbol(ohlcv_bars: Sequence[Any], window: int = 60) -> Dict[str, Optional[float]]:
    """One-shot estimator bundle over a list of bar objects/dicts carrying
    open/high/low/close/volume. Every leg fail-open (None preserved)."""
    bars = list(ohlcv_bars or [])
    opens = [_field(b, "open") for b in bars]
    highs = [_field(b, "high") for b in bars]
    lows = [_field(b, "low") for b in bars]
    closes = [_field(b, "close") for b in bars]
    volumes = [_field(b, "volume") for b in bars]
    return {
        "kyle_lambda": kyle_lambda(closes, volumes, window=window, opens=opens),
        "amihud_illiq": amihud_illiq(closes, volumes, window=window),
        "cs_spread": corwin_schultz_spread(highs, lows),
        "realized_skew": realized_skew(closes, window=window),
    }
