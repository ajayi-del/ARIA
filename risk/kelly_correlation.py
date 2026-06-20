"""
risk/kelly_correlation.py — Correlation-Adjusted Kelly Framework

Leak 8 fix: When trading correlated assets, the naive Kelly fraction overestimates
optimal size because it ignores covariance. This module adjusts f_i downward using:

  f_i_adjusted = f_i * max(0.20, 1 - ρ_ij * Σ_j f_j)

Where:
  f_i     = base Kelly fraction for new trade i (size_usd / balance)
  ρ_ij    = empirical correlation between asset i and each open position j
  Σ_j f_j = sum of Kelly fractions for all other open positions

Guard: correlation is only applied when >= 20 closed trades exist for the pair.
Otherwise falls back to asset-class heuristic correlations.
"""

from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional

import structlog

log = structlog.get_logger(__name__)

# Heuristic correlations when empirical data is insufficient
_ASSET_CLASS_RHO: dict = {
    ("equity", "equity"): 0.85,
    ("equity", "equity_index"): 0.90,
    ("equity_index", "equity_index"): 0.95,
    ("crypto", "crypto"): 0.70,
    ("crypto", "equity"): 0.40,
    ("crypto", "equity_index"): 0.45,
    ("commodity", "commodity"): 0.60,
    ("commodity", "equity"): 0.30,
    ("commodity", "crypto"): 0.25,
}

_MIN_TRADES_FOR_EMPIRICAL = 20


def _asset_class(symbol: str) -> str:
    """Infer asset class from symbol suffix."""
    if symbol.endswith("-USD"):
        _base = symbol.replace("-USD", "")
        _equities = {
            "NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "TSLA",
            "TSM", "ORCL",
        }
        _indices = {"SPCX", "USTECH100"}
        if _base in _equities:
            return "equity"
        if _base in _indices:
            return "equity_index"
        _commodities = {"CL", "COPPER", "XAUT"}
        if _base in _commodities:
            return "commodity"
        return "crypto"
    return "crypto"


def _heuristic_rho(sym1: str, sym2: str) -> float:
    """Fallback correlation when empirical data is insufficient."""
    c1 = _asset_class(sym1)
    c2 = _asset_class(sym2)
    if c1 == c2:
        return _ASSET_CLASS_RHO.get((c1, c2), 0.50)
    # Cross-class: look up both orderings
    return _ASSET_CLASS_RHO.get((c1, c2), _ASSET_CLASS_RHO.get((c2, c1), 0.30))


class KellyCorrelationAdjuster:
    """
    Stateful adjuster that reads the trade journal and builds a pairwise
    correlation matrix. Correlations are computed from same-direction trade
    outcomes (win/loss concordance), not price returns — faster convergence
    for small sample sizes.
    """

    def __init__(self, journal_path: Optional[str] = None) -> None:
        self._journal_path = journal_path
        self._matrix: Dict[str, Dict[str, float]] = {}
        self._counts: Dict[str, Dict[str, int]] = {}
        self._last_load = 0.0

    def _load_journal(self) -> List[dict]:
        if not self._journal_path or not os.path.exists(self._journal_path):
            return []
        try:
            with open(self._journal_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "trades" in data:
                return data["trades"]
        except (json.JSONDecodeError, OSError):
            pass
        return []

    def _build_matrix(self, trades: List[dict]) -> None:
        """Build concordance-based correlation matrix from closed trades."""
        # Group trades by symbol
        by_sym: Dict[str, List[dict]] = {}
        for t in trades:
            sym = t.get("symbol") or t.get("asset", "")
            if not sym:
                continue
            by_sym.setdefault(sym, []).append(t)

        symbols = list(by_sym.keys())
        self._matrix = {s: {} for s in symbols}
        self._counts = {s: {} for s in symbols}

        for i, s1 in enumerate(symbols):
            for s2 in symbols[i + 1:]:
                _pair = self._pair_correlation(by_sym[s1], by_sym[s2])
                self._matrix[s1][s2] = _pair["rho"]
                self._matrix[s2][s1] = _pair["rho"]
                self._counts[s1][s2] = _pair["count"]
                self._counts[s2][s1] = _pair["count"]

    @staticmethod
    def _pair_correlation(trades1: List[dict], trades2: List[dict]) -> dict:
        """Compute concordance correlation: how often both win or both lose."""
        # Build date-indexed outcome lists
        def _outcome(t: dict) -> int:
            pnl = float(t.get("pnl", t.get("realized_pnl", 0)))
            return 1 if pnl > 0 else -1

        def _ts(t: dict) -> str:
            return str(t.get("date", t.get("opened_at", "")))[:10]

        m1 = {_ts(t): _outcome(t) for t in trades1}
        m2 = {_ts(t): _outcome(t) for t in trades2}
        common_dates = set(m1.keys()) & set(m2.keys())

        if len(common_dates) < _MIN_TRADES_FOR_EMPIRICAL:
            return {"rho": 0.0, "count": len(common_dates)}

        concordant = sum(1 for d in common_dates if m1[d] == m2[d])
        discordant = len(common_dates) - concordant
        # Simple concordance ratio mapped to [-1, 1] then clamped to [0, 1]
        rho = max(0.0, (concordant - discordant) / len(common_dates))
        return {"rho": rho, "count": len(common_dates)}

    def refresh(self) -> None:
        """Reload journal and rebuild matrix. Call periodically (e.g. every 5 min)."""
        trades = self._load_journal()
        if trades:
            self._build_matrix(trades)
            self._last_load = time.time()

    def get_correlation(self, sym1: str, sym2: str) -> float:
        """Return empirical correlation if sufficient samples, else heuristic."""
        if sym1 == sym2:
            return 1.0
        _emp = self._matrix.get(sym1, {}).get(sym2)
        _cnt = self._counts.get(sym1, {}).get(sym2, 0)
        if _emp is not None and _cnt >= _MIN_TRADES_FOR_EMPIRICAL:
            return _emp
        return _heuristic_rho(sym1, sym2)

    def adjust_size(
        self,
        symbol: str,
        base_size_usd: float,
        balance: float,
        open_positions: List[dict],
    ) -> float:
        """
        Apply correlation-adjusted Kelly sizing.

        Parameters
        ----------
        symbol : str
            New trade symbol.
        base_size_usd : float
            Pre-adjustment trade size in USD.
        balance : float
            Total account balance.
        open_positions : list[dict]
            Each dict must have: symbol, size, entry_price (or notional_usd).

        Returns
        -------
        float
            Adjusted size in USD (never below 20% of base).
        """
        if balance <= 0 or base_size_usd <= 0 or not open_positions:
            return base_size_usd

        _f_i = base_size_usd / balance
        _f_sum = 0.0

        for pos in open_positions:
            _pos_sym = pos.get("symbol", "")
            if _pos_sym == symbol:
                continue
            _notional = pos.get("notional_usd")
            if _notional is None:
                _notional = float(pos.get("size", 0)) * float(pos.get("entry_price", 0))
            if _notional > 0:
                _rho = self.get_correlation(symbol, _pos_sym)
                _f_sum += (_notional / balance) * _rho

        _adj = max(0.20, 1.0 - _f_sum)
        _new_size = base_size_usd * _adj

        log.debug("kelly_correlation_adjusted",
                  symbol=symbol,
                  base_size=round(base_size_usd, 2),
                  adjusted=round(_new_size, 2),
                  factor=round(_adj, 3),
                  f_sum=round(_f_sum, 4))

        return _new_size


# Singleton accessor for main.py
_kelly_adjuster: Optional[KellyCorrelationAdjuster] = None


def get_kelly_adjuster(journal_path: Optional[str] = None) -> KellyCorrelationAdjuster:
    global _kelly_adjuster
    if _kelly_adjuster is None:
        _kelly_adjuster = KellyCorrelationAdjuster(journal_path)
    return _kelly_adjuster


if __name__ == "__main__":
    # Simple sanity check
    adj = KellyCorrelationAdjuster()
    _open = [
        {"symbol": "AAPL-USD", "size": 1.0, "entry_price": 200.0},
        {"symbol": "MSFT-USD", "size": 0.5, "entry_price": 400.0},
    ]
    _new = adj.adjust_size("NVDA-USD", 1000.0, 10_000.0, _open)
    print(f"Base $1000 -> Adjusted ${_new:.2f} (equity correlation guard)")
