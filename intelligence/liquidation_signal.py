"""
LiquidationSignalEngine — ARIA v1.6 (Tier 6 On-Chain Intelligence)

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

  Final tier6_score = size_factor × time_decay. Capped at 1.5 in coherence engine.

Conflict handling:
  If Tier 6 direction conflicts with interpreter's _direction_lock,
  a 70% penalty is applied by the interpreter (not here).
  The signal is never suppressed entirely — liq events have intrinsic value.

State is NOT persisted across sessions — liquidation events are ephemeral.
"""

import time
import structlog
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = structlog.get_logger(__name__)

_CASCADE_EXPIRY_S    = 90.0   # Type A (cascade_entry) signals expire after 90s
_RECOVERY_EXPIRY_S   = 300.0  # Type B (recovery_entry) signals expire after 300s (5 min)
_SIGNAL_EXPIRY_S     = _CASCADE_EXPIRY_S  # Alias — used in time_decay/expiry checks for Type A
_RECOVERY_SILENCE_S  = 120.0  # 2 min silence → Type B eligible
_EVENT_PRUNE_WINDOW  = 300.0  # Keep raw events for 5 min (2× cascade + buffer)


@dataclass
class ActiveLiqSignal:
    """
    An active (not yet expired) Tier 6 signal, used for scoring and display.

    v2: Added phase-aware fields for the FSM integration:
      zscore     — normalized liquidation intensity (0 = noise, 5+ = exhaustion)
      phase      — "none" | "trigger" | "expansion" | "exhaustion"
      confidence — min(zscore/5, 1.0) — probability weight for signal ranker
    """
    symbol: str           # "" = market-wide; affects all symbols
    signal_type: str      # "cascade_entry" (A) or "recovery_entry" (B)
    direction: str        # "long" or "short" (the TRADE direction)
    size_factor: float    # Raw size factor before time decay
    generated_at: float   # Unix timestamp
    expires_at: float     # generated_at + 90s
    # v2 phase fields (default safe values for backward compatibility)
    zscore: float = 0.0
    phase: str = "none"   # "none" | "trigger" | "expansion" | "exhaustion"
    confidence: float = 0.5  # min(zscore/5, 1.0)

    def time_decay(self) -> float:
        """Stepwise decay: 1.0 → 0.7 → 0.4 → 0.0 across 90s."""
        age = time.time() - self.generated_at
        if age < 30.0:  return 1.0
        if age < 60.0:  return 0.7
        if age < 90.0:  return 0.4
        return 0.0

    def current_score(self) -> float:
        """
        Phase-aware score: size_factor × time_decay × phase_multiplier.
        EXHAUSTION phase: 0.7× (trend may reverse; don't amplify late entry)
        EXPANSION phase: 1.0×
        TRIGGER phase:   0.8× (direction not yet confirmed dominant)
        """
        phase_mult = {"exhaustion": 0.7, "expansion": 1.0, "trigger": 0.8}.get(self.phase, 0.9)
        return self.size_factor * self.time_decay() * phase_mult

    def is_expired(self) -> bool:
        return time.time() >= self.expires_at

    def seconds_remaining(self) -> float:
        return max(0.0, self.expires_at - time.time())


class LiquidationSignalEngine:
    """
    Converts raw LiquidationSignal events into Tier 6 coherence scores.

    Integration:
      1. Call process_liquidation(sig) inside on_liquidation_signal().
      2. Call check_recovery_signals() every 30s from recovery_signal_loop().
      3. Call get_tier6_score(symbol) in interpreter._build_and_publish() before
         generate_market_state() — inject result into processed["tier6_liq_score"].
      4. Call get_all_active_signals() for terminal display panel.
    """

    def __init__(self):
        # Raw liq history (for 2-min silence detection)
        self._raw_events: List[Dict] = []  # {timestamp, direction, symbol, notional_usd}
        # Active Tier 6 signals (Type A + Type B, pruned on expiry)
        self._active_signals: List[ActiveLiqSignal] = []
        # Track last cascade direction per symbol (for recovery direction inference)
        self._last_cascade_dir: Dict[str, str] = {}  # symbol → "bearish" | "bullish"
        # Last liq timestamp per symbol (for silence detection)
        self._last_liq_ts: Dict[str, float] = {}

    # ── Public interface ───────────────────────────────────────────────────────

    async def process_liquidation(self, sig) -> None:
        """
        Process an incoming LiquidationSignal (from ValueChainMonitor).

        sig.direction: "bearish" (longs liquidated) or "bullish" (shorts liquidated)
        sig.cascade:   True if ≥3 liqs in 60s
        sig.notional_usd: float
        sig.symbol: str (may be "" for market-wide)

        Creates a Type A cascade_entry signal.
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
            e for e in self._raw_events
            if now - e["timestamp"] < _EVENT_PRUNE_WINDOW
        ]

        # Track last liq per symbol
        self._last_liq_ts[sym] = now

        # Track last cascade direction for recovery inference
        if cascade or notional >= 10_000:
            self._last_cascade_dir[sym] = direction

        # Size factor
        size_factor = self._size_factor(notional, cascade)

        # Trade direction = WITH the liquidation pressure:
        # "bearish" = longs being liquidated = downward pressure → SHORT
        # "bullish" = shorts being liquidated = upward pressure  → LONG
        if direction == "bearish":
            trade_dir = "short"
        elif direction == "bullish":
            trade_dir = "long"
        else:
            # Unknown direction — skip
            log.debug("tier6_unknown_direction", direction=direction, sym=sym)
            return

        signal = ActiveLiqSignal(
            symbol=sym,
            signal_type="cascade_entry",
            direction=trade_dir,
            size_factor=size_factor,
            generated_at=now,
            expires_at=now + _SIGNAL_EXPIRY_S,
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
            score=round(signal.current_score(), 3),
            expires_in_s=_SIGNAL_EXPIRY_S,
        )

    async def check_recovery_signals(self) -> None:
        """
        Called every 30s. Emits Type B (recovery_entry) signals when a 2-minute
        silence in liquidations is detected for any tracked symbol.

        Type B is higher value than Type A (confirmed exhaustion, not just pressure).
        Direction is AGAINST the last cascade direction.
        """
        now = time.time()
        self._prune_expired()

        for sym, last_ts in list(self._last_liq_ts.items()):
            silence_s = now - last_ts
            if silence_s < _RECOVERY_SILENCE_S:
                continue

            # Skip if we already have a live Type B for this symbol.
            # Include is_expired() check — _prune_expired may not have run yet.
            already_active = any(
                s.signal_type == "recovery_entry"
                and s.symbol == sym
                and not s.is_expired()
                for s in self._active_signals
            )
            if already_active:
                continue

            # Need a known cascade direction to invert
            last_dir = self._last_cascade_dir.get(sym)
            if not last_dir:
                continue

            # Recovery = AGAINST the liq pressure direction
            # "bearish" cascade (longs liq'd → market sold down) → recovery = "long" (buyers return)
            # "bullish" cascade (shorts liq'd → market pumped)   → recovery = "short" (sellers return)
            recovery_dir = "long" if last_dir == "bearish" else "short"

            signal = ActiveLiqSignal(
                symbol=sym,
                signal_type="recovery_entry",
                direction=recovery_dir,
                size_factor=1.5,  # Max: confirmed exhaustion > mere pressure
                generated_at=now,
                expires_at=now + _RECOVERY_EXPIRY_S,  # 300s — recovery stays valid longer
            )
            self._active_signals.append(signal)

            log.info(
                "tier6_signal_b_recovery",
                symbol=sym or "market_wide",
                direction=recovery_dir,
                silence_s=round(silence_s, 1),
                score=round(signal.current_score(), 3),
            )

    def get_tier6_score(self, symbol: str) -> float:
        """
        Returns best active Tier 6 score for this symbol.
        Considers both symbol-specific and market-wide signals (symbol="").
        Returns 0.0 if no active signals.

        Called by interpreter._build_and_publish() before generate_market_state().
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
        """
        Returns the highest-scoring active signal for this symbol.
        Used by interpreter to check direction conflict for 70% penalty.
        """
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

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _size_factor(notional_usd: float, cascade: bool) -> float:
        """
        Map notional USD to a base score weight.
        Cascade events always return 1.5 regardless of individual notional.
        """
        if cascade:
            return 1.5
        if notional_usd > 200_000:
            return 1.3
        if notional_usd > 50_000:
            return 1.0
        if notional_usd > 10_000:
            return 0.6
        if notional_usd > 1_000:
            return 0.3
        return 0.1  # Sub-$1k: minimal weight, still logged

    def _prune_expired(self) -> None:
        self._active_signals = [s for s in self._active_signals if not s.is_expired()]
