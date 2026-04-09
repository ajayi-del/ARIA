"""
Nonce Manager for EIP-712 signed requests to SoDEX
Manages atomic nonce generation per API key
"""

import time
import threading
from typing import Optional


class NonceManager:
    """
    Manages EIP-712 nonces per API key.
    SoDEX tracks 100 highest nonces per address.
    Nonces must be unique, increasing, and within
    (T - 2 days, T + 1 day) window.
    """
    
    def __init__(self, api_key: str):
        self._api_key = api_key
        self._counter: int = 0
        self._last_nonce: int = 0
        self._lock = threading.Lock()
        
    def next_nonce(self) -> int:
        """
        Returns current Unix timestamp in ms.
        If last nonce == current ms, increments by 1.
        Thread-safe using threading.Lock().
        Never returns the same nonce twice.
        """
        with self._lock:
            # Current timestamp in milliseconds
            ts = int(time.time() * 1000)
            
            # Ensure uniqueness and monotonic increase
            if ts <= self._last_nonce:
                ts = self._last_nonce + 1
                
            self._last_nonce = ts
            return ts
    
    def reset(self) -> None:
        """
        Resets counter. Use only in tests.
        """
        with self._lock:
            self._counter = 0
            self._last_nonce = 0
