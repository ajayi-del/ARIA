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

from typing import Any

PROFILE_STANDARD = "standard"
PROFILE_EVALUATION = "evaluation"

# quant exit ladder: (R multiple, fraction) — banks 40% at 1R, runner 30% to 5R
EVAL_LADDER: tuple[tuple[str, str], ...] = (
    ("1.0", "0.4"),
    ("2.5", "0.3"),
    ("5.0", "0.3"),
)

# Venue leverage ladder (leverage-limits/effective pinned live 2026-11-07).
# Margin efficiency only — risk is always stop-bounded.
_LEV_OVERRIDES: dict[str, int] = {
    "BTC": 10, "ETH": 10, "SOL": 10, "xyz:SP500": 10, "xyz:XYZ100": 10,
    "xyz:GOLD": 8, "xyz:SILVER": 8, "xyz:COPPER": 8, "xyz:PLATINUM": 8,
    "xyz:PALLADIUM": 8, "xyz:ALUMINIUM": 8,
    "xyz:CL": 8, "xyz:NATGAS": 8, "xyz:HO": 8, "xyz:TTF": 8,
    "xyz:EUR": 25, "xyz:JPY": 25, "xyz:GBP": 25, "xyz:DXY": 25,
    "xyz:KRW": 25,
    "xyz:JP225": 5, "xyz:KR200": 5, "xyz:NIFTY": 5, "xyz:MAGS": 5,
    "xyz:IBOV": 5, "xyz:EWJ": 5, "xyz:EWT": 5, "xyz:EWY": 5,
    "xyz:EWZ": 5,
}


def leverage_for(asset: str) -> int:
    """Static venue ladder. Unknown xyz: -> 4 (equity/pre-IPO default),
    unknown crypto -> 2 (venue crypto default)."""
    if asset in _LEV_OVERRIDES:
        return _LEV_OVERRIDES[asset]
    return 4 if asset.startswith("xyz:") else 2


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


def risk_for_signal(profile: str, coherence: Any, remaining: float,
                    headroom: float, hard_zero: float, mult: float,
                    r_base: float, r_max_eval: float,
                    coh_big: float = 7.0, coh_max: float = 8.5,
                    risk_frac: float = 0.70) -> float | None:
    """Eval-profile risk for one signal. None -> caller uses the legacy
    budget.risk_cap path (standard profile bit-for-bit).

    Clamps: remaining day budget, headroom - hard_zero (never bankrupt),
    r_max_eval. Drawdown mult compresses big bets in CAUTIOUS+ bands —
    defense outranks offense."""
    if profile != PROFILE_EVALUATION:
        return None
    probe = r_base * mult
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
