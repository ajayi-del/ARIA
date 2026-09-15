"""S3 — ETH/BTC ratio spread, 4h (Governor 2026-09-15, BYBIT-MAP register).

Zero-I/O brain for the pairs trade LONG ETH perp / SHORT BTC perp.
Entry requires ALL legs:
  1. ETH OI 30d >= +5%       (positioning rotating INTO the ETH leg)
  2. BTC OI 30d <= -10%      (positioning rotating OUT of the BTC leg)
  3. ETH whale L/S minus BTC whale L/S >= 0.5
     (injected as TWO floats, eth_ls_ratio and btc_ls_ratio; the spread is
     computed inside so the ledger keeps both raw planes, not just the diff)
  4. ETH top-trader position ratio >= 1.70 — THE BYBIT V5 ENDPOINT IS 404:
     probed 2026-09-15, /v5/market/top-trader-ratio does not exist.
     Optional[float]; None = dark leg, recorded for segmentation;
     REQUIRE_TOP_TRADER=False default so dark never blocks.
  5. ETH/BTC ratio <= 4h VWAP of the ratio series (buy the ratio at a
     discount to its own flow-weighted mean)

VWAP CONVENTION (interpretive — the register does not define it): with a
volume series injected, VWAP = sum(ratio*vol)/sum(vol); with NO volume
series the fallback is the plain arithmetic mean of the ratio series
(close-weighted — every 4h print counts equally, the honest read when
turnover on the ratio itself is unobservable). Mismatched series lengths
= None (fail-closed, ambiguous input).

Exit: ratio reaches 4h Fib R1 of the ratio series (R1 = low + 0.236*
(high - low) over FIB_LOOKBACK — the first retracement resistance of the
down-leg, i.e. the ratio's first bounce target) OR either leg's funding
flips negative for 2 consecutive prints.

Kill: BTC ETF 24h net inflow > +$800M (a mega-inflow is a BTC-dominance
event — the short BTC leg is structurally wrong). None = no opinion,
never kills. An ETH-specific negative event is a MANUAL OPERATOR FLAG
only — documented here, deliberately not coded (no auto-source exists).

POLEMARCH STAMPS (filed with the spec, bind its interpretation):
  - CARRY: negative carry ~= -0.039bp/8h — a wash, NOT a carry trade.
    The estimand must NET the funding paid/received on BOTH legs.
  - SIGNAL COUNT: n = 3-5 per 90d — far below any significance bar.
    SHADOW_ONLY=True: this strategy is shadow-scored from birth and any
    live-trading gate requires an explicit Governor override. evaluate_entry
    still returns its verdict dict; the live gate reads SHADOW_ONLY (the
    wiring is the local node's, not this module's).
  - Cross-venue: the OI / L-S planes are Bybit, execution is SoDEX.
  - Base rate UNKNOWN at n=3-5/90d — the shadow ledger carries n from zero.
"""

from __future__ import annotations

from typing import Optional, Sequence

# Knob defaults (config overrides ride main.py's splice; these pin the spec)
ETH_OI_30D_MIN_PCT = 5.0
BTC_OI_30D_MAX_PCT = -10.0
MIN_LS_SPREAD = 0.5
MIN_TOP_TRADER_RATIO = 1.70
REQUIRE_TOP_TRADER = False
FIB_LOOKBACK = 30
FIB_R1_RETRACE = 0.236
KILL_BTC_ETF_INFLOW_USD = 800_000_000
FUNDING_FLIP_PRINTS = 2

# Polemarch stamp: n=3-5 per 90d is below any significance bar — this
# strategy is shadow-scored from birth; live trading needs a Governor
# override of this constant. Declared, never enforced here.
SHADOW_ONLY = True


def ratio_vwap(ratios: Sequence[float],
               volumes: Optional[Sequence[float]] = None) -> Optional[float]:
    """Flow-weighted mean of the ETH/BTC ratio series.

    With volumes: sum(r*v)/sum(v). Without volumes (or a zero-sum volume
    series): arithmetic mean — the close-weighted fallback documented in
    the module docstring. None on an empty ratio series or on mismatched
    series lengths (fail-closed: ambiguous input is never averaged).
    """
    if not ratios:
        return None
    rs = list(ratios)
    if volumes is None:
        return sum(rs) / len(rs)
    vs = list(volumes)
    if len(vs) != len(rs):
        return None
    tot = sum(vs)
    if tot <= 0:
        return sum(rs) / len(rs)
    return sum(r * v for r, v in zip(rs, vs)) / tot


def fib_r1(highs: Sequence[float], lows: Sequence[float],
           lookback: int = FIB_LOOKBACK) -> Optional[float]:
    """First retracement resistance of the ratio's swing over the last
    `lookback` bars: R1 = swing_low + 0.236 * (swing_high - swing_low).
    None on insufficient data — never fabricate a level from a partial
    window."""
    if lookback <= 0 or len(highs) < lookback or len(lows) < lookback:
        return None
    hi = max(highs[-lookback:])
    lo = min(lows[-lookback:])
    return lo + FIB_R1_RETRACE * (hi - lo)


def evaluate_entry(eth_oi_30d: Optional[float], btc_oi_30d: Optional[float],
                   eth_ls_ratio: Optional[float],
                   btc_ls_ratio: Optional[float],
                   top_trader_ratio: Optional[float],
                   ratio_now: Optional[float], vwap: Optional[float],
                   eth_oi_min: float = ETH_OI_30D_MIN_PCT,
                   btc_oi_max: float = BTC_OI_30D_MAX_PCT,
                   min_spread: float = MIN_LS_SPREAD,
                   min_tt: float = MIN_TOP_TRADER_RATIO) -> dict:
    """All-legs verdict. Required legs (dark = fail-closed, blocks):
    eth_oi_30d, btc_oi_30d, ls_spread, ratio_vs_vwap. The top-trader leg
    is OPTIONAL (Bybit V5 endpoint is 404 — probed 2026-09-15): dark is
    recorded for segmentation but never blocks while REQUIRE_TOP_TRADER
    is False; a present-but-failing value blocks regardless.

    The L/S leg is injected as the two raw plane floats; the spread is
    computed here so the verdict dict carries both planes AND the diff.
    """
    legs = {}

    if eth_oi_30d is None:
        legs["eth_oi_30d"] = "dark"
    else:
        legs["eth_oi_30d"] = "pass" if eth_oi_30d >= eth_oi_min else "fail"

    if btc_oi_30d is None:
        legs["btc_oi_30d"] = "dark"
    else:
        legs["btc_oi_30d"] = "pass" if btc_oi_30d <= btc_oi_max else "fail"

    ls_spread = None
    if eth_ls_ratio is not None and btc_ls_ratio is not None:
        ls_spread = eth_ls_ratio - btc_ls_ratio
    if ls_spread is None:
        legs["ls_spread"] = "dark"
    else:
        legs["ls_spread"] = "pass" if ls_spread >= min_spread else "fail"

    if top_trader_ratio is None:
        legs["top_trader"] = "dark"
    else:
        legs["top_trader"] = "pass" if top_trader_ratio >= min_tt else "fail"

    if ratio_now is None or vwap is None:
        legs["ratio_vs_vwap"] = "dark"
    else:
        legs["ratio_vs_vwap"] = "pass" if ratio_now <= vwap else "fail"

    required = ("eth_oi_30d", "btc_oi_30d", "ls_spread", "ratio_vs_vwap")
    ok = all(legs[k] == "pass" for k in required)
    if REQUIRE_TOP_TRADER:
        ok = ok and legs["top_trader"] == "pass"
    else:
        ok = ok and legs["top_trader"] != "fail"

    return {"ok": ok, "legs": legs, "eth_oi_30d": eth_oi_30d,
            "btc_oi_30d": btc_oi_30d, "eth_ls_ratio": eth_ls_ratio,
            "btc_ls_ratio": btc_ls_ratio, "ls_spread": ls_spread,
            "top_trader_ratio": top_trader_ratio, "ratio_now": ratio_now,
            "vwap": vwap, "shadow_only": SHADOW_ONLY}


def _two_negative(rates: Sequence[float]) -> bool:
    recent = list(rates)[-FUNDING_FLIP_PRINTS:]
    return len(recent) == FUNDING_FLIP_PRINTS and all(r < 0 for r in recent)


def evaluate_exit(ratio_now: Optional[float], r1: Optional[float],
                  eth_rates: Sequence[float],
                  btc_rates: Sequence[float]) -> tuple:
    """(exit, reason). reason="" when no exit. The R1 target needs BOTH a
    priced ratio and a computed level — a dark leg simply cannot fire that
    branch (the funding-flip branch still can; exiting on carry turning
    against both legs never waits for a missing level)."""
    if ratio_now is not None and r1 is not None and ratio_now >= r1:
        return True, "fib_r1_reached"
    if _two_negative(eth_rates):
        return True, "eth_funding_2x_negative"
    if _two_negative(btc_rates):
        return True, "btc_funding_2x_negative"
    return False, ""


def evaluate_kill(btc_etf_flow_24h_usd: Optional[float],
                  kill_inflow: float = KILL_BTC_ETF_INFLOW_USD) -> tuple:
    """(kill, reason). A BTC mega-inflow is a BTC-dominance event — the
    short BTC leg is structurally wrong. None = no opinion, never kills.
    """
    if btc_etf_flow_24h_usd is not None and btc_etf_flow_24h_usd > kill_inflow:
        return True, "btc_etf_mega_inflow"
    return False, ""
