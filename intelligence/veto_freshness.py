"""Veto freshness — regime-keyed veto invalidation + TTL/decay (2026-09-17).

Motivation (today's incident): a "quiet_market_pause" veto blocked with-trend
UNI longs 08:34-08:40Z and 14:58-15:14Z on STALE evidence ("quiet tape, 22%
recent WR" from an old trailing window) while UNI was in a short squeeze
(HV 119.6%, OI +3.4% 24h, funding 83% above 30d avg, +17.49% move). The
evidence described a regime that no longer existed at veto time.

This module makes veto evidence expire:
  1. Hard TTL — evidence older than its shelf life weighs zero.
  2. Regime keying — evidence collected under regime R is void the moment the
     live regime is no longer R (regime shift invalidates immediately).
  3. Half-life decay — inside the TTL, weight decays as 0.5 ** (age / half_ttl)
     so a veto at half its TTL counts half.

THRESHOLDS ARE UNSOURCED STARTING HYPOTHESES. Every constant below (TTLs,
half-life ratio, fire threshold, classifier cutoffs) is a module constant
deliberately exposed for tuning; none are derived from measured gate
economics yet. The shadow LOO reporter (LooAttributionReporter) exists to
produce exactly that evidence before any threshold is trusted.

Stdlib only. All I/O is fail-silent: a broken shadow log or a garbage metric
must never crash or change the trade path.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from intelligence import kill_switch

# ── Kill-switch helpers ───────────────────────────────────────────────────────

def freshness_enabled() -> bool:
    """True when the freshness gate may influence live decisions (kill switch
    ``veto_freshness``; default OFF — shadow-first)."""
    try:
        return kill_switch.enabled("veto_freshness")
    except Exception:
        return False


def loo_enabled() -> bool:
    """True when the leave-one-out shadow reporter may write (kill switch
    ``veto_loo_report``; default ON — measure-only)."""
    try:
        return kill_switch.enabled("veto_loo_report")
    except Exception:
        return False


# ── TTL table (UNSOURCED starting hypotheses) ─────────────────────────────────
# quiet_market_pause: quiet tape flips fast once flow returns -> short shelf.
# low_winrate_regime: trailing WR windows move slowly -> longer shelf.
# coherence_decay: coherence is a fast signal -> 1h.
VETO_TTL_SECONDS: Dict[str, float] = {
    "quiet_market_pause": 4.0 * 3600.0,
    "low_winrate_regime": 8.0 * 3600.0,
    "coherence_decay": 3600.0,
}
DEFAULT_TTL_SECONDS: float = 4.0 * 3600.0

# A veto "fires" (counts as blocking) when its effective weight meets this.
# UNSOURCED starting hypothesis.
VETO_FIRE_THRESHOLD: float = 0.40

# Half-life: at age == hard_ttl/2 the weight has halved.
_HALF_LIFE_FRACTION: float = 0.5


# ── Regime classifier thresholds (UNSOURCED starting hypotheses) ──────────────
BREAKOUT_HV_PCT: float = 100.0          # annualized HV % at/above -> BREAKOUT
BREAKOUT_OI_DELTA_24H_PCT: float = 2.0  # OI 24h delta % at/above -> BREAKOUT
BREAKOUT_FUNDING_RATIO: float = 1.5     # funding vs 30d avg at/above -> BREAKOUT
QUIET_VOLUME_RATIO: float = 0.6         # volume ratio below this (with low HV) -> QUIET
QUIET_HV_PCT: float = 40.0              # HV below this (with low volume) -> QUIET

_REGIME_BREAKOUT = "BREAKOUT"
_REGIME_QUIET = "QUIET"
_REGIME_NORMAL = "NORMAL"
_REGIME_UNKNOWN = "UNKNOWN"


def classify_regime(metrics: dict) -> str:
    """Map live metrics to a regime label. Pure; never raises.

    Keys (all optional): ``hv_annualized_pct``, ``oi_delta_24h_pct``,
    ``funding_vs_avg_ratio``, ``volume_ratio``.

    Precedence: BREAKOUT > QUIET > NORMAL. A missing key cannot fire the leg
    it feeds (absent evidence is not evidence). If ALL keys are missing or
    non-numeric the regime is "UNKNOWN" — UNKNOWN never matches a collection
    regime, so unknown-regime vetoes carry zero weight (fail-closed).
    """
    try:
        if not isinstance(metrics, dict):
            return _REGIME_UNKNOWN

        def _num(key: str) -> Optional[float]:
            v = metrics.get(key)
            try:
                f = float(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
            if f != f:  # NaN
                return None
            return f

        hv = _num("hv_annualized_pct")
        oi = _num("oi_delta_24h_pct")
        fr = _num("funding_vs_avg_ratio")
        vr = _num("volume_ratio")

        if hv is None and oi is None and fr is None and vr is None:
            return _REGIME_UNKNOWN

        if (hv is not None and hv >= BREAKOUT_HV_PCT) or \
           (oi is not None and oi >= BREAKOUT_OI_DELTA_24H_PCT) or \
           (fr is not None and fr >= BREAKOUT_FUNDING_RATIO):
            return _REGIME_BREAKOUT

        if vr is not None and hv is not None and \
           vr < QUIET_VOLUME_RATIO and hv < QUIET_HV_PCT:
            return _REGIME_QUIET

        return _REGIME_NORMAL
    except Exception:
        return _REGIME_UNKNOWN


# ── VetoRecord ────────────────────────────────────────────────────────────────

@dataclass
class VetoRecord:
    """One piece of veto evidence with a collection timestamp and regime stamp.

    ``hard_ttl_s`` is the shelf life in seconds (UNSOURCED per-type values in
    VETO_TTL_SECONDS). Effective weight:
      * age > hard_ttl_s                       -> 0.0 (expired)
      * current_regime != collected_regime     -> 0.0 (regime shift voids it)
      * else weight * 0.5 ** (age / (hard_ttl_s * _HALF_LIFE_FRACTION))
    """
    veto_type: str
    weight: float
    collected_at: float
    collected_regime: str
    hard_ttl_s: float = DEFAULT_TTL_SECONDS

    def effective_weight(self, now: float, current_regime: str) -> float:
        """Decayed weight at ``now`` under ``current_regime``. Fail-silent:
        any garbage input returns 0.0 (the evidence cannot vouch)."""
        try:
            age = float(now) - float(self.collected_at)
            if age < 0.0 or age > float(self.hard_ttl_s):
                return 0.0
            if str(current_regime) != str(self.collected_regime):
                return 0.0
            half_life = float(self.hard_ttl_s) * _HALF_LIFE_FRACTION
            if half_life <= 0.0:
                return 0.0
            return float(self.weight) * 0.5 ** (age / half_life)
        except Exception:
            return 0.0


# ── VetoRegistry ─────────────────────────────────────────────────────────────

class VetoRegistry:
    """In-memory store of the latest VetoRecord per veto type.

    Single asyncio loop, no locks by design (main.py is single-loop).
    ``now`` is injectable everywhere for deterministic tests.
    """

    def __init__(self, ttl_table: Optional[Dict[str, float]] = None) -> None:
        self._ttl = dict(ttl_table) if ttl_table else dict(VETO_TTL_SECONDS)
        self._records: Dict[str, VetoRecord] = {}

    def record(
        self,
        veto_type: str,
        weight: float,
        regime: str,
        now: Optional[float] = None,
    ) -> None:
        """Stamp a veto observation. Latest write wins. Fail-silent."""
        try:
            ts = time.time() if now is None else float(now)
            self._records[str(veto_type)] = VetoRecord(
                veto_type=str(veto_type),
                weight=float(weight),
                collected_at=ts,
                collected_regime=str(regime),
                hard_ttl_s=float(self._ttl.get(str(veto_type), DEFAULT_TTL_SECONDS)),
            )
        except Exception:
            pass

    def effective_weight(
        self,
        veto_type: str,
        now: float,
        current_regime: str,
    ) -> float:
        """Effective weight of the latest record for ``veto_type`` (0.0 when
        absent, expired, or regime-shifted). Fail-silent."""
        try:
            rec = self._records.get(str(veto_type))
            if rec is None:
                return 0.0
            return rec.effective_weight(now, current_regime)
        except Exception:
            return 0.0

    def fires(self, veto_type: str, now: float, current_regime: str) -> bool:
        """True when the veto's effective weight meets VETO_FIRE_THRESHOLD."""
        try:
            return self.effective_weight(veto_type, now, current_regime) >= VETO_FIRE_THRESHOLD
        except Exception:
            return False

    def prune(self, now: Optional[float] = None) -> int:
        """Drop records past their hard TTL. Returns count dropped. Fail-silent."""
        try:
            ts = time.time() if now is None else float(now)
            dead = [k for k, r in self._records.items()
                    if ts - r.collected_at > r.hard_ttl_s]
            for k in dead:
                del self._records[k]
            return len(dead)
        except Exception:
            return 0

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self._records)


# ── Leave-one-out attribution reporter (shadow, measure-only) ────────────────

class LooAttributionReporter:
    """Cheap leave-one-out attribution for blocked candidates.

    Every block is appended as one JSONL line carrying per-veto responsibility
    shares: a veto that fired ALONE is the unique blocker (share 1.0); when N
    vetoes fired together each gets 1/N — a documented cheap Shapley
    approximation (uniform credit over the firing coalition, ignoring order
    effects; good enough to rank veto classes by marginal cost).

    Every method is fail-silent: the shadow log must never touch the trade
    path. Writes are gated on the ``veto_loo_report`` kill switch.
    """

    DEFAULT_PATH = Path(__file__).resolve().parent.parent / "logs" / "veto_loo_shadow.jsonl"

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else self.DEFAULT_PATH

    @staticmethod
    def responsibility_shares(fired_vetoes: List[str]) -> Dict[str, float]:
        """share = 1.0 for a unique blocker, else 1/len(fired). Fail-silent."""
        try:
            fired = [str(v) for v in (fired_vetoes or [])]
            if not fired:
                return {}
            share = 1.0 / len(fired)
            return {v: share for v in fired}
        except Exception:
            return {}

    def record_block(
        self,
        candidate_id: str,
        fired_vetoes: List[str],
        ts: float,
        symbol: str,
        direction: str,
        intended_notional: float,
    ) -> None:
        """Append one JSONL row. No-op when the kill switch is off. Never raises."""
        try:
            if not loo_enabled():
                return
            row = {
                "ts": float(ts),
                "candidate_id": str(candidate_id),
                "symbol": str(symbol),
                "direction": str(direction),
                "intended_notional": float(intended_notional),
                "fired_vetoes": [str(v) for v in (fired_vetoes or [])],
                "shares": self.responsibility_shares(list(fired_vetoes or [])),
                "attribution": "loo_uniform_coalition_v1",
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
        except Exception:
            pass
