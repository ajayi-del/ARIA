"""
intelligence/skeptic.py — Mode 3: THE SKEPTIC (Taleb)

"What survives empirically, and what is narrative?"

The static gates know rules; the Skeptic knows BASE RATES. It queries the
Historian's scored counterfactuals — every gate refusal and Dreamer
candidate, shadow-scored at 1h/4h/24h with market_energy + day_type
attached — and answers: "setups that looked like THIS one, in THIS
weather, won how often?"

The answer replaces Nietzsche's global hist_wr with a context-matched
base rate, shrinkage-blended (k=20) toward the realized-WR prior:

    blended = (wins + k * prior) / (n + k)

Bootstrap-safe: n=0 → pure prior; n≈20 → half weight; n≥60 → the data
speaks for itself. Phase B wiring: main.py feeds the blend into
compute_conviction(historical_wr=...) and nietzsche_engine.compute(
win_rate=...) — the Skeptic advises, the engines still decide.

Match dimensions (applied only when BOTH sides carry the field):
  coherence ±0.5   — signal-quality axis
  regime    equal  — 13-regime classifier state at refusal time
  energy    ±10    — the Watcher's market-energy weather scalar
  category  equal  — asset category (relative_strength.ASSET_CATEGORIES)

"Win" = the journal's own verdict: won_24h (24h PnL > 0 AND the
hypothetical stop was never hit). Memory = the journal's 35d scored cap.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)

_SHRINK_K = 20.0
_COHERENCE_BAND = 0.5
_ENERGY_BAND = 10.0
_MEMO_TTL_S = 60.0


def _category_of(symbol: str) -> str:
    try:
        from intelligence.relative_strength import ASSET_CATEGORIES
        return ASSET_CATEGORIES.get(symbol, "")
    except Exception:
        return ""


class Skeptic:
    """Empirical base-rate layer over the shadow journal's scored records."""

    def __init__(self, journal, k: float = _SHRINK_K) -> None:
        self._journal = journal
        self._k = float(k)
        self._memo: Dict[tuple, Tuple[float, float, int]] = {}

    def _matches(self, rec: Dict, coherence: Optional[float], regime: str,
                 market_energy: Optional[float], category: str) -> bool:
        if coherence is not None:
            rc = rec.get("coherence")
            if isinstance(rc, (int, float)) and rc > 0:
                if abs(float(rc) - coherence) > _COHERENCE_BAND:
                    return False
        if regime:
            rr = str(rec.get("regime", "") or "")
            if rr and rr != regime:
                return False
        if market_energy is not None:
            re_ = rec.get("market_energy")
            if isinstance(re_, (int, float)):
                if abs(float(re_) - market_energy) > _ENERGY_BAND:
                    return False
        if category:
            if _category_of(str(rec.get("symbol", ""))) != category:
                return False
        return True

    def base_rate(self, *, coherence: Optional[float] = None,
                  regime: str = "", market_energy: Optional[float] = None,
                  symbol: str = "", prior_wr: float = 0.5,
                  now: Optional[float] = None) -> Tuple[float, int]:
        """Context-matched win rate. Returns (blended_wr, n_matched)."""
        now = now if now is not None else time.time()
        category = _category_of(symbol)
        key = (round(coherence * 2) / 2 if coherence is not None else None,
               regime,
               round(market_energy / 10) * 10 if market_energy is not None else None,
               category, round(float(prior_wr), 3))
        hit = self._memo.get(key)
        if hit and now - hit[0] < _MEMO_TTL_S:
            return hit[1], hit[2]

        n = 0
        wins = 0
        try:
            records = self._journal.scored_records() if self._journal else []
        except Exception:
            records = []
        for rec in records:
            if "won_24h" not in rec:
                continue
            if self._matches(rec, coherence, regime, market_energy, category):
                n += 1
                if rec.get("won_24h"):
                    wins += 1

        prior = min(1.0, max(0.0, float(prior_wr)))
        blended = (wins + self._k * prior) / (n + self._k)
        if len(self._memo) > 512:
            self._memo.clear()
        self._memo[key] = (now, blended, n)
        return blended, n
