"""S2 — BTC 4h dip-buy swing (Governor 2026-09-15, BYBIT-MAP register).

Zero-I/O brain. Entry requires ALL legs on the BTC plane:
  1. BTC funding >= 0 for 3 consecutive 8h settlement prints (longs not
     paying into the dip — no negative carry while bidding support)
  2. whale long/short account ratio >= 1.3 (Bybit L/S positioning plane)
  3. close <= 4h Fibonacci S1 support (the dip INTO the bid wall)
  4. IV rank < 55 (ARIA HAS NO OPTIONS DATA PLANE — Optional leg, see stamps)
  5. GEX != negative (same dark-leg treatment — no options plane)
  6. OI 24h change > -3% (no open-interest unwind under the bid)

Exits: TP1 = entry + 3.40*ATR(14,4h), TP2 = entry + 4.09*ATR(14,4h),
stop = entry - 0.73*ATR. TIME_STOP_H = 48 (constant only; the local node
wires the clock). Kill legs (exit early): funding 2 consecutive prints
< 0; ETF 24h net flow < -$600M (None = no opinion, never kills);
OI 30d change < -20%.

FIBONACCI CONVENTION (interpretive — the register does not define it):
  swing high/low = max/min of the last FIB_LOOKBACK (default 30) 4h
  highs/lows; S1 = high - 0.236*(high - low), the first retracement
  support of the upswing. None on fewer bars than the lookback
  (fail-closed: a partial-window swing is a fabricated level).

POLEMARCH STAMPS (filed with the spec, bind its interpretation):
  - Base rate UNKNOWN — this is the first live evidence of the class;
    the shadow record carries n from zero.
  - The 23.4% breakeven-WR cushion quoted in the source analysis is a
    LOWER BOUND here: it assumes the Bybit fee grid, while SoDEX live
    effective RT taker is 7.60bp — cheaper, so the true cushion is wider.
  - The OI and L/S legs are CORRELATED (whale positioning drives OI) —
    treat the entry as ~4 effective independent confirmations, not 6.
  - Cross-venue: the OI / L-S / funding planes are Bybit, execution is
    SoDEX — a boundary print is regime-ambiguous (borderline band on the
    L/S and OI legs so the ledger can segment near-misses).
  - IV-rank and GEX have NO data plane in ARIA (probed 2026-09-15).
    REQUIRE_OPTIONS_LEGS=False: a DARK options leg is recorded for
    ledger segmentation but never blocks entry; a PRESENT and failing
    options leg still blocks (evidence in hand outranks a missing plane).
  - The estimand NETS funding paid/received over the hold.
"""

from __future__ import annotations

from typing import Optional, Sequence

from intelligence.s1_oi_pullback import wilder_atr, funding_leg_ok

# Knob defaults (config overrides ride main.py's splice; these pin the spec)
MIN_LS_RATIO = 1.3
LS_BORDERLINE = 0.05
OI_24H_MIN_PCT = -3.0
OI_BORDERLINE_PCT = 0.1
IV_RANK_MAX = 55.0
REQUIRE_OPTIONS_LEGS = False
KILL_ETF_OUTFLOW_USD = -600_000_000
KILL_OI_30D_PCT = -20.0
STOP_ATR_MULT = 0.73
TP1_ATR_MULT = 3.40
TP2_ATR_MULT = 4.09
TIME_STOP_H = 48
FUNDING_PRINTS = 3
FIB_LOOKBACK = 30
FIB_S1_RETRACE = 0.236

__all__ = ["wilder_atr", "fib_s1", "evaluate_entry", "evaluate_kill", "bracket"]


def fib_s1(highs: Sequence[float], lows: Sequence[float],
           lookback: int = FIB_LOOKBACK) -> Optional[float]:
    """First retracement support of the swing over the last `lookback` bars.

    S1 = swing_high - 0.236 * (swing_high - swing_low). None on
    insufficient data — never fabricate a level from a partial window.
    """
    if lookback <= 0 or len(highs) < lookback or len(lows) < lookback:
        return None
    hi = max(highs[-lookback:])
    lo = min(lows[-lookback:])
    return hi - FIB_S1_RETRACE * (hi - lo)


def evaluate_entry(rates: Sequence[float], ls_ratio: Optional[float],
                   close: float, s1: Optional[float],
                   iv_rank: Optional[float], gex: Optional[str],
                   oi_chg_24h: Optional[float],
                   min_ls: float = MIN_LS_RATIO,
                   oi_min: float = OI_24H_MIN_PCT,
                   iv_max: float = IV_RANK_MAX,
                   funding_prints: int = FUNDING_PRINTS) -> dict:
    """All-legs verdict. Every leg reports pass/fail/borderline/dark so the
    shadow record can segment near-misses from structural absences.

    Required legs (dark = fail-closed, blocks): funding, ls_ratio,
    fib_support, oi_24h. Options legs (iv_rank, gex): dark is recorded but
    does NOT block while REQUIRE_OPTIONS_LEGS is False; a present-but-
    failing options leg blocks regardless (evidence beats absence).
    """
    legs = {}

    fok = funding_leg_ok(rates, n=funding_prints)
    legs["funding"] = "dark" if fok is None else ("pass" if fok else "fail")

    if ls_ratio is None:
        legs["ls_ratio"] = "dark"
    elif ls_ratio >= min_ls:
        legs["ls_ratio"] = "pass"
    elif ls_ratio >= min_ls - LS_BORDERLINE:
        legs["ls_ratio"] = "borderline"
    else:
        legs["ls_ratio"] = "fail"

    if s1 is None or close <= 0:
        legs["fib_support"] = "dark"
    else:
        legs["fib_support"] = "pass" if close <= s1 else "fail"

    if iv_rank is None:
        legs["iv_rank"] = "dark"
    else:
        legs["iv_rank"] = "pass" if iv_rank < iv_max else "fail"

    if gex is None:
        legs["gex"] = "dark"
    else:
        legs["gex"] = "fail" if gex == "negative" else "pass"

    if oi_chg_24h is None:
        legs["oi_24h"] = "dark"
    elif oi_chg_24h > oi_min:
        legs["oi_24h"] = "pass"
    elif oi_chg_24h >= oi_min - OI_BORDERLINE_PCT:
        legs["oi_24h"] = "borderline"
    else:
        legs["oi_24h"] = "fail"

    required = ("funding", "ls_ratio", "fib_support", "oi_24h")
    ok = all(legs[k] == "pass" for k in required)
    if REQUIRE_OPTIONS_LEGS:
        ok = ok and all(legs[k] == "pass" for k in ("iv_rank", "gex"))
    else:
        ok = ok and all(legs[k] != "fail" for k in ("iv_rank", "gex"))

    return {"ok": ok, "legs": legs, "ls_ratio": ls_ratio, "close": close,
            "s1": s1, "iv_rank": iv_rank, "gex": gex,
            "oi_chg_24h": oi_chg_24h}


def evaluate_kill(rates: Sequence[float], etf_flow_24h_usd: Optional[float],
                  oi_chg_30d: Optional[float],
                  kill_etf: float = KILL_ETF_OUTFLOW_USD,
                  kill_oi: float = KILL_OI_30D_PCT) -> tuple:
    """(kill, reason). reason="" when no kill. Funding needs 2 consecutive
    negative prints; the ETF leg has NO OPINION on None (a dark flow plane
    never kills); the OI 30d collapse fires at the structural unwind line.
    """
    recent = list(rates)[-2:]
    if len(recent) == 2 and all(r < 0 for r in recent):
        return True, "funding_2x_negative"
    if etf_flow_24h_usd is not None and etf_flow_24h_usd < kill_etf:
        return True, "etf_outflow"
    if oi_chg_30d is not None and oi_chg_30d < kill_oi:
        return True, "oi_30d_collapse"
    return False, ""


def bracket(entry: float, atr_4h: float, stop_mult: float = STOP_ATR_MULT,
            tp1_mult: float = TP1_ATR_MULT,
            tp2_mult: float = TP2_ATR_MULT) -> tuple:
    """(stop, tp1, tp2) for a LONG swing. All three distances are ATR
    multiples per the spec (not fixed percentages)."""
    stop = entry - stop_mult * atr_4h
    return stop, entry + tp1_mult * atr_4h, entry + tp2_mult * atr_4h
