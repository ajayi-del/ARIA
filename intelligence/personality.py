"""
intelligence/personality.py — ARIA 7-state Market Personality Engine.

Seven market emotional states, assessed per-symbol on each SIGNAL_READY event.
Assessment is deterministic and <0.5ms (no I/O, no DB queries).

Personality priority order (checked in sequence):
  1. SHIELD    — capital preservation; blocks all new entries
  2. SOVEREIGN — staking-anchored component divergence; yield-funded; equity only
  3. AFTERMATH — post-cascade mean reversion; highest expected WR
  4. APEX      — cascade momentum; maximum aggression (crypto only)
  5. COIL      — weak signal + confused regime; arb only, no directional
  6. FLOW      — HTF-aligned trend; core profitability
  7. SCOUT     — fallback; cautious exploration with tight parameters

Primary discriminators (in order of importance):
  1. Cascade state     — objective on-chain signal
  2. Calendar state    — known risk events
  3. Signal conviction — coherence score (or spread z-score for SOVEREIGN)
  4. Regime state      — cross-asset intelligence
  5. HTF alignment     — 4H trend bias
  6. ATR vs baseline   — supporting signal only (not primary gate)

SOVEREIGN special properties:
  - Not coherence-based. Signal = component spread z-score vs MAG7 index.
  - Requires stake_balance > 0 (staked MAG7 as structural anchor).
  - Funded from staking yield only — never touches main trading capital.
  - Available: equity asset class (MAG7 components) only.

Hysteresis: 3 consecutive assessments required before personality switch.
SHIELD and SOVEREIGN bypass hysteresis (structural states, not trend-following).

PersonalityContextCache:
  Background loops update slow-changing fields (regime, cascade, calendar).
  Per-signal fields (symbol, coherence, direction, htf) injected at call time.
  SOVEREIGN fields (stake_balance, sovereign_budget, component_signals) updated
  by staking_monitor and yield_tracker background loops.
  Context build cost: ~0.1ms (vs ~8ms for full recomputation).

Usage:
    # Startup
    context_cache = PersonalityContextCache()
    personality_engine = PersonalityEngine(config)

    # Background loop (cascade_loop, every 30s)
    context_cache.update_cascade(phase, direction, zscore, notional, aftermath)

    # Background loop (calendar_loop, every 5min)
    context_cache.update_calendar(states_dict)

    # Candle close handler (already fires per candle)
    context_cache.update_atr(symbol, atr_vs_baseline)

    # Performance loop (every 60s)
    context_cache.update_performance(daily_pnl_pct, win_rate)

    # Staking loop (every 60 min)
    context_cache.update_sovereign(stake_balance, sovereign_budget, component_signals)

    # On SIGNAL_READY (hot path)
    ctx = context_cache.build(symbol, coherence, direction, htf)
    params = personality_engine.assess(symbol, ctx)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

import structlog

from core.asset_classes import (
    ASSET_CLASS_ATR_THRESHOLDS,
    PERSONALITY_AVAILABILITY,
    get_asset_class,
)

log = structlog.get_logger(__name__)

# ── Personality Enum ──────────────────────────────────────────────────────────


class Personality(str, Enum):
    SHIELD    = "SHIELD"
    SOVEREIGN = "SOVEREIGN"   # Staking-anchored MAG7 component divergence (equity only)
    AFTERMATH = "AFTERMATH"
    APEX      = "APEX"
    COIL      = "COIL"
    FLOW      = "FLOW"
    SCOUT     = "SCOUT"


# ── Parameters per personality ────────────────────────────────────────────────

@dataclass(frozen=True)
class PersonalityParams:
    """Execution parameters for a personality assignment. Immutable."""
    name:           Personality
    size_mult:      float   # position size multiplier applied to base size
    stop_atr_mult:  float   # stop distance multiplier (× ATR)
    rr_min:         float   # minimum reward:risk ratio required
    coherence_min:  float   # minimum coherence score (documented; not re-gated here)
    max_hold_s:     int     # max hold in seconds (0 = unlimited)
    max_concurrent: int     # max simultaneous positions for this personality
    arb_allowed:    bool = False   # funding arb trades allowed
    directional:    bool = True    # False = no new directional trades
    confidence:     float = 0.5   # baseline confidence (0.0–1.0)

    # ── Aliased accessors for test API compatibility ───────────────────────────
    @property
    def personality(self) -> "Personality":
        """Alias for `.name` — test API compatibility."""
        return self.name

    @property
    def size_multiplier(self) -> float:
        """Alias for `.size_mult` — test API compatibility."""
        return self.size_mult

    @property
    def rr_target(self) -> float:
        """Alias for `.rr_min` — test API compatibility."""
        return self.rr_min


# Internal params — used by PersonalityEngine._build_params()
_INTERNAL_PARAMS: Dict[Personality, PersonalityParams] = {
    Personality.SOVEREIGN: PersonalityParams(
        name=Personality.SOVEREIGN,
        size_mult=1.0,           # base; actual size = stake × component_weight (computed externally)
        stop_atr_mult=2.0,       # wide — spread widening is the risk, not price ATR
        rr_min=1.5,              # target: spread closes to 0 (structural, not momentum)
        coherence_min=0.0,       # not coherence-based — z-score is the signal gate
        max_hold_s=86400,        # 24 hours — divergences resolve within a day
        max_concurrent=2,        # max 2 components simultaneously
        directional=True,
        arb_allowed=False,
        confidence=0.62,         # baseline; actual = function of |z_score| magnitude
    ),
    Personality.SHIELD: PersonalityParams(
        name=Personality.SHIELD,
        size_mult=0.0,
        stop_atr_mult=1.5,
        rr_min=99.0,
        coherence_min=99.0,
        max_hold_s=0,
        max_concurrent=0,
        arb_allowed=False,
        directional=False,
        confidence=0.10,
    ),
    Personality.AFTERMATH: PersonalityParams(
        name=Personality.AFTERMATH,
        size_mult=1.0,
        stop_atr_mult=1.0,    # tight — abort fast if no reclaim
        rr_min=1.5,            # smaller target — mean reversion is quick
        coherence_min=4.0,     # lower floor — aftermath signals are inherently weaker
        max_hold_s=3600,       # 1 hour — aftermath resolves fast or fails
        max_concurrent=2,
        directional=True,
        confidence=0.65,       # highest base confidence — historical best setup
    ),
    Personality.APEX: PersonalityParams(
        name=Personality.APEX,
        size_mult=1.25,
        stop_atr_mult=2.0,     # wide — cascade volatility is real
        rr_min=2.0,
        coherence_min=6.0,
        max_hold_s=0,           # no time limit — ride the cascade
        max_concurrent=3,
        directional=True,
        confidence=0.72,
    ),
    Personality.COIL: PersonalityParams(
        name=Personality.COIL,
        size_mult=0.0,
        stop_atr_mult=1.5,
        rr_min=4.5,
        coherence_min=99.0,    # effectively blocks all directional
        max_hold_s=0,
        max_concurrent=0,
        arb_allowed=True,      # funding arb only
        directional=False,
        confidence=0.30,
    ),
    Personality.FLOW: PersonalityParams(
        name=Personality.FLOW,
        size_mult=1.0,
        stop_atr_mult=1.5,
        rr_min=2.0,
        coherence_min=4.5,
        max_hold_s=14400,      # 4 hours
        max_concurrent=2,
        directional=True,
        confidence=0.62,
    ),
    Personality.SCOUT: PersonalityParams(
        name=Personality.SCOUT,
        size_mult=0.6,
        stop_atr_mult=1.2,
        rr_min=2.5,
        coherence_min=4.0,
        max_hold_s=7200,       # 2 hours
        max_concurrent=2,
        directional=True,
        confidence=0.50,
    ),
}

# Public dict-of-dicts API — compatible with `assertIn(key, params)` pattern in tests
PERSONALITY_PARAMS: Dict[Personality, Dict] = {
    p: {
        "size_multiplier": params.size_mult,
        "stop_atr_mult":   params.stop_atr_mult,
        "rr_target":       params.rr_min,
        "coherence_min":   params.coherence_min,
        "max_hold_s":      params.max_hold_s,
        "max_concurrent":  params.max_concurrent,
        "directional":     params.directional,
        "arb_allowed":     params.arb_allowed,
        "confidence":      params.confidence,
    }
    for p, params in _INTERNAL_PARAMS.items()
}


# ── Personality Context ───────────────────────────────────────────────────────

@dataclass
class PersonalityContext:
    """
    Per-symbol context snapshot for personality assessment.
    Built by PersonalityContextCache.build() — cost ~0.1ms.
    All fields read from in-memory caches; zero I/O.
    """
    # Per-signal inputs (injected at call time)
    symbol:            str
    direction:         str     # "long" | "short"
    coherence:         float   # effective coherence score (post flow-mult)
    htf:               str     # "bullish" | "bearish" | "neutral"

    # Cascade state (updated every 30s by cascade_loop)
    cascade_phase:     str     # "idle" | "blocked" | "primed" | "momentum"
    cascade_direction: str     # "bearish" | "bullish" | "mixed" | ""
    cascade_zscore:    float
    cascade_notional:  float   # USD notional in cascade batch
    aftermath_signals: int     # count of confirmed aftermath signals (0–5)

    # Regime (updated every 15min by ssi/regime loop)
    regime:            str     # "risk_on" | "risk_off" | "confused" | etc.
    regime_confidence: float   # 0.0–1.0

    # ATR (updated per candle close — supporting signal only)
    atr_vs_baseline:   float   # current_atr / 20-bar_avg_atr (self-calibrating)

    # Calendar (updated every 5min)
    calendar_regime:   str     # "BLOCK" | "blackout" | "caution" | "normal" | "CLEAR"
    hours_to_event:    Optional[float]  # hours to next macro event (None = no event)

    # Performance (updated every 60s)
    daily_pnl_pct:     float   # today's PnL as fraction of starting balance
    session_win_rate:  float   # rolling session win rate (0.0–1.0)

    # Risk state (updated from vc_monitor/basis)
    basis_stress_count: int    # number of assets under basis stress
    rpc_health_score:  float   # 0.0–1.0 (1.0 = fully healthy)
    freeze_active:     bool    # True when vc_monitor cascade freeze is active

    # XAUT thermometer (updated per XAUT candle)
    xaut_direction:    str = "neutral"   # "long" | "short" | "neutral"
    xaut_mult:         float = 1.0

    # Post-freeze grace period: seconds the freeze was active before releasing.
    # When > 0 and freeze_active=False, SHIELD persists for grace period.
    freeze_elapsed_s:  float = 0.0

    # SOVEREIGN: staking-anchored divergence fields (updated every 60 min)
    stake_balance:     float = 0.0      # USD staked in MAG7 (default $200 once wired)
    sovereign_budget:  float = 0.0      # available yield-funded budget for SOVEREIGN
    component_signals: Dict = field(default_factory=dict)   # {symbol: z_score}
    best_divergence:   tuple = ("", 0.0)  # (symbol, z_score) — highest |z_score|


# ── Context Cache ─────────────────────────────────────────────────────────────

class PersonalityContextCache:
    """
    Zero-latency context builder for the personality engine.

    Slow-changing fields (regime, cascade, calendar, performance) are cached
    by background loops via update_* methods. Per-signal fields (symbol,
    coherence, direction, htf) are injected at build() call time.

    Thread safety: fields are plain Python assignments. In CPython, simple
    attribute writes are GIL-protected. Sufficient for async hot path.

    Background loop wiring (add to main.py):
        context_cache.update_cascade(phase, direction, zscore, notional, aftermath_count)
        context_cache.update_calendar(states_dict)
        context_cache.update_atr(symbol, atr_vs_baseline)   # per candle close
        context_cache.update_performance(daily_pnl_pct, win_rate)
        context_cache.update_basis_stress(count)
        context_cache.update_rpc_health(fail_count, recovered)
        context_cache.update_freeze(active)
    """

    def __init__(self) -> None:
        # Cascade state
        self._cascade_phase:     str   = "idle"
        self._cascade_direction: str   = ""
        self._cascade_zscore:    float = 0.0
        self._cascade_notional:  float = 0.0
        self._aftermath_signals: int   = 0

        # Regime
        self._regime:            str   = "confused"
        self._regime_confidence: float = 0.5
        self._xaut_direction:    str   = "neutral"
        self._xaut_mult:         float = 1.0

        # Per-symbol ATR ratios
        self._atr_ratios: Dict[str, float] = {}

        # Calendar
        self._calendar_states: Dict[str, object] = {}

        # Performance
        self._daily_pnl_pct:    float = 0.0
        self._session_win_rate: float = 0.5

        # Risk
        self._basis_stress_count: int   = 0
        self._rpc_health_score:   float = 1.0
        self._freeze_active:      bool  = False

        # SOVEREIGN: staking-anchored divergence
        self._stake_balance:     float = 0.0
        self._sovereign_budget:  float = 0.0
        self._component_signals: Dict[str, float] = {}   # {symbol: z_score}
        self._best_divergence:   tuple = ("", 0.0)

    # ── Update methods (called by background loops) ────────────────────────────

    def update_cascade(
        self,
        phase: str,
        direction: str,
        zscore: float,
        notional: float,
        aftermath_signals: int = 0,
    ) -> None:
        """Called by cascade_loop() every 30s."""
        self._cascade_phase     = phase
        self._cascade_direction = direction
        self._cascade_zscore    = zscore
        self._cascade_notional  = notional
        self._aftermath_signals = aftermath_signals

    def update_regime(
        self,
        regime: str,
        confidence: float,
        xaut_direction: str = "neutral",
        xaut_mult: float = 1.0,
    ) -> None:
        """Called by regime/ssi_loop() every 15min."""
        self._regime            = regime
        self._regime_confidence = confidence
        self._xaut_direction    = xaut_direction
        self._xaut_mult         = xaut_mult

    def update_atr(self, symbol: str, atr_vs_baseline: float) -> None:
        """Called on CANDLE_CLOSED for each symbol."""
        self._atr_ratios[symbol] = max(0.0, atr_vs_baseline)

    def update_calendar(self, states: Dict) -> None:
        """Called by calendar_loop() every 5min. states = {symbol: CalendarState}."""
        self._calendar_states = states

    def update_performance(
        self, daily_pnl_pct: float, win_rate: float
    ) -> None:
        """Called by performance/session_loop() every 60s."""
        self._daily_pnl_pct    = daily_pnl_pct
        self._session_win_rate = win_rate

    def update_basis_stress(self, count: int) -> None:
        """Called by basis_loop() every 30s."""
        self._basis_stress_count = max(0, count)

    def update_rpc_health(self, fail_count: int, recovered: bool = False) -> None:
        """Called from vc_monitor on fail/recover."""
        if recovered:
            self._rpc_health_score = min(1.0, self._rpc_health_score + 0.2)
        else:
            self._rpc_health_score = max(0.0, 1.0 - fail_count / 10.0)

    def update_freeze(self, active: bool, elapsed_s: float = 0.0) -> None:
        """Called when vc_monitor freeze activates/releases."""
        self._freeze_active = active
        self._freeze_elapsed = float(elapsed_s)

    def update_sovereign(
        self,
        stake_balance: float,
        sovereign_budget: float,
        component_signals: Dict[str, float],
    ) -> None:
        """
        Called by staking_monitor + yield_tracker background loop (every 60 min).
        Updates SOVEREIGN context fields atomically.
        """
        self._stake_balance    = max(0.0, stake_balance)
        self._sovereign_budget = max(0.0, sovereign_budget)
        self._component_signals = dict(component_signals)
        # Compute best (highest |z_score|) divergence for hot-path access
        if component_signals:
            best_sym = max(component_signals, key=lambda s: abs(component_signals[s]))
            self._best_divergence = (best_sym, component_signals[best_sym])
        else:
            self._best_divergence = ("", 0.0)

    @property
    def _sovereign(self) -> Dict[str, object]:
        """Dict accessor for sovereign_signal_loop — avoids AttributeError crash."""
        return {
            "stake_balance":     self._stake_balance,
            "sovereign_budget":  self._sovereign_budget,
            "component_signals": dict(self._component_signals),
        }

    # ── Hot-path builder ──────────────────────────────────────────────────────

    def build(
        self,
        symbol: str,
        coherence: float,
        direction: str,
        htf: str,
    ) -> PersonalityContext:
        """
        Build PersonalityContext for a SIGNAL_READY event.
        Cost: ~0.1ms (dict lookup + field assignment, no I/O).
        """
        # Extract per-symbol calendar state
        cal_state   = self._calendar_states.get(symbol)
        cal_regime  = getattr(cal_state, "regime",         "normal") if cal_state else "normal"
        cal_hours   = getattr(cal_state, "hours_to_event", None)     if cal_state else None

        return PersonalityContext(
            symbol             = symbol,
            direction          = direction,
            coherence          = coherence,
            htf                = htf,
            cascade_phase      = self._cascade_phase,
            cascade_direction  = self._cascade_direction,
            cascade_zscore     = self._cascade_zscore,
            cascade_notional   = self._cascade_notional,
            aftermath_signals  = self._aftermath_signals,
            regime             = self._regime,
            regime_confidence  = self._regime_confidence,
            atr_vs_baseline    = self._atr_ratios.get(symbol, 1.0),
            calendar_regime    = cal_regime,
            hours_to_event     = cal_hours,
            daily_pnl_pct      = self._daily_pnl_pct,
            session_win_rate   = self._session_win_rate,
            basis_stress_count = self._basis_stress_count,
            rpc_health_score   = self._rpc_health_score,
            freeze_active      = self._freeze_active,
            xaut_direction     = self._xaut_direction,
            xaut_mult          = self._xaut_mult,
            stake_balance      = self._stake_balance,
            sovereign_budget   = self._sovereign_budget,
            component_signals  = dict(self._component_signals),
            best_divergence    = self._best_divergence,
        )


# ── Personality Engine ────────────────────────────────────────────────────────

_DIRECTIONAL_REGIMES = frozenset({
    "risk_on", "risk_off", "btc_dominance", "tech_led", "cex_flow", "defi_infra"
})


class PersonalityEngine:
    """
    Stateless personality assessor with hysteresis.

    assess(symbol, ctx) → PersonalityParams
    Called from hot path — must complete in <0.5ms.
    """

    def __init__(self, config=None) -> None:
        self._config  = config
        # Hysteresis state per symbol
        self._current: Dict[str, Personality]          = {}
        self._pending: Dict[str, Tuple[Personality, int]] = {}
        self._HYSTERESIS  = 3    # consecutive assessments to confirm switch
        # SHIELD and SOVEREIGN activate instantly — structural states, not trend-following.
        # SHIELD: emergency capital preservation. SOVEREIGN: static stake position changes.
        self._HYSTERESIS_HARD = frozenset({Personality.SHIELD, Personality.SOVEREIGN})

    def assess(self, symbol: str, ctx: PersonalityContext) -> PersonalityParams:
        """
        Main entry point. Returns personality params for this signal.
        Hot path — <0.5ms, no I/O.
        """
        raw    = self._raw_personality(symbol, ctx)
        stable = self._apply_hysteresis(symbol, raw)
        params = self._build_params(stable, ctx)

        log.debug("personality_assessed",
                  symbol=symbol,
                  personality=stable.value,
                  direction=ctx.direction,
                  coherence=round(ctx.coherence, 2),
                  cascade_phase=ctx.cascade_phase,
                  regime=ctx.regime,
                  atr_vs_baseline=round(ctx.atr_vs_baseline, 3))

        return params

    def get_current(self, symbol: str) -> Optional[Personality]:
        """Return last assessed personality for a symbol (for display)."""
        return self._current.get(symbol)

    # ── Priority-ordered personality checks ───────────────────────────────────

    def _raw_personality(self, symbol: str, ctx: PersonalityContext) -> Personality:
        """Evaluate conditions in strict priority order."""
        available = PERSONALITY_AVAILABILITY.get(
            get_asset_class(symbol),
            ["SHIELD", "FLOW", "SCOUT", "COIL"],
        )

        # 1. SHIELD — always checked first; capital preservation trumps everything
        if self._is_shield(ctx):
            return Personality.SHIELD

        # 2. SOVEREIGN — staking-anchored divergence (equity only, yield-funded)
        #    Checked before AFTERMATH/APEX: SOVEREIGN is defensive and structural,
        #    not momentum-driven. Its edge is orthogonal to all other personalities.
        if "SOVEREIGN" in available and self._is_sovereign(ctx):
            return Personality.SOVEREIGN

        # 3. AFTERMATH — post-cascade mean reversion (crypto + commodity + equity_index)
        if "AFTERMATH" in available and self._is_aftermath(ctx):
            return Personality.AFTERMATH

        # 4. APEX — cascade momentum aggression (crypto only)
        if "APEX" in available and self._is_apex(ctx):
            return Personality.APEX

        # 4. COIL — weak conviction + directionless regime
        if self._is_coil(ctx):
            return Personality.COIL

        # 5. FLOW — HTF-aligned trend continuation
        if "FLOW" in available and self._is_flow(ctx):
            return Personality.FLOW

        # 6. SCOUT — fallback; above minimum threshold but not clear trend
        return Personality.SCOUT

    # ── Condition checks ──────────────────────────────────────────────────────

    def _is_shield(self, ctx: PersonalityContext) -> bool:
        """Capital preservation — any blocking condition fires SHIELD."""
        # Hard triggers — any one = SHIELD immediately
        if ctx.calendar_regime in ("BLOCK", "blackout"):
            return True
        if ctx.freeze_active:
            return True
        if ctx.basis_stress_count >= 3:
            return True
        if ctx.daily_pnl_pct <= -0.025:     # -2.5% daily loss limit
            return True
        # Post-freeze grace period: if freeze was recently active (elapsed_s > 0),
        # SHIELD persists for 5 minutes after release to let market normalise.
        if not ctx.freeze_active and ctx.freeze_elapsed_s > 0:
            return True

        # Soft triggers — any two = SHIELD
        soft = 0
        if ctx.calendar_regime == "caution":
            soft += 1
        if (ctx.hours_to_event is not None and ctx.hours_to_event < 3.0
                and ctx.calendar_regime not in ("normal", "CLEAR")):
            soft += 1
        if ctx.basis_stress_count >= 2:
            soft += 1
        if ctx.daily_pnl_pct <= -0.015:
            soft += 1
        if ctx.session_win_rate < 0.20 and ctx.daily_pnl_pct < -0.005:
            soft += 1
        return soft >= 2

    def _is_sovereign(self, ctx: PersonalityContext) -> bool:
        """
        Staking-anchored component divergence — yield-funded structural edge.

        Conditions (all required):
          1. stake_balance > 0          — structural anchor exists
          2. sovereign_budget > 0       — yield pool funded; never borrows main capital
          3. |best_divergence z| >= 1.5 — component significantly diverged from MAG7
          4. regime != blackout         — basic calendar filter (hard events block)
          5. calendar_regime != BLOCK   — calendar hard gate

        SOVEREIGN does NOT require coherence or cascade signals.
        Its signal source is orthogonal: spread z-score vs MAG7 index.
        """
        # Must have staked position as structural anchor
        if ctx.stake_balance <= 0:
            return False

        # Must have yield-funded budget — never touches main capital
        if ctx.sovereign_budget <= 0:
            return False

        # Must have a valid component divergence
        best_sym, best_z = ctx.best_divergence
        if not best_sym or abs(best_z) < 1.5:
            return False

        # Calendar hard gates
        if ctx.calendar_regime in ("BLOCK", "blackout"):
            return False

        # Regime must not be completely ambiguous for very low-conviction divergence
        # (pure confused regime + z < 2.0 → noise, not signal)
        if ctx.regime == "confused" and abs(best_z) < 2.0:
            return False

        return True

    def _is_aftermath(self, ctx: PersonalityContext) -> bool:
        """
        Post-cascade mean reversion — highest expected win rate.
        Cascade must be in aftermath window (PRIMED phase).
        Direction must be OPPOSITE to cascade direction (mean reversion).
        """
        # Cascade tracker must be in aftermath window
        if ctx.cascade_phase not in ("primed", "aftermath"):
            return False
        # Need at least 2 confirmed aftermath signals
        if ctx.aftermath_signals < 2:
            return False
        # Direction must be opposite to the cascade (mean reversion logic)
        if ctx.cascade_direction == "bearish" and ctx.direction != "long":
            return False
        if ctx.cascade_direction == "bullish" and ctx.direction != "short":
            return False
        # Calendar must not block
        if ctx.calendar_regime in ("BLOCK", "blackout"):
            return False
        return True

    def _is_apex(self, ctx: PersonalityContext) -> bool:
        """Maximum aggression during confirmed liquidation cascade."""
        # Cascade must be active and dangerous
        if ctx.cascade_phase not in ("blocked", "momentum"):
            return False
        # High conviction signal required — low-coherence signals are noise during cascade
        if ctx.coherence < 6.0:
            return False
        # Direction must match cascade direction
        if ctx.cascade_direction not in ("", "mixed"):
            if ctx.direction == "long" and ctx.cascade_direction != "bullish":
                return False
            if ctx.direction == "short" and ctx.cascade_direction != "bearish":
                return False
        # Significant cascade notional — filters micro-cascade false positives
        if ctx.cascade_notional < 10_000:
            return False
        # RPC must be healthy — APEX depends on cascade data from ValueChain RPC
        if ctx.rpc_health_score < 0.70:
            return False
        # Calendar must be clear
        if ctx.calendar_regime in ("BLOCK", "blackout"):
            return False
        return True

    def _is_coil(self, ctx: PersonalityContext) -> bool:
        """
        Market is in a coiling/consolidation regime — no directional edge.

        Primary discriminator: ATR vs baseline (market is literally coiling).
        A symbol with ATR below its own baseline indicates compression/consolidation
        regardless of signal strength.

        No active cascade required (idle or primed phases only).
        """
        # No active cascade — cascade phases use AFTERMATH/APEX
        if ctx.cascade_phase not in ("idle", "primed"):
            return False

        # Primary gate: ATR vs baseline below asset-class threshold
        # Missing ATR data (0.0) does NOT force COIL — pass through to SCOUT
        _cls    = get_asset_class(ctx.symbol)
        _thresh = ASSET_CLASS_ATR_THRESHOLDS.get(_cls, 0.80)
        return 0.0 < ctx.atr_vs_baseline < _thresh

    def _is_flow(self, ctx: PersonalityContext) -> bool:
        """Confident HTF-aligned trend continuation — core profitability."""
        # No active cascade
        if ctx.cascade_phase not in ("idle", "primed"):
            return False

        # Minimum coherence
        if ctx.coherence < 4.5:
            return False

        # HTF must be aligned with signal direction
        htf_aligned = (
            (ctx.direction == "long" and ctx.htf == "bullish") or
            (ctx.direction == "short" and ctx.htf == "bearish")
        )
        if not htf_aligned:
            return False

        # Regime must be directional (or coherence compensates)
        if ctx.regime not in _DIRECTIONAL_REGIMES and ctx.coherence < 5.5:
            return False

        # Too much basis stress degrades FLOW to SCOUT
        if ctx.basis_stress_count >= 2:
            return False

        return True

    # ── Hysteresis ────────────────────────────────────────────────────────────

    def _apply_hysteresis(self, symbol: str, raw: Personality) -> Personality:
        """
        Require N consecutive assessments before switching personality.
        SHIELD switches instantly (no hysteresis — safety first).
        """
        # SHIELD is always instant — no hysteresis on defensive state
        if raw in self._HYSTERESIS_HARD:
            self._current[symbol] = raw
            self._pending.pop(symbol, None)
            return raw

        current = self._current.get(symbol, raw)

        if raw == current:
            self._pending.pop(symbol, None)
            return current

        pending_p, count = self._pending.get(symbol, (raw, 0))

        if pending_p != raw:
            # Different target — reset counter
            self._pending[symbol] = (raw, 1)
            return current

        count += 1
        if count >= self._HYSTERESIS:
            # Confirmed switch
            self._current[symbol] = raw
            self._pending.pop(symbol, None)
            log.info("personality_switched",
                     symbol=symbol,
                     from_personality=current.value,
                     to_personality=raw.value,
                     confirmations=count)
            return raw

        self._pending[symbol] = (raw, count)
        return current

    # ── Parameter builder ─────────────────────────────────────────────────────

    def _build_params(
        self, personality: Personality, ctx: PersonalityContext
    ) -> PersonalityParams:
        """
        Return base params with RPC health reduction applied if needed.
        APEX is already blocked at _is_apex level when RPC degraded.
        FLOW/SCOUT/AFTERMATH get proportional size reduction when RPC < 0.70.
        """
        base = _INTERNAL_PARAMS[personality]

        if (ctx.rpc_health_score < 0.70
                and personality not in (Personality.SHIELD, Personality.SOVEREIGN,
                                        Personality.COIL, Personality.APEX)):
            rpc_mult = max(0.3, ctx.rpc_health_score)
            reduced_size = round(base.size_mult * rpc_mult, 3)
            log.info("rpc_degraded_personality_adjusted",
                     symbol=ctx.symbol,
                     personality=personality.value,
                     rpc_health=round(ctx.rpc_health_score, 2),
                     size_mult_orig=base.size_mult,
                     size_mult_adj=reduced_size)
            return PersonalityParams(
                name=base.name,
                size_mult=reduced_size,
                stop_atr_mult=base.stop_atr_mult,
                rr_min=base.rr_min,
                coherence_min=base.coherence_min,
                max_hold_s=base.max_hold_s,
                max_concurrent=base.max_concurrent,
                arb_allowed=base.arb_allowed,
                directional=base.directional,
                confidence=base.confidence,
            )

        return base

    # ── Test-API alias for current personality ───────────────────────────────
    @property
    def _current_personality(self) -> Dict[str, Personality]:
        """Alias for _current — allows `engine._current_personality["BTC-USD"] = p` in tests."""
        return self._current


# ── Module-level aliases ──────────────────────────────────────────────────────

# Test API alias — same class, different name
MarketPersonalityEngine = PersonalityEngine

# PERSONALITY_AVAILABILITY re-exported for direct import by tests
from core.asset_classes import PERSONALITY_AVAILABILITY  # noqa: F401, E402
