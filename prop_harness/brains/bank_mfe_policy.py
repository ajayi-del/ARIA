"""Bank green-MFE policy registers (ATS-201, 2026-11-19).

Pure zero-I/O. Counterfactual + live helpers for capture floors that
bank Mid/Runner peak instead of scratching to red.

Winning CF (48h Vultr journals, focus seats, PROXY labeled in docs):
  A peak R-lock @ 0.5R→50% / 1.0R→75% of peak$  ≈ +$1902 of ~$2661 left
  B Mid partial tax                              ≈ +$509 (dominated by A)
  C sticky apex                                  ≈ $0 (twin refuse live)
  D scrape standdown                             ≈ +$15

Live wire = A: honor PROP_LIVE_PEAK_LOCK_RUNGS_R (env already set;
code previously ignored it and armed only on absolute USD ≥$30).
"""

from __future__ import annotations

from typing import Any

# Default R-rungs matching live prop.env PROP_LIVE_PEAK_LOCK_RUNGS_R.
DEFAULT_PEAK_RUNGS_R: tuple[tuple[float, float], ...] = (
    (0.5, 0.50),   # Mid proof → lock 50% of peak$
    (1.0, 0.75),   # Runner proof → lock 75%
)

DEFAULT_PARTIAL_RUNGS: tuple[tuple[float, float], ...] = (
    (0.5, 0.25),
    (1.0, 0.25),
    (2.0, 0.25),
)


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return v


def parse_rungs_r(raw: Any) -> tuple[tuple[float, float], ...]:
    """Parse ``0.5:0.5,1.0:0.75`` → ((trigger_r, lock_frac), ...).

    Empty / junk → default. Used by config + CF + live_peak verdict.
    """
    if raw is None or raw == "":
        return DEFAULT_PEAK_RUNGS_R
    if isinstance(raw, (list, tuple)):
        out = []
        for row in raw:
            try:
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    out.append((float(row[0]), float(row[1])))
            except (TypeError, ValueError):
                continue
        return tuple(out) if out else DEFAULT_PEAK_RUNGS_R
    parts: list[tuple[float, float]] = []
    for tok in str(raw).split(","):
        tok = tok.strip()
        if not tok or ":" not in tok:
            continue
        a, b = tok.split(":", 1)
        try:
            parts.append((float(a), float(b)))
        except (TypeError, ValueError):
            continue
    return tuple(parts) if parts else DEFAULT_PEAK_RUNGS_R


def lock_frac_for_mfe(
        mfe_peak_r: Any,
        rungs_r: Any = None,
) -> float | None:
    """Highest lock_frac whose trigger_r ≤ mfe. None if below first rung."""
    mfe = _f(mfe_peak_r)
    if mfe is None or mfe <= 0:
        return None
    frac = None
    for trigger_r, lock_frac in parse_rungs_r(rungs_r):
        if mfe + 1e-12 >= float(trigger_r):
            frac = float(lock_frac)
    return frac


def peak_usd_from_r(
        mfe_peak_r: Any,
        risk_usd: Any,
) -> float | None:
    """peak$ = mfe_peak_r × risk (birth or residual). None on junk."""
    mfe = _f(mfe_peak_r)
    risk = _f(risk_usd)
    if mfe is None or risk is None or mfe <= 0 or risk <= 0:
        return None
    return float(mfe) * float(risk)


def cf_peak_r_banked(
        *,
        mfe_peak_r: Any,
        risk_usd: Any,
        realized_pnl: Any,
        rungs_r: Any = None,
) -> dict[str, Any]:
    """PROXY CF: if R-rung peak lock held to exit, banked = max(actual, frac×peak$).

    Recovery delta = banked − actual. No path replay of post-lock giveback.
    """
    actual = _f(realized_pnl)
    peak = peak_usd_from_r(mfe_peak_r, risk_usd)
    frac = lock_frac_for_mfe(mfe_peak_r, rungs_r)
    out: dict[str, Any] = {
        "armed": False, "frac": frac, "peak_usd": peak,
        "banked": actual, "delta": 0.0,
    }
    if actual is None or peak is None or frac is None:
        return out
    banked = max(actual, float(frac) * float(peak))
    out.update({
        "armed": True, "banked": banked, "delta": banked - actual,
    })
    return out


def cf_partial_banked(
        *,
        mfe_peak_r: Any,
        risk_usd: Any,
        realized_pnl: Any,
        rungs: Any = None,
) -> dict[str, Any]:
    """PROXY CF: Mid partial tax — bank frac of remaining at each rung mark."""
    actual = _f(realized_pnl)
    mfe = _f(mfe_peak_r)
    risk = _f(risk_usd)
    out: dict[str, Any] = {
        "armed": False, "banked": actual, "delta": 0.0, "partial_usd": 0.0,
    }
    if actual is None or mfe is None or risk is None or mfe <= 0 or risk <= 0:
        return out
    ladder = parse_rungs_r(rungs) if rungs else DEFAULT_PARTIAL_RUNGS
    # partial ladder uses (trigger, qty_frac) — reuse parser shape
    rem = 1.0
    partial_usd = 0.0
    armed = False
    for trigger_r, qty_frac in ladder:
        if mfe + 1e-12 < float(trigger_r):
            continue
        armed = True
        partial_usd += rem * float(qty_frac) * (float(trigger_r) * float(risk))
        rem *= (1.0 - float(qty_frac))
    if not armed:
        return out
    banked = partial_usd + rem * actual
    out.update({
        "armed": True, "partial_usd": partial_usd,
        "banked": banked, "delta": banked - actual, "rem_frac": rem,
    })
    return out
