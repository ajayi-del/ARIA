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
# Tradfi / kill-OFF baseline (turbo2 stocks keep this bit-for-bit).
EVAL_LADDER: tuple[tuple[str, str], ...] = (
    ("1.0", "0.4"),
    ("2.5", "0.3"),
    ("5.0", "0.3"),
)

# Big-bet institutional ladder (BTC/SOL class, coh>7): denser early TPs so a
# 1–2% tape move ($20–60 on today's risk) de-risks via several scale-outs,
# while a ≥30% runner keeps the convex right tail. Probe path keeps
# EVAL_LADDER bit-for-bit. Fracs sum to 1.0.
EVAL_BIG_BET_LADDER: tuple[tuple[str, str], ...] = (
    ("0.5", "0.20"),
    ("1.0", "0.20"),
    ("1.5", "0.15"),
    ("2.5", "0.15"),
    ("5.0", "0.30"),  # runner
)

# Crypto-only slight tighten (Governor 2026-11-10): turbo HL-candle census
# n=13 matched closes — probe median MFE 0.41R, TP1@1.0 touch 14% → @0.8
# touch 29%; TP2/TP3 almost never hit at 2.5/5 while runners gave back
# (BTC 3.49R peak → −$2.75). Big TP1 stays 0.5R (83% touch — do not
# scratch into fee band). PROP_CRYPTO_TP_TIGHTEN=0 → EVAL_* above.
# xyz: assets never take these ladders (turbo2 tradfi untouched).
EVAL_LADDER_CRYPTO: tuple[tuple[str, str], ...] = (
    ("0.8", "0.4"),
    ("2.0", "0.3"),
    ("4.0", "0.3"),
)
EVAL_BIG_BET_LADDER_CRYPTO: tuple[tuple[str, str], ...] = (
    ("0.5", "0.20"),
    ("0.85", "0.20"),
    ("1.25", "0.15"),
    ("2.0", "0.15"),
    ("4.0", "0.30"),  # runner — rotate before 5R giveback class
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
                    crypto_min_risk: float = 7.0,
                    day_latched: bool = False) -> float | None:
    """Eval-profile risk for one signal. None -> caller uses the legacy
    budget.risk_cap path (standard profile bit-for-bit).

    Clamps: remaining day budget, headroom - hard_zero (never bankrupt),
    r_max_eval. Drawdown mult compresses big bets in CAUTIOUS+ bands —
    defense outranks offense. ATS-35: evaluation xyz: probes floor at
    tradfi_min_risk when the kill switch is on. ATS-39: evaluation crypto
    probes floor at crypto_min_risk (capital_block surplus path). Both
    still clamped by the $40 day budget.

    H4 ``day_latched``: force probe class only (SOL stack — no coh>7
    re-spend of remaining after a fast big-bet loss / spent latch).
    Kill OFF at the caller → day_latched never True."""
    if profile != PROFILE_EVALUATION:
        return None
    probe = eval_probe_floor(
        profile, asset, r_base, mult,
        tradfi_enabled=tradfi_min_enabled, tradfi_min=tradfi_min_risk,
        crypto_enabled=crypto_min_enabled, crypto_min=crypto_min_risk)
    coh = _f(coherence)
    if day_latched or coh is None or not (coh > coh_big):
        risk = probe
    else:
        frac = risk_frac if coh < coh_max else 1.0
        risk = max(probe, remaining * frac * mult)
    return max(0.0, min(risk, remaining,
                        max(0.0, headroom - hard_zero), r_max_eval))


# ── H4 day-budget latch (SOL stack class) ─────────────────────────────
# After a coh>7 fill that dies ≤−0.7R within T_fast, OR when spent_frac
# of the day budget crosses the threshold, remaining day risk collapses
# to the probe floor until the next UTC budget.roll. Never mutates
# EVAL_LADDER / risk_frac globals.

DAY_LATCH_FAST_LOSS = "fast_big_bet_loss"
DAY_LATCH_SPENT = "spent_threshold"


def day_budget_spent_frac(budget: float, remaining: float) -> float | None:
    """1 - remaining/budget. None on degenerate."""
    b, r = _f(budget), _f(remaining)
    if b is None or r is None or b <= 0:
        return None
    return max(0.0, min(1.0, 1.0 - (r / b)))


def day_budget_latch_arm_verdict(
        profile: str, enabled: bool, *,
        coherence_at_entry: Any = None,
        risk_usd: Any = None,
        realized_pnl: Any = None,
        hold_s: Any = None,
        spent_frac: Any = None,
        coh_big: float = 7.0,
        loss_r: float = -0.7,
        t_fast_s: float = 300.0,
        spent_threshold: float = 0.70,
) -> str | None:
    """Return latch reason, or None to leave unlatched.

    Arm A — fast big-bet loss (SOL): coh>coh_big ∧ pnl/risk ≤ loss_r
    ∧ hold ≤ t_fast.
    Arm B — spent concentration: spent_frac > spent_threshold.
    Kill off / standard profile → None (legacy)."""
    if not enabled or profile != PROFILE_EVALUATION:
        return None
    sf = _f(spent_frac)
    if sf is not None and sf > spent_threshold:
        return DAY_LATCH_SPENT
    coh = _f(coherence_at_entry)
    risk = _f(risk_usd)
    pnl = _f(realized_pnl)
    hold = _f(hold_s)
    if (coh is None or risk is None or pnl is None or hold is None
            or risk <= 0 or hold < 0):
        return None
    if not (coh > coh_big):
        return None
    if hold > t_fast_s:
        return None
    if (pnl / risk) > loss_r:
        return None
    return DAY_LATCH_FAST_LOSS


# ── Prop MFE sharpen — BTC peak→floor ratio (not hard TP) ─────────────
# LIVE turbo open BTC risk_released≈$32 (~Dayo "31") with runner trail.
# Doctrine (2026-11-11 conquest): once favorable peak_usd ≥ 10, floor
# stop to lock max(5, frac×peak) — mid-green single-leg complement to
# basket TP. Banked ~$20–25 class still guarded by bank_protect_min=$20.
# After bank: scalper-style denser lock frac (harvest on giveback/retouch),
# reload/re-risk stood down — pyramid idiom. owns_tp=false.

MFE_SHARPEN_PEAK_USD = 10.0              # was 30 — mid-green conquest (2026-11-11)
MFE_SHARPEN_FLOOR_FRAC = 0.5
MFE_SHARPEN_MIN_FLOOR_USD = 5.0           # was 20 — pairs with $10 peak trigger
MFE_SHARPEN_BANKED_FLOOR_FRAC = 0.75      # after ≥$min bank: lock harder
BANK_PROTECT_MIN_USD = 20.0              # Dayo 75% @ ~$20 class (unchanged)

# Scalper FLOOR_RUNGS idiom on a banked runner: peak_R → lock frac of peak.
# Re-touch / giveback harvests; never re-risks. Tighten-only vs current.
RUNNER_BANK_FLOOR_RUNGS: tuple[tuple[float, float], ...] = (
    (0.5, 0.50),
    (1.0, 0.75),
    (2.0, 0.85),
)


def ratchet_mfe_peak_r(bracket: dict, mark: Any) -> float | None:
    """Raise-only birth-R peak on the bracket. None if mark unusable."""
    moved = mark_moved_r(bracket, mark)
    if moved is None:
        return None
    prev = _f(bracket.get("mfe_peak_r")) or 0.0
    if moved > prev:
        bracket["mfe_peak_r"] = float(moved)
    return _f(bracket.get("mfe_peak_r"))


def residual_risk_usd(bracket: dict) -> float | None:
    """Stop-bounded risk still on the runner after TP / size-sync harvests.

    birth risk_usd stays immutable for budget accounting; residual =
    risk_usd − risk_released (fail-open to birth when released missing)."""
    risk = _f(bracket.get("risk_usd"))
    if risk is None or risk <= 0:
        return None
    released = _f(bracket.get("risk_released")) or 0.0
    rem = risk - max(0.0, float(released))
    if rem <= 0:
        return None
    return rem


def mfe_peak_usd(bracket: dict, *, residual: bool = False) -> float | None:
    """Favorable peak in USD = mfe_peak_r × risk (stop-bounded R).

    residual=True sizes against risk still on the runner so floor labels
    stop claiming birth cash after qty shrinks (turbo BTC phantom class)."""
    peak_r = _f(bracket.get("mfe_peak_r"))
    risk = (residual_risk_usd(bracket) if residual
            else _f(bracket.get("risk_usd")))
    if peak_r is None or risk is None or risk <= 0 or peak_r <= 0:
        return None
    return peak_r * risk


def residual_stop_lock_usd(bracket: dict) -> float | None:
    """Honest lock $ = residual qty × |current_stop − entry|."""
    qty = _f(bracket.get("quantity"))
    entry = _f(bracket.get("entry_ref"))
    stop = _f(bracket.get("current_stop")) or _f(bracket.get("stop_price"))
    if qty is None or entry is None or stop is None or qty <= 0:
        return None
    return abs(float(stop) - float(entry)) * float(qty)


def mfe_floor_dollars(
        bracket: dict, *,
        floor_frac: float = MFE_SHARPEN_FLOOR_FRAC,
        min_floor_usd: float = MFE_SHARPEN_MIN_FLOOR_USD,
) -> tuple[float | None, float | None]:
    """(floor_usd_label birth, floor_usd_residual capped). Telemetry pair."""
    peak_label = mfe_peak_usd(bracket, residual=False)
    peak_res = mfe_peak_usd(bracket, residual=True)
    label = None
    if peak_label is not None:
        label = max(float(min_floor_usd), float(peak_label) * float(floor_frac))
    residual = None
    if peak_res is not None and peak_res > 0:
        # Cap at residual peak so lock_r ≤ peak_r (never stop through mark).
        residual = min(
            max(float(min_floor_usd), float(peak_res) * float(floor_frac)),
            float(peak_res))
    return label, residual


def _banked(bracket: dict, min_bank_usd: float) -> bool:
    if bracket.get("bank_protected"):
        return True
    booked = _f(bracket.get("realized_booked"))
    return booked is not None and booked >= min_bank_usd


def mfe_sharpen_verdict(
        profile: str, bracket: dict, mark: Any, *,
        enabled: bool = True,
        peak_usd_trigger: float = MFE_SHARPEN_PEAK_USD,
        floor_frac: float = MFE_SHARPEN_FLOOR_FRAC,
        min_floor_usd: float = MFE_SHARPEN_MIN_FLOOR_USD,
        banked_floor_frac: float = MFE_SHARPEN_BANKED_FLOOR_FRAC,
        min_bank_usd: float = BANK_PROTECT_MIN_USD,
) -> float | None:
    """Tighten-only stop from peak→floor ratio. None = no-op.

    Requires evaluation + kill ON + *birth* peak_usd ≥ trigger (so a
    harvested runner still arms). Floor / lock_r use *residual* risk so
    claimed dollars match qty×(stop−entry). After a real bank (≥min_bank),
    uses banked_floor_frac. Never invents TP legs."""
    if not enabled or profile != PROFILE_EVALUATION:
        return None
    ratchet_mfe_peak_r(bracket, mark)
    peak_label = mfe_peak_usd(bracket, residual=False)
    if peak_label is None or peak_label < peak_usd_trigger:
        return None
    risk = residual_risk_usd(bracket)
    if risk is None:
        risk = _f(bracket.get("risk_usd"))
    entry = _f(bracket.get("entry_ref"))
    birth = _f(bracket.get("stop_price"))
    if risk is None or entry is None or birth is None or risk <= 0:
        return None
    stop_dist = abs(entry - birth)
    if stop_dist <= 0:
        return None
    frac = (banked_floor_frac if _banked(bracket, min_bank_usd)
            else floor_frac)
    _label, floor_usd = mfe_floor_dollars(
        bracket, floor_frac=frac, min_floor_usd=min_floor_usd)
    if floor_usd is None or floor_usd <= 0:
        return None
    lock_r = floor_usd / risk
    side = bracket.get("side")
    if side == "sell":
        target = entry - lock_r * stop_dist
    elif side == "buy":
        target = entry + lock_r * stop_dist
    else:
        return None
    current = _f(bracket.get("current_stop")) or birth
    if side == "sell" and target >= current:
        return None
    if side == "buy" and target <= current:
        return None
    return target


def runner_bank_floor_verdict(
        profile: str, bracket: dict, mark: Any, *,
        enabled: bool = True,
        min_bank_usd: float = BANK_PROTECT_MIN_USD,
        rungs: tuple[tuple[float, float], ...] = RUNNER_BANK_FLOOR_RUNGS,
) -> float | None:
    """Pyramid/scalper harvest on a banked runner — lock frac of peak R.

    Arms only when realized bank ≥ min (not dust). Re-touch / giveback
    tightens stop; never adds risk. Compose after bank_protect / trail.
    Residual risk sizes the floor (same phantom repair as mfe_sharpen).
    Kill OFF → None."""
    if not enabled or profile != PROFILE_EVALUATION:
        return None
    if not _banked(bracket, min_bank_usd):
        return None
    peak_r = ratchet_mfe_peak_r(bracket, mark)
    if peak_r is None or peak_r <= 0:
        return None
    lock_frac = None
    for trigger_r, frac in rungs:
        if peak_r >= trigger_r:
            lock_frac = frac
    if lock_frac is None:
        return None
    risk = residual_risk_usd(bracket)
    if risk is None:
        risk = _f(bracket.get("risk_usd"))
    entry = _f(bracket.get("entry_ref"))
    birth = _f(bracket.get("stop_price"))
    if risk is None or entry is None or birth is None or risk <= 0:
        return None
    stop_dist = abs(entry - birth)
    if stop_dist <= 0:
        return None
    peak_res = float(peak_r) * float(risk)
    # Floor USD never below the institutional bank min, never above residual peak.
    floor_usd = min(
        max(float(min_bank_usd), peak_res * float(lock_frac)),
        peak_res)
    lock_r = floor_usd / risk
    side = bracket.get("side")
    if side == "sell":
        target = entry - lock_r * stop_dist
    elif side == "buy":
        target = entry + lock_r * stop_dist
    else:
        return None
    current = _f(bracket.get("current_stop")) or birth
    if side == "sell" and target >= current:
        return None
    if side == "buy" and target <= current:
        return None
    return target


def mfe_floor_standdown_reload(
        bracket: dict, enabled: bool = True,
        min_bank_usd: float = BANK_PROTECT_MIN_USD) -> bool:
    """True → refuse reload (bank locked; pyramid = harvest not re-risk)."""
    if not enabled:
        return False
    if bracket.get("mfe_floor_armed") or bracket.get("bank_protected"):
        return True
    # Bank exists even if protect resize pending — never re-risk into it.
    return _banked(bracket, min_bank_usd)


def runner_rerisk_standdown(
        open_brackets: list, asset: str, side: str, *,
        enabled: bool = True,
        min_bank_usd: float = BANK_PROTECT_MIN_USD) -> str | None:
    """Refuse NEW place on an asset whose runner is already banking.

    Pyramid law: re-touch = scale/harvest the open runner, not a second
    birth risk. Returns standdown reason or None."""
    if not enabled:
        return None
    for held in open_brackets or []:
        if held.get("status") and held.get("status") != "open":
            continue
        if held.get("asset") != asset:
            continue
        if held.get("side") != side:
            continue
        if _banked(held, min_bank_usd) or held.get("mfe_floor_armed"):
            return "runner_bank_rerisk"
    return None


def is_crypto_asset(asset: Any) -> bool:
    """True for non-xyz perps. turbo2 tradfi is all xyz: — excluded."""
    if asset is None:
        return False
    s = str(asset).strip()
    return bool(s) and not s.startswith("xyz:")


def ladder_touch_rate(
        mfe_rs: Iterable[Any], rungs: Iterable[Any],
) -> list[float]:
    """Fraction of MFE observations that reached each R rung (census CF)."""
    peaks = [float(x) for x in mfe_rs if _f(x) is not None]
    rs = [float(x) for x in rungs]
    if not peaks or not rs:
        return [0.0 for _ in rs]
    n = float(len(peaks))
    return [sum(1 for p in peaks if p >= r) / n for r in rs]


def ladder_for_signal(
        profile: str, coherence: Any, *,
        big_bet_ladder_enabled: bool = True,
        coh_big: float = 7.0,
        risk_usd: Any = None,
        min_risk_usd: float = 20.0,
        big_ladder: tuple[tuple[str, str], ...] = EVAL_BIG_BET_LADDER,
        base_ladder: tuple[tuple[str, str], ...] = EVAL_LADDER,
        asset: Any = None,
        crypto_tp_tighten_enabled: bool = True,
        crypto_ladder: tuple[tuple[str, str], ...] = EVAL_LADDER_CRYPTO,
        crypto_big_ladder: tuple[tuple[str, str], ...] = EVAL_BIG_BET_LADDER_CRYPTO,
) -> tuple[tuple[str, str], ...]:
    """Pick TP ladder. Big-bet denser rungs when kill ON and coh>coh_big
    (or risk ≥ min_risk). Crypto assets optionally use the tightened
    EVAL_*_CRYPTO ladders (PROP_CRYPTO_TP_TIGHTEN). xyz:/tradfi and kill
    OFF keep EVAL_LADDER / EVAL_BIG_BET_LADDER bit-for-bit. Never mutates
    the constant tuples."""
    use_crypto = bool(crypto_tp_tighten_enabled) and is_crypto_asset(asset)
    base = crypto_ladder if use_crypto else base_ladder
    big = crypto_big_ladder if use_crypto else big_ladder
    if profile != PROFILE_EVALUATION:
        return base
    if not big_bet_ladder_enabled:
        return base
    coh = _f(coherence)
    risk = _f(risk_usd)
    big_by_coh = coh is not None and coh > coh_big
    big_by_risk = risk is not None and risk >= min_risk_usd
    if big_by_coh or big_by_risk:
        return big
    return base


def bank_protect_criteria_met(
        bracket: dict, *,
        min_bank_usd: float = BANK_PROTECT_MIN_USD,
) -> bool:
    """True when realized bank OR residual/MFE lock warrants bank_protect.

    GOLD 4.62R specimen: realized_booked dust ($0.24) but mfe_floor_armed
    + stop through entry + residual peak ≥ min → must latch
    ``bank_protected``. Also honors residual_stop_lock_usd ≥ min (honest
    locked $ on the runner)."""
    if _banked(bracket, min_bank_usd):
        return True
    lock = residual_stop_lock_usd(bracket)
    if lock is not None and lock >= min_bank_usd:
        return True
    if not bracket.get("mfe_floor_armed"):
        return False
    if not stop_locks_entry(bracket.get("side"), bracket.get("current_stop"),
                            bracket.get("entry_ref")):
        return False
    peak = mfe_peak_usd(bracket, residual=True)
    if peak is None:
        peak = mfe_peak_usd(bracket, residual=False)
    return peak is not None and peak >= min_bank_usd


def bank_protect_verdict(
        profile: str, bracket: dict, *,
        enabled: bool = True,
        min_bank_usd: float = BANK_PROTECT_MIN_USD,
) -> float | None:
    """After bank criteria met, lock remainder at entry (resize target).

    Dayo: banked ~$20 must never return — min is institutional, not dust
    ($15 was too soft). Criteria = realized_booked ≥ min OR residual/MFE
    latch (see bank_protect_criteria_met). Worst case on the runner is
    flat; net ≥ bank. Tighten-only. Already-tighter → None (caller still
    stamps bank_protected via latch-only path). Kill OFF / standard /
    criteria unmet → None. Never places TP."""
    if not enabled or profile != PROFILE_EVALUATION:
        return None
    if not bank_protect_criteria_met(bracket, min_bank_usd=min_bank_usd):
        return None
    entry = _f(bracket.get("entry_ref"))
    if entry is None:
        return None
    side = bracket.get("side")
    if side not in ("buy", "sell"):
        return None
    target = entry  # BE on remainder ⇒ bank never returns
    birth = _f(bracket.get("stop_price"))
    current = _f(bracket.get("current_stop")) or birth
    if current is None:
        return None
    if side == "sell" and target >= current:
        return None  # already tighter (trail/MFE past entry)
    if side == "buy" and target <= current:
        return None
    return target


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


# H2 — place-time coh stamp invariant (impl-plan 1c / GOLD 0.8 stain).
# Honest ladder is ARIA 0–10. Values in (0, 1) are the pre-#24 strength-as-
# coherence fingerprint (live {0.6, 0.8}); refuse fail-closed rather than
# auto-rescale. None = abstain (legacy place without coh stamp).
COH_STAMP_REFUSED = "coh_stamp_refused"
COH_UNIT_STAIN_HI = 1.0  # exclusive: 0 < coh < 1.0


def coh_stamp_verdict(coherence: Any, enabled: bool = True) -> str | None:
    """Refuse place when stamped coh is outside honest 0–10 (or unit stain).

    Returns standdown reason, or None to allow. Kill switch off → None
    (legacy bit-for-bit). None/absent coherence → allow (probe paths may
    omit). NaN/inf / out of [0,10] / (0,1) stain → COH_STAMP_REFUSED.
    """
    if not enabled:
        return None
    if coherence is None:
        return None
    coh = _f(coherence)
    if coh is None:
        return COH_STAMP_REFUSED
    if coh < 0.0 or coh > 10.0:
        return COH_STAMP_REFUSED
    # Unit stain: 0–1 scale leaked onto 0–10 ladder (GOLD 0.8 class).
    # Exact 0.0 / 1.0 kept legal (floor/edge); open interval is the wound.
    if 0.0 < coh < COH_UNIT_STAIN_HI:
        return COH_STAMP_REFUSED
    return None


def mark_moved_r(bracket: dict, mark: Any) -> float | None:
    """Birth-R move from an HL mark. None on unparseable / zero stop dist."""
    px, entry, birth = _f(mark), _f(bracket.get("entry_ref")), _f(
        bracket.get("stop_price"))
    if px is None or entry is None or birth is None:
        return None
    stop_dist = abs(entry - birth)
    if stop_dist <= 0:
        return None
    side = bracket.get("side")
    moved = (entry - px) if side == "sell" else (px - entry)
    return moved / stop_dist


def rebase_entry_from_venue(
        bracket: dict, position: dict | None, *,
        min_rel_diff: float = 1e-9) -> str | None:
    """Stamp entry_ref from venue entryPrice when signal ≠ fill (BTC #2).

    Live wound: signal entry_ref 74507 vs Propr entryPrice 75190 → false
    +2.6R → BE/trail armed ~12s after birth; trail 74768 sat under the
    fill → −$4.21. Returns previous entry_ref when rebased, else None.
    Resets mfe_peak_r so phantom signal-R cannot survive. Birth
    stop_price untouched (protective)."""
    if not isinstance(position, dict):
        return None
    venue = _f(position.get("entryPrice"))
    if venue is None or venue <= 0:
        return None
    prev = _f(bracket.get("entry_ref"))
    if prev is None:
        bracket["entry_ref"] = str(venue)
        return None
    # Relative epsilon — ignore pure float noise; catch real fill gaps.
    if abs(venue - prev) <= max(abs(prev), 1.0) * min_rel_diff:
        return None
    old = str(bracket.get("entry_ref"))
    bracket["entry_ref"] = str(venue)
    if "mfe_peak_r" in bracket:
        bracket["mfe_peak_r"] = 0.0
    return old


def be_trail_mfe_gate(
        bracket: dict, mark: Any, arm_r: float = 0.5) -> bool:
    """True when ratcheted mfe_peak_r ≥ arm_r (shared BE/trail floor).

    Prefer existing eval_be_arm_r (~0.5). Call after venue entry rebase so
    the peak measures fill geometry, not signal/fill gap phantoms."""
    if arm_r is None or arm_r < 0:
        arm_r = 0.5
    peak = ratchet_mfe_peak_r(bracket, mark)
    if peak is None:
        return False
    return float(peak) >= float(arm_r)


def should_arm_breakeven_from_mark(
        profile: str, bracket: dict, mark: Any,
        arm_r: float = 0.5) -> bool:
    """H1 mark-plane BE — GOLD/HIP-3 class when venue uPnL BE stays dark.

    True when ratcheted mfe_peak_r ≥ arm_r (eval_be_arm_r) and the stop
    does not yet lock entry. Kill-switch gating is the caller's job.
    Never stamps on fee-concession alone (stop_locks_entry still gates
    be_armed after the exact-entry resize)."""
    if profile != PROFILE_EVALUATION or bracket.get("be_armed"):
        return False
    if stop_locks_entry(bracket.get("side"), bracket.get("current_stop"),
                        bracket.get("entry_ref")):
        return False  # already locking — heal stamp path owns this
    return be_trail_mfe_gate(bracket, mark, arm_r)


def be_armed_needs_heal(bracket: dict) -> bool:
    """True when current_stop already locks entry but be_armed is false.

    Biconditional heal for ``be_armed ↔ stop_locks_entry`` (false-negative
    side). PR02 sealed the false-positive (no stamp on fee concession)."""
    if bracket.get("be_armed"):
        return False
    return stop_locks_entry(bracket.get("side"), bracket.get("current_stop"),
                            bracket.get("entry_ref"))


# Trailing rungs (spec §3.5 unlock 1): (trigger_R, lock_R). At +1R the stop
# moves to breakeven + round-trip fees; at +2R it locks +1R. The R ruler is
# the BIRTH geometry (entry_ref vs the birth stop_price) — later tighten/
# reload moves never re-baseline R.
TRAIL_RUNGS: tuple[tuple[float, float], ...] = ((1.0, 0.0), (2.0, 1.0))


def trail_verdict(profile: str, bracket: dict, mark: Any,
                  fee_rate: float,
                  rungs: tuple[tuple[float, float], ...] = TRAIL_RUNGS,
                  min_peak_r: float = 0.5,
                  ) -> float | None:
    """HL-mark-driven trailing stop. Returns the new trigger or None.

    HL is a REFERENCE plane: the mark drives the TRIGGER only. None on
    stale/absent mark (caller runs the legacy uPnL breakeven path),
    unparseable geometry, standard profile, below the shared mfe_peak_r
    floor (eval_be_arm_r ~0.5 — BTC #2 birth-trail class), below the
    first rung, or when the rung target is not tighter than current_stop
    (tighten-only — the trail never gives back locked ground)."""
    if profile != PROFILE_EVALUATION:
        return None
    if not be_trail_mfe_gate(bracket, mark, min_peak_r):
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
