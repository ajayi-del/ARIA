"""
Session-aware trading configuration for ARIA.

Session boundaries (UTC, non-overlapping, full 24h coverage):
  00:00–06:59  →  asian
  07:00–12:59  →  london
  13:00–15:59  →  overlap   (London + US — highest volume)
  16:00–21:59  →  us
  22:00–23:59  →  asian      (transitional / pre-Asian gap)
"""

from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import List
import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _SessionConfig:
    name: str
    coherence_minimum: float
    size_multiplier: float
    max_positions: int
    time_stop_minutes: int
    excluded_symbols: List[str]
    allowed_strategies: List[str]   # empty list = all strategies permitted


_SESSIONS: dict[str, _SessionConfig] = {
    "asian": _SessionConfig(
        name="asian",
        coherence_minimum=3.0,
        size_multiplier=0.85,
        max_positions=5,
        time_stop_minutes=45,
        excluded_symbols=[],        # no symbol exclusions — trade 24/7
        allowed_strategies=[],      # all strategies permitted
    ),
    "london": _SessionConfig(
        name="london",
        coherence_minimum=3.0,
        size_multiplier=0.90,
        max_positions=5,
        time_stop_minutes=30,
        excluded_symbols=[],
        allowed_strategies=[],
    ),
    "overlap": _SessionConfig(
        name="overlap",
        coherence_minimum=3.0,
        size_multiplier=1.10,
        max_positions=5,
        time_stop_minutes=30,
        excluded_symbols=[],
        allowed_strategies=[],
    ),
    "us": _SessionConfig(
        name="us",
        coherence_minimum=3.0,
        size_multiplier=1.0,
        max_positions=5,
        time_stop_minutes=30,
        excluded_symbols=[],
        allowed_strategies=[],
    ),
}


class SessionManager:
    """Query session parameters for the current UTC time."""

    def get_current_session(self) -> str:
        hour = datetime.now(timezone.utc).hour
        if 13 <= hour < 16:
            return "overlap"
        if 16 <= hour < 22:
            return "us"
        if 7 <= hour < 13:
            return "london"
        return "asian"

    def get_coherence_minimum(self) -> float:
        return _SESSIONS[self.get_current_session()].coherence_minimum

    def get_size_multiplier(self) -> float:
        return _SESSIONS[self.get_current_session()].size_multiplier

    def get_max_positions(self) -> int:
        return _SESSIONS[self.get_current_session()].max_positions

    def get_excluded_symbols(self) -> List[str]:
        return list(_SESSIONS[self.get_current_session()].excluded_symbols)

    def get_time_stop_minutes(self) -> int:
        return _SESSIONS[self.get_current_session()].time_stop_minutes

    def get_allowed_strategies(self) -> list:
        return list(_SESSIONS[self.get_current_session()].allowed_strategies)

    def is_strategy_allowed(self, strategy: str) -> bool:
        allowed = _SESSIONS[self.get_current_session()].allowed_strategies
        return not allowed or strategy.upper() in allowed

    def log_session(self) -> None:
        sess = self.get_current_session()
        cfg = _SESSIONS[sess]
        logger.info(
            "session_active",
            session=sess,
            coherence_min=cfg.coherence_minimum,
            size_mult=cfg.size_multiplier,
            max_positions=cfg.max_positions,
            time_stop_min=cfg.time_stop_minutes,
            excluded=cfg.excluded_symbols,
            allowed_strategies=cfg.allowed_strategies or "all",
        )


session_manager = SessionManager()
