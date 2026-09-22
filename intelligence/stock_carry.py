"""Stock carry brains — ORCL basis-episode harvest + META regime shift
(register Stocks 1 & 2; B4 plane = tools/stock_basis_register.py).

Zero-I/O pure module. All inputs injected by the caller from the B4 plane
(basis z prints, funding-rate series, best-effort spread_bps) and from the
perp kline plane (4h volume/range — SoDEX klines own these symbols since the
2026-09-11 tradfi→SoDEX kline migration). None = dark, fail-closed.

STRATEGY A — ORCL basis-episode carry harvest. Entry requires ALL legs:
  1. 4h candle volume >= VOL_MULT x baseline      (injected current + baseline)
  2. 4h candle range  >= RANGE_MULT x baseline
  3. close STRICTLY > BREAKOUT_BARS-bar high of prior 4h closes
  4. basis episode active: trailing run of >=3 consecutive positive
     z-significant prints (B4 trailing_run semantics, see stamps)
  5. >=5 consecutive zero funding prints prior (trailing streak)
Exit (not kill): funding returns to 0 for one print, or flips negative.
Kill legs: basis z flips negative; spread widens > KILL_SPREAD_BP
(spread None = no opinion — B4's spread is a best-effort orderbook probe).

STRATEGY B — META regime shift. Entry: >=7 consecutive zero-funding prints,
then the FIRST positive print (rate > FUNDING_ZERO_EPS). The trigger must be
the NEWEST print in the injected series — a stale trigger never re-fires.
Exit: funding returns to zero for 2 consecutive prints.
Kill: regime normalizes (3 consecutive prints with |rate| >=
FUNDING_NORMAL_EPS) OR spread widens (same leg as A). None prints break
every run count (a dark print is unknown, never evidence).

POLEMARCH STAMPS (filed with the spec, bind its interpretation):
  - REBASED-SYNTHETIC SUBSTITUTION (B4 discovery): SoDEX equity perps are
    rebased synthetics — raw basis LEVELS carry extreme constant offsets
    (USTECH100 +398,133bps). The register's ">10bp basis" leg is therefore
    re-expressed in Z terms: basis episode = trailing same-sign run of
    z-significant prints per (symbol, session segment), exactly B4's
    episode semantics (EPISODE_MIN_PRINTS=3, Z_SIGNIFICANT=1.0). Raw bp
    levels are NEVER read by this module.
  - STRICTNESS SUBSTITUTION: B4's trailing_run significance is STRICT
    (z > 1.0, not >=) — tools/stock_basis_register.py:227. The register
    text said ">= 1.0 sustained". B4's serving wins: z == 1.0 exactly is
    NOT significant in this module.
  - 4h VOLUME/RANGE SUBSTITUTION: the B4 plane serves basis/z/spread and
    funding episodes only — it does NOT serve 4h candle volume or range
    baselines. Those legs are injected by the caller from the perp's own
    kline plane as (current, baseline) scalar pairs; this module computes
    the ratios. None or non-positive baseline = dark (no fabricated ratio).
  - FUNDING PLANE: B4's funding history is hourly records (funding/
    history.py, 168 max). "Zero" = |rate| < FUNDING_ZERO_EPS (1e-6).
    None entries BREAK streak counts — an absent print is dark, not zero.
  - SPREAD LEG: B4 serves spread_bps best-effort (orderbook depth probe;
    None when the book is thin/unreachable). None = NO OPINION: the kill
    leg abstains, it never blocks and never fires on a dark book.
  - FUNDING_NORMAL_EPS = 5e-5 CHOICE: "regime normalizes" needs a magnitude
    meaningfully above the zero band — 5e-5 (0.005%/h ≈ 43.8% annualized)
    is 50x the zero eps and matches B4's funding-episode significance
    (threshold 0.0 = any nonzero rate) seen through a noise filter. It is a
    declared knob, not a measured constant — n=1 history cannot calibrate it.
  - EDGE (ORCL): ~14-20bp per episode maker-in/maker-out; episodes are
    2-4 per quarter — n is TINY. SHADOW_ONLY=True; live requires an
    explicit Governor override.
  - HISTORY (META): +53bp/8h sustained 6 weeks in the source episode —
    ONE episode, n=1 anecdote. SHADOW_ONLY=True; live requires an
    explicit Governor override.
  - STANDING VERDICT — TSM and TSLA basis-arb are STRUCTURALLY DEAD
    (register Stocks 3 & 4): spread exceeds the basis / basis too small
    to clear round-trip costs. Verdict only, NO CODE. No future agent
    rebuilds them without new plane evidence reversing the spread/basis
    inequality.

STRATEGY C — CRYPTO FUNDING-FLIP SHORT (audit Strategy 15, Governor
2026-09-22: LIVE from day one). A funding extreme that COLLAPSES without
a price move is crowded longs quietly unwinding — the short rides the
deleveraging. Entry requires ALL legs on the hourly funding series
(funding/history.py plane, HOURLY decimal rates):
  1. funding 8h ago >= FF_FUNDING_HIGH (0.0008 = the extreme)
  2. funding now   <= FF_FUNDING_LOW  (0.0002 = collapsed)
  3. drop          >= FF_DROP_MIN     (0.0006 = a real unwind, not drift)
  4. price flat over the same 8h (|chg| <= FF_PRICE_FLAT_PCT) — the move
     has NOT happened yet; entering after the flush is chasing.
Series shorter than FF_MIN_RECORDS (9) or any None in the two compared
prints = dark, fail-closed. Entry LIMIT mark - 0.15% (the audit's
marketable limit for the short). Stop = recent high + 0.3%; TP1 -2%,
TP2 -4%, TP3 = 4h MA when it sits below entry (else None). Kill
(fail-open): funding re-extremes >= FF_FUNDING_HIGH — the crowding is
rebuilding, the unwind already happened. DOUBLE-SIGNAL (splice note):
same-episode dead-cat short (s4_cascade_fade S12) on the same symbol =
1.5x sizing per the audit; composition lives at the splice.

STRATEGY D — TRADFI LEAD ORACLES (audit Strategy 13, Governor 2026-09-22:
LIVE from day one). Three planes, one direction vote + modifiers:
  D1 COIN LEAD: COIN-USD deviating >= 0.5% from its own 30-min VWAP is
     the crypto-equity complex repricing — BTC follows in 15-30 min.
     Staleness > 600s = dark (a stale lead is no lead). Direction:
     above VWAP = long lead, below = short lead.
  D2 XAUT RISK ORACLE: gold outperforming BTC over 2h while the gold
     market is OPEN = risk_off warning (capital hiding). Market-closed
     (weekend from Friday 17:00 EST) = dark abstain — a closed plane
     never votes.
  D3 CL COHERENCE MODIFIER: crude as the risk barometer — |ret| >=
     CL_MATERIAL_PCT pays +/- 0.5 coherence WITH/AGAINST the trade
     direction; below materiality or a dark plane pays 0.0 exactly.
These are ORACLES, not standalone brackets: the splice composes D1's
direction with the standard candidate path; D2 cautions, D3 tilts.
"""

from __future__ import annotations

from typing import Optional, Sequence

# Knob constants (pin the spec; config overrides ride the splice)
FUNDING_ZERO_EPS = 1e-6
FUNDING_NORMAL_EPS = 5e-5
VOL_MULT = 3.0
RANGE_MULT = 3.0
BREAKOUT_BARS = 20
BASIS_Z_MIN = 1.0
BASIS_EPISODE_MIN_PRINTS = 3
ORCL_ZERO_STREAK_MIN = 5
META_ZERO_STREAK_MIN = 7
META_EXIT_ZERO_PRINTS = 2
META_NORMAL_RUN_PRINTS = 3
KILL_SPREAD_BP = 7.0

# Both strategies are evidence-thin by declaration (see stamps). A live
# splice MUST check this flag and stand down unless the Governor overrides.
SHADOW_ONLY = True


def funding_zero_streak(rates: Sequence[Optional[float]],
                        eps: float = FUNDING_ZERO_EPS) -> int:
    """Trailing consecutive prints with |r| < eps. A None print breaks the
    count (dark is not zero). Empty series -> 0."""
    streak = 0
    for r in reversed(list(rates)):
        if r is None or abs(r) >= eps:
            break
        streak += 1
    return streak


def first_positive_after_streak(rates: Sequence[Optional[float]],
                                min_streak: int = META_ZERO_STREAK_MIN,
                                eps: float = FUNDING_ZERO_EPS) -> Optional[int]:
    """Index of the FIRST print with r > eps whose immediately preceding
    min_streak prints are all present and zero (|r| < eps). A None anywhere
    inside that window interrupts — no fire. None when no trigger exists."""
    rs = list(rates)
    for i, r in enumerate(rs):
        if r is None or r <= eps:
            continue
        if i < min_streak:
            continue
        window = rs[i - min_streak:i]
        if all(w is not None and abs(w) < eps for w in window):
            return i
    return None


def basis_episode_run(zs: Sequence[Optional[float]],
                      z_min: float = BASIS_Z_MIN) -> int:
    """Trailing consecutive POSITIVE z-significant prints (z > z_min,
    strict — B4 trailing_run semantics, tools/stock_basis_register.py:227).
    A None or non-significant print breaks the run. The current bar must be
    part of the episode for the run to count."""
    run = 0
    for z in reversed(list(zs)):
        if z is None or z <= z_min:
            break
        run += 1
    return run


def evaluate_orcl_entry(vol_now: Optional[float],
                        vol_baseline: Optional[float],
                        range_now: Optional[float],
                        range_baseline: Optional[float],
                        closes: Sequence[Optional[float]],
                        basis_zs: Sequence[Optional[float]],
                        funding_rates: Sequence[Optional[float]],
                        vol_mult: float = VOL_MULT,
                        range_mult: float = RANGE_MULT,
                        breakout_bars: int = BREAKOUT_BARS,
                        z_min: float = BASIS_Z_MIN,
                        episode_min: int = BASIS_EPISODE_MIN_PRINTS,
                        zero_streak_min: int = ORCL_ZERO_STREAK_MIN) -> dict:
    """All-five-legs verdict. Every leg reports pass/fail/dark so the shadow
    record can segment near-misses from structural absences. ok requires
    ALL legs pass."""
    legs = {}

    vol_ratio = None
    if vol_now is None or vol_baseline is None or vol_baseline <= 0:
        legs["volume_surge"] = "dark"
    else:
        vol_ratio = vol_now / vol_baseline
        legs["volume_surge"] = "pass" if vol_ratio >= vol_mult else "fail"

    range_ratio = None
    if range_now is None or range_baseline is None or range_baseline <= 0:
        legs["range_surge"] = "dark"
    else:
        range_ratio = range_now / range_baseline
        legs["range_surge"] = "pass" if range_ratio >= range_mult else "fail"

    breakout_high = None
    cs = list(closes)
    if len(cs) < breakout_bars + 1 or cs[-1] is None:
        legs["breakout"] = "dark"
    else:
        prior = cs[-(breakout_bars + 1):-1]
        if any(c is None for c in prior):
            legs["breakout"] = "dark"
        else:
            breakout_high = max(prior)
            # STRICTLY greater — equal-to-high does NOT fire.
            legs["breakout"] = "pass" if cs[-1] > breakout_high else "fail"

    run = basis_episode_run(basis_zs, z_min)
    legs["basis_episode"] = "pass" if run >= episode_min else (
        "dark" if len(list(basis_zs)) == 0 or list(basis_zs)[-1] is None
        else "fail")

    streak = funding_zero_streak(funding_rates)
    legs["funding_zero_streak"] = ("dark" if len(list(funding_rates)) == 0
                                   or list(funding_rates)[-1] is None else
                                   ("pass" if streak >= zero_streak_min
                                    else "fail"))

    ok = all(v == "pass" for v in legs.values())
    return {"ok": ok, "legs": legs, "vol_ratio": vol_ratio,
            "range_ratio": range_ratio, "breakout_high": breakout_high,
            "basis_run": run, "funding_streak": streak,
            "shadow_only": SHADOW_ONLY}


def evaluate_meta_entry(rates: Sequence[Optional[float]],
                        min_streak: int = META_ZERO_STREAK_MIN,
                        eps: float = FUNDING_ZERO_EPS) -> dict:
    """Regime-shift trigger. ok ONLY when the first-positive-after-streak
    trigger is the NEWEST print in the series — the entry fires on the
    trigger bar and never re-fires on a stale series."""
    rs = list(rates)
    trigger = first_positive_after_streak(rs, min_streak, eps)
    streak_prior = 0
    if rs and rs[-1] is not None:
        # zeros immediately before the newest print
        for r in reversed(rs[:-1]):
            if r is None or abs(r) >= eps:
                break
            streak_prior += 1
    legs = {
        "zero_streak": ("dark" if not rs or rs[-1] is None else
                        ("pass" if streak_prior >= min_streak else "fail")),
        "first_positive": ("dark" if not rs or rs[-1] is None else
                           ("pass" if rs[-1] > eps else "fail")),
    }
    ok = trigger is not None and trigger == len(rs) - 1
    return {"ok": ok, "legs": legs, "trigger_index": trigger,
            "streak_prior": streak_prior, "shadow_only": SHADOW_ONLY}


def evaluate_exit(strategy: str, rates: Sequence[Optional[float]],
                  basis_z: Optional[float] = None,
                  spread_bp: Optional[float] = None,
                  eps: float = FUNDING_ZERO_EPS,
                  normal_eps: float = FUNDING_NORMAL_EPS,
                  kill_spread_bp: float = KILL_SPREAD_BP) -> tuple:
    """(should_exit, reason); reason="" when flat. Kills are checked before
    exits. spread_bp None = no opinion (abstain). None funding prints give
    no opinion on that leg (dark is not evidence either way)."""
    rs = list(rates)

    # Kill leg shared by both strategies: spread widening. None = no opinion.
    if spread_bp is not None and spread_bp > kill_spread_bp:
        return True, "spread_widened"

    if strategy == "orcl":
        if basis_z is not None and basis_z < 0:
            return True, "basis_z_flipped_negative"
        if rs and rs[-1] is not None:
            if rs[-1] < -eps:
                return True, "funding_flipped_negative"
            if abs(rs[-1]) < eps:
                return True, "funding_returned_zero"
        return False, ""

    if strategy == "meta":
        tail = rs[-META_NORMAL_RUN_PRINTS:]
        if (len(tail) == META_NORMAL_RUN_PRINTS
                and all(r is not None and abs(r) >= normal_eps for r in tail)):
            return True, "regime_normalized"
        tail2 = rs[-META_EXIT_ZERO_PRINTS:]
        if (len(tail2) == META_EXIT_ZERO_PRINTS
                and all(r is not None and abs(r) < eps for r in tail2)):
            return True, "funding_zero_2x"
        return False, ""

    raise ValueError(f"unknown strategy {strategy!r}")


# ═══════════════════════════════════════════════════════════════════════
# STRATEGY C — CRYPTO FUNDING-FLIP SHORT (audit Strategy 15). LIVE from
# birth per Governor 2026-09-22 — FUNDING_FLIP_ENABLED is the kill switch
# the main.py splice reads; False = the strategy never fires.
# ═══════════════════════════════════════════════════════════════════════

FF_FUNDING_HIGH = 0.0008        # the extreme 8h ago (hourly decimal rate)
FF_FUNDING_LOW = 0.0002         # collapsed to at-or-below this now
FF_DROP_MIN = 0.0006            # minimum unwind (8h-ago minus now)
FF_PRICE_FLAT_PCT = 1.0         # |8h price change| must be <= 1%
FF_MIN_RECORDS = 9              # rates[0] is the 8h-ago print
FF_LIMIT_OFFSET_PCT = 0.15      # entry LIMIT = mark - 0.15% (audit spec)
FF_STOP_BUFFER_PCT = 0.3        # stop = recent high + 0.3%
FF_TP1_PCT = 2.0
FF_TP2_PCT = 4.0

FUNDING_FLIP_ENABLED = True     # Governor 2026-09-22: live from day one


def funding_flip_verdict(rates: Sequence[Optional[float]],
                         price_chg_8h_pct: Optional[float],
                         enabled: bool = FUNDING_FLIP_ENABLED) -> dict:
    """Four-leg SHORT verdict on the hourly funding series. Legs:
    high_8h_ago / now_low / drop / price_flat. None prints in the two
    compared slots or a short series = dark, fail-closed."""
    legs = {}
    rs = list(rates or [])
    ago = rs[0] if len(rs) >= FF_MIN_RECORDS else None
    now = rs[-1] if len(rs) >= FF_MIN_RECORDS else None

    if ago is None:
        legs["high_8h_ago"] = "dark"
    else:
        legs["high_8h_ago"] = "pass" if ago >= FF_FUNDING_HIGH else "fail"

    if now is None:
        legs["now_low"] = "dark"
    else:
        legs["now_low"] = "pass" if now <= FF_FUNDING_LOW else "fail"

    drop = None
    if ago is None or now is None:
        legs["drop"] = "dark"
    else:
        drop = ago - now
        legs["drop"] = "pass" if drop >= FF_DROP_MIN else "fail"

    if price_chg_8h_pct is None:
        legs["price_flat"] = "dark"
    else:
        legs["price_flat"] = ("pass" if abs(price_chg_8h_pct) <= FF_PRICE_FLAT_PCT
                              else "fail")

    ok = enabled and all(v == "pass" for v in legs.values())
    return {"ok": ok, "legs": legs, "direction": "short",
            "funding_8h_ago": ago, "funding_now": now, "drop": drop,
            "price_chg_8h_pct": price_chg_8h_pct,
            "enabled": enabled, "shadow_only": False}


def funding_flip_limit(mark: float) -> float:
    """The audit's entry: LIMIT at mark - 0.15% (a marketable limit for
    the short — crosses the spread by 15bp rather than resting)."""
    return mark * (1.0 - FF_LIMIT_OFFSET_PCT / 100.0)


def funding_flip_bracket(entry: float, recent_high: float,
                         ma4h: Optional[float] = None) -> tuple:
    """(stop, tp1, tp2, tp3_or_None) for the SHORT. TP3 is the 4h MA —
    only when it sits below entry (a target, not a ceiling)."""
    stop = recent_high * (1.0 + FF_STOP_BUFFER_PCT / 100.0)
    tp1 = entry * (1.0 - FF_TP1_PCT / 100.0)
    tp2 = entry * (1.0 - FF_TP2_PCT / 100.0)
    tp3 = ma4h if (ma4h is not None and 0 < ma4h < entry) else None
    return stop, tp1, tp2, tp3


def funding_flip_kill(latest_rate: Optional[float]) -> tuple:
    """(kill, reason). Funding re-extreming = the crowding is rebuilding;
    the unwind already paid someone else. Fail OPEN on a dark print."""
    if latest_rate is not None and latest_rate >= FF_FUNDING_HIGH:
        return True, "funding_re_extreme"
    return False, ""


# ═══════════════════════════════════════════════════════════════════════
# STRATEGY D — TRADFI LEAD ORACLES (audit Strategy 13). LIVE from birth
# per Governor 2026-09-22 — TRADFI_LEAD_ENABLED is the kill switch the
# main.py splice reads; False = the oracles never vote.
# ═══════════════════════════════════════════════════════════════════════

COIN_DEV_PCT = 0.5              # |COIN vs its 30m VWAP| >= 0.5% = a lead
LEAD_STALE_MAX_S = 600          # staleness guard — a stale lead is no lead
XAUT_MATERIAL_OUTPERFORM_PCT = 0.0  # gold > BTC over 2h = risk_off warning
CL_COHERENCE_BONUS = 0.5        # additive coherence tilt, direction-signed
CL_MATERIAL_PCT = 0.3           # |CL ret| below this pays exactly 0.0

TRADFI_LEAD_ENABLED = True      # Governor 2026-09-22: live from day one


def coin_lead_verdict(coin_price: Optional[float],
                      coin_vwap_30m: Optional[float],
                      coin_age_s: Optional[float],
                      enabled: bool = TRADFI_LEAD_ENABLED) -> dict:
    """COIN's deviation from its own 30-min VWAP as the BTC lead. Legs:
    freshness (<= 600s), deviation (>= 0.5%). direction = long when COIN
    trades above its anchor, short below. Dark when any plane is missing
    — oracles abstain, they never guess."""
    legs = {}
    if coin_age_s is None:
        legs["freshness"] = "dark"
    else:
        legs["freshness"] = ("pass" if 0 <= coin_age_s <= LEAD_STALE_MAX_S
                             else "fail")

    dev_pct = None
    if coin_price is None or coin_vwap_30m is None or coin_vwap_30m <= 0:
        legs["deviation"] = "dark"
    else:
        dev_pct = (coin_price - coin_vwap_30m) / coin_vwap_30m * 100.0
        legs["deviation"] = ("pass" if abs(dev_pct) >= COIN_DEV_PCT else "fail")

    ok = enabled and all(v == "pass" for v in legs.values())
    direction = None
    if dev_pct is not None and abs(dev_pct) >= COIN_DEV_PCT:
        direction = "long" if dev_pct > 0 else "short"
    return {"ok": ok, "legs": legs, "direction": direction,
            "dev_pct": dev_pct, "coin_age_s": coin_age_s,
            "enabled": enabled, "shadow_only": False}


def xaut_risk_oracle(xaut_ret_2h_pct: Optional[float],
                     btc_ret_2h_pct: Optional[float],
                     xaut_market_open: Optional[bool]) -> dict:
    """Gold outperforming BTC over 2h = risk_off warning (capital hiding
    in the metal). A CLOSED gold market (None/False) = dark abstain —
    weekend prints never vote."""
    if not xaut_market_open:
        return {"risk_off": None, "legs": {"market_open": "dark"},
                "xaut_ret_2h_pct": xaut_ret_2h_pct,
                "btc_ret_2h_pct": btc_ret_2h_pct}
    if xaut_ret_2h_pct is None or btc_ret_2h_pct is None:
        return {"risk_off": None, "legs": {"market_open": "pass",
                                           "outperformance": "dark"},
                "xaut_ret_2h_pct": xaut_ret_2h_pct,
                "btc_ret_2h_pct": btc_ret_2h_pct}
    outperform = xaut_ret_2h_pct - btc_ret_2h_pct
    risk_off = outperform > XAUT_MATERIAL_OUTPERFORM_PCT
    return {"risk_off": risk_off,
            "legs": {"market_open": "pass",
                     "outperformance": "pass" if risk_off else "fail"},
            "outperform_pct": outperform,
            "xaut_ret_2h_pct": xaut_ret_2h_pct,
            "btc_ret_2h_pct": btc_ret_2h_pct}


def cl_coherence_modifier(cl_ret_pct: Optional[float],
                          side: Optional[str]) -> float:
    """Crude as the risk barometer: a material CL move WITH the trade
    direction pays +CL_COHERENCE_BONUS, AGAINST pays -bonus, a dark or
    immaterial plane pays exactly 0.0 (never a fabricated tilt)."""
    if cl_ret_pct is None or side not in ("long", "short"):
        return 0.0
    if abs(cl_ret_pct) < CL_MATERIAL_PCT:
        return 0.0
    agrees = (cl_ret_pct > 0) == (side == "long")
    return CL_COHERENCE_BONUS if agrees else -CL_COHERENCE_BONUS
