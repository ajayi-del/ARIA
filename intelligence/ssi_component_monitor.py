"""
intelligence/ssi_component_monitor.py — MAG7 Component Divergence Monitor.

Computes per-component spread z-scores:
  spread(t) = return(component, t) - return(MAG7_index, t)
  z_score   = (spread - spread_mean) / spread_std  over rolling 20 periods

Designed for the SOVEREIGN personality:
  - Runs in background every 15 minutes (cold path)
  - Stores rolling price history in-memory
  - Emits component divergence dict: {symbol: z_score}
  - Thread-safe via simple assignment (CPython GIL sufficient)

The monitor does NOT call external APIs directly. It receives price updates
via update_price() called from the existing candle loop.
"""

from __future__ import annotations

import collections
import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# MAG7 components traded on SoDEX as equity perps
# Weights sum to 1.0 (approximate market-cap weights as of 2025-04)
MAG7_COMPONENTS: Dict[str, float] = {
    "NVDA-USD":  0.25,
    "MSFT-USD":  0.18,
    "AAPL-USD":  0.15,
    "AMZN-USD":  0.14,
    "GOOGL-USD": 0.12,
    "META-USD":  0.10,
    "TSLA-USD":  0.06,
}

_ROLLING_N = 20          # rolling window for z-score computation
_MIN_HISTORY = 5         # minimum history required before z-score is valid
_DIVERGENCE_THRESHOLD = 1.5  # |z_score| >= 1.5 → COMPONENT_DIVERGENCE event


@dataclass
class ComponentState:
    """Per-component rolling price and spread history."""
    symbol:        str
    weight:        float   # fraction of MAG7 index (0.0–1.0)
    prices:        collections.deque = field(default_factory=lambda: collections.deque(maxlen=_ROLLING_N + 1))
    spreads:       collections.deque = field(default_factory=lambda: collections.deque(maxlen=_ROLLING_N))
    last_z_score:  float = 0.0
    last_updated:  float = 0.0  # Unix timestamp


@dataclass
class ComponentDivergence:
    """Snapshot of the strongest diverging component at a point in time."""
    symbol:         str
    z_score:        float          # negative = underperforming, positive = outperforming
    direction:      str            # "short" (momentum) or "long" (reversion)
    weight:         float          # fraction of MAG7 index
    spread_pct:     float          # component return - index return (this period)
    regime_signal:  str = "none"   # set by sovereign_signal.py
    timestamp_ms:   int = 0


class SSIComponentMonitor:
    """
    MAG7 component divergence tracker.

    Usage (background loop, every 15 min):
        monitor = SSIComponentMonitor()
        # On each candle close or price update:
        monitor.update_price("NVDA-USD", current_price, index_return_pct)
        monitor.update_price("AAPL-USD", ...)
        # Query:
        signals = monitor.get_all_z_scores()      # {symbol: z_score}
        divergences = monitor.get_divergences()   # list of ComponentDivergence
        best = monitor.get_best_divergence()      # highest |z_score| or None
    """

    def __init__(self) -> None:
        self._components: Dict[str, ComponentState] = {
            sym: ComponentState(symbol=sym, weight=w)
            for sym, w in MAG7_COMPONENTS.items()
        }
        # Rolling index return history (MAG7 index level)
        self._index_returns: collections.deque = collections.deque(maxlen=_ROLLING_N)
        self._last_index_price: float = 0.0

    def update_index_price(self, price: float) -> None:
        """Call when MAG7 index price is updated. Computes period return."""
        if self._last_index_price > 0:
            pct_return = (price - self._last_index_price) / self._last_index_price
            self._index_returns.append(pct_return)
        self._last_index_price = price

    def update_price(self, symbol: str, price: float, index_return_pct: Optional[float] = None) -> None:
        """
        Update component price and compute spread vs MAG7 index.

        Parameters
        ----------
        symbol          : e.g. "NVDA-USD"
        price           : current price
        index_return_pct: MAG7 index return for this period (if known).
                          If None, uses last stored index return.
        """
        comp = self._components.get(symbol)
        if comp is None:
            return

        comp.prices.append(price)
        comp.last_updated = time.time()

        if len(comp.prices) < 2:
            return

        # Component period return
        comp_return = (comp.prices[-1] - comp.prices[-2]) / comp.prices[-2]

        # Index return for this period
        idx_ret = index_return_pct
        if idx_ret is None:
            idx_ret = self._index_returns[-1] if self._index_returns else 0.0

        spread = comp_return - idx_ret
        comp.spreads.append(spread)
        comp.last_z_score = self._compute_z_score(comp.spreads)

    def _compute_z_score(self, spreads: collections.deque) -> float:
        """Rolling z-score of component spread. Returns 0.0 if insufficient data."""
        if len(spreads) < _MIN_HISTORY:
            return 0.0
        vals = list(spreads)
        n = len(vals)
        mean = sum(vals) / n
        variance = sum((v - mean) ** 2 for v in vals) / n
        std = math.sqrt(variance)
        if std < 1e-9:
            return 0.0
        current = vals[-1]
        return (current - mean) / std

    def get_all_z_scores(self) -> Dict[str, float]:
        """Return {symbol: z_score} for all MAG7 components."""
        return {sym: comp.last_z_score for sym, comp in self._components.items()}

    def get_divergences(self, threshold: float = _DIVERGENCE_THRESHOLD) -> List[ComponentDivergence]:
        """Return all components with |z_score| >= threshold, sorted by |z_score| desc."""
        result = []
        for sym, comp in self._components.items():
            z = comp.last_z_score
            if abs(z) >= threshold and len(comp.spreads) >= _MIN_HISTORY:
                spread_pct = comp.spreads[-1] if comp.spreads else 0.0
                result.append(ComponentDivergence(
                    symbol=sym,
                    z_score=z,
                    direction="long" if z < 0 else "short",   # default: mean reversion
                    weight=comp.weight,
                    spread_pct=spread_pct,
                    timestamp_ms=int(time.time() * 1000),
                ))
        result.sort(key=lambda d: abs(d.z_score), reverse=True)
        return result

    def get_best_divergence(self) -> Optional[ComponentDivergence]:
        """Return the component with the highest |z_score|, or None if below threshold."""
        divs = self.get_divergences()
        return divs[0] if divs else None

    def inject_z_scores(self, z_scores: Dict[str, float]) -> None:
        """
        Directly inject z-scores (for testing or pre-computed signal injection).
        Bypasses rolling history computation.
        """
        for sym, z in z_scores.items():
            comp = self._components.get(sym)
            if comp is not None:
                comp.last_z_score = z
                # Inject a minimal spread so get_divergences() count check passes
                for _ in range(_MIN_HISTORY):
                    if len(comp.spreads) < _MIN_HISTORY:
                        comp.spreads.append(0.0)

    def reset(self) -> None:
        """Reset all history (for testing)."""
        for comp in self._components.values():
            comp.prices.clear()
            comp.spreads.clear()
            comp.last_z_score = 0.0
        self._index_returns.clear()
        self._last_index_price = 0.0