"""S1 — ETH 4h OI-pullback swing (Governor 2026-09-15, BYBIT-MAP B5 + register).

Zero-I/O brain. Entry requires ALL legs on the ETH plane:
  1. OI 30d change >= +5%           (positioning build-up; Bybit OI plane)
  2. whale L/S account ratio >= 1.8 (Bybit global accounts plane; the named-
     whale registry rides as a recorded auxiliary confirmation, non-blocking)
  3. last 3 SoDEX funding prints >= 0  (longs not paying — no negative carry)
  4. close <= BB(20,2) lower on 4h     (the pullback INTO the build-up)
  5. IV rank < 60                     (DARK — no options plane; see below)
  6. GEX != negative                  (DARK — no options plane; see below)

Exits: TP1 +3.9% / TP2 +4.8% / stop = entry - 0.79 * Wilder ATR(14, 4h);
40h time stop (rides _TT_CUTOFFS). Kill-switch legs (exit early):
funding 2 consecutive prints < 0, OI 24h change < -3%, or ETH ETF 24h
net flow < -$400M (SoSoValue plane, injected).

DARK OPTIONS LEGS: ARIA has no options data plane (2026-09-15). IV rank and
GEX arrive as None and record "dark"; REQUIRE_OPTIONS_LEGS=False means dark
does not block entry but IS recorded per-leg so the ledger can segment
regimes with/without options confirmation. Flipping the constant to True
makes dark options legs fail-closed (the honest state once a plane exists).
Never fabricate an IV/GEX value to satisfy the leg.

POLEMARCH STAMPS (filed with the spec, bind its interpretation):
  - Base rate UNKNOWN — this is the first live evidence; shadow scorer is
    wired from birth and the strategy_ledger line carries n from zero.
  - The 17.4% breakeven-WR cushion quoted in the source analysis is an
    UPPER BOUND (assumes taker-only costs at the raw grid).
  - Legs 1 and 2 are CORRELATED (whale positioning drives OI) — treat the
    entry as ~3 effective independent confirmations, not 4 (register spec:
    ~4 of 6 after the options legs go dark).
  - Cross-venue trigger tolerance +/-0.1%: the OI plane is Bybit, execution
    is SoDEX — a trigger near the boundary is regime-ambiguous.
  - The estimand NETS funding paid/received over the hold.
  - Register amendment 2026-09-15: leg 2 changed from named-whale net-long
    to whale L/S >= 1.8 (register spec). The registry leg stays recorded
    (aux_named_whale_long) for segmentation but never blocks.
"""

from __future__ import annotations

from typing import Optional, Sequence

# Knob defaults (config overrides ride main.py's splice; these pin the spec)
MIN_OI_30D_CHG_PCT = 5.0
KILL_OI_24H_CHG_PCT = -3.0
MIN_WHALE_LS = 1.8
KILL_ETF_OUTFLOW_USD = -400_000_000.0
IV_RANK_MAX = 60.0
REQUIRE_OPTIONS_LEGS = False
FUNDING_PRINTS = 3
BB_LEN = 20
BB_MULT = 2.0
STOP_ATR_MULT = 0.79
TP1_PCT = 3.9
TP2_PCT = 4.8
TIME_STOP_H = 40

# Cross-venue trigger ambiguity band (Polemarch stamp): within this of the
# threshold the leg reports "borderline" so the ledger can segment.
BORDERLINE_PCT = 0.1


def oi_nearest(rows: Sequence[tuple], target_ms: int) -> Optional[tuple]:
    """(open_time_ms, oi) row nearest to target_ms, or None on empty.

    The collector stores mixed 5min/1h resolution — nearest-row lookup is
    the honest read (never interpolate a positioning series).
    """
    best = None
    for r in rows:
        if best is None or abs(r[0] - target_ms) < abs(best[0] - target_ms):
            best = r
    return best


def oi_change_pct(rows: Sequence[tuple], lookback_ms: int,
                  now_ms: Optional[int] = None) -> Optional[float]:
    """Percent change of OI over the lookback using nearest-row endpoints.

    None when coverage is insufficient (the then-row must sit within the
    OLDER half of the window — otherwise 'change' is measured on air).
    """
    if not rows:
        return None
    now_ms = now_ms if now_ms is not None else max(r[0] for r in rows)
    then = oi_nearest(rows, now_ms - lookback_ms)
    now = oi_nearest(rows, now_ms)
    if then is None or now is None or then[0] >= now[0] or then[1] <= 0:
        return None
    if then[0] > now_ms - lookback_ms * 0.5:
        return None
    return (now[1] / then[1] - 1.0) * 100.0


def bollinger_lower(closes: Sequence[float], length: int = BB_LEN,
                    mult: float = BB_MULT) -> Optional[float]:
    if len(closes) < length:
        return None
    window = list(closes[-length:])
    mean = sum(window) / length
    var = sum((c - mean) ** 2 for c in window) / length
    return mean - mult * (var ** 0.5)


def wilder_atr(highs: Sequence[float], lows: Sequence[float],
               closes: Sequence[float], period: int = 14) -> Optional[float]:
    """Wilder recursive ATR — identical recursion to
    core/structure_analyzer._calculate_atr (pinned equal in tests).
    None (not a price-relative guess) on insufficient data: the S1 stop is
    sized off this number, so a fabricated ATR is a fabricated stop.
    """
    if len(closes) < period + 1 or len(highs) < period + 1 or len(lows) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    if not trs:
        return None
    alpha = 1.0 / period
    atr = trs[0]
    for tr in trs[1:]:
        atr = alpha * tr + (1.0 - alpha) * atr
    return atr


def funding_leg_ok(rates: Sequence[float], n: int = FUNDING_PRINTS) -> Optional[bool]:
    """Last n prints all >= 0 (longs not paying). None = insufficient data
    (fail-closed: no entry on a dark funding plane)."""
    if len(rates) < n:
        return None
    return all(r >= 0 for r in list(rates)[-n:])


def evaluate_entry(oi_chg_30d: Optional[float], whale_ls: Optional[float],
                   funding_ok: Optional[bool], close: float,
                   bb_lower: Optional[float],
                   iv_rank: Optional[float] = None,
                   gex: Optional[str] = None,
                   named_whale_net: Optional[float] = None,
                   min_oi_chg: float = MIN_OI_30D_CHG_PCT,
                   min_ls: float = MIN_WHALE_LS) -> dict:
    """All-legs verdict. Every leg reports pass/fail/borderline/dark so
    the shadow record can segment near-misses from structural absences.
    Options legs (iv_rank, gex) are dark by construction until an options
    plane exists; REQUIRE_OPTIONS_LEGS governs whether dark blocks.
    named_whale_net is a recorded auxiliary confirmation, never blocking."""
    legs = {}
    if oi_chg_30d is None:
        legs["oi_30d"] = "dark"
    elif oi_chg_30d >= min_oi_chg:
        legs["oi_30d"] = "pass"
    elif oi_chg_30d >= min_oi_chg - BORDERLINE_PCT:
        legs["oi_30d"] = "borderline"
    else:
        legs["oi_30d"] = "fail"
    legs["whale_ls"] = ("dark" if whale_ls is None
                        else ("pass" if whale_ls >= min_ls else "fail"))
    legs["funding"] = ("dark" if funding_ok is None
                       else ("pass" if funding_ok else "fail"))
    if bb_lower is None or close <= 0:
        legs["bb_pullback"] = "dark"
    else:
        legs["bb_pullback"] = "pass" if close <= bb_lower else "fail"
    legs["iv_rank"] = ("dark" if iv_rank is None
                       else ("pass" if iv_rank < IV_RANK_MAX else "fail"))
    legs["gex"] = ("dark" if gex is None
                   else ("fail" if gex == "negative" else "pass"))
    required = ("oi_30d", "whale_ls", "funding", "bb_pullback")
    ok = all(legs[k] == "pass" for k in required)
    if REQUIRE_OPTIONS_LEGS:
        ok = ok and all(legs[k] == "pass" for k in ("iv_rank", "gex"))
    return {"ok": ok, "legs": legs,
            "aux_named_whale_long": (None if named_whale_net is None
                                     else named_whale_net > 0),
            "oi_chg_30d": oi_chg_30d, "whale_ls": whale_ls,
            "close": close, "bb_lower": bb_lower}


def evaluate_kill(rates: Sequence[float], oi_chg_24h: Optional[float],
                  etf_flow_24h_usd: Optional[float] = None,
                  kill_oi_chg: float = KILL_OI_24H_CHG_PCT,
                  kill_etf_usd: float = KILL_ETF_OUTFLOW_USD) -> tuple:
    """(kill, reason). reason="" when no kill. Funding needs 2 consecutive
    negative prints; OI kill fires at the 24h unwind threshold; ETF kill on
    a >$400M 24h outflow. None flows carry no opinion, never kill."""
    recent = list(rates)[-2:]
    if len(recent) == 2 and all(r < 0 for r in recent):
        return True, "funding_2x_negative"
    if oi_chg_24h is not None and oi_chg_24h < kill_oi_chg:
        return True, "oi_24h_unwind"
    if etf_flow_24h_usd is not None and etf_flow_24h_usd < kill_etf_usd:
        return True, "etf_outflow"
    return False, ""


def bracket(entry: float, atr_4h: float, stop_mult: float = STOP_ATR_MULT,
            tp1_pct: float = TP1_PCT, tp2_pct: float = TP2_PCT) -> tuple:
    """(stop, tp1, tp2) for a LONG swing. Stop distances off the 4h ATR;
    TPs are fixed percentages per the spec (not R-multiples)."""
    stop = entry - stop_mult * atr_4h
    return stop, entry * (1 + tp1_pct / 100.0), entry * (1 + tp2_pct / 100.0)
