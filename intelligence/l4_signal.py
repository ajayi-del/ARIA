"""
intelligence/l4_signal.py — L4 Book Signal Intelligence

A quant-grade, always-on signal layer derived entirely from the live L4
orderbook (bid/ask depth, imbalance, spread, wall detection).

Three tiers of output:
  1. ScalpSignal  — sub-60s scalp confirmation gate
     Uses top-3 level imbalance + spread tightness.
     Confirms or blocks momentum scalp entries.

  2. SwingContext — 1h–6h position context
     Uses depth ratio vs baseline + wall detection to predict
     likely support/resistance for stop placement and TP anchoring.

  3. FillQuality  — order type recommendation + slippage estimate
     Determines market vs limit; flags blown spreads pre-execution.

Key design decisions (quant rationale):
  - L4 is used for CONFIRMATION only — never as a sole entry signal.
    L4 can be spoofed; its value is correlation with price action, not direction.
  - Imbalance is computed over top 5 levels (not full book) — deep book
    orders are strategic spoofs; top-of-book is execution pressure.
  - Wall detection: a single level with >10× the average level size = a wall.
    Walls act as TP magnets (price sweeps to wall, triggers TPs, then reverses).
  - Exit safety: if spread > 2× baseline AND depth < 30% baseline, do not close.
    Closing into a blown spread costs 2-4x normal slippage.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ScalpSignal:
    """
    L4 confirmation for sub-60s scalp entries.
    confirmed=True means L4 book aligns with proposed direction.
    """
    confirmed:     bool
    direction:     str      # "long" | "short" | "neutral"
    imbalance:     float    # raw top-5 imbalance: +1=all bids, -1=all asks
    spread_bps:    float    # current spread in basis points
    confidence:    float    # 0.0–1.0 — how strong is the L4 confirmation
    reason:        str


@dataclass(frozen=True)
class SwingContext:
    """
    L4 structural context for swing trades (1h–6h).
    Provides support/resistance walls and depth regime.
    """
    bid_wall_price:  Optional[float]    # nearest large bid wall below price (support)
    ask_wall_price:  Optional[float]    # nearest large ask wall above price (resistance)
    bid_wall_str:    float              # wall strength 0–1 (vol relative to avg level)
    ask_wall_str:    float
    depth_regime:    str                # "depleted" | "thin" | "normal" | "thick"
    depth_ratio:     float              # current / baseline
    tp_anchor_pct:   Optional[float]    # suggested TP distance as % of entry (wall-based)
    stop_clear_pct:  Optional[float]    # suggested stop just beyond wall on other side


@dataclass(frozen=True)
class FillQuality:
    """
    Pre-execution fill quality assessment.
    """
    order_type:        str      # "limit" | "market" | "defer"
    spread_bps:        float
    spread_ok:         bool     # spread <= 2x baseline
    est_slippage_pct:  float    # estimated slippage as % of entry
    depth_ok:          bool     # enough depth to absorb the order size
    should_defer:      bool     # True = delay execution (blown spread + thin depth)


# ── Constants ─────────────────────────────────────────────────────────────────

_IMBALANCE_CONFIRM_LONG  = 0.25   # bid vol > ask vol by 25% → bullish L4 pressure
_IMBALANCE_CONFIRM_SHORT = -0.25  # ask vol > bid vol by 25% → bearish L4 pressure
_WALL_MULTIPLIER         = 8.0    # level > 8× avg level size = wall
_WALL_SCAN_LEVELS        = 20     # how far into the book to scan for walls
_SPREAD_BLOWN_RATIO      = 2.5    # spread > 2.5× baseline = blown
_SPREAD_LIMIT_BPS        = 8.0    # spread < 8bps = tight enough for limit order
_DEPTH_DEPLETED          = 0.30   # depth < 30% of baseline = depleted
_DEPTH_THIN              = 0.60   # depth < 60% of baseline = thin
_MAX_BOOK_AGE_MS         = 8_000  # 8s max age — stale book = neutral signal


# ── Core functions ─────────────────────────────────────────────────────────────

def get_scalp_signal(
    ob,                         # OrderbookStore instance
    direction: str,             # proposed "long" | "short"
    entry_price: float,
) -> ScalpSignal:
    """
    Gate scalp entries with L4 book confirmation.

    A scalp signal is confirmed when:
      - L4 imbalance aligns with direction (buy pressure for long, sell for short)
      - Spread is < 10bps (tight enough to not eat the edge)
      - Book is fresh (< 8s old)

    Returns ScalpSignal with confirmed=False if any condition fails.
    """
    _neutral = ScalpSignal(
        confirmed=False, direction="neutral",
        imbalance=0.0, spread_bps=0.0,
        confidence=0.0, reason="no_l4_data"
    )

    if ob is None or entry_price <= 0:
        return _neutral

    if ob.age_ms() > _MAX_BOOK_AGE_MS:
        return ScalpSignal(
            confirmed=False, direction="neutral",
            imbalance=0.0, spread_bps=9999.0,
            confidence=0.0, reason="l4_book_stale"
        )

    try:
        bid, ask, spread = ob.top_of_book()
    except Exception:
        return _neutral

    if bid <= 0 or ask <= 0:
        return _neutral

    spread_bps = (spread / entry_price) * 10_000
    imbalance  = ob.imbalance(depth=5)

    # Confirm direction via imbalance
    if direction == "long":
        if imbalance >= _IMBALANCE_CONFIRM_LONG:
            # Strong bid-side pressure
            confidence = min(1.0, imbalance / 0.5) * (1.0 - spread_bps / 50.0)
            confirmed  = spread_bps < 10.0 and confidence > 0.3
            reason     = f"l4_long_confirmed_imb={imbalance:.2f}_spread={spread_bps:.1f}bps"
        elif imbalance < -0.40:
            # Actively bearish book — block long
            return ScalpSignal(
                confirmed=False, direction="short",
                imbalance=imbalance, spread_bps=spread_bps,
                confidence=0.8, reason=f"l4_book_contradicts_long_imb={imbalance:.2f}"
            )
        else:
            confirmed  = False
            confidence = 0.0
            reason     = f"l4_neutral_for_long_imb={imbalance:.2f}"
    elif direction == "short":
        if imbalance <= _IMBALANCE_CONFIRM_SHORT:
            confidence = min(1.0, abs(imbalance) / 0.5) * (1.0 - spread_bps / 50.0)
            confirmed  = spread_bps < 10.0 and confidence > 0.3
            reason     = f"l4_short_confirmed_imb={imbalance:.2f}_spread={spread_bps:.1f}bps"
        elif imbalance > 0.40:
            return ScalpSignal(
                confirmed=False, direction="long",
                imbalance=imbalance, spread_bps=spread_bps,
                confidence=0.8, reason=f"l4_book_contradicts_short_imb={imbalance:.2f}"
            )
        else:
            confirmed  = False
            confidence = 0.0
            reason     = f"l4_neutral_for_short_imb={imbalance:.2f}"
    else:
        return _neutral

    return ScalpSignal(
        confirmed=confirmed, direction=direction,
        imbalance=round(imbalance, 4),
        spread_bps=round(spread_bps, 2),
        confidence=round(max(0.0, confidence), 3),
        reason=reason,
    )


def get_swing_context(
    ob,
    entry_price: float,
    direction: str,
    depth_baseline: float = 0.0,   # from CascadeBasketIntelligence._depth_baselines
    spread_baseline_bps: float = 0.0,
) -> SwingContext:
    """
    L4 structural context for swing position management.

    Detects walls (large standing orders) that act as:
      - Support (bid walls below) → stop anchor + TP magnet on bounce
      - Resistance (ask walls above) → TP target + entry confirmation for shorts

    Also classifies depth regime vs baseline to inform whether to trade
    into current conditions or defer.
    """
    _empty = SwingContext(
        bid_wall_price=None, ask_wall_price=None,
        bid_wall_str=0.0, ask_wall_str=0.0,
        depth_regime="normal", depth_ratio=1.0,
        tp_anchor_pct=None, stop_clear_pct=None,
    )

    if ob is None or entry_price <= 0:
        return _empty

    if ob.age_ms() > _MAX_BOOK_AGE_MS:
        return _empty

    try:
        bid, ask, spread = ob.top_of_book()
    except Exception:
        return _empty

    if bid <= 0:
        return _empty

    # ── Depth regime ──────────────────────────────────────────────────────────
    try:
        total_depth = ob.depth_usd(side="both", levels=5)
    except Exception:
        total_depth = 0.0

    depth_ratio = (total_depth / depth_baseline) if depth_baseline > 0 else 1.0
    depth_ratio = min(2.0, max(0.0, depth_ratio))

    if depth_ratio < _DEPTH_DEPLETED:
        depth_regime = "depleted"
    elif depth_ratio < _DEPTH_THIN:
        depth_regime = "thin"
    elif depth_ratio > 1.4:
        depth_regime = "thick"
    else:
        depth_regime = "normal"

    # ── Wall detection ────────────────────────────────────────────────────────
    # Detect oversized levels in the top-N book.
    # Wall = level with notional >= WALL_MULTIPLIER × avg level notional.
    bid_wall_price, bid_wall_str = _detect_wall(ob.bids, entry_price, side="bid")
    ask_wall_price, ask_wall_str = _detect_wall(ob.asks, entry_price, side="ask")

    # ── TP / Stop anchors from walls ──────────────────────────────────────────
    # For longs: ask wall above = TP magnet; bid wall below = stop anchor
    # For shorts: bid wall below = TP magnet; ask wall above = stop anchor
    tp_anchor_pct   = None
    stop_clear_pct  = None

    if direction == "long":
        if ask_wall_price and ask_wall_price > entry_price:
            tp_anchor_pct  = (ask_wall_price - entry_price) / entry_price
        if bid_wall_price and bid_wall_price < entry_price:
            stop_clear_pct = (entry_price - bid_wall_price) / entry_price * 1.02  # 2% below wall
    elif direction == "short":
        if bid_wall_price and bid_wall_price < entry_price:
            tp_anchor_pct  = (entry_price - bid_wall_price) / entry_price
        if ask_wall_price and ask_wall_price > entry_price:
            stop_clear_pct = (ask_wall_price - entry_price) / entry_price * 1.02

    return SwingContext(
        bid_wall_price=bid_wall_price,
        ask_wall_price=ask_wall_price,
        bid_wall_str=round(bid_wall_str, 3),
        ask_wall_str=round(ask_wall_str, 3),
        depth_regime=depth_regime,
        depth_ratio=round(depth_ratio, 3),
        tp_anchor_pct=round(tp_anchor_pct, 5) if tp_anchor_pct else None,
        stop_clear_pct=round(stop_clear_pct, 5) if stop_clear_pct else None,
    )


def get_fill_quality(
    ob,
    entry_price: float,
    order_size_usd: float,
    coherence: float = 0.0,
    spread_baseline_bps: float = 0.0,
    depth_baseline_usd: float = 0.0,
) -> FillQuality:
    """
    Pre-execution fill quality assessment.

    Determines:
      1. Whether to use limit or market order
      2. Whether the current spread is acceptable
      3. Whether to defer entirely (blown spread + thin book)

    Quant rationale:
      - Limit orders save half the spread = edge preservation
      - But limit orders risk non-fill on momentum moves → use market
      - At coherence >= 7.5: momentum likely → prefer market for certainty
      - At coherence < 6.0: signal is moderate → prefer limit to save edge
      - Blown spread + thin depth = structural regime change → defer
    """
    _bad = FillQuality(
        order_type="market", spread_bps=9999.0,
        spread_ok=False, est_slippage_pct=0.5,
        depth_ok=True, should_defer=False,
    )

    if ob is None or entry_price <= 0:
        return _bad

    if ob.age_ms() > _MAX_BOOK_AGE_MS:
        return _bad

    try:
        bid, ask, spread = ob.top_of_book()
    except Exception:
        return _bad

    if bid <= 0 or ask <= 0:
        return _bad

    spread_bps = (spread / entry_price) * 10_000
    est_slippage_pct = (spread_bps / 10_000) * 100.0 / 2.0  # half-spread

    # ── Spread check vs baseline ──────────────────────────────────────────────
    if spread_baseline_bps > 0:
        spread_ratio = spread_bps / spread_baseline_bps
        spread_ok    = spread_ratio <= _SPREAD_BLOWN_RATIO
    else:
        spread_ok = spread_bps <= 20.0   # fallback: 20bps hard cap

    # ── Depth check ───────────────────────────────────────────────────────────
    try:
        total_depth = ob.depth_usd(side="both", levels=5)
    except Exception:
        total_depth = order_size_usd * 10   # assume enough

    depth_ok = total_depth >= order_size_usd * 3.0  # need 3x order size in book

    # ── Defer gate ────────────────────────────────────────────────────────────
    # If spread is blown AND depth is thin AND depth < 40% of baseline:
    # the book is in a stress state — entering now means buying into a vacuum.
    depth_ratio = total_depth / max(depth_baseline_usd, total_depth) if depth_baseline_usd > 0 else 1.0
    should_defer = (not spread_ok) and (depth_ratio < 0.40)

    # ── Order type selection ──────────────────────────────────────────────────
    # High coherence (momentum) → market (certainty over edge preservation)
    # Tight spread + low coherence → limit (save the spread)
    # Blown spread → market (don't wait, price may move further against us)
    if should_defer:
        order_type = "defer"
    elif coherence >= 7.5:
        # Strong momentum — fill certainty matters more than spread cost
        order_type = "market"
    elif spread_bps <= _SPREAD_LIMIT_BPS and not should_defer:
        order_type = "limit"
    else:
        order_type = "market"

    return FillQuality(
        order_type=order_type,
        spread_bps=round(spread_bps, 2),
        spread_ok=spread_ok,
        est_slippage_pct=round(est_slippage_pct, 4),
        depth_ok=depth_ok,
        should_defer=should_defer,
    )


# ── Internal helpers ───────────────────────────────────────────────────────────

def _detect_wall(
    levels: list,       # [(price, qty), ...] already sorted (bids desc / asks asc)
    reference_price: float,
    side: str,          # "bid" | "ask"
    scan_depth: int = _WALL_SCAN_LEVELS,
) -> tuple[Optional[float], float]:
    """
    Detect the nearest large standing order (wall) in the book.

    OrderbookStore.bids is sorted descending (best bid first).
    OrderbookStore.asks is sorted ascending (best ask first).
    We use the first scan_depth entries as-is — no re-sort needed.

    Returns (wall_price, wall_strength) where wall_strength is
    the level's notional relative to average level notional (0–1).
    Returns (None, 0.0) if no wall found.
    """
    if not levels or reference_price <= 0:
        return None, 0.0

    top_n = levels[:scan_depth]
    if not top_n:
        return None, 0.0

    # Compute average notional per level
    notionals = [p * q for p, q in top_n if p > 0 and q > 0]
    if not notionals:
        return None, 0.0
    avg_notional = sum(notionals) / len(notionals)
    if avg_notional <= 0:
        return None, 0.0

    # Find the largest level that qualifies as a wall
    best_wall_price = None
    best_wall_str   = 0.0

    for price, qty in top_n:
        if price <= 0 or qty <= 0:
            continue
        notional = price * qty
        wall_str = notional / avg_notional
        if wall_str >= _WALL_MULTIPLIER and wall_str > best_wall_str:
            best_wall_price = price
            best_wall_str   = wall_str

    if best_wall_price is None:
        return None, 0.0

    # Normalize: wall_str maps [WALL_MULTIPLIER, WALL_MULTIPLIER*4] → [0, 1]
    normalized_str = min(1.0, (best_wall_str - _WALL_MULTIPLIER) / (_WALL_MULTIPLIER * 3))
    return best_wall_price, normalized_str
