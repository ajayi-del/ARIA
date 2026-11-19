"""Early-tighten gate — MANAGEMENT geometry (prop harness, 2026-11-19).

Separate from BIRTH (stop_floor). Premature BE is a synthetic tight stop:
BTC turbo prop-acct1-BTC-1795082053035 raised trail/BE at age 7s and
runner_harvest at 22s into a noise mfe peak → scrape −$1.84 at 68.8s.
TRX bank-at-entry at ≈1.9s after soft rebase (stop_equals_entry).

Doctrine:
  NEVER raise stop toward entry until
    (a) age ≥ min_hold_s (crypto band 300–900s; default 300s) AND
    (b) mfe_peak_r ≥ R★ — 0.5R before BE/bank/trail; 1.0R before
        runner_harvest / live_peak / mfe_sharpen.

Kill PROP_EARLY_TIGHTEN_GATE=0 → always allow.
Fail-OPEN: missing opened_ts → allow; missing mfe when R★>0 → allow.
"""

from __future__ import annotations

import time
from typing import Any

DEFAULT_MIN_AGE_S = 300.0          # bottom of 300–900s crypto doctrine band
DEFAULT_BE_MIN_MFE_R = 0.5         # Mid proof before BE/bank/trail
DEFAULT_HARVEST_MIN_MFE_R = 1.0    # full 1R before aggressive harvest

# Stages that RAISE the stop into/through price (management, not birth).
BE_STAGES = frozenset({
    "bank", "breakeven", "breakeven_mark", "trail",
})
HARVEST_STAGES = frozenset({
    "mfe_sharpen", "live_peak", "runner_harvest",
})
TIGHTEN_STAGES = BE_STAGES | HARVEST_STAGES


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return v


def min_mfe_for_stage(
        stage: str | None,
        be_min_mfe_r: float = DEFAULT_BE_MIN_MFE_R,
        harvest_min_mfe_r: float = DEFAULT_HARVEST_MIN_MFE_R,
) -> float:
    if stage in HARVEST_STAGES:
        return float(harvest_min_mfe_r)
    if stage in BE_STAGES:
        return float(be_min_mfe_r)
    # Unknown tighten stage: use the stricter harvest bar (safe-side).
    if stage in TIGHTEN_STAGES:
        return float(harvest_min_mfe_r)
    return 0.0


def early_tighten_allowed(
        bracket: dict | None,
        *,
        enabled: bool = True,
        min_age_s: float = DEFAULT_MIN_AGE_S,
        be_min_mfe_r: float = DEFAULT_BE_MIN_MFE_R,
        harvest_min_mfe_r: float = DEFAULT_HARVEST_MIN_MFE_R,
        now: float | None = None,
        stage: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """True when a management tighten may raise the stop."""
    mfe_need = min_mfe_for_stage(stage, be_min_mfe_r, harvest_min_mfe_r)
    detail: dict[str, Any] = {
        "stage": stage,
        "min_age_s": float(min_age_s) if min_age_s is not None else None,
        "min_mfe_r": mfe_need,
        "be_min_mfe_r": float(be_min_mfe_r),
        "harvest_min_mfe_r": float(harvest_min_mfe_r),
    }
    if not enabled:
        detail["reason"] = "kill_off"
        return True, detail
    if stage is not None and stage not in TIGHTEN_STAGES:
        detail["reason"] = "non_tighten_stage"
        return True, detail
    if not isinstance(bracket, dict):
        detail["reason"] = "no_bracket_fail_open"
        return True, detail

    ts = _f(now)
    if ts is None:
        ts = time.time()
    opened = _f(bracket.get("opened_ts"))
    if opened is None or opened <= 0:
        detail["reason"] = "missing_opened_ts_fail_open"
        return True, detail
    age = ts - opened
    detail["age_s"] = age
    detail["mfe_peak_r"] = _f(bracket.get("mfe_peak_r"))

    try:
        age_need = float(min_age_s or 0.0)
    except (TypeError, ValueError):
        age_need = 0.0

    if age_need > 0 and age < age_need:
        detail["reason"] = "age_below_min"
        return False, detail

    if mfe_need > 0:
        mfe = _f(bracket.get("mfe_peak_r"))
        if mfe is None:
            detail["reason"] = "missing_mfe_fail_open"
            return True, detail
        if mfe < mfe_need:
            detail["reason"] = "mfe_below_min"
            return False, detail

    detail["reason"] = "allowed"
    return True, detail
