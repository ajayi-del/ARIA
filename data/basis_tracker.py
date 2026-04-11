"""
Layer 0: Basis Tracker
======================
Continuously measures  basis = sodex_mark - bybit_last_price  per symbol.

When basis widens beyond its rolling σ-band, it signals venue dislocation.
Directional trading is suspended until basis normalises.

Logic:
  sodex_mark   = mark_price_stores[symbol].mark_price   (SoDEX native)
  bybit_last   = candle_buffers[symbol]["1m"].latest(1)[-1].close

  basis_pct = (sodex_mark - bybit_last) / bybit_last

  is_stressed when:
    |basis_pct| > MAX_BASIS_PCT  (hard cap: 0.50%)
    OR
    |basis_pct - rolling_mean| > STRESS_SIGMA * rolling_std  (3σ statistical)
"""

import math
import structlog
from collections import deque
from typing import Dict, Deque

logger = structlog.get_logger(__name__)


class BasisTracker:
    """
    Measures SoDEX – Bybit price basis.  Thread-safe for asyncio single-threaded use.
    """

    STRESS_SIGMA:   float = 2.5     # σ multiplier for statistical stress flag
    WINDOW:         int   = 60      # rolling window (candle ticks)
    MAX_BASIS_PCT:  float = 0.005   # 0.50% hard cap — anything beyond is dislocation
    MIN_HISTORY:    int   = 10      # minimum samples before statistical check fires

    def __init__(self, mark_price_stores: dict, candle_buffers: dict):
        self._mark_stores  = mark_price_stores
        self._candle_bufs  = candle_buffers
        self._history:  Dict[str, Deque[float]] = {}
        self._latest:   Dict[str, float]        = {}

    # ─────────────────────────────────────────────────────────────────────
    def update(self, symbol: str) -> float:
        """
        Recompute basis for *symbol*.
        Returns basis_pct (signed).  Also refreshes internal rolling history.
        """
        sodex_store = self._mark_stores.get(symbol)
        if not sodex_store:
            return 0.0

        sodex_price = getattr(sodex_store, "mark_price", 0.0)
        if not sodex_price or sodex_price <= 0:
            return 0.0

        buf = self._candle_bufs.get(symbol, {}).get("1m")
        if not buf or buf.count() < 1:
            return 0.0

        try:
            bybit_last = buf.latest(1)[-1].close
        except Exception:
            return 0.0

        if not bybit_last or bybit_last <= 0:
            return 0.0

        basis_pct = (sodex_price - bybit_last) / bybit_last

        if symbol not in self._history:
            self._history[symbol] = deque(maxlen=self.WINDOW)
        self._history[symbol].append(basis_pct)
        self._latest[symbol] = basis_pct

        return basis_pct

    # ─────────────────────────────────────────────────────────────────────
    def is_stressed(self, symbol: str) -> bool:
        """
        Returns True when basis is abnormally wide → suspend directional trading.
        Two checks:
          1. Hard cap: |basis| > MAX_BASIS_PCT
          2. Statistical: |basis - μ| > STRESS_SIGMA × σ
        """
        basis = self._latest.get(symbol, 0.0)

        # 1. Hard cap
        if abs(basis) > self.MAX_BASIS_PCT:
            logger.warning("basis_hard_cap_hit", symbol=symbol,
                           basis_pct=f"{basis:.4%}", cap=f"{self.MAX_BASIS_PCT:.4%}")
            return True

        # 2. Statistical check
        history = self._history.get(symbol, deque())
        if len(history) < self.MIN_HISTORY:
            return False  # not enough data for stat check

        n    = len(history)
        mean = sum(history) / n
        var  = sum((x - mean) ** 2 for x in history) / n
        std  = math.sqrt(var)

        if std < 1e-6:   # essentially flat basis → never stressed
            return False

        z = abs(basis - mean) / std
        if z > self.STRESS_SIGMA:
            logger.warning("basis_statistical_stress", symbol=symbol,
                           z_score=f"{z:.2f}", basis_pct=f"{basis:.4%}")
            return True

        return False

    # ─────────────────────────────────────────────────────────────────────
    def get_basis(self, symbol: str) -> float:
        """Latest basis_pct for *symbol* (or 0.0 if unseen)."""
        return self._latest.get(symbol, 0.0)

    def get_all(self) -> Dict[str, float]:
        """Refresh and return basis_pct for every tracked symbol."""
        return {sym: self.update(sym) for sym in self._mark_stores}
