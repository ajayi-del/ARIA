"""PRE-CASCADE — 4-point pre-cascade regime score (Governor 2026-09-15
directive: "wire the pre-cascade score into the fingerprint").

Zero-I/O brain. This is a REGIME signal layer, not a strategy: per 1h
observation on one symbol, four legs score the trapped-long equilibrium:

  +1  latest funding print < 0
  +1  two prior consecutive funding settlements < 0
      (with leg 1 that is 3 consecutive negatives — legs 1 and 2 are read
      as ONE joint trailing streak: a non-negative latest breaks the chain
      for both)
  +1  OI 6h pct change > 0  (expanding INTO negative funding = unstable
      equilibrium — new positioning piling onto the side that is paying)
  +1  whale long/short account ratio > 1.8  (STRICTLY greater; exactly
      1.8 fails)

Score 0-4. THRESHOLD_SCORE = 4.

DARK SEMANTICS (fail-closed, documented because it is the whole point):
  - funding plane dark (None or empty rates) -> funding_neg_streak returns
    the -1 DARK SENTINEL (a streak can never legitimately be -1, so the
    sentinel is unambiguous downstream; a caller that forgets to check and
    compares ">= 3" still fails closed).
  - Any single dark leg makes "score" None and "threshold_met" None. A
    3/3 measured is NEVER presented as a 3/4: legs are not interchangeable
    — a missing leg is missing information, not a zero. Summing the
    measured legs would manufacture a score the evidence does not support
    (the unmeasured leg could pass). Fail closed: dark in, None out.

POLEMARCH STAMPS (filed with the spec, bind its interpretation):
  - REGIME READ: negative funding + expanding OI + whale L/S > 1.8 = a
    trapped-long equilibrium. It resolves either as a short squeeze (rips)
    or a long capitulation (cascade). The score says WHICH REGIME, not
    WHICH DIRECTION — direction comes from the cascade print itself or
    the strategy layer. Never trade the score as a direction signal.
  - STRUCTURAL PRIOR: P(down-cascade within 7d | score >= 4) estimated
    45-55%, MEDIUM confidence, base rates NOT yet event-counted.
    tools/cascade_census.py is being built in parallel to measure it —
    THRESHOLD_SCORE and the leg thresholds may be revisited when that
    census lands. This module is coupled to that census; treat the
    constants as priors awaiting measurement, not calibrated truths.
  - CROSS-VENUE: the L/S ratio is a Bybit plane, execution is SoDEX —
    regime ambiguity near the 1.8 boundary. score_symbol reports the raw
    ls_ratio in the returned dict so the ledger can segment near-boundary
    reads from decisive ones.
  - OI endpoints are nearest-row reads (mixed 5min/1h collector
    resolution — never interpolate a positioning series); thin coverage
    (then-row in the newer half of the 6h window) is dark, per
    intelligence/s1_oi_pullback's older-half rule.
"""

from __future__ import annotations

from typing import Optional, Sequence

from intelligence.s1_oi_pullback import oi_change_pct

# Knob constants (the spec pins these; census may revisit — see stamps)
THRESHOLD_SCORE = 4
WHALE_LS_THRESHOLD = 1.8
OI_WINDOW_MS = 6 * 3600 * 1000
DARK_STREAK = -1  # sentinel: funding plane dark (never a real streak)


def funding_neg_streak(rates: Optional[Sequence[float]]) -> int:
    """Trailing consecutive NEGATIVE funding settlements, newest last.

    None or empty = the funding plane is dark -> returns DARK_STREAK (-1),
    the dark sentinel (documented in the module docstring). A rate of
    exactly 0.0 is NOT negative — longs paying nothing is not the trapped
    regime. A None element inside the series is not negative either (it
    breaks the streak rather than darkening the whole plane).
    """
    if rates is None or len(rates) == 0:
        return DARK_STREAK
    streak = 0
    for r in reversed(list(rates)):
        if r is not None and r < 0:
            streak += 1
        else:
            break
    return streak


def oi_expanding(oi_rows: Sequence[tuple], now_ms: int,
                 window_ms: int = OI_WINDOW_MS) -> Optional[bool]:
    """True when OI pct change over the window is > 0 (strictly).

    oi_rows = (ts_ms, oi) ascending. Nearest-row endpoints via
    s1_oi_pullback.oi_change_pct; None when coverage is thin (then-row in
    the newer half of the window — 'change' measured on air is dark, not
    flat).
    """
    chg = oi_change_pct(oi_rows, window_ms, now_ms)
    if chg is None:
        return None
    return chg > 0


def score_symbol(funding_rates: Optional[Sequence[float]],
                 oi_rows: Sequence[tuple],
                 ls_ratio: Optional[float],
                 now_ms: int,
                 window_ms: int = OI_WINDOW_MS,
                 ls_threshold: float = WHALE_LS_THRESHOLD) -> dict:
    """Assemble the 4-leg score for one symbol at one observation.

    Every leg reports "pass"/"fail"/"dark" so the shadow record segments
    near-misses from structural absences. Any dark leg -> score None and
    threshold_met None (never a partial score presented as a real one).
    The raw ls_ratio rides the dict for cross-venue ledger segmentation.
    """
    streak = funding_neg_streak(funding_rates)
    if streak == DARK_STREAK:
        legs = {"funding_neg": "dark", "funding_neg_2prior": "dark"}
    else:
        legs = {
            "funding_neg": "pass" if streak >= 1 else "fail",
            "funding_neg_2prior": "pass" if streak >= 3 else "fail",
        }

    expanding = oi_expanding(oi_rows, now_ms, window_ms)
    legs["oi_expanding"] = ("dark" if expanding is None
                            else ("pass" if expanding else "fail"))

    legs["whale_ls"] = ("dark" if ls_ratio is None
                        else ("pass" if ls_ratio > ls_threshold else "fail"))

    if any(v == "dark" for v in legs.values()):
        score = None
        threshold_met = None
    else:
        score = sum(1 for v in legs.values() if v == "pass")
        threshold_met = score >= THRESHOLD_SCORE

    return {"score": score, "legs": legs, "threshold_met": threshold_met,
            "ls_ratio": ls_ratio}


def verdict(score_dict: dict) -> str:
    """Regime label: pre_cascade_armed (threshold met) / watch (3) /
    calm (0-2) / dark (score None — plane dark, no regime claim made)."""
    if score_dict.get("score") is None:
        return "dark"
    if score_dict.get("threshold_met"):
        return "pre_cascade_armed"
    if score_dict["score"] == 3:
        return "watch"
    return "calm"
