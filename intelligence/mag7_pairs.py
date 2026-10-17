"""
intelligence/mag7_pairs.py — MAG7 relative-value pair universe (Governor
directive 2026-10-17, "STOCK EXECUTION ARCHITECTURE" spec, piece 2).
SHADOW-FIRST: score and emit only — never changes trading behavior.

The alpha: mega-cap tech trades as a complex. Relative-value
dislocations BETWEEN members (NVDA/AMD, MSFT/AAPL, GOOGL/META, TSLA vs
the complex) are the tradeable residual — the market prices the factor;
this module prices the idiosyncratic spread.

This EXTENDS the pair-spread plane (intelligence/pair_spread.py), it does
not rebuild it. pair_spread.py declares no universe — its pairs are
injected as row dicts {sym_a, sym_b, plane, hedge_ratio, intercept,
spread_std, half_life_days, status}. MAG7_PAIRS below is the universe
declaration the orchestrator converts into that row shape (or feeds
through this module's simpler return-spread verdict while the
cointegration screen accrues history on the new symbols).

Zero-I/O (department template): no network, no files, no logging. The
splice injects per-leg returns over a window; the brain returns
telemetry dicts the splice emits.

Complex leg doctrine (Chan, trade the residual):
  - The complex leg is an equal-weight mean of the AVAILABLE member
    returns. Missing members are DROPPED, never zero-filled (a zero-fill
    fabricates a flat leg the market never printed).
  - complex_return() abstains (None) when fewer than
    COMPLEX_MIN_MEMBERS (4) of the 7 members are available — a complex
    priced on a minority of its members is a different instrument.
  - For the TSLA-vs-complex pair the caller passes the OTHER members
    (focal leg excluded) — the complex is the hedge, not the position.

Fire doctrine (bounded, fixed, never self-tuning):
  fired when |spread_pct| >= DIVERGENCE_FIRE_PCT (2.0 — a 2% RV
  dislocation over the window between two members of the same complex
  is the materiality bar; below it fees and basis noise eat the trade).

Kill switch: env MAG7_PAIRS_ENABLED (default "true"). False = the
splice stands down; pre-module system bit-for-bit.

Splice event: "mag7_pair_divergence" with fields {pair, spread_pct,
rich_leg, fired}.
"""
from __future__ import annotations

import os
from typing import Dict, Optional

MAG7_PAIR_EVENT = "mag7_pair_divergence"
DIVERGENCE_FIRE_PCT = 2.0       # |spread| materiality bar, in pct
COMPLEX_MIN_MEMBERS = 4         # below this the complex is a different instrument

# The seven members of the MAG7 complex, in SoDEX perp symbol form.
MAG7_MEMBERS = (
    "NVDA-USD", "AMD-USD", "MSFT-USD", "AAPL-USD",
    "GOOGL-USD", "META-USD", "TSLA-USD",
)

# The equal-weight complex leg members (all seven; callers exclude the
# focal leg when scoring a member-vs-complex pair).
MAG7_COMPLEX_MEMBERS = MAG7_MEMBERS

# Relative-value pair universe. kind="pair" legs are two single names;
# kind="complex" leg_b is the equal-weight complex (see module docstring
# for who computes it). Names match the spec's declared dislocations.
MAG7_PAIRS = (
    {"name": "NVDA/AMD", "leg_a": "NVDA-USD", "leg_b": "AMD-USD",
     "kind": "pair"},
    {"name": "MSFT/AAPL", "leg_a": "MSFT-USD", "leg_b": "AAPL-USD",
     "kind": "pair"},
    {"name": "GOOGL/META", "leg_a": "GOOGL-USD", "leg_b": "META-USD",
     "kind": "pair"},
    {"name": "TSLA/MAG7", "leg_a": "TSLA-USD", "leg_b": "MAG7_COMPLEX",
     "kind": "complex"},
)


def mag7_pairs_enabled() -> bool:
    """Env kill switch helper. Default on; 'false' (any case) stands down."""
    return os.environ.get("MAG7_PAIRS_ENABLED", "true").lower() != "false"


def complex_return(member_rets: Dict[str, Optional[float]]) -> Optional[float]:
    """Equal-weight mean of the available member returns.

    member_rets maps MAG7 member symbols to their window returns
    (decimal: 0.01 = 1%). Missing members (absent key or None/non-numeric
    value) are DROPPED. Abstains (None) when fewer than
    COMPLEX_MIN_MEMBERS of the 7 members are available. Returns None on
    an empty/None input.
    """
    if not member_rets:
        return None
    vals = []
    for sym in MAG7_MEMBERS:
        v = member_rets.get(sym)
        if v is None:
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    if len(vals) < COMPLEX_MIN_MEMBERS:
        return None
    return sum(vals) / len(vals)


def pair_divergence(leg_a_ret: Optional[float], leg_b_ret: Optional[float],
                    window: str,
                    fire_pct: float = DIVERGENCE_FIRE_PCT) -> dict:
    """Spread between two legs over the window, in pct, plus the rich leg.

    Returns {spread_pct, rich_leg, fired, window}. Inputs are decimal
    returns (0.01 = 1%); spread_pct = (leg_a_ret - leg_b_ret) x 100.
    rich_leg = the OUTPERFORMER ("leg_a" when spread > 0, "leg_b" when
    spread < 0, "none" at an exact tie) — RV doctrine sells the rich leg
    and buys the cheap one. Abstains (spread_pct None, fired False) on a
    missing/degenerate leg. `window` is a label carried through for the
    telemetry row (e.g. "5d") — the caller measures the returns.
    fired: |spread_pct| >= fire_pct (default 2.0).
    """
    a = _as_float(leg_a_ret)
    b = _as_float(leg_b_ret)
    if a is None or b is None:
        return {"spread_pct": None, "rich_leg": "none",
                "fired": False, "window": window}
    spread = round((a - b) * 100.0, 6)
    if spread > 0:
        rich = "leg_a"
    elif spread < 0:
        rich = "leg_b"
    else:
        rich = "none"
    return {"spread_pct": spread, "rich_leg": rich,
            "fired": abs(spread) >= fire_pct, "window": window}


def _as_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
