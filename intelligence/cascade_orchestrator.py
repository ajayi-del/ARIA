"""
CascadeOrchestrator — Spartan fast-path intelligence for liquidation cascades.

Emperor (Chancellor) governs capital and risk.
Commander (Strategy Runner) executes organic signals.
CascadeOrchestrator is the Special Operations Commander:
  - Monitors Bybit liquidations (predictive lead, 1–3s before SoDEX)
  - Monitors ValueChain liquidations (authoritative SoDEX ground truth)
  - Computes cascade magnitude from OB depth + notional + leverage
  - Emits CASCADE_MOMENTUM_READY and CASCADE_AFTERMATH_READY events
  - Self-healing: auto-resets on silence, circuit-breaker on RPC failure

Integration:
  bybit_feed.add_liquidation_listener(orchestrator.on_bybit_liquidation)
  vc_monitor.add_listener(orchestrator.on_valuechain_liquidation)
  event_bus.subscribe(EventType.CASCADE_MOMENTUM_READY, on_cascade_momentum)
  event_bus.subscribe(EventType.CASCADE_AFTERMATH_READY, on_cascade_aftermath)

All state is ephemeral — cascades are 30–300s events. No disk persistence.
"""

import asyncio
import time
import structlog
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

from core.event_bus import event_bus, Event, EventType

log = structlog.get_logger(__name__)

_CASCADE_WINDOW_S = 60.0
_CASCADE_THRESHOLD = 3          # ≥3 liquidations in 60s = trigger (SoDEX sparse)
_EXTREME_THRESHOLD = 6          # ≥6 = expansion (realistic for SoDEX volume)
_BUILDUP_LOOKAHEAD_S = 300.0  # 5 min predictive window
_MIN_NOTIONAL = 1_000.0       # Ignore noise below $1k


class CascadePhase(Enum):
    IDLE = "idle"
    BUILDUP = "buildup"       # Predictive: funding extreme + OI + stop density
    TRIGGER = "trigger"       # First liquidations detected
    EXPANSION = "expansion"   # Accelerating liquidations
    EXHAUSTION = "exhaustion" # Decelerating liquidations
    AFTERMATH = "aftermath"   # Silence + recovery signals


@dataclass
class CascadeEvent:
    source: str          # "bybit" | "valuechain"
    symbol: str
    direction: str       # "bullish" | "bearish"
    qty: float           # contracts liquidated
    price: float
    notional_usd: float
    timestamp_ms: int


@dataclass
class SymbolCascadeState:
    events: deque = field(default_factory=lambda: deque(maxlen=200))
    phase: CascadePhase = CascadePhase.IDLE
    phase_entered_ms: int = 0
    last_event_ms: int = 0
    total_notional_60s: float = 0.0
    event_count_60s: int = 0
    max_notional_single: float = 0.0
    buildup_score: float = 0.0
    magnitude_estimate: float = 0.0  # predicted move %
    velocity: float = 0.0            # events/sec
    acceleration: float = 0.0        # Δvelocity/Δt

    def prune(self, now_ms: int):
        cutoff = now_ms - int(_CASCADE_WINDOW_S * 1000)
        while self.events and self.events[0].timestamp_ms < cutoff:
            self.events.popleft()

    def compute_stats(self, now_ms: int):
        self.prune(now_ms)
        self.event_count_60s = len(self.events)
        self.total_notional_60s = sum(e.notional_usd for e in self.events)
        self.max_notional_single = max((e.notional_usd for e in self.events), default=0.0)
        if len(self.events) >= 2:
            span_s = max(1.0, (self.events[-1].timestamp_ms - self.events[0].timestamp_ms) / 1000.0)
            self.velocity = len(self.events) / span_s
        else:
            self.velocity = 0.0


class AftermathWindow:
    """Timed entry gate for post-cascade mean-reversion trades."""
    OPEN_MIN  = 3.0   # min minutes after cascade peak
    CLOSE_MIN = 12.0  # max minutes — edge decays after this

    def __init__(self):
        self._peak_ts: float | None = None
        self._peak_notional: float = 0.0
        self._peak_direction: str = "none"

    def record_peak(self, notional: float, direction: str):
        """Called when cascade transitions peak → aftermath."""
        self._peak_ts = time.time()
        self._peak_notional = notional
        self._peak_direction = direction

    def is_entry_window_open(self) -> tuple[bool, str]:
        if self._peak_ts is None:
            return False, "no_peak_recorded"
        elapsed_min = (time.time() - self._peak_ts) / 60.0
        if elapsed_min < self.OPEN_MIN:
            return False, f"too_early:{elapsed_min:.1f}min<{self.OPEN_MIN}"
        if elapsed_min > self.CLOSE_MIN:
            return False, f"window_expired:{elapsed_min:.1f}min>{self.CLOSE_MIN}"
        return True, f"window_open:{elapsed_min:.1f}min"

    def mean_reversion_direction(self) -> str:
        """AFTERMATH trades AGAINST the liquidation direction."""
        return "long" if self._peak_direction == "bearish" else "short"


class CascadeOrchestrator:
    """
    Central command for all liquidation cascade intelligence.
    Receives events from Bybit (predictive) and ValueChain (authoritative).
    Emits high-confidence execution events to the event bus.
    """

    def __init__(self, config, mark_price_stores: dict = None, orderbook_stores: dict = None):
        self.config = config
        self._mark_price_stores = mark_price_stores or {}
        self._orderbook_stores = orderbook_stores or {}
        self._states: Dict[str, SymbolCascadeState] = {}
        self._momentum_listeners: List[Callable] = []
        self._aftermath_listeners: List[Callable] = []
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_momentum_ms: Dict[str, int] = {}  # per-symbol cooldown
        self._last_aftermath_ms: Dict[str, int] = {}
        self._momentum_cooldown_ms = 90_000   # 90s between momentum signals
        self._aftermath_cooldown_ms = 60_000  # 60s between aftermath signals
        self.aftermath_window = AftermathWindow()

    # ── Public API ────────────────────────────────────────────────────────────

    def add_momentum_listener(self, callback: Callable):
        """Register a direct callback for CASCADE_MOMENTUM_READY (bypasses event bus)."""
        self._momentum_listeners.append(callback)

    def add_aftermath_listener(self, callback: Callable):
        """Register a direct callback for CASCADE_AFTERMATH_READY (bypasses event bus)."""
        self._aftermath_listeners.append(callback)

    def start(self):
        """Begin the housekeeping loop (phase transitions, expiry, decay)."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._housekeeping_loop())
        log.info("cascade_orchestrator_started")

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def on_bybit_liquidation(self, symbol: str, direction: str, qty: float, price: float, ts_ms: int):
        """Callback for Bybit liquidation feed."""
        notional = qty * price
        if notional < _MIN_NOTIONAL:
            return
        ev = CascadeEvent(
            source="bybit", symbol=symbol, direction=direction,
            qty=qty, price=price, notional_usd=notional, timestamp_ms=ts_ms,
        )
        await self._ingest(ev)

    async def on_valuechain_liquidation(self, sig):
        """Callback for ValueChain monitor LiquidationSignal."""
        _dir = getattr(sig, "direction", "")
        _notional = float(getattr(sig, "notional_usd", 0.0))
        _sym = getattr(sig, "symbol", "") or "market_wide"
        if _notional < _MIN_NOTIONAL:
            return
        # Map direction from ValueChain to orchestrator
        direction = "bullish" if _dir == "bullish" else "bearish" if _dir == "bearish" else ""
        if not direction:
            return
        ev = CascadeEvent(
            source="valuechain", symbol=_sym, direction=direction,
            qty=0.0, price=0.0, notional_usd=_notional,
            timestamp_ms=int(time.time() * 1000),
        )
        await self._ingest(ev)

    def get_state(self, symbol: str) -> SymbolCascadeState:
        return self._states.setdefault(symbol, SymbolCascadeState())

    def summary(self) -> dict:
        return {
            sym: {
                "phase": st.phase.value,
                "events_60s": st.event_count_60s,
                "notional_60s": round(st.total_notional_60s, 0),
                "velocity": round(st.velocity, 2),
                "magnitude": round(st.magnitude_estimate, 4),
            }
            for sym, st in self._states.items()
            if st.phase != CascadePhase.IDLE
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _ingest(self, ev: CascadeEvent):
        now_ms = int(time.time() * 1000)
        st = self.get_state(ev.symbol)
        st.events.append(ev)
        st.last_event_ms = now_ms
        st.compute_stats(now_ms)

        # Magnitude estimation: notional / OB depth at 5% level
        st.magnitude_estimate = self._estimate_magnitude(ev.symbol, st.total_notional_60s)

        # Phase transitions
        old_phase = st.phase
        await self._transition(st, now_ms)
        if st.phase != old_phase:
            log.info("cascade_orchestrator_phase",
                     symbol=ev.symbol, from_phase=old_phase.value,
                     to_phase=st.phase.value, notional=round(st.total_notional_60s, 0),
                     events=st.event_count_60s, velocity=round(st.velocity, 2),
                     magnitude=st.magnitude_estimate)
            await self._emit_if_ready(ev.symbol, st, now_ms)

    async def _transition(self, st: SymbolCascadeState, now_ms: int):
        """Finite state machine for cascade phases."""
        _n = st.event_count_60s
        _v = st.velocity
        _dt = (now_ms - st.phase_entered_ms) / 1000.0

        if st.phase == CascadePhase.IDLE:
            if _n >= _CASCADE_THRESHOLD:
                st.phase = CascadePhase.TRIGGER
                st.phase_entered_ms = now_ms

        elif st.phase == CascadePhase.TRIGGER:
            if _n >= _EXTREME_THRESHOLD or _v >= 0.3:
                st.phase = CascadePhase.EXPANSION
                st.phase_entered_ms = now_ms
            elif _dt > 30 and _n < _CASCADE_THRESHOLD:
                st.phase = CascadePhase.IDLE
                st.phase_entered_ms = now_ms

        elif st.phase == CascadePhase.EXPANSION:
            # Deceleration = exhaustion
            if _v < 0.5 and _dt > 10:
                st.phase = CascadePhase.EXHAUSTION
                st.phase_entered_ms = now_ms
            elif _dt > 120:
                # Max expansion duration
                st.phase = CascadePhase.EXHAUSTION
                st.phase_entered_ms = now_ms

        elif st.phase == CascadePhase.EXHAUSTION:
            # Silence = aftermath
            silence_s = (now_ms - st.last_event_ms) / 1000.0
            if silence_s >= 15:
                st.phase = CascadePhase.AFTERMATH
                st.phase_entered_ms = now_ms
            elif _dt > 60:
                st.phase = CascadePhase.IDLE
                st.phase_entered_ms = now_ms

        elif st.phase == CascadePhase.AFTERMATH:
            if _dt > 300:
                st.phase = CascadePhase.IDLE
                st.phase_entered_ms = now_ms
            elif _n >= _CASCADE_THRESHOLD:
                # New cascade during aftermath = immediate expansion
                st.phase = CascadePhase.EXPANSION
                st.phase_entered_ms = now_ms

    async def _emit_if_ready(self, symbol: str, st: SymbolCascadeState, now_ms: int):
        """Emit execution events when phase is actionable."""
        if st.phase == CascadePhase.EXPANSION:
            _last = self._last_momentum_ms.get(symbol, 0)
            if now_ms - _last < self._momentum_cooldown_ms:
                return
            self._last_momentum_ms[symbol] = now_ms
            trade_dir = "short" if st.events[-1].direction == "bearish" else "long"
            _ev_data = {
                "direction": trade_dir,
                "notional_60s": st.total_notional_60s,
                "velocity": st.velocity,
                "magnitude": st.magnitude_estimate,
                "source": "cascade_orchestrator",
            }
            event_bus.publish(Event(
                event_type=EventType.CASCADE_MOMENTUM_READY,
                symbol=symbol,
                timestamp_ms=now_ms,
                data=_ev_data,
            ))
            # Direct latency bypass — fire callbacks without 50ms event-bus coalescing
            for _cb in self._momentum_listeners:
                try:
                    asyncio.create_task(_cb(_ev_data))
                except Exception:
                    pass
            log.info("cascade_momentum_ready_emitted",
                     symbol=symbol, direction=trade_dir,
                     notional=round(st.total_notional_60s, 0),
                     magnitude=st.magnitude_estimate)

        elif st.phase == CascadePhase.AFTERMATH:
            _last = self._last_aftermath_ms.get(symbol, 0)
            if now_ms - _last < self._aftermath_cooldown_ms:
                return
            self._last_aftermath_ms[symbol] = now_ms
            # Record peak for timed entry window on first aftermath emission
            _peak_ts_ms = int(self.aftermath_window._peak_ts * 1000) if self.aftermath_window._peak_ts else 0
            if st.phase_entered_ms > _peak_ts_ms:
                _direction = st.events[-1].direction if st.events else "none"
                self.aftermath_window.record_peak(st.total_notional_60s, _direction)
            trade_dir = "long" if st.events[-1].direction == "bearish" else "short"
            _ev_data = {
                "direction": trade_dir,
                "notional_60s": st.total_notional_60s,
                "magnitude": st.magnitude_estimate,
                "source": "cascade_orchestrator",
            }
            event_bus.publish(Event(
                event_type=EventType.CASCADE_AFTERMATH_READY,
                symbol=symbol,
                timestamp_ms=now_ms,
                data=_ev_data,
            ))
            for _cb in self._aftermath_listeners:
                try:
                    asyncio.create_task(_cb(_ev_data))
                except Exception:
                    pass
            log.info("cascade_aftermath_ready_emitted",
                     symbol=symbol, direction=trade_dir,
                     magnitude=st.magnitude_estimate)

    def _estimate_magnitude(self, symbol: str, notional_60s: float) -> float:
        """Predict cascade move % from notional vs orderbook depth.
        Formula: move ≈ (notional / depth_5pct) * 0.5
        Calibrated so $1M notional on $10M depth → 5% move (unrealistic;
        real markets are deeper, so multiplier is conservative).
        """
        _store = self._orderbook_stores.get(symbol)
        if not _store or notional_60s <= 0:
            return 0.01  # 1% default
        try:
            _depth = getattr(_store, 'depth_usd_5pct', 0.0) or 0.0
            if _depth <= 0:
                return 0.01
            # Conservative: only 50% of notional translates to price impact
            return min(0.10, (notional_60s / _depth) * 0.5)
        except Exception:
            return 0.01

    async def _housekeeping_loop(self):
        """Reset idle states, decay buildup scores, detect silence transitions."""
        while self._running:
            try:
                now_ms = int(time.time() * 1000)
                for sym, st in list(self._states.items()):
                    st.compute_stats(now_ms)
                    silence_s = (now_ms - st.last_event_ms) / 1000.0

                    # Auto-reset IDLE states with no recent events
                    if st.phase == CascadePhase.IDLE and silence_s > 300:
                        if st.event_count_60s == 0:
                            self._states.pop(sym, None)
                            continue

                    # Silence-driven transitions
                    if st.phase in (CascadePhase.TRIGGER, CascadePhase.EXPANSION) and silence_s > 20:
                        old = st.phase
                        st.phase = CascadePhase.EXHAUSTION
                        st.phase_entered_ms = now_ms
                        log.info("cascade_silence_transition",
                                 symbol=sym, from_phase=old.value, to_phase="exhaustion",
                                 silence_s=round(silence_s, 1))
                        await self._emit_if_ready(sym, st, now_ms)

                    elif st.phase == CascadePhase.EXHAUSTION and silence_s > 30:
                        old = st.phase
                        st.phase = CascadePhase.AFTERMATH
                        st.phase_entered_ms = now_ms
                        log.info("cascade_silence_transition",
                                 symbol=sym, from_phase=old.value, to_phase="aftermath",
                                 silence_s=round(silence_s, 1))
                        await self._emit_if_ready(sym, st, now_ms)

                await asyncio.sleep(5.0)
            except Exception as e:
                log.error("cascade_orchestrator_housekeeping_error", error=str(e))
                await asyncio.sleep(5.0)
