"""S4 — ETH 4h Cascade Fade, long on the OI flush (Governor 2026-09-15).

Zero-I/O brain. The register's highest signal-to-noise structural call.
SHADOW-ONLY FROM BIRTH: the module constant SHADOW_ONLY=True is the birth
state; live wiring requires an explicit Governor override (expected event
count is single digits per quarter — see stamp 2).

THESIS: a pre-cascade score >= 4 (negative funding streak + OI expanding +
whale L/S > 1.8 = trapped-long equilibrium) ARMS the setup; the 1h cascade
print (OI <= -3% AND close <= -2% on the same bar) is the TRIGGER; the
trade fades the flush LONG once the knife stops falling. Direction comes
from the print, not the score — the score only arms, it never directs.

ENTRY (ALL FOUR legs):
  1. armed: pre-cascade score >= 4 within the last ARM_WINDOW_H (6h),
     injected as (score, score_ts_ms, now_ms). None score/ts = dark,
     fail-closed. Inclusive: age exactly 6h still passes.
  2. trigger: a 1h bar with OI pct change <= OI_DROP_PCT (-3.0) AND close
     pct change <= PRICE_DROP_PCT (-2.0), inclusive boundaries — injected
     bar-level values (oi_chg_1h, close_chg_1h). None = dark.
  3. stabilization (knife check): after the trigger bar, price has NOT made
     a new low BEYOND KNIFE_BUFFER_PCT (0.4) below the trigger bar's low,
     measured over the last STABILIZATION_MIN (5) minutes of 1m lows
     (injected). Mirrors ARIA's whale_absorption knife-check doctrine.
     "Beyond" is strict: a low exactly at the buffer floor still passes.
     None/empty 1m coverage = dark, fail-closed.
  4. funding not freshly worse: latest funding print > FUNDING_FLOOR
     (-0.0005 = -5bp). A deepening-negative print means the carry stress
     is accelerating, not resolving. Injected Optional, None = dark.

EXITS: stop = trigger-bar low minus STOP_BUFFER_PCT (1.0%) — below the
flush low the fade thesis is wrong. TP1 = entry + 0.5 * (trigger_bar_open
- trigger_bar_low): a projected half-retrace of the flush LEG — the target
scale comes from the cascade leg itself, not a fixed pct (retraces, not
percentages). TP2 = the trigger bar's open (full retrace of the flush
bar). Ordering stop < tp1 < tp2 holds for a long entered in the lower
half of the flush bar (the fade geometry); a chase-entry above the flush
midpoint is outside this module's thesis. TIME_STOP_H = 48 (median trough
duration 18-48h per the source analysis).

KILL LEGS (exit early): a second cascade bar prints while in the trade
(the equilibrium is resolving AGAINST the fade); funding prints <=
FUNDING_FLOOR; OI 24h change < KILL_OI_24H_PCT (-6.0) = broad
deleveraging, not a flush. OI injected Optional — None = no opinion
(kill legs fail OPEN: only entry legs fail closed).

POLEMARCH STAMPS (filed with the spec, bind its interpretation):
  1. Parameters are PRIORS. tools/cascade_census.py (building in parallel)
     will deliver event-counted drawdown distributions (avg/median/p75)
     that should recalibrate the TP/stop geometry — the bracket constants
     here couple to that census and MUST be revisited when it lands.
  2. The 90d sample at score >= 4 is 5-25 events — the Wilson CI is too
     wide to trade live on. SHADOW_ONLY is not caution theater; it is the
     statistics. Graduation is an evidence problem, not a courage problem.
  3. The score's legs are CORRELATED (whale positioning drives OI) —
     effective independent confirmations < 4. Treat the arm as ~3
     confirmations, mirroring the S1 stamp.
  4. Cross-venue: the score planes are Bybit, execution is SoDEX — a
     trigger near the boundary is regime-ambiguous across the planes.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

# Knob defaults (config overrides ride main.py's splice; these pin the spec)
ARM_WINDOW_H = 6
OI_DROP_PCT = -3.0
PRICE_DROP_PCT = -2.0
KNIFE_BUFFER_PCT = 0.4
STABILIZATION_MIN = 5
FUNDING_FLOOR = -0.0005
STOP_BUFFER_PCT = 1.0
TIME_STOP_H = 48
KILL_OI_24H_PCT = -6.0

# Birth state. Live wiring requires an explicit Governor override.
SHADOW_ONLY = True

_MIN_SCORE = 4


def cascade_bar(oi_chg_1h: Optional[float],
                close_chg_1h: Optional[float]) -> bool:
    """True iff the 1h bar is a cascade print: OI <= -3% AND close <= -2%,
    inclusive boundaries. None input = not a cascade (fail-closed for the
    trigger, which is an entry leg)."""
    if oi_chg_1h is None or close_chg_1h is None:
        return False
    return oi_chg_1h <= OI_DROP_PCT and close_chg_1h <= PRICE_DROP_PCT


def knife_check(trigger_low: float, recent_1m_lows: Optional[Sequence[float]],
                buffer_pct: float = KNIFE_BUFFER_PCT) -> Optional[bool]:
    """True = knife stopped (no 1m low BEYOND buffer_pct below the trigger
    bar's low); False = still falling. None = insufficient coverage
    (None/empty lows, or a degenerate trigger_low) — fail-closed upstream.

    "Beyond" is strict: a low exactly at the floor passes.
    """
    if not recent_1m_lows or trigger_low is None or trigger_low <= 0:
        return None
    floor = trigger_low * (1.0 - buffer_pct / 100.0)
    return all(low >= floor for low in recent_1m_lows)


def _bar_field(trigger_bar, key: str) -> Optional[float]:
    """trigger_bar is injected as a Mapping {"open":..., "low":...}; a
    (open, low) 2-tuple is also accepted. Missing/degenerate = None."""
    if trigger_bar is None:
        return None
    if isinstance(trigger_bar, Mapping):
        return trigger_bar.get(key)
    if isinstance(trigger_bar, (tuple, list)) and len(trigger_bar) >= 2:
        return trigger_bar[0] if key == "open" else trigger_bar[1]
    return None


def evaluate_entry(armed_score: Optional[float], armed_ts_ms: Optional[int],
                   now_ms: int, oi_chg_1h: Optional[float],
                   close_chg_1h: Optional[float], trigger_bar,
                   recent_1m_lows: Optional[Sequence[float]],
                   funding_latest: Optional[float],
                   arm_window_h: float = ARM_WINDOW_H) -> dict:
    """All-four-legs verdict. Every leg reports pass/fail/dark so the
    shadow record can segment near-misses from structural absences.
    ok = all four legs pass."""
    legs = {}

    # 1. armed: score >= 4 and stamped within the window (inclusive).
    if armed_score is None or armed_ts_ms is None:
        legs["armed"] = "dark"
    else:
        age_ms = now_ms - armed_ts_ms
        if armed_score >= _MIN_SCORE and 0 <= age_ms <= arm_window_h * 3600 * 1000:
            legs["armed"] = "pass"
        else:
            legs["armed"] = "fail"

    # 2. trigger: the 1h cascade print.
    if oi_chg_1h is None or close_chg_1h is None:
        legs["trigger"] = "dark"
    else:
        legs["trigger"] = ("pass" if cascade_bar(oi_chg_1h, close_chg_1h)
                           else "fail")

    # 3. stabilization: knife check over the post-trigger 1m lows.
    trigger_low = _bar_field(trigger_bar, "low")
    kc = knife_check(trigger_low, recent_1m_lows)
    legs["stabilization"] = ("dark" if kc is None
                             else ("pass" if kc else "fail"))

    # 4. funding not freshly worse (strictly above the floor).
    if funding_latest is None:
        legs["funding"] = "dark"
    else:
        legs["funding"] = ("pass" if funding_latest > FUNDING_FLOOR
                           else "fail")

    ok = all(v == "pass" for v in legs.values())
    return {"ok": ok, "legs": legs, "armed_score": armed_score,
            "oi_chg_1h": oi_chg_1h, "close_chg_1h": close_chg_1h,
            "trigger_low": trigger_low, "funding_latest": funding_latest,
            "shadow_only": SHADOW_ONLY}


def evaluate_kill(second_cascade: bool, funding_latest: Optional[float],
                  oi_chg_24h: Optional[float],
                  kill_oi_chg: float = KILL_OI_24H_PCT) -> tuple:
    """(kill, reason). reason="" when no kill. Kill legs fail OPEN —
    None funding/OI is no opinion, never a kill."""
    if second_cascade:
        return True, "second_cascade"
    if funding_latest is not None and funding_latest <= FUNDING_FLOOR:
        return True, "funding_floor_breach"
    if oi_chg_24h is not None and oi_chg_24h < kill_oi_chg:
        return True, "oi_24h_deleveraging"
    return False, ""


def bracket(entry: float, trigger_open: float, trigger_low: float,
            stop_buffer_pct: float = STOP_BUFFER_PCT) -> tuple:
    """(stop, tp1, tp2) for a LONG fade. Stop sits stop_buffer_pct below
    the trigger bar's low; TP1 is the half-retrace of the flush leg off
    the entry; TP2 is the full retrace (the trigger bar's open)."""
    stop = trigger_low * (1.0 - stop_buffer_pct / 100.0)
    tp1 = entry + 0.5 * (trigger_open - trigger_low)
    return stop, tp1, trigger_open
