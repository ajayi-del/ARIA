"""Band clock — zero-I/O brain for the Arcus RWA trading-band state machine.

Department template: pure verdict functions + an injected-state tracker. The
feed (data/arcus_feed.py) transports marketAttributes entries; this module
owns interpretation. No I/O, no clocks of its own (now_s injected), no
consumption sites — the consumers (scalper, session-bridge coherence, vol
forecast) are wired in later phases behind the arcus_band_clock_live_enabled
kill switch.

Venue mechanics (docs.arcus.xyz/api-reference/market-data/marketattributes):
  - RTH 04:00–20:00 ET. At RTH exit the 5-min impact-price EWMA seals as
    currentSettlementPrice = the off-hours ANCHOR.
  - Off-hours band = anchor ± anchor × offHoursInitialMarginFraction × rung,
    rung walking 0.5×→1×→2×→4× ONE-WAY per session (never contracts).
  - A side enters its expansion zone when the 1h EWMA holds within 90% of its
    boundary; the zone clock (zoneEnteredAt + 1h) completing while still
    in-zone widens that side one rung (tradingBoundExpanded).
  - Every entry is the market's COMPLETE block (fields null, never omitted).

Why this matters (the trade that only exists here): the band can only WIDEN
intra-session, so a freshly expanded boundary is a one-way door — price at
the new edge has asymmetric room back to the sealed anchor. SoDEX equity
marks freeze at RTH exit (documented wound); Arcus RWA perps trade through.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

RUNGS = (0.5, 1.0, 2.0, 4.0)


def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def band_rung(entry: Dict[str, Any]) -> Optional[float]:
    """Estimate the current rung (0.5/1/2/4) from band geometry:
    half_width = anchor × imf × rung. Snaps to the nearest declared rung;
    returns None when geometry is incomplete (in-RTH, null fields)."""
    anchor = _f(entry.get("currentSettlementPrice"))
    imf = _f(entry.get("offHoursInitialMarginFraction"))
    upper = _f(entry.get("upperTradingBound"))
    lower = _f(entry.get("lowerTradingBound"))
    if not anchor or not imf or not upper or not lower or anchor <= 0 \
            or imf <= 0 or upper <= lower:
        return None
    raw = ((upper - lower) / 2.0) / (anchor * imf)
    return min(RUNGS, key=lambda r: abs(r - raw))


def dist_to_bounds_pct(entry: Dict[str, Any],
                       mark: float) -> Dict[str, Optional[float]]:
    """Signed room to each edge as % of mark. Positive = room remaining.
    None legs when bounds are null (in-RTH)."""
    upper = _f(entry.get("upperTradingBound"))
    lower = _f(entry.get("lowerTradingBound"))
    if mark <= 0:
        return {"upper_pct": None, "lower_pct": None}
    return {
        "upper_pct": ((upper - mark) / mark * 100.0) if upper else None,
        "lower_pct": ((mark - lower) / mark * 100.0) if lower else None,
    }


def expansion_imminent(entry: Dict[str, Any], now_s: float,
                       within_s: float = 600.0) -> Dict[str, bool]:
    """A side's zone clock completes at expectedExpansionAt; 'imminent' = the
    completion is within `within_s` AND the side is still in-zone (the docs
    warn the clock is not a guarantee — EWMA must hold)."""
    def _side(zone_flag: str, expected: str) -> bool:
        if entry.get(zone_flag) is not True:
            return False
        exp = entry.get(expected)
        if not isinstance(exp, (int, float)) or exp <= 0:
            return False
        return 0.0 <= (exp - now_s) <= within_s

    return {
        "upper": _side("isUpperInExpansionZone", "upperExpectedExpansionAt"),
        "lower": _side("isLowerInExpansionZone", "lowerExpectedExpansionAt"),
    }


def anchor_reversion_verdict(
        entry: Dict[str, Any], mark: float, now_s: float,
        edge_proximity_pct: float = 0.35,
        min_room_pct: float = 0.3) -> Dict[str, Any]:
    """The one-way-door trade: armed when price sits at a band edge off-hours
    with ≥min_room_pct back to the sealed anchor. Direction is TOWARD the
    anchor; stop geometry belongs to the caller (sealed anchor ∓ ATR).

    Fail-closed: any missing geometry → armed False with a reason. The
    one-way ratchet (rungs never contract intra-session) is what bounds the
    adverse tail — in-RTH entries never arm (bands don't bind there)."""
    out = {"armed": False, "direction": None, "anchor": None,
           "room_pct": None, "rung": band_rung(entry), "reason": ""}
    if entry.get("isOutsideRth") is not True:
        out["reason"] = "in_rth_or_unknown"
        return out
    anchor = _f(entry.get("currentSettlementPrice"))
    if not anchor or anchor <= 0 or mark <= 0:
        out["reason"] = "no_anchor"
        return out
    d = dist_to_bounds_pct(entry, mark)
    room = (mark - anchor) / mark * 100.0
    if d["upper_pct"] is not None and d["upper_pct"] <= edge_proximity_pct \
            and room >= min_room_pct:
        out.update(armed=True, direction="short", anchor=anchor,
                   room_pct=round(room, 4), reason="at_upper_bound")
        return out
    room = (anchor - mark) / mark * 100.0
    if d["lower_pct"] is not None and d["lower_pct"] <= edge_proximity_pct \
            and room >= min_room_pct:
        out.update(armed=True, direction="long", anchor=anchor,
                   room_pct=round(room, 4), reason="at_lower_bound")
        return out
    out["reason"] = "mid_band"
    return out


def session_bridge_verdict(entry: Dict[str, Any],
                           mark: float) -> Dict[str, Any]:
    """Overnight drift vs the sealed anchor — the gap-direction prior for the
    SoDEX equity perp whose mark froze at RTH exit. Positive gap_pct = Arcus
    overnight price above the sealed anchor."""
    anchor = _f(entry.get("currentSettlementPrice"))
    if not anchor or anchor <= 0 or mark <= 0:
        return {"valid": False, "gap_pct": None, "direction": None,
                "is_outside_rth": entry.get("isOutsideRth")}
    gap = (mark - anchor) / anchor * 100.0
    return {"valid": True, "gap_pct": round(gap, 4),
            "direction": "long" if gap > 0 else "short" if gap < 0 else None,
            "is_outside_rth": entry.get("isOutsideRth")}


class BandClock:
    """Per-market band state tracker. Ingests raw marketAttributes entries
    (complete blocks — wholesale replace per venue contract) and answers
    verdicts. Zero I/O: callers inject marks and now_s."""

    def __init__(self) -> None:
        self._by_name: Dict[str, Dict[str, Any]] = {}
        self._events: List[Dict[str, Any]] = []   # boundEvent ring, capped
        self._event_cap = 500

    def ingest_entry(self, entry: Dict[str, Any],
                     is_snapshot: bool = False) -> Optional[Dict[str, Any]]:
        name = entry.get("marketDisplayName")
        if not name:
            return None
        self._by_name[name] = entry
        ev = entry.get("boundEvent")
        if ev and not is_snapshot:
            rec = {"event": ev, "side": entry.get("boundSide"),
                   "market": name, "ts_us": entry.get("timestamp"),
                   "rung": band_rung(entry)}
            self._events.append(rec)
            if len(self._events) > self._event_cap:
                self._events = self._events[-self._event_cap:]
            return rec
        return None

    def entry(self, symbol: str) -> Optional[Dict[str, Any]]:
        return self._by_name.get(symbol)

    def verdict(self, symbol: str, mark: float,
                now_s: float) -> Dict[str, Any]:
        e = self._by_name.get(symbol)
        if e is None:
            return {"tracked": False}
        return {
            "tracked": True,
            "is_outside_rth": e.get("isOutsideRth"),
            "rung": band_rung(e),
            "dist": dist_to_bounds_pct(e, mark),
            "expansion_imminent": expansion_imminent(e, now_s),
            "anchor_reversion": anchor_reversion_verdict(e, mark, now_s),
            "session_bridge": session_bridge_verdict(e, mark),
        }

    def recent_events(self, symbol: Optional[str] = None,
                      limit: int = 50) -> List[Dict[str, Any]]:
        evs = self._events if symbol is None else \
            [e for e in self._events if e["market"] == symbol]
        return evs[-limit:]

    def expansion_rate(self, symbol: str, window_us: int,
                       now_us: float) -> int:
        """Count of tradingBoundExpanded events in the window — the realized-
        vol proxy the SoDEX stop-widening consumer reads."""
        lo = now_us - window_us
        return sum(1 for e in self._events
                   if e["market"] == symbol
                   and e["event"] == "tradingBoundExpanded"
                   and isinstance(e.get("ts_us"), (int, float))
                   and e["ts_us"] >= lo)
