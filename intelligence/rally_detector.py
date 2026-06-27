"""
intelligence/rally_detector.py — Rally Detection Engine

Detects organic rallies (sustained directional moves) that are NOT driven by
liquidation cascades. Composites five signals into a rally score:

  1. VELOCITY    — price change z-score vs 20-period history
  2. VOLUME      — volume > 1.5× 20-period average
  3. FUNDING     — extreme funding rate (>95th percentile)
  4. L4 DEPTH    — bid/ask depth shift confirming direction
  5. HTF BIAS    — 4H trend aligned with move direction

Score 0–2: no rally (IDLE)
Score 3:   rally alert (ALERT)
Score 4–5: rally confirmed (CONFIRMED) → fast entry path

State machine: IDLE → ALERT → CONFIRMED → DECAY → IDLE

Philosophy:
  Kant:   each signal is a structural pillar — rally requires ≥3 pillars
  Nietzsche: conviction scales with score (3=CONVICTED, 4+=AGGRESSIVE)
  Chancellor: rally exposure capped at 40% of balance, max 2 pyramid layers

Backward compatible: rally detector is additive. Existing signal flow
(cascade → macro → interpreter → Kant → Nietzsche → execution) is
unchanged. Rally events are consumed optionally by listeners.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

import structlog

log = structlog.get_logger(__name__)

# ── Thresholds ───────────────────────────────────────────────────────────────
_VELOCITY_Z_THRESHOLD = 2.0      # z-score > 2.0 = significant velocity
_VELOCITY_DECAY_THRESHOLD = 1.5  # z-score < 1.5 for 2 ticks = decay
_VOLUME_MULT_THRESHOLD = 1.5     # volume > 1.5× avg
_FUNDING_PCTILE_THRESHOLD = 95   # funding > 95th percentile = extreme
_L4_DEPTH_MULT_THRESHOLD = 1.5   # depth > 1.5× baseline = building
_L4_DEPTH_DECAY_THRESHOLD = 0.8  # depth < 0.8× baseline = reversing

# ── State machine ────────────────────────────────────────────────────────────
class RallyPhase(Enum):
    IDLE = "idle"
    ALERT = "alert"           # 2 signals firing
    CONFIRMED = "confirmed"   # 3+ signals firing
    DECAY = "decay"           # velocity fading, position management phase


@dataclass
class RallySignal:
    velocity: bool = False
    volume: bool = False
    funding: bool = False
    l4_depth: bool = False
    htf_aligned: bool = False

    @property
    def score(self) -> int:
        return sum([self.velocity, self.volume, self.funding,
                    self.l4_depth, self.htf_aligned])


@dataclass
class RallyState:
    phase: RallyPhase = RallyPhase.IDLE
    direction: str = ""          # "long" | "short" | ""
    score: int = 0
    signals: RallySignal = field(default_factory=RallySignal)
    confirmed_at: float = 0.0
    decay_warned: bool = False
    # Price history for velocity computation: [(timestamp_ms, price), ...]
    _price_history: deque = field(default_factory=lambda: deque(maxlen=30))
    # Volume history: [(timestamp_ms, volume), ...]
    _volume_history: deque = field(default_factory=lambda: deque(maxlen=30))


class RallyDetector:
    """
    Per-symbol rally detector. Instantiate one detector per major symbol,
    or one detector that tracks multiple symbols.

    Thread-safe: no mutable shared state across symbols.
    """

    def __init__(self, symbols: List[str], config=None):
        self.symbols = symbols
        self.config = config
        self._states: Dict[str, RallyState] = {
            sym: RallyState() for sym in symbols
        }
        self._cooldown_until: Dict[str, float] = {}
        self._last_alert_logged: Dict[str, RallyPhase] = {}
        # Funding history: symbol → list of (timestamp_ms, rate)
        self._funding_history: Dict[str, deque] = {
            sym: deque(maxlen=100) for sym in symbols
        }

    # ── Public API ────────────────────────────────────────────────────────────

    def ingest_price(self, symbol: str, price: float, volume: float = 0.0):
        """Call on every tick or candle close."""
        if symbol not in self._states:
            return
        ts = int(time.time() * 1000)
        self._states[symbol]._price_history.append((ts, price))
        if volume > 0:
            self._states[symbol]._volume_history.append((ts, volume))

    def ingest_funding(self, symbol: str, funding_rate: float):
        """Call when funding rate updates."""
        if symbol not in self._funding_history:
            self._funding_history[symbol] = deque(maxlen=100)
        self._funding_history[symbol].append((int(time.time() * 1000), funding_rate))

    def update(
        self,
        symbol: str,
        direction: str = "",
        l4_depth_ratio: float = 0.0,
        htf_bias: str = "neutral",
    ) -> Tuple[RallyPhase, int, RallySignal]:
        """
        Main update cycle. Call every 10–30s per symbol.

        Returns: (phase, score, signals)
        """
        if symbol not in self._states:
            return RallyPhase.IDLE, 0, RallySignal()

        st = self._states[symbol]
        sig = RallySignal()

        # 1. Velocity z-score
        sig.velocity = self._check_velocity(symbol)

        # 2. Volume surge
        sig.volume = self._check_volume(symbol)

        # 3. Funding extreme
        sig.funding = self._check_funding(symbol, direction)

        # 4. L4 depth confirming direction
        sig.l4_depth = self._check_l4_depth(l4_depth_ratio, direction)

        # 5. HTF alignment
        sig.htf_aligned = self._check_htf(htf_bias, direction)

        score = sig.score
        old_phase = st.phase

        # State machine transitions
        if score >= 3:
            if st.phase in (RallyPhase.IDLE, RallyPhase.ALERT, RallyPhase.DECAY):
                st.phase = RallyPhase.CONFIRMED
                st.confirmed_at = time.time()
                st.decay_warned = False
        elif score == 2:
            if st.phase == RallyPhase.IDLE:
                st.phase = RallyPhase.ALERT
        elif score <= 1:
            if st.phase == RallyPhase.CONFIRMED:
                # Require 2 consecutive low-score ticks before decay
                # (prevents flicker on brief pullback)
                if not st.decay_warned:
                    st.decay_warned = True
                else:
                    st.phase = RallyPhase.DECAY
            elif st.phase == RallyPhase.ALERT:
                st.phase = RallyPhase.IDLE
            elif st.phase == RallyPhase.DECAY:
                # Decay → IDLE after 60s cooldown
                if time.time() - st.confirmed_at > 60.0:
                    st.phase = RallyPhase.IDLE

        st.score = score
        st.signals = sig
        if direction in ("long", "short"):
            st.direction = direction

        # Log on phase transitions only (dedup)
        if st.phase != old_phase:
            _prev = self._last_alert_logged.get(symbol)
            if _prev != st.phase:
                self._last_alert_logged[symbol] = st.phase
                if st.phase == RallyPhase.CONFIRMED:
                    log.info("rally_confirmed",
                             symbol=symbol,
                             direction=st.direction,
                             score=score,
                             signals={k: v for k, v in sig.__dict__.items()},
                             note="rally entry path open — fast execution")
                elif st.phase == RallyPhase.DECAY:
                    log.info("rally_decay",
                             symbol=symbol,
                             direction=st.direction,
                             score=score,
                             duration_s=round(time.time() - st.confirmed_at, 1),
                             note="rally velocity fading — no new adds")
                elif st.phase == RallyPhase.ALERT:
                    log.info("rally_alert",
                             symbol=symbol,
                             direction=st.direction,
                             score=score,
                             note="rally forming — await confirmation")

        return st.phase, score, sig

    def is_confirmed(self, symbol: str) -> bool:
        return self._states.get(symbol, RallyState()).phase == RallyPhase.CONFIRMED

    def get_state(self, symbol: str) -> RallyState:
        return self._states.get(symbol, RallyState())

    def summary(self) -> Dict[str, dict]:
        """Return summary for all tracked symbols."""
        return {
            sym: {
                "phase": st.phase.value,
                "direction": st.direction,
                "score": st.score,
                "signals": {k: v for k, v in st.signals.__dict__.items()},
                "confirmed_ago_s": round(time.time() - st.confirmed_at, 1)
                if st.confirmed_at > 0 else None,
            }
            for sym, st in self._states.items()
            if st.phase != RallyPhase.IDLE
        }

    # ── Internal signal checks ────────────────────────────────────────────────

    def _check_velocity(self, symbol: str) -> bool:
        """True if price velocity z-score > threshold."""
        hist = self._states[symbol]._price_history
        if len(hist) < 10:
            return False

        prices = [p for _, p in hist]
        # Compute returns
        returns = []
        for i in range(1, len(prices)):
            if prices[i - 1] > 0:
                returns.append((prices[i] - prices[i - 1]) / prices[i - 1])

        if len(returns) < 5:
            return False

        mean_ret = sum(returns) / len(returns)
        var_ret = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
        std_ret = var_ret ** 0.5

        if std_ret == 0:
            return False

        latest_ret = returns[-1]
        z_score = (latest_ret - mean_ret) / std_ret
        return abs(z_score) > _VELOCITY_Z_THRESHOLD

    def _check_volume(self, symbol: str) -> bool:
        """True if volume > 1.5× 20-period average."""
        hist = self._states[symbol]._volume_history
        if len(hist) < 10:
            return False
        volumes = [v for _, v in hist]
        avg_vol = sum(volumes[:-1]) / max(1, len(volumes) - 1)
        if avg_vol == 0:
            return False
        return volumes[-1] / avg_vol > _VOLUME_MULT_THRESHOLD

    def _check_funding(self, symbol: str, direction: str) -> bool:
        """True if funding is extreme AND aligned with move direction."""
        hist = self._funding_history.get(symbol)
        if not hist or len(hist) < 20:
            return False

        rates = [r for _, r in hist]
        current = rates[-1]

        # Percentile check
        sorted_rates = sorted(rates)
        idx = sum(1 for r in sorted_rates if r < current)
        percentile = (idx / len(sorted_rates)) * 100.0

        is_extreme = percentile > _FUNDING_PCTILE_THRESHOLD
        if not is_extreme:
            return False

        # Alignment: extreme positive funding = shorts paying = bullish rally fuel
        # extreme negative funding = longs paying = bearish rally fuel
        if direction == "long" and current > 0:
            return True
        if direction == "short" and current < 0:
            return True
        return False

    def _check_l4_depth(self, depth_ratio: float, direction: str) -> bool:
        """True if L4 depth confirms direction (bids rebuilding for long, etc)."""
        if direction == "long":
            return depth_ratio > _L4_DEPTH_MULT_THRESHOLD
        elif direction == "short":
            return depth_ratio < (1.0 / _L4_DEPTH_MULT_THRESHOLD)
        return False

    def _check_htf(self, htf_bias: str, direction: str) -> bool:
        """True if HTF bias aligns with trade direction."""
        if direction == "long" and htf_bias == "bullish":
            return True
        if direction == "short" and htf_bias == "bearish":
            return True
        return False
