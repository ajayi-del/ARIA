"""Arcus scalper brain — zero-I/O department (docs/DEPARTMENT_TEMPLATE.md).

The maker-only scalping brain for the Arcus venue. Arcus charges 0% maker /
0.0225% taker (beta), so the ENTIRE edge is fill quality and adverse
selection, not fee avoidance — every entry this brain arms is a resting
maker limit; taker entries are refused by construction (reason=would_cross).

Inputs (all injected — no network, no clocks, no file reads):
  - band-clock verdicts: the dict returned by
    intelligence/band_clock.BandClock.verdict(symbol, mark, now_s)
    ({tracked, is_outside_rth, rung, dist, expansion_imminent,
      anchor_reversion, session_bridge}). Absolute band edges are
    reconstructed from mark + dist pct (verdict carries no absolutes).
  - BBO book state: {"bid","ask","bid_qty","ask_qty","ts"} (ArcusFeed.book).
  - Taker flow events ingested into FlowWindows (taker_buy None = stale BBO
    inference, ignored — the feed contract).
  - Predicted 1h funding (decimal): carried into shadow economics only; at
    the 240s scalp horizon funding is a cohort label, not a gate.

Scalp verdict classes (each a pure function → verdict dict with
armed/direction/entry/limit_price/stop/grade/reason + a shadow record —
shadow-from-birth: every verdict, armed or refused, carries the
counterfactual geometry for the integrator's journal):
  a. band_edge_verdict — off-hours RWA anchor reversion. band_clock
     anchor_reversion armed AND the BBO confirms the touch at the edge →
     maker limit ONE tick inside the edge, direction toward the sealed
     anchor. Grade A when room_pct ≥ 0.4 and rung ≤ 1.0 (the one-way
     ratchet bounds the adverse tail at tight rungs); B otherwise.
  b. spread_capture_verdict — in-RTH spread capture. spread ≥ 2 ticks AND
     taker-flow imbalance over the last N directional fills agrees AND
     microprice bias confirms → join the bid/ask. Grade A only when book
     depth at the touch ≥ 3× intended size; B ≥ 1×; C below (thin book is
     still a valid maker attempt at 0% fee — adverse selection is graded,
     not refused).
  c. expansion_fade_verdict — expansion_imminent on a side + price pinned
     at that edge → pre-position the reversion limit BEFORE the widening
     event (the one-way door: after tradingBoundExpanded the new edge is a
     magnet back to anchor). Grade B by construction (event risk).

Sizing (Governor doctrine): band-bridge maker entries and isolated probes
may use up to 15x; anything taker-entered or cross-margin caps at 8x.
Notional = risk_usd / stop_frac, capped at equity × leverage, floored at
the $5 venue min notional. volume_projection is a FUNCTION of
(trades_per_day, avg_notional) — the ~$100k/day venue-volume target is
math, never a constant.

Lifecycle (manage_scalp): time-stop 240s (inherited scalper doctrine),
breakeven floor armed after the first favorable tick + the fee offset
(fee-aware rungs, 2026-11-02 — raw-tick rungs priced the floor BELOW the
post-promo 6bp round-trip fee, every "win" net-negative), abort on a band
event adverse to the position (the ratchet just moved against us), stop
touch → market, target → limit. Missing state → hold (fail closed: never
act on dark data).

ScalpRegistry: open-scalp dedup (one per symbol), concurrent cap from
config (Governor 2026-11-02: 13), per-class daily counters, loss cooloff
per symbol (5 consecutive losses → 1h park; Governor 2026-10-29, was 2).
Fill-confirmed entry (2026-11-02): a resting PostOnly is NOT a position —
register_entry_pending parks the record until the venue shows the fill;
confirm_entry opens the track on VENUE qty/entry. Pyramid adds fold
through the same pending plane (kind=add → VWAP re-anchor on confirm).
All injected state, no I/O.
"""

from __future__ import annotations

import math
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

# ── Doctrine constants ────────────────────────────────────────────────────
TIME_STOP_S = 240.0             # inherited scalper force-close horizon
MAX_CONCURRENT = 3              # inherited doctrine (parasite scalper)

# Profit-floor ladder (Governor 2026-10-29: "profit-taking rungs like
# SoDEX"). peak_fav IS the scalp's MFE plane — once the favorable
# excursion reaches a rung (in ticks), the floor tightens to lock that
# fraction of the peak. Tighten-only, breakeven floor is rung zero.
FLOOR_RUNGS = ((3.0, 0.40), (6.0, 0.60))   # (peak margin ticks → lock frac)
EXIT_CONFIRM_S = 120.0          # resting exit limit older than this → market out
LOSS_COOLOFF_S = 3600.0         # 5 consecutive losses → 1h symbol park
LOSS_STREAK = 5                 # Governor 2026-10-29: was 2
MIN_NOTIONAL_USD = 5.0          # Arcus venue minimum
LEVERAGE_MAKER_ISOLATED = 15    # Governor cap; band-bridge maker + probes
LEVERAGE_TAKER_CROSS = 8        # taker-entered or cross-margin

# Fill-confirmed entry + harvest/pyramid (Governor 2026-11-02: "no take
# profits... fix" + "keep up to 13 concurrent trades... the take profit
# basket and harvest should be able to harvest with this kind of trades as
# well as the pyramid").
ENTRY_FILL_TIMEOUT_S = 20.0     # unfilled PostOnly entry older than this → cancel
BASKET_TARGET_ROE_PCT = 6.0     # small-account stack-cap doctrine (book ROE)
BASKET_MIN_PROFIT_USD = 1.0     # treasury $1 min-harvest guard idiom
BASKET_COOLOFF_S = 300.0        # one basket harvest per book per window
PYRAMID_ADD_FRAC = 0.5          # size off the BASE qty, never equity
PYRAMID_MAX_ADDS = 1            # one add ever per scalp

EDGE_PROXIMITY_PCT = 0.35       # mirrors band_clock edge_proximity default
GRADE_A_MIN_ROOM_PCT = 0.4      # band-edge grade-A room floor
GRADE_A_MAX_RUNG = 1.0          # tight band = bounded adverse tail
BOOK_MAX_AGE_S = 5.0            # BBO staleness → fail closed
STOP_PAD_PCT = 0.10             # stop pad beyond the edge/touch, % of mark
STOP_PAD_MIN_TICKS = 2.0        # ...but never tighter than 2 ticks

SPREAD_MIN_TICKS = 2.0          # spread-capture arming floor
FLOW_MIN_IMBALANCE = 0.3        # taker-flow agreement floor
FLOW_WINDOW_N = 10              # last-N directional fills
FLOW_MIN_FILLS = 4              # below → imbalance None (dark)
DEPTH_MULT_A = 3.0              # grade-A depth = 3× intended at the touch
DEPTH_MULT_B = 1.0              # grade-B depth floor; below → C

TARGET_VOLUME_DAILY_USD = 100_000.0   # fund venue-volume target


def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _to_grid(price: float, tick: float) -> Optional[float]:
    """Round to the tick grid. None on degenerate inputs (fail-closed)."""
    if price <= 0 or tick <= 0:
        return None
    return round(round(price / tick) * tick, 10)


def _book_ok(book: Optional[Dict[str, Any]], now_s: float,
             max_age_s: float = BOOK_MAX_AGE_S) -> bool:
    if not isinstance(book, dict):
        return False
    bid, ask = _f(book.get("bid")), _f(book.get("ask"))
    ts = _f(book.get("ts"))
    if not bid or not ask or bid <= 0 or ask <= bid or ts is None:
        return False
    return 0.0 <= (now_s - ts) <= max_age_s


def _edge_price(mark: float, dist_pct: Optional[float], side: str
                ) -> Optional[float]:
    """Reconstruct the absolute band edge from mark + signed room pct."""
    if mark <= 0 or dist_pct is None:
        return None
    return mark * (1.0 + dist_pct / 100.0) if side == "upper" \
        else mark * (1.0 - dist_pct / 100.0)


def _shadow(gate: str, cls: str, verdict: Dict[str, Any]) -> Dict[str, Any]:
    """The counterfactual record every verdict carries from birth."""
    return {
        "gate": gate, "cls": cls, "grade": verdict.get("grade"),
        "armed": verdict.get("armed", False),
        "direction": verdict.get("direction"),
        "limit_price": verdict.get("limit_price"),
        "stop": verdict.get("stop"), "reason": verdict.get("reason"),
    }


# ── 1a. Band-edge anchor reversion (off-hours RWA) ───────────────────────

def band_edge_verdict(band: Dict[str, Any], mark: float,
                      book: Optional[Dict[str, Any]], tick: float,
                      now_s: float, *,
                      edge_proximity_pct: float = EDGE_PROXIMITY_PCT,
                      grade_a_room_pct: float = GRADE_A_MIN_ROOM_PCT,
                      grade_a_max_rung: float = GRADE_A_MAX_RUNG,
                      stop_pad_pct: float = STOP_PAD_PCT) -> Dict[str, Any]:
    """The one-way-door scalp. band_clock anchor_reversion must be armed
    (off-hours + edge proximity + room already proven there); this layer
    adds BBO confirmation of the touch and maker-safe geometry."""
    out: Dict[str, Any] = {"armed": False, "cls": "band_edge",
                           "direction": None, "entry": None,
                           "limit_price": None, "stop": None,
                           "grade": None, "reason": ""}
    ar = (band or {}).get("anchor_reversion") or {}
    if ar.get("armed") is not True:
        out["reason"] = "anchor_reversion_not_armed"
        out["shadow"] = _shadow("band_edge_anchor_reversion", "band_edge", out)
        return out
    mark = _f(mark) or 0.0
    if mark <= 0 or tick <= 0:
        out["reason"] = "bad_mark_or_tick"
        out["shadow"] = _shadow("band_edge_anchor_reversion", "band_edge", out)
        return out
    if not _book_ok(book, now_s):
        out["reason"] = "no_book"
        out["shadow"] = _shadow("band_edge_anchor_reversion", "band_edge", out)
        return out
    direction = ar.get("direction")
    side = "upper" if direction == "short" else "lower"
    dist = (band or {}).get("dist") or {}
    edge = _edge_price(mark, _f(dist.get(f"{side}_pct")), side)
    if edge is None or edge <= 0:
        out["reason"] = "no_edge"
        out["shadow"] = _shadow("band_edge_anchor_reversion", "band_edge", out)
        return out
    # BBO must confirm the touch: mid within edge_proximity of the edge.
    bid, ask = float(book["bid"]), float(book["ask"])
    mid = (bid + ask) / 2.0
    mid_dist_pct = abs(edge - mid) / mid * 100.0
    if mid_dist_pct > edge_proximity_pct:
        out["reason"] = "bbo_no_touch"
        out["shadow"] = _shadow("band_edge_anchor_reversion", "band_edge", out)
        return out
    # Maker limit ONE tick inside the edge, on-grid.
    raw = edge - tick if direction == "short" else edge + tick
    limit = _to_grid(raw, tick)
    if limit is None:
        out["reason"] = "bad_mark_or_tick"
        out["shadow"] = _shadow("band_edge_anchor_reversion", "band_edge", out)
        return out
    # Never arm a taker: short limit must sit at/above the bid, long at/below
    # the ask. Crossing would pay taker fees AND eat adverse selection.
    if direction == "short" and limit < bid:
        out["reason"] = "would_cross"
        out["shadow"] = _shadow("band_edge_anchor_reversion", "band_edge", out)
        return out
    if direction == "long" and limit > ask:
        out["reason"] = "would_cross"
        out["shadow"] = _shadow("band_edge_anchor_reversion", "band_edge", out)
        return out
    pad = max(stop_pad_pct / 100.0 * mark, STOP_PAD_MIN_TICKS * tick)
    stop = _to_grid(edge + pad if direction == "short" else edge - pad, tick)
    room = _f(ar.get("room_pct")) or 0.0
    rung = _f(ar.get("rung"))
    grade = "A" if (room >= grade_a_room_pct
                    and rung is not None and rung <= grade_a_max_rung) else "B"
    out.update(armed=True, direction=direction, entry=limit,
               limit_price=limit, stop=stop, grade=grade,
               reason=str(ar.get("reason") or "armed"))
    out["shadow"] = _shadow("band_edge_anchor_reversion", "band_edge", out)
    return out


# ── 1b. Spread-capture scalp (in-RTH) ────────────────────────────────────

def spread_capture_verdict(book: Optional[Dict[str, Any]], tick: float,
                           flow_imbalance: Optional[float], now_s: float, *,
                           intended_qty: Optional[float] = None,
                           is_outside_rth: Optional[bool] = None,
                           min_spread_ticks: float = SPREAD_MIN_TICKS,
                           flow_min: float = FLOW_MIN_IMBALANCE,
                           depth_mult_a: float = DEPTH_MULT_A,
                           depth_mult_b: float = DEPTH_MULT_B,
                           stop_pad_pct: float = STOP_PAD_PCT) -> Dict[str, Any]:
    """Join the bid/ask when the spread pays for the adverse selection and
    the last-N-fills taker imbalance + microprice agree on direction."""
    out: Dict[str, Any] = {"armed": False, "cls": "spread_capture",
                           "direction": None, "entry": None,
                           "limit_price": None, "stop": None,
                           "grade": None, "reason": ""}
    gate = "spread_capture"
    if is_outside_rth is True:
        out["reason"] = "off_hours"
        out["shadow"] = _shadow(gate, "spread_capture", out)
        return out
    if not _book_ok(book, now_s) or tick <= 0:
        out["reason"] = "no_book"
        out["shadow"] = _shadow(gate, "spread_capture", out)
        return out
    bid, ask = float(book["bid"]), float(book["ask"])
    # Epsilon: (100.02 - 100.00)/0.01 == 1.999999999999602 in IEEE — a
    # genuine 2-tick spread must not read thin on float dust.
    spread_ticks = (ask - bid) / tick
    if spread_ticks + 1e-6 < min_spread_ticks:
        out["reason"] = "spread_too_thin"
        out["shadow"] = _shadow(gate, "spread_capture", out)
        return out
    imb = _f(flow_imbalance)
    if imb is None or abs(imb) < flow_min:
        out["reason"] = "flow_dark_or_weak"
        out["shadow"] = _shadow(gate, "spread_capture", out)
        return out
    direction = "long" if imb > 0 else "short"
    # Microprice confirmation: bid-heavy book pulls microprice toward the ask
    # (up-pressure) — must agree with the flow direction.
    bq, aq = _f(book.get("bid_qty")) or 0.0, _f(book.get("ask_qty")) or 0.0
    if bq > 0 and aq > 0:
        micro = (bid * aq + ask * bq) / (bq + aq)
        mid = (bid + ask) / 2.0
        if (direction == "long" and micro < mid) or \
                (direction == "short" and micro > mid):
            out["reason"] = "microprice_disagrees"
            out["shadow"] = _shadow(gate, "spread_capture", out)
            return out
    limit = bid if direction == "long" else ask
    pad = max(stop_pad_pct / 100.0 * limit, STOP_PAD_MIN_TICKS * tick)
    stop = _to_grid(limit - pad if direction == "long" else limit + pad, tick)
    # Grade on depth at the touch vs intended size.
    depth = bq if direction == "long" else aq
    iq = _f(intended_qty)
    if iq is None or iq <= 0:
        grade = "B"  # depth unverifiable — never grade A blind
    elif depth >= depth_mult_a * iq:
        grade = "A"
    elif depth >= depth_mult_b * iq:
        grade = "B"
    else:
        grade = "C"
    out.update(armed=True, direction=direction, entry=limit,
               limit_price=limit, stop=stop, grade=grade,
               reason="spread_capture")
    out["shadow"] = _shadow(gate, "spread_capture", out)
    return out


# ── 1c. Expansion-clock fade (pre-position the one-way door) ─────────────

def expansion_fade_verdict(band: Dict[str, Any], mark: float,
                           book: Optional[Dict[str, Any]], tick: float,
                           now_s: float, *,
                           edge_proximity_pct: float = EDGE_PROXIMITY_PCT,
                           stop_pad_pct: float = STOP_PAD_PCT) -> Dict[str, Any]:
    """expansion_imminent on a side + price pinned at that edge → rest the
    reversion limit before the widening event. Grade B by construction."""
    out: Dict[str, Any] = {"armed": False, "cls": "expansion_fade",
                           "direction": None, "entry": None,
                           "limit_price": None, "stop": None,
                           "grade": None, "reason": ""}
    gate = "expansion_clock_fade"
    band = band or {}
    exp = band.get("expansion_imminent") or {}
    side = "upper" if exp.get("upper") else "lower" if exp.get("lower") \
        else None
    if side is None:
        out["reason"] = "no_expansion_clock"
        out["shadow"] = _shadow(gate, "expansion_fade", out)
        return out
    mark = _f(mark) or 0.0
    if mark <= 0 or tick <= 0:
        out["reason"] = "bad_mark_or_tick"
        out["shadow"] = _shadow(gate, "expansion_fade", out)
        return out
    if not _book_ok(book, now_s):
        out["reason"] = "no_book"
        out["shadow"] = _shadow(gate, "expansion_fade", out)
        return out
    dist = band.get("dist") or {}
    d = _f(dist.get(f"{side}_pct"))
    if d is None or d > edge_proximity_pct:
        out["reason"] = "not_at_edge"
        out["shadow"] = _shadow(gate, "expansion_fade", out)
        return out
    ar = band.get("anchor_reversion") or {}
    direction = "short" if side == "upper" else "long"
    # The fade only makes sense with room back to the sealed anchor.
    if ar.get("direction") != direction or not (_f(ar.get("room_pct")) or 0):
        out["reason"] = "no_anchor_room"
        out["shadow"] = _shadow(gate, "expansion_fade", out)
        return out
    edge = _edge_price(mark, d, side)
    if edge is None or edge <= 0:
        out["reason"] = "no_edge"
        out["shadow"] = _shadow(gate, "expansion_fade", out)
        return out
    bid, ask = float(book["bid"]), float(book["ask"])
    raw = edge - tick if direction == "short" else edge + tick
    limit = _to_grid(raw, tick)
    if limit is None:
        out["reason"] = "bad_mark_or_tick"
        out["shadow"] = _shadow(gate, "expansion_fade", out)
        return out
    if (direction == "short" and limit < bid) or \
            (direction == "long" and limit > ask):
        out["reason"] = "would_cross"
        out["shadow"] = _shadow(gate, "expansion_fade", out)
        return out
    pad = max(stop_pad_pct / 100.0 * mark, STOP_PAD_MIN_TICKS * tick)
    stop = _to_grid(edge + pad if direction == "short" else edge - pad, tick)
    out.update(armed=True, direction=direction, entry=limit,
               limit_price=limit, stop=stop, grade="B",
               reason="expansion_preposition")
    out["shadow"] = _shadow(gate, "expansion_fade", out)
    return out


# ── 2. Sizing math ────────────────────────────────────────────────────────

def leverage_cap_for(entry_style: str, margin_mode: str) -> int:
    """Governor doctrine: band-bridge maker entries and isolated probes up
    to 15x; anything taker-entered or cross-margin caps at 8x."""
    if margin_mode == "cross" or entry_style == "taker":
        return LEVERAGE_TAKER_CROSS
    return LEVERAGE_MAKER_ISOLATED


def stop_floor_verdict(stop: float, limit_price: float, direction: str,
                       mark: float, tick: float, floor_pct: float
                       ) -> Tuple[float, bool]:
    """Widen-only stop floor (Governor 2026-11-04 re-arm doctrine). The
    2026-11-03 postmortem census: ~0.1% verdict pads vs the 6bps roundtrip
    fee band → stop-outs 1-60s after entry, 467 stop exits, 71.6% scratch.
    The floor widens any verdict stop tighter than floor_pct % of mark out
    to the floor, on-grid. Constant risk_usd means the wider stop shrinks
    the NOTIONAL, never the risk — the trade gets smaller, not tighter.

    Strictly widen-or-unchanged: grid rounding that fails to widen returns
    the original stop. floor_pct <= 0 or degenerate inputs = legacy
    bit-for-bit (stop returned unchanged, floored=False)."""
    if floor_pct <= 0 or mark <= 0 or tick <= 0 or limit_price <= 0:
        return stop, False
    if direction not in ("long", "short"):
        return stop, False
    cur = abs(limit_price - stop)
    if cur <= 0:
        return stop, False
    floor_dist = floor_pct / 100.0 * mark
    if cur >= floor_dist:
        return stop, False
    raw = (limit_price - floor_dist if direction == "long"
           else limit_price + floor_dist)
    new = _to_grid(raw, tick)
    if new is None or abs(limit_price - new) <= cur:
        return stop, False
    return new, True


def scalp_size(*, equity_usd: float, risk_usd: float, entry: float,
               stop: float, leverage: int,
               min_notional: float = MIN_NOTIONAL_USD,
               max_margin_pct: float = 0.0) -> Dict[str, Any]:
    """Notional = risk / stop_frac, capped at equity × leverage, floored at
    the venue min. Fail-closed on degenerate geometry.

    max_margin_pct (Governor 2026-10-24, "fix this so our risk is
    contained"): per-bracket margin ceiling as a fraction of equity —
    margin = notional/leverage ≤ equity × max_margin_pct, so the cap is
    notional ≤ equity × max_margin_pct × leverage. Can only SHRINK a
    bracket (realized stop risk drops below risk_usd), never grow it.
    0.0 / unreadable = legacy bit-for-bit (uncapped). When the cap binds,
    the verdict carries capped_by="margin" (telemetry observability)."""
    out: Dict[str, Any] = {"ok": False, "notional": 0.0, "margin": 0.0,
                           "leverage": int(leverage), "stop_frac": None,
                           "reason": ""}
    eq, risk = _f(equity_usd), _f(risk_usd)
    e, s = _f(entry), _f(stop)
    if not eq or eq <= 0 or risk is None or risk <= 0:
        out["reason"] = "bad_equity_or_risk"
        return out
    if not e or e <= 0 or s is None or s <= 0 or s == e:
        out["reason"] = "bad_geometry"
        return out
    lev = max(1, min(LEVERAGE_MAKER_ISOLATED, int(leverage)))
    stop_frac = abs(e - s) / e
    notional = min(risk / stop_frac, eq * lev)
    try:
        _mcp = float(max_margin_pct)
    except (TypeError, ValueError):
        _mcp = 0.0
    if _mcp > 0:
        _cap_notional = eq * _mcp * lev
        if _cap_notional < notional:
            notional = _cap_notional
            out["capped_by"] = "margin"
    if notional < min_notional:
        out.update(stop_frac=round(stop_frac, 8),
                   reason="below_min_notional", notional=round(notional, 4))
        return out
    out.update(ok=True, notional=round(notional, 4),
               margin=round(notional / lev, 4), leverage=lev,
               stop_frac=round(stop_frac, 8), reason="ok")
    return out


def volume_projection(trades_per_day: float, avg_notional: float,
                      target_daily: float = TARGET_VOLUME_DAILY_USD
                      ) -> Dict[str, Any]:
    """Dynamic volume math — the $100k/day venue target is a function, not
    a constant: given the current trade count and average clip, where does
    the day land and how many trades does the target need?"""
    tpd, avg = _f(trades_per_day) or 0.0, _f(avg_notional) or 0.0
    daily = tpd * avg
    needed = math.ceil(target_daily / avg) if avg > 0 else None
    return {"daily_volume_usd": round(daily, 2),
            "target_daily_usd": target_daily,
            "share_of_target": round(daily / target_daily, 4)
            if target_daily > 0 else None,
            "trades_needed_for_target": needed}


def funding_carry_usd(notional: float, funding_rate_1h: Optional[float]
                      ) -> Optional[float]:
    """Predicted 1h funding carry in USD on a notional — shadow economics
    only (at the 240s horizon funding labels the cohort, it doesn't gate)."""
    r = _f(funding_rate_1h)
    n = _f(notional)
    if r is None or n is None:
        return None
    return round(n * r, 6)


# ── 3. Lifecycle verdicts ────────────────────────────────────────────────

def manage_scalp(position_state: Optional[Dict[str, Any]],
                 book: Optional[Dict[str, Any]], now_s: float, *,
                 tick: float = 0.0,
                 time_stop_s: float = TIME_STOP_S,
                 band_event: Optional[Dict[str, Any]] = None,
                 floor_rungs: Optional[Tuple[Tuple[float, float], ...]] = None,
                 book_max_age_s: float = BOOK_MAX_AGE_S,
                 fee_bps: float = 0.0) -> Dict[str, Any]:
    """position_state: {symbol, direction, entry, stop, target, opened_ts,
    peak_fav, qty}. peak_fav = best favorable price seen (highest bid for a
    long, lowest ask for a short) — maintained by the caller/registry.
    band_event: {event, side, ts} — a recent boundEvent; an expansion of
    the side we're fading INTO moves the ratchet against us → abort.
    floor_rungs: ((peak_ticks, lock_frac), ...) tighten-only profit-floor
    ladder; None = flat breakeven floor only (legacy rung-zero).
    fee_bps: round-trip fee in basis points (2026-11-02) — shifts the arm
    threshold AND every rung/floor measure by the fee offset so a "win"
    banks real money, not sub-fee dust. 0.0 = legacy raw-tick bit-for-bit.

    Verdict order is load-bearing: dark state holds (never act blind);
    protection (stop, adverse ratchet) outranks the clock; the clock
    outranks targets; the breakeven floor only exists after the first
    favorable tick."""
    hold = {"action": "hold", "reason": ""}
    ps = position_state or {}
    direction = ps.get("direction")
    entry = _f(ps.get("entry"))
    opened = _f(ps.get("opened_ts"))
    if direction not in ("long", "short") or not entry or entry <= 0 \
            or opened is None:
        hold["reason"] = "insufficient_state"
        return hold
    if not _book_ok(book, now_s, book_max_age_s):
        hold["reason"] = "no_book"
        return hold
    bid, ask = float(book["bid"]), float(book["ask"])

    # 1. stop touch → market (protection first).
    stop = _f(ps.get("stop"))
    if stop and stop > 0:
        if direction == "long" and bid <= stop:
            return {"action": "exit_market", "reason": "stop"}
        if direction == "short" and ask >= stop:
            return {"action": "exit_market", "reason": "stop"}

    # 2. adverse band event: a ratchet expansion of the side we fade INTO.
    if isinstance(band_event, dict) \
            and band_event.get("event") == "tradingBoundExpanded":
        ev_ts = _f(band_event.get("ts")) or 0.0
        if ev_ts >= opened:
            if direction == "short" and band_event.get("side") == "upper":
                return {"action": "exit_market",
                        "reason": "band_ratchet_adverse"}
            if direction == "long" and band_event.get("side") == "lower":
                return {"action": "exit_market",
                        "reason": "band_ratchet_adverse"}

    # 3. time-stop — the 240s thesis horizon.
    if now_s - opened >= time_stop_s:
        return {"action": "exit_market", "reason": "time_stop"}

    # 4. target → limit out at the anchor/touch objective.
    target = _f(ps.get("target"))
    if target and target > 0:
        if direction == "long" and bid >= target:
            return {"action": "exit_limit", "price": target,
                    "reason": "target"}
        if direction == "short" and ask <= target:
            return {"action": "exit_limit", "price": target,
                    "reason": "target"}

    # 5. profit floor — armed only after the first favorable tick, then
    # TIGHTEN-ONLY up the FLOOR_RUNGS ladder as peak_fav (the scalp's
    # MFE plane) grows. Breach is protection → market out. (The legacy
    # flat floor exited via PostOnly limit AT entry — marketable by
    # construction whenever price was through it, so every floor exit
    # rejected POST_ONLY_WOULD_CROSS and the phantom-close loop stacked
    # PUMP to 3x intended size on 2026-10-29. Floors now exit at market.)
    peak = _f(ps.get("peak_fav"))
    if peak is not None:
        # Fee-aware ladder (2026-11-02): fee_ticks shifts the arm threshold
        # and is subtracted from the excursion before rungs measure it; the
        # floor itself sits at entry + fee_offset + lock × NET excursion, so
        # even rung zero exits fee-covered. fee_bps=0.0 → fee_ticks=0 →
        # legacy bit-for-bit.
        fb = _f(fee_bps) or 0.0
        fee_ticks = int(math.ceil(fb * 1e-4 * entry / tick)) \
            if (fb > 0 and tick > 0) else 0
        arm_margin = (fee_ticks + 1) * tick if tick > 0 else 1e-9
        armed = (peak >= entry + arm_margin) if direction == "long" \
            else (peak <= entry - arm_margin)
        if armed:
            gross = (peak - entry) if direction == "long" else (entry - peak)
            net_fav = max(0.0, gross - fee_ticks * tick)
            lock = 0.0
            if tick > 0 and floor_rungs:
                adj_ticks = net_fav / tick
                for rung_ticks, frac in floor_rungs:
                    if adj_ticks >= rung_ticks:
                        lock = max(lock, float(frac))
            floor_off = fee_ticks * tick + lock * net_fav
            floor_px = (entry + floor_off) if direction == "long" \
                else (entry - floor_off)
            if direction == "long" and bid <= floor_px:
                return {"action": "exit_market",
                        "reason": "breakeven_floor" if lock == 0.0
                                  else "profit_floor_rung"}
            if direction == "short" and ask >= floor_px:
                return {"action": "exit_market",
                        "reason": "breakeven_floor" if lock == 0.0
                                  else "profit_floor_rung"}
    return hold


# ── 4. Trackers (injected state, no I/O) ─────────────────────────────────

class FlowWindows:
    """Per-symbol ring of the last N directional taker fills. taker_buy
    None (stale-BBO inference) is ignored per the feed contract."""

    def __init__(self, window_n: int = FLOW_WINDOW_N,
                 min_fills: int = FLOW_MIN_FILLS) -> None:
        self._n = max(2, int(window_n))
        self._min = max(1, int(min_fills))
        self._fills: Dict[str, List[Tuple[bool, float]]] = {}

    def ingest_trade(self, symbol: str, price: float, size: float,
                     ts_us: float, taker_buy: Optional[bool]) -> bool:
        if not symbol or taker_buy is None:
            return False
        sz = _f(size)
        if sz is None or sz <= 0:
            return False
        ring = self._fills.setdefault(symbol, [])
        ring.append((bool(taker_buy), sz))
        if len(ring) > self._n:
            del ring[:len(ring) - self._n]
        return True

    def imbalance(self, symbol: str) -> Optional[float]:
        """Qty-weighted signed imbalance in [-1, 1] over the window;
        None when the window is too thin to read (dark)."""
        ring = self._fills.get(symbol) or []
        if len(ring) < self._min:
            return None
        buy = sum(sz for tb, sz in ring if tb)
        sell = sum(sz for tb, sz in ring if not tb)
        tot = buy + sell
        if tot <= 0:
            return None
        return (buy - sell) / tot


class ScalpRegistry:
    """Open scalps, per-symbol dedup, max-concurrent, per-class daily
    counters, loss cooloff. All state injected — the integrator drives."""

    def __init__(self, max_concurrent: int = MAX_CONCURRENT,
                 cooloff_s: float = LOSS_COOLOFF_S,
                 loss_streak: int = LOSS_STREAK,
                 churn_guards_enabled: bool = True,
                 hourly_loss_budget: int = 3,
                 hourly_loss_window_s: float = 3600.0,
                 post_loss_reentry_s: float = 180.0,
                 flip_block_s: float = 900.0,
                 whitelist: Optional[FrozenSet[str]] = None) -> None:
        self._max = max(1, int(max_concurrent))
        self._cooloff_s = float(cooloff_s)
        self._streak = max(1, int(loss_streak))
        self._open: Dict[str, Dict[str, Any]] = {}
        self._entry_pending: Dict[str, Dict[str, Any]] = {}
        self._cooloff: Dict[str, float] = {}
        self._loss_chain: Dict[str, List[str]] = {}
        self._daily: Dict[int, Dict[str, int]] = {}
        self._band_events: Dict[str, Dict[str, Any]] = {}
        # Churn-guard state (2026-11-04 PUMP class): the consecutive-loss
        # park only engages on a perfect streak — scratch/win interleave
        # resets it while the symbol bleeds fees all day. These legs count
        # losses on a ROLLING window and bound stop→re-entry latency.
        self._churn_on = bool(churn_guards_enabled)
        self._hourly_budget = max(0, int(hourly_loss_budget))
        self._hourly_window_s = max(60.0, float(hourly_loss_window_s))
        self._reentry_s = max(0.0, float(post_loss_reentry_s))
        self._flip_block_s = max(0.0, float(flip_block_s))
        self._loss_times: Dict[str, List[float]] = {}
        self._last_close: Dict[str, Tuple[float, str, str]] = {}
        # Equity-cohort whitelist (Governor 2026-11-04): only these symbols
        # may open. None = all symbols (legacy bit-for-bit, kill switch).
        self._whitelist = frozenset(whitelist) if whitelist else None

    @staticmethod
    def _day(now_s: float) -> int:
        return int(now_s // 86400)

    # ── admission ────────────────────────────────────────────────────────

    def can_open(self, symbol: str, cls: str, now_s: float,
                 class_daily_cap: Optional[int] = None,
                 direction: str = "") -> Tuple[bool, str]:
        if self._whitelist is not None and symbol not in self._whitelist:
            return False, "not_whitelisted"
        if symbol in self._open:
            return False, "symbol_open"
        if symbol in self._entry_pending:
            return False, "entry_pending"
        until = self._cooloff.get(symbol)
        if until is not None and now_s < until:
            return False, "cooloff"
        if self._churn_on:
            # Rolling hourly loss budget — counts losses regardless of
            # scratch/win interleave (the streak park's blind spot).
            if self._hourly_budget > 0:
                cutoff = now_s - self._hourly_window_s
                recent = [t for t in (self._loss_times.get(symbol) or [])
                          if t >= cutoff]
                if len(recent) >= self._hourly_budget:
                    return False, "hourly_loss_budget"
            # Post-loss re-entry floor — stop→re-entry gaps of 2-8s were
            # machine-gunning the same noise band.
            last = self._last_close.get(symbol)
            if last is not None and self._reentry_s > 0:
                if last[1] == "loss" and now_s - last[0] < self._reentry_s:
                    return False, "post_loss_reentry"
            # Flip-flop block — a close in one direction parks the
            # opposite direction for flip_block_s.
            if last is not None and self._flip_block_s > 0:
                if (direction and last[2] and direction != last[2]
                        and now_s - last[0] < self._flip_block_s):
                    return False, "flip_flop"
        # Pending entries occupy capacity too — a resting limit can fill.
        if len(self._open) + len(self._entry_pending) >= self._max:
            return False, "max_concurrent"
        if class_daily_cap is not None:
            used = self._daily.get(self._day(now_s), {}).get(cls, 0)
            if used >= class_daily_cap:
                return False, "class_daily_cap"
        return True, "ok"

    def register_open(self, symbol: str, record: Dict[str, Any],
                      now_s: float) -> bool:
        """record: {direction, cls, entry, stop, target, qty, notional,
        grade}. Dedup enforced — a second open on the symbol is refused."""
        if symbol in self._open:
            return False
        rec = dict(record or {})
        rec["opened_ts"] = now_s
        rec.setdefault("peak_fav", rec.get("entry"))
        self._open[symbol] = rec
        day = self._daily.setdefault(self._day(now_s), {})
        cls = str(rec.get("cls") or "unknown")
        day[cls] = day.get(cls, 0) + 1
        return True

    def register_close(self, symbol: str, outcome: str, now_s: float
                       ) -> bool:
        """outcome ∈ win|loss|scratch. LOSS_STREAK consecutive losses park
        the symbol for cooloff_s; a win or scratch breaks the streak."""
        rec = self._open.pop(symbol, None)
        existed = rec is not None
        oc = str(outcome or "").strip().lower()
        if oc not in ("win", "loss", "scratch"):
            return existed
        if rec is not None:
            self._last_close[symbol] = (
                now_s, oc, str(rec.get("direction") or ""))
        if self._churn_on and oc == "loss":
            ring = self._loss_times.setdefault(symbol, [])
            ring.append(now_s)
            cutoff = now_s - self._hourly_window_s
            self._loss_times[symbol] = [t for t in ring if t >= cutoff]
        chain = self._loss_chain.setdefault(symbol, [])
        chain.append(oc)
        del chain[:-10]
        if oc != "loss":
            self._loss_chain[symbol] = []
        elif len(chain) >= self._streak \
                and all(o == "loss" for o in chain[-self._streak:]):
            self._cooloff[symbol] = now_s + self._cooloff_s
            self._loss_chain[symbol] = []
        return existed

    def arm_cooloff(self, symbol: str, now_s: float) -> None:
        # Foreign-flow firewall (2026-11-01 PUMP class): a heal that found
        # the venue leg netted away / flat must park the symbol — the
        # scratch close otherwise breaks the loss streak and the loop
        # re-enters against the operator's manual position every ~15s.
        self._cooloff[symbol] = now_s + self._cooloff_s
        self._loss_chain[symbol] = []

    # ── entry-pending lifecycle (fill-confirmed registration, 2026-11-02) ──
    # A resting PostOnly entry is NOT a position — registering at order
    # ACCEPTANCE manufactured phantom tracks whose every exit rejected
    # REDUCE_ONLY_WOULD_INCREASE against a flat venue (the dominant Arcus
    # bleed class). The integrator parks the record here and confirms it
    # against venue truth; the daily class counter only counts real fills.

    def register_entry_pending(self, symbol: str, record: Dict[str, Any],
                               now_s: float) -> bool:
        """record: the register_open payload + order_id + kind (entry|add).
        Refused while already pending (one resting scalp order per symbol).
        kind=entry is refused while the symbol is open; kind=add REQUIRES
        the open base track it folds into. The integrator must cancel the
        resting order on a False here."""
        if symbol in self._entry_pending:
            return False
        rec = dict(record or {})
        if str(rec.get("kind") or "entry") == "add":
            if symbol not in self._open:
                return False
        elif symbol in self._open:
            return False
        rec["pending_ts"] = now_s
        rec.setdefault("kind", "entry")
        self._entry_pending[symbol] = rec
        return True

    def entry_pending(self, symbol: str) -> Optional[Dict[str, Any]]:
        rec = self._entry_pending.get(symbol)
        return dict(rec) if rec else None

    def entry_pending_since(self, symbol: str) -> Optional[float]:
        rec = self._entry_pending.get(symbol)
        return _f(rec.get("pending_ts")) if rec else None

    def pending_count(self) -> int:
        return len(self._entry_pending)

    def confirm_entry(self, symbol: str, qty: float, entry: float,
                      now_s: float) -> bool:
        """Venue shows the fill → the track opens on VENUE truth (qty and
        average entry), never on intended geometry. kind=add folds the fill
        into the existing track (pyramid VWAP re-anchor). Bad qty leaves
        the pending record in place — the integrator retries next wake."""
        pend = self._entry_pending.get(symbol)
        if pend is None:
            return False
        q = _f(qty)
        if q is None or q <= 0:
            return False
        self._entry_pending.pop(symbol, None)
        if str(pend.get("kind") or "entry") == "add":
            return self.register_add(symbol, q, _f(entry), now_s)
        rec = {k: v for k, v in pend.items()
               if k not in ("pending_ts", "kind", "order_id")}
        e = _f(entry)
        if e is not None and e > 0:
            rec["entry"] = e
        rec["qty"] = q
        if rec.get("entry"):
            rec["notional"] = q * float(rec["entry"])
        return self.register_open(symbol, rec, now_s)

    def cancel_entry_pending(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Drop the pending record (timeout/abort) and return it so the
        integrator can cancel the resting order by order_id. No close is
        booked and no cooloff is consumed — an unfilled entry is not a
        loss."""
        return self._entry_pending.pop(symbol, None)

    def register_add(self, symbol: str, add_qty: float,
                     add_price: Optional[float], now_s: float) -> bool:
        """Pyramid add into a proven scalp (Governor 2026-11-02): qty grows,
        entry re-anchors to the combined VWAP, adds_done increments. The
        profit floor then reads the combined basis — tighten-only by
        construction on a same-direction add above the old entry."""
        rec = self._open.get(symbol)
        q = _f(add_qty)
        if rec is None or q is None or q <= 0:
            return False
        base_q = _f(rec.get("qty")) or 0.0
        base_e = _f(rec.get("entry")) or 0.0
        p = _f(add_price)
        if p is not None and p > 0 and base_q > 0 and base_e > 0:
            rec["entry"] = (base_q * base_e + q * p) / (base_q + q)
        rec["qty"] = base_q + q
        if rec.get("entry"):
            rec["notional"] = rec["qty"] * float(rec["entry"])
        rec["adds_done"] = int(rec.get("adds_done") or 0) + 1
        return True

    # ── state feeds ──────────────────────────────────────────────────────

    def note_price(self, symbol: str, price: float) -> None:
        """Update the peak-favorable excursion for the breakeven floor."""
        rec = self._open.get(symbol)
        p = _f(price)
        if rec is None or p is None or p <= 0:
            return
        peak = _f(rec.get("peak_fav"))
        if peak is None:
            rec["peak_fav"] = p
        elif rec.get("direction") == "long":
            rec["peak_fav"] = max(peak, p)
        else:
            rec["peak_fav"] = min(peak, p)

    def ingest_band_event(self, symbol: str, event: str, side: str,
                          ts: float) -> None:
        """Store the latest boundEvent per symbol (from BandClock.
        ingest_entry's rec) — manage() reads it for the adverse abort."""
        self._band_events[symbol] = {"event": event, "side": side,
                                     "ts": ts}

    # ── reads ────────────────────────────────────────────────────────────

    def is_open(self, symbol: str) -> bool:
        return symbol in self._open

    def open_count(self) -> int:
        return len(self._open)

    def open_margins(self) -> List[Tuple[str, float]]:
        """(symbol, margin_usd) per open scalp — the Chancellor exposure feed
        (mapping-agent item 1, 2026-11-03): combined-balance sizing counts
        Arcus equity as free capital while the Chancellor's _open_margins
        excluded Arcus margin entirely → cross-venue over-leverage. Spliced
        into the Chancellor's margin census in main.py."""
        out: List[Tuple[str, float]] = []
        for sym, rec in self._open.items():
            try:
                notional = float(rec.get("notional", 0.0) or 0.0)
                lev = float(rec.get("leverage") or LEVERAGE_MAKER_ISOLATED)
                if notional > 0:
                    out.append((sym, notional / max(lev, 1.0)))
            except (TypeError, ValueError):
                continue
        return out

    def cooloff_until(self, symbol: str) -> Optional[float]:
        return self._cooloff.get(symbol)

    def daily_count(self, cls: str, now_s: float) -> int:
        return self._daily.get(self._day(now_s), {}).get(cls, 0)

    def position_state(self, symbol: str) -> Optional[Dict[str, Any]]:
        rec = self._open.get(symbol)
        return dict(rec) if rec else None

    # ── exit bookkeeping (phantom-close repair, 2026-10-29) ─────────────

    def mark_exit_pending(self, symbol: str, now_s: float,
                          outcome: str = "scratch") -> bool:
        """An exit LIMIT is resting on the venue — manage() short-circuits
        while it works; the integrator confirms the fill or times it out."""
        rec = self._open.get(symbol)
        if rec is None:
            return False
        rec["exit_pending_ts"] = now_s
        rec["exit_outcome"] = str(outcome or "scratch")
        return True

    def clear_exit_pending(self, symbol: str) -> bool:
        rec = self._open.get(symbol)
        if rec is None or "exit_pending_ts" not in rec:
            return False
        del rec["exit_pending_ts"]
        rec.pop("exit_outcome", None)
        return True

    def exit_pending_since(self, symbol: str) -> Optional[float]:
        rec = self._open.get(symbol)
        return _f(rec.get("exit_pending_ts")) if rec else None

    def resync_qty(self, symbol: str, qty: float) -> bool:
        """Venue-truth repair: a partial fill or a phantom exit left the
        venue size != registry size; adopt the venue's number."""
        rec = self._open.get(symbol)
        q = _f(qty)
        if rec is None or q is None or q <= 0:
            return False
        rec["qty"] = q
        return True

    def manage(self, symbol: str, book: Optional[Dict[str, Any]],
               now_s: float, *, tick: float = 0.0,
               time_stop_s: float = TIME_STOP_S,
               floor_rungs: Optional[Tuple[Tuple[float, float], ...]]
               = None,
               fee_bps: float = 0.0) -> Dict[str, Any]:
        """Convenience: build the pure-function inputs from registry state
        and delegate to manage_scalp. A symbol with a resting exit limit
        (exit_pending) holds — the integrator owns the confirm/timeout."""
        rec = self._open.get(symbol)
        if rec is None:
            return {"action": "hold", "reason": "not_open"}
        if rec.get("exit_pending_ts") is not None:
            return {"action": "hold", "reason": "exit_pending"}
        return manage_scalp(rec, book, now_s, tick=tick,
                            time_stop_s=time_stop_s,
                            band_event=self._band_events.get(symbol),
                            floor_rungs=floor_rungs, fee_bps=fee_bps)


# ── 5. Basket harvest + pyramid (Governor 2026-11-02) ────────────────────

def leg_upnl_usd(rec: Dict[str, Any], book: Optional[Dict[str, Any]]
                 ) -> Optional[float]:
    """Mark-to-exit-side uPnL for one scalp leg: longs bank at bid, shorts
    at ask (the price the exit actually crosses). None on dark inputs —
    fail closed, never price a leg you cannot see."""
    if not isinstance(rec, dict) or not isinstance(book, dict):
        return None
    entry = _f(rec.get("entry"))
    qty = _f(rec.get("qty"))
    direction = rec.get("direction")
    if not entry or entry <= 0 or not qty or qty <= 0:
        return None
    px = _f(book.get("bid")) if direction == "long" else _f(book.get("ask"))
    if px is None or px <= 0:
        return None
    return ((px - entry) * qty) if direction == "long" \
        else ((entry - px) * qty)


def basket_harvest_verdict(positions: List[Dict[str, Any]],
                           books: Dict[str, Optional[Dict[str, Any]]],
                           now_s: float, *,
                           target_roe_pct: float = BASKET_TARGET_ROE_PCT,
                           min_profit_usd: float = BASKET_MIN_PROFIT_USD,
                           cooloff_s: float = BASKET_COOLOFF_S,
                           last_harvest_ts: float = 0.0,
                           fee_bps: float = 0.0) -> Dict[str, Any]:
    """Take-profit basket for the scalp book — the treasury TP idiom ported
    to the Arcus sleeve. When the AGGREGATE open uPnL (net of the
    round-trip fee on every leg) covers the book-ROE target AND clears the
    dust guard, bank the green legs at market. Fail closed: any dark leg →
    hold (never harvest a book you cannot price).
    Returns {action: harvest|hold, legs: [symbol, ...] green-first,
    upnl_usd (net), roe_pct, reason}."""
    hold = {"action": "hold", "legs": [], "upnl_usd": 0.0, "roe_pct": 0.0,
            "reason": ""}
    if not positions:
        hold["reason"] = "empty_book"
        return hold
    if last_harvest_ts and now_s - last_harvest_ts < cooloff_s:
        hold["reason"] = "cooloff"
        return hold
    upnl = 0.0
    margin = 0.0
    notional_sum = 0.0
    legs: List[Tuple[str, float]] = []
    for rec in positions:
        sym = str(rec.get("symbol") or "")
        u = leg_upnl_usd(rec, books.get(sym))
        if u is None:
            hold["reason"] = "dark_leg"
            return hold
        upnl += u
        n = _f(rec.get("notional")) or 0.0
        notional_sum += n
        lev = max(_f(rec.get("leverage")) or 1.0, 1e-9)
        margin += n / lev
        if u > 0:
            legs.append((sym, u))
    if margin <= 0:
        hold["reason"] = "no_margin"
        return hold
    fb = _f(fee_bps) or 0.0
    net = upnl - notional_sum * fb * 1e-4
    roe_pct = (net / margin) * 100.0
    hold["upnl_usd"] = round(net, 6)
    hold["roe_pct"] = round(roe_pct, 4)
    if net < min_profit_usd:
        hold["reason"] = "below_min_profit"
        return hold
    if roe_pct < target_roe_pct:
        hold["reason"] = "below_target"
        return hold
    if not legs:
        hold["reason"] = "no_green_legs"
        return hold
    return {"action": "harvest",
            "legs": [s for s, _ in sorted(legs, key=lambda kv: -kv[1])],
            "upnl_usd": round(net, 6), "roe_pct": round(roe_pct, 4),
            "reason": "basket_tp"}


def pyramid_add_verdict(position_state: Optional[Dict[str, Any]],
                        book: Optional[Dict[str, Any]], now_s: float, *,
                        tick: float = 0.0,
                        add_frac: float = PYRAMID_ADD_FRAC,
                        max_adds: int = PYRAMID_MAX_ADDS,
                        floor_rungs: Optional[Tuple[Tuple[float, float], ...]]
                        = None,
                        fee_bps: float = 0.0) -> Dict[str, Any]:
    """Pyramid add into a proven scalp (Governor 2026-11-02: "as well as
    the pyramid"). The add may only join AFTER the fee-adjusted first rung
    is proven — the floor then banks the base by geometry (the SoDEX
    pyramid TP1/breakeven doctrine in tick space). One add max, sized off
    the BASE qty (never equity), joined at the touch as a maker (the
    sleeve's only entry form).
    Returns {armed, direction, qty, limit_price, reason}."""
    out: Dict[str, Any] = {"armed": False, "reason": ""}
    ps = position_state or {}
    direction = ps.get("direction")
    entry = _f(ps.get("entry"))
    qty = _f(ps.get("qty"))
    if direction not in ("long", "short") or not entry or entry <= 0 \
            or not qty or qty <= 0:
        out["reason"] = "insufficient_state"
        return out
    if int(ps.get("adds_done") or 0) >= max(1, int(max_adds)):
        out["reason"] = "max_adds"
        return out
    if tick <= 0:
        out["reason"] = "bad_tick"
        return out
    if not _book_ok(book, now_s):
        out["reason"] = "no_book"
        return out
    peak = _f(ps.get("peak_fav"))
    if peak is None:
        out["reason"] = "no_peak"
        return out
    fb = _f(fee_bps) or 0.0
    fee_ticks = int(math.ceil(fb * 1e-4 * entry / tick)) if fb > 0 else 0
    gross_ticks = ((peak - entry) / tick) if direction == "long" \
        else ((entry - peak) / tick)
    rung1 = float(floor_rungs[0][0]) if floor_rungs else 1.0
    if gross_ticks < fee_ticks + max(rung1, 1.0):
        out["reason"] = "floor_not_proven"
        return out
    add_qty = qty * max(0.0, float(add_frac))
    if add_qty <= 0:
        out["reason"] = "dust_add"
        return out
    bid, ask = float(book["bid"]), float(book["ask"])
    out.update({"armed": True, "direction": direction, "qty": add_qty,
                "limit_price": bid if direction == "long" else ask,
                "reason": "rung1_proven"})
    return out
