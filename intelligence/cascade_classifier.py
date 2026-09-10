"""C7 cascade continuation/exhaustion classifier (CEO s27 queue #4).

SHADOW-ONLY. When the cascade-aftermath path primes, the live path fades
the cascade (reversal off exhaustion). This classifier answers the prior
question the fade assumes away: is the cascade EXHAUSTED (fade justified)
or CONTINUING (fade = knife-catching; the momentum side is the paying
side)? Named pure function over the LiqPhaseEngine snapshot plus the latest
whale_absorption evidence — zero I/O, zero live orders. Wired as shadow
gates c7_exhaustion / c7_continuation so both arms are scored from birth
against the same clock the shadow journal applies to every gate.

Starvation caveat (#50, CEO s27): this scores candidates only. It does NOT
open the base_rate_veto door (95.5% of standard-path flow dies there) and
no classifier may be built around that closed door.

Doctrine: the fade leg stays shadow-graded until the WAS bar (n ≥ 50
scored shadow records, cost-adjusted EV > +0.15R, CI > 0, PF > 1.15) —
the classifier inherits the same graduation doctrine as whale_absorption.
"""

from __future__ import annotations

VERDICTS = ("exhaustion", "continuation", "unclear")

# Scoring weights — features, not thresholds on a single leg (WAS audit
# amendment #6 doctrine: no hard gate on any one feature).
_W_PHASE_EXHAUSTED = 2    # EXHAUSTION / AFTERMATH phase
_W_VELOCITY_DECAY = 1     # liq rate decelerating
_W_SILENCE = 1            # forced flow gone quiet
_W_WAS_TRUE = 2           # true_absorption on the fade side (whale identity)
_W_WAS_FOOTPRINT = 1      # footprint_only (book refill, no identity)
_W_FUNDING = 1            # funding aligned with the fade
_W_PHASE_EXPANSION = 2    # still EXPANSION — cascade accelerating
_W_VELOCITY_ACCEL = 1     # positive velocity
_W_XVENUE_LAG = 1         # Bybit still leading in the cascade direction

_VERDICT_MARGIN = 2       # minimum score separation to leave "unclear"
_WAS_FRESH_S = 900.0      # thesis half-life "full" band (whale_absorption)


def classify_aftermath(snapshot, was_evidence: dict | None = None,
                       cascade_direction: str = "") -> dict:
    """Classify a primed aftermath window for ONE symbol.

    snapshot: LiqPhaseSnapshot (or any object with .phase/.velocity/
        .silence_s/.cross_venue_lag/.cross_venue_dir/.funding_aligned).
    was_evidence: last whale_absorption emission for the symbol —
        {"direction", "class", "age_s"} — or None.
    cascade_direction: "bearish" | "bullish" — the forced-flow direction.

    Returns {"verdict", "trade_direction", "fade_direction",
             "continuation_direction", "scores", "features"}.
    trade_direction is the side the classifier WOULD trade (shadow-scored);
    "none" when the verdict is unclear.
    """
    fade = "long" if cascade_direction == "bearish" else "short"
    cont = "short" if fade == "long" else "long"

    phase_val = getattr(getattr(snapshot, "phase", None), "value",
                        getattr(snapshot, "phase", ""))
    velocity = float(getattr(snapshot, "velocity", 0.0) or 0.0)
    silence = float(getattr(snapshot, "silence_s", 0.0) or 0.0)
    xlag = bool(getattr(snapshot, "cross_venue_lag", False))
    xdir = str(getattr(snapshot, "cross_venue_dir", "none") or "none")
    funding_aligned = bool(getattr(snapshot, "funding_aligned", False))

    exh = 0
    con = 0
    feats = {"phase": phase_val, "velocity": velocity, "silence_s": silence,
             "cross_venue_lag": xlag, "cross_venue_dir": xdir,
             "funding_aligned": funding_aligned}

    if phase_val in ("exhaustion", "aftermath"):
        exh += _W_PHASE_EXHAUSTED
    elif phase_val == "expansion":
        con += _W_PHASE_EXPANSION

    if velocity <= -0.1:
        exh += _W_VELOCITY_DECAY
    elif velocity >= 0.3:
        con += _W_VELOCITY_ACCEL

    if silence >= 60.0:
        exh += _W_SILENCE

    if xlag and xdir == cont:
        con += _W_XVENUE_LAG

    was_class = None
    if was_evidence:
        was_class = was_evidence.get("class")
        age = float(was_evidence.get("age_s", float("inf")))
        if was_evidence.get("direction") == fade and age <= _WAS_FRESH_S:
            if was_class == "true_absorption":
                exh += _W_WAS_TRUE
            elif was_class == "footprint_only":
                exh += _W_WAS_FOOTPRINT
    feats["was_class"] = was_class

    if funding_aligned:
        exh += _W_FUNDING

    if exh - con >= _VERDICT_MARGIN:
        verdict, trade = "exhaustion", fade
    elif con - exh >= _VERDICT_MARGIN:
        verdict, trade = "continuation", cont
    else:
        verdict, trade = "unclear", "none"

    return {"verdict": verdict, "trade_direction": trade,
            "fade_direction": fade, "continuation_direction": cont,
            "scores": {"exhaustion": exh, "continuation": con},
            "features": feats}
