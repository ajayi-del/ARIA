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
  direction equal  — long/short (2026-08-29 journal-corruption audit:
                     pooling opposed-tide shorts with aligned longs poisoned
                     the base rate both ways; SKEPTIC_DIRECTION_ENABLED=false
                     restores legacy pooling)

Recency decay (2026-08-29, same audit): a rally-week record should not
carry the same vote as yesterday's once the regime has turned. Each
matched record weighs 0.5 ** (age_days / halflife); the shrinkage blend
runs on the weighted counts and n returned is the rounded effective
sample size (so VETO_MIN_N binds on effective evidence, not raw count).
SKEPTIC_DECAY_HALFLIFE_DAYS (default 14.0; 0 = off, legacy bit-for-bit).

"Win" = the journal's own verdict: won_24h (24h PnL > 0 AND the
hypothetical stop was never hit). Memory = the journal's 35d scored cap.
"""

from __future__ import annotations

import os
import time
from typing import Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)

_SHRINK_K = 20.0
_COHERENCE_BAND = 0.5
_ENERGY_BAND = 10.0
_MEMO_TTL_S = 60.0

# Base-rate expectancy veto (2026-08-22, ZEC autopsy — operator directive).
# Chan (Algorithmic Trading ch.6) / Thorp (Kelly): a setup class with
# measured negative expectancy gets size ZERO, not size-small. ZEC 2026-08-21
# entered with hist_wr 0.187 known at conviction time — Nietzsche's basket
# cap shrank it to 25% and fired anyway. The veto fires only when the
# SHRUNK base rate is decisively below the candidate's breakeven WR with
# enough observations that the k=20 shrinkage no longer dominates.
VETO_MIN_N = 10        # below n the prior dominates — defer to size machinery
VETO_WR_MARGIN = 0.6   # blended must be < 60% of breakeven to veto


def base_rate_veto_enabled() -> bool:
    return os.environ.get("BASE_RATE_VETO_ENABLED", "true").strip().lower() != "false"


def skeptic_direction_enabled() -> bool:
    return os.environ.get("SKEPTIC_DIRECTION_ENABLED", "true").strip().lower() != "false"


def skeptic_decay_halflife_days() -> float:
    try:
        return max(0.0, float(os.environ.get("SKEPTIC_DECAY_HALFLIFE_DAYS", "14.0")))
    except (TypeError, ValueError):
        return 14.0


def base_rate_veto(blended_wr, n, rr_ratio=None,
                   min_n: int = VETO_MIN_N, margin: float = VETO_WR_MARGIN) -> bool:
    """True when the shrunk base rate is decisively below breakeven.

    blended_wr : Skeptic.base_rate output (already shrinkage-blended —
        a veto means the data overwhelmed the prior, not a small-n fluke).
    n          : matched observations behind the blend.
    rr_ratio   : candidate's own reward:risk; breakeven WR = 1/(1+rr).
        Unknown/invalid → 0.5 (1:1) — the conservative default for ARIA's
        small-account bracket geometry.
    """
    if int(n or 0) < min_n:
        return False
    rr = float(rr_ratio or 0.0)
    breakeven = 1.0 / (1.0 + rr) if rr > 0 else 0.5
    return float(blended_wr) < breakeven * margin


VETO_LATCH_CLEAR_MARGIN = 0.75  # latched veto clears only above 75% of breakeven


def base_rate_veto_latch_enabled() -> bool:
    return os.environ.get("BASE_RATE_VETO_LATCH_ENABLED", "true").strip().lower() != "false"


def base_rate_veto_latch_ttl_s() -> float:
    try:
        return max(0.0, float(os.environ.get("BASE_RATE_VETO_LATCH_S", "1800")))
    except (TypeError, ValueError):
        return 1800.0


def base_rate_veto_latched(blended_wr, n, rr_ratio=None, latched: bool = False,
                           min_n: int = VETO_MIN_N, margin: float = VETO_WR_MARGIN,
                           clear_margin: float = VETO_LATCH_CLEAR_MARGIN):
    """(veto, relatch) — knife-edge hysteresis over base_rate_veto.

    2026-09-03 (SPCX incident): vetoed at blended 0.159 twice (threshold
    0.160 = 0.6 × 0.267 breakeven), then n-jitter (208→204) moved the blend
    to 0.163 and the class EXECUTED 62s later — a measured 39%-below-
    breakeven setup, −$5.29. A boundary that flips on ±0.004 noise is the
    Q10 luck-dominated quadrant: the gate re-tests jitter, not information.
    Once vetoed, STAY vetoed until the blend clears breakeven × clear_margin
    (a genuine recovery), then the latch releases. n < min_n never latches."""
    if base_rate_veto(blended_wr, n, rr_ratio, min_n=min_n, margin=margin):
        return True, True
    if latched and int(n or 0) >= min_n:
        rr = float(rr_ratio or 0.0)
        breakeven = 1.0 / (1.0 + rr) if rr > 0 else 0.5
        if float(blended_wr) < breakeven * clear_margin:
            return True, True
    return False, False


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
                 market_energy: Optional[float], category: str,
                 direction: str = "") -> bool:
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
        if direction:
            rd = str(rec.get("direction", "") or "").lower()
            if rd and rd != direction.lower():
                return False
        return True

    def base_rate(self, *, coherence: Optional[float] = None,
                  regime: str = "", market_energy: Optional[float] = None,
                  symbol: str = "", prior_wr: float = 0.5,
                  direction: str = "",
                  now: Optional[float] = None) -> Tuple[float, int]:
        """Context-matched win rate. Returns (blended_wr, n_matched).

        With decay on, n_matched is the rounded EFFECTIVE sample size
        (sum of recency weights) — VETO_MIN_N binds on effective evidence.
        """
        now = now if now is not None else time.time()
        category = _category_of(symbol)
        _dir = direction.lower() if (direction and skeptic_direction_enabled()) else ""
        _halflife_d = skeptic_decay_halflife_days()
        _halflife_s = _halflife_d * 86400.0 if _halflife_d > 0 else 0.0
        key = (round(coherence * 2) / 2 if coherence is not None else None,
               regime,
               round(market_energy / 10) * 10 if market_energy is not None else None,
               category, round(float(prior_wr), 3), _dir,
               round(_halflife_d, 2))
        hit = self._memo.get(key)
        if hit and now - hit[0] < _MEMO_TTL_S:
            return hit[1], hit[2]

        n = 0
        wins = 0
        n_w = 0.0
        wins_w = 0.0
        try:
            records = self._journal.scored_records() if self._journal else []
        except Exception:
            records = []
        for rec in records:
            if "won_24h" not in rec:
                continue
            if self._matches(rec, coherence, regime, market_energy, category, _dir):
                if _halflife_s > 0:
                    try:
                        age_s = max(0.0, now - float(rec.get("ts") or now))
                    except (TypeError, ValueError):
                        age_s = 0.0
                    w = 0.5 ** (age_s / _halflife_s)
                    n_w += w
                    if rec.get("won_24h"):
                        wins_w += w
                else:
                    n += 1
                    if rec.get("won_24h"):
                        wins += 1

        prior = min(1.0, max(0.0, float(prior_wr)))
        if _halflife_s > 0:
            blended = (wins_w + self._k * prior) / (n_w + self._k)
            n_out = int(round(n_w))
        else:
            blended = (wins + self._k * prior) / (n + self._k)
            n_out = n
        if len(self._memo) > 512:
            self._memo.clear()
        self._memo[key] = (now, blended, n_out)
        return blended, n_out
