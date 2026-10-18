"""
intelligence/etf_flow_ratio.py — ETF flow teeth (Governor directive
2026-10-17, "STOCK EXECUTION ARCHITECTURE" spec, piece 1). SHADOW-FIRST:
score and emit only — never changes trading behavior.

The problem: raw ETF flow USD numbers are meaningless without scale. A
+$300M day into BTC ETFs and a +$300M day into ETH ETFs are different
animals — AUM differs by an order of magnitude. This brain normalizes
flow by assets under management and percentile-ranks the ratio against
the feed's OWN history so a "big flow day" is comparable across
BTC/ETH/SOL ETFs.

Zero-I/O (department template): no network, no files, no logging. The
splice injects flow_usd / aum_usd (BitGuard row fields total_net_inflow
/ total_net_assets, SoSoValue row shape) and the accumulated ratio
history; the brain returns telemetry dicts the splice emits.

Inputs to the splice (single accessor _etf_flow, main.py): the freshest-
wins fallback chain already serves rows with total_net_inflow and
total_net_assets — this module consumes those numbers, never the feed.

Doctrines:
  - Magnitude ranking: the percentile is computed on |ratio| (a big
    OUTFLOW day is as informative as a big inflow day — Hasbrouck:
    signed direction is a separate variable, not a separate signal).
    `direction` carries the sign ("inflow"/"outflow").
  - Honest abstain (Aronson): missing/zero/negative AUM → abstain; a
    ratio built on a broken denominator is worse than no ratio. Fewer
    than MIN_HISTORY_POINTS past ratios → abstain; a percentile against
    thin history is fake precision.
  - Fire threshold: FLOW_FIRE_PERCENTILE = 90 — only the top decile of
    the feed's own history counts as a "big flow day". Bounded, fixed,
    never self-tuning (bound every degree of freedom).

Kill switch: env ETF_FLOW_RATIO_ENABLED (default "true"). False = the
splice stands down; pre-module system bit-for-bit.

Splice event: "etf_flow_ratio_signal" with fields {asset, ratio,
percentile, direction, fired}.

TIER-1 FLOW DIRECTION (2026-10-18 Governor BUILD ORDER, ETF acceleration):
while the percentile path abstains on thin history (< MIN_HISTORY_POINTS),
a sign-only fallback grants a LONG bonus on inflow days. ADDITIVE-ONLY
doctrine: flow_direction_signal fires ONLY for positive flow — an inflow
day grants a long bonus; outflow days grant NOTHING, never a penalty.
When the percentile path is live (20+ points), Tier-1 is inert — no
double-count. Splice event: "etf_flow_direction_signal" with fields
{asset, flow_usd, direction, fired, tier: 1}. Actuation knob:
ETF_FLOW_TIER1_BONUS (default 0.2, half the Tier-2 ETF_FLOW_BONUS 0.4).
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

ETF_FLOW_RATIO_EVENT = "etf_flow_ratio_signal"
ETF_FLOW_DIRECTION_EVENT = "etf_flow_direction_signal"
FLOW_FIRE_PERCENTILE = 90.0     # top decile of the feed's own history fires
MIN_HISTORY_POINTS = 20         # below this a percentile is fake precision


def etf_flow_ratio_enabled() -> bool:
    """Env kill switch helper. Default on; 'false' (any case) stands down."""
    return os.environ.get("ETF_FLOW_RATIO_ENABLED", "true").lower() != "false"


def flow_ratio(asset: str, flow_usd: Optional[float],
               aum_usd: Optional[float]) -> Tuple[Optional[float], Optional[str]]:
    """flow_usd / aum_usd. Returns (ratio, None) or (None, reason).

    Abstains on missing flow or missing/zero/negative AUM — the honest
    null beats a ratio on a broken denominator.
    """
    if flow_usd is None:
        return None, "no_flow"
    try:
        flow = float(flow_usd)
    except (TypeError, ValueError):
        return None, "bad_flow"
    if aum_usd is None:
        return None, "no_aum"
    try:
        aum = float(aum_usd)
    except (TypeError, ValueError):
        return None, "bad_aum"
    if aum <= 0:
        return None, "zero_aum"
    return flow / aum, None


def flow_percentile(asset: str, ratio: Optional[float],
                    history: Optional[List[float]]) -> Tuple[Optional[float], Optional[str]]:
    """Percentile (0-100) of |ratio| against injected |ratio| history.

    percentile = 100 * (# history values strictly below |ratio|) / n.
    Abstains when history has fewer than MIN_HISTORY_POINTS points or the
    ratio itself is missing. History values are past |ratio| magnitudes —
    the caller accumulates one per feed day.
    """
    if ratio is None:
        return None, "no_ratio"
    mag = abs(float(ratio))
    hist = []
    for h in (history or []):
        try:
            hist.append(abs(float(h)))
        except (TypeError, ValueError):
            continue
    if len(hist) < MIN_HISTORY_POINTS:
        return None, "thin_history"
    below = sum(1 for h in hist if h < mag)
    return 100.0 * below / len(hist), None


def flow_signal(asset: str, flow_usd: Optional[float],
                aum_usd: Optional[float],
                history: Optional[List[float]],
                fire_percentile: float = FLOW_FIRE_PERCENTILE) -> dict:
    """Composite verdict for the splice: {ratio, percentile, fired,
    direction}. Never raises — every abstain path returns fired=False
    with the reason recorded.

    direction: "inflow" | "outflow" | "unknown" (from the sign of
    flow_usd; zero flow reads "unknown" — no direction on no flow).
    fired: percentile >= fire_percentile (default 90 = top decile).
    """
    ratio, ratio_reason = flow_ratio(asset, flow_usd, aum_usd)
    pct, pct_reason = flow_percentile(asset, ratio, history)
    direction = "unknown"
    if flow_usd is not None:
        try:
            f = float(flow_usd)
            if f > 0:
                direction = "inflow"
            elif f < 0:
                direction = "outflow"
        except (TypeError, ValueError):
            pass
    fired = pct is not None and pct >= fire_percentile
    return {
        "asset": asset,
        "ratio": ratio,
        "percentile": pct,
        "direction": direction,
        "fired": fired,
        "reason": ratio_reason or pct_reason,
    }


# ---- Tier-1: sign-only flow direction (thin-history fallback) ----

def flow_direction_signal(asset: str, flow_usd: Optional[float]) -> dict:
    """Tier-1 direction verdict: {asset, flow_usd, direction, fired, tier}.

    ADDITIVE-ONLY doctrine: fired=True ONLY for positive flow — an inflow
    day grants a long bonus; outflow and zero-flow days grant nothing,
    never a penalty (direction still reports the sign for the shadow
    record). Missing/garbage flow = fired False, direction "unknown".
    Never raises — the honest null beats a guessed direction.
    """
    direction = "unknown"
    fired = False
    flow: Optional[float] = None
    try:
        if flow_usd is not None:
            flow = float(flow_usd)
            if flow > 0:
                direction = "inflow"
                fired = True
            elif flow < 0:
                direction = "outflow"
    except (TypeError, ValueError):
        flow = None
    return {
        "asset": asset,
        "flow_usd": flow,
        "direction": direction,
        "fired": fired,
        "tier": 1,
    }
