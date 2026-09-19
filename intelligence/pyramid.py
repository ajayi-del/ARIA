"""intelligence/pyramid.py — Pyramid layer brain (pure, zero-I/O).

Governor directive 2026-09-18: staircase legs restoring Nietzsche intent
through proof-of-move adds — LIVE from day one (no shadow phase). Driver:
the vol-stop constant-risk resize amputates entries (UNI +9.88% runner paid
$0.77); the pyramid rebuilds notional INTO moves the market has proven.

Netting venues (SoDEX + Aster both net per symbol): legs are sub-allocations
of ONE exchange position — each add re-anchors the tracked entry VWAP and
ratchets ONE combined native stop tighten-only (aster_swing precedent).
Leg sizes normalize to the ACTUAL filled base qty (post vol-stop resize,
post FIX-A clamp): raw Governor weights anchor on leg-0, so only ratios
survive.

Ownership model (Governor 2026-09-18): the TP1-gated add guard
(main.py aster_swing_add_gate / the :624 family) is the PARENT gate — the
brain DELEGATES via the injected `tp1_cleared`; it never reimplements the
TP1 check (two sources of truth = divergence class). campaign_pyramid owns
narrative/event-driven adds; this module owns price-trigger ATR adds;
siblings below the TP1 parent.

Anti-martingale invariant (Livermore/LeBeau): before every add the combined
stop moves to VWAP breakeven ∓ buffer — a pyramided trade cannot turn red
beyond the buffer. Warmup doctrine: while the volatility/positioning
pillars are still seeding (post-restart window), adds DEFER
(add_deferred_warmup) — the staircase fires the moment the ruler is honest.

Floor math is INJECTED (`floor_fn`) — the template forbids importing
main.py from intelligence/; the splice passes main.aster_swing_floor_price
so the pyramid and the legacy swing add share one source of truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

PHASE_BASE_FILLED = "BASE_FILLED"
PHASE_LEG_PENDING = "LEG2_PENDING"      # next leg armed, trigger not hit
PHASE_BUILDING = "BUILDING"             # add/stop ratchet in flight
PHASE_PYRAMIDED = "PYRAMIDED"           # all legs placed
PHASE_UNWINDING = "UNWINDING"           # TRAIL | SCALE_OUT | HARD_EXIT active

# Exit-stack pause covers build phases only — never PYRAMIDED/UNWINDING, and
# the software stop guardian is never paused (protection never suspends).
PAUSE_PHASES = frozenset({PHASE_BASE_FILLED, PHASE_LEG_PENDING, PHASE_BUILDING})

SWING_TRADE_TYPES = frozenset({"aster_swing", "breakout", "s1_oi_pullback"})
NEVER_REGISTER_TAGS = frozenset({"whale_probe", "explosive"})

WARMUP_MIN_FRAC = 0.80      # pillar-seed completeness below this defers adds
MAX_ADD_RETRACE_FRAC = 0.50 # add dies if >50% of the proven move retraced
COHERENCE_COLLAPSE = 3.0    # unwind floor (below = thesis dead, scale out)


@dataclass
class LegPlan:
    weights: tuple                      # raw Governor weights, leg0 = base
    triggers_atr: tuple                 # per-leg ATR step (len = len(weights)-1)
    be_buffer_pct: float = 0.004


@dataclass
class PyramidTrack:
    symbol: str
    side: str                           # "long" | "short"
    klass: str                          # "scalp" | "swing"
    base_qty: float                     # ACTUAL filled qty at registration
    base_entry: float
    current_vwap: float                 # re-anchored after each add
    current_qty: float
    legs_done: int                      # adds completed (0 = base only)
    atr_at_reg: float                   # 15m ATR-14 frozen at registration
    phase: str
    registered_at: float
    last_add_ts: float = 0.0
    add_attempts: int = 0               # per-leg cap (aster_swing precedent)
    unwind_mode: str = ""               # "TRAIL" | "SCALE_OUT" | "HARD_EXIT"
    peak_move_atr: float = 0.0          # favorable move from base_entry, ATR
    closed_at: float = 0.0              # re-entry watch window anchor
    tp1_cleared: bool = False           # written by the splice (parent gate)
    rebuilt_boot: bool = False          # terminal-state rebuild at boot (adds never fire)


@dataclass(frozen=True)
class AddVerdict:
    allowed: bool
    reason: str                         # ADD_VERDICT_REASONS registry
    leg_idx: int = 0
    qty: float = 0.0
    floor_price: float = 0.0            # pre-add breakeven floor for prior legs
    trigger_px: float = 0.0


@dataclass(frozen=True)
class UnwindVerdict:
    mode: str                           # "TRAIL" | "SCALE_OUT" | "HARD_EXIT" | ""
    reason: str


@dataclass(frozen=True)
class ReentryVerdict:
    eligible: bool
    reason: str
    retrace_frac: float = 0.0


def _parse_weights(raw) -> tuple:
    try:
        ws = tuple(float(w) for w in str(raw).split(",") if w.strip())
    except Exception:
        return ()
    if len(ws) < 2 or any(w <= 0 for w in ws):
        return ()
    return ws


def boot_rebuild_track(symbol: str, side: str, klass: str, *, base_qty: float,
                       base_entry: float, atr, plan: LegPlan,
                       now: float) -> Optional[PyramidTrack]:
    """Restart orphan seam (2026-09-19): _PYRAMID_STATE is memory-only and
    tracks register ONLY on the fresh-entry path, so startup-sync-adopted
    positions ran the exit stack with zero pyramid integration. Rebuild the
    track in the TERMINAL state — the staircase is treated as complete at
    birth: phase PYRAMIDED + legs_done = full, so the add path is doubly dead
    (phase gate + already_complete), kill-switch HARD_EXIT still covers the
    adopted position, and SCALE_OUT is suppressed in unwind_verdict (the leg
    geometry a 50% scale-out needs is unknowable post-restart — the normal
    exit stack owns thesis damage). Degenerate inputs abstain (fail-closed)."""
    try:
        qty = float(base_qty)
        entry = float(base_entry)
        atr_f = float(atr)
    except (TypeError, ValueError):
        return None
    if plan is None or qty <= 0 or entry <= 0 or atr_f <= 0:
        return None
    return PyramidTrack(
        symbol=symbol, side=side, klass=klass,
        base_qty=qty, base_entry=entry,
        current_vwap=entry, current_qty=qty,
        legs_done=len(plan.weights) - 1, atr_at_reg=atr_f,
        phase=PHASE_PYRAMIDED, registered_at=float(now),
        rebuilt_boot=True)


def leg_plans(cfg) -> dict:
    """klass -> LegPlan; malformed knobs drop the class (fail-closed)."""
    buf = float(getattr(cfg, "pyramid_be_buffer_pct", 0.004))
    out = {}
    sw = _parse_weights(getattr(cfg, "pyramid_scalp_leg_weights", "0.5,0.5"))
    if sw:
        step = float(getattr(cfg, "pyramid_scalp_trigger_atr", 0.3))
        out["scalp"] = LegPlan(sw, (step,) * (len(sw) - 1), buf)
    ww = _parse_weights(getattr(cfg, "pyramid_swing_leg_weights",
                                "0.405,0.25,0.20,0.145"))
    if ww:
        step = float(getattr(cfg, "pyramid_swing_trigger_atr", 1.0))
        out["swing"] = LegPlan(ww, (step,) * (len(ww) - 1), buf)
    return out


def klass_for(trade_type: str) -> str:
    return "swing" if trade_type in SWING_TRADE_TYPES else "scalp"


def registrable(trade_type: str, strategy_tag: str = "",
                is_campaign: bool = False) -> bool:
    """Mutex registry: probe runners own their conversion, explosive owns its
    trailing doctrine, campaign symbols stand down."""
    if is_campaign:
        return False
    if trade_type in NEVER_REGISTER_TAGS or strategy_tag in NEVER_REGISTER_TAGS:
        return False
    return True


def pause_exits(track: Optional[PyramidTrack], enabled: bool,
                shadow: bool) -> bool:
    """THE exit-stack pause predicate. Shadow mode NEVER pauses."""
    return (enabled and not shadow and track is not None
            and track.phase in PAUSE_PHASES)


def owns_stop(track: Optional[PyramidTrack], enabled: bool,
              shadow: bool) -> bool:
    """Stop ownership for trail/roe_ratchet loops — pyramid owns the native
    stop while building; both resume at PYRAMIDED (tighten-only)."""
    return pause_exits(track, enabled, shadow)


def leg_qty(plan: LegPlan, leg_idx: int, base_qty: float) -> float:
    """Leg qty normalized to the ACTUAL base (w_i/w_0) — never to pre-resize
    candidate intent (FIX-A clamp adaptation). 1-based leg_idx."""
    if base_qty <= 0 or leg_idx < 1 or leg_idx >= len(plan.weights):
        return 0.0
    return base_qty * plan.weights[leg_idx] / plan.weights[0]


def trigger_price(track: PyramidTrack, plan: LegPlan, leg_idx: int,
                  mode: str = "step") -> Optional[float]:
    """Proof-of-move rung. "step": fresh ATR step from the CURRENT VWAP
    anchor (one spike cannot fire two legs). "cumulative": Governor-literal
    k×ATR from base_entry. Degenerate ruler ⇒ None (abstain)."""
    atr = track.atr_at_reg
    if atr <= 0 or leg_idx < 1 or leg_idx >= len(plan.weights):
        return None
    if mode == "cumulative":
        dist = sum(plan.triggers_atr[:leg_idx]) * atr
        anchor = track.base_entry
    else:
        dist = plan.triggers_atr[leg_idx - 1] * atr
        anchor = track.current_vwap
    if anchor <= 0:
        return None
    return anchor + dist if track.side == "long" else anchor - dist


def pre_add_floor(side: str, vwap: float, buffer_pct: float) -> float:
    """Breakeven floor for the EXISTING combined position (its breakeven IS
    the current VWAP) — replaced exchange-side BEFORE the add fires."""
    if vwap <= 0:
        return 0.0
    return vwap * (1.0 - buffer_pct) if side == "long" else vwap * (1.0 + buffer_pct)


def breakeven_floor(side: str, pre_qty: float, pre_entry: float,
                    add_qty: float, add_px: float, buffer_pct: float,
                    floor_fn: Callable) -> float:
    """Post-add combined VWAP floor — delegates to the injected
    aster_swing_floor_price (single source of truth, template import ban)."""
    return floor_fn(side, pre_qty, pre_entry, add_qty, add_px, buffer_pct)


def retrace_frac_of_peak(track: PyramidTrack, mark) -> Optional[float]:
    """Fraction of the proven move (base→peak) given back at `mark`."""
    if mark is None or mark <= 0 or track.atr_at_reg <= 0:
        return None
    peak = track.peak_move_atr * track.atr_at_reg
    if peak <= 0:
        return None
    if track.side == "long":
        return ((track.base_entry + peak) - mark) / peak
    return (mark - (track.base_entry - peak)) / peak


def add_verdict(track: PyramidTrack, plan: LegPlan, *, mark, atr_now=None,
                coherence=None, rv_rank_now=None, funding_rate=None,
                oi_delta_pct=None, regime_ok=None, trend_verdict=None,
                tp1_cleared=None, warmup_frac=None,
                concurrent_pyramids: int = 0, pyramid_margin_frac: float = 0.0,
                cluster_frac=None, account_risk_frac=None,
                projected_gross_frac=None,
                l4_imbalance=None, l4_spread_bps=None,
                spread_cap_bps: float = 25.0, recovery_active: bool = False,
                etf_tide=None, mark_scale_ok: bool = True,
                floor_fn: Callable = None, cfg=None) -> AddVerdict:
    """Guard stack, fixed order (Governor 2026-09-18 — ordering is
    load-bearing): TP1 parent gate + trigger exit early (99% of checks);
    kill switches before the coherence compute; regime continuity before
    account guards. Every None pillar input abstains (pillar-null = safe);
    account-guard inputs are inert when their plane does not exist."""
    total_legs = len(plan.weights) - 1
    if track.legs_done >= total_legs:
        return AddVerdict(False, "already_complete")
    if track.add_attempts >= int(getattr(cfg, "pyramid_max_add_attempts", 2)):
        return AddVerdict(False, "max_attempts")

    # 1. TP1 PARENT GATE — delegated, never reimplemented (main.py:624
    # family owns the edge cases). Falsy (incl. None) = not cleared.
    if not (tp1_cleared if tp1_cleared is not None else track.tp1_cleared):
        return AddVerdict(False, "tp1_not_confirmed")

    # 2. Trigger evaluation.
    if mark is None or mark <= 0:
        return AddVerdict(False, "mark_unknown")
    leg_idx = track.legs_done + 1
    tpx = trigger_price(track, plan, leg_idx,
                        getattr(cfg, "pyramid_trigger_mode", "step"))
    if tpx is None:
        return AddVerdict(False, "atr_unknown")
    if track.side == "long" and mark < tpx:
        if warmup_frac is not None and warmup_frac < WARMUP_MIN_FRAC:
            return AddVerdict(False, "in_warmup_window", leg_idx,
                              trigger_px=tpx)
        return AddVerdict(False, "trigger_not_hit", leg_idx, trigger_px=tpx)
    if track.side == "short" and mark > tpx:
        if warmup_frac is not None and warmup_frac < WARMUP_MIN_FRAC:
            return AddVerdict(False, "in_warmup_window", leg_idx,
                              trigger_px=tpx)
        return AddVerdict(False, "trigger_not_hit", leg_idx, trigger_px=tpx)

    # Trigger HIT but the pillar ruler is still seeding — queue, don't fire.
    if warmup_frac is not None and warmup_frac < WARMUP_MIN_FRAC:
        return AddVerdict(False, "add_deferred_warmup", leg_idx,
                          trigger_px=tpx)

    # 3. Kill switches (before any expensive coherence compute).
    if funding_rate is None:
        return AddVerdict(False, "funding_unknown", leg_idx, trigger_px=tpx)
    fcap = float(getattr(cfg, "pyramid_funding_extreme_pct", 0.10)) / 100.0
    if abs(funding_rate) > fcap:
        return AddVerdict(False, "funding_extreme", leg_idx, trigger_px=tpx)
    if rv_rank_now is None:
        return AddVerdict(False, "rv_rank_unknown", leg_idx, trigger_px=tpx)
    if rv_rank_now >= float(getattr(cfg, "pyramid_rv_rank_kill", 90.0)):
        return AddVerdict(False, "rv_rank_extreme", leg_idx, trigger_px=tpx)
    if oi_delta_pct is None:
        return AddVerdict(False, "oi_delta_unknown", leg_idx, trigger_px=tpx)
    if oi_delta_pct < float(getattr(cfg, "pyramid_oi_delta_kill_pct", -2.0)):
        return AddVerdict(False, "oi_delta_flush", leg_idx, trigger_px=tpx)

    # 4. Regime continuity / thesis evidence.
    if recovery_active:
        return AddVerdict(False, "recovery_mode", leg_idx, trigger_px=tpx)
    if track.klass == "swing":
        # Native-evidence gate (aftermath signals carry no arbiter coherence).
        if trend_verdict != "aligned":
            return AddVerdict(False, f"trend_verdict_{trend_verdict}",
                              leg_idx, trigger_px=tpx)
        if l4_imbalance is None or l4_spread_bps is None:
            return AddVerdict(False, "no_l4", leg_idx, trigger_px=tpx)
        if l4_spread_bps > spread_cap_bps:
            return AddVerdict(False, "spread_too_wide", leg_idx, trigger_px=tpx)
        if track.side == "long" and l4_imbalance < -0.10:
            return AddVerdict(False, "l4_against", leg_idx, trigger_px=tpx)
        if track.side == "short" and l4_imbalance > 0.10:
            return AddVerdict(False, "l4_against", leg_idx, trigger_px=tpx)
    else:
        if coherence is None:
            return AddVerdict(False, "coherence_unavailable", leg_idx,
                              trigger_px=tpx)
        if coherence < float(getattr(cfg, "pyramid_add_coherence_min", 5.0)):
            return AddVerdict(False, "coherence_below_threshold", leg_idx,
                              trigger_px=tpx)
        if trend_verdict == "counter":
            return AddVerdict(False, "regime_changed", leg_idx, trigger_px=tpx)
        if trend_verdict == "unknown":
            return AddVerdict(False, "regime_unknown", leg_idx, trigger_px=tpx)
    if regime_ok is False:
        return AddVerdict(False, "regime_changed", leg_idx, trigger_px=tpx)
    frac = retrace_frac_of_peak(track, mark)
    if frac is not None and frac > MAX_ADD_RETRACE_FRAC:
        return AddVerdict(False, "retrace_too_deep", leg_idx, trigger_px=tpx)
    if etf_tide == "opposed":
        return AddVerdict(False, "etf_tide_opposed", leg_idx, trigger_px=tpx)

    # 5. Account / portfolio guards (inert when the plane does not exist).
    if concurrent_pyramids >= int(getattr(cfg, "pyramid_max_concurrent", 2)):
        return AddVerdict(False, "concurrency_cap", leg_idx, trigger_px=tpx)
    if pyramid_margin_frac >= float(getattr(cfg, "pyramid_max_exposure_pct", 0.30)):
        return AddVerdict(False, "exposure_cap", leg_idx, trigger_px=tpx)
    if cluster_frac is not None and cluster_frac >= 0.15:
        return AddVerdict(False, "cluster_cap_hit", leg_idx, trigger_px=tpx)
    if account_risk_frac is not None and account_risk_frac >= 0.02:
        return AddVerdict(False, "account_risk_budget_full", leg_idx,
                          trigger_px=tpx)
    if projected_gross_frac is not None and projected_gross_frac >= 0.50:
        return AddVerdict(False, "leverage_cap_hit", leg_idx, trigger_px=tpx)
    if not mark_scale_ok:
        return AddVerdict(False, "mark_scale_quarantined", leg_idx,
                          trigger_px=tpx)

    qty = leg_qty(plan, leg_idx, track.base_qty)
    if qty <= 0:
        return AddVerdict(False, "qty_degenerate", leg_idx, trigger_px=tpx)
    floor = pre_add_floor(track.side, track.current_vwap, plan.be_buffer_pct)
    return AddVerdict(True, f"add_approved_leg{leg_idx + 1}", leg_idx, qty,
                      floor, tpx)


def unwind_verdict(track: PyramidTrack, plan: LegPlan, *, coherence=None,
                   rv_rank_now=None, oi_delta_pct=None, funding_rate=None,
                   trend_verdict=None, cfg=None) -> UnwindVerdict:
    """Kill-switch fire ⇒ HARD_EXIT; thesis damage ⇒ SCALE_OUT; staircase
    complete ⇒ TRAIL handback. Pillar-null never forces an exit."""
    if funding_rate is not None:
        fcap = float(getattr(cfg, "pyramid_funding_extreme_pct", 0.10)) / 100.0
        if abs(funding_rate) > fcap:
            return UnwindVerdict("HARD_EXIT", "funding_extreme")
    if rv_rank_now is not None and \
            rv_rank_now >= float(getattr(cfg, "pyramid_rv_rank_kill", 90.0)):
        return UnwindVerdict("HARD_EXIT", "rv_rank_extreme")
    if oi_delta_pct is not None and \
            oi_delta_pct < float(getattr(cfg, "pyramid_oi_delta_kill_pct", -2.0)):
        return UnwindVerdict("HARD_EXIT", "oi_delta_flush")
    # Boot-rebuilt tracks never SCALE_OUT: the leg geometry a thesis-damage
    # 50% scale-out needs is unknowable post-restart (the position may already
    # be pyramided) — the unpaused normal exit stack owns thesis damage.
    # Kill switches above still fire (symbol-level risk-off protection).
    _rebuilt = bool(getattr(track, "rebuilt_boot", False))
    if not _rebuilt and coherence is not None and coherence < COHERENCE_COLLAPSE:
        return UnwindVerdict("SCALE_OUT", "coherence_collapse")
    if not _rebuilt and trend_verdict == "counter":
        return UnwindVerdict("SCALE_OUT", "trend_flip")
    if track.legs_done >= len(plan.weights) - 1:
        return UnwindVerdict("TRAIL", "complete")
    return UnwindVerdict("", "hold")


def reentry_verdict(closed_track: PyramidTrack, *, mark, coherence=None,
                    now: float = 0.0, cfg=None) -> ReentryVerdict:
    """24h watch after a pyramid closes: a 30-65% retrace of the proven move
    with fresh conviction is a re-entry CANDIDATE (signal only — live
    re-entry flows through the standard path and its registries)."""
    if closed_track.closed_at <= 0:
        return ReentryVerdict(False, "not_closed")
    if now - closed_track.closed_at > int(getattr(
            cfg, "pyramid_reentry_window_s", 86400)):
        return ReentryVerdict(False, "window_expired")
    frac = retrace_frac_of_peak(closed_track, mark)
    if frac is None:
        return ReentryVerdict(False, "no_move")
    rmin = float(getattr(cfg, "pyramid_reentry_retrace_min", 0.30))
    rmax = float(getattr(cfg, "pyramid_reentry_retrace_max", 0.65))
    if frac < rmin:
        return ReentryVerdict(False, "retrace_shallow", frac)
    if frac > rmax:
        return ReentryVerdict(False, "retrace_deep", frac)
    if coherence is None:
        return ReentryVerdict(False, "coherence_unavailable", frac)
    if coherence < float(getattr(cfg, "pyramid_reentry_coherence_min", 6.5)):
        return ReentryVerdict(False, "coherence_below_threshold", frac)
    return ReentryVerdict(True, "ok", frac)
