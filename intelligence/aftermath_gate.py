"""Cascade-aftermath TWO-CONDITION entry gate (Governor directive 2026-09-15 —
Bybit AI structural model, ordered built).

The aftermath entry today fires at a fixed 90s with zero depth/imbalance
conditions. The Bybit structural model says a post-cascade fade entry is
eligible only when:

  1. TIME  — a tier-scaled minimum delay has passed since the cascade batch
             (larger forced flow needs longer to exhaust). ALWAYS binds —
             seconds_since_cascade is always computable.
  2. DEPTH — L4 top-5 depth has recovered >= a tier-scaled fraction of its
             pre-cascade baseline (cascade_basket.get_depth_ratio). Dark L4
             plane (None) ABSTAINS — a dead data plane must never block
             entries (fail-open), it simply does not vote.
  3. IMBALANCE — the entry-side top-5 book imbalance favors the fade:
             LONG fade requires bid_usd/ask_usd >= floor, SHORT fade
             ask_usd/bid_usd >= floor. None abstains (fail-open).

Verdicts: "allow_full" | "allow_half" | "block". A depth ratio recovered
into the [tier floor, half_size_depth_ceiling) band earns HALF size (the
degradation ladder — same doctrine as the ETF tide haircut), never a block.

Governor-stamped constants — CLOSED to tuning until the shadow gate
"aftermath_gate" has n>=30 scored records (Aronson: no verdict on noise).
MIN_DELAY_S is the Bybit medium-row averaged across assets as the ARIA
default ladder: cascade batches here are market-wide (cross-symbol
liquidation clusters), not single-asset events, so the medium row is the
honest prior rather than any per-asset row.

Zero-I/O pure brain: injected values only, never raises, fail-open ALLOW on
garbage input. Book grounding:
- Hasbrouck: forced-flow impact decays on a measurable half-life — the time
  leg is the half-life made mechanical.
- O'Hara (PIN): post-shock book imbalance carries the informed/uninformed
  split — fading into a still-skewed book is trading against the informed.
"""

# ── Governor-stamped constants (CLOSED until shadow n>=30) ───────────────────
MIN_DELAY_S = {"small": 90, "medium": 120, "large": 180, "whale": 300}
DEPTH_FLOOR = {"small": 0.55, "medium": 0.60, "large": 0.65, "whale": 0.70}
IMBALANCE_FLOOR = 1.20  # entry-side top-5 ratio must favor the fade by >= 20%

# Tier boundaries (cascade batch notional, USD)
_TIER_MEDIUM_MIN = 500_000.0
_TIER_LARGE_MIN = 2_000_000.0
_TIER_WHALE_MIN = 5_000_000.0


def classify_tier(batch_notional_usd) -> str:
    """Tier from cascade batch notional. Garbage/missing -> "small" (the
    shortest delay = most permissive, fail-open: a missing notional read must
    not manufacture a longer lockout than the data justifies)."""
    try:
        n = float(batch_notional_usd)
    except (TypeError, ValueError):
        return "small"
    if n != n or n < 0:  # NaN / negative
        return "small"
    if n >= _TIER_WHALE_MIN:
        return "whale"
    if n >= _TIER_LARGE_MIN:
        return "large"
    if n >= _TIER_MEDIUM_MIN:
        return "medium"
    return "small"


def _finite_or_none(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def aftermath_verdict(*, tier, seconds_since_cascade, session_mult,
                      depth_ratio, entry_side_imbalance,
                      half_size_depth_ceiling: float = 0.75,
                      imbalance_floor: float = IMBALANCE_FLOOR) -> tuple:
    """Return (verdict, reason).

    verdict in {"allow_full", "allow_half", "block"}.
    block reasons: "time_gate" | "depth_gate" | "imbalance_gate".
    Ordering: TIME outranks DEPTH outranks IMBALANCE — the first failing leg
    names the reason. None (dark data) on depth/imbalance skips that leg;
    the time leg always binds. Never raises — any internal failure returns
    ("allow_full", "gate_error") so a brain defect can never block entries.
    """
    try:
        t = tier if tier in MIN_DELAY_S else "small"

        # ── Leg 1: TIME (always binds) ──
        sm = _finite_or_none(session_mult)
        if sm is None or sm <= 0:
            sm = 1.0
        secs = _finite_or_none(seconds_since_cascade)
        if secs is None or secs < 0:
            secs = 0.0
        if secs < MIN_DELAY_S[t] * sm:
            return "block", "time_gate"

        # ── Leg 2: DEPTH (None abstains) ──
        depth_half = False
        dr = _finite_or_none(depth_ratio)
        if dr is not None:
            if dr < DEPTH_FLOOR[t]:
                return "block", "depth_gate"
            ceiling = _finite_or_none(half_size_depth_ceiling)
            if ceiling is not None and dr < ceiling:
                depth_half = True

        # ── Leg 3: IMBALANCE (None abstains) ──
        imb = _finite_or_none(entry_side_imbalance)
        if imb is not None:
            floor = _finite_or_none(imbalance_floor)
            if floor is None or floor <= 0:
                floor = IMBALANCE_FLOOR
            if imb < floor:
                return "block", "imbalance_gate"

        if depth_half:
            return "allow_half", "depth_band"
        return "allow_full", "ok"
    except Exception:
        return "allow_full", "gate_error"
