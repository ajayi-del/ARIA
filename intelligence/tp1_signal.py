"""intelligence/tp1_signal.py — TP1-as-signal brain (pure, zero-I/O).

2026-10-24 (Governor axiom stack): a confirmed TP1 is not the end of a trade
— it is a SIGNAL. The moment TP1 banks, the market has proven the direction
and printed a future S/R landmark; this module computes what happens next:
whether the carrier pyramids, trails, or closes (funding gate), where the
resting retrace limits sit, how big each pyramid leg may be, whether a
with-trend scalp fires into the retracement, and the lifecycle state machine
the orchestrator drives.

Doctrine:
  Livermore  — the pyramid is a tactical add to a PROVEN winner; TP1 is the
               proof. Adds rest at retracement levels, never chase.
  Freeman-Shor — winners must not become losers; the funding gate's
               trail_exit/close_rest verdicts tighten before the give-back.
  Aronson    — every degree of freedom is a bounded knob; verdicts are pure
               functions of injected inputs, thresholds are parameters.
  Hasbrouck  — funding is positioning made visible: crowded-against-you at
               TP1 = squeeze fuel remains (pyramid); crowded-with-you =
               reversal risk (close_rest).

Fail-closed everywhere: degenerate geometry returns None, unknown funding
returns the middle verdict, illegal transitions return the current state.
No I/O, no imports from main/config — the orchestrator splices.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ── Knob defaults (orchestrator mirrors these in core/config.py) ─────────────
FUNDING_PYRAMID_THRESH = 1e-5    # native venue signed funding decimal
FUNDING_CLOSE_THRESH = 3e-5
RETRACE_FRAC = 0.38              # L1 sits 38% of the entry→TP1 move back
L2_BUFFER_FRAC = 0.005           # L2 rests just past breakeven (+0.5% / −0.5%)
PYRAMID_LADDER = (0.5, 0.25, 0.25)   # leg notionals as multiples of original
MAX_TOTAL_MULT = 1.75            # original + legs may never exceed 1.75x
SCALP_MIN_RETRACE_PCT = 0.015
SCALP_TP_PCT = 0.005
SCALP_STOP_PCT = 0.004
LOWER_LIMIT_DIPS = (0.01, 0.025, 0.04)
LOWER_LIMIT_SIZES = (1.0, 1.0, 0.5)

# ── Axiom 8: lifecycle states ────────────────────────────────────────────────
TP1_STATES = ("IDLE", "TP1_FIRED", "L1_PENDING", "L1_ACTIVE",
              "EXHAUSTED", "CLOSED")

_TRANSITIONS = {
    ("IDLE", "tp1_fired"): "TP1_FIRED",
    ("TP1_FIRED", "l1_placed"): "L1_PENDING",
    ("TP1_FIRED", "verdict_close_rest"): "CLOSED",
    ("TP1_FIRED", "ttl_expired"): "EXHAUSTED",
    ("TP1_FIRED", "carrier_closed"): "CLOSED",
    ("L1_PENDING", "l1_filled"): "L1_ACTIVE",
    ("L1_PENDING", "ttl_expired"): "EXHAUSTED",
    ("L1_PENDING", "verdict_close_rest"): "CLOSED",
    ("L1_PENDING", "carrier_closed"): "CLOSED",
    ("L1_ACTIVE", "l2_filled"): "EXHAUSTED",
    ("L1_ACTIVE", "ttl_expired"): "EXHAUSTED",
    ("L1_ACTIVE", "verdict_close_rest"): "CLOSED",
    ("L1_ACTIVE", "carrier_closed"): "CLOSED",
}


def _round_tick(price: float, tick: float) -> float:
    """Round to the injected tick grid (nearest tick, float-dust cleaned)."""
    return round(round(price / tick) * tick, 12)


# ── Axiom 1: funding gate at TP1 (direction-aware) ───────────────────────────
def funding_verdict(side: str, funding_rate: Optional[float],
                    pyramid_thresh: float = FUNDING_PYRAMID_THRESH,
                    close_thresh: float = FUNDING_CLOSE_THRESH) -> str:
    """Verdict at TP1 from the native venue's signed funding rate.

    LONG carrier:  funding < -pyramid_thresh → "pyramid"  (shorts crowded,
                   squeeze fuel remains); funding > +close_thresh →
                   "close_rest" (longs crowded, reversal risk); between
                   (inclusive) → "trail_exit" (fuel normalizing — tighten,
                   no adds). SHORT carrier mirrored. funding None or
                   unparsable → "trail_exit" (fail-closed to the middle).
    """
    if side not in ("long", "short"):
        return "trail_exit"
    if funding_rate is None:
        return "trail_exit"
    try:
        f = float(funding_rate)
    except (TypeError, ValueError):
        return "trail_exit"

    if side == "long":
        if f < -pyramid_thresh:
            return "pyramid"
        if f > close_thresh:
            return "close_rest"
        return "trail_exit"
    # short carrier: mirrored
    if f > pyramid_thresh:
        return "pyramid"
    if f < -close_thresh:
        return "close_rest"
    return "trail_exit"


# ── Axiom 2: retracement targets (resting pyramid limits) ────────────────────
def retrace_targets(entry: float, tp1: float, side: str, tick: float,
                    retrace_frac: float = RETRACE_FRAC,
                    l2_buffer_frac: float = L2_BUFFER_FRAC
                    ) -> Optional[tuple[float, float]]:
    """(L1, L2) resting limit prices placed the moment TP1 fires, tick-rounded.

    LONG:  L1 = tp1 − retrace_frac × (tp1 − entry);  L2 = entry × (1 + buffer)
    SHORT: L1 = tp1 + retrace_frac × (entry − tp1);  L2 = entry × (1 − buffer)

    None on degenerate input: entry ≤ 0, tick ≤ 0, bad side, or tp1 on the
    wrong side of entry (a long whose TP1 sits below entry never fires).
    """
    try:
        e, t, k = float(entry), float(tp1), float(tick)
    except (TypeError, ValueError):
        return None
    if e <= 0 or k <= 0 or side not in ("long", "short"):
        return None
    if side == "long" and t <= e:
        return None
    if side == "short" and t >= e:
        return None

    move = abs(t - e)
    if side == "long":
        l1 = t - retrace_frac * move
        l2 = e * (1.0 + l2_buffer_frac)
    else:
        l1 = t + retrace_frac * move
        l2 = e * (1.0 - l2_buffer_frac)
    return _round_tick(l1, k), _round_tick(l2, k)


# ── Axiom 3: pyramid sizing ladder ───────────────────────────────────────────
def pyramid_leg_notional(orig_notional: float, leg_index: int,
                         ladder: tuple = PYRAMID_LADDER,
                         max_total_mult: float = MAX_TOTAL_MULT) -> float:
    """Notional for pyramid leg `leg_index` (0-based) as a USD figure.

    leg 0 = ladder[0]× original, etc. The CUMULATIVE notional (original +
    all legs, computed as if every prior ladder leg filled in full) may never
    exceed max_total_mult × original — a leg is clamped to the remaining
    headroom (0.0 when the cap is already spent). leg_index beyond the
    ladder → 0.0. Degenerate original ≤ 0 → 0.0.

    Note (interpretation): with the default ladder (0.5, 0.25, 0.25) and cap
    1.75 the cap binds at leg 2 — original(1.0) + 0.5 + 0.25 = 1.75 leaves
    zero headroom, so leg 2 returns 0.0. Loosening the cap (or shortening
    the ladder) is the knob-level way to admit a third add.
    """
    try:
        orig = float(orig_notional)
        idx = int(leg_index)
    except (TypeError, ValueError):
        return 0.0
    if orig <= 0 or idx < 0 or idx >= len(ladder):
        return 0.0
    already_mult = 1.0 + sum(ladder[:idx])
    headroom = max(0.0, float(max_total_mult) - already_mult)
    return orig * min(float(ladder[idx]), headroom)


@dataclass
class Tp1Track:
    """Stateful track for one position's post-TP1 lifecycle.

    legs_filled holds (price, qty, ts) tuples appended by the orchestrator
    as resting limits fill. cumulative_mult() = total position notional ÷
    original notional (legs marked at their fill price, original at entry);
    1.0 when no legs have filled or orig_qty is unknown (fail-safe).
    """
    symbol: str
    side: str
    entry: float
    tp1_price: float
    fired_ts: float
    funding_at_tp1: Optional[float] = None
    orig_qty: float = 0.0
    legs_filled: list = field(default_factory=list)
    l1_order_id: Optional[str] = None
    l2_order_id: Optional[str] = None
    l1_placed_ts: Optional[float] = None
    verdict: str = "trail_exit"
    scalp_active: bool = False
    state: str = "IDLE"

    def cumulative_mult(self) -> float:
        try:
            e, q = float(self.entry), float(self.orig_qty)
        except (TypeError, ValueError):
            return 1.0
        if e <= 0 or q <= 0:
            return 1.0
        orig_notional = e * q
        legs_notional = 0.0
        for leg in self.legs_filled:
            try:
                legs_notional += float(leg[0]) * float(leg[1])
            except (TypeError, ValueError, IndexError):
                continue
        return 1.0 + legs_notional / orig_notional


# ── Axiom 4: scalp trigger verdict ───────────────────────────────────────────
def scalp_trigger_verdict(retrace_pct: float, decelerating: bool,
                          funding_verdict_str: str, scalp_already_active: bool,
                          min_retrace_pct: float = SCALP_MIN_RETRACE_PCT
                          ) -> tuple[bool, str]:
    """With-trend scalp on the TP1 retracement — fires into the scalper sleeve.

    Fires only when ALL hold: funding verdict is "pyramid", the retrace is
    deep enough (≥ min_retrace_pct), the counter-move is losing steam
    (decelerating), and no scalp is already active on this track. Skip
    reasons, checked in order: funding_not_pyramid, retrace_too_shallow,
    retrace_accelerating, scalp_active.
    """
    if funding_verdict_str != "pyramid":
        return False, "funding_not_pyramid"
    try:
        r = float(retrace_pct)
    except (TypeError, ValueError):
        return False, "retrace_too_shallow"
    if r < float(min_retrace_pct):
        return False, "retrace_too_shallow"
    if not decelerating:
        return False, "retrace_accelerating"
    if scalp_already_active:
        return False, "scalp_active"
    return True, "fire"


# ── Axiom 5: scalp geometry ──────────────────────────────────────────────────
def scalp_bracket(entry_price: float, side: str,
                  tp_pct: float = SCALP_TP_PCT,
                  stop_pct: float = SCALP_STOP_PCT,
                  tick: float = 0.0) -> Optional[tuple[float, float]]:
    """(tp, stop) for the scalp, side-mirrored and tick-rounded.

    LONG:  tp = entry × (1 + tp_pct),  stop = entry × (1 − stop_pct)
    SHORT: tp = entry × (1 − tp_pct),  stop = entry × (1 + stop_pct)
    None on degenerate input (entry ≤ 0, tick ≤ 0, bad side, negative pcts).
    """
    try:
        e, k = float(entry_price), float(tick)
        tp_p, sl_p = float(tp_pct), float(stop_pct)
    except (TypeError, ValueError):
        return None
    if e <= 0 or k <= 0 or side not in ("long", "short"):
        return None
    if tp_p <= 0 or sl_p <= 0:
        return None
    if side == "long":
        return _round_tick(e * (1.0 + tp_p), k), _round_tick(e * (1.0 - sl_p), k)
    return _round_tick(e * (1.0 - tp_p), k), _round_tick(e * (1.0 + sl_p), k)


# ── Axiom 6: lower-limit entry ladder (STAGED — orchestrator default-OFF) ────
def lower_limit_ladder(price: float, side: str, tick: float,
                       dips: tuple = LOWER_LIMIT_DIPS,
                       sizes: tuple = LOWER_LIMIT_SIZES) -> list:
    """Resting lower-limit entries below (LONG) / above (SHORT) `price`.

    Every row carries stage="staged" — the feature ships dark; the
    orchestrator wires it default-OFF and the stamp marks every record so
    downstream consumers can never mistake it for a live ladder.
    Returns [] on degenerate input (price ≤ 0, tick ≤ 0, bad side).
    """
    try:
        p, k = float(price), float(tick)
    except (TypeError, ValueError):
        return []
    if p <= 0 or k <= 0 or side not in ("long", "short"):
        return []
    rows = []
    for i, dip in enumerate(dips):
        d = float(dip)
        px = p * (1.0 - d) if side == "long" else p * (1.0 + d)
        size_frac = float(sizes[i]) if i < len(sizes) else 0.0
        rows.append({
            "level": i + 1,
            "price": _round_tick(px, k),
            "size_frac": size_frac,
            "stage": "staged",
        })
    return rows


# ── Axiom 7: landmark record (TP1 prices are future S/R landmarks) ───────────
def landmark_record(symbol: str, side: str, tp1_price: float,
                    fired_ts: float, entry_price: float,
                    trade_class: str) -> dict:
    """JSONL-ready landmark row. Pure builder — the caller appends.

    move_pct = |tp1 − entry| / entry; None when entry ≤ 0 (fail-closed —
    a landmark with an unmeasurable move is still a landmark).
    """
    try:
        e, t = float(entry_price), float(tp1_price)
    except (TypeError, ValueError):
        e, t = 0.0, 0.0
    move_pct = abs(t - e) / e if e > 0 else None
    return {
        "ts": fired_ts,
        "symbol": symbol,
        "side": side,
        "tp1_price": t,
        "entry_price": e,
        "move_pct": move_pct,
        "trade_class": trade_class,
    }


# ── Axiom 8: state machine ───────────────────────────────────────────────────
def next_state(track: Tp1Track, event: str) -> tuple[str, bool]:
    """Pure transition: returns (new_state, transition_ok).

    Illegal transitions (unknown event, or no edge from the current state)
    return the track's current state unchanged with transition_ok=False.
    Never mutates the track — the orchestrator assigns the result.
    """
    current = getattr(track, "state", "IDLE")
    nxt = _TRANSITIONS.get((current, event))
    if nxt is None:
        return current, False
    return nxt, True
