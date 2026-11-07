"""Deterministic ULIDs for venue intentIds + semantic journal IDs.

Venue requires intentId to be a valid ULID. Deterministic seeding from
(account, asset, ts_ms) makes resubmission after 5xx/timeout idempotent —
the venue dedupes on identical intentId.
"""

from __future__ import annotations

import hashlib
import os
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def _ulid(ts_ms: int, rand80: int) -> str:
    return _encode(ts_ms & ((1 << 48) - 1), 10) + _encode(rand80 & ((1 << 80) - 1), 16)


def new_ulid(ts_ms: int | None = None) -> str:
    ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
    return _ulid(ts, int.from_bytes(os.urandom(10), "big"))


def intent_ulid(account: str, tag: str, ts_ms: int) -> str:
    digest = hashlib.sha256(f"prop|{account}|{tag}|{ts_ms}".encode()).digest()
    return _ulid(ts_ms, int.from_bytes(digest[:10], "big"))


def semantic_id(account: str, asset: str, ts_ms: int) -> str:
    return f"prop-{account}-{asset}-{ts_ms}"
