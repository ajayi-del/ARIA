"""
risk_calendar/time_regime.py — Time Regime Overlay Engine for ARIA.

Outputs a deterministic, stateless overlay of risk / cooldown / confidence
multipliers based on:
  - Time-of-month  (month_start / mid_month / month_end)
  - Day-of-week    (Mon low-conviction → Tue/Wed trend efficiency → Fri risk-off)
  - Hour-of-day    (Sunday evening gap risk, Monday open liquidity hunt)
  - Macro event    (FOMC, CPI, NFP, PCE — caution / block zones)
  - Volatility     (Friday high-vol: trend continuation allowed)

Application contract (set by caller in main.py):
    final_size      = base_size * drawdown_mult * temporal_mult
                      * time_regime.risk_multiplier
    final_cooldown  = base_cooldown * time_regime.cooldown_multiplier
    final_confidence = signal_confidence * time_regime.confidence_multiplier

Constraints:
  - Never generates trades
  - Fully stateless and deterministic per call
  - Never raises — returns safe defaults on any internal error
"""

import calendar as _calendar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class TimeRegime:
    """Immutable overlay result for a single evaluation cycle."""
    phase:                str    # Human-readable phase label
    risk_multiplier:      float  # 0.0–1.15  — multiply into base_size
    cooldown_multiplier:  float  # 1.0–2.0   — multiply into base_cooldown
    confidence_multiplier: float # 0.80–1.10 — multiply into signal_confidence
    notes:                str    # Pipe-delimited human-readable explanation


# ── High-impact event types that trigger macro override ───────────────────────
_HIGH_IMPACT_EVENTS = frozenset({"FOMC", "CPI", "NFP", "PCE", "EARNINGS_MAG7"})


def evaluate(
    now_utc:             Optional[datetime] = None,
    event_type:          Optional[str]      = None,
    hours_to_event:      Optional[float]    = None,
    volatility_elevated: bool               = False,
) -> TimeRegime:
    """
    Evaluate the time regime for the current moment.

    Parameters
    ----------
    now_utc : datetime, optional
        Current UTC time. Defaults to ``datetime.now(timezone.utc)``.
    event_type : str, optional
        Nearest upcoming event type, e.g. ``"FOMC"``, ``"CPI"``.
    hours_to_event : float, optional
        Hours until next macro event.  Triggers pre-event caution zones.
    volatility_elevated : bool
        Friday session flag.  If True, high-vol trend continuation is
        allowed (risk_mult 1.05); otherwise default risk-off (0.95).

    Returns
    -------
    TimeRegime
        Always returns a valid instance — never raises.
    """
    try:
        return _evaluate(now_utc, event_type, hours_to_event, volatility_elevated)
    except Exception:
        # Fail-safe: return neutral defaults so sizing chain is unaffected
        return TimeRegime(
            phase="unknown",
            risk_multiplier=1.0,
            cooldown_multiplier=1.0,
            confidence_multiplier=1.0,
            notes="time_regime_eval_error — defaults applied",
        )


def _evaluate(
    now_utc:             Optional[datetime],
    event_type:          Optional[str],
    hours_to_event:      Optional[float],
    volatility_elevated: bool,
) -> TimeRegime:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    dow       = now_utc.weekday()   # 0=Mon … 6=Sun
    day       = now_utc.day
    hour_utc  = now_utc.hour
    month     = now_utc.month
    year      = now_utc.year
    days_in_m = _calendar.monthrange(year, month)[1]
    days_left = days_in_m - day     # 0 = last calendar day

    notes: list[str] = []

    # ── 1. Monthly cycle ─────────────────────────────────────────────────────
    if day <= 5:
        monthly_phase = "month_start"
        monthly_risk  = 1.15
        notes.append("month_start: trend continuation / bullish flow bias")
    elif days_left < 5:
        monthly_phase = "month_end"
        monthly_risk  = 0.85
        notes.append("month_end: liq + window dressing — reduced risk")
    else:
        monthly_phase = "mid_month"
        monthly_risk  = 0.90
        notes.append("mid_month: chop / mean-reversion — reduced risk")

    # ── 2. Weekly cycle ──────────────────────────────────────────────────────
    if dow == 0:         # Monday
        weekly_phase      = "monday"
        weekly_risk       = 1.00
        weekly_confidence = 0.90
        notes.append("monday: low conviction / higher reversals")
    elif dow in (1, 2):  # Tuesday–Wednesday
        weekly_phase      = "tue_wed"
        weekly_risk       = 1.00
        weekly_confidence = 1.10
        notes.append("tue_wed: highest trend efficiency window")
    elif dow == 3:       # Thursday
        weekly_phase      = "thursday"
        weekly_risk       = 1.00
        weekly_confidence = 1.00
        notes.append("thursday: positioning + vol build")
    elif dow == 4:       # Friday
        weekly_phase      = "friday"
        weekly_confidence = 1.00
        if volatility_elevated:
            weekly_risk   = 1.05
            notes.append("friday_hv: high-vol trend continuation allowed")
        else:
            weekly_risk   = 0.95
            notes.append("friday_rv: risk-off default (low vol)")
    elif dow == 5:       # Saturday
        weekly_phase      = "saturday"
        weekly_risk       = 0.90
        weekly_confidence = 0.90
        notes.append("saturday: reduced crypto liquidity")
    else:                # Sunday (dow == 6)
        weekly_phase      = "sunday"
        weekly_risk       = 0.90
        weekly_confidence = 0.90
        notes.append("sunday: gap risk + liquidity hunt window")

    # ── 3. Crypto-specific intra-day adjustments ─────────────────────────────
    crypto_risk = 1.0
    if dow == 6 and hour_utc >= 20:        # Sunday 20:00+ UTC — pre-Mon gap
        crypto_risk = 0.80
        notes.append("sunday_evening: gap risk before Mon open — cautious")
    elif dow == 0 and hour_utc < 8:        # Monday 00:00–08:00 UTC
        crypto_risk = 0.85
        notes.append("monday_open: liquidity hunt window — extra cautious")

    # ── 4. Compose base multipliers (conservative: take min for risk) ─────────
    base_risk       = min(monthly_risk, weekly_risk, crypto_risk)
    base_confidence = weekly_confidence
    base_cooldown   = 1.0
    phase           = f"{monthly_phase}_{weekly_phase}"

    # ── 5. Macro / event override (highest priority) ──────────────────────────
    if event_type in _HIGH_IMPACT_EVENTS and hours_to_event is not None:
        if hours_to_event <= 2.0:
            # Hard block zone — CalendarEngine already returns BLOCK at this range;
            # echo here as a sizing-layer redundancy so risk_mult = 0.0 even if
            # the calendar gate somehow passes.
            base_risk       = 0.0
            base_cooldown   = 2.0
            base_confidence = 0.0
            phase           = f"event_block_{event_type}"
            notes           = [f"BLOCK: {event_type} in {hours_to_event:.1f}h — no new entries"]

        elif hours_to_event <= 4.0:
            base_risk       = min(base_risk, 0.70)
            base_cooldown   = max(base_cooldown, 1.5)
            base_confidence = min(base_confidence, 0.85)
            phase           = f"pre_event_{event_type}"
            notes.append(f"pre_event: {event_type} in {hours_to_event:.1f}h — high caution")

        elif hours_to_event <= 12.0:
            base_risk       = min(base_risk, 0.85)
            base_cooldown   = max(base_cooldown, 1.25)
            base_confidence = min(base_confidence, 0.90)
            phase           = f"event_caution_{event_type}"
            notes.append(f"event_caution: {event_type} in {hours_to_event:.1f}h")

    return TimeRegime(
        phase                = phase,
        risk_multiplier      = round(base_risk, 4),
        cooldown_multiplier  = round(base_cooldown, 4),
        confidence_multiplier= round(base_confidence, 4),
        notes                = " | ".join(notes),
    )
