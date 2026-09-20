"""intelligence/hedge_manager.py — P2 hedge brain (pure, zero-I/O).

2026-09-19 Governor-approved ByBit hedge subsystem, Phase 2 (evidence-scoped
build — the forensic economics on 1,035 closes drove every design choice).

Mode 1 PROFIT-LOCK (live-ready, still gated by config.hedge_enabled):
a primary long whose house ROE (intelligence/roe_ratchet.roe_pct basis —
price move % × leverage, the treasury formula) crosses a ladder rung earns a
partial SHORT on the hedge venue sized as the rung's fraction of the long
(0.30%→3%, 0.50%→6%, 0.70%→10%, 1.00%→15%). Highest rung wins; the rung
ratchets UP only. Arming requires ALL of: rung crossed, remaining hedge
notional ≥ hedge_min_notional (ByBit min ≈ 5 USDT), basis gate open
(BasisTracker not stressed AND basis ≥ hedge_min_basis_bp — never short the
hedge venue into a dislocated-down basis), dynamic short-limit room, and
dual-mark coverage. Entry is an amend-chase (2026-09-19 Governor budget
15s/3×/1×ATR): resting post-only short limit at mark×(1+tick_est), amended
down only when price fell ≥ max(0.25%, 2×spread) AND ≥15s since the last
amend; after hedge_chase_max_steps one market conversion inside
hedge_slippage_cap, else the plan aborts. If the mark drifts ≥ 1×ATR below
the arm-time mark the thesis has changed — the plan ABANDONS (never chase
the thesis into a worse entry). On fill the leg gets an exchange-side
trailing stop (hedge_trail_pct × entry, absolute — survives restarts,
tonight's doctrine) plus a catastrophic fixed stop at
entry×(1+hedge_catastrophic_stop_pct).

Derived leverage (CRITICAL, 2026-09-19 Governor cross-buffer formula): the
catastrophic 18% stop is BEYOND isolated-margin liquidation at lev ≥ 5, so
leverage is derived, never assumed: min_clearance = 1.25×stop_frac + mmr −
free_equity/notional; ≤ 0 → the cross buffer alone covers → leverage_max;
else lev = 1/min_clearance clamped to [leverage_min, leverage_max], below
min → unhedgeable skip. Cross margin pushes liquidation out at small hedge
notional and tightens automatically as account utilization rises — but the
formula is only safe because the exchange-side catastrophic stop survives
restarts (the two doctrines are load-bearing on each other).

Mode 2 RESCUE (SHADOW ONLY — hedge_rescue_live stays False): the naive
−15%/100%/8%-trail rescue spec was REFUTED by the 2026-09-19 counterfactual
economics (negative-EV in 3 of 4 scenarios). A long in loss ≥
hedge_rescue_min_time_s with ROE ≤ hedge_rescue_roe_trigger_pct AND drift
confirmation (mover_relief armed / funding carry adverse / ByBit OI rising)
emits hedge_rescue_would_arm telemetry + a shadow registry row; the de-hedge
model (cover at pair_net ≥ 0 OR short-profit giveback ≥ 50% from peak) emits
hedge_rescue_would_cover. ZERO order actions, ever.

Dynamic short limit (P3 wires it into the order router): S_max =
max(hedge_short_floor_usd, hedge_short_upnl_frac × Σ max(0, upnl_long_i));
each plan must fit S_max − current_short_notional or it is blocked with
hedge_short_limit_blocked.

The brain is pure: no I/O, no imports from main.py. Every decision is an
Action dataclass; the main-loop executor owns each exchange/registry/log
side effect and reports results back through the narrow feedback methods
(on_order_placed / on_place_failed / on_protection_ok / on_close_confirmed).

Market-fork doctrine (Governor 2026-09-20, budget_sizing_enabled): "the same
movement we suffer profits on the other end — we are forking the market."
Three modes on one spine, every hedge funded ONLY by a budget the primary
position itself defined, never by principal:
  GREEN (profit-lock): rung trigger; budget = locked floor above the stop +
  haircut × open profit; notional = remaining budget / 2% stop.
  RED (pain-harvest): mark ≥ 50% of the way entry→stop, stop not hit,
  thesis intact; budget = designed risk (entry−stop)×qty × haircut.
  FORK-RIDE: thesis positively broken while hedged → cancel the 1.5% TP,
  stop ratchets to breakeven (the fork cannot lose), ride the flush, cover
  on 30% giveback from peak short profit.
Geometry per fill: stop = fill×1.02, reduce-only TP = fill×0.985, no trail;
covers re-arm after rearm_cooloff_s while budget and harvests (cap 4)
remain. A broken thesis never ARMS a new hedge — the fork only rides one
already on. Worst case documented: GREEN surrenders ≤ haircut × open
profit; RED ≤ (1 + haircut) × designed per-trade risk. Hedge P&L per cycle
is inferred from the cover-side mark — bounded by the [TP, stop] band.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from intelligence.roe_ratchet import roe_pct

# ── Plan states ──────────────────────────────────────────────────────────────
# armed → (limit placed) → chasing → (fill) → filled → (protection ok) →
# protected → (exchange cover / primary closed) → covered
# chasing → (step cap) → converting → (fill) → filled | (slippage/failure) →
# aborted.  Shadow rescue: shadow_armed → shadow_covered.
STATE_ARMED = "armed"
STATE_CHASING = "chasing"
STATE_CONVERTING = "converting"
STATE_FILLED = "filled"
STATE_PROTECTED = "protected"
STATE_CLOSING = "closing"
STATE_COVERED = "covered"
STATE_ABORTED = "aborted"
STATE_SHADOW_ARMED = "shadow_armed"
STATE_SHADOW_COVERED = "shadow_covered"

ACTIVE_STATES = (STATE_ARMED, STATE_CHASING, STATE_CONVERTING, STATE_FILLED,
                 STATE_PROTECTED, STATE_CLOSING)
LIVE_PLAN_STATES = (STATE_CHASING, STATE_CONVERTING, STATE_FILLED,
                    STATE_PROTECTED, STATE_CLOSING)

# Action kinds that touch the exchange — the executor guards these behind
# hedge_enabled + bybit_enabled + wrapper presence. Rescue NEVER emits one.
ORDER_ACTION_KINDS = frozenset({
    "place_limit_short", "amend_short", "convert_market_short",
    "set_protection", "close_short_market", "cancel_order"})


@dataclass
class HedgeKnobs:
    """All operator-tunable constants (mirrored from core/config.py hedge
    block — WHY comments live there). Defaults match the 2026-09-19 spec."""
    rungs: Tuple[Tuple[float, float], ...] = (
        (0.30, 0.03), (0.50, 0.06), (0.70, 0.10), (1.00, 0.15))
    min_notional: float = 6.0
    min_basis_bp: float = -2.0
    leverage_min: int = 5
    leverage_max: int = 15
    mmr: float = 0.01               # conservative maintenance-margin estimate
    liq_buffer: float = 1.25        # liq_distance ≥ buffer × stop_distance
    trail_pct: float = 0.08
    catastrophic_stop_pct: float = 0.18
    chase_max_steps: int = 3
    chase_reprice_pct: float = 0.0025
    chase_min_amend_s: float = 15.0
    chase_abandon_atr_mult: float = 1.0
    slippage_cap: float = 0.001
    tick_est_pct: float = 0.0005    # fallback per-symbol tick estimate
    spread_est_pct: float = 0.0005  # fallback per-symbol spread estimate
    whipsaw_cooloff_s: float = 7200.0
    short_floor_usd: float = 20.0
    short_upnl_frac: float = 0.5
    rescue_min_time_s: float = 3600.0
    rescue_roe_trigger_pct: float = -10.0
    rescue_giveback_frac: float = 0.5
    hedge_venue: str = "bybit"
    armed_retry_s: float = 30.0     # re-emit a dropped place after this age
    protection_fail_max: int = 3    # D52 S3: consecutive protection rejections
                                    # before the trail latches unavailable;
                                    # 0 = never latch (legacy re-emit forever)
    # ── Market-fork budget doctrine (Governor 2026-09-20) ──────────────────
    # "The same movement we suffer profits on the other end." Every hedge is
    # funded by a budget the PRIMARY position itself defined — never by
    # principal. budget_sizing_enabled=False = legacy rung sizing bit-for-bit.
    budget_sizing_enabled: bool = False   # brain default = legacy; the live
                                    # arming lives in core/config.py (False
                                    # here keeps the pre-doctrine system
                                    # bit-for-bit for every legacy caller)
    hedge_stop_pct: float = 0.02    # hedge-leg stop: fill × (1+2%); worst-case
                                    # spend per cycle = notional × this
    hedge_tp_pct: float = 0.015     # oscillation TP: fill × (1−1.5%)
    budget_haircut: float = 0.7     # fraction of open profit / designed risk
                                    # that may fund hedge spend
    pain_mode_enabled: bool = True  # RED: harvest the approach to the stop
    pain_trigger_frac: float = 0.5  # (entry−mark)/(entry−stop) ≥ this arms RED
    pain_max_ratio: float = 1.0     # RED notional ≤ primary notional × this
    rearm_cooloff_s: float = 900.0  # budget-mode cover → re-arm delay (the
                                    # legacy 7200s whipsaw cooloff stays for
                                    # legacy rung plans)
    max_harvests_per_primary: int = 4   # total cover cycles per primary leg
    fork_ride_enabled: bool = True  # thesis breaks while hedged → cancel the
                                    # TP, stop→breakeven, ride the flush and
                                    # cover on short-profit giveback (the
                                    # rescue de-hedge model, live)
    fork_ride_giveback_frac: float = 0.30   # FM-1: cover EARLY — 30% retrace
                                    # of peak short profit banks the flush
    max_hedge_hold_s: float = 172800.0  # FM-4: force-cover a budget hedge
                                    # still open after 48h (funding bleed
                                    # bound); 0 = never
    hedge_stop_atr_mult: float = 1.0    # vol-honest stop (Governor 2026-09-20):
                                    # stop_frac = max(hedge_stop_pct,
                                    # mult×ATR15/mark). One ATR read flows into
                                    # notional, leverage and TP — violent
                                    # markets get smaller, wider hedges; quiet
                                    # markets stay bit-for-bit at the floor
    tp1_approach_frac: float = 0.9      # pre-TP1 GREEN trigger: arm when the
                                    # mark has covered ≥ this fraction of the
                                    # entry→TP1 distance (the pyramid adds at
                                    # local tops — the fork shorts the retrace
                                    # the add suffers through); 0 = disabled


@dataclass
class HedgePlan:
    plan_id: str
    symbol: str
    side: str = "short"                 # hedges are shorts in v1
    state: str = STATE_ARMED
    shadow: bool = False
    primary_opened_at: int = 0          # primary-leg identity (opened_at_ms)
    primary_entry: float = 0.0
    primary_qty: float = 0.0
    primary_closed: bool = False
    qty: float = 0.0                    # planned → actual on fill
    hedge_ratio: float = 0.0
    rung_roe: float = 0.0               # highest rung crossed (ratchets up)
    leverage: int = 0
    limit_price: float = 0.0            # current resting chase price
    order_id: str = ""
    chase_steps: int = 0
    last_amend_ts: float = 0.0
    created_ts: float = 0.0
    arm_mark: float = 0.0               # hedge-venue mark at arming (chase thesis anchor)
    arm_atr: float = 0.0                # ATR in price units at arming (abandon yardstick)
    entry_price: float = 0.0            # filled price
    trail_dist_abs: float = 0.0
    catastrophic_stop: float = 0.0
    peak_short_frac: float = 0.0        # rescue shadow: peak short profit
    protection_last_emit: float = 0.0
    protection_fail_count: int = 0      # D52 S3: consecutive protection rejects
    protection_unavailable: bool = False  # latched — stop re-emitting
    # ── market-fork budget doctrine ─────────────────────────────────────────
    mode: str = ""                      # "green" | "pain" | "" legacy rungs
    tp_price: float = 0.0               # oscillation TP (budget mode)
    tp_order_id: str = ""
    riding: bool = False                # fork-ride: thesis broke, TP cancelled,
                                        # stop at breakeven, giveback cover
    stop_frac: float = 0.0              # budget mode: the ATR-scaled stop
                                        # fraction this plan was armed with
                                        # (S_max risk terms + fill geometry)


@dataclass
class LongCtx:
    """One primary-book long (hedge candidates are longs only in v1)."""
    symbol: str
    side: str
    entry_price: float
    qty: float
    leverage: float
    mark: float
    opened_at_ms: int
    age_s: float
    stop_price: float = 0.0             # primary's live stop (budget doctrine:
                                        # the stop DEFINES the hedge budget)
    tp1_price: float = 0.0              # primary's TP1 (tp1_approach trigger:
                                        # 0 = unknown, trigger abstains)


@dataclass
class BookCtx:
    """Everything the brain may read for one evaluate tick. hedge_positions =
    hedge-venue short snapshot {symbol: {"qty", "entry"}}; None = UNKNOWN
    (poll failed / not taken) — fill/cover detection is skipped, never read
    as "gone". enabled=False is the master kill switch: zero actions."""
    now: float
    longs: List[LongCtx] = field(default_factory=list)
    marks: Dict[str, float] = field(default_factory=dict)
    hedge_marks: Dict[str, float] = field(default_factory=dict)
    basis_bp: Dict[str, float] = field(default_factory=dict)
    basis_stressed: Dict[str, bool] = field(default_factory=dict)
    funding_carry_adverse: Dict[str, bool] = field(default_factory=dict)
    mover_relief: Set[str] = field(default_factory=set)
    oi_rising: Dict[str, bool] = field(default_factory=dict)
    hedge_positions: Optional[Dict[str, Dict[str, float]]] = None
    tick_est_pct: Dict[str, float] = field(default_factory=dict)
    spread_est: Dict[str, float] = field(default_factory=dict)
    atr_abs: Dict[str, float] = field(default_factory=dict)
    hedge_free_equity: float = 0.0      # cross-margin free equity; 0 = unknown → isolated-only
    enabled: bool = False
    profit_lock_enabled: bool = True
    rescue_shadow_enabled: bool = True
    thesis_intact: Dict[str, bool] = field(default_factory=dict)
                                        # fork doctrine: False = trend verdict
                                        # positively AGAINST the held side
                                        # (executor-derived); missing key =
                                        # abstain, treated as intact


@dataclass
class Action:
    kind: str
    plan_id: str = ""
    symbol: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


# ── Pure helpers ─────────────────────────────────────────────────────────────

def derive_hedge_leverage(stop_frac: float, notional: float = 0.0,
                          free_equity: float = 0.0, mmr: float = 0.01,
                          liq_buffer: float = 1.25, lev_min: int = 5,
                          lev_max: int = 15) -> int:
    """Cross-buffer leverage (2026-09-19 Governor formula):
        cross_buffer  = free_equity / notional   (fraction of the position the
                        account's free equity absorbs; 0 when unknown)
        min_clearance = liq_buffer × stop_frac + mmr − cross_buffer
        min_clearance ≤ 0  → cross buffer alone covers → lev_max
        else lev = int(1 / min_clearance), clamped to [lev_min, lev_max];
        < lev_min → 0 = unhedgeable (the caller skips the plan).
    The formula is only safe because the exchange-side catastrophic stop
    survives restarts — it and the derived leverage are a load-bearing pair.
    free_equity=0 (unknown) derives isolated-only, fail-closed."""
    try:
        sf = float(stop_frac)
    except (TypeError, ValueError):
        return 0
    if sf <= 0:
        return 0
    cross_buffer = 0.0
    try:
        if notional > 0 and free_equity > 0:
            cross_buffer = float(free_equity) / float(notional)
    except (TypeError, ValueError):
        cross_buffer = 0.0
    min_clearance = float(liq_buffer) * sf + float(mmr) - cross_buffer
    if min_clearance <= 0:
        return int(lev_max)
    lev = int(1.0 / min_clearance)
    if lev > int(lev_max):
        lev = int(lev_max)
    if lev < int(lev_min):
        return 0
    return lev


def rung_for_roe(rungs, roe: float) -> Optional[Tuple[float, float]]:
    """Highest (rung_roe, hedge_ratio) the ROE has crossed, or None below
    the first rung. Rungs are (roe_pct, ratio) pairs on the roe_ratchet
    house-ROE basis (percent)."""
    best: Optional[Tuple[float, float]] = None
    for rung_roe, ratio in sorted(rungs or ()):
        try:
            if float(roe) >= float(rung_roe):
                best = (float(rung_roe), float(ratio))
        except (TypeError, ValueError):
            continue
    return best


def short_limit_cap(floor_usd: float, upnl_frac: float,
                    long_upnls) -> float:
    """S_max = max(floor, frac × Σ max(0, upnl_long_i)) — the hedge book is
    funded by open-long profits, never by margin hope."""
    pos = 0.0
    for u in (long_upnls or []):
        try:
            pos += max(0.0, float(u))
        except (TypeError, ValueError):
            continue
    return max(float(floor_usd), float(upnl_frac) * pos)


def match_hedge_orphans(hedge_positions: List[dict],
                        primary_symbols: Set[str],
                        known_symbols: Set[str]) -> dict:
    """Boot-rebuild matcher (2026-09-19): the registry is memory-only, so a
    hedge short filled pre-restart is orphaned at boot. Sort each live hedge
    leg: symbol has an open primary AND is unknown to the registry → "adopt"
    (the manager rebuilds a coverable plan for it); no primary AND unknown →
    "unmatched" (human/Governor decision — never auto-covered, it may be an
    operator leg); already in the registry → omitted (the plan exists)."""
    out = {"adopt": [], "unmatched": []}
    for pos in (hedge_positions or []):
        sym = pos.get("symbol")
        if not sym or sym in (known_symbols or set()):
            continue
        if sym in (primary_symbols or set()):
            out["adopt"].append(pos)
        else:
            out["unmatched"].append(pos)
    return out


class HedgeManager:
    """The hedge brain. Holds plan state machines; evaluate() returns the
    full action list for one tick. Zero I/O — the executor feeds exchange
    truth back through BookCtx.hedge_positions and the feedback methods."""

    def __init__(self, knobs: Optional[HedgeKnobs] = None):
        self.knobs = knobs or HedgeKnobs()
        self._plans: Dict[str, HedgePlan] = {}
        self._cooldowns: Dict[str, float] = {}   # symbol → whipsaw free until
        self._block_fired: Set[Tuple[str, int, str]] = set()  # rising-edge
        # Budget doctrine state, keyed f"{symbol}-{primary_opened_at_ms}":
        self._budget_spent: Dict[str, float] = {}
        self._harvest_count: Dict[str, int] = {}
        self._rearm_after: Dict[str, float] = {}   # symbol → budget re-arm ts

    # ── introspection (executor) ─────────────────────────────────────────────

    def has_live_plans(self) -> bool:
        """True when any plan expects/needs exchange truth — the executor
        only polls hedge-venue positions when this holds."""
        return any((not p.shadow) and p.state in LIVE_PLAN_STATES
                   for p in self._plans.values())

    def plans(self) -> List[HedgePlan]:
        return list(self._plans.values())

    def _active_plan_for(self, symbol: str) -> Optional[HedgePlan]:
        for p in self._plans.values():
            if not p.shadow and p.symbol == symbol and p.state in ACTIVE_STATES:
                return p
        return None

    # ── action constructors ──────────────────────────────────────────────────

    @staticmethod
    def _event(name: str, plan: Optional[HedgePlan] = None,
               symbol: str = "", **fields) -> Action:
        data: Dict[str, Any] = {}
        if plan is not None:
            data["plan_id"] = plan.plan_id
            data["symbol"] = plan.symbol
        elif symbol:
            data["symbol"] = symbol
        data.update(fields)
        return Action("event", getattr(plan, "plan_id", ""),
                      getattr(plan, "symbol", symbol),
                      {"name": name, "fields": data})

    @staticmethod
    def _reg_update(plan: HedgePlan, fields: Dict[str, Any]) -> Action:
        return Action("registry_update", plan.plan_id, plan.symbol,
                      {"pair_id": plan.plan_id, "fields": fields})

    def _reg_upsert(self, plan: HedgePlan, state: str) -> Action:
        return Action("registry_upsert", plan.plan_id, plan.symbol, {
            "pair_id": plan.plan_id, "symbol": plan.symbol,
            "venue": self.knobs.hedge_venue, "side": plan.side,
            "qty": plan.qty,
            "entry_price": plan.entry_price or plan.limit_price,
            "state": state, "hedge_of": plan.plan_id})

    def _block_once(self, symbol: str, opened_at: int, kind: str,
                    event: str, **extra) -> List[Action]:
        """Rising-edge block telemetry — once per primary-leg identity."""
        key = (symbol, int(opened_at or 0), kind)
        if key in self._block_fired:
            return []
        self._block_fired.add(key)
        return [self._event(event, symbol=symbol, reason=kind, **extra)]

    # ── feedback methods (executor → brain) ──────────────────────────────────

    def on_order_placed(self, plan_id: str, order_id: str) -> None:
        plan = self._plans.get(plan_id)
        if plan is not None and plan.state == STATE_ARMED:
            plan.order_id = str(order_id or "")
            plan.state = STATE_CHASING

    def on_tp_placed(self, plan_id: str, order_id: str) -> None:
        plan = self._plans.get(plan_id)
        if plan is not None:
            plan.tp_order_id = str(order_id or "")

    def on_place_failed(self, plan_id: str, reason: str = "rejected"
                        ) -> List[Action]:
        plan = self._plans.get(plan_id)
        if plan is None or plan.state in (STATE_COVERED, STATE_ABORTED):
            return []
        plan.state = STATE_ABORTED
        return [self._reg_update(plan, {"state": "aborted"}),
                self._event("hedge_aborted", plan, reason=reason)]

    def on_protection_ok(self, plan_id: str) -> List[Action]:
        plan = self._plans.get(plan_id)
        if plan is None or plan.state != STATE_FILLED:
            return []
        plan.state = STATE_PROTECTED
        return [self._event("hedge_protected", plan,
                            trail_abs=plan.trail_dist_abs,
                            catastrophic_stop=plan.catastrophic_stop)]

    def on_protection_failed(self, plan_id: str, reason: str = "rejected",
                             stop_confirmed: bool = False) -> List[Action]:
        """D52 S3 failure latch (2026-09-19): the confirm gate required BOTH
        trail and stop True, so venue rejections kept the plan re-emitting
        every 20s forever (hedge_protected 0 lifetime, 662 combined rejects).
        After protection_fail_max consecutive failures the trail latches
        unavailable and re-emission stops. A latched trail with a CONFIRMED
        catastrophic stop still completes protection — the venue holds the
        disaster floor; only the trail is given up. protection_fail_max=0
        disables the latch (legacy bit-for-bit)."""
        plan = self._plans.get(plan_id)
        if plan is None or plan.state != STATE_FILLED:
            return []
        plan.protection_fail_count += 1
        _max = int(getattr(self.knobs, "protection_fail_max", 3))
        if _max <= 0 or plan.protection_fail_count < _max:
            return []
        plan.protection_unavailable = True
        out = [self._event("hedge_trail_unavailable", plan, reason=reason,
                           failures=plan.protection_fail_count,
                           stop_confirmed=stop_confirmed)]
        if stop_confirmed:
            plan.state = STATE_PROTECTED
            out.append(self._reg_update(plan, {"state": "protected",
                                               "trail": "unavailable"}))
            out.append(self._event("hedge_protected", plan,
                                   trail_abs=0.0,
                                   trail_state="latched_unavailable",
                                   catastrophic_stop=plan.catastrophic_stop))
        return out

    def on_close_confirmed(self, plan_id: str,
                           ctx: Optional["BookCtx"] = None) -> List[Action]:
        plan = self._plans.get(plan_id)
        if plan is None or plan.state != STATE_CLOSING:
            return []
        plan.state = STATE_COVERED
        out = [self._reg_update(plan, {"state": "covered"})]
        if plan.mode in ("green", "pain") and plan.entry_price > 0:
            # Bot-initiated cover (fork-ride giveback / hold expiry /
            # primary-closed): same bounded P&L inference as exchange covers.
            key = f"{plan.symbol}-{plan.primary_opened_at}"
            mark = 0.0
            if ctx is not None:
                mark = float(ctx.hedge_marks.get(plan.symbol, 0.0) or 0.0) \
                    or float(ctx.marks.get(plan.symbol, 0.0) or 0.0)
            if mark > 0:
                pnl_frac = (plan.entry_price - mark) / plan.entry_price
                spend = max(0.0, -pnl_frac) * plan.entry_price * plan.qty
                self._budget_spent[key] = self._budget_spent.get(key, 0.0) \
                    + spend
            self._harvest_count[key] = self._harvest_count.get(key, 0) + 1
            self._rearm_after[plan.symbol] = time.time() + float(
                self.knobs.rearm_cooloff_s)
            out.append(self._event(
                "hedge_harvest_covered", plan, mode=plan.mode,
                budget_spent=round(self._budget_spent.get(key, 0.0), 4),
                harvests=self._harvest_count[key],
                rearm_s=float(self.knobs.rearm_cooloff_s)))
        return out

    # ── boot rebuild ─────────────────────────────────────────────────────────

    def adopt_plan_at_boot(self, symbol: str, qty: float, entry_price: float,
                           leverage: int) -> HedgePlan:
        """Rebuild a PROTECTED plan around a pre-restart hedge fill (the
        registry is memory-only; the exchange-side trail/catastrophic stops
        survived the restart, so the leg already carries protection — the
        plan skips armed/chase/filled and lands straight in protected).
        primary_opened_at stays 0 = identity unknown; evaluate() binds it to
        the live primary long on first sight. The adopted plan is coverable
        by the normal primary-closed unwind and leg-gone paths."""
        now = time.time()
        plan = HedgePlan(plan_id=f"boot-{symbol}-{int(now)}", symbol=symbol,
                         state=STATE_FILLED, qty=float(qty),
                         entry_price=float(entry_price),
                         leverage=int(leverage), created_ts=now,
                         protection_last_emit=now)
        plan.trail_dist_abs = self.knobs.trail_pct * plan.entry_price
        plan.catastrophic_stop = plan.entry_price * (
            1.0 + self.knobs.catastrophic_stop_pct)
        self._plans[plan.plan_id] = plan
        self.on_protection_ok(plan.plan_id)   # FILLED → PROTECTED; the event
                                              # is dropped (no executor at boot)
        return plan

    # ── internal transitions ─────────────────────────────────────────────────

    def _on_fill(self, plan: HedgePlan, price: float, qty: float,
                 ctx: BookCtx) -> List[Action]:
        plan.entry_price = float(price)
        plan.qty = float(qty)
        # Market-conversion slippage verdict (the one place fill price is
        # judged): beyond the cap the plan aborts and the accidental leg is
        # closed at market immediately.
        if plan.state == STATE_CONVERTING and not plan.primary_closed:
            mark = ctx.hedge_marks.get(plan.symbol, 0.0) or \
                ctx.marks.get(plan.symbol, 0.0)
            if mark > 0 and abs(price / mark - 1.0) > self.knobs.slippage_cap:
                plan.state = STATE_CLOSING
                return [Action("close_short_market", plan.plan_id, plan.symbol,
                               {"qty": qty, "reason": "abort_slippage"}),
                        self._reg_update(plan, {"qty": qty,
                                                "entry_price": price,
                                                "state": "closing"}),
                        self._event("hedge_aborted", plan,
                                    reason="slippage_cap_exceeded",
                                    fill_price=price, mark=mark)]
        if plan.primary_closed:
            # Primary died mid-entry — flatten, never protect.
            plan.state = STATE_CLOSING
            return [Action("close_short_market", plan.plan_id, plan.symbol,
                           {"qty": qty, "reason": "primary_closed"}),
                    self._reg_update(plan, {"qty": qty, "entry_price": price,
                                            "state": "closing"})]
        plan.state = STATE_FILLED
        if plan.mode in ("green", "pain"):
            # Budget geometry: ATR-scaled stop + ratio-preserved reduce-only
            # TP, NO trail — the oscillation cycles bank and re-arm;
            # FORK-RIDE owns the flush case. tp_frac scales with stop_frac so
            # the harvest R-multiple is vol-invariant (0.75R by default).
            _sf = (plan.stop_frac if plan.stop_frac > 0
                   else float(self.knobs.hedge_stop_pct))
            _tf = float(self.knobs.hedge_tp_pct) * (
                _sf / float(self.knobs.hedge_stop_pct))
            plan.trail_dist_abs = 0.0
            plan.catastrophic_stop = price * (1.0 + _sf)
            plan.tp_price = price * (1.0 - _tf)
        else:
            plan.trail_dist_abs = self.knobs.trail_pct * price
            plan.catastrophic_stop = price * (
                1.0 + self.knobs.catastrophic_stop_pct)
        plan.protection_last_emit = ctx.now
        return [self._reg_update(plan, {"qty": qty, "entry_price": price,
                                        "state": "open"}),
                self._event("hedge_filled", plan, fill_price=price, qty=qty,
                            leverage=plan.leverage, mode=plan.mode,
                            tp_price=plan.tp_price),
                Action("set_protection", plan.plan_id, plan.symbol,
                       {"trail_abs": plan.trail_dist_abs,
                        "active_price": price,
                        "catastrophic_stop": plan.catastrophic_stop,
                        "tp_price": plan.tp_price, "tp_qty": qty})]

    def _on_leg_gone(self, plan: HedgePlan, now: float,
                     ctx: Optional[BookCtx] = None) -> List[Action]:
        """Exchange-side cover. Legacy rung plans: whipsaw cooloff (bit-for-
        bit). Budget plans: the cycle's P&L is inferred from the cover-side
        mark (the true fill is inside [TP, stop] by construction — the
        inference error is bounded by that band), the spend ledger and
        harvest counter for this primary leg are updated, the resting TP is
        cancelled (a stop-hit cover leaves it dangling), and the symbol
        re-arms after rearm_cooloff_s while budget and harvests remain."""
        plan.state = STATE_COVERED
        if plan.mode in ("green", "pain") and plan.entry_price > 0:
            key = f"{plan.symbol}-{plan.primary_opened_at}"
            mark = 0.0
            if ctx is not None:
                mark = float(ctx.hedge_marks.get(plan.symbol, 0.0) or 0.0) \
                    or float(ctx.marks.get(plan.symbol, 0.0) or 0.0)
            pnl_frac = 0.0
            if mark > 0:
                pnl_frac = (plan.entry_price - mark) / plan.entry_price
                spend = max(0.0, -pnl_frac) * plan.entry_price * plan.qty
                self._budget_spent[key] = self._budget_spent.get(key, 0.0) \
                    + spend
            self._harvest_count[key] = self._harvest_count.get(key, 0) + 1
            self._rearm_after[plan.symbol] = now + float(
                self.knobs.rearm_cooloff_s)
            out = [self._reg_update(plan, {"state": "covered"})]
            if plan.tp_order_id:
                out.append(Action("cancel_order", plan.plan_id, plan.symbol,
                                  {"order_id": plan.tp_order_id}))
            out.append(self._event(
                "hedge_harvest_covered", plan, mode=plan.mode,
                inferred_pnl_frac=round(pnl_frac, 6),
                budget_spent=round(self._budget_spent[key], 4),
                harvests=self._harvest_count[key],
                rearm_s=float(self.knobs.rearm_cooloff_s)))
            return out
        self._cooldowns[plan.symbol] = now + self.knobs.whipsaw_cooloff_s
        return [self._reg_update(plan, {"state": "covered"}),
                self._event("hedge_leg_covered", plan,
                            reason="exchange_cover",
                            cooloff_s=self.knobs.whipsaw_cooloff_s)]

    def _unwind(self, plan: HedgePlan) -> List[Action]:
        """Primary leg closed for ANY reason → the hedge leg follows at
        market immediately (chasing = cancel the resting limit)."""
        if plan.state == STATE_ARMED:
            plan.state = STATE_COVERED
            return [self._reg_update(plan, {"state": "covered"}),
                    self._event("hedge_cover_primary_closed", plan,
                                reason="primary_closed_pre_fill")]
        if plan.state == STATE_CHASING:
            plan.state = STATE_COVERED
            return [Action("cancel_order", plan.plan_id, plan.symbol,
                           {"order_id": plan.order_id}),
                    self._reg_update(plan, {"state": "covered"}),
                    self._event("hedge_cover_primary_closed", plan,
                                reason="primary_closed_chasing")]
        if plan.state == STATE_CONVERTING:
            plan.primary_closed = True   # fill handler flattens on arrival
            return []
        if plan.state in (STATE_FILLED, STATE_PROTECTED, STATE_CLOSING):
            plan.state = STATE_CLOSING
            out = [Action("close_short_market", plan.plan_id, plan.symbol,
                          {"qty": plan.qty, "reason": "primary_closed"}),
                   self._reg_update(plan, {"state": "closing"}),
                   self._event("hedge_cover_primary_closed", plan,
                               reason="primary_closed")]
            if plan.tp_order_id:
                out.append(Action("cancel_order", plan.plan_id, plan.symbol,
                                  {"order_id": plan.tp_order_id}))
            return out
        return []

    # ── market-fork budget helpers ───────────────────────────────────────────

    def _budget_key(self, lng: LongCtx) -> str:
        return f"{lng.symbol}-{lng.opened_at_ms}"

    def _green_budget(self, lng: LongCtx) -> float:
        """GREEN (profit-lock): the hedge may spend AT MOST the profit the
        position has already locked plus the haircut share of what is still
        open. locked_floor = guaranteed profit above the stop (roe_ratchet
        breakeven/ratchet stops make this positive); open_profit = the
        unbanked remainder. Worst case per doctrine: give back the haircut
        share of open profit — never the locked floor, never principal."""
        floor = max(0.0, lng.stop_price - lng.entry_price) * lng.qty
        open_profit = (max(0.0, lng.mark - max(lng.stop_price,
                                               lng.entry_price)) * lng.qty)
        return floor + float(self.knobs.budget_haircut) * open_profit

    def _pain_budget(self, lng: LongCtx) -> float:
        """RED (pain-harvest): the TOTAL designed risk of the trade — the
        distance to the stop — funds the harvest. Worst case: hedge spends
        the haircut share AND the primary stops out = 1 + haircut × designed
        risk. Documented and bounded; never principal beyond the trade's own
        stop budget."""
        designed_risk = (lng.entry_price - lng.stop_price) * lng.qty
        return max(0.0, designed_risk) * float(self.knobs.budget_haircut)

    def _arm_budget_plan(self, lng: LongCtx, ctx: BookCtx, mode: str,
                         budget_total: float, trigger: str
                         ) -> List[Action]:
        """Shared arming path for GREEN/RED budget plans. Gates (in order):
        re-arm cooloff, harvest cap, dual-mark, budget remaining → notional,
        min-notional, basis, cross-buffer leverage on the 2% stop, S_max."""
        sym = lng.symbol
        key = self._budget_key(lng)
        if ctx.thesis_intact.get(sym, True) is False:
            return []   # orchestration: a broken thesis never ARMS — the
                        # fork only rides a hedge already on
        if ctx.now < self._rearm_after.get(sym, 0.0):
            return []
        if self._harvest_count.get(key, 0) >= int(
                self.knobs.max_harvests_per_primary):
            return self._block_once(sym, lng.opened_at_ms, "harvest_cap",
                                    "hedge_budget_standdown",
                                    harvests=self._harvest_count.get(key, 0))
        hedge_mark = float(ctx.hedge_marks.get(sym, 0.0) or 0.0)
        if hedge_mark <= 0:
            return []
        # Vol-honest stop (Governor 2026-09-20): a hardcoded 2% stop sits
        # inside one violent-market 5m wick — one stop-out spends the ENTIRE
        # remaining budget (notional = remaining/stop_frac). The ATR-scaled
        # stop survives the wick; notional and leverage shrink to match, so
        # budget risk per cycle is constant in any vol regime (P1b
        # constant-risk doctrine, hedge leg). Quiet markets bind the floor.
        _atr = float(ctx.atr_abs.get(sym, 0.0) or 0.0)
        stop_frac = float(self.knobs.hedge_stop_pct)
        if _atr > 0:
            stop_frac = max(stop_frac, float(self.knobs.hedge_stop_atr_mult)
                            * _atr / hedge_mark)
        spent = self._budget_spent.get(key, 0.0)
        remaining = budget_total - spent
        notional = remaining / stop_frac
        if mode == "pain":
            notional = min(notional,
                           lng.qty * hedge_mark
                           * float(self.knobs.pain_max_ratio))
        if notional < self.knobs.min_notional:
            return self._block_once(
                sym, lng.opened_at_ms, "budget_exhausted",
                "hedge_budget_standdown", mode=mode,
                budget_total=round(budget_total, 4),
                spent=round(spent, 4))
        if ctx.basis_stressed.get(sym, False):
            return self._block_once(sym, lng.opened_at_ms, "basis_stressed",
                                    "hedge_basis_blocked")
        if float(ctx.basis_bp.get(sym, 0.0)) < self.knobs.min_basis_bp:
            return self._block_once(
                sym, lng.opened_at_ms, "basis_below_min", "hedge_basis_blocked",
                basis_bp=round(float(ctx.basis_bp.get(sym, 0.0)), 2),
                min_basis_bp=self.knobs.min_basis_bp)
        lev = derive_hedge_leverage(
            stop_frac, notional,
            ctx.hedge_free_equity, self.knobs.mmr,
            self.knobs.liq_buffer, self.knobs.leverage_min,
            self.knobs.leverage_max)
        if lev < self.knobs.leverage_min:
            return self._block_once(sym, lng.opened_at_ms, "unhedgeable",
                                    "hedge_unhedgeable_skip",
                                    stop_frac=stop_frac)
        long_upnls = [(l.mark - l.entry_price) * l.qty
                      for l in ctx.longs if l.side == "long"]
        s_max = short_limit_cap(self.knobs.short_floor_usd,
                                self.knobs.short_upnl_frac, long_upnls)
        # Risk terms, not notional: a fork hedge risks stop_frac of its
        # notional — comparing raw notional to S_max would starve the fork
        # 100× (S_max was calibrated to the legacy 18% catastrophic stop).
        cur_risk = 0.0
        for p in self._plans.values():
            if p.shadow or p.state not in ACTIVE_STATES:
                continue
            pm = float(ctx.hedge_marks.get(p.symbol, 0.0) or 0.0) or \
                p.entry_price or p.limit_price
            p_frac = ((p.stop_frac if p.stop_frac > 0
                       else float(self.knobs.hedge_stop_pct))
                      if p.mode in ("green", "pain")
                      else float(self.knobs.catastrophic_stop_pct))
            cur_risk += p.qty * pm * p_frac
        if notional * stop_frac > s_max - cur_risk:
            return self._block_once(
                sym, lng.opened_at_ms, "short_limit",
                "hedge_short_limit_blocked",
                s_max=round(s_max, 2), current_risk=round(cur_risk, 2),
                plan_risk=round(notional * stop_frac, 2))
        qty = notional / hedge_mark
        plan_id = f"hfk-{mode[0]}-{sym}-{lng.opened_at_ms}"
        tick = float(ctx.tick_est_pct.get(sym, self.knobs.tick_est_pct))
        limit_price = hedge_mark * (1.0 + tick)
        plan = HedgePlan(plan_id=plan_id, symbol=sym, state=STATE_ARMED,
                         primary_opened_at=lng.opened_at_ms,
                         primary_entry=lng.entry_price, primary_qty=lng.qty,
                         qty=qty, leverage=lev, limit_price=limit_price,
                         created_ts=ctx.now, last_amend_ts=ctx.now,
                         arm_mark=hedge_mark,
                         arm_atr=float(ctx.atr_abs.get(sym, 0.0) or 0.0),
                         mode=mode, stop_frac=stop_frac)
        self._plans[plan_id] = plan
        return [self._reg_upsert(plan, "armed"),
                self._event("hedge_budget_armed", plan, mode=mode,
                            trigger=trigger,
                            budget_total=round(budget_total, 4),
                            spent=round(spent, 4), qty=qty, leverage=lev,
                            limit_price=limit_price,
                            stop_frac=round(stop_frac, 6)),
                Action("place_limit_short", plan_id, sym,
                       {"qty": qty, "price": limit_price, "leverage": lev})]

    # ── per-tick evaluations ─────────────────────────────────────────────────

    def _eval_profit_lock(self, lng: LongCtx, ctx: BookCtx) -> List[Action]:
        sym = lng.symbol
        if lng.side != "long":
            return []
        roe = roe_pct("long", lng.entry_price, lng.mark, lng.leverage)
        if roe is None:
            return []
        hit = rung_for_roe(self.knobs.rungs, roe)

        # ── Market-fork budget sizing (Governor 2026-09-20) ────────────────
        # Size comes from the position's own stop geometry, not the rung
        # ratio. TWO triggers: the ROE rung (price already proved itself) and
        # the tp1_approach (price at ~90% of the TP1 distance — the pyramid
        # adds at local tops, so the fork shorts the retrace the add suffers
        # through). budget_sizing_enabled=False = legacy rung-ratio path
        # bit-for-bit below.
        if self.knobs.budget_sizing_enabled:
            trigger = ""
            if hit is not None:
                trigger = f"rung_{hit[0]}"
            elif (lng.tp1_price > lng.entry_price > 0
                    and float(self.knobs.tp1_approach_frac) > 0):
                progress = ((lng.mark - lng.entry_price)
                            / (lng.tp1_price - lng.entry_price))
                if progress >= float(self.knobs.tp1_approach_frac):
                    trigger = f"tp1_approach_{progress:.2f}"
            if not trigger:
                return []
            if self._active_plan_for(sym) is not None:
                return []   # one fork plan per primary leg at a time
            if lng.stop_price <= 0 or lng.entry_price <= 0:
                return self._block_once(sym, lng.opened_at_ms, "no_stop",
                                        "hedge_budget_standdown",
                                        mode="green")
            return self._arm_budget_plan(lng, ctx, "green",
                                         self._green_budget(lng),
                                         trigger=trigger)

        if hit is None:
            return []
        rung_roe, ratio = hit

        plan = self._active_plan_for(sym)
        if plan is not None:
            # Rung ratchet: UP only. Pre-fill the target follows the higher
            # ratio; post-fill the rung is recorded but v1 does not resize.
            if rung_roe > plan.rung_roe:
                plan.rung_roe = rung_roe
                plan.hedge_ratio = ratio
                if plan.state in (STATE_ARMED, STATE_CHASING):
                    plan.qty = ratio * lng.qty
                    return [self._event("hedge_rung_ratchet", plan,
                                        rung_roe=rung_roe, ratio=ratio,
                                        qty=plan.qty)]
                return [self._event("hedge_rung_ratchet", plan,
                                    rung_roe=rung_roe, ratio=ratio,
                                    note="post_fill_no_resize")]
            return []

        # ── New plan: ALL arming gates (2026-09-19 spec) ─────────────────
        if ctx.now < self._cooldowns.get(sym, 0.0):
            return []   # whipsaw cooloff — silent by design
        hedge_mark = float(ctx.hedge_marks.get(sym, 0.0) or 0.0)
        if hedge_mark <= 0:
            return []   # dual-mark coverage required
        qty = ratio * lng.qty
        notional = qty * hedge_mark
        if notional < self.knobs.min_notional:
            return []   # below the ByBit minimum — nothing sensible to do
        if ctx.basis_stressed.get(sym, False):
            return self._block_once(sym, lng.opened_at_ms, "basis_stressed",
                                    "hedge_basis_blocked")
        if float(ctx.basis_bp.get(sym, 0.0)) < self.knobs.min_basis_bp:
            return self._block_once(
                sym, lng.opened_at_ms, "basis_below_min", "hedge_basis_blocked",
                basis_bp=round(float(ctx.basis_bp.get(sym, 0.0)), 2),
                min_basis_bp=self.knobs.min_basis_bp)
        lev = derive_hedge_leverage(
            self.knobs.catastrophic_stop_pct, notional,
            ctx.hedge_free_equity, self.knobs.mmr,
            self.knobs.liq_buffer, self.knobs.leverage_min,
            self.knobs.leverage_max)
        if lev < self.knobs.leverage_min:
            return self._block_once(sym, lng.opened_at_ms, "unhedgeable",
                                    "hedge_unhedgeable_skip",
                                    stop_frac=self.knobs.catastrophic_stop_pct)
        long_upnls = [(l.mark - l.entry_price) * l.qty
                      for l in ctx.longs if l.side == "long"]
        s_max = short_limit_cap(self.knobs.short_floor_usd,
                                self.knobs.short_upnl_frac, long_upnls)
        cur_short = 0.0
        for p in self._plans.values():
            if p.shadow or p.state not in ACTIVE_STATES:
                continue
            pm = float(ctx.hedge_marks.get(p.symbol, 0.0) or 0.0) or \
                p.entry_price or p.limit_price
            cur_short += p.qty * pm
        if notional > s_max - cur_short:
            return self._block_once(
                sym, lng.opened_at_ms, "short_limit",
                "hedge_short_limit_blocked",
                s_max=round(s_max, 2), current=round(cur_short, 2),
                plan_notional=round(notional, 2))

        plan_id = f"hpl-{sym}-{lng.opened_at_ms}"
        tick = float(ctx.tick_est_pct.get(sym, self.knobs.tick_est_pct))
        limit_price = hedge_mark * (1.0 + tick)
        plan = HedgePlan(plan_id=plan_id, symbol=sym, state=STATE_ARMED,
                         primary_opened_at=lng.opened_at_ms,
                         primary_entry=lng.entry_price, primary_qty=lng.qty,
                         qty=qty, hedge_ratio=ratio, rung_roe=rung_roe,
                         leverage=lev, limit_price=limit_price,
                         created_ts=ctx.now, last_amend_ts=ctx.now,
                         arm_mark=hedge_mark,
                         arm_atr=float(ctx.atr_abs.get(sym, 0.0) or 0.0))
        self._plans[plan_id] = plan
        return [self._reg_upsert(plan, "armed"),
                self._event("hedge_plan_armed", plan, rung_roe=rung_roe,
                            ratio=ratio, qty=qty, leverage=lev,
                            limit_price=limit_price, roe=round(roe, 3)),
                Action("place_limit_short", plan_id, sym,
                       {"qty": qty, "price": limit_price, "leverage": lev})]

    def _eval_pain(self, lng: LongCtx, ctx: BookCtx) -> List[Action]:
        """RED (pain-harvest, Governor 2026-09-20): "every time price
        approaches our stop or takes us red we hedge with very high leverage
        — the same movement we suffer profits on the other end." Arms when
        the mark has travelled ≥ pain_trigger_frac of the way from entry to
        the stop, the stop is NOT yet hit, and the thesis is still intact
        (a broken thesis is the exit stack's job, not a harvest)."""
        sym = lng.symbol
        if lng.side != "long":
            return []
        if not self.knobs.pain_mode_enabled:
            return []
        if self._active_plan_for(sym) is not None:
            return []
        if lng.stop_price <= 0 or lng.entry_price <= lng.stop_price:
            return []
        if lng.mark <= lng.stop_price or lng.mark >= lng.entry_price:
            return []   # stop hit (exit stack owns) or not in pain
        pain_frac = ((lng.entry_price - lng.mark)
                     / (lng.entry_price - lng.stop_price))
        if pain_frac < float(self.knobs.pain_trigger_frac):
            return []
        return self._arm_budget_plan(lng, ctx, "pain",
                                     self._pain_budget(lng),
                                     trigger=f"pain_{pain_frac:.2f}")

    def _eval_fork_ride(self, plan: HedgePlan, ctx: BookCtx) -> List[Action]:
        """FORK-RIDE (Governor 2026-09-20): "trend can change and there can
        be more profits on the hedge side if there is a flush out." A filled
        budget hedge whose primary's thesis is now BROKEN stops scalping and
        becomes the alpha leg: TP cancelled, stop ratcheted to breakeven (the
        fork can no longer lose), and the flush is ridden until the short
        profit retraces fork_ride_giveback_frac from its peak — the rescue
        de-hedge model, live. The primary-close unwind still outranks
        everything (a naked 15x short is never left behind)."""
        sym = plan.symbol
        if plan.state not in (STATE_FILLED, STATE_PROTECTED):
            return []
        mark = float(ctx.hedge_marks.get(sym, 0.0) or 0.0) or \
            float(ctx.marks.get(sym, 0.0) or 0.0)
        if not plan.riding:
            if not self.knobs.fork_ride_enabled:
                return []
            if plan.tp_price <= 0 or plan.entry_price <= 0:
                return []
            if ctx.thesis_intact.get(sym, True) is not False:
                return []
            plan.riding = True
            plan.catastrophic_stop = plan.entry_price   # breakeven — the
                                                        # fork cannot lose
            out: List[Action] = [self._reg_update(plan, {
                "state": "protected", "riding": True,
                "catastrophic_stop": plan.catastrophic_stop})]
            if plan.tp_order_id:
                out.append(Action("cancel_order", plan.plan_id, sym,
                                  {"order_id": plan.tp_order_id}))
            out.append(Action("set_protection", plan.plan_id, sym,
                              {"trail_abs": 0.0,
                               "active_price": plan.entry_price,
                               "catastrophic_stop": plan.catastrophic_stop,
                               "tp_price": 0.0}))
            out.append(self._event("hedge_fork_ride_armed", plan,
                                   entry=plan.entry_price,
                                   tp_cancelled=plan.tp_price))
            return out
        # Riding: track the peak and cover on giveback.
        if mark <= 0 or plan.entry_price <= 0:
            return []
        short_frac = (plan.entry_price - mark) / plan.entry_price
        plan.peak_short_frac = max(plan.peak_short_frac, short_frac)
        if (plan.peak_short_frac > 0
                and (plan.peak_short_frac - short_frac)
                >= float(self.knobs.fork_ride_giveback_frac)
                * plan.peak_short_frac):
            plan.state = STATE_CLOSING
            return [Action("close_short_market", plan.plan_id, sym,
                           {"qty": plan.qty, "reason": "fork_ride_giveback"}),
                    self._reg_update(plan, {"state": "closing"}),
                    self._event("hedge_fork_ride_cover", plan,
                                peak_short_frac=round(
                                    plan.peak_short_frac, 6),
                                mark=mark)]
        return []

    def _eval_hold_expiry(self, plan: HedgePlan, ctx: BookCtx) -> List[Action]:
        """FM-4: force-cover a budget hedge held past max_hedge_hold_s —
        funding bleed and tail exposure are bounded by clock, not hope."""
        if plan.state not in (STATE_FILLED, STATE_PROTECTED):
            return []
        max_hold = float(self.knobs.max_hedge_hold_s)
        if max_hold <= 0 or plan.created_ts <= 0:
            return []
        if ctx.now - plan.created_ts < max_hold:
            return []
        plan.state = STATE_CLOSING
        return [Action("close_short_market", plan.plan_id, plan.symbol,
                       {"qty": plan.qty, "reason": "max_hold_expired"}),
                self._reg_update(plan, {"state": "closing"}),
                self._event("hedge_hold_expired", plan,
                            held_s=round(ctx.now - plan.created_ts, 0))]

    def _eval_chase(self, plan: HedgePlan, ctx: BookCtx) -> List[Action]:
        sym = plan.symbol
        mark = float(ctx.hedge_marks.get(sym, 0.0) or 0.0)
        if mark <= 0 or plan.limit_price <= 0:
            return []
        # ATR abandonment (2026-09-19 Governor budget): if the mark has
        # drifted ≥ chase_abandon_atr_mult × arm_atr below the arm-time mark,
        # the entry thesis changed — cancel and abort, never convert.
        if (plan.arm_atr > 0 and plan.arm_mark > 0
                and mark < plan.arm_mark
                - self.knobs.chase_abandon_atr_mult * plan.arm_atr):
            plan.state = STATE_ABORTED
            return [self._reg_update(plan, {"state": "aborted"}),
                    self._event("hedge_aborted", plan,
                                reason="chase_atr_drift",
                                arm_mark=plan.arm_mark,
                                arm_atr=plan.arm_atr, mark=mark),
                    Action("cancel_order", plan.plan_id, sym,
                           {"order_id": plan.order_id})]
        spread = float(ctx.spread_est.get(sym, self.knobs.spread_est_pct))
        thresh = max(self.knobs.chase_reprice_pct, 2.0 * spread)
        # Chase DOWN only: the resting short is above the mark; a fall of
        # ≥ max(0.25%, 2×spread) from the order price re-prices it.
        if mark >= plan.limit_price * (1.0 - thresh):
            return []
        if ctx.now - plan.last_amend_ts < self.knobs.chase_min_amend_s:
            return []
        if plan.chase_steps >= self.knobs.chase_max_steps:
            plan.state = STATE_CONVERTING
            return [self._event("hedge_chase_market_conversion", plan,
                                steps=plan.chase_steps),
                    Action("convert_market_short", plan.plan_id, sym,
                           {"qty": plan.qty})]
        tick = float(ctx.tick_est_pct.get(sym, self.knobs.tick_est_pct))
        new_price = mark * (1.0 + tick)
        plan.chase_steps += 1
        plan.last_amend_ts = ctx.now
        plan.limit_price = new_price
        return [self._event("hedge_chase_amend", plan,
                            steps=plan.chase_steps, new_price=new_price),
                Action("amend_short", plan.plan_id, sym,
                       {"order_id": plan.order_id, "new_price": new_price})]

    def _eval_rescue(self, lng: LongCtx, ctx: BookCtx) -> List[Action]:
        """Mode 2 SHADOW — refuted economics (2026-09-19): telemetry and a
        shadow registry row, NEVER an order action."""
        sym = lng.symbol
        if lng.side != "long":
            return []
        plan_id = f"hrc-{sym}-{lng.opened_at_ms}"
        if plan_id in self._plans:
            return []
        if lng.age_s < self.knobs.rescue_min_time_s:
            return []
        roe = roe_pct("long", lng.entry_price, lng.mark, lng.leverage)
        if roe is None or roe > self.knobs.rescue_roe_trigger_pct:
            return []
        drift = (sym in ctx.mover_relief
                 or ctx.funding_carry_adverse.get(sym, False)
                 or ctx.oi_rising.get(sym, False))
        if not drift:
            return []
        mark = float(ctx.hedge_marks.get(sym, 0.0) or 0.0) or lng.mark
        if mark <= 0:
            return []
        plan = HedgePlan(plan_id=plan_id, symbol=sym, state=STATE_SHADOW_ARMED,
                         shadow=True, primary_opened_at=lng.opened_at_ms,
                         primary_entry=lng.entry_price, primary_qty=lng.qty,
                         qty=lng.qty, entry_price=mark, created_ts=ctx.now)
        self._plans[plan_id] = plan
        return [Action("registry_upsert", plan_id, sym, {
                    "pair_id": plan_id, "symbol": sym,
                    "venue": self.knobs.hedge_venue, "side": "short",
                    "qty": lng.qty, "entry_price": mark,
                    "state": "shadow", "hedge_of": plan_id}),
                self._event("hedge_rescue_would_arm", plan, size_pct=100,
                            entry=mark, trail_pct=self.knobs.trail_pct,
                            dehedge="pair_net>=0 or short_giveback>=50pct",
                            roe=round(roe, 2), age_s=round(lng.age_s, 0))]

    def _eval_shadow_cover(self, plan: HedgePlan,
                           ctx: BookCtx) -> List[Action]:
        """De-hedge model: cover at pair_net ≥ 0 OR short-profit giveback
        ≥ 50% from peak. Equal-notional legs → price-fraction arithmetic."""
        lng = next((l for l in ctx.longs if l.symbol == plan.symbol), None)
        if lng is None or plan.primary_entry <= 0 or plan.entry_price <= 0:
            return []
        mark = lng.mark
        if mark <= 0:
            return []
        long_frac = mark / plan.primary_entry - 1.0
        short_frac = (plan.entry_price - mark) / plan.entry_price
        pair_net = long_frac + short_frac
        plan.peak_short_frac = max(plan.peak_short_frac, short_frac)
        reason = None
        if pair_net >= 0:
            reason = "pair_net_nonnegative"
        elif (plan.peak_short_frac > 0
              and (plan.peak_short_frac - short_frac)
              >= self.knobs.rescue_giveback_frac * plan.peak_short_frac):
            reason = "short_profit_giveback"
        if reason is None:
            return []
        plan.state = STATE_SHADOW_COVERED
        return [self._reg_update(plan, {"state": "shadow_covered"}),
                self._event("hedge_rescue_would_cover", plan, reason=reason,
                            pair_net=round(pair_net, 6),
                            peak_short_frac=round(plan.peak_short_frac, 6))]

    # ── main entry ───────────────────────────────────────────────────────────

    def evaluate(self, ctx: BookCtx) -> List[Action]:
        if not ctx.enabled:
            return []   # master kill switch — zero actions, zero reads matter
        actions: List[Action] = []
        now = ctx.now
        longs_by_sym = {l.symbol: l for l in ctx.longs}

        # 1. Exchange truth: fill / cover / close confirmation. None = the
        #    snapshot is UNKNOWN — skip detection entirely (never read an
        #    absent poll as "leg gone"). Runs BEFORE primary-gone detection
        #    so an exchange-covered leg is never also market-closed.
        if ctx.hedge_positions is not None:
            for plan in list(self._plans.values()):
                if plan.shadow or plan.state not in ACTIVE_STATES:
                    continue
                pos = ctx.hedge_positions.get(plan.symbol)
                has_pos = bool(pos) and float(pos.get("qty", 0) or 0) > 0
                if plan.state in (STATE_CHASING, STATE_CONVERTING):
                    if has_pos:
                        actions.extend(self._on_fill(
                            plan, float(pos.get("entry", 0) or 0),
                            float(pos.get("qty", 0) or 0), ctx))
                elif plan.state in (STATE_FILLED, STATE_PROTECTED):
                    if not has_pos:
                        actions.extend(self._on_leg_gone(plan, now, ctx))
                elif plan.state == STATE_CLOSING:
                    if has_pos:
                        # Close retry — the previous market close did not land.
                        actions.append(Action(
                            "close_short_market", plan.plan_id, plan.symbol,
                            {"qty": float(pos.get("qty", 0) or 0) or plan.qty,
                             "reason": "close_retry"}))
                    else:
                        actions.extend(self.on_close_confirmed(plan.plan_id,
                                                               ctx))

        # 2. Primary-gone detection → unwind (or shadow cleanup). Identity is
        #    (symbol, opened_at_ms) — a re-entry on the same symbol unwinds
        #    the old plan and may arm a fresh one next tick.
        for plan in list(self._plans.values()):
            lng = longs_by_sym.get(plan.symbol)
            if (not plan.shadow and plan.primary_opened_at == 0
                    and lng is not None and plan.state in ACTIVE_STATES):
                # Boot-adopted plan (identity unknown at adoption): bind to
                # the live primary long on first sight so a later re-entry
                # unwinds correctly instead of instantly covering.
                plan.primary_opened_at = lng.opened_at_ms
            gone = lng is None or lng.opened_at_ms != plan.primary_opened_at
            if plan.shadow:
                if gone and plan.state in (STATE_SHADOW_ARMED,
                                           STATE_SHADOW_COVERED):
                    actions.append(Action("registry_remove", plan.plan_id,
                                          plan.symbol,
                                          {"pair_id": plan.plan_id}))
                    del self._plans[plan.plan_id]
                continue
            if gone and plan.state in ACTIVE_STATES:
                actions.extend(self._unwind(plan))
            if gone:
                # Budget ledgers are keyed by primary identity — a closed
                # leg's ledger dies with it (a re-entry gets a fresh key).
                self._budget_spent.pop(f"{plan.symbol}-"
                                       f"{plan.primary_opened_at}", None)
                self._harvest_count.pop(f"{plan.symbol}-"
                                        f"{plan.primary_opened_at}", None)

        # 3. Stuck-arm retry: a place action dropped by the executor (venue
        #    down at arm time) is re-emitted every armed_retry_s.
        for plan in list(self._plans.values()):
            if (not plan.shadow and plan.state == STATE_ARMED
                    and not plan.order_id
                    and now - plan.created_ts >= self.knobs.armed_retry_s):
                plan.created_ts = now
                actions.append(Action("place_limit_short", plan.plan_id,
                                      plan.symbol,
                                      {"qty": plan.qty,
                                       "price": plan.limit_price,
                                       "leverage": plan.leverage}))

        # 4. Profit-lock arming + rung ratchet (GREEN budget mode forks here).
        if ctx.profit_lock_enabled:
            for lng in ctx.longs:
                actions.extend(self._eval_profit_lock(lng, ctx))

        # 4b. RED pain-harvest arming (budget doctrine).
        if self.knobs.budget_sizing_enabled:
            for lng in ctx.longs:
                actions.extend(self._eval_pain(lng, ctx))

        # 5. Chase management.
        for plan in list(self._plans.values()):
            if (not plan.shadow and plan.state == STATE_CHASING
                    and plan.order_id):
                actions.extend(self._eval_chase(plan, ctx))

        # 5b. FORK-RIDE (thesis break → ride the flush) + FM-4 hold expiry.
        #     Runs BEFORE protection re-emit so a ride-arming's breakeven
        #     stop is the geometry the re-emit loop sees.
        for plan in list(self._plans.values()):
            if not plan.shadow and plan.mode in ("green", "pain"):
                actions.extend(self._eval_fork_ride(plan, ctx))
                if plan.state != STATE_CLOSING:
                    actions.extend(self._eval_hold_expiry(plan, ctx))

        # 6. Protection re-emit while a filled leg awaits confirmation
        #    (exchange trading-stop is idempotent — re-setting is harmless).
        #    Latched plans (D52 S3) never re-emit — the venue rejected the
        #    trail protection_fail_max times; further emits are pure spam.
        for plan in list(self._plans.values()):
            if (not plan.shadow and plan.state == STATE_FILLED
                    and not plan.protection_unavailable
                    and now - plan.protection_last_emit >= 20.0):
                plan.protection_last_emit = now
                actions.append(Action("set_protection", plan.plan_id,
                                      plan.symbol,
                                      {"trail_abs": plan.trail_dist_abs,
                                       "active_price": plan.entry_price,
                                       "catastrophic_stop":
                                           plan.catastrophic_stop,
                                       "tp_price": (0.0 if plan.riding
                                                    else plan.tp_price),
                                       "tp_qty": plan.qty}))

        # 7. Rescue shadow arming + de-hedge model.
        if ctx.rescue_shadow_enabled:
            for lng in ctx.longs:
                actions.extend(self._eval_rescue(lng, ctx))
            for plan in list(self._plans.values()):
                if plan.shadow and plan.state == STATE_SHADOW_ARMED:
                    actions.extend(self._eval_shadow_cover(plan, ctx))

        return actions
