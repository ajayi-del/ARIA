"""
intelligence/equity_outliers.py — equity/stock-perp outlier detectors
(2026-10-17 Governor directive, "STOCK EXECUTION ARCHITECTURE" spec).

Four SHADOW-FIRST telemetry detectors for the equity/stock-perp cohort.
They score and return verdicts; the splice emits events. They NEVER
change trading behavior — loosen-ONLY doctrine: shadow scoring can never
reject or alter a trade.

Detectors:
  1. oi_velocity — equity-perp open interest accelerating vs its OWN
     trailing baseline (informed positioning before moves). Consumes the
     data/oi_history_feed.py output shape: oldest-first [(ts_ms, oi)].
     Abstains on < OI_VELOCITY_MIN_POINTS or staleness > 48h, matching
     the feed's doctrine.
  2. rotation_residual — the stock's return residual vs what its
     category flow predicts (idiosyncratic = stock-specific news/alpha,
     not beta). Z-score of the current residual against the symbol's own
     injected residual window (intelligence/rotation.py residual style).
  3. funding_divergence — equity-perp funding diverging from the crypto
     cohort's funding median (perp-DEX crowd positioning; equity perps
     on DEXes skew retail). Inputs are DECIMAL rates (0.0001 = 1 bp).
  4. pre_earnings_oi_build — OI building into a known earnings date
     (risk_calendar/equity_events.py). Window: 5 trading days (5 daily
     OI bars). Abstains with no earnings date or outside the window.

Department-template shape (docs/DEPARTMENT_TEMPLATE.md):
  - Zero I/O: no network, no files, no logging. All state injected.
  - One splice point (orchestrator-owned, NOT this module).
  - Kill switch: env EQUITY_OUTLIER_ENABLED default "true"; "false" =
    callers must behave as legacy, i.e., NEVER call the brains. The
    brains themselves stay pure and score regardless — the helper is
    the splice's gate, not a behavior mutation inside the detectors.
  - Own telemetry namespace: equity_outlier_* events (names below).
  - Own test file: tests/test_equity_outliers.py.

TIMESTAMP UNITS (load-bearing): OI series points are (ts_ms, oi) in
MILLISECONDS — the data/oi_history_feed.py parse_series contract.
earnings_ts and now_ts are epoch SECONDS.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


# ── Kill switch ───────────────────────────────────────────────────────────────

def equity_outlier_enabled() -> bool:
    """Module-level kill switch (env EQUITY_OUTLIER_ENABLED, default true).
    False = the splice behaves as legacy: it must NEVER call the brains
    and emit nothing — indistinguishable from this module not existing."""
    return os.environ.get("EQUITY_OUTLIER_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on")


# ── Event names (the splice emits exactly these) ──────────────────────────────

EVENT_OI_VELOCITY = "equity_outlier_oi_velocity"
EVENT_ROTATION_RESIDUAL = "equity_outlier_rotation_residual"
EVENT_FUNDING_DIVERGENCE = "equity_outlier_funding_divergence"
EVENT_PRE_EARNINGS_OI = "equity_outlier_pre_earnings_oi"

EVENT_NAMES = (
    EVENT_OI_VELOCITY,
    EVENT_ROTATION_RESIDUAL,
    EVENT_FUNDING_DIVERGENCE,
    EVENT_PRE_EARNINGS_OI,
)


# ── Thresholds (conservative, documented; shadow-only so they never reject) ──

# 1. OI velocity: fire when the newest daily ΔOI is ≥ 2 standard deviations
#    from the symbol's own trailing ΔOI baseline — a 2-sigma informed-
#    positioning read; sub-2-sigma OI wiggles are noise on daily bars.
OI_VELOCITY_Z_THRESHOLD = 2.0
OI_VELOCITY_MIN_POINTS = 7          # need ≥6 baseline deltas + the newest one
OI_VELOCITY_STALE_S = 48 * 3600.0   # matches oi_history_feed 48h abstain law

# 2. Rotation residual: fire when |z| ≥ 2 of the current residual vs the
#    symbol's own residual window — idiosyncratic beyond 2-sigma of its
#    normal category deviation; inside the band is beta, not alpha.
ROTATION_RESIDUAL_Z_THRESHOLD = 2.0
ROTATION_RESIDUAL_MIN_WINDOW = 10   # <10 past residuals = no distribution

# 3. Funding divergence: fire when |symbol − cohort median| ≥ 10 bps.
#    Perp funding typically lives in a ±1-10 bp band per interval; 10 bps
#    of divergence from the crypto cohort is a clear crowd-positioning skew.
FUNDING_DIVERGENCE_BPS_THRESHOLD = 10.0

# 4. Pre-earnings OI build: fire when OI grows ≥ 10% over the 5-bar window
#    into a dated catalyst — double-digit OI expansion is positioning, not
#    drift. Window is 5 trading days per the Governor's spec.
PRE_EARNINGS_WINDOW_DAYS = 5.0
PRE_EARNINGS_LOOKBACK_POINTS = 5    # 5 daily bars ≈ 5 trading days
PRE_EARNINGS_MIN_POINTS = 5
PRE_EARNINGS_BUILD_RATIO_THRESHOLD = 1.10
PRE_EARNINGS_STALE_S = 48 * 3600.0

DAY_S = 86400.0

# A baseline/window std below this is float dust on a constant series —
# abstain as degenerate rather than divide by ~1e-18 and mint a fake z.
_DEGENERATE_STD_EPS = 1e-12


# ── Verdict ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OutlierVerdict:
    """One detector's score. `event` is the exact telemetry name the splice
    emits; `score` is None on abstain; `fired` is score vs the detector's
    documented threshold; `abstain_reason` is non-empty exactly when the
    detector refused to guess (never invents a score on thin data)."""
    symbol: str
    event: str
    score: Optional[float]
    fired: bool
    abstain_reason: str = ""
    detail: Dict = field(default_factory=dict)


# ── Shared helpers ────────────────────────────────────────────────────────────

def _f(x) -> Optional[float]:
    """Coerce to float; None on garbage/NaN/inf (fail-closed idiom)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _clean_series(oi_series) -> List[Tuple[float, float]]:
    """Normalize an injected OI series to oldest-first [(ts_ms, oi)] with
    non-negative OI and finite timestamps. One bad point kills only itself
    (one-bad-line doctrine); the series is re-sorted, never trusted."""
    out: List[Tuple[float, float]] = []
    for pt in oi_series or []:
        try:
            ts, oi = pt[0], pt[1]
        except (TypeError, IndexError, KeyError):
            continue
        ts_f, oi_f = _f(ts), _f(oi)
        if ts_f is None or oi_f is None or oi_f < 0.0:
            continue
        out.append((ts_f, oi_f))
    out.sort(key=lambda t: t[0])
    return out


def _pstdev(values: Sequence[float]) -> float:
    """Population standard deviation; 0.0 on empty (callers guard length)."""
    n = len(values)
    if n == 0:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(max(var, 0.0))


# ── 1. OI velocity ────────────────────────────────────────────────────────────

def oi_velocity(symbol, oi_series, now_ts) -> OutlierVerdict:
    """Z-score of the NEWEST daily ΔOI vs the symbol's own trailing ΔOI
    baseline. Fires on |z| ≥ OI_VELOCITY_Z_THRESHOLD (side in detail:
    "expansion" / "contraction"). Abstains on thin data (< MIN_POINTS),
    staleness > 48h (feed doctrine), a degenerate (zero-variance)
    baseline, or bad now_ts — never guesses."""
    sym = str(symbol or "").strip().upper()
    series = _clean_series(oi_series)
    now = _f(now_ts)
    if now is None:
        return OutlierVerdict(sym, EVENT_OI_VELOCITY, None, False, "bad_now_ts")
    if len(series) < OI_VELOCITY_MIN_POINTS:
        return OutlierVerdict(sym, EVENT_OI_VELOCITY, None, False,
                              "insufficient_points",
                              {"points": len(series),
                               "min_points": OI_VELOCITY_MIN_POINTS})
    age_s = max(0.0, now - series[-1][0] / 1000.0)
    if age_s > OI_VELOCITY_STALE_S:
        return OutlierVerdict(sym, EVENT_OI_VELOCITY, None, False,
                              "stale", {"age_s": round(age_s, 1),
                                        "stale_s": OI_VELOCITY_STALE_S})
    deltas = [series[i][1] - series[i - 1][1] for i in range(1, len(series))]
    baseline, newest = deltas[:-1], deltas[-1]
    mean = sum(baseline) / len(baseline)
    std = _pstdev(baseline)
    if std <= _DEGENERATE_STD_EPS:
        return OutlierVerdict(sym, EVENT_OI_VELOCITY, None, False,
                              "degenerate_baseline")
    z = (newest - mean) / std
    fired = abs(z) >= OI_VELOCITY_Z_THRESHOLD
    return OutlierVerdict(
        sym, EVENT_OI_VELOCITY, round(z, 4), fired, "",
        {"z": round(z, 4), "threshold": OI_VELOCITY_Z_THRESHOLD,
         "side": "expansion" if z >= 0 else "contraction",
         "delta_latest": newest, "baseline_mean": round(mean, 6),
         "baseline_std": round(std, 6), "points": len(series)})


# ── 2. Rotation residual ──────────────────────────────────────────────────────

def rotation_residual(symbol, symbol_ret, category_ret, residual_window) -> OutlierVerdict:
    """Z-score of the current residual (symbol_ret − category_ret) against
    the symbol's OWN injected residual window. Positive z = outperforming
    its category flow (idiosyncratic strength); negative = idiosyncratic
    weakness. Fires on |z| ≥ ROTATION_RESIDUAL_Z_THRESHOLD. Abstains on a
    thin window (< MIN_WINDOW), zero-variance window, or bad inputs."""
    sym = str(symbol or "").strip().upper()
    s_ret, c_ret = _f(symbol_ret), _f(category_ret)
    if s_ret is None or c_ret is None:
        return OutlierVerdict(sym, EVENT_ROTATION_RESIDUAL, None, False, "bad_input")
    window: List[float] = []
    for r in residual_window or []:
        rv = _f(r)
        if rv is not None:
            window.append(rv)
    if len(window) < ROTATION_RESIDUAL_MIN_WINDOW:
        return OutlierVerdict(sym, EVENT_ROTATION_RESIDUAL, None, False,
                              "insufficient_window",
                              {"window": len(window),
                               "min_window": ROTATION_RESIDUAL_MIN_WINDOW})
    current = s_ret - c_ret
    mean = sum(window) / len(window)
    std = _pstdev(window)
    if std <= _DEGENERATE_STD_EPS:
        return OutlierVerdict(sym, EVENT_ROTATION_RESIDUAL, None, False,
                              "degenerate_window")
    z = (current - mean) / std
    fired = abs(z) >= ROTATION_RESIDUAL_Z_THRESHOLD
    return OutlierVerdict(
        sym, EVENT_ROTATION_RESIDUAL, round(z, 4), fired, "",
        {"z": round(z, 4), "threshold": ROTATION_RESIDUAL_Z_THRESHOLD,
         "residual": round(current, 8), "window_mean": round(mean, 8),
         "window_std": round(std, 8), "window": len(window)})


# ── 3. Funding divergence ─────────────────────────────────────────────────────

def funding_divergence(symbol, symbol_funding, cohort_funding_median) -> OutlierVerdict:
    """Divergence of the equity perp's funding from the crypto cohort's
    median, in basis points. Inputs are DECIMAL rates (0.0001 = 1 bp).
    side="rich" (symbol funding above cohort — longs paying, crowded long)
    / "cheap". Fires on |divergence| ≥ FUNDING_DIVERGENCE_BPS_THRESHOLD.
    Abstains on bad inputs — never invents a funding state."""
    sym = str(symbol or "").strip().upper()
    s_fr, c_fr = _f(symbol_funding), _f(cohort_funding_median)
    if s_fr is None or c_fr is None:
        return OutlierVerdict(sym, EVENT_FUNDING_DIVERGENCE, None, False, "bad_input")
    div_bps = (s_fr - c_fr) * 1e4
    fired = abs(div_bps) >= FUNDING_DIVERGENCE_BPS_THRESHOLD
    return OutlierVerdict(
        sym, EVENT_FUNDING_DIVERGENCE, round(div_bps, 4), fired, "",
        {"divergence_bps": round(div_bps, 4),
         "threshold_bps": FUNDING_DIVERGENCE_BPS_THRESHOLD,
         "side": "rich" if div_bps >= 0 else "cheap",
         "symbol_funding": s_fr, "cohort_median": c_fr})


# ── 4. Pre-earnings OI build ──────────────────────────────────────────────────

def pre_earnings_oi_build(symbol, oi_series, earnings_ts, now_ts) -> OutlierVerdict:
    """OI build ratio (latest / window-start) into a known earnings date.
    Window = PRE_EARNINGS_WINDOW_DAYS trading days (LOOKBACK_POINTS daily
    bars). Fires on ratio ≥ PRE_EARNINGS_BUILD_RATIO_THRESHOLD. Abstains:
    no earnings date, earnings in the past or beyond the window, thin or
    stale (>48h) OI series, degenerate (zero) window-start OI, bad now_ts."""
    sym = str(symbol or "").strip().upper()
    ets, now = _f(earnings_ts), _f(now_ts)
    if now is None:
        return OutlierVerdict(sym, EVENT_PRE_EARNINGS_OI, None, False, "bad_now_ts")
    if ets is None:
        return OutlierVerdict(sym, EVENT_PRE_EARNINGS_OI, None, False,
                              "no_earnings_date")
    days_to = (ets - now) / DAY_S
    if days_to <= 0.0 or days_to > PRE_EARNINGS_WINDOW_DAYS:
        return OutlierVerdict(sym, EVENT_PRE_EARNINGS_OI, None, False,
                              "outside_window",
                              {"days_to_earnings": round(days_to, 3),
                               "window_days": PRE_EARNINGS_WINDOW_DAYS})
    series = _clean_series(oi_series)
    if len(series) < PRE_EARNINGS_MIN_POINTS:
        return OutlierVerdict(sym, EVENT_PRE_EARNINGS_OI, None, False,
                              "insufficient_points",
                              {"points": len(series),
                               "min_points": PRE_EARNINGS_MIN_POINTS})
    age_s = max(0.0, now - series[-1][0] / 1000.0)
    if age_s > PRE_EARNINGS_STALE_S:
        return OutlierVerdict(sym, EVENT_PRE_EARNINGS_OI, None, False,
                              "stale", {"age_s": round(age_s, 1),
                                        "stale_s": PRE_EARNINGS_STALE_S})
    start_oi = series[max(0, len(series) - PRE_EARNINGS_LOOKBACK_POINTS)][1]
    latest_oi = series[-1][1]
    if start_oi <= 0.0:
        return OutlierVerdict(sym, EVENT_PRE_EARNINGS_OI, None, False,
                              "degenerate_base")
    ratio = latest_oi / start_oi
    fired = ratio >= PRE_EARNINGS_BUILD_RATIO_THRESHOLD
    return OutlierVerdict(
        sym, EVENT_PRE_EARNINGS_OI, round(ratio, 4), fired, "",
        {"build_ratio": round(ratio, 4),
         "threshold": PRE_EARNINGS_BUILD_RATIO_THRESHOLD,
         "days_to_earnings": round(days_to, 3),
         "window_days": PRE_EARNINGS_WINDOW_DAYS,
         "oi_start": start_oi, "oi_latest": latest_oi,
         "points": len(series)})
