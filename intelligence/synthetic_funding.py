"""
intelligence/synthetic_funding.py — Synthetic Funding Score (SFS) for ARIA.

Design principle:
    "Funding is NOT observed directly — it is inferred as positioning pressure
    embedded in OI + volume + cross-venue lag."

SoDEX is a thin market that does not publish a live funding rate.  This module
reconstructs *equivalent* funding pressure from four observable proxies:

    1. OI pressure     — Open-interest delta × price direction.
                         Rising OI into a rising price = long crowding.
                         Rising OI into a falling price = short crowding.

    2. Volume imbalance — (buy_vol - sell_vol) / total_vol.
                          Positive = buy pressure → long building.

    3. Price divergence — (sodex_price - bybit_price) / bybit_price.
                          Positive = SoDEX at premium → longs crowded here.
                          Set to 0.0 (fail-safe) if either price is unavailable.

    4. Liq alignment   — (long_liq_count - short_liq_count) / total_liq.
                          Positive = more longs liquidated → bearish clearing.

Weighted composite (SFS):
    SFS = 0.30×OI + 0.25×VOL + 0.30×DIV + 0.15×LIQ
    Range ≈ −1.0 to +1.0
    Positive = longs paying (headwind for new longs, tailwind for shorts).
    Negative = shorts paying (bullish for new entries).

SFSCache:
    Thin dict-based cache with per-symbol 500ms staleness guard.
    Returns None for stale entries so callers can decide whether to recompute.
"""

import time
from dataclasses import dataclass
from typing import Dict, Optional

import structlog

log = structlog.get_logger(__name__)

# ── Component weights (must sum to 1.0) ──────────────────────────────────────
_W_OI  = 0.30
_W_VOL = 0.25
_W_DIV = 0.30
_W_LIQ = 0.15

assert abs(_W_OI + _W_VOL + _W_DIV + _W_LIQ - 1.0) < 1e-9, "SFS weights must sum to 1.0"

# Threshold for bias interpretation
_BIAS_THRESHOLD = 0.3

# SFSCache staleness guard
_CACHE_MAX_AGE_S = 0.500  # 500 ms


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SFSResult:
    """
    Immutable snapshot of a single SFS computation.

    All component scores are in the range −1.0 to +1.0 before weighting.
    sfs_score is the final weighted composite.

    Attributes
    ----------
    sfs_score:
        Final weighted SFS. Positive = long crowding (bearish headwind).
        Negative = short crowding (bullish tailwind).
    oi_pressure:
        OI-delta × price-direction component.
    vol_imbalance:
        (buy_vol − sell_vol) / total_vol component.
    price_divergence:
        (sodex_price − bybit_price) / bybit_price component.
        0.0 when either price is unavailable (fail_safe=True).
    liq_alignment:
        (long_liq − short_liq) / total_liq component.
    confidence:
        1.0 when all inputs are fresh; reduced when price data is missing.
    bybit_available:
        True when a non-zero bybit_price was supplied.
    fail_safe:
        True when price_divergence was forced to 0.0 due to missing data.
    """
    sfs_score:        float
    oi_pressure:      float
    vol_imbalance:    float
    price_divergence: float
    liq_alignment:    float
    confidence:       float
    bybit_available:  bool
    fail_safe:        bool


# ── Core computation (pure, stateless, <1 ms) ─────────────────────────────────

def compute_sfs(
    oi_delta_pct:    float = 0.0,
    price_direction: float = 0.0,
    volume_buy:      float = 0.0,
    volume_sell:     float = 0.0,
    bybit_price:     float = 0.0,
    sodex_price:     float = 0.0,
    long_liq_count:  int   = 0,
    short_liq_count: int   = 0,
) -> SFSResult:
    """
    Compute the Synthetic Funding Score from raw market inputs.

    All parameters are optional and default to zero (neutral contribution).
    The function is pure and side-effect-free; call it as frequently as needed.

    Parameters
    ----------
    oi_delta_pct:
        Percentage change in open interest over the last 5 minutes.
        Positive = OI growing (positioning is building).
    price_direction:
        Direction of recent price change: +1.0 = rising, −1.0 = falling,
        0.0 = flat.  Intermediate values are accepted but ±1.0 is typical.
    volume_buy:
        Aggressive buy volume (taker buys) over the measurement window.
    volume_sell:
        Aggressive sell volume (taker sells) over the measurement window.
    bybit_price:
        ByBit perpetual mark price.  0.0 signals "unavailable".
    sodex_price:
        SoDEX mark price.  0.0 signals "unavailable".
    long_liq_count:
        Number of long liquidations in the last 60 seconds.
    short_liq_count:
        Number of short liquidations in the last 60 seconds.

    Returns
    -------
    SFSResult
        Fully populated result; never raises.
    """
    confidence = 1.0
    fail_safe  = False
    bybit_available = bybit_price != 0.0

    # ── Component 1: OI pressure ─────────────────────────────────────────────
    # oi_delta_pct is already a signed percentage.  Multiplying by price
    # direction gives:
    #   +OI × +price → long crowding   → positive pressure
    #   +OI × −price → short crowding  → negative pressure
    #   −OI (any)   → position unwinding → smaller magnitude
    # Clamp to [−1, +1] so an extreme OI print doesn't dominate.
    raw_oi = oi_delta_pct * price_direction
    oi_pressure = max(-1.0, min(1.0, raw_oi))

    # ── Component 2: Volume imbalance ────────────────────────────────────────
    total_vol = volume_buy + volume_sell
    vol_imbalance = (volume_buy - volume_sell) / (total_vol + 1e-9)
    # Already in [−1, +1] by construction.

    # ── Component 3: Price divergence (fail-safe if either price missing) ────
    if sodex_price == 0.0 or bybit_price == 0.0:
        price_divergence = 0.0
        confidence      *= 0.6   # meaningful reduction — cross-venue leg missing
        fail_safe        = True
        if not bybit_available:
            log.debug(
                "sfs_failsafe_no_bybit",
                bybit_price=bybit_price,
                sodex_price=sodex_price,
            )
    else:
        raw_div = (sodex_price - bybit_price) / (bybit_price + 1e-9)
        # Clamp: a 5% divergence is already extreme for a perpetual pair.
        price_divergence = max(-1.0, min(1.0, raw_div * 20.0))
        # Scale by ×20 so a 5% spread maps to 1.0 and a 0.5% spread ≈ 0.1.

    # ── Component 4: Liquidation alignment ──────────────────────────────────
    total_liq = long_liq_count + short_liq_count
    liq_alignment = (long_liq_count - short_liq_count) / (total_liq + 1e-9)
    # Already in [−1, +1] by construction.

    # ── Weighted composite ───────────────────────────────────────────────────
    sfs_score = (
        _W_OI  * oi_pressure
        + _W_VOL * vol_imbalance
        + _W_DIV * price_divergence
        + _W_LIQ * liq_alignment
    )
    # Clamp composite to [−1, +1] for consumers that rely on this range.
    sfs_score = max(-1.0, min(1.0, sfs_score))

    return SFSResult(
        sfs_score        = sfs_score,
        oi_pressure      = oi_pressure,
        vol_imbalance    = vol_imbalance,
        price_divergence = price_divergence,
        liq_alignment    = liq_alignment,
        confidence       = confidence,
        bybit_available  = bybit_available,
        fail_safe        = fail_safe,
    )


# ── Interpretation helpers ────────────────────────────────────────────────────

def sfs_to_bias(sfs_score: float) -> str:
    """
    Translate a raw SFS score to a human-readable funding-pressure bias.

    Returns
    -------
    "bearish"  — longs paying heavily (headwind for new long entries)
    "bullish"  — shorts paying heavily (tailwind for new long entries)
    "neutral"  — no dominant funding pressure
    """
    if sfs_score > _BIAS_THRESHOLD:
        return "bearish"   # longs are crowded — funding pressure opposes new longs
    if sfs_score < -_BIAS_THRESHOLD:
        return "bullish"   # shorts are crowded — funding pressure supports new longs
    return "neutral"


def sfs_confidence_mult(sfs_score: float, candidate_direction: str) -> float:
    """
    Return a position-sizing multiplier based on SFS alignment with the
    proposed trade direction.

    This function NEVER blocks a trade — it only adjusts size slightly.

    Rules
    -----
    - Direction aligns with SFS: 1.1× boost
        e.g. short when SFS is bearish (longs crowded → short is with the
        funding flow)
    - Direction fights SFS:      0.85× penalty
        e.g. long when SFS is bearish (taking on funding headwind)
    - Neutral zone (|sfs| ≤ 0.3): 1.0× (no adjustment)

    Parameters
    ----------
    sfs_score:
        Output of compute_sfs().sfs_score.
    candidate_direction:
        "long" or "short" (case-insensitive).
    """
    direction = candidate_direction.lower()
    bias = sfs_to_bias(sfs_score)

    if bias == "neutral":
        return 1.0

    # SFS bearish = longs crowded = headwind for longs, tailwind for shorts
    if bias == "bearish":
        if direction == "short":
            return 1.1    # aligned: riding the funding pressure
        if direction == "long":
            return 0.85   # fighting the funding pressure
        return 1.0

    # SFS bullish = shorts crowded = headwind for shorts, tailwind for longs
    if bias == "bullish":
        if direction == "long":
            return 1.1    # aligned
        if direction == "short":
            return 0.85   # fighting
        return 1.0

    return 1.0  # unreachable but satisfies type checkers


# ── Per-symbol cache ──────────────────────────────────────────────────────────

class SFSCache:
    """
    Lightweight per-symbol SFS result cache with 500ms staleness guard.

    Thread / asyncio safety:
        Plain dict operations in CPython are GIL-protected.  Because ARIA
        runs intelligence modules in a single asyncio event loop, no further
        synchronisation is needed.

    Usage
    -----
    ::
        result = sfs_cache.get("BTCUSDT")
        if result is None:
            result = compute_sfs(...)
            sfs_cache.update("BTCUSDT", result)
    """

    def __init__(self) -> None:
        # symbol → (SFSResult, monotonic-timestamp-of-computation)
        self._store: Dict[str, tuple] = {}

    def get(self, symbol: str) -> Optional[SFSResult]:
        """
        Return the cached SFSResult for *symbol*, or None if:
          - No entry exists for this symbol.
          - The entry is older than 500ms.

        Callers should treat None as a prompt to recompute.
        """
        entry = self._store.get(symbol)
        if entry is None:
            return None
        result, ts = entry
        age = time.monotonic() - ts
        if age > _CACHE_MAX_AGE_S:
            log.debug(
                "sfs_cache_stale",
                symbol=symbol,
                age_ms=round(age * 1000, 1),
            )
            return None
        return result

    def update(self, symbol: str, result: SFSResult) -> None:
        """Store *result* for *symbol* with the current monotonic timestamp."""
        self._store[symbol] = (result, time.monotonic())

    def invalidate(self, symbol: str) -> None:
        """Explicitly evict a symbol's cached entry (e.g. on reconnect)."""
        self._store.pop(symbol, None)

    def size(self) -> int:
        """Return the number of symbols currently cached (may include stale)."""
        return len(self._store)


# ── Module-level singletons ───────────────────────────────────────────────────
# Import and use these directly; do not instantiate additional copies.
sfs_cache = SFSCache()
