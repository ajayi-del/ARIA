"""Evaluation-phase risk profile (Governor doctrine 2026-11-08).

The $25 evaluation account is an OPTION, not capital: replacement cost is
the fee, so the eval-phase objective is P(pass) per unit time — few big
bets, losses cut by geometry, winners run. Funded phase (standard profile)
is the conservative mirror. Mixing the two is the classic elimination
failure mode.

Doctrine encoded:
- daily loss budget $40 ($50 once the day is paying us — the harness has
  no price plane, so "trend day" is read from the day's realized PnL).
- coherence > 7 = big-bet class: risk = 70% of the remaining day budget;
  coherence >= 8.5 = 100%. Below the bar the probe class (r_base) keeps
  the account in the game without spending the option.
- leverage is a MARGIN-EFFICIENCY knob, never a risk knob: risk is
  stop-bounded (risk = qty x stop_dist), leverage only decides how much
  margin the notional posts. Margin cap (30% of equity) makes the
  2x/4x/10x venue ladder self-select: big notional is only reachable on
  high-leverage classes.
- exit ladder 1R/2.5R/5R (40/30/30) with a +0.5R breakeven arm read from
  position unrealizedPnl (no price plane needed) — the XRP +$2 -> -$5
  give-back class dies at the arm.
- reload graph (live evidence 2026-11-08, BCH short 260.88): skipped
  same-direction signals decomposed into same-level chase (correctly
  skipped — the birth ladder owns size) and matured re-fires at worse
  prices with RISING coherence (5.66 -> 8.71 as the move proved itself).
  Zero better-price re-fires occurred. So: reloads only into PROVEN
  trades (TP banked or breakeven armed), sized out of HARVESTED risk
  (never new principal), stop tighten-only. Worse-price reloads demand
  the 8.5 bar and reload half; better-price reloads take the 7 bar full.

Pure module: verdict math only. IO lives in the caller.
"""

from __future__ import annotations

from typing import Any, Iterable

PROFILE_STANDARD = "standard"
PROFILE_EVALUATION = "evaluation"

# quant exit ladder: (R multiple, fraction) — banks 40% at 1R, runner 30% to 5R
EVAL_LADDER: tuple[tuple[str, str], ...] = (
    ("1.0", "0.4"),
    ("2.5", "0.3"),
    ("5.0", "0.3"),
)

# Venue leverage ladder — must match /leverage-limits/effective
# (probed live turbo2 2026-11-08). Margin efficiency only — risk is
# always stop-bounded. API shape: defaults{crypto/equity/pre_ipo/index/
# commodity/fx} + sparse per-asset overrides. Pinning above the venue
# max → 400 13304 margin_config_invalid_leverage (COPPER@8, EWJ@5, …).
_LEV_OVERRIDES: dict[str, int] = {
    # API overrides
    "BTC": 10, "ETH": 10, "SOL": 10,
    "xyz:SP500": 10, "xyz:XYZ100": 10,
    "xyz:GOLD": 8, "xyz:SILVER": 8, "xyz:CL": 8, "xyz:BRENTOIL": 8,
    # commodity default 5 (NOT 8 — only GOLD/SILVER/CL/BRENTOIL are 8)
    # TTF omitted: venue refuses its config updates outright (400 13302
    # live on turbo2 2026-11-08) — provisioning it only journals noise.
    "xyz:COPPER": 5, "xyz:PLATINUM": 5, "xyz:PALLADIUM": 5,
    "xyz:ALUMINIUM": 5, "xyz:NATGAS": 5, "xyz:HO": 5,
    # fx default 25
    "xyz:EUR": 25, "xyz:JPY": 25, "xyz:GBP": 25, "xyz:DXY": 25,
    "xyz:KRW": 25,
    # index default 5 (true index perps — JP225/KR200/NIFTY provisioned
    # live; IBOV omitted, same 13302 refusal class as TTF)
    "xyz:JP225": 5, "xyz:KR200": 5, "xyz:NIFTY": 5,
    # country ETFs / MAGS: venue class = equity (max 4). Theme family may
    # say "index" but 5 → 13304 live on turbo2 2026-11-08.
    "xyz:MAGS": 4, "xyz:EWJ": 4, "xyz:EWT": 4, "xyz:EWY": 4, "xyz:EWZ": 4,
}

# Venue instrument class for /leverage-limits/effective defaults lookup.
# Distinct from theme families (rotation) — EWJ is theme-index, venue-equity.
_VENUE_CLASS: dict[str, str] = {
    "BTC": "crypto", "ETH": "crypto", "SOL": "crypto",
    "xyz:SP500": "index", "xyz:XYZ100": "index",
    "xyz:JP225": "index", "xyz:KR200": "index",
    "xyz:NIFTY": "index", "xyz:IBOV": "index",
    "xyz:MAGS": "equity", "xyz:EWJ": "equity", "xyz:EWT": "equity",
    "xyz:EWY": "equity", "xyz:EWZ": "equity",
    "xyz:GOLD": "commodity", "xyz:SILVER": "commodity",
    "xyz:COPPER": "commodity", "xyz:PLATINUM": "commodity",
    "xyz:PALLADIUM": "commodity", "xyz:ALUMINIUM": "commodity",
    "xyz:CL": "commodity", "xyz:BRENTOIL": "commodity",
    "xyz:NATGAS": "commodity", "xyz:HO": "commodity", "xyz:TTF": "commodity",
    "xyz:EUR": "fx", "xyz:JPY": "fx", "xyz:GBP": "fx",
    "xyz:DXY": "fx", "xyz:KRW": "fx",
}


def leverage_for(asset: str) -> int:
    """Static venue ladder. Unknown xyz: -> 4 (equity/pre-IPO default),
    unknown crypto -> 2 (venue crypto default)."""
    if asset in _LEV_OVERRIDES:
        return _LEV_OVERRIDES[asset]
    return 4 if asset.startswith("xyz:") else 2


def venue_class_for(asset: str) -> str:
    """Instrument class for leverage-limits defaults. Unknown xyz: -> equity."""
    if asset in _VENUE_CLASS:
        return _VENUE_CLASS[asset]
    return "equity" if asset.startswith("xyz:") else "crypto"


def venue_max_leverage(asset: str, limits: dict | None) -> int | None:
    """Max leverage from /leverage-limits/effective. None = unknown/malformed.

    Prefer per-asset overrides; else defaults[venue_class]. Never invents
    a number above the venue table — provision clamps with this."""
    if not isinstance(limits, dict):
        return None
    overrides = limits.get("overrides")
    if isinstance(overrides, dict) and asset in overrides:
        try:
            v = int(overrides[asset])
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None
    defaults = limits.get("defaults")
    if not isinstance(defaults, dict):
        return None
    raw = defaults.get(venue_class_for(asset))
    try:
        v = int(raw)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def clamp_leverage(asset: str, wanted: int, limits: dict | None) -> int:
    """min(wanted, venue max). limits=None/unknown -> wanted unchanged."""
    vmax = venue_max_leverage(asset, limits)
    if vmax is None:
        return int(wanted)
    return min(int(wanted), vmax)


# Venue refuses config updates outright (400 13302 live on turbo2
# 2026-11-08). Never provision-spam these even when they sit in the
# family / SYMBOL_MAP universe.
_PROVISION_REFUSED: frozenset[str] = frozenset({"xyz:TTF", "xyz:IBOV"})


def provision_assets_for(
        asset_class: str,
        candidates: Iterable[str] | None = None) -> tuple[str, ...]:
    """Venue-leverage pin list for boot provision, filtered by sleeve.

    candidates=None → legacy `_LEV_OVERRIDES` only (27 elevated-lev pins).
    Prefer callers pass the family-map keys so wave-3 sleeve assets get a
    venue pin before first fill — without hardcoding 134 override rows.
    Target leverage itself comes from `target_leverage` (venue
    /leverage-limits/effective when known). Sleeve filter: crypto =
    non-xyz, tradfi = xyz:, all = union. Skips `_PROVISION_REFUSED`.
    Sorted for stable journals."""
    if asset_class not in ("all", "crypto", "tradfi"):
        raise ValueError(f"bad asset_class {asset_class!r}")
    src = _LEV_OVERRIDES if candidates is None else candidates
    out = []
    for asset in src:
        if asset in _PROVISION_REFUSED:
            continue
        is_tradfi = str(asset).startswith("xyz:")
        if asset_class == "crypto" and is_tradfi:
            continue
        if asset_class == "tradfi" and not is_tradfi:
            continue
        out.append(str(asset))
    return tuple(sorted(set(out)))


def target_leverage(asset: str, limits: dict | None) -> int:
    """Boot pin target: venue max when /leverage-limits/effective is known,
    else static `leverage_for`. Prefer venue truth over a 134-line hardcode;
    static ladder remains the fail-closed fallback when limits are dark."""
    vmax = venue_max_leverage(asset, limits)
    if vmax is not None:
        return vmax
    return leverage_for(asset)


def _f(x: Any) -> float | None:
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def trend_day(realized_today: float | None) -> bool:
    """The day is paying us -> the $50 ceiling. Local proxy for the
    Governor's trend-day expansion; the harness has no price plane."""
    r = _f(realized_today)
    return r is not None and r > 0.0


def daily_floor(profile: str, realized_today: float | None,
                eval_budget: float, eval_trend_budget: float,
                standard_floor: float) -> float:
    if profile != PROFILE_EVALUATION:
        return standard_floor
    return eval_trend_budget if trend_day(realized_today) else eval_budget


def is_tradfi_asset(asset: Any) -> bool:
    """Sleeve marker: xyz: listings are the tradfi/stocks nation."""
    return str(asset or "").startswith("xyz:")


def tradfi_probe_floor(profile: str, asset: Any, enabled: bool,
                       r_base: float, min_risk: float,
                       mult: float = 1.0) -> float:
    """Eval tradfi/stocks probe floor (ATS-35). Thin wrapper → eval_probe_floor."""
    return eval_probe_floor(
        profile, asset, r_base, mult,
        tradfi_enabled=enabled, tradfi_min=min_risk,
        crypto_enabled=False, crypto_min=0.0)


def eval_probe_floor(profile: str, asset: Any, r_base: float,
                     mult: float = 1.0, *,
                     tradfi_enabled: bool = False,
                     tradfi_min: float = 7.0,
                     crypto_enabled: bool = False,
                     crypto_min: float = 7.0) -> float:
    """Eval sleeve probe floor (ATS-35 tradfi + ATS-39 crypto).

    Standard / kill-switch-off → r_base × mult (legacy).
    Evaluation xyz: + tradfi switch → max(r_base, tradfi_min) × mult.
    Evaluation crypto + crypto switch → max(r_base, crypto_min) × mult.
    Probe class stays small vs coh>7 big bets; day-budget clamps bind
    upstream in risk_for_signal. Day $40 still owns the ceiling."""
    probe = float(r_base) * float(mult)
    if profile != PROFILE_EVALUATION:
        return probe
    if is_tradfi_asset(asset):
        if not tradfi_enabled:
            return probe
        floor = max(0.0, float(tradfi_min))
    else:
        if not crypto_enabled:
            return probe
        floor = max(0.0, float(crypto_min))
    if floor <= 0:
        return probe
    return max(probe, floor * float(mult))


def cost_aware_max_frac(profile: str, asset: Any, enabled: bool,
                        base_frac: float, entry: Any, stop: Any,
                        fee_rate: float, hard_cap: float = 0.30,
                        *,
                        crypto_enabled: bool = False,
                        crypto_hard_cap: float = 0.30
                        ) -> float:
    """Cost-aware fee/risk allowance for eval sleeves (ATS-35/39).

    Fee/risk = 2×fee×entry/stop_dist is size-invariant — raising $5→$7
    alone cannot clear a 0.35% stop that prints $1.29 rt on a $5 probe.
    When the geometric ratio exceeds base_frac, raise the allowance to
    hard_cap (not the exact ratio) so ATR stops (~0.35–0.5%) clear the
    Decimal gate without float-equality rejects; dust whose ratio exceeds
    hard_cap still dies.

    ATS-35: evaluation xyz: + `enabled` → tradfi hard_cap.
    ATS-39: evaluation crypto + `crypto_enabled` → crypto_hard_cap.
    Kill switch / standard → base_frac."""
    base = max(0.0, float(base_frac))
    if profile != PROFILE_EVALUATION:
        return base
    if is_tradfi_asset(asset):
        if not enabled:
            return base
        cap = float(hard_cap)
    else:
        if not crypto_enabled:
            return base
        cap = float(crypto_hard_cap)
    entry_f, stop_f = _f(entry), _f(stop)
    fee = _f(fee_rate)
    if entry_f is None or stop_f is None or fee is None or entry_f <= 0:
        return base
    stop_dist = abs(entry_f - stop_f)
    if stop_dist <= 0:
        return base
    ratio = 2.0 * fee * entry_f / stop_dist
    if ratio <= base:
        return base
    # Hard cap — never return the exact geometric ratio. Exact equality
    # against Decimal(rt) spuriously rejects valid 0.3–0.5% stock stops.
    return max(base, cap)


def cost_aware_risk_floor(risk: float, entry: Any, stop: Any,
                          fee_rate: float, max_frac: float,
                          min_floor: float = 7.0) -> float:
    """Absolute fee-cover floor: max(risk, min_floor, rt/max_frac).

    Uses the inbound risk's notional. After a proportional resize the
    fee/risk ratio is unchanged — callers still need cost_aware_max_frac
    (or an equivalent gate) for tight stock stops. Day budget / ceiling
    clamps remain the caller's job."""
    out = max(float(risk), max(0.0, float(min_floor)))
    entry_f, stop_f = _f(entry), _f(stop)
    fee = _f(fee_rate)
    frac = _f(max_frac)
    if (entry_f is None or stop_f is None or fee is None or frac is None
            or entry_f <= 0 or frac <= 0 or out <= 0):
        return out
    stop_dist = abs(entry_f - stop_f)
    if stop_dist <= 0:
        return out
    notional = (out / stop_dist) * entry_f
    rt_cost = notional * 2.0 * fee
    return max(out, rt_cost / frac)


def risk_for_signal(profile: str, coherence: Any, remaining: float,
                    headroom: float, hard_zero: float, mult: float,
                    r_base: float, r_max_eval: float,
                    coh_big: float = 7.0, coh_max: float = 8.5,
                    risk_frac: float = 0.70,
                    asset: Any = None,
                    tradfi_min_enabled: bool = False,
                    tradfi_min_risk: float = 7.0,
                    crypto_min_enabled: bool = False,
                    crypto_min_risk: float = 7.0) -> float | None:
    """Eval-profile risk for one signal. None -> caller uses the legacy
    budget.risk_cap path (standard profile bit-for-bit).

    Clamps: remaining day budget, headroom - hard_zero (never bankrupt),
    r_max_eval. Drawdown mult compresses big bets in CAUTIOUS+ bands —
    defense outranks offense. ATS-35: evaluation xyz: probes floor at
    tradfi_min_risk when the kill switch is on. ATS-39: evaluation crypto
    probes floor at crypto_min_risk (capital_block surplus path). Both
    still clamped by the $40 day budget."""
    if profile != PROFILE_EVALUATION:
        return None
    probe = eval_probe_floor(
        profile, asset, r_base, mult,
        tradfi_enabled=tradfi_min_enabled, tradfi_min=tradfi_min_risk,
        crypto_enabled=crypto_min_enabled, crypto_min=crypto_min_risk)
    coh = _f(coherence)
    if coh is None or not (coh > coh_big):
        risk = probe
    else:
        frac = risk_frac if coh < coh_max else 1.0
        risk = max(probe, remaining * frac * mult)
    return max(0.0, min(risk, remaining,
                        max(0.0, headroom - hard_zero), r_max_eval))


def risk_ceiling(profile: str, remaining: float, headroom: float,
                 hard_zero: float, r_max_eval: float) -> float | None:
    """Kant belt for evaluation place_bracket. None -> legacy risk_cap.

    The legacy BudgetBrain.risk_cap clamps at r_max ($7.5). That silently
    vetoes every coh>7 big bet sized by risk_for_signal ($28–$50). Eval
    ceiling mirrors risk_for_signal's clamps: remaining day budget,
    headroom − hard_zero, single-trade r_max_eval (trend-day budget).
    """
    if profile != PROFILE_EVALUATION:
        return None
    if headroom <= hard_zero:
        return 0.0
    return max(0.0, min(remaining, headroom - hard_zero, r_max_eval))


# Probe squat fix (schema Phase 4 / ATS-21): $5 probes must not fill the
# last owner slot before a coh>7 big bet arrives. Telemetry reason below.
SLOT_RESERVE_STANDDOWN = "probe_standdown_slot_reserve"


def slot_reserve_verdict(
        profile: str,
        enabled: bool,
        coherence: Any,
        open_count: int,
        max_positions: int,
        reserve: int = 1,
        coh_big: float = 7.0) -> str | None:
    """Stand down probe-class entries that would spend a reserved slot.

    Returns the standdown reason, or None to allow. Kill switch off /
    standard profile / reserve≤0 / free slots above the reserve → None
    (legacy bit-for-bit). When free ≤ reserve, only coh > coh_big may
    consume the remaining capacity; probes (and unparseable coherence)
    stand down so the pass path stays reachable.
    """
    if not enabled or profile != PROFILE_EVALUATION:
        return None
    try:
        res_n = int(reserve)
        max_n = int(max_positions)
        open_n = int(open_count)
    except (TypeError, ValueError):
        return None
    if res_n <= 0 or max_n <= 0:
        return None
    free = max(0, max_n - max(0, open_n))
    if free > res_n:
        return None
    coh = _f(coherence)
    if coh is not None and coh > coh_big:
        return None
    return SLOT_RESERVE_STANDDOWN


def margin_ok(profile: str, notional: float, asset: str, equity: float,
              cap_frac: float = 0.30) -> bool | None:
    """Eval margin cap: notional/leverage <= cap_frac x equity.
    None -> abstain (standard profile never binds)."""
    if profile != PROFILE_EVALUATION:
        return None
    lev = leverage_for(asset)
    if lev <= 0 or equity <= 0:
        return False
    return (notional / lev) <= cap_frac * equity


def should_arm_breakeven(profile: str, bracket: dict, unrealized_pnl: Any,
                         arm_r: float = 0.5) -> bool:
    """+arm_r R unrealized -> stop moves to entry (give-back kill).
    Reads the position poll's unrealizedPnl — no price plane needed."""
    if profile != PROFILE_EVALUATION or bracket.get("be_armed"):
        return False
    upnl = _f(unrealized_pnl)
    risk = _f(bracket.get("risk_usd"))
    if upnl is None or risk is None or risk <= 0:
        return False
    return upnl >= arm_r * risk


def better_price(side: str, fresh_entry: float, held_entry: float) -> bool:
    """Shorts: higher is better. Longs: lower is better."""
    return fresh_entry > held_entry if side == "sell" else fresh_entry < held_entry


def reload_verdict(profile: str, bracket: dict, coherence: Any,
                   fresh_entry: Any, coh_big: float = 7.0,
                   coh_max: float = 8.5,
                   max_reloads: int = 1) -> dict[str, Any] | None:
    """The re-entry graph edge: fresh same-direction signal on a held,
    PROVEN bracket. Returns {"frac": float, "better": bool} or None.

    Gates: proven (TP banked or breakeven armed), reload budget left,
    coherence bar (better price: >coh_big; worse price: >=coh_max and
    half size). Averaging into unproven risk is martingale — refused."""
    if profile != PROFILE_EVALUATION:
        return None
    if int(bracket.get("reloads", 0)) >= max_reloads:
        return None
    proven = (bracket.get("be_armed")
              or _f(bracket.get("risk_released")) not in (None, 0.0))
    if not proven:
        return None
    coh = _f(coherence)
    entry = _f(fresh_entry)
    held = _f(bracket.get("entry_ref"))
    if coh is None or entry is None or held is None:
        return None
    better = better_price(bracket["side"], entry, held)
    if better and coh > coh_big:
        return {"frac": 1.0, "better": True}
    if not better and coh >= coh_max:
        return {"frac": 0.5, "better": False}
    return None


def reload_size(bracket: dict, frac: float) -> float:
    """Reload spends HARVESTED risk only — the bracket's total commitment
    never exceeds its original budget line."""
    released = _f(bracket.get("risk_released")) or 0.0
    return max(0.0, released * frac)


def tightened_stop(side: str, current_stop: Any, fresh_stop: Any) -> float | None:
    """Tighten-only: short takes the LOWER stop, long the HIGHER.
    None when either leg is unparseable (caller keeps current)."""
    cur, fresh = _f(current_stop), _f(fresh_stop)
    if cur is None or fresh is None:
        return None
    return min(cur, fresh) if side == "sell" else max(cur, fresh)


def stop_locks_entry(side: str, stop: Any, entry: Any) -> bool:
    """True when stop is at or through entry (no fee hole vs exact BE).

    Trail rung-1 places a fee-concession stop (short: entry+fees, long:
    entry−fees) which is LOOSER than the uPnL exact-entry arm. Stamping
    be_armed on that rung permanently suppresses the tighter BE pass and
    lets an HL mark unlock the proven gate before venue uPnL confirms.
    Rung-2 (+1R lock) and exact-entry BE both lock through entry."""
    s, e = _f(stop), _f(entry)
    if s is None or e is None:
        return False
    return s <= e if side == "sell" else s >= e


# Trailing rungs (spec §3.5 unlock 1): (trigger_R, lock_R). At +1R the stop
# moves to breakeven + round-trip fees; at +2R it locks +1R. The R ruler is
# the BIRTH geometry (entry_ref vs the birth stop_price) — later tighten/
# reload moves never re-baseline R.
TRAIL_RUNGS: tuple[tuple[float, float], ...] = ((1.0, 0.0), (2.0, 1.0))


def trail_verdict(profile: str, bracket: dict, mark: Any,
                  fee_rate: float,
                  rungs: tuple[tuple[float, float], ...] = TRAIL_RUNGS
                  ) -> float | None:
    """HL-mark-driven trailing stop. Returns the new trigger or None.

    HL is a REFERENCE plane: the mark drives the TRIGGER only. None on
    stale/absent mark (caller runs the legacy uPnL breakeven path),
    unparseable geometry, standard profile, below the first rung, or when
    the rung target is not tighter than current_stop (tighten-only — the
    trail never gives back locked ground)."""
    if profile != PROFILE_EVALUATION:
        return None
    px, entry, birth = _f(mark), _f(bracket.get("entry_ref")), _f(
        bracket.get("stop_price"))
    if px is None or entry is None or birth is None:
        return None
    stop_dist = abs(entry - birth)
    if stop_dist <= 0:
        return None
    side = bracket["side"]
    moved = (entry - px) if side == "sell" else (px - entry)
    moved_r = moved / stop_dist
    lock_r = None
    for trigger_r, lock in rungs:
        if moved_r >= trigger_r:
            lock_r = lock
    if lock_r is None:
        return None
    if lock_r == 0.0:
        # breakeven + fees: the stop covers the round trip, not just entry
        buffer = 2.0 * fee_rate * entry
        target = entry + buffer if side == "sell" else entry - buffer
    else:
        target = (entry - lock_r * stop_dist if side == "sell"
                  else entry + lock_r * stop_dist)
    current = _f(bracket.get("current_stop")) or birth
    if side == "sell" and target >= current:
        return None
    if side != "sell" and target <= current:
        return None
    return target


def overflow_verdict(peer_snaps: list[dict | None], tradfi: bool,
                     now: float, stale_s: float = 90.0) -> bool:
    """Overflow-account gate (Governor 2026-11-08): the demo fires only
    when the turbo that OWNS this signal's class is at max positions.

    A peer covers the class when its sleeve is "all" or matches. Every
    covering peer must be fresh AND full. Fail-CLOSED: a missing, stale,
    or sleeveless peer refuses the trade — the overflow account never
    doubles a signal on blind data."""
    covering = []
    for snap in peer_snaps:
        if not snap:
            return False
        ts = _f(snap.get("ts"))
        if ts is None or now - ts > stale_s:
            return False
        sleeve = snap.get("sleeve")
        if sleeve == "all" or (sleeve == "tradfi") == tradfi and sleeve in (
                "crypto", "tradfi"):
            covering.append(snap)
    if not covering:
        return False
    return all(len(s.get("positions") or [])
               >= int(s.get("max_positions") or 3) for s in covering)
