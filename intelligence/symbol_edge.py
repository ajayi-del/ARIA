"""
SymbolEdgeThrottler — cybernetic feedback loop for per-symbol edge.

Closes the gap between trade journal and live sizing:
  1. Reads closed trades per symbol from TradeJournal.
  2. Computes realized win rate, avg P&L, and hold-time correlation.
  3. Returns edge_mult (size throttle/boost) and hold_time_bias_ms.

Integration:
  - build_candidate():   candidate.size *= edge_mult
  - _time_stop_loop():   _loser_cutoff += hold_time_bias_ms

Safety:
  - Minimum 5 trades before any throttle activates (avoid noise on small samples).
  - Edge mult clamped [0.5, 1.5] so a single bad streak cannot kill sizing.
  - Hold-time bias clamped [-30 min, +30 min] relative to base cutoff.
"""

import math
import time
from typing import Dict, List, Optional
import structlog

logger = structlog.get_logger(__name__)

_MIN_TRADES = 5
_EDGE_FLOOR = 0.50
_EDGE_CEIL = 1.50
_HOLD_BIAS_FLOOR_MS = -30 * 60 * 1000
_HOLD_BIAS_CEIL_MS = 30 * 60 * 1000


class SymbolEdgeThrottler:
    """
    Per-symbol edge throttling + hold-time tuning.
    """

    def __init__(self, min_trades: int = _MIN_TRADES):
        self.min_trades = min_trades
        # Cache: symbol -> stats dict, invalidated on refresh
        self._cache: Dict[str, dict] = {}
        self._last_refresh_ms: int = 0
        self._cache_ttl_ms: int = 60_000  # refresh at most once per minute

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def get_symbol_edge(self, symbol: str, journal=None) -> dict:
        """
        Return edge dict for a symbol:
          edge_mult          float  — size multiplier (0.5–1.5)
          hold_time_bias_ms  int    — adjustment to loser cutoff (-30min to +30min)
          win_rate           float  — realized win rate (0–1)
          avg_pnl            float  — average net P&L per trade
          hold_corr          float  — correlation hold_time_ms vs pnl (-1 to 1)
          reason             str    — human-readable rationale

        Pass a journal object (with .get_closed()) or a list of closed entries.
        Results cached for 60s to avoid repeated computation.
        """
        _now_ms = int(time.time() * 1000)
        if _now_ms - self._last_refresh_ms < self._cache_ttl_ms:
            _cached = self._cache.get(symbol)
            if _cached is not None:
                return _cached

        _closed = []
        if journal is not None:
            _closed = journal.get_closed() if hasattr(journal, "get_closed") else list(journal)

        _entries = [e for e in _closed if e.get("symbol") == symbol]
        _n = len(_entries)
        if _n < self.min_trades:
            return {
                "edge_mult": 1.0,
                "hold_time_bias_ms": 0,
                "win_rate": 0.0,
                "avg_pnl": 0.0,
                "hold_corr": 0.0,
                "reason": f"sample_size_{_n}_below_min_{self.min_trades}",
            }

        _wins = [e for e in _entries if e.get("pnl_net_usd", e.get("pnl_usd", 0)) > 0]
        _win_rate = len(_wins) / _n
        _avg_pnl = sum(
            e.get("pnl_net_usd", e.get("pnl_usd", 0)) for e in _entries
        ) / _n

        # Edge multiplier: punish losers, reward winners
        _edge_mult = self._compute_edge_mult(_win_rate, _avg_pnl)

        # Hold-time correlation: do longer holds help or hurt?
        _hold_corr = self._hold_time_correlation(_entries)
        _bias_ms = self._hold_bias_from_correlation(_hold_corr)

        _reason = (
            f"wr={_win_rate:.0%} avg_pnl=${_avg_pnl:+.2f} "
            f"hold_corr={_hold_corr:+.2f} mult={_edge_mult:.2f}"
        )

        _result = {
            "edge_mult": round(_edge_mult, 2),
            "hold_time_bias_ms": int(_bias_ms),
            "win_rate": round(_win_rate, 3),
            "avg_pnl": round(_avg_pnl, 2),
            "hold_corr": round(_hold_corr, 3),
            "reason": _reason,
        }
        self._cache[symbol] = _result
        self._last_refresh_ms = _now_ms
        return _result

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _compute_edge_mult(self, win_rate: float, avg_pnl: float) -> float:
        """
        Map realized win rate + avg P&L to a size multiplier.

        Rationale:
          - Negative avg P&L with any win rate → throttle (system is bleeding on
            this symbol, either via bad RR or too many losses).
          - Win rate < 25% → strong throttle 0.5× regardless of avg P&L.
          - Win rate 25–40% with negative avg → moderate throttle 0.75×.
          - Win rate > 45% with positive avg → boost 1.25×.
          - Win rate > 55% with positive avg → strong boost 1.5×.
        """
        if win_rate < 0.25:
            return 0.50
        if avg_pnl < 0:
            if win_rate < 0.40:
                return 0.75
            return 0.90  # slight throttle even if WR decent but avg negative
        # avg_pnl >= 0
        if win_rate >= 0.55:
            return 1.50
        if win_rate >= 0.45:
            return 1.25
        return 1.0

    def _hold_time_correlation(self, entries: List[dict]) -> float:
        """
        Pearson correlation between hold_time_ms and net P&L.
        Positive = longer holds are more profitable (extend cutoff).
        Negative = longer holds are more lossy (tighten cutoff).
        """
        _pairs = []
        for e in entries:
            _ht = e.get("hold_time_ms")
            _pnl = e.get("pnl_net_usd", e.get("pnl_usd", None))
            if _ht is not None and _pnl is not None:
                _pairs.append((_ht, _pnl))
        if len(_pairs) < self.min_trades:
            return 0.0

        _xs = [p[0] for p in _pairs]
        _ys = [p[1] for p in _pairs]
        _n = len(_pairs)
        _mean_x = sum(_xs) / _n
        _mean_y = sum(_ys) / _n

        _num = sum((x - _mean_x) * (y - _mean_y) for x, y in _pairs)
        _den_x = sum((x - _mean_x) ** 2 for x in _xs)
        _den_y = sum((y - _mean_y) ** 2 for y in _ys)
        _den = math.sqrt(_den_x * _den_y)
        if _den == 0:
            return 0.0
        return _num / _den

    def _hold_bias_from_correlation(self, corr: float) -> int:
        """
        Convert correlation to a millisecond bias on the loser cutoff.

        Thresholds:
          corr > +0.20  → extend 30 min (longer holds are profitable)
          corr < -0.20  → reduce 30 min (cut losers faster)
          otherwise      → no bias
        """
        if corr > 0.20:
            return _HOLD_BIAS_CEIL_MS
        if corr < -0.20:
            return _HOLD_BIAS_FLOOR_MS
        return 0
