"""Scalps 2+3 — shared 5m momentum-breakout brain, ETH and SOL configs.

Zero-I/O brain. Momentum long through a resistance ZONE:
    ZONE = max(BB(20,2) upper, session_VWAP + 1 * VWAP-band-sigma)
computed from the injected 5m series — NEVER hardcoded prices. The
register's $2,509 (ETH) / $101.90 (SOL) are DATED SNAPSHOTS, not inputs.

Entry requires ALL legs on 5m bars (per symbol config):
  1. close > zone AND prior close <= zone (the cross)
  2. close within MAX_WITHIN_BP of the zone (ETH 50bp, SOL 40bp — no chasing)
  3. volume >= VOL_MULT * MA(20) (ETH 2.0, SOL 1.8)
  4. funding >= 0 (Optional, None = dark, fail-closed)
  5. iv_rank < 65 — NO options plane exists: Optional[float], None = dark
     leg RECORDED, but REQUIRE_OPTIONS_LEGS=False so dark does NOT block
     (segmentation only). A PRESENT iv_rank >= 65 fails and blocks.
  6. SOL ONLY: SOL OI 24h change > -1.5% (Optional; None = dark,
     fail-closed; the ETH config leaves this leg ABSENT)
  7. fee-ratio leg: (spread_bp + taker_RT_bp) / farthest_TP <= 0.30
     (taker RT; farthest TP = tp2 when set else tp1)

Kill legs (register): volume < 1.5x MA on the entry bar (re-check),
funding flip negative, price re-enters the zone within 2 bars; SOL adds
OI 24h < -2% (second-day unwind) and funding < -5bp.

POLEMARCH STAMPS (filed with the spec, bind its interpretation):
  - The zone is computed from bars EXCLUDING the signal bar (bars[:-1]) —
    the signal bar is never part of its own breakout level (no look-ahead).
  - VWAP-band-sigma is the VOLUME-WEIGHTED stdev of typical price
    ((h+l+c)/3) around the session VWAP over the injected series.
  - BB math reuses sc1_eth_bb_bounce.bb_bands (population stdev) — one
    variance convention per department.
  - All funding values injected into this module are in BP. The entry leg
    is sign-only (>= 0); kill thresholds (-5bp SOL) compare in bp.
  - Kill-reason precedence: SOL deep-negative funding (-5bp) outranks the
    generic flip for telemetry; both kill. Checks fire: volume re-check,
    funding, zone re-entry, SOL OI second-day unwind.
  - SOL entry OI > -1.5 vs kill OI < -2.0 leaves a deliberate [-2.0, -1.5]
    hysteresis band — no entry/kill flap on a hovering OI print.
  - The register-quoted taker breakevens (ETH 44.9% / SOL 38.5%) are
    context, NOT reproduced arithmetic; the binding gate is fee_ratio
    <= 0.30 with spread included. spread_bp rides fee_state["spread_bp"],
    default 0.0 when absent (the fee leg never invents a spread).
  - Kills fail OPEN on dark planes; entries fail CLOSED.
  - TIME_STOP_MIN is a config constant only; the clock rides the runner.
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence, Tuple

from intelligence.min_viable_tp import FEE_RATIO_RETIRE
from intelligence.sc1_eth_bb_bounce import bb_bands, fee_ratio, vol_ma_ok

# No options plane exists at SoDEX — the IV leg records dark but never
# blocks (Polemarch stamp; a PRESENT iv_rank >= IV_RANK_MAX still fails).
REQUIRE_OPTIONS_LEGS = False
IV_RANK_MAX = 65.0
FEE_RATIO_MAX = FEE_RATIO_RETIRE  # 0.30 — Governor structural fee gate
VOL_MA_LEN = 20
ZONE_REENTRY_BARS = 2

ETH_CONFIG = {
    "symbol": "ETH",
    "vol_mult": 2.0,
    "within_bp": 50.0,
    "tp1_bp": 37.0,
    "tp2_bp": None,
    "stop_bp": 24.0,
    "time_stop_min": 30,
    "oi_leg": False,
}

SOL_CONFIG = {
    "symbol": "SOL",
    "vol_mult": 1.8,
    "within_bp": 40.0,
    "tp1_bp": 42.0,
    "tp2_bp": 62.0,
    "stop_bp": 21.0,
    "time_stop_min": 25,
    "oi_leg": True,
    "oi_min": -1.5,
    "kill_oi_chg": -2.0,
    "kill_funding_bp": -5.0,
}


def session_vwap(bars: Sequence[tuple]) -> Optional[float]:
    """Typical-price VWAP over (open, high, low, close, vol) bars.

    None on empty input or zero total volume (fail-closed).
    """
    if not bars:
        return None
    num = den = 0.0
    for b in bars:
        tp = (b[1] + b[2] + b[3]) / 3.0
        num += tp * b[4]
        den += b[4]
    if den <= 0:
        return None
    return num / den


def vwap_band_sigma(bars: Sequence[tuple]) -> Optional[float]:
    """Volume-weighted stdev of typical price around the session VWAP."""
    v = session_vwap(bars)
    if v is None:
        return None
    num = den = 0.0
    for b in bars:
        tp = (b[1] + b[2] + b[3]) / 3.0
        num += b[4] * (tp - v) ** 2
        den += b[4]
    return (num / den) ** 0.5


def zone_price(closes: Sequence[float], highs: Sequence[float],
               lows: Sequence[float], vols: Sequence[float]) -> Optional[float]:
    """max(BB(20,2) upper, session VWAP + 1*VWAP-band-sigma).

    None on insufficient/mismatched series — a fabricated zone is a
    fabricated breakout level (fail-closed).
    """
    if not (len(closes) == len(highs) == len(lows) == len(vols)):
        return None
    bands = bb_bands(closes)
    if bands is None:
        return None
    bars = [(0.0, highs[i], lows[i], closes[i], vols[i])
            for i in range(len(closes))]
    v = session_vwap(bars)
    s = vwap_band_sigma(bars)
    if v is None or s is None:
        return None
    return max(bands[2], v + s)


def evaluate_entry(config: Mapping, bars: Sequence[tuple],
                   funding: Optional[float], iv_rank: Optional[float],
                   oi_chg_24h: Optional[float],
                   fee_state: Optional[Mapping]) -> dict:
    """All-legs verdict. bars = (o,h,l,c,v) tuples, LAST = signal bar.

    The zone is built from bars[:-1] (no look-ahead). ok = every leg pass,
    EXCEPT the iv leg which may also be dark (REQUIRE_OPTIONS_LEGS=False).
    """
    legs = {}
    zone = None
    if len(bars) >= 2:
        hist = bars[:-1]
        zone = zone_price([b[3] for b in hist], [b[1] for b in hist],
                          [b[2] for b in hist], [b[4] for b in hist])
    close = bars[-1][3] if bars else None
    prior_close = bars[-2][3] if len(bars) >= 2 else None

    if zone is None or close is None or prior_close is None:
        legs["zone_cross"] = "dark"
    else:
        legs["zone_cross"] = ("pass" if (close > zone and prior_close <= zone)
                              else "fail")

    within = None
    if zone is None or close is None or zone <= 0:
        legs["within_band"] = "dark"
    else:
        within = (close - zone) / zone * 1e4
        legs["within_band"] = ("pass" if 0.0 <= within <= config["within_bp"]
                               else "fail")

    vol_ok = vol_ma_ok([b[4] for b in bars], config["vol_mult"], VOL_MA_LEN)
    legs["volume"] = ("dark" if vol_ok is None
                      else ("pass" if vol_ok else "fail"))
    legs["funding"] = ("dark" if funding is None
                       else ("pass" if funding >= 0 else "fail"))

    if iv_rank is None:
        legs["iv_rank"] = "dark"  # recorded, non-blocking (stamp)
    else:
        legs["iv_rank"] = "pass" if iv_rank < IV_RANK_MAX else "fail"

    if config.get("oi_leg"):
        legs["oi_24h"] = ("dark" if oi_chg_24h is None
                          else ("pass" if oi_chg_24h > config["oi_min"]
                                else "fail"))

    gross_tp = config["tp2_bp"] if config.get("tp2_bp") else config["tp1_bp"]
    spread = (fee_state.get("spread_bp") or 0.0) if fee_state is not None else None
    fr = fee_ratio(spread, fee_state, gross_tp)
    if fr is None:
        legs["fee_ratio"] = "dark"
    else:
        legs["fee_ratio"] = "pass" if fr <= FEE_RATIO_MAX else "fail"

    ok = all(v == "pass" for k, v in legs.items() if k != "iv_rank") \
        and legs["iv_rank"] in ("pass", "dark")
    return {"ok": ok, "legs": legs, "zone": zone, "within_bp": within,
            "fee_ratio": fr, "close": close}


def evaluate_kill(config: Mapping, zone: Optional[float] = None,
                  close_now: Optional[float] = None,
                  bars_held: Optional[int] = None,
                  vol_ok_15: Optional[bool] = None,
                  funding_bp: Optional[float] = None,
                  oi_chg_24h: Optional[float] = None) -> tuple:
    """(kill, reason). reason="" when no kill. Kills fail OPEN on dark
    planes. Precedence (Polemarch stamp): volume re-check, funding (SOL
    deep-negative outranks generic flip), zone re-entry, SOL OI unwind."""
    if vol_ok_15 is not None and not vol_ok_15:
        return True, "entry_volume_recheck_fail"
    if funding_bp is not None:
        if (config.get("oi_leg")
                and funding_bp < config.get("kill_funding_bp", -5.0)):
            return True, "funding_deep_negative"
        if funding_bp < 0:
            return True, "funding_flip_negative"
    if (zone is not None and close_now is not None
            and bars_held is not None
            and bars_held <= ZONE_REENTRY_BARS and close_now <= zone):
        return True, "zone_reentry_within_2_bars"
    if (config.get("oi_leg") and oi_chg_24h is not None
            and oi_chg_24h < config.get("kill_oi_chg", -2.0)):
        return True, "oi_24h_unwind_day2"
    return False, ""


def bracket(config: Mapping, entry: float) -> Tuple[float, float, Optional[float]]:
    """(stop, tp1, tp2_or_None) for a LONG scalp, bp-of-entry -> price."""
    stop = entry * (1.0 - config["stop_bp"] / 1e4)
    tp1 = entry * (1.0 + config["tp1_bp"] / 1e4)
    tp2 = (entry * (1.0 + config["tp2_bp"] / 1e4)
           if config.get("tp2_bp") else None)
    return stop, tp1, tp2
