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
    anticipator_level_coverage_enabled   True      (2026-09-27 resilience:
                                         level-bucket idempotency; False =
                                         pre-repair bit-for-bit)
    anticipator_level_tolerance_pct      0.1       (percent scale, /100)
    anticipator_min_rest_s               300.0     (eviction grace; 0.0 =
                                         pre-repair bit-for-bit)
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


# In-place upgrade threshold: a candidate must beat the covering incumbent's
# strength by this much to justify evict-replacing it at the same level.
# Only reachable when anticipator_inplace_upgrade_enabled is True — default
# False since 2026-09-28 (the conveyor: 2,076 placed / 2,032 evicted / 3
# filled in 19h; the upgrade's whole value is <= +$14 margin while the
# venue queue position is lost every refresh cycle).
_REFRESH_STRENGTH_DELTA = 0.15


def _order_side(o: Dict[str, Any]) -> Optional[str]:
    """Side of a resting fleet row: explicit "side" key, else parse the tag
    (ant-{symbol}-{side}-{ms} — symbols themselves contain "-", so the side
    is always the second-to-last dash segment)."""
    s = str(o.get("side") or "").strip().lower()
    if s in ("long", "short"):
        return s
    parts = str(o.get("tag", "")).split("-")
    if len(parts) >= 3:
        s = parts[-2].strip().lower()
        if s in ("long", "short"):
            return s
    return None


def _order_age(o: Dict[str, Any], now_ts: float) -> float:
    """Age of a fleet row in seconds; tolerates ms created_ts."""
    try:
        created = float(o.get("created_ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        created = 0.0
    if created > 1e12:
        created /= 1000.0
    return float(now_ts) - created


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
    size_mult: float = 1.0,
) -> Tuple[List[OrderSpec], List[SkipReason]]:
    """Plan a fleet of resting-limit order specs from predicted levels.

    Returns (specs, skips). skips covers rejected levels AND evictions of
    incumbent fleet orders (reason="evicted", detail=tag).

    open_fleet: dicts with keys tag/symbol/limit_price/created_ts and
    optional strength — the incumbent resting fleet for cap accounting.

    size_mult (2026-09-30 fleet balance scaling): the caller's equity
    float clamp(equity/ref, min, max) — multiplies every rung margin so
    the fixed-USD ladder scales with the balance (Governor directive
    "scale as balance increases or reduced"). 1.0 = legacy bit-for-bit;
    the dust floor still applies AFTER scaling (a scaled-down rung that
    lands sub-floor dies unplaced — fail-safe).
    """
    specs: List[OrderSpec] = []
    skips: List[SkipReason] = []

    if not _knob(cfg, "anticipator_enabled", False):
        return specs, skips

    # Hourly funding clock gate (Governor 2026-09-28 cybernetic paste):
    # SoDEX funding settles HOURLY at :00 — no new placements from :55 to
    # :02 while the settlement reprices the book. Cancel-side maintenance
    # (prune_verdicts) and exits are never gated. Knob False = legacy.
    if _knob(cfg, "anticipator_funding_clock_gate_enabled", True):
        _sec_in_hour = float(now_ts) % 3600.0
        if _sec_in_hour >= 3300.0 or _sec_in_hour < 120.0:
            for lv in (levels or []):
                try:
                    skips.append(SkipReason(float(lv[0]), None,
                                            "funding_window"))
                except Exception:
                    skips.append(SkipReason(None, None, "funding_window"))
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
    # Governor 2026-09-28 margin ladder: per-symbol campaign slot margins
    # ("BTC-USD:80,ETH-USD:55,SOL-USD:35,XRP-USD:20,US500-USD:20,
    # USTECH100-USD:25" — Σ=235 ≤ the 250 budget so every slot can rest).
    # Listed symbols override the global base; malformed entries skipped;
    # empty knob = legacy global bit-for-bit.
    margin_ladder: dict = {}
    for _part in str(_knob(cfg, "anticipator_margin_usd_by_symbol", "") or "").split(","):
        _k, _, _v = _part.partition(":")
        if _k.strip() and _v.strip():
            try:
                margin_ladder[_k.strip()] = float(_v.strip())
            except (TypeError, ValueError):
                continue
    # Governor 2026-09-28 dust floor ("i saw a trade worth 2usd 15x that is
    # dustt we need volume"): a sub-floor margin mints notional the fee leg
    # eats and burns a fleet slot. 0.0 = legacy no-floor bit-for-bit.
    min_margin = float(_knob(cfg, "anticipator_min_margin_usd", 10.0))
    # Resilience knobs (2026-09-27): coverage idempotency + eviction grace.
    # coverage False + min_rest_s 0.0 = pre-repair bit-for-bit.
    coverage_on = bool(_knob(cfg, "anticipator_level_coverage_enabled", True))
    tol = float(_knob(cfg, "anticipator_level_tolerance_pct", 0.1)) / 100.0
    min_rest_s = float(_knob(cfg, "anticipator_min_rest_s", 300.0))

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

    evicted_tags: set = set()
    covered_tags: set = set()

    # --- Drift eviction (2026-09-28 evening, price-relationship audit) -----
    # A resting row whose distance from mark has grown BEYOND the placement
    # band is evicted this tick regardless of age — the audit measured the
    # fleet 1.6-4.3% deep while the stale-cancel needs 45min + a 0.5%
    # away-move, so young orders drifted their whole TTL. Young rows inside
    # the min_rest_s grace keep their protection; an unhealthy mark never
    # reaches here (the fail-closed mark/ATR gate above returns first).
    if bool(_knob(cfg, "anticipator_drift_evict_enabled", True)):
        for o in open_sym:
            tag = str(o.get("tag", ""))
            if not tag or tag in evicted_tags:
                continue
            if min_rest_s > 0.0 and _order_age(o, now_ts) < min_rest_s:
                continue
            try:
                lvl = float(o.get("limit_price", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if lvl > 0 and abs(lvl - mark) / mark > max_d:
                skips.append(SkipReason(None, None, "evicted", tag))
                skips.append(SkipReason(None, None, "drifted_out_of_band",
                                        tag))
                evicted_tags.add(tag)

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

    def _evict(pool: List[Dict[str, Any]], n: int) -> None:
        eligible = pool
        if min_rest_s > 0.0:
            # Eviction grace: orders younger than min_rest_s never feed the
            # capacity conveyor — they rest out their grace and the prune
            # pass owns their lifecycle (2026-09-27 campaign-killer repair:
            # the median order was dying 40-80s after placement).
            eligible = [o for o in pool
                        if _order_age(o, now_ts) >= min_rest_s]
        for o in sorted(eligible, key=_evict_key)[:max(0, n)]:
            tag = str(o.get("tag", ""))
            if tag in evicted_tags or tag in covered_tags:
                continue
            skips.append(SkipReason(None, None, "evicted", tag))
            evicted_tags.add(tag)

    # --- Level-coverage idempotency (2026-09-27 keystone) -------------------
    # With near-static cluster levels, most candidates duplicate coverage the
    # fleet already rests at; placing them re-mints the same level with a
    # fresh epoch tag and feeds the eviction conveyor. A candidate whose
    # (side, level bucket) is already covered is skipped ("covered") and the
    # incumbent is protected from capacity eviction below. Bucketing is per
    # (symbol, side, level within tolerance) — same-symbol probes at
    # DIFFERENT levels remain two trades (Governor ruling 2026-09-26: the
    # ladder is legitimate, never one-order-per-side).
    if coverage_on and tol > 0.0 and open_sym:
        survivors: List[Tuple[float, str, float]] = []
        for price, side, strength in candidates:
            covering: List[Dict[str, Any]] = []
            for o in open_sym:
                if str(o.get("tag", "")) in evicted_tags:
                    continue
                if _order_side(o) != side:
                    continue
                try:
                    lvl = float(o.get("limit_price", 0.0) or 0.0)
                except (TypeError, ValueError):
                    continue
                if lvl > 0 and abs(lvl - price) / price <= tol:
                    covering.append(o)
            if not covering:
                survivors.append((price, side, strength))
                continue
            # Representative incumbent: strongest, ties newest (the survivor
            # under the eviction ordering).
            covering.sort(key=_evict_key)
            best = covering[-1]
            # Duplicates covering the same bucket are evicted immediately,
            # bypassing the rest grace (dedup, not churn).
            for dup in covering[:-1]:
                dtag = str(dup.get("tag", ""))
                if dtag not in evicted_tags:
                    skips.append(SkipReason(None, None, "evicted", dtag))
                    evicted_tags.add(dtag)
            btag = str(best.get("tag", ""))
            try:
                bstrength = float(best.get("strength", 0.0) or 0.0)
            except (TypeError, ValueError):
                bstrength = 0.0
            if (bool(_knob(cfg, "anticipator_inplace_upgrade_enabled", False))
                    and strength >= bstrength + _REFRESH_STRENGTH_DELTA):
                if min_rest_s > 0.0 and _order_age(best, now_ts) < min_rest_s:
                    # A meaningfully stronger read arrived while the incumbent
                    # still rests inside its grace — keep the incumbent.
                    skips.append(SkipReason(price, side, "refresh_grace"))
                    if btag:
                        covered_tags.add(btag)
                    continue
                # In-place upgrade at the same level: evict the weak
                # incumbent (bypasses the rest grace — replacement, not
                # churn) and place the stronger candidate.
                if btag not in evicted_tags:
                    skips.append(SkipReason(None, None, "evicted", btag))
                    evicted_tags.add(btag)
                survivors.append((price, side, strength))
                continue
            skips.append(SkipReason(price, side, "covered"))
            if btag:
                covered_tags.add(btag)
        candidates = survivors

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

        margin = round(margin_ladder.get(symbol, base_margin)
                       * (0.5 + 0.5 * strength)
                       * max(0.0, float(size_mult)), 2)
        if margin < min_margin:  # dust floor: the spec dies, never clamped up
            continue

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
