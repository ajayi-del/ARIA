"""
intelligence/axiom_stack.py — The Governor's axiom stack (pure brain, zero-I/O).

2026-09-26 (Governor directive): encode the axiom-stack trading doctrine as
the system's new decision tree — tier classification from whale evidence,
the Kant checklist, the Nietzsche ladder, Kelly-by-tier sizing, the regime
gate, the propagation map, and exit-side pure verdicts. Department shape
(docs/DEPARTMENT_TEMPLATE.md): all market reads arrive as arguments;
decisions leave as frozen verdict dataclasses. main.py owns the I/O; the
splice is a later phase.

Doctrine spine (verbatim Governor semantics):
  1. TIER — a signal's tier comes from whale evidence, never price action.
     Two evidence modes: PLANE-NATIVE (percentile of the Bybit account-ratio
     plane, compressed distribution) and ABSOLUTE (externally-pasted scan
     ladder values). Short-skew evidence (<40 pct / <1.3 ls) is a REJECT
     regardless of price action. Overcrowded funding downgrades one tier.
  2. KANT — structural soundness: whale floor, RR off the ACTUAL clamped
     stop (the bracket the exchange will really run, not the intended one),
     narrative clock, coherence floor, restricted symbols, funding.
  3. NIETZSCHE — conviction ladder by tier (tier1 is napoleonic).
  4. KELLY-BY-TIER — full Kelly shrunk toward a prior (k=20, the skeptic
     doctrine: small n must never mint insane fractions), scaled by the
     tier fraction, hard-ceilinged at 30% margin.
  5. REGIME — six-state gate with a strict priority order; DEAD_ZONE and
     POST_CASCADE are defensive and outrank every offensive state.
  6. PROPAGATION — leader→laggard family map; the target must lag and BOTH
     legs must carry valid whale evidence (the EIGEN trap: a +9% sympathy
     move on bearish whale evidence is a rejection, not an entry).
  7. EXITS — pure verdicts only (min-hold, narrative clock, ROE ratchet
     rungs, pyramid add); execution wiring is a later phase.

Fail-closed law: nothing in this module raises on bad input — every
function fails toward REJECT / abstain / 0 with a named reason.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple


# ── Kill switch ───────────────────────────────────────────────────────────────

def axiom_stack_enabled() -> bool:
    """Module-level kill switch (env AXIOM_STACK_ENABLED, default true).
    False = the splice phase must reproduce the pre-stack system bit-for-bit."""
    return os.environ.get("AXIOM_STACK_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on")


# ── Shared helpers ────────────────────────────────────────────────────────────

def _f(x) -> Optional[float]:
    """Coerce to float; None on any garbage (fail-closed idiom)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def base_name(symbol: str) -> str:
    """'BCH-USD' -> 'BCH' (symbols arrive venue-suffixed)."""
    try:
        s = str(symbol)
    except Exception:
        return ""
    if not s or s == "None":
        return ""
    return s.split("-")[0].strip().upper()


# ── 1. Tier classifier ────────────────────────────────────────────────────────

MODE_PLANE = "plane"        # whale_ratio_percentile (0-100, compressed dist)
MODE_ABSOLUTE = "absolute"  # externally-pasted whale_ls / top_trader ladder
MODE_PROXY = "proxy"        # WhaleProxy composite score (0-1, funding+OI+spike+ratio)

FUNDING_OVERCROWDED_MULT = 2.5


def _funding_overcrowded(funding_current, funding_avg) -> bool:
    """Overcrowded funding: current > avg × 2.5. Missing/non-positive avg =
    abstain (no downgrade — never invent a funding state)."""
    fc, fa = _f(funding_current), _f(funding_avg)
    if fc is None or fa is None or fa <= 0:
        return False
    return fc > fa * FUNDING_OVERCROWDED_MULT


def _absolute_ladder_tier(ls: float) -> int:
    """The absolute whale ladder: T1 ≥3.5, T2 ≥2.0, T3 ≥1.5, <1.3 REJECT,
    [1.3, 1.5) = no tier (0)."""
    if ls >= 3.5:
        return 1
    if ls >= 2.0:
        return 2
    if ls >= 1.5:
        return 3
    return 0


def classify_tier(mode: str, whale_pct=None, whale_ls=None, top_trader=None,
                  funding_current=None, funding_avg=None) -> int:
    """Signal tier from whale evidence -> 0|1|2|3 (0 = reject).

    PLANE-NATIVE (primary): whale_pct percentile of the account-ratio plane.
      T1 ≥90, T2 ≥75, T3 ≥60, <40 (short skew) = REJECT regardless of price
      action; [40,60) = no tier. Overcrowded funding (current > avg×2.5)
      downgrades one tier (plane mode only — the absolute ladder is a pasted
      compatibility path and carries no funding axis).
    ABSOLUTE (compatibility): whale_ls ladder T1 ≥3.5 / T2 ≥2.0 / T3 ≥1.5 /
      <1.3 REJECT. Optional second axis top_trader: when present it is
      laddered identically and the tier is the WEAKER of the two; top_trader
      <1.3 alone forces REJECT.
    PROXY (WhaleProxy composite, 2026-09-26 Governor doctrine): whale_pct
      carries the 0-1 composite score (funding discipline + OI-30d growth +
      spike-vs-sustained + account ratio — the engineered replacement for
      the unavailable external whale_ls). T1 ≥0.80 / T2 ≥0.60 / T3 ≥0.40 /
      <0.40 REJECT (proxy band 4 = watch-only, band 5 = reject — both are
      no-entry here). No funding downgrade: funding is already INSIDE the
      composite.
    """
    if mode == MODE_PROXY:
        score = _f(whale_pct)
        if score is None or score < 0.40:
            return 0
        if score >= 0.80:
            return 1
        if score >= 0.60:
            return 2
        return 3

    if mode == MODE_PLANE:
        pct = _f(whale_pct)
        if pct is None:
            return 0
        if pct < 40.0:
            return 0                    # short skew — hard reject
        if pct >= 90.0:
            tier = 1
        elif pct >= 75.0:
            tier = 2
        elif pct >= 60.0:
            tier = 3
        else:
            return 0                    # 40-59: no conviction
        if tier > 0 and _funding_overcrowded(funding_current, funding_avg):
            tier = tier + 1 if tier < 3 else 0  # downgrade one tier (1→2→3→0)
        return tier

    if mode == MODE_ABSOLUTE:
        ls = _f(whale_ls)
        if ls is None or ls < 1.3:
            return 0
        tier = _absolute_ladder_tier(ls)
        tt = _f(top_trader)
        if tt is not None:
            if tt < 1.3:
                return 0                # second axis alone forces reject
            tier = max(tier, _absolute_ladder_tier(tt))  # weaker axis wins
        return tier

    return 0                            # unknown mode — fail closed


# ── 2. Kant checklist ─────────────────────────────────────────────────────────

RESTRICTED = frozenset({"BCH", "XLM", "INJ", "BONK", "VIRTUAL"})
KANT_RR_MIN = 1.8
KANT_COHERENCE_MIN = 5.3
KANT_NARRATIVE_MAX_DAY = 4


def rr_from_bracket(entry, actual_stop, tp1) -> Optional[float]:
    """R:R computed from the ACTUAL clamped stop — the bracket the exchange
    will really run. reward = |tp1 − entry|, risk = |entry − actual_stop|.
    None on degenerate geometry (risk ≤ 0, bad inputs) — fail-closed."""
    e, s, t = _f(entry), _f(actual_stop), _f(tp1)
    if e is None or s is None or t is None or e <= 0:
        return None
    risk = abs(e - s)
    if risk <= 0:
        return None
    return abs(t - e) / risk


@dataclass(frozen=True)
class KantVerdict:
    approved: bool
    failures: Tuple[str, ...] = ()
    tier: int = 0


def kant_check(tier, rr_actual, narrative_day, coherence, symbol,
               funding_current=None, funding_avg=None) -> KantVerdict:
    """The Kant checklist — ALL legs must pass. Every failure is named."""
    failures: List[str] = []
    t = tier if isinstance(tier, int) and tier in (0, 1, 2, 3) else 0
    if t <= 0:
        failures.append("whale_floor")
    rr = _f(rr_actual)
    if rr is None or rr < KANT_RR_MIN:
        failures.append("rr_below_min")
    nd = _f(narrative_day)
    if nd is None or nd > KANT_NARRATIVE_MAX_DAY:
        failures.append("narrative_expired")
    coh = _f(coherence)
    if coh is None or coh < KANT_COHERENCE_MIN:
        failures.append("coherence_floor")
    if base_name(symbol) in RESTRICTED:
        failures.append("restricted_symbol")
    if _funding_overcrowded(funding_current, funding_avg):
        failures.append("funding_overcrowded")
    return KantVerdict(approved=not failures, failures=tuple(failures), tier=t)


# ── 3. Nietzsche ladder ───────────────────────────────────────────────────────

NIETZSCHE_LADDER = {1: 2.5, 2: 1.75, 3: 1.25, 0: 0.0}   # tier1 = napoleonic


def nietzsche_multiplier(tier) -> float:
    """Conviction multiplier by tier. Unknown tier = 0.0 (abstain)."""
    try:
        return NIETZSCHE_LADDER.get(int(tier), 0.0)
    except (TypeError, ValueError):
        return 0.0


# ── 4. Kelly-by-tier sizing (Governor full-send) ─────────────────────────────

KELLY_HARD_CEIL = 0.30
TIER_KELLY_FRACTION = {1: 0.5, 2: 1.0 / 3.0, 3: 0.25}
KELLY_PRIOR_WR = 0.55
KELLY_PRIOR_B = 2.0
KELLY_SHRINK_K = 20


def full_kelly(p, b) -> Optional[float]:
    """f* = (p·b − (1−p)) / b. None on degenerate inputs (b ≤ 0, bad p)."""
    p, b = _f(p), _f(b)
    if p is None or b is None or b <= 0:
        return None
    return (p * b - (1.0 - p)) / b


def shrunk_kelly(wins, losses, avg_win, avg_loss,
                 prior_wr: float = KELLY_PRIOR_WR,
                 prior_b: float = KELLY_PRIOR_B,
                 k: int = KELLY_SHRINK_K) -> Optional[float]:
    """Empirical-Bayes shrunk Kelly — small n can never mint insane fractions
    (same k=20 doctrine as the skeptic base rate). Both p and b shrink toward
    the prior: p̂ = (w + k·p₀)/(n + k), b̂ = (n·b_emp + k·b₀)/(n + k).
    n = 0 → the prior Kelly. Unpayable loss leg (no losses / avg_loss ≤ 0)
    → empirical b falls back to the prior b (abstain from fantasy payoff)."""
    w, l = _f(wins), _f(losses)
    if w is None or l is None or w < 0 or l < 0:
        return None
    n = w + l
    k = max(0.0, _f(k) or 0.0)
    if n <= 0:
        return full_kelly(prior_wr, prior_b)
    p_emp = w / n
    aw, al = _f(avg_win), _f(avg_loss)
    if aw is None or al is None or aw <= 0 or al <= 0:
        b_emp = prior_b
    else:
        b_emp = aw / al
    p_shrunk = (w + k * prior_wr) / (n + k)
    b_shrunk = (n * b_emp + k * prior_b) / (n + k)
    return full_kelly(p_shrunk, b_shrunk)


def margin_fraction(tier, wins, losses, avg_win, avg_loss) -> float:
    """Margin fraction = min(shrunk_kelly × tier_kelly_fraction, HARD_CEIL).
    Tier 0 / unknown / degenerate → 0.0 (no margin for unproven evidence)."""
    frac = TIER_KELLY_FRACTION.get(tier if isinstance(tier, int) else -1)
    if frac is None:
        return 0.0
    k = shrunk_kelly(wins, losses, avg_win, avg_loss)
    if k is None or k <= 0:
        return 0.0
    return min(k * frac, KELLY_HARD_CEIL)


# ── 5. Regime gate ────────────────────────────────────────────────────────────

REGIME_COIL_COMPRESS = "COIL_COMPRESS"
REGIME_COIL_BREAK_UP = "COIL_BREAK_UP"
REGIME_NARRATIVE_ROTATION = "NARRATIVE_ROTATION"
REGIME_POST_CASCADE = "POST_CASCADE"
REGIME_DEAD_ZONE = "DEAD_ZONE"
REGIME_NORMAL = "NORMAL"

DEAD_ZONE_HOURS_UTC = frozenset({23, 0, 1})     # 23:00–02:00 UTC
POST_CASCADE_BTC_DROP_2H = -2.0
POST_CASCADE_OI_24H = -2.0
COIL_DAY_MOVE_ABS = 1.5
COIL_FUNDING_ABS_BPS = 3.0
BREAK_UP_FUNDING_BPS = 3.0
BREAK_UP_OI_4H = 1.0
NARRATIVE_BTC_DAY_ABS = 1.0
NARRATIVE_SYMBOL_DAY = 8.0
NARRATIVE_WHALE_PCT = 60.0
NARRATIVE_COHERENCE = 5.5
CASCADE_COOLDOWN_S = 1800.0


def _utc_hour(now_utc) -> Optional[int]:
    """Accept a datetime (uses .hour) or epoch seconds."""
    h = getattr(now_utc, "hour", None)
    if isinstance(h, int):
        return h
    ts = _f(now_utc)
    if ts is None:
        return None
    try:
        return int(time.gmtime(ts).tm_hour)
    except (OverflowError, OSError, ValueError):
        return None


def classify_regime(now_utc=None, btc_day_moves_3d=None, oi_delta_24h=None,
                    funding_bps=None, btc_bollinger_width_pctile=None,
                    btc_drop_2h_pct=None, btc_1h_close=None,
                    btc_1d_bollinger_upper=None, oi_delta_4h=None,
                    symbol_day_move_pct=None, symbol_whale_pct=None,
                    coherence=None) -> str:
    """Six-state regime gate. Strict priority (defense outranks offense):
      DEAD_ZONE > POST_CASCADE > COIL_BREAK_UP > NARRATIVE_ROTATION
        > COIL_COMPRESS > NORMAL.
    Any missing input fails that state's condition — never manufactured.
    btc_bollinger_width_pctile rides the signature for the splice plane
    (compression telemetry); the classification rules are the Governor's
    verbatim set and do not consume it."""
    hour = _utc_hour(now_utc) if now_utc is not None else None
    if hour is not None and hour in DEAD_ZONE_HOURS_UTC:
        return REGIME_DEAD_ZONE

    drop, oi24 = _f(btc_drop_2h_pct), _f(oi_delta_24h)
    if (drop is not None and oi24 is not None
            and drop <= POST_CASCADE_BTC_DROP_2H
            and oi24 <= POST_CASCADE_OI_24H):
        return REGIME_POST_CASCADE

    close, upper, fr, oi4 = (_f(btc_1h_close), _f(btc_1d_bollinger_upper),
                             _f(funding_bps), _f(oi_delta_4h))
    if (close is not None and upper is not None and fr is not None
            and oi4 is not None and close > upper
            and fr > BREAK_UP_FUNDING_BPS and oi4 > BREAK_UP_OI_4H):
        return REGIME_COIL_BREAK_UP

    moves = None
    if btc_day_moves_3d is not None:
        try:
            moves = [_f(x) for x in btc_day_moves_3d]
        except TypeError:
            moves = None
    btc_day = moves[-1] if moves else None
    sdm, swp, coh = _f(symbol_day_move_pct), _f(symbol_whale_pct), _f(coherence)
    if (btc_day is not None and sdm is not None and swp is not None
            and coh is not None and abs(btc_day) < NARRATIVE_BTC_DAY_ABS
            and sdm > NARRATIVE_SYMBOL_DAY and swp >= NARRATIVE_WHALE_PCT
            and coh >= NARRATIVE_COHERENCE):
        return REGIME_NARRATIVE_ROTATION

    if (moves and len(moves) >= 3 and all(m is not None for m in moves)
            and all(abs(m) < COIL_DAY_MOVE_ABS for m in moves)
            and fr is not None and abs(fr) < COIL_FUNDING_ABS_BPS
            and oi24 is not None and oi24 < 0):
        return REGIME_COIL_COMPRESS

    return REGIME_NORMAL


def cascade_cooldown_active(cascade_ts, now_ts,
                            cooldown_s: float = CASCADE_COOLDOWN_S) -> bool:
    """30-min new-entry cooldown from cascade detection. Active strictly
    inside the window; boundary (exactly cooldown_s) is expired."""
    c, n, cd = _f(cascade_ts), _f(now_ts), _f(cooldown_s)
    if c is None or n is None or cd is None:
        return True                     # unknown clock — stay defensive
    return (n - c) < cd


@dataclass(frozen=True)
class RegimePolicy:
    regime: str
    new_entries_allowed: bool
    btc_beta_allowed: bool
    leverage_mult: float
    reserve_deploy_max: float


def regime_policy(regime) -> RegimePolicy:
    """Flags per regime. COIL: narratives-only, no BTC directional, leverage
    0.75. BREAK_UP: full. POST_CASCADE: the caller blocks during the cooldown
    (cascade_cooldown_active), then half-size snap-back. DEAD_ZONE: blocked.
    NARRATIVE: narrative, not beta. Unknown regime = fully blocked
    (fail-closed)."""
    r = str(regime)
    if r == REGIME_COIL_COMPRESS:
        return RegimePolicy(r, new_entries_allowed=True, btc_beta_allowed=False,
                            leverage_mult=0.75, reserve_deploy_max=0.5)
    if r == REGIME_COIL_BREAK_UP:
        return RegimePolicy(r, new_entries_allowed=True, btc_beta_allowed=True,
                            leverage_mult=1.0, reserve_deploy_max=1.0)
    if r == REGIME_POST_CASCADE:
        return RegimePolicy(r, new_entries_allowed=True, btc_beta_allowed=True,
                            leverage_mult=0.5, reserve_deploy_max=0.5)
    if r == REGIME_NARRATIVE_ROTATION:
        return RegimePolicy(r, new_entries_allowed=True, btc_beta_allowed=False,
                            leverage_mult=1.0, reserve_deploy_max=0.75)
    if r == REGIME_NORMAL:
        return RegimePolicy(r, new_entries_allowed=True, btc_beta_allowed=True,
                            leverage_mult=1.0, reserve_deploy_max=1.0)
    # DEAD_ZONE and anything unrecognized: stand down.
    return RegimePolicy(r if r else REGIME_DEAD_ZONE, new_entries_allowed=False,
                        btc_beta_allowed=False, leverage_mult=0.0,
                        reserve_deploy_max=0.0)


# ── 6. Propagation map (static config) ───────────────────────────────────────

DEPENDENCY_MAP = {
    "ONDO": ["LINK", "SNX", "PENDLE", "AAVE"],
    "FET": ["RENDER", "TAO", "NVDA"],
    "ARB": ["ZRO"],
    "SOL": ["JUP", "WIF"],
    "COIN": ["BTC"],
}

PROPAGATION_LAG_FRAC = 0.4


@dataclass(frozen=True)
class PropagationVerdict:
    approved: bool
    reason: str
    lead: str = ""
    target: str = ""


def _whale_valid(value, mode) -> bool:
    """Whale-evidence validity per mode: plane pct ≥ 60 / absolute ls ≥ 1.3."""
    v = _f(value)
    if v is None:
        return False
    if mode == MODE_PLANE:
        return v >= 60.0
    if mode == MODE_ABSOLUTE:
        return v >= 1.3
    return False


def propagation_verdict(lead_symbol, lead_move_pct, target_symbol,
                        target_move_pct, lead_whale_pct_or_ls,
                        target_whale_pct_or_ls, mode) -> PropagationVerdict:
    """Leader→laggard propagation verdict. Evidence before geometry: weak
    lead evidence can prove nothing about the family, and weak target
    evidence means the lag is distribution, not opportunity (the EIGEN trap:
    FET +21% / EIGEN +9.46% on bearish whale evidence = propagation_target_weak).
    Then family membership, then the lag test (target < 0.4 × lead)."""
    lead, target = base_name(lead_symbol), base_name(target_symbol)
    if not lead or not target:
        return PropagationVerdict(False, "propagation_bad_symbol", lead, target)
    if not _whale_valid(lead_whale_pct_or_ls, mode):
        return PropagationVerdict(False, "propagation_lead_weak", lead, target)
    if not _whale_valid(target_whale_pct_or_ls, mode):
        return PropagationVerdict(False, "propagation_target_weak", lead, target)
    family = DEPENDENCY_MAP.get(lead) or []
    if target not in family:
        return PropagationVerdict(False, "propagation_no_edge", lead, target)
    lm, tm = _f(lead_move_pct), _f(target_move_pct)
    if lm is None or tm is None or lm <= 0:
        return PropagationVerdict(False, "propagation_no_lag", lead, target)
    if not (tm < lm * PROPAGATION_LAG_FRAC):
        return PropagationVerdict(False, "propagation_no_lag", lead, target)
    return PropagationVerdict(True, "propagation_approved", lead, target)


# ── 7. Exit-side pure verdicts ───────────────────────────────────────────────

MIN_HOLD_S = 600.0
NARRATIVE_MAX_DAY = 5
RATCHET_RUNGS = ((3.0, 3, 0.18), (2.0, 2, 0.12), (1.0, 1, 0.0764))
PYRAMID_ADD_FRAC = 0.5
PYRAMID_NARRATIVE_MAX_DAY = 3


def min_hold_ok(age_s, min_s: float = MIN_HOLD_S) -> bool:
    """The 10-minute minimum hold. 599 = hold, 600 = free."""
    a, m = _f(age_s), _f(min_s)
    if a is None or m is None:
        return False
    return a >= m


def narrative_clock_expired(narrative_day, max_day: int = NARRATIVE_MAX_DAY) -> bool:
    """Narrative clock: day 4 alive, day 5 expired."""
    nd, md = _f(narrative_day), _f(max_day)
    if nd is None or md is None:
        return True                     # unknown clock = expired (abstain)
    return nd >= md


def ratchet_rung(roe_multiple) -> Tuple[int, float]:
    """ROE-multiple ratchet: ≥3.0 → (3, 0.18), ≥2.0 → (2, 0.12),
    ≥1.0 → (1, 0.0764), else (0, 0.0)."""
    r = _f(roe_multiple)
    if r is None:
        return (0, 0.0)
    for rung_roe, rung, frac in RATCHET_RUNGS:
        if r >= rung_roe:
            return (rung, frac)
    return (0, 0.0)


@dataclass(frozen=True)
class PyramidVerdict:
    allow: bool
    add_notional_frac: float = 0.0      # × original notional
    move_stop_to_entry: bool = False
    reason: str = ""


def pyramid_verdict(tp1_filled, narrative_day, regime,
                    already_pyramided) -> PyramidVerdict:
    """Pyramid add verdict: TP1 banked, narrative fresh (day ≤ 3), regime
    permissive, not already pyramided → add 0.5× original notional and move
    the stop to entry. POST_CASCADE blocks absolutely (snap-back tape is not
    pyramid tape); DEAD_ZONE blocks (defensive adaptation — an add is an
    entry-class action and the dead zone blocks entries)."""
    if not tp1_filled:
        return PyramidVerdict(False, reason="tp1_not_filled")
    if already_pyramided:
        return PyramidVerdict(False, reason="already_pyramided")
    if str(regime) == REGIME_POST_CASCADE:
        return PyramidVerdict(False, reason="regime_post_cascade")
    if str(regime) == REGIME_DEAD_ZONE:
        return PyramidVerdict(False, reason="regime_dead_zone")
    nd = _f(narrative_day)
    if nd is None or nd > PYRAMID_NARRATIVE_MAX_DAY:
        return PyramidVerdict(False, reason="narrative_clock")
    return PyramidVerdict(True, add_notional_frac=PYRAMID_ADD_FRAC,
                          move_stop_to_entry=True, reason="pyramid_approved")
