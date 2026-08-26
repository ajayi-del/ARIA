"""Risk-parity position sizing (Carver / Van Tharp) — 2026-08-26.

The legacy chain sizes NOTIONAL: every trade gets base × conviction-stack
regardless of stop distance, so a 0.4% stop and a 3% stop carry ~7x different
risk at the same notional — and every trade LOOKS the same size.

Doctrine:
  Carver (Systematic Trading): position = risk_budget / instrument_risk.
  Van Tharp (R-multiples): size so one R (|entry - stop| per unit) equals the
    chosen capital fraction.
  Thorp (fractional Kelly): the fraction itself scales with edge — the
    existing multiplier chain (conviction, recovery, session, drawdown)
    already does that and is preserved multiplicatively.
  Vince (Leverage Space): fraction of the EXECUTING venue's equity — the
    chain is already venue-local (39136a5-era fix); unchanged here.
  Aronson: bound every new degree of freedom — the resize ratio is clamped.

Implementation = risk-invariant resize. Keep the chain's aggregate size as
the RISK intent, re-express it through the candidate's own stop distance:

    ratio = ref_stop_pct / actual_stop_pct     clamped [MIN_RATIO, MAX_RATIO]
    size *= ratio

A trade whose stop sits at the reference distance is bit-for-bit unchanged;
tighter stops earn more notional, wider stops less — risk per trade is
equalized, not notional. Abstains (None) on missing or degenerate stops:
a missing stop must never resize a trade.
"""

import os
from typing import Optional

REF_STOP_PCT = float(os.getenv("RISK_PARITY_REF_STOP_PCT", "0.01"))
MIN_RATIO = float(os.getenv("RISK_PARITY_MIN_RATIO", "0.25"))
MAX_RATIO = float(os.getenv("RISK_PARITY_MAX_RATIO", "3.0"))

# Below 1bp the stop distance is data noise, not structure — abstain.
_MIN_STOP_DIST_PCT = 1e-4


def risk_parity_enabled() -> bool:
    return os.getenv("RISK_PARITY_SIZING_ENABLED", "true").lower() == "true"


def risk_parity_ratio(
    entry_price,
    stop_price,
    ref_stop_pct: float = REF_STOP_PCT,
    min_ratio: float = MIN_RATIO,
    max_ratio: float = MAX_RATIO,
) -> Optional[float]:
    """Resize ratio for risk-parity sizing, or None to keep the legacy size."""
    try:
        entry = float(entry_price)
        stop = float(stop_price)
    except (TypeError, ValueError):
        return None
    if entry <= 0.0 or stop <= 0.0 or stop == entry:
        return None
    dist_pct = abs(entry - stop) / entry
    if dist_pct < _MIN_STOP_DIST_PCT:
        return None
    ratio = ref_stop_pct / dist_pct
    return max(min_ratio, min(max_ratio, ratio))
