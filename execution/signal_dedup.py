"""
execution/signal_dedup.py — Hash-based signal deduplication for ARIA.

Prevents the same directional signal from executing twice within a rolling
time window per symbol.  Designed for asyncio — no threading locks required.

Key design decisions:
  - Hash key = (symbol, direction, strategy_tag, 30-second time bucket).
    Two signals that arrive in the same 30-second bucket with identical
    metadata are considered identical and the second is rejected.
  - Cascade signals use a tighter 10-second bucket because cascade trades
    are highly time-sensitive; stale cascade dedup would be harmful.
  - TTL enforcement is lazy (on every is_duplicate check) rather than via
    a background task.  This keeps the implementation simple and avoids any
    asyncio task lifecycle concerns.
  - Never blocks a trade due to an internal error — all exceptions are
    caught and treated as "not a duplicate" so the signal passes through.
"""

import hashlib
import time
from typing import Dict

import structlog

log = structlog.get_logger(__name__)

# ── Time-bucket widths ────────────────────────────────────────────────────────
_STANDARD_BUCKET_S: int = 30   # 30-second dedup window for normal signals
_CASCADE_BUCKET_S: int  = 10   # 10-second dedup window for cascade signals


def _bucket(strategy_tag: str, now: float) -> int:
    """
    Return the integer time-bucket index for *now*.

    Cascade signals use a 10-second bucket; all other signals use 30 seconds.
    The bucket index is floor(epoch_seconds / bucket_width).
    """
    width = _CASCADE_BUCKET_S if strategy_tag == "cascade" else _STANDARD_BUCKET_S
    return int(now) // width


def _make_hash(symbol: str, direction: str, strategy_tag: str, bucket: int) -> str:
    """
    Build a short, stable hex hash from the four dedup dimensions.

    SHA-256 is overkill for a cache key but guarantees collision-free
    composition across arbitrary string values without manual escaping.
    We take only the first 16 hex chars (64-bit) — more than sufficient for
    a hot-path in-memory set.
    """
    raw = f"{symbol}|{direction}|{strategy_tag}|{bucket}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class SignalDeduplicator:
    """
    Asyncio-safe, hash-based signal deduplication store.

    Internal state is a plain dict: ``{hash_key: expiry_epoch_float}``.
    Expired entries are pruned lazily on every :meth:`is_duplicate` call so
    memory stays bounded without background tasks.

    Thread-safety note:
        CPython's GIL makes dict reads/writes effectively atomic at the
        instruction level.  Because this module targets asyncio (single OS
        thread), no additional synchronisation is needed.
    """

    def __init__(self) -> None:
        # hash_key → epoch-seconds at which this entry expires
        self._store: Dict[str, float] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def is_duplicate(
        self,
        symbol: str,
        direction: str,
        strategy_tag: str,
        regime: str,
    ) -> bool:
        """
        Return True if an identical signal already executed within the
        active dedup window, False otherwise.

        The *regime* parameter is included in the hash so that the same
        directional signal fired under a different macro regime is treated
        as a distinct signal (regime shifts legitimately change the trade
        thesis).

        On any unexpected error this returns False — a false negative is
        always safer than blocking a real trade.
        """
        try:
            now = time.monotonic()
            self._expire(now)
            key = self._build_key(symbol, direction, strategy_tag, regime)
            if key in self._store:
                remaining = self._store[key] - now
                log.debug(
                    "signal_duplicate_rejected",
                    symbol=symbol,
                    direction=direction,
                    strategy_tag=strategy_tag,
                    regime=regime,
                    key=key,
                    ttl_remaining_s=round(remaining, 2),
                )
                return True
            return False
        except Exception as exc:  # noqa: BLE001
            log.error(
                "signal_dedup_error_passthrough",
                exc=repr(exc),
                symbol=symbol,
                direction=direction,
                strategy_tag=strategy_tag,
            )
            return False  # fail-open: never block on internal error

    def record(
        self,
        symbol: str,
        direction: str,
        strategy_tag: str,
        regime: str,
    ) -> str:
        """
        Record that a signal has been executed.  Returns the hash key that
        was stored so callers can log it for correlation.

        Call *after* the order is placed (or at least decided) to avoid
        recording a duplicate entry for a signal that ultimately failed.
        """
        key = self._build_key(symbol, direction, strategy_tag, regime)
        ttl = _CASCADE_BUCKET_S if strategy_tag == "cascade" else _STANDARD_BUCKET_S
        expiry = time.monotonic() + ttl
        self._store[key] = expiry
        log.debug(
            "signal_recorded",
            symbol=symbol,
            direction=direction,
            strategy_tag=strategy_tag,
            regime=regime,
            key=key,
            ttl_s=ttl,
        )
        return key

    def size(self) -> int:
        """Return the number of currently active (non-expired) entries."""
        now = time.monotonic()
        self._expire(now)
        return len(self._store)

    def clear(self) -> None:
        """Flush all entries (useful in tests and emergency resets)."""
        self._store.clear()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _build_key(
        self,
        symbol: str,
        direction: str,
        strategy_tag: str,
        regime: str,
    ) -> str:
        """
        Build the dedup hash key.

        We incorporate *regime* into the hash (not into the time bucket) so
        that signals fired under different regimes are always independent.
        The time bucket is based on wall-clock time (time.time()) for
        deterministic bucketing across restarts; monotonic time is used only
        for TTL comparisons.
        """
        bucket = _bucket(strategy_tag, time.time())
        # Embed regime in the raw string before hashing
        raw = f"{symbol}|{direction}|{strategy_tag}|{regime}|{bucket}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _expire(self, now: float) -> None:
        """
        Remove all entries whose TTL has elapsed.

        Called lazily from is_duplicate to keep memory bounded.  Using a
        list comprehension to collect expired keys first avoids mutating the
        dict while iterating it.
        """
        expired = [k for k, exp in self._store.items() if exp <= now]
        for k in expired:
            del self._store[k]


# ── Module-level singleton ────────────────────────────────────────────────────
# Import and use this directly; do not instantiate a second copy.
signal_deduplicator = SignalDeduplicator()
