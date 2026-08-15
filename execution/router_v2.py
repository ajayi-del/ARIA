"""
execution/router_v2.py — Mode 4: THE STRATEGIST (Sun Tzu), Phase C shadow.

"Every battle is won before it is fought."

Today execution/venue.py routes by static symbol partition. Router v2
scores both venues per symbol+direction and the shadow loop LOGS the
would-be decision (structlog router_v2_shadow) without touching dispatch.
Same doctrine as the Dreamer: the shadow log proves the scorer's choices
before it ever earns dispatch rights.

    score(venue) = −fee_bps − carry_bps − health_bps   (higher wins)

  fee:    live taker rate in bps at entry style (taker = honest default)
  carry:  signed 8h funding we would PAY (bps); negative = we receive.
          SoDEX side uses the Bybit perp rate as documented proxy (SoDEX
          perps track the deep market); Aster side uses its native
          markPrice-stream rate.
  health: feed staleness tax — 0.5 bps/s beyond 5s, capped at 20 bps.
          Unlisted symbol or halted sleeve → −999 (never chosen).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

_NEVER = -999.0
_STALE_GRACE_S = 5.0
_STALE_BPS_PER_S = 0.5
_STALE_CAP_BPS = 20.0


class RouterV2:
    """Pure scorer — no state, no I/O. The shadow loop feeds it context."""

    def score_venue(self, *, fee_bps: float, funding_rate: float,
                    direction: str, feed_age_s: Optional[float],
                    listed: bool = True, halted: bool = False) -> float:
        if halted or not listed:
            return _NEVER
        carry_bps = funding_rate * 1e4 * (1.0 if direction == "long" else -1.0)
        health_bps = 0.0
        if feed_age_s is None:
            health_bps = _STALE_CAP_BPS          # no data = worst-case tax
        elif feed_age_s > _STALE_GRACE_S:
            health_bps = min(_STALE_CAP_BPS,
                             (feed_age_s - _STALE_GRACE_S) * _STALE_BPS_PER_S)
        return -(fee_bps) - carry_bps - health_bps

    def compare(self, symbol: str, direction: str, static_venue: str,
                ctx: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """ctx: venue → score_venue kwargs. Returns the shadow verdict."""
        scores = {v: self.score_venue(**c) for v, c in ctx.items()}
        choice = max(scores, key=scores.get) if scores else static_venue
        return {
            "symbol": symbol, "direction": direction,
            "static_venue": static_venue, "v2_choice": choice,
            "scores": {v: round(s, 2) for v, s in scores.items()},
            "delta_bps": round(scores.get(choice, 0.0)
                               - scores.get(static_venue, 0.0), 2),
            "diverges": choice != static_venue,
        }
