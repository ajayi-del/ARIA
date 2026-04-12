"""
Session Timing Multiplier — ARIA v1.7

Adjusts the coherence THRESHOLD (not the score) based on session quality.
Higher quality session → lower threshold needed → more trades fire.
Lower quality session → higher threshold → fewer trades.
"""
from datetime import datetime, timezone
import structlog

log = structlog.get_logger(__name__)


class SessionTimingMultiplier:
    """
    Session quality table (UTC hours, weekday only):
      NY+London overlap  13-17h → 1.20
      London open        08-10h → 1.10
      NY open            13-16h → 1.10 (absorbed by overlap above)
      NY close           17-20h → 1.00
      Pre-NY             10-13h → 1.00
      NY late            20-24h → 0.95
      Late night          0- 2h → 0.85
      Asian session       2- 8h → 0.80
      Weekend (any hour)        → 0.75
    """

    SESSION_TABLE = [
        # (start_hour, end_hour, mult, name)  — first match wins
        (13, 17, 1.20, "NY_London_overlap"),
        (8,  10, 1.10, "London_open"),
        (17, 20, 1.00, "NY_close"),
        (10, 13, 1.00, "pre_NY"),
        (20, 24, 0.95, "NY_late"),
        (0,   2, 0.85, "late_night"),
        (2,   8, 0.80, "Asian_session"),
    ]
    WEEKEND_MULT = 0.75

    def get_multiplier(self) -> tuple:
        """Returns (multiplier: float, session_name: str)."""
        now = datetime.now(timezone.utc)
        if now.weekday() >= 5:  # Saturday=5, Sunday=6
            return self.WEEKEND_MULT, "weekend"
        h = now.hour
        for start, end, mult, name in self.SESSION_TABLE:
            if start <= h < end:
                return mult, name
        return 0.90, "off_hours"

    def adjusted_threshold(self, base_threshold: float, session_mult: float) -> float:
        """
        Effective coherence threshold = base / session_mult.
        High-quality session (1.2×) → lower bar → more trades.
        Low-quality session  (0.8×) → higher bar → fewer trades.
        """
        return base_threshold / max(session_mult, 0.1)
