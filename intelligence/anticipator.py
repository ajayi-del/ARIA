"""
intelligence/anticipator.py — the anticipator brain (Governor doctrine,
locked 2026-09-26, his words):

    "Aria should predict with limit orders under the book far from price.
     It should place a CL short when price is on a rise in anticipation for
     a cascade, hours before it happens. Spot that metals are rising and
     send limit orders anticipating the flip so we get filled on light
     wicks. With tight stop loss. And large cage profits in case of
     bounces. An unfilled limit costs nothing — free options. We can test
     both sides."

ZERO-I/O BRAIN. This module never touches the exchange, the network, the
clock, or the journal. The coordinator injects mark/ATR/levels/structure
and receives order SPECS; the coordinator's executor places them via
sodex_client.place_order_simple (GTC limit, reduce_only=False) and cancels
the tags prune_verdicts returns.

Doctrine:
    - Cascade-anticipated SHORT  -> resting limit ABOVE mark at a
      resistance / short-stops cluster, filled on the wick up.
    - Flip-anticipated LONG      -> resting limit BELOW mark at a support /
      long-stops cluster, filled on the wick down.
    - Entry nudged 0.05% toward mark to front-run the cluster.
    - Stop beyond the cluster by max(0.3%, stop_atr_frac x ATR).
    - TP cage: tp distance >= cage_min (3.0) x stop distance; if the
      natural next opposite cluster gives more, snap to it.
    - Levels inside min_distance_pct (0.8%) belong to the standard path;
      beyond max_distance_pct (6%) are fantasy. The anticipator hunts
      WICKS in the band between.
    - Both-sides: structure["two_sided"] True plans counter-side too —
      unfilled limits are free options.
    - Fleet caps per symbol (4) and global (12); weakest-strength (ties:
      oldest) evicted first when planning new.

KILL SWITCH: anticipator_enabled=False (default) -> plan_fleet returns an
empty fleet; every other function stays inert-but-safe (prune still works
so a flipped kill switch never strands resting orders).

FAIL-CLOSED: mark None/<=0 or ATR None/<=0 -> no specs, no crash.

Telemetry events (emitted by the coordinator splice, one per call site):
    anticipator_fleet_planned        (symbol, planned, skipped, evicted)
    anticipator_order_placed         (tag, symbol, side, limit_price)
    anticipator_pruned               (tags)
    anticipator_fill_attributed      (tag, attributed_qty)
    anticipator_residual_completed   (tag, qty)
    anticipator_residual_cancelled   (tag, reason)

Config knobs (getattr, defaults):
    anticipator_enabled                  False
    anticipator_min_distance_pct         0.8
    anticipator_max_distance_pct         6.0
    anticipator_stale_s                  2700      (45 min — also spec ttl)
    anticipator_max_age_s                14400     (4h absolute cap)
    anticipator_max_per_symbol           4
    anticipator_max_global               12
    anticipator_cage_min                 3.0
    anticipator_stop_atr_frac            1.0
    anticipator_margin_usd               8.0       (campaign $8 margin —
                                         cross-review P0: was 100.0, 12x the
                                         locked campaign margin; the splice
                                         drives this from fast_cycle)
    anticipator_entry_nudge_pct          0.05
    anticipator_residual_complete_frac   0.6
    anticipator_residual_chase_pct       0.003     (FRACTION, not percent —
                                         0.003 = 0.3%; unlike the *_pct knobs
                                         above which are percents)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderSpec:
    """One resting-limit fleet order spec. The coordinator's executor turns
    this into a GTC limit via place_order_simple; stop/tp ride the spec so
    the bracket layer can arm them on fill."""
    tag: str
    symbol: str
    side: str                 # "short" | "long"
    limit_price: float
    stop_price: float
    tp_price: float
    qty_margin_usd: float
    strength: float           # 0..1 after hint boosts
    ttl_s: float = 2700.0
    created_ts: float = 0.0


@dataclass(frozen=True)
class SkipReason:
    """Why a candidate level (or an incumbent fleet order) was not kept."""
    level_price: Optional[float]
    side: Optional[str]
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class ResidualVerdict:
    action: str               # "hold" | "complete_market" | "cancel_remainder"
    reason: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _knob(cfg: Any, name: str, default: float) -> Any:
    try:
        v = getattr(cfg, name, default)
    except Exception:
        return default
    return default if v is None else v


_SHORT_SIDES = {"short_stops", "above", "resistance", "sell", "short"}
_LONG_SIDES = {"long_stops", "below", "support", "buy", "long"}


def _level_side(raw_side: Any, price: float, mark: float) -> Optional[str]:
    """Normalize a level's side to the fleet direction it implies.
    Unknown side strings fall back to position vs mark (above -> short,
    below -> long)."""
    s = str(raw_side or "").strip().lower()
    if s in _SHORT_SIDES:
        return "short"
    if s in _LONG_SIDES:
        return "long"
    if price > mark:
        return "short"
    if price < mark:
        return "long"
    return None


def _hint_side(raw: Any) -> Optional[str]:
    """Normalize a hint direction/vote to the fleet side vocabulary.
    Cross-review P1: family emits "up"/"down" and the spread oracle votes
    "UP"/"DOWN" while fleet sides are "long"/"short" — unmapped, every
    comparison failed and both x1.10 boosts were dead code."""
    s = str(raw or "").strip().lower()
    if s in ("long", "up", "buy", "bullish"):
        return "long"
    if s in ("short", "down", "sell", "bearish"):
        return "short"
    return None


def _strength_boosts(strength: float, side: str, structure: Dict[str, Any]) -> float:
    """Optional injected hints boost (never create) conviction, capped 1.0.

    family_hint: (leader_symbol, direction, eta_minutes) — matching direction
        x1.10.
    spread_oracle: (spread_multiple, direction_vote) or dict — matching vote
        x1.10.
    regime: string containing "pre_cascade" -> x1.15.
    """
    mult = 1.0
    try:
        fam = structure.get("family_hint")
        if fam:
            if isinstance(fam, dict):
                fdir = fam.get("direction")
            else:
                fdir = fam[1] if len(fam) > 1 else None
            if fdir and _hint_side(fdir) == side:
                mult *= 1.10
    except Exception:
        pass
    try:
        orc = structure.get("spread_oracle")
        if orc:
            if isinstance(orc, dict):
                vote = orc.get("direction_vote")
            else:
                vote = orc[1] if len(orc) > 1 else None
            if vote and _hint_side(vote) == side:
                mult *= 1.10
    except Exception:
        pass
    try:
        regime = structure.get("regime")
        if regime and "pre_cascade" in str(regime).lower():
            mult *= 1.15
    except Exception:
        pass
    return min(1.0, strength * mult)


# ---------------------------------------------------------------------------
# 1. Fleet planning
# ---------------------------------------------------------------------------


def plan_fleet(
    cfg: Any,
    *,
    symbol: str,
    mark_price: Optional[float],
    atr: Optional[float],
    levels: Sequence[Tuple[float, Any, float]],
    structure: Optional[Dict[str, Any]],
    now_ts: float,
    open_fleet: Optional[Sequence[Dict[str, Any]]] = None,
) -> Tuple[List[OrderSpec], List[SkipReason]]:
    """Plan a fleet of resting-limit order specs from predicted levels.

    Returns (specs, skips). skips covers rejected levels AND evictions of
    incumbent fleet orders (reason="evicted", detail=tag).

    open_fleet: dicts with keys tag/symbol/limit_price/created_ts and
    optional strength — the incumbent resting fleet for cap accounting.
    """
    specs: List[OrderSpec] = []
    skips: List[SkipReason] = []

    if not _knob(cfg, "anticipator_enabled", False):
        return specs, skips

    # Fail-closed on missing mark / ATR.
    try:
        mark = float(mark_price) if mark_price is not None else 0.0
    except (TypeError, ValueError):
        mark = 0.0
    try:
        atr_v = float(atr) if atr is not None else 0.0
    except (TypeError, ValueError):
        atr_v = 0.0
    if mark <= 0.0 or atr_v <= 0.0:
        for lv in (levels or []):
            try:
                skips.append(SkipReason(float(lv[0]), None, "mark_or_atr_unknown"))
            except Exception:
                skips.append(SkipReason(None, None, "mark_or_atr_unknown"))
        return specs, skips

    min_d = float(_knob(cfg, "anticipator_min_distance_pct", 0.8)) / 100.0
    max_d = float(_knob(cfg, "anticipator_max_distance_pct", 6.0)) / 100.0
    cage_min = float(_knob(cfg, "anticipator_cage_min", 3.0))
    stop_atr_frac = float(_knob(cfg, "anticipator_stop_atr_frac", 1.0))
    nudge = float(_knob(cfg, "anticipator_entry_nudge_pct", 0.05)) / 100.0
    stale_s = float(_knob(cfg, "anticipator_stale_s", 2700.0))
    max_per_symbol = int(_knob(cfg, "anticipator_max_per_symbol", 4))
    max_global = int(_knob(cfg, "anticipator_max_global", 12))
    base_margin = float(_knob(cfg, "anticipator_margin_usd", 8.0))

    structure = structure or {}
    struct_dir = structure.get("direction") or structure.get("bias")
    struct_dir = str(struct_dir).lower() if struct_dir else None
    if struct_dir not in ("short", "long"):
        struct_dir = None
    two_sided = bool(structure.get("two_sided"))

    # Index levels by side for the opposite-cluster TP snap.
    parsed: List[Tuple[float, str, float]] = []
    for lv in (levels or []):
        try:
            price = float(lv[0])
            strength = float(lv[2])
        except (TypeError, ValueError, IndexError):
            skips.append(SkipReason(None, None, "malformed_level"))
            continue
        if price <= 0:
            skips.append(SkipReason(price, None, "nonpositive_level"))
            continue
        raw_side = lv[1] if len(lv) > 1 else None
        side = _level_side(raw_side, price, mark)
        if side is None:
            skips.append(SkipReason(price, None, "level_at_mark"))
            continue
        parsed.append((price, side, max(0.0, min(1.0, strength))))

    candidates: List[Tuple[float, str, float]] = []  # (price, side, strength)
    for price, side, strength in parsed:
        # Positional consistency: a short limit must rest ABOVE mark, a long
        # limit BELOW — otherwise it would execute immediately, not on a wick.
        if side == "short" and price <= mark:
            skips.append(SkipReason(price, side, "wrong_side_of_mark"))
            continue
        if side == "long" and price >= mark:
            skips.append(SkipReason(price, side, "wrong_side_of_mark"))
            continue
        dist = abs(price - mark) / mark
        if dist <= min_d:  # exactly at min distance -> reject (standard path's turf)
            skips.append(SkipReason(price, side, "inside_min_distance"))
            continue
        if dist > max_d:
            skips.append(SkipReason(price, side, "beyond_max_distance"))
            continue
        # Structure-hint direction filter. two_sided=False -> never plan
        # the counter side.
        if struct_dir and side != struct_dir and not two_sided:
            skips.append(SkipReason(price, side, "counter_side_suppressed"))
            continue
        candidates.append((price, side, strength))

    # Strongest first; caps trimmed below.
    candidates.sort(key=lambda c: c[2], reverse=True)

    # --- Cap accounting + eviction (weakest-strength first, ties: oldest) ---
    open_fleet = list(open_fleet or [])
    open_sym = [o for o in open_fleet if o.get("symbol") == symbol]

    def _evict_key(o: Dict[str, Any]) -> Tuple[float, float]:
        try:
            st = float(o.get("strength", 0.0) or 0.0)
        except (TypeError, ValueError):
            st = 0.0
        try:
            ts = float(o.get("created_ts", 0.0) or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        return (st, ts)  # weakest first, then oldest

    evicted_tags: set = set()

    def _evict(pool: List[Dict[str, Any]], n: int) -> None:
        for o in sorted(pool, key=_evict_key)[:max(0, n)]:
            tag = str(o.get("tag", ""))
            if tag in evicted_tags:
                continue
            skips.append(SkipReason(None, None, "evicted", tag))
            evicted_tags.add(tag)

    # Symbol cap: free room from THIS symbol's incumbents only.
    need_sym = max(0, len(candidates) - (max_per_symbol - len(open_sym)))
    _evict(open_sym, need_sym)
    # Global cap: free room from whatever remains weakest fleet-wide.
    remaining_glob = len(open_fleet) - len(evicted_tags)
    need_glob = max(0, len(candidates) - (max_global - remaining_glob))
    _evict([o for o in open_fleet if str(o.get("tag", "")) not in evicted_tags],
           need_glob)

    room_sym = max_per_symbol - (len(open_sym) - len(evicted_tags & {
        str(o.get("tag", "")) for o in open_sym}))
    room_glob = max_global - (len(open_fleet) - len(evicted_tags))
    room = max(0, min(room_sym, room_glob))

    kept = candidates[:room]
    for price, side, strength in candidates[room:]:
        skips.append(SkipReason(price, side, "fleet_capacity"))

    # --- Geometry per kept candidate ---
    ms = int(now_ts * 1000)
    used_tags = set()
    for price, side, strength in kept:
        strength = _strength_boosts(strength, side, structure)

        # Entry nudged toward mark to front-run the cluster.
        if side == "short":
            entry = price * (1.0 - nudge)
        else:
            entry = price * (1.0 + nudge)

        # Stop beyond the cluster by max(0.3%, stop_atr_frac x ATR fraction).
        stop_frac = max(0.003, stop_atr_frac * atr_v / price)
        if side == "short":
            stop = price * (1.0 + stop_frac)
        else:
            stop = price * (1.0 - stop_frac)
        stop_dist = abs(stop - entry)

        # TP cage: >= cage_min x stop distance; snap to the nearest opposite
        # cluster beyond the cage floor when it gives more.
        tp_min_dist = cage_min * stop_dist
        best_opp: Optional[float] = None
        for op, oside, _ in parsed:
            if oside == side:
                continue
            if side == "short" and op < entry and (entry - op) >= tp_min_dist:
                if best_opp is None or op > best_opp:  # nearest below entry
                    best_opp = op
            if side == "long" and op > entry and (op - entry) >= tp_min_dist:
                if best_opp is None or op < best_opp:  # nearest above entry
                    best_opp = op
        if best_opp is not None:
            # Nudge the target slightly back toward entry to front-run it.
            tp = best_opp * (1.0 + nudge) if side == "short" else best_opp * (1.0 - nudge)
        else:
            tp = entry - tp_min_dist if side == "short" else entry + tp_min_dist

        margin = round(base_margin * (0.5 + 0.5 * strength), 2)

        tag = f"ant-{symbol}-{side}-{ms}"
        if tag in used_tags:  # same-ms collision within a batch
            i = 1
            while f"{tag}-{i}" in used_tags:
                i += 1
            tag = f"{tag}-{i}"
        used_tags.add(tag)

        specs.append(OrderSpec(
            tag=tag,
            symbol=symbol,
            side=side,
            limit_price=entry,
            stop_price=stop,
            tp_price=tp,
            qty_margin_usd=margin,
            strength=strength,
            ttl_s=stale_s,
            created_ts=float(now_ts),
        ))

    return specs, skips


# ---------------------------------------------------------------------------
# 2. Prune verdicts
# ---------------------------------------------------------------------------


def prune_verdicts(
    cfg: Any,
    *,
    open_orders: Sequence[Dict[str, Any]],
    mark_prices: Dict[str, Optional[float]],
    now_ts: float,
) -> List[str]:
    """Tags to cancel.

    Stale thesis: age > stale_s AND mark has moved AWAY from the level by
    > 0.5% of the level since placement (requires placed_mark on the order
    dict; without it the away leg is unprovable -> KEEP, free options are
    cheap). Absolute cap: age > max_age_s regardless.
    """
    stale_s = float(_knob(cfg, "anticipator_stale_s", 2700.0))
    max_age_s = float(_knob(cfg, "anticipator_max_age_s", 14400.0))
    cancel: List[str] = []

    for o in (open_orders or []):
        tag = str(o.get("tag", ""))
        if not tag:
            continue
        try:
            created = float(o.get("created_ts", 0.0) or 0.0)
        except (TypeError, ValueError):
            created = 0.0
        if created > 1e12:  # tolerate ms timestamps
            created /= 1000.0
        age = float(now_ts) - created
        if age > max_age_s:
            cancel.append(tag)
            continue
        if age > stale_s:
            try:
                level = float(o.get("limit_price", 0.0) or 0.0)
            except (TypeError, ValueError):
                level = 0.0
            mark = (mark_prices or {}).get(o.get("symbol"))
            try:
                mark_f = float(mark) if mark is not None else 0.0
            except (TypeError, ValueError):
                mark_f = 0.0
            placed = o.get("placed_mark")
            try:
                placed_f = float(placed) if placed is not None else 0.0
            except (TypeError, ValueError):
                placed_f = 0.0
            if level <= 0 or mark_f <= 0 or placed_f <= 0:
                continue  # away-leg unprovable -> KEEP (fail-safe)
            dist_now = abs(mark_f - level) / level
            dist_then = abs(placed_f - level) / level
            if (dist_now - dist_then) > 0.005:
                cancel.append(tag)
    return cancel


# ---------------------------------------------------------------------------
# 3. Fill attribution
# ---------------------------------------------------------------------------


def fill_attribution(
    *,
    order_tag: str,
    fills: Sequence[Dict[str, Any]],
    limit_price: Optional[float] = None,
    placed_ts: Optional[float] = None,
) -> float:
    """Attribute exchange fill deltas to a fleet order.

    A fill counts toward order_tag when ALL hold:
      - fill.get("tag") in (None, order_tag)   (untagged fills eligible)
      - fill price within 0.1% of limit_price  (when limit_price given)
      - fill ts >= placed_ts                   (when placed_ts given; both
                                                seconds or both ms — ms
                                                auto-normalized)
    Returns summed attributed quantity.
    """
    try:
        lim = float(limit_price) if limit_price is not None else None
    except (TypeError, ValueError):
        lim = None
    try:
        placed = float(placed_ts) if placed_ts is not None else None
    except (TypeError, ValueError):
        placed = None
    if placed is not None and placed > 1e12:
        placed /= 1000.0

    qty = 0.0
    for f in (fills or []):
        ftag = f.get("tag")
        if ftag is not None and str(ftag) != order_tag:
            continue
        if lim is not None and lim > 0:
            try:
                px = float(f.get("price"))
            except (TypeError, ValueError):
                continue
            if px <= 0 or abs(px - lim) / lim > 0.001:
                continue
        if placed is not None:
            try:
                fts = float(f.get("ts", f.get("ts_ms", 0.0)) or 0.0)
            except (TypeError, ValueError):
                continue
            if fts > 1e12:
                fts /= 1000.0
            if fts < placed:
                continue
        try:
            qty += float(f.get("qty", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
    return qty


# ---------------------------------------------------------------------------
# 4. Residual completion verdict
# ---------------------------------------------------------------------------


def residual_completion_verdict(
    cfg: Any,
    *,
    spec: OrderSpec,
    filled_qty: float,
    target_qty: float,
    mark_price: Optional[float],
    now_ts: float,
) -> ResidualVerdict:
    """What to do with the unfilled remainder of a partially filled spec.

    Within TTL: hold. At/past TTL: filled >= complete_frac (0.6) of target
    completes the residual AT MARKET only while mark is within chase_pct
    (0.3%) of the limit — chasing further kills the wick-harvest edge, so
    cancel. Below complete_frac -> cancel the remainder.
    """
    try:
        target = float(target_qty)
    except (TypeError, ValueError):
        target = 0.0
    if target <= 0:
        return ResidualVerdict("cancel_remainder", "zero_target")
    try:
        filled = float(filled_qty)
    except (TypeError, ValueError):
        filled = 0.0
    if filled >= target:
        return ResidualVerdict("hold", "fully_filled")

    ttl = float(getattr(spec, "ttl_s", 2700.0) or 2700.0)
    created = float(getattr(spec, "created_ts", 0.0) or 0.0)
    if float(now_ts) - created <= ttl:
        return ResidualVerdict("hold", "within_ttl")

    complete_frac = float(_knob(cfg, "anticipator_residual_complete_frac", 0.6))
    chase_pct = float(_knob(cfg, "anticipator_residual_chase_pct", 0.003))
    # NB: chase_pct is a FRACTION (0.003 = 0.3%), unlike the percent knobs.
    frac = filled / target

    if frac < complete_frac:
        return ResidualVerdict("cancel_remainder", "below_complete_frac")

    try:
        mark = float(mark_price) if mark_price is not None else 0.0
    except (TypeError, ValueError):
        mark = 0.0
    lim = float(getattr(spec, "limit_price", 0.0) or 0.0)
    if mark <= 0 or lim <= 0:
        return ResidualVerdict("cancel_remainder", "mark_unknown")
    if abs(mark - lim) / lim <= chase_pct:
        return ResidualVerdict("complete_market", "residual_within_chase_band")
    return ResidualVerdict("cancel_remainder", "chase_would_kill_edge")
