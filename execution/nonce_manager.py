"""
Nonce Manager for EIP-712 signed requests to SoDEX
Manages atomic nonce generation per API key
"""

import time
from typing import Optional


class NonceManager:
    """
    Manages EIP-712 nonces per API key.
    SoDEX tracks 100 highest nonces per address.
    Nonces must be unique, increasing, and within
    (T - 2 days, T + 1 day) window.

    ARIA runs in a single asyncio event loop — no threads share this object.
    No lock needed; simple monotonic increment is sufficient and avoids the
    ~0.5µs threading.Lock overhead on every order placement.
    """

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._last_nonce: int = 0

    def next_nonce(self) -> int:
        """
        Returns a unique, strictly monotonically increasing nonce.
        Guaranteed never to return the same value twice within this process.
        """
        ts = int(time.time() * 1000)
        if ts <= self._last_nonce:
            ts = self._last_nonce + 1
        self._last_nonce = ts
        return ts

    def reset(self) -> None:
        """Resets state. Use only in tests."""
        self._last_nonce = 0
