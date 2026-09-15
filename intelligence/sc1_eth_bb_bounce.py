"""Scalp 1 — ETH 5m BB(20,2) lower-band bounce (register 2026-09-15).

Zero-I/O brain. The ONLY taker-viable crypto scalp; ETH only by design —
BTC cannot be scalped (a 1:2R stop is 1.26 ATR, inside one 5m candle's
noise; Governor fee doctrine 2026-09-15).

Entry requires ALL legs on 5m bars:
  1. close <= BB(20,2) lower AND close > BB_lower * 0.9980
     (the touch-without-rupture band)
  2. prior bar closed INSIDE the bands (prior_lower < prior_close < prior_upper)
  3. volume >= 1.2 * MA(20) of volume
  4. funding >= 0 (last print; injected Optional — None = dark, fail-closed)
  5. spread <= 2.5bp (injected Optional[float] bp; None = dark)
  6. entry bar closes bullish (close > open)
  7. fee-ratio leg: (spread_bp + taker_RT_bp) / TP2_BP <= 0.30
     (Governor structural fee gate; SoDEX live effective taker RT = 7.60bp
     raw Tier-0 4.0bp/side less the 5% SOSO-staking discount)

Exits: TP1 +30bp (BB-mid convention), TP2 +59bp (BB-upper convention),
stop -14bp, TIME_STOP_MIN = 25 (constant only — the clock rides the runner).

Kill legs (exit early):
  - 5m BB width expands > 3x its width at entry
  - funding flips negative mid-trade
  - volume on the entry bar < 1.5x MA (re-check leg)

POLEMARCH STAMPS (filed with the spec, bind its interpretation):
  - Bollinger math uses POPULATION variance (divide by n), identical to
    intelligence/s1_oi_pullback.bollinger_lower — one variance convention
    per department, pinned in tests.
  - MA(20) of volume EXCLUDES the signal bar: the entry bar's own volume
    must never lift its own benchmark. Needs n+1 prints, else dark.
  - The rupture band is STRICT: close == BB_lower * 0.9980 fails (must be
    strictly above the floor); close == BB_lower passes (touch allowed).
  - Prior-bar-inside is STRICT: a prior close exactly ON either band fails.
  - The fee leg is measured to TP2 (+59bp, the farthest target) at TAKER
    round-trip — the most conservative read. The register's quoted "34.2%
    taker breakeven at TP2" is context, NOT reproduced arithmetic; the
    binding gate is fee_ratio <= 0.30 with spread included (FEE_RATIO_MAX,
    mirrored from min_viable_tp.FEE_RATIO_RETIRE).
  - Dark = fail-closed on EVERY injected leg (bands, vol, funding, spread,
    fee state). Only the bar itself is never dark — the caller owns it.
  - TIME_STOP_MIN is a constant pin only; no clock lives in this module.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence, Tuple

from intelligence.min_viable_tp import FEE_RATIO_RETIRE

# Knob constants (config overrides ride the splice; these pin the spec)
BB_LEN = 20
BB_MULT = 2.0
RUPTURE_FLOOR = 0.9980
VOL_MULT = 1.2
VOL_KILL_MULT = 1.5
VOL_MA_LEN = 20
SPREAD_MAX_BP = 2.5
TP1_BP = 30.0
TP2_BP = 59.0
STOP_BP = 14.0
TIME_STOP_MIN = 25
BB_WIDTH_KILL_MULT = 3.0
FEE_RATIO_MAX = FEE_RATIO_RETIRE  # 0.30 — Governor structural fee gate


def bb_bands(closes: Sequence[float], length: int = BB_LEN,
             mult: float = BB_MULT) -> Optional[Tuple[float, float, float]]:
    """(lower, mid, upper) over the last `length` closes, population stdev.

    None on insufficient data — never a fabricated band (fail-closed).
    """
    if len(closes) < length:
        return None
    window = list(closes[-length:])
    mean = sum(window) / length
    var = sum((c - mean) ** 2 for c in window) / length
    sd = var ** 0.5
    return mean - mult * sd, mean, mean + mult * sd


def vol_ma_ok(vols: Sequence[float], mult: float,
              n: int = VOL_MA_LEN) -> Optional[bool]:
    """Latest bar's volume >= mult * MA(n) over the n PRECEDING bars.

    None when fewer than n+1 prints exist (dark, fail-closed). The signal
    bar is never part of its own benchmark (Polemarch stamp).
    """
    if len(vols) < n + 1:
        return None
    ma = sum(list(vols)[-(n + 1):-1]) / n
    return list(vols)[-1] >= mult * ma


def fee_ratio(spread_bp: Optional[float], fee_state: Optional[Mapping],
              gross_tp_bp: Optional[float]) -> Optional[float]:
    """(spread_bp + taker_RT_bp) / gross_tp_bp — the Governor's structural
    fee ratio. None = dark (any input missing or non-positive target)."""
    if fee_state is None or spread_bp is None:
        return None
    if gross_tp_bp is None or gross_tp_bp <= 0:
        return None
    rt = fee_state.get("taker")
    if rt is None:
        return None
    return (spread_bp + rt) / gross_tp_bp


def fee_ratio_ok(spread_bp: Optional[float], fee_state: Optional[Mapping],
                 gross_tp_bp: Optional[float],
                 max_ratio: float = FEE_RATIO_MAX) -> Optional[bool]:
    """Structural fee gate: ratio <= max_ratio passes. None = dark."""
    fr = fee_ratio(spread_bp, fee_state, gross_tp_bp)
    return None if fr is None else fr <= max_ratio


def evaluate_entry(bar: Mapping, prior_bar: Mapping,
                   bands: Optional[Tuple[float, float, float]],
                   prior_bands: Optional[Tuple[float, float, float]],
                   vol_ok: Optional[bool], funding: Optional[float],
                   spread_bp: Optional[float],
                   fee_state: Optional[Mapping],
                   tp_bp: float = TP2_BP) -> dict:
    """All-legs verdict. Every leg reports pass/fail/dark so the shadow
    record segments near-misses from structural absences. ok = ALL pass."""
    legs = {}
    close = bar["close"]
    open_ = bar["open"]

    if bands is None:
        legs["bb_touch"] = "dark"
    else:
        lower = bands[0]
        legs["bb_touch"] = ("pass" if (close <= lower
                                       and close > lower * RUPTURE_FLOOR)
                            else "fail")

    if prior_bands is None:
        legs["prior_inside"] = "dark"
    else:
        p_close = prior_bar["close"]
        legs["prior_inside"] = ("pass" if (p_close > prior_bands[0]
                                           and p_close < prior_bands[2])
                                else "fail")

    legs["volume"] = ("dark" if vol_ok is None
                      else ("pass" if vol_ok else "fail"))
    legs["funding"] = ("dark" if funding is None
                       else ("pass" if funding >= 0 else "fail"))
    legs["spread"] = ("dark" if spread_bp is None
                      else ("pass" if spread_bp <= SPREAD_MAX_BP else "fail"))
    legs["bullish_close"] = "pass" if close > open_ else "fail"

    fr = fee_ratio(spread_bp, fee_state, tp_bp)
    if fr is None:
        legs["fee_ratio"] = "dark"
    else:
        legs["fee_ratio"] = "pass" if fr <= FEE_RATIO_MAX else "fail"

    ok = all(v == "pass" for v in legs.values())
    return {"ok": ok, "legs": legs, "fee_ratio": fr, "close": close,
            "spread_bp": spread_bp}


def evaluate_kill(width_now: Optional[float] = None,
                  width_entry: Optional[float] = None,
                  funding_now: Optional[float] = None,
                  vol_ok_15: Optional[bool] = None,
                  width_kill_mult: float = BB_WIDTH_KILL_MULT) -> tuple:
    """(kill, reason). reason="" when no kill. Kills fail OPEN — a dark
    plane never exits a live trade, it only blocks entries. Checks fire in
    spec order: width expansion, funding flip, entry-volume re-check."""
    if (width_now is not None and width_entry is not None
            and width_entry > 0 and width_now > width_kill_mult * width_entry):
        return True, "bb_width_expansion_3x"
    if funding_now is not None and funding_now < 0:
        return True, "funding_flip_negative"
    if vol_ok_15 is not None and not vol_ok_15:
        return True, "entry_volume_recheck_fail"
    return False, ""


def bracket(entry: float, tp1_bp: float = TP1_BP, tp2_bp: float = TP2_BP,
            stop_bp: float = STOP_BP) -> tuple:
    """(stop, tp1, tp2) for a LONG scalp, bp-of-entry -> price conversion."""
    stop = entry * (1.0 - stop_bp / 1e4)
    tp1 = entry * (1.0 + tp1_bp / 1e4)
    tp2 = entry * (1.0 + tp2_bp / 1e4)
    return stop, tp1, tp2
