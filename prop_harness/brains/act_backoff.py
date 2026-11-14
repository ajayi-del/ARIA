"""Act-path reliability — 429 backoff / coalesce / latch (giveback P0).

Conquest already decides floors (trail / sharpen / basket / bank). When
Propr returns 429, unconditional retry every reconcile tick amplifies
into a storm and the decided floor never settles → giveback.

Pure state machine. Per-key latch + process-local **account** clock
(Design A): one place-429 cools every later admit in this process.
Kill OFF → always allow; note_* are no-ops.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

ACCOUNT_KEY = "__account__"


def is_rate_limited(exc: BaseException) -> bool:
    """True for HTTP 429 / too_many_requests shape."""
    status = getattr(exc, "status_code", None)
    if status == 429:
        return True
    msg = str(exc).lower()
    return "429" in msg or "too_many_requests" in msg or "rate_limited" in msg


@dataclass
class ActBackoff:
    enabled: bool = True
    initial_s: float = 30.0
    max_s: float = 300.0
    latch_after: int = 3
    # key → unix ts when next attempt is allowed
    _next_ok: dict[str, float] = field(default_factory=dict)
    # consecutive 429 count per key
    _fails: dict[str, int] = field(default_factory=dict)
    # latched keys (after latch_after consecutive 429s)
    _latched: set[str] = field(default_factory=set)
    # keys that already emitted act_backoff_deferred in the current window
    _deferred_done: set[str] = field(default_factory=set)
    # process-local account clock (shared across semantic_ids)
    _account_until: float = 0.0
    _account_fails: int = 0

    def blocked(self, key: str, now: float | None = None) -> bool:
        if not self.enabled:
            return False
        t = time.time() if now is None else float(now)
        if t < float(self._account_until or 0.0):
            return True
        if not key:
            return False
        return t < float(self._next_ok.get(key, 0.0) or 0.0)

    def remaining_s(self, key: str, now: float | None = None) -> float:
        if not self.enabled:
            return 0.0
        t = time.time() if now is None else float(now)
        rem_acct = max(0.0, float(self._account_until or 0.0) - t)
        rem_key = 0.0
        if key:
            rem_key = max(0.0, float(self._next_ok.get(key, 0.0) or 0.0) - t)
        return max(rem_acct, rem_key)

    def allow(self, key: str, now: float | None = None) -> bool:
        return not self.blocked(key, now=now)

    def _backoff_s(self, fails: int) -> float:
        # fails is 1-indexed count after this note
        exp = max(0, int(fails) - 1)
        return min(float(self.max_s), float(self.initial_s) * (2.0 ** exp))

    def note_rate_limit(self, key: str, now: float | None = None) -> dict[str, Any]:
        """Record a 429. Arms per-key latch + account clock. Journal payload."""
        if not self.enabled:
            return {}
        t = time.time() if now is None else float(now)
        # Account clock — cools every later admit in this process.
        self._account_fails = min(int(self._account_fails or 0) + 1, 16)
        acct_wait = self._backoff_s(self._account_fails)
        self._account_until = max(float(self._account_until or 0.0),
                                  t + acct_wait)
        self._deferred_done.discard(ACCOUNT_KEY)

        fails = 0
        wait = acct_wait
        latched = False
        if key:
            fails = int(self._fails.get(key, 0) or 0) + 1
            self._fails[key] = fails
            wait = self._backoff_s(fails)
            self._next_ok[key] = t + wait
            latched = fails >= int(self.latch_after or 3)
            if latched:
                self._latched.add(key)
                self._next_ok[key] = t + float(self.max_s)
                wait = float(self.max_s)
            self._deferred_done.discard(key)

        return {
            "type": "act_backoff_latched" if latched else "act_backoff_armed",
            "backoff_key": key or ACCOUNT_KEY,
            "fails": fails,
            "account_fails": int(self._account_fails),
            "wait_s": round(max(wait, acct_wait), 3),
            "latched": latched,
        }

    def note_success(self, key: str) -> None:
        """Clear per-key latch. account_fails reset; account_until runs out."""
        if key:
            self._fails.pop(key, None)
            self._next_ok.pop(key, None)
            self._latched.discard(key)
            self._deferred_done.discard(key)
        self._account_fails = 0
        # account_until left alone (Design A) — process cools out the window

    def should_journal_deferred(self, key: str, now: float | None = None) -> bool:
        """Throttle deferred spam: at most one deferred event per arm window."""
        if not self.enabled or not self.blocked(key, now=now):
            return False
        # Prefer key throttle; fall back to account throttle when key empty
        # or only the account clock is binding.
        t = time.time() if now is None else float(now)
        key_blocking = bool(
            key and t < float(self._next_ok.get(key, 0.0) or 0.0))
        throttle_key = key if key_blocking else ACCOUNT_KEY
        if throttle_key in self._deferred_done:
            return False
        self._deferred_done.add(throttle_key)
        return True

    def deferred_payload(self, key: str, now: float | None = None) -> dict[str, Any]:
        return {
            "type": "act_backoff_deferred",
            "backoff_key": key or ACCOUNT_KEY,
            "remaining_s": round(self.remaining_s(key, now=now), 3),
            "fails": int(self._fails.get(key, 0) or 0) if key else 0,
            "account_fails": int(self._account_fails or 0),
            "latched": bool(key and key in self._latched),
        }


def resize_key(semantic_id: str) -> str:
    return f"resize:{semantic_id}" if semantic_id else ""


def basket_key(semantic_id: str | None = None) -> str:
    """Per-leg basket close key; None → book-level cooloff."""
    if semantic_id:
        return f"basket:{semantic_id}"
    return "basket:__book__"
