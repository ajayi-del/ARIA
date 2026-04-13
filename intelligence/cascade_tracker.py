"""
CascadeTracker — Liquidation cascade state machine for ARIA.

Phase transitions:
  IDLE       → DETECTING   : first liquidation batch arrives
  DETECTING  → BLOCKED     : ≥CASCADE_THRESHOLD events in window
  BLOCKED    → PRIMED      : aftermath conditions met (≥3 of 5 recovery signals)
  BLOCKED    → MOMENTUM    : second derivative of event count is accelerating
  PRIMED     → IDLE        : primed signal consumed (trade fired)
  MOMENTUM   → IDLE        : momentum signal consumed
  Any        → IDLE        : 90s silence after BLOCKED

Exhaustion cascade (decelerating liquidation rate):
  - Wait for PRIMED before trading
  - Trade AGAINST the cascade direction (recovery play)

Momentum cascade (accelerating liquidation rate):
  - Trade WITH cascade direction immediately
  - Tight stop: 0.3% or 0.5×ATR, 120s expiry
  - Requires velocity > threshold AND notional > threshold
"""

import time
import structlog
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

log = structlog.get_logger(__name__)

CASCADE_COOLDOWN_MS       = 90_000   # 90s dedup — only one cascade signal per batch
CASCADE_BLOCKED_TIMEOUT_S = 90.0     # Auto-release BLOCKED → IDLE after 90s silence
AFTERMATH_MIN_SIGNALS     = 3        # Need ≥3 of 5 aftermath signals to go PRIMED
PRIMED_EXPIRY_S           = 300.0    # PRIMED auto-expires after 5 min if not consumed
MOMENTUM_EXPIRY_S         = 120.0    # MOMENTUM auto-expires after 2 min


class CascadePhase(Enum):
    IDLE      = "idle"
    DETECTING = "detecting"
    BLOCKED   = "blocked"
    PRIMED    = "primed"
    MOMENTUM  = "momentum"


@dataclass
class CascadeSnapshot:
    """Immutable record of a completed cascade batch."""
    batch_notional_usd: float
    batch_direction: str          # "bearish" | "bullish"
    event_count: int
    detected_at: float            # unix timestamp
    velocity: float               # second derivative (Δevents/Δt — positive = accelerating)

    @property
    def trade_dir_momentum(self) -> str:
        """Trade WITH the pressure during momentum cascade."""
        return "short" if self.batch_direction == "bearish" else "long"

    @property
    def trade_dir_recovery(self) -> str:
        """Trade AGAINST the pressure during recovery aftermath."""
        return "long" if self.batch_direction == "bearish" else "short"


class CascadeTracker:
    """
    State machine tracking liquidation cascade phases for ARIA.

    Integration in main.py:
        cascade_tracker = CascadeTracker(
            config, mark_price_stores, funding_history, vpin_calculator
        )
        # Called from on_liquidation_signal() in main.py when cascade=True
        cascade_tracker.on_liquidation_batch(events_count, total_notional, direction)

        # Called every 15s from aftermath_loop task in main.py
        cascade_tracker.check_aftermath()

        # Consumed in execution path
        if cascade_tracker.is_primed():
            direction = cascade_tracker.consume_primed()
        if cascade_tracker.is_momentum():
            direction, notional = cascade_tracker.consume_momentum()
    """

    def __init__(
        self,
        config,
        mark_price_stores: Dict = None,
        funding_history=None,
        vpin_calculator=None,
    ):
        self._config = config
        self._mark_price_stores = mark_price_stores or {}
        self._funding_history = funding_history
        self._vpin_calculator = vpin_calculator

        self._phase: CascadePhase = CascadePhase.IDLE
        self._last_snapshot: Optional[CascadeSnapshot] = None
        self._last_event_ts: float = 0.0
        self._last_cascade_signal_ms: int = 0  # dedup cooldown timestamp

        # Velocity tracking: timestamps of recent liquidation arrivals (for 2nd derivative)
        self._event_timestamps: deque = deque(maxlen=30)

        # Pre-cascade price references for overshoot detection (symbol → price)
        self._pre_cascade_prices: Dict[str, float] = {}

        # Aftermath state
        self._aftermath_signals: Dict[str, bool] = {}
        self._primed_direction: str = ""
        self._primed_at: float = 0.0

        # Momentum state
        self._momentum_direction: str = ""
        self._momentum_notional: float = 0.0
        self._momentum_at: float = 0.0

    # ── Public API ─────────────────────────────────────────────────────────────

    def on_liquidation_batch(
        self,
        events_in_window: int,
        total_notional: float,
        direction: str,
        symbol: str = "",
    ) -> None:
        """
        Called when cascade threshold (≥3 liqs in 60s) is met.
        Applies 90s cooldown dedup to prevent 30×-per-batch firing.

        direction: "bearish" (longs liquidated) | "bullish" (shorts liquidated)
        """
        now = time.time()
        now_ms = int(now * 1000)

        # ── Cooldown dedup: fire once per 90s ──────────────────────────────────
        if (now_ms - self._last_cascade_signal_ms) < CASCADE_COOLDOWN_MS:
            log.debug("cascade_cooldown_active",
                      remaining_s=round((CASCADE_COOLDOWN_MS - (now_ms - self._last_cascade_signal_ms)) / 1000, 1))
            return

        self._last_cascade_signal_ms = now_ms
        self._last_event_ts = now
        self._event_timestamps.append(now)

        # ── Capture pre-cascade prices ─────────────────────────────────────────
        if self._phase in (CascadePhase.IDLE, CascadePhase.DETECTING):
            self._snapshot_prices()

        # ── Compute velocity ───────────────────────────────────────────────────
        velocity = self._compute_velocity()

        snapshot = CascadeSnapshot(
            batch_notional_usd=total_notional,
            batch_direction=direction,
            event_count=events_in_window,
            detected_at=now,
            velocity=velocity,
        )
        self._last_snapshot = snapshot
        prev_phase = self._phase

        # ── Classify and transition ────────────────────────────────────────────
        if self._is_momentum_cascade(velocity, total_notional):
            self._phase = CascadePhase.MOMENTUM
            self._momentum_direction = snapshot.trade_dir_momentum
            self._momentum_notional = total_notional
            self._momentum_at = now
            log.info("cascade_momentum_detected",
                     direction=direction,
                     trade_dir=self._momentum_direction,
                     velocity=round(velocity, 3),
                     notional_usd=round(total_notional, 0))
        else:
            # Exhaustion cascade → BLOCKED until aftermath confirms
            self._phase = CascadePhase.BLOCKED
            self._aftermath_signals = {}
            log.info("cascade_blocked",
                     prev_phase=prev_phase.value,
                     direction=direction,
                     events=events_in_window,
                     notional_usd=round(total_notional, 0),
                     velocity=round(velocity, 3))

    def check_aftermath(self) -> None:
        """
        Called every 15s from aftermath_loop task in main.py.
        Evaluates 5 recovery signals when BLOCKED.
        BLOCKED → PRIMED when ≥ AFTERMATH_MIN_SIGNALS confirm.
        Also handles auto-timeout (90s silence → IDLE).
        """
        now = time.time()

        if self._phase == CascadePhase.BLOCKED:
            if now - self._last_event_ts > CASCADE_BLOCKED_TIMEOUT_S:
                log.info("cascade_timeout_idle",
                         silence_s=round(now - self._last_event_ts, 1))
                self._phase = CascadePhase.IDLE
                return

            if not self._last_snapshot:
                return

            confirmations = self._evaluate_aftermath()
            self._aftermath_signals = confirmations
            n_confirmed = sum(1 for v in confirmations.values() if v)

            if n_confirmed >= AFTERMATH_MIN_SIGNALS:
                recovery_dir = self._last_snapshot.trade_dir_recovery
                self._phase = CascadePhase.PRIMED
                self._primed_direction = recovery_dir
                self._primed_at = now
                log.info("cascade_primed",
                         direction=recovery_dir,
                         confirmed=n_confirmed,
                         signals=confirmations)

        elif self._phase == CascadePhase.PRIMED:
            if now - self._primed_at > PRIMED_EXPIRY_S:
                log.info("cascade_primed_expired")
                self._phase = CascadePhase.IDLE

        elif self._phase == CascadePhase.MOMENTUM:
            if now - self._momentum_at > MOMENTUM_EXPIRY_S:
                log.info("cascade_momentum_expired")
                self._phase = CascadePhase.IDLE

    def is_blocked(self) -> bool:
        return self._phase == CascadePhase.BLOCKED

    def is_primed(self) -> bool:
        return self._phase == CascadePhase.PRIMED

    def consume_primed(self) -> str:
        """Returns primed trade direction and resets to IDLE."""
        direction = self._primed_direction
        self._phase = CascadePhase.IDLE
        self._primed_direction = ""
        log.info("cascade_primed_consumed", direction=direction)
        return direction

    def get_primed_direction(self) -> str:
        return self._primed_direction

    def is_momentum(self) -> bool:
        return self._phase == CascadePhase.MOMENTUM

    def consume_momentum(self) -> Tuple[str, float]:
        """Returns (trade_direction, total_notional_usd) and resets to IDLE."""
        direction = self._momentum_direction
        notional = self._momentum_notional
        self._phase = CascadePhase.IDLE
        self._momentum_direction = ""
        self._momentum_notional = 0.0
        log.info("cascade_momentum_consumed", direction=direction,
                 notional_usd=round(notional, 0))
        return direction, notional

    def get_cascade_velocity(self) -> float:
        return self._last_snapshot.velocity if self._last_snapshot else 0.0

    def get_phase(self) -> CascadePhase:
        return self._phase

    def get_aftermath_signals(self) -> Dict[str, bool]:
        return dict(self._aftermath_signals)

    def get_snapshot(self) -> Optional[CascadeSnapshot]:
        return self._last_snapshot

    def get_summary(self) -> Dict:
        snap = self._last_snapshot
        return {
            "phase": self._phase.value,
            "primed_direction": self._primed_direction,
            "momentum_direction": self._momentum_direction,
            "velocity": round(self.get_cascade_velocity(), 3),
            "aftermath_signals": dict(self._aftermath_signals),
            "snapshot": {
                "direction": snap.batch_direction,
                "notional_usd": round(snap.batch_notional_usd, 0),
                "events": snap.event_count,
                "velocity": round(snap.velocity, 3),
            } if snap else None,
        }

    # ── Internal ───────────────────────────────────────────────────────────────

    def _snapshot_prices(self) -> None:
        """Record current mark prices as pre-cascade reference."""
        for sym, store in self._mark_price_stores.items():
            try:
                mp = 0.0
                if hasattr(store, "latest_mark"):
                    mp = store.latest_mark or 0.0
                if mp <= 0 and hasattr(store, "_mark"):
                    mp = store._mark or 0.0
                if mp > 0:
                    self._pre_cascade_prices[sym] = mp
            except Exception:
                pass

    def _compute_velocity(self) -> float:
        """
        Second derivative of event rate (acceleration of liquidations).
        Compares event counts in two consecutive 15s windows.
        Positive = accelerating, negative = decelerating.
        """
        events = list(self._event_timestamps)
        if len(events) < 3:
            return 0.0
        now = time.time()
        window_a = sum(1 for t in events if now - 30.0 < t <= now - 15.0)
        window_b = sum(1 for t in events if now - 15.0 < t <= now)
        return (window_b - window_a) / 15.0

    def _is_momentum_cascade(self, velocity: float, total_notional: float) -> bool:
        vel_threshold = getattr(self._config, "momentum_velocity_threshold", 3.0)
        notional_threshold = getattr(self._config, "momentum_notional_threshold", 50_000.0)
        return velocity > vel_threshold and total_notional >= notional_threshold

    def _evaluate_aftermath(self) -> Dict[str, bool]:
        """Evaluate 5 recovery signals for BLOCKED → PRIMED transition."""
        if not self._last_snapshot:
            return {}

        direction = self._last_snapshot.batch_direction
        return {
            "price_overshoot":        self._check_price_overshoot(direction),
            "vpin_recovering":        self._check_vpin_recovering(),
            "funding_normalising":    self._check_funding_normalising(direction),
            "orderbook_rebuilding":   self._check_orderbook_rebuilding(),
            "cross_venue_normalising":self._check_cross_venue_normalising(),
        }

    def _check_price_overshoot(self, direction: str) -> bool:
        """True if price is ≥0.5% from pre-cascade level (cascade pushed it hard)."""
        for sym, pre_price in self._pre_cascade_prices.items():
            if pre_price <= 0:
                continue
            store = self._mark_price_stores.get(sym)
            if not store:
                continue
            try:
                curr = getattr(store, "latest_mark", None) or getattr(store, "_mark", 0.0)
                if not curr or curr <= 0:
                    continue
                move = (curr - pre_price) / pre_price
                if direction == "bearish" and move < -0.005:
                    return True
                if direction == "bullish" and move > 0.005:
                    return True
            except Exception:
                pass
        return False

    def _check_vpin_recovering(self) -> bool:
        """True if any major symbol VPIN is above 0.5 (informed flow still active)."""
        if not self._vpin_calculator:
            return False
        try:
            for sym in ("BTC-USD", "ETH-USD", "SOL-USD"):
                store = self._mark_price_stores.get(sym)
                if not store:
                    continue
                vpin = getattr(store, "_vpin", None)
                if vpin is not None and float(vpin) > 0.50:
                    return True
        except Exception:
            pass
        return False

    def _check_funding_normalising(self, direction: str) -> bool:
        """True if funding rate moved toward neutral since cascade."""
        if not self._funding_history:
            return False
        try:
            for sym in ("BTC-USD", "ETH-USD"):
                rates = self._funding_history.get_rates(sym, 3)
                if len(rates) < 2:
                    continue
                if direction == "bearish" and rates[-1] < rates[-2]:
                    return True
                if direction == "bullish" and rates[-1] > rates[-2]:
                    return True
        except Exception:
            pass
        return False

    def _check_orderbook_rebuilding(self) -> bool:
        """True if major symbol OB stores are healthy (fresh data = book is active)."""
        try:
            for sym in ("BTC-USD", "ETH-USD"):
                store = self._mark_price_stores.get(sym)
                if not store:
                    continue
                if hasattr(store, "is_healthy") and store.is_healthy(5000):
                    return True
                last_upd = getattr(store, "_last_update_ms", None)
                if last_upd and (int(time.time() * 1000) - last_upd) < 5000:
                    return True
        except Exception:
            pass
        return True  # Default True — missing data should not block PRIMED transition

    def _check_cross_venue_normalising(self) -> bool:
        """True if cross-venue funding spread is narrow (< 1bps)."""
        if not self._funding_history:
            return False
        try:
            if hasattr(self._funding_history, "get_cross_venue_spread"):
                for sym in ("BTC-USD", "ETH-USD"):
                    spread = self._funding_history.get_cross_venue_spread(sym)
                    if spread is not None and abs(spread) < 0.0001:
                        return True
        except Exception:
            pass
        return False
