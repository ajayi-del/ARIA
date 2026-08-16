"""intelligence/graduation.py — subsystem graduation registry (2026-08-16).

Shadow evidence → TTL'd privilege keys. A shadow subsystem that accumulates
enough live-forward evidence ENTERS the graduated state — a state the system
holds, not a deploy. Graduation is ADVISORY: consumers surface it (heartbeat,
alerts, router heartbeat flag); what a graduation unlocks stays an explicit
operator decision. Fail-closed: default is always shadow.

TTL doctrine (same pattern as rally graduation, applied to subsystems instead
of symbols): the privilege key is re-earned every evaluation while criteria
hold and lapses automatically when evidence decays. No permanent grants.

Win rate is shrinkage-adjusted toward 0.5 with k=20 (same prior as the
Skeptic) so 8/10 lucky streaks don't graduate anyone.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)

_K = 20.0          # shrinkage prior strength
_KEY_PREFIX = "grad_"


class GraduationRegistry:
    def __init__(self, param_store, *, min_samples: int = 30,
                 min_span_days: float = 7.0, min_shrunk_wr: float = 0.5,
                 ttl_s: int = 72 * 3600) -> None:
        self._ps = param_store
        self._min_samples = int(min_samples)
        self._min_span_s = float(min_span_days) * 86400.0
        self._min_wr = float(min_shrunk_wr)
        self._ttl_s = int(ttl_s)
        self._announced: set = set()   # subsystems with a live graduation

    @staticmethod
    def shrunk_wr(wins: float, n: int) -> float:
        return (wins + _K * 0.5) / (n + _K) if n > 0 else 0.0

    def is_graduated(self, subsystem: str) -> bool:
        if self._ps is None:
            return False
        return self._ps.get_ai_param(_KEY_PREFIX + subsystem, None) is not None

    def evaluate(self, subsystem: str,
                 outcomes: List[Tuple[float, bool]],
                 now: Optional[float] = None) -> Dict[str, Any]:
        """outcomes: [(ts, won)]. Sets/refreshes the TTL key while criteria
        hold; lets it lapse when they don't. Returns the evidence state."""
        now = now if now is not None else time.time()
        n = len(outcomes)
        wins = sum(1 for _, w in outcomes if w)
        span = (max(ts for ts, _ in outcomes) - min(ts for ts, _ in outcomes)
                ) if n >= 2 else 0.0
        wr = self.shrunk_wr(wins, n)
        meets = (n >= self._min_samples
                 and span >= self._min_span_s
                 and wr > self._min_wr)
        state = {"subsystem": subsystem, "n": n, "wins": wins,
                 "span_days": round(span / 86400.0, 2),
                 "shrunk_wr": round(wr, 3), "graduated": False}

        if self._ps is None:
            return state

        key = _KEY_PREFIX + subsystem
        if meets:
            self._ps.set_ai_param(key, {
                "n": n, "wins": wins, "shrunk_wr": round(wr, 4),
                "earned_at": now}, ttl_seconds=self._ttl_s)
            state["graduated"] = True
            if subsystem not in self._announced:
                self._announced.add(subsystem)
                logger.info("subsystem_graduated", **state)
        else:
            if subsystem in self._announced and not self.is_graduated(subsystem):
                self._announced.discard(subsystem)
                logger.info("subsystem_lapsed", **state)
        return state
