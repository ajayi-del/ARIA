"""intelligence/kline_confidence.py — Epistemic gate input (pure brain).

2026-09-19: rally graduation needs to know how much the kline/mark evidence
plane can be trusted BEFORE it graduates a symbol. This brain collapses the
four trust inputs (buffer depth, candle floor, mark-scale quarantine, mark
staleness) into one bounded confidence multiplier. The graduation logic
(Agent 2's side) decides what to do with the number; this module only
measures. Fail-open: no buffer count → abstain (None), never fabricate.
"""
from __future__ import annotations

from typing import Optional

FULL_BUFFER = 55.0        # the 55-bar boot seed = full evidence
STALE_MARK_S = 90.0       # mark-scale sentinel's own staleness bound


def kline_confidence(
    buf_count: Optional[int],
    min_candles: Optional[int],
    quarantined: Optional[bool],
    mark_age_s: Optional[float],
) -> Optional[float]:
    """Confidence in [0, 1] that the kline/mark plane is telling the truth.
    None when buf_count is unknown (abstain)."""
    if buf_count is None:
        return None
    try:
        n = float(buf_count)
    except (TypeError, ValueError):
        return None
    conf = min(n / FULL_BUFFER, 1.0)
    if quarantined:
        return 0.0        # a split plane carries zero information
    if min_candles and n < float(min_candles):
        conf *= 0.5
    if mark_age_s is not None:
        try:
            if float(mark_age_s) > STALE_MARK_S:
                conf *= 0.3
        except (TypeError, ValueError):
            pass          # unreadable age = unknown, not stale
    return conf
