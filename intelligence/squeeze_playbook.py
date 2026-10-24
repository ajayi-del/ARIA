"""intelligence/squeeze_playbook.py — multi-leg squeeze lifecycle state machine (SHADOW).

Evidence (EdgeDepth event database, Oct 2026): squeezes are MULTI-LEG
(BMT +34.8% then a +98.6% leg 2 51h later; IRYS +85.6% then +71.5%), and
post-peak cascades are fadeable (AGT -36.4% in 7h after the peak; APR -42.9%).
The squeeze_scanner/squeeze_radar planes see the SETUP; this module owns the
LIFECYCLE of one symbol's squeeze once the trigger fires — leg 1, the
consolidation, the leg-2 refuel read, and the post-peak fade — and emits
INTENTS (pure data). A splice executor acts on them; this brain never does
I/O (department template: zero I/O, injected data, constructor-injected
tunables, call-time kill switch, own namespace).

SHADOW-FIRST: every intent is a journal row, never an order.

State machine (per-symbol context):
  IDLE -> WATCHING        radar_tier >= 2
  WATCHING -> LEG1_ACTIVE volume_spike > trigger_volume_mult (the trigger)
  LEG1_ACTIVE -> CONSOLIDATING
                          leg1 MFE > leg1_confirm_pct AND
                          hours since peak >= consol_min_h
  CONSOLIDATING -> LEG2_SETUP
                          funding < leg2_funding_rebuilt AND
                          oi_growth_3h > leg2_oi_growth_min
                          (new shorts re-entering while OI grows = leg-2 fuel)
  CONSOLIDATING -> CASCADE_SHORT  hours since peak > consol_max_h without the
                          leg-2 conditions (no intent yet — armed, waiting)
  LEG2_SETUP -> CASCADE_SHORT
                          hours since leg2 entry > leg2_max_h (armed, waiting)
                          OR pullback from combined peak > cascade_pullback
                          with funding > cascade_funding_flip (confirms
                          immediately, same tick)
  CASCADE_SHORT -> EXHAUSTED
                          pullback from combined peak > cascade_pullback AND
                          funding > cascade_funding_flip — ONE-SHOT per event
  EXHAUSTED               terminal; the context drops exhausted_ttl_h after
                          exhaustion (the only reset path)

MFE-lock trailer (tighten-only, never lowers):
  floor = entry + (peak - entry) * lock_fraction(tier, mfe)
  anchored at the LEG1 entry for the whole event (leg-2 peak folds into the
  combined peak). Band tables are constructor-injected via PlaybookConfig.

Intent discipline: intents fire ONLY on state transitions, when the trailer
floor ratchets to a NEW lock band (kind = the live shadow long's kind,
state_from == state_to, meta reason "floor_band_ratchet"), and when an
EXHAUSTED context expires (kind "exhausted"). Transitions without a named
action (LEG1->CONSOLIDATING, the two timeout arms into CASCADE_SHORT) emit
nothing. The fade intent is "counter_short_shadow" when the CASCADE_SHORT
state was reached from the CONSOLIDATING timeout (fading leg-1 exhaustion —
no leg 2 ever fired) and "cascade_short_shadow" on the leg-2 paths.

Fail-closed everywhere: missing/None inputs defer the transitions that need
them (never inferred); mark <= 0 (or None) is a no-op tick that does not even
create the context; advance() never raises (per-symbol try/except).

Kill switch: SQUEEZE_PLAYBOOK_ENABLED (env, default "true"). False: advance()
returns [] and all contexts are inert (not advanced, not dropped) — the
pre-module system bit-for-bit.

VSM: S4 intelligence (outside-and-then adaptation). Telemetry namespace:
squeeze_playbook_* rows are emitted by the splice, not here.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

# ── Kill switch ───────────────────────────────────────────────────────────────

def squeeze_playbook_enabled() -> bool:
    """SQUEEZE_PLAYBOOK_ENABLED (env, default true). False = inert."""
    return os.environ.get("SQUEEZE_PLAYBOOK_ENABLED", "true").strip().lower() != "false"


# ── Public types ──────────────────────────────────────────────────────────────

class PlaybookState(Enum):
    IDLE = "IDLE"
    WATCHING = "WATCHING"
    LEG1_ACTIVE = "LEG1_ACTIVE"
    CONSOLIDATING = "CONSOLIDATING"
    LEG2_SETUP = "LEG2_SETUP"
    CASCADE_SHORT = "CASCADE_SHORT"
    EXHAUSTED = "EXHAUSTED"


@dataclass(frozen=True)
class PlaybookInput:
    """One symbol's per-tick snapshot (the splice gathers every field)."""
    symbol: str
    mark_price: float
    funding_rate: Optional[float]
    oi_growth_3h: Optional[float]
    volume_spike: Optional[float]
    radar_tier: int                      # from squeeze_radar (0-3)
    arcus_listed: bool = False           # counter-leg venue availability
    now_s: float = 0.0


@dataclass(frozen=True)
class PlaybookIntent:
    symbol: str
    kind: str          # watch | enter_long_shadow | leg2_add_shadow |
                       # counter_short_shadow | cascade_short_shadow |
                       # exit_shadow | exhausted
    venue_hint: str    # sodex | aster_if_listed | arcus | none
    state_from: str
    state_to: str
    trigger_price: Optional[float]
    trail_floor: Optional[float]         # current MFE-lock floor (shadow long)
    meta: dict = field(default_factory=dict)


# MFE lock tables: ascending (mfe_floor_inclusive, lock_fraction); the LAST
# rung whose floor the MFE clears wins; below the first rung lock = 0.00.
DEFAULT_LOCK_TIER3: Tuple[Tuple[float, float], ...] = (
    (0.10, 0.50), (0.30, 0.65), (0.50, 0.75), (1.00, 0.80), (5.00, 0.85),
)
DEFAULT_LOCK_TIER2: Tuple[Tuple[float, float], ...] = (
    (0.10, 0.40), (0.30, 0.55), (1.00, 0.70),
)


@dataclass(frozen=True)
class PlaybookConfig:
    """All tunables constructor-injected (defaults = the Oct-2026 evidence read)."""
    trigger_volume_mult: float = 3.0
    leg1_confirm_pct: float = 0.30
    consol_min_h: float = 2.0
    consol_max_h: float = 8.0
    leg2_funding_rebuilt: float = -1e-5
    leg2_oi_growth_min: float = 0.05
    leg2_max_h: float = 12.0
    cascade_pullback: float = 0.05
    cascade_funding_flip: float = 2e-5
    exhausted_ttl_h: float = 24.0
    lock_tier3: Tuple[Tuple[float, float], ...] = DEFAULT_LOCK_TIER3
    lock_tier2: Tuple[Tuple[float, float], ...] = DEFAULT_LOCK_TIER2


# ── Internal context ──────────────────────────────────────────────────────────

@dataclass
class _Context:
    symbol: str
    state: PlaybookState = PlaybookState.IDLE
    tier: int = 0
    entry_price: Optional[float] = None      # mark at WATCHING->LEG1 (shadow long entry)
    entry_ts: float = 0.0
    peak_price: Optional[float] = None       # leg-1 peak
    peak_ts: float = 0.0
    leg2_entry_price: Optional[float] = None
    leg2_entry_ts: float = 0.0
    leg2_peak: Optional[float] = None
    combined_peak: Optional[float] = None    # max(leg1 peak, leg2 peak)
    floor: Optional[float] = None            # tighten-only MFE-lock floor
    floor_band: int = 0
    mfe: float = 0.0
    cascade_origin: str = ""                 # consol_timeout | leg2_timeout | leg2_confirm
    exhausted_ts: float = 0.0
    last_mark: float = 0.0
    last_ts: float = 0.0


class SqueezePlaybook:
    """Per-symbol squeeze lifecycle brain. Zero I/O; all state in-memory."""

    def __init__(self, cfg: PlaybookConfig) -> None:
        self._cfg = cfg
        self._contexts: Dict[str, _Context] = {}

    # ── public API ────────────────────────────────────────────────────────────

    def advance(self, radar_verdicts: Dict[str, int],
                inputs: List[PlaybookInput]) -> List[PlaybookIntent]:
        """Advance every presented symbol one tick. Never raises."""
        if not squeeze_playbook_enabled():
            return []
        intents: List[PlaybookIntent] = []
        for inp in inputs or []:
            try:
                intents.extend(self._advance_one(radar_verdicts or {}, inp))
            except Exception:
                continue   # fail-closed per symbol
        return intents

    def state_snapshot(self) -> dict:
        """Telemetry readout: kill switch + every live context."""
        return {
            "enabled": squeeze_playbook_enabled(),
            "contexts": {
                sym: {
                    "state": ctx.state.value,
                    "tier": ctx.tier,
                    "entry_price": ctx.entry_price,
                    "entry_ts": ctx.entry_ts,
                    "peak_price": ctx.peak_price,
                    "peak_ts": ctx.peak_ts,
                    "leg2_entry_price": ctx.leg2_entry_price,
                    "leg2_entry_ts": ctx.leg2_entry_ts,
                    "leg2_peak": ctx.leg2_peak,
                    "combined_peak": ctx.combined_peak,
                    "floor": ctx.floor,
                    "floor_band": ctx.floor_band,
                    "mfe": ctx.mfe,
                    "cascade_origin": ctx.cascade_origin,
                    "exhausted_ts": ctx.exhausted_ts,
                    "last_mark": ctx.last_mark,
                    "last_ts": ctx.last_ts,
                }
                for sym, ctx in self._contexts.items()
            },
        }

    # ── internals ─────────────────────────────────────────────────────────────

    def _lock(self, tier: int, mfe: float) -> Tuple[int, float]:
        """(band_index, lock_fraction) for a tier/MFE. Unknown tier -> (0, 0)."""
        if tier >= 3:
            table = self._cfg.lock_tier3
        elif tier == 2:
            table = self._cfg.lock_tier2
        else:
            return 0, 0.0
        band, lock = 0, 0.0
        for i, (thr, frac) in enumerate(table):
            if mfe >= thr:
                band, lock = i + 1, frac
        return band, lock

    def _update_floor(self, ctx: _Context) -> bool:
        """Recompute the MFE-lock floor off the combined peak (tighten-only).

        Returns True when the floor ratcheted into a NEW lock band.
        """
        peak = ctx.combined_peak
        if ctx.entry_price is None or peak is None or ctx.entry_price <= 0:
            return False
        mfe = (peak - ctx.entry_price) / ctx.entry_price
        band, lock = self._lock(ctx.tier, mfe)
        new_floor = ctx.entry_price + (peak - ctx.entry_price) * lock
        if ctx.floor is None or new_floor > ctx.floor:
            ctx.floor = new_floor
        ctx.mfe = mfe
        if band > ctx.floor_band:
            ctx.floor_band = band
            return True
        return False

    def _intent(self, ctx: _Context, kind: str, venue: str,
                state_from: PlaybookState, state_to: PlaybookState,
                trigger: Optional[float], floor: Optional[float],
                meta: dict) -> PlaybookIntent:
        return PlaybookIntent(
            symbol=ctx.symbol, kind=kind, venue_hint=venue,
            state_from=state_from.value, state_to=state_to.value,
            trigger_price=trigger, trail_floor=floor, meta=meta)

    def _advance_one(self, radar: Dict[str, int],
                     inp: PlaybookInput) -> List[PlaybookIntent]:
        out: List[PlaybookIntent] = []
        try:
            mark = float(inp.mark_price)
        except (TypeError, ValueError):
            return out
        if not math.isfinite(mark) or mark <= 0:
            return out   # no-op tick: no context created, no state touched
        now = float(inp.now_s or 0.0)
        sym = inp.symbol

        ctx = self._contexts.get(sym)
        if ctx is None:
            ctx = _Context(symbol=sym)
            self._contexts[sym] = ctx
        ctx.last_mark = mark
        ctx.last_ts = now

        try:
            tier = int(radar.get(sym, inp.radar_tier))
        except (TypeError, ValueError):
            tier = ctx.tier

        st = ctx.state

        if st is PlaybookState.IDLE:
            if tier >= 2:
                ctx.state = PlaybookState.WATCHING
                ctx.tier = tier
                out.append(self._intent(
                    ctx, "watch", "none", PlaybookState.IDLE,
                    PlaybookState.WATCHING, None, None,
                    {"radar_tier": tier}))

        elif st is PlaybookState.WATCHING:
            spike = inp.volume_spike
            if spike is not None:
                try:
                    fired = float(spike) > self._cfg.trigger_volume_mult
                except (TypeError, ValueError):
                    fired = False
                if fired:
                    ctx.state = PlaybookState.LEG1_ACTIVE
                    ctx.entry_price = mark
                    ctx.entry_ts = now
                    ctx.peak_price = mark
                    ctx.peak_ts = now
                    ctx.combined_peak = mark
                    ctx.floor = mark
                    ctx.floor_band = 0
                    ctx.mfe = 0.0
                    out.append(self._intent(
                        ctx, "enter_long_shadow", "aster_if_listed",
                        PlaybookState.WATCHING, PlaybookState.LEG1_ACTIVE,
                        mark, ctx.floor,
                        {"volume_spike": float(spike), "tier": ctx.tier}))

        elif st is PlaybookState.LEG1_ACTIVE:
            if ctx.peak_price is None or mark > ctx.peak_price:
                ctx.peak_price = mark
                ctx.peak_ts = now
            ctx.combined_peak = ctx.peak_price
            if self._update_floor(ctx):
                out.append(self._intent(
                    ctx, "enter_long_shadow", "aster_if_listed",
                    PlaybookState.LEG1_ACTIVE, PlaybookState.LEG1_ACTIVE,
                    None, ctx.floor,
                    {"reason": "floor_band_ratchet", "band": ctx.floor_band,
                     "mfe": ctx.mfe}))
            if ctx.entry_price and ctx.peak_price:
                gain = (ctx.peak_price - ctx.entry_price) / ctx.entry_price
                hours_since_peak = max(0.0, (now - ctx.peak_ts) / 3600.0)
                if (gain > self._cfg.leg1_confirm_pct
                        and hours_since_peak >= self._cfg.consol_min_h):
                    ctx.state = PlaybookState.CONSOLIDATING   # no intent

        elif st is PlaybookState.CONSOLIDATING:
            hours_since_peak = max(0.0, (now - ctx.peak_ts) / 3600.0)
            leg2_ok = False
            if inp.funding_rate is not None and inp.oi_growth_3h is not None:
                try:
                    leg2_ok = (float(inp.funding_rate) < self._cfg.leg2_funding_rebuilt
                               and float(inp.oi_growth_3h) > self._cfg.leg2_oi_growth_min)
                except (TypeError, ValueError):
                    leg2_ok = False
            if leg2_ok:
                ctx.state = PlaybookState.LEG2_SETUP
                ctx.leg2_entry_price = mark
                ctx.leg2_entry_ts = now
                ctx.leg2_peak = mark
                if ctx.combined_peak is None or mark > ctx.combined_peak:
                    ctx.combined_peak = mark
                out.append(self._intent(
                    ctx, "leg2_add_shadow", "aster_if_listed",
                    PlaybookState.CONSOLIDATING, PlaybookState.LEG2_SETUP,
                    mark, ctx.floor,
                    {"funding_rate": float(inp.funding_rate),
                     "oi_growth_3h": float(inp.oi_growth_3h)}))
            elif hours_since_peak > self._cfg.consol_max_h:
                ctx.state = PlaybookState.CASCADE_SHORT       # armed, no intent
                ctx.cascade_origin = "consol_timeout"

        elif st is PlaybookState.LEG2_SETUP:
            if ctx.leg2_peak is None or mark > ctx.leg2_peak:
                ctx.leg2_peak = mark
            if ctx.combined_peak is None or mark > ctx.combined_peak:
                ctx.combined_peak = mark
            if self._update_floor(ctx):
                out.append(self._intent(
                    ctx, "leg2_add_shadow", "aster_if_listed",
                    PlaybookState.LEG2_SETUP, PlaybookState.LEG2_SETUP,
                    None, ctx.floor,
                    {"reason": "floor_band_ratchet", "band": ctx.floor_band,
                     "mfe": ctx.mfe}))
            hours_since_leg2 = max(0.0, (now - ctx.leg2_entry_ts) / 3600.0)
            pullback = self._pullback(ctx, mark)
            funding_flipped = False
            if inp.funding_rate is not None:
                try:
                    funding_flipped = float(inp.funding_rate) > self._cfg.cascade_funding_flip
                except (TypeError, ValueError):
                    funding_flipped = False
            if hours_since_leg2 > self._cfg.leg2_max_h:
                ctx.state = PlaybookState.CASCADE_SHORT       # armed, no intent
                ctx.cascade_origin = "leg2_timeout"
            elif pullback is not None and pullback > self._cfg.cascade_pullback \
                    and funding_flipped:
                ctx.cascade_origin = "leg2_confirm"
                self._fire_cascade(ctx, inp, mark, now, out)

        elif st is PlaybookState.CASCADE_SHORT:
            pullback = self._pullback(ctx, mark)
            funding_flipped = False
            if inp.funding_rate is not None:
                try:
                    funding_flipped = float(inp.funding_rate) > self._cfg.cascade_funding_flip
                except (TypeError, ValueError):
                    funding_flipped = False
            if pullback is not None and pullback > self._cfg.cascade_pullback \
                    and funding_flipped:
                self._fire_cascade(ctx, inp, mark, now, out)

        elif st is PlaybookState.EXHAUSTED:
            if now - ctx.exhausted_ts > self._cfg.exhausted_ttl_h * 3600.0:
                out.append(self._intent(
                    ctx, "exhausted", "none", PlaybookState.EXHAUSTED,
                    PlaybookState.EXHAUSTED, None, None,
                    {"reason": "ttl_expired"}))
                del self._contexts[sym]

        return out

    def _pullback(self, ctx: _Context, mark: float) -> Optional[float]:
        peak = ctx.combined_peak
        if peak is None or peak <= 0:
            return None
        return (peak - mark) / peak

    def _fire_cascade(self, ctx: _Context, inp: PlaybookInput, mark: float,
                      now: float, out: List[PlaybookIntent]) -> None:
        """The one-shot fade + long exit, then EXHAUSTED. Never repeats."""
        kind = ("counter_short_shadow" if ctx.cascade_origin == "consol_timeout"
                else "cascade_short_shadow")
        meta = {
            "origin": ctx.cascade_origin,
            "pullback": self._pullback(ctx, mark),
            "funding_rate": inp.funding_rate,
            "combined_peak": ctx.combined_peak,
        }
        if inp.arcus_listed:
            venue = "arcus"
        else:
            venue = "none"
            meta["note"] = "cross-venue leg unavailable — abstain"
        out.append(self._intent(
            ctx, kind, venue, PlaybookState.CASCADE_SHORT,
            PlaybookState.EXHAUSTED, mark, ctx.floor, meta))
        out.append(self._intent(
            ctx, "exit_shadow", "aster_if_listed", PlaybookState.CASCADE_SHORT,
            PlaybookState.EXHAUSTED, mark, ctx.floor,
            {"reason": "trailer_endgame"}))
        ctx.state = PlaybookState.EXHAUSTED
        ctx.exhausted_ts = now
