"""
Session-aware trading configuration for ARIA.

Markets behave differently by session. Asian session has thin order books,
wider spreads, and higher noise-to-signal ratio. Applying US-session thresholds
overnight is the single largest source of avoidable losses.

Session boundaries (UTC, non-overlapping, full 24h coverage):
  00:00–06:59  →  asian
  07:00–12:59  →  london
  13:00–15:59  →  overlap   (London + US — highest volume)
  16:00–21:59  →  us
  22:00–23:59  →  asian      (transitional / pre-Asian gap)

Integration (in main.py, before on_signal_ready):
    from core.session_config import session_manager
    _sess = session_manager.get_current_session()
    if candidate.symbol in session_manager.get_excluded_symbols():
        skip
    if not session_manager.is_strategy_allowed(_personality_name):
        skip
    if coherence < session_manager.get_coherence_minimum():
        skip
    candidate.size *= session_manager.get_size_multiplier()
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
        coherence_minimum=7.0,
        size_multiplier=0.60,
        max_positions=1,
        time_stop_minutes=45,
        excluded_symbols=["BASED-USD", "TRUMP-USD", "1000PEPE-USD"],
        allowed_strategies=["APEX", "AFTERMATH"],
    ),
    "london": _SessionConfig(
        name="london",
        coherence_minimum=5.0,
        size_multiplier=0.85,
        max_positions=2,
        time_stop_minutes=30,
        excluded_symbols=[],
        allowed_strategies=[],
    ),
    "overlap": _SessionConfig(
        name="overlap",
        coherence_minimum=4.5,
        size_multiplier=1.10,
        max_positions=3,
        time_stop_minutes=30,
        excluded_symbols=[],
        allowed_strategies=[],
    ),
    "us": _SessionConfig(
        name="us",
        coherence_minimum=4.714,
        size_multiplier=1.0,
        max_positions=3,
        time_stop_minutes=30,
        excluded_symbols=[],
        allowed_strategies=[],
    ),
}


class SessionManager:
    """
    Query session parameters for the current UTC time.

    All methods are stateless reads — safe to call at any frequency.
    """

    def get_current_session(self) -> str:
        """
        Returns the active session name based on current UTC hour.
        Overlap (13-16 UTC) takes priority over both london and us.
        """
        hour = datetime.now(timezone.utc).hour
        if 13 <= hour < 16:   # 13:00–15:59 UTC — London/US overlap
            return "overlap"
        if 16 <= hour < 22:   # 16:00–21:59 UTC
            return "us"
        if 7 <= hour < 13:    # 07:00–12:59 UTC
            return "london"
        return "asian"         # 22:00–23:59 and 00:00–06:59 UTC

    def get_coherence_minimum(self) -> float:
        """Minimum coherence score required to open a trade this session."""
        return _SESSIONS[self.get_current_session()].coherence_minimum

    def get_size_multiplier(self) -> float:
        """
        Position size multiplier for this session.
        Applied after all other multipliers (coherence, drawdown, freshness).
        """
        return _SESSIONS[self.get_current_session()].size_multiplier

    def get_max_positions(self) -> int:
        """Maximum simultaneous open positions allowed this session."""
        return _SESSIONS[self.get_current_session()].max_positions

    def get_excluded_symbols(self) -> List[str]:
        """Symbols that must not be traded during this session."""
        return list(_SESSIONS[self.get_current_session()].excluded_symbols)

    def get_time_stop_minutes(self) -> int:
        """Time-stop duration for this session in minutes."""
        return _SESSIONS[self.get_current_session()].time_stop_minutes

    def get_allowed_strategies(self) -> list:
        """Returns list of permitted strategies, or empty list (= all allowed)."""
        return list(_SESSIONS[self.get_current_session()].allowed_strategies)

    def is_strategy_allowed(self, strategy: str) -> bool:
        """
        Returns True if the strategy/personality is permitted this session.
        An empty allowed_strategies list means all strategies are permitted.
        """
        allowed = _SESSIONS[self.get_current_session()].allowed_strategies
        return not allowed or strategy.upper() in allowed

    def log_session(self) -> None:
        """Emit a structured log line with the full current session config."""
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


# Module-level singleton — import and use everywhere
session_manager = SessionManager()
