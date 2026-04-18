"""
LiquidationSignalEngine — ARIA v2.1 (Tier 6 On-Chain Intelligence)

Converts SoDEX on-chain liquidation events into coherence scores with two
distinct signal types:

  TYPE A — CASCADE ENTRY (trade WITH the liquidation pressure):
    Short when longs are being liquidated (bearish direction from VC).
    Long when shorts are being liquidated (bullish direction from VC).
    Fires immediately on liq detection. Size factor based on notional USD.

  TYPE B — RECOVERY ENTRY (trade AGAINST the cascade direction, higher value):
    Fires when cascade stops — 2min silence after last liquidation.
    Direction AGAINST the prior cascade direction.
    Confirmed exhaustion > simple pressure.

v2.1 changes vs v1.6:
  - LiqPhaseEngine integration: every event is fed to liq_phase_engine,
    which provides Z-score, phase classification, and cross-venue lag status.
  - Phase-aware score multipliers: EXHAUSTION 0.7×, EXPANSION 1.0×, TRIGGER 0.8×.
  - Execution amplification guard: size amplification is only granted when
    (1) direction confirmed by price response, (2) funding aligned, (3) coherence >= 5.0.
    This is exposed via should_amplify() for the risk engine to check.
  - funding + liquidation interaction surface: alignment → continuation bias,
    divergence → reversal bias, both surfaced via get_funding_liq_bias().
  - Funding alignment tracked per-signal at emission time — no re-checks needed.

Signal scoring:
  SIZE FACTOR by notional_usd:
    > $200,000: 1.3
    > $50,000:  1.0
    > $10,000:  0.6
    > $1,000:   0.3
    cascade:    1.5 (automatic when sig.cascade=True)

  TIME DECAY (seconds since signal generated):
    < 30s:  1.0   (fresh)
    30-60s: 0.7
    60-90s: 0.4
    > 90s:  0.0   (expired)

  PHASE MULTIPLIER (from LiqPhaseEngine):
    exhaustion: 0.7× (trend reversing — don't amplify late momentum entry)
    expansion:  1.0×
    trigger:    0.8× (direction not yet confirmed)
    aftermath:  1.2× (confirmed exhaustion — recovery premium)

  Final tier6_score = size_factor × time_decay × phase_mult. Capped at 1.5 in coherence.

Funding / liq interaction:
  Aligned   (liq direction matches funding bias) → continuation signal, +10% size
  Divergent (liq direction opposes funding bias)  → reversal signal, +15% size (fade)

Conflict handling:
  If Tier 6 direction conflicts with interpreter's _direction_lock,
  a 70% penalty is applied by the interpreter (not here).
  The signal is never suppressed entirely.

State is NOT persisted across sessions — liquidation events are ephemeral.
"""

import time
import structlog
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from intelligence.liq_phase_engine import liq_phase_engine, LiqPhase

log = structlog.get_logger(__name__)

_CASCADE_EXPIRY_S    = 90.0    # Type A expire after 90s
_RECOVERY_EXPIRY_S   = 300.0   # Type B expire after 300s (5 min)
_SIGNAL_EXPIRY_S     = _CASCADE_EXPIRY_S
_RECOVERY_SILENCE_S  = 120.0   # 2 min silence → Type B eligible
_EVENT_PRUNE_WINDOW  = 300.0   # Keep raw events 5 min


@dataclass
class ActiveLiqSignal:
    """
    An active (not yet expired) Tier 6 signal.

    v2.1 additions:
      zscore           — normalised liq intensity at emission
      phase            — LiqPhase at emission time
      confidence       — min(zscore/5, 1.0)
      funding_aligned  — True when SFS agrees with trade direction at emission

    v2.2 additions:
      event_count_60s  — number of liquidations in the 60s window at emission;
                         used in current_score() so 2 liqs ≠ 10 liqs score-wise
    """
    symbol: str
    signal_type: str      # "cascade_entry" | "recovery_entry"
    direction: str        # trade direction: "long" | "short"
    size_factor: float
    generated_at: float
    expires_at: float
    zscore: float = 0.0
    phase: str = "none"
    confidence: float = 0.5
    funding_aligned: bool = False
    event_count_60s: int = 1

    def time_decay(self) -> float:
        age = time.time() - self.generated_at
        if age < 30.0:  return 1.0
        if age < 60.0:  return 0.7
        if age < 90.0:  return 0.4
        return 0.0

    def current_score(self) -> float:
        """
        Phase-aware score: size_factor × time_decay × phase_mult × zscore_mult.

        zscore_mult scales with event intensity so that 10 liquidations score
        meaningfully higher than 2. Formula: 1.0 + clamp(zscore, 0, 5) × 0.1
          zscore=0   → 1.0× (no boost — baseline)
          zscore=1.5 → 1.15× (trigger phase)
          zscore=3.0 → 1.30× (expansion)
          zscore=5.0 → 1.50× (cap — exhaustion)
        """
        phase_mult = {
            "exhaustion": 0.7,
            "expansion":  1.0,
            "trigger":    0.8,
            "aftermath":  1.2,
            "quiet":      0.9,
        }.get(self.phase, 0.9)
        zscore_mult = 1.0 + min(max(self.zscore, 0.0), 5.0) * 0.10
        return self.size_factor * self.time_decay() * phase_mult * zscore_mult

    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    def seconds_remaining(self) -> float:
        return max(0.0, self.expires_at - time.time())


class LiquidationSignalEngine:
    """
    Converts raw LiquidationSignal events into Tier 6 coherence scores.

    Integration:
      1. Call process_liquidation(sig) from on_liquidation_signal().
      2. Call check_recovery_signals() every 30s from recovery_signal_loop().
      3. Call get_tier6_score(symbol) in interpreter._build_and_publish() — inject
         result into processed["tier6_liq_score"].
      4. Call should_amplify(symbol, direction, coherence, funding_score) in risk
         engine before applying liq_phase_size_mult.
      5. Call get_funding_liq_bias(symbol) to get continuation/reversal bias for
         injection into processed["funding_liq_bias"].
    """

    def __init__(self):
        self._raw_events: List[Dict] = []
        self._active_signals: List[ActiveLiqSignal] = []
        self._last_cascade_dir: Dict[str, str] = {}
        self._last_liq_ts: Dict[str, float] = {}
        # Dedup cache: key=(direction, round(notional,-3), phase) → last emission ts
        self._last_emission: Dict[tuple, float] = {}

    # ── Public interface ───────────────────────────────────────────────────────

    async def process_liquidation(
        self,
        sig,
        bybit_price: float = 0.0,
        sodex_price: float = 0.0,
        funding_score: float = 0.0,
    ) -> None:
        """
        Process an incoming LiquidationSignal.

        sig.direction: "bearish" (longs liquidated) or "bullish" (shorts liquidated)
        sig.cascade:   True if >= 3 liqs in 60s
        sig.notional_usd: float
        sig.symbol: str (may be "" for market-wide)

        bybit_price, sodex_price: current prices for cross-venue lag detection.
        funding_score: SFS score for funding alignment check (positive = longs paying).
        """
        now = time.time()
        sym = getattr(sig, "symbol", "") or ""
        direction = getattr(sig, "direction", "")
        notional = float(getattr(sig, "notional_usd", 0.0))
        cascade = bool(getattr(sig, "cascade", False))

        # Record raw event
        self._raw_events.append({
            "timestamp": now,
            "direction": direction,
            "symbol": sym,
            "notional_usd": notional,
        })
        self._raw_events = [
            e for e in self._raw_events if now - e["timestamp"] < _EVENT_PRUNE_WINDOW
        ]
        self._last_liq_ts[sym] = now

        if cascade or notional >= 10_000:
            self._last_cascade_dir[sym] = direction

        # Feed into phase engine for Z-score + phase classification
        liq_phase_engine.on_event(sym, notional, direction, bybit_price, sodex_price)
        liq_phase_engine.update_funding_score(sym, funding_score)

        # Minimum notional filter — ignore noise below $1,000
        if notional < 1_000:
            log.debug("tier6_notional_below_minimum", notional=round(notional, 0), minimum=1_000)
            return

        # Determine trade direction
        if direction == "bearish":
            trade_dir = "short"
        elif direction == "bullish":
            trade_dir = "long"
        else:
            log.debug("tier6_unknown_direction", direction=direction, sym=sym)
            return

        snap = liq_phase_engine.get_snapshot(sym)
        size_factor = self._size_factor(notional, cascade)
        confidence = min(snap.zscore / 5.0, 1.0) if snap.zscore > 0 else 0.5

        # Funding alignment at emission time
        funding_aligned = snap.funding_aligned

        # 500ms dedup — same direction + notional bucket + phase within window is one event
        _dedup_key = (direction, round(notional, -3), snap.phase.value)
        _last_ts = self._last_emission.get(_dedup_key, 0.0)
        if now - _last_ts < 0.5:
            log.debug("tier6_dedup_suppressed",
                      direction=direction, notional_bucket=round(notional, -3),
                      phase=snap.phase.value, age_ms=round((now - _last_ts) * 1000))
            return
        self._last_emission[_dedup_key] = now

        event_count = int(getattr(sig, "event_count_60s", 1))
        signal = ActiveLiqSignal(
            symbol=sym,
            signal_type="cascade_entry",
            direction=trade_dir,
            size_factor=size_factor,
            generated_at=now,
            expires_at=now + _SIGNAL_EXPIRY_S,
            zscore=snap.zscore,
            phase=snap.phase.value,
            confidence=confidence,
            funding_aligned=funding_aligned,
            event_count_60s=event_count,
        )
        self._active_signals.append(signal)
        self._prune_expired()

        log.info(
            "tier6_signal_a",
            symbol=sym or "market_wide",
            direction=trade_dir,
            size_factor=round(size_factor, 2),
            notional_usd=round(notional, 0),
            cascade=cascade,
            zscore=round(snap.zscore, 2),
            phase=snap.phase.value,
            funding_aligned=funding_aligned,
            score=round(signal.current_score(), 3),
            cross_venue_lag=snap.cross_venue_lag,
            cross_venue_dir=snap.cross_venue_dir,
        )

    async def check_recovery_signals(self) -> None:
        """
        Called every 30s. Emits Type B (recovery_entry) signals when a 2-minute
        silence is detected for any tracked symbol.
        """
        now = time.time()
        self._prune_expired()

        for sym, last_ts in list(self._last_liq_ts.items()):
            silence_s = now - last_ts
            if silence_s < _RECOVERY_SILENCE_S:
                continue

            already_active = any(
                s.signal_type == "recovery_entry"
                and s.symbol == sym
                and not s.is_expired()
                for s in self._active_signals
            )
            if already_active:
                continue

            last_dir = self._last_cascade_dir.get(sym)
            if not last_dir:
                continue

            recovery_dir = "long" if last_dir == "bearish" else "short"

            snap = liq_phase_engine.get_snapshot(sym)

            signal = ActiveLiqSignal(
                symbol=sym,
                signal_type="recovery_entry",
                direction=recovery_dir,
                size_factor=1.5,
                generated_at=now,
                expires_at=now + _RECOVERY_EXPIRY_S,
                zscore=snap.zscore,
                phase=snap.phase.value,
                confidence=1.0,  # confirmed exhaustion = max confidence
                funding_aligned=snap.funding_aligned,
            )
            self._active_signals.append(signal)

            log.info(
                "tier6_signal_b_recovery",
                symbol=sym or "market_wide",
                direction=recovery_dir,
                silence_s=round(silence_s, 1),
                phase=snap.phase.value,
                funding_aligned=snap.funding_aligned,
                score=round(signal.current_score(), 3),
            )

    def get_tier6_score(self, symbol: str) -> float:
        """
        Returns best active Tier 6 score for this symbol (symbol-specific + market-wide).
        Conflict penalty (70%) is applied by the interpreter, not here.
        """
        self._prune_expired()
        best = 0.0
        for sig in self._active_signals:
            if sig.symbol != symbol and sig.symbol != "":
                continue
            score = sig.current_score()
            if score > best:
                best = score
        return best

    def get_best_signal(self, symbol: str) -> Optional[ActiveLiqSignal]:
        """Returns the highest-scoring active signal. Used by interpreter for conflict check."""
        self._prune_expired()
        best: Optional[ActiveLiqSignal] = None
        best_score = 0.0
        for sig in self._active_signals:
            if sig.symbol != symbol and sig.symbol != "":
                continue
            score = sig.current_score()
            if score > best_score:
                best_score = score
                best = sig
        return best

    def get_all_active_signals(self) -> List[ActiveLiqSignal]:
        """Returns all non-expired signals. Used by terminal display."""
        self._prune_expired()
        return list(self._active_signals)

    def should_amplify(
        self,
        symbol: str,
        direction: str,
        coherence: float,
        funding_score: float,
    ) -> bool:
        """
        Delegate to LiqPhaseEngine.should_amplify().
        Call from the risk engine before applying phase size_mult.
        """
        return liq_phase_engine.should_amplify(symbol, direction, coherence, funding_score)

    def get_funding_liq_bias(self, symbol: str) -> str:
        """
        Funding + liquidation interaction bias for the current symbol.

        Returns:
          "continuation" — liq direction and funding align → momentum bias
          "reversal"     — liq direction and funding diverge → fade/reversal bias
          "neutral"      — insufficient data or no active liq signals
        """
        self._prune_expired()
        best = self.get_best_signal(symbol)
        if best is None:
            return "neutral"
        return "continuation" if best.funding_aligned else "reversal"

    def is_momentum_blocked(self, symbol: str) -> bool:
        """
        Delegate to LiqPhaseEngine.is_momentum_blocked().
        Use in interpreter/risk engine to block momentum strategies during EXHAUSTION.
        Does NOT block reversal strategies.
        """
        return liq_phase_engine.is_momentum_blocked(symbol)

    def get_phase_snapshot(self, symbol: str):
        """Expose LiqPhaseSnapshot for interpreter injection."""
        return liq_phase_engine.get_snapshot(symbol)

    def on_silence_tick(self, symbol: str) -> None:
        """
        Drive EXHAUSTION→AFTERMATH phase transition during silence.
        Call every 15s from cascade_aftermath_loop for each tracked symbol.

        Without these ticks, _advance_phase() only runs on event arrival.
        AFTERMATH requires silence — so without ticks, EXHAUSTION never advances.
        """
        liq_phase_engine.on_silence_tick(symbol)

    def update_bybit_price(self, symbol: str, price: float) -> None:
        liq_phase_engine.update_bybit_price(symbol, price)

    def update_sodex_price(self, symbol: str, price: float) -> None:
        liq_phase_engine.update_sodex_price(symbol, price)

    def update_funding_score(self, symbol: str, sfs_score: float) -> None:
        liq_phase_engine.update_funding_score(symbol, sfs_score)

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _size_factor(notional_usd: float, cascade: bool) -> float:
        if cascade:        return 1.5
        if notional_usd > 200_000: return 1.3
        if notional_usd > 50_000:  return 1.0
        if notional_usd > 10_000:  return 0.6
        if notional_usd > 1_000:   return 0.3
        return 0.1

    def _prune_expired(self) -> None:
        self._active_signals = [s for s in self._active_signals if not s.is_expired()]
