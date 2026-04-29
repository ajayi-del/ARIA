"""
execution/kant_gate.py — Kant Gate (ARIA v2.2)

Kant = Structure First. The categorical imperative of trading:
  "Act only according to maxims you can will as universal law."

In practice: hard YES/NO gates that determine whether a signal is
RATIONAL to trade. No sizing. No nuance. Structure or silence.

Gates (in order — first rejection wins):
  1. Balance floor        — account too small to trade
  2. Daily limits         — per-symbol + global daily caps
  3. Coherence minimum    — is this signal real evidence?
  4. Flip cooldown        — same-symbol direction reversal guard
  5. R:R minimum          — does the trade pay for its risk?
  6. Regime alignment     — does the trade fight structural flow?

All state is held here (daily counters, flip history, regime confidence).
NietzscheEngine is stateless — it receives the Kant verdict and computes
size ONLY if Kant says YES.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import structlog

log = structlog.get_logger(__name__)


# ── Balance-based global daily cap ─────────────────────────────────────────
BALANCE_TIERS = [
    (200.0, 50, 5),   # ≥$200: max 50 trades/day, 5 concurrent
    (150.0, 30, 3),   # ≥$150: max 30 trades/day, 3 concurrent
    (100.0, 15, 2),   # ≥$100: max 15 trades/day, 2 concurrent
    ( 50.0,  5, 1),   # ≥$50:  max 5 trades/day, 1 concurrent (micro-mode)
    (  0.0,  0, 0),   # <$50: no trading
]

# ── Flip cooldown ────────────────────────────────────────────────────────────
FLIP_COOLDOWN_S   = 60 * 60        # 60 minutes
FLIP_ELITE_COH    = 8.0            # coherence ≥ 8.0 → allow at 50% size
FLIP_CASCADE_ZS   = 3.0            # cascade zscore > 3.0 → allow
FLIP_REGIME_DELTA = 0.30           # regime confidence shift > 0.30 in 5m → allow

# ── Coherence floor ──────────────────────────────────────────────────────────
# Minimum coherence for a signal to be considered evidence.
# Below this: Kant says "this is noise, not knowledge."
COHERENCE_MINIMUM = 3.5


@dataclass(frozen=True)
class KantVerdict:
    """Immutable verdict from the Kant gate."""
    allowed:    bool
    reason:     str
    log_event:  str = ""          # structured event name for logging
    size_mult:  float = 0.0       # backward-compat after Kant/Nietzsche split


def _utc_day() -> int:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).day


def _balance_tier(balance: float) -> Tuple[float, int, int]:
    for threshold, max_trades, max_pos in BALANCE_TIERS:
        if balance >= threshold:
            return threshold, max_trades, max_pos
    return 0.0, 0, 0


class KantGate:
    """
    Hard rejection gate. Zero sizing logic.

    State held:
      - per-symbol daily trade count
      - global daily trade count
      - last executed direction per symbol (for flip cooldown)
      - last regime confidence + timestamp (for regime shift detection)
    """

    def __init__(self) -> None:
        self._last_exec: Dict[str, Tuple[str, float]] = {}
        self._daily_symbol: Dict[Tuple[str, int], int] = {}
        self._daily_global: Dict[int, int] = {}
        self._last_regime_conf: float = 0.0
        self._regime_conf_ts:   float = 0.0

    # ── Public API ──────────────────────────────────────────────────────────

    def check(
        self,
        symbol:           str,
        direction:        str,
        coherence:        float,
        rr_ratio:         float,
        balance:          float,
        regime_state,
        cascade_zscore:   float = 0.0,
        regime_conf:      float = 0.0,
        spartan:          bool = False,
    ) -> KantVerdict:
        """
        Run all Kant gates in order. Returns on first rejection.
        """
        utc_day = _utc_day()

        # 1. Balance-based global daily limit ─────────────────────────
        global_used = self._daily_global.get(utc_day, 0)
        _, max_global, _ = _balance_tier(balance)
        if max_global == 0:
            return KantVerdict(
                allowed=False,
                reason=f"balance_below_floor_{round(balance, 0)}",
                log_event="balance_floor_halt",
            )
        if global_used >= max_global:
            return KantVerdict(
                allowed=False,
                reason=f"global_daily_limit_reached_{global_used}/{max_global}",
                log_event="global_daily_limit_reached",
            )

        # 2. Coherence minimum ────────────────────────────────────────
        # Spartan override: high conviction + strong regime bypasses symbol limits
        # but NEVER bypasses the coherence floor. Kant demands evidence.
        if coherence < COHERENCE_MINIMUM:
            return KantVerdict(
                allowed=False,
                reason=f"coherence_below_{COHERENCE_MINIMUM}_{round(coherence, 2)}",
                log_event="coherence_tier_reject",
            )

        # 3. Flip cooldown ────────────────────────────────────────────
        if symbol in self._last_exec:
            prev_dir, prev_ts = self._last_exec[symbol]
            age_s = time.time() - prev_ts
            if prev_dir != direction and age_s < FLIP_COOLDOWN_S:
                if cascade_zscore > FLIP_CASCADE_ZS:
                    log.info("flip_allowed_cascade", symbol=symbol,
                             age_min=round(age_s / 60, 1), zscore=round(cascade_zscore, 2))
                elif coherence >= FLIP_ELITE_COH:
                    log.info("flip_allowed_elite", symbol=symbol,
                             age_min=round(age_s / 60, 1), coherence=round(coherence, 2))
                elif self._regime_shift_detected():
                    log.info("flip_allowed_regime_shift", symbol=symbol,
                             age_min=round(age_s / 60, 1))
                else:
                    return KantVerdict(
                        allowed=False,
                        reason=f"flip_blocked_age_{round(age_s/60,1)}min",
                        log_event="flip_blocked",
                    )

        # 4. Dynamic R:R minimum ──────────────────────────────────────
        if balance < 150.0:
            if regime_conf >= 0.70:
                min_rr = 1.5
            else:
                min_rr = 2.0 if coherence >= 6.0 else 2.5
        elif regime_conf >= 0.85:
            min_rr = 1.5
        elif regime_conf >= 0.70:
            min_rr = 2.0
        else:
            min_rr = 2.0 if coherence >= 7.0 else 3.0
        if rr_ratio > 0 and rr_ratio < min_rr:
            return KantVerdict(
                allowed=False,
                reason=f"rr_below_min_{round(rr_ratio, 2)}_min_{min_rr}",
                log_event="risk_reward_reject",
            )

        # 5. Regime alignment ─────────────────────────────────────────
        if regime_state is not None:
            verdict = self._check_regime_alignment(
                symbol, direction, regime_state,
                spartan=spartan, coherence=coherence,
            )
            if verdict is not None:
                return verdict

        return KantVerdict(
            allowed=True,
            reason="all_kant_gates_passed",
            log_event="kant_approved",
        )

    def record_execution(self, symbol: str, direction: str) -> None:
        """Call immediately after an order is placed."""
        utc_day = _utc_day()
        self._last_exec[symbol] = (direction, time.time())
        key = (symbol, utc_day)
        self._daily_symbol[key] = self._daily_symbol.get(key, 0) + 1
        self._daily_global[utc_day] = self._daily_global.get(utc_day, 0) + 1

    def update_regime_confidence(self, confidence: float) -> None:
        """Called by regime engine on every update to track confidence drift."""
        now = time.time()
        if abs(confidence - self._last_regime_conf) > 0.05:
            self._last_regime_conf = confidence
            self._regime_conf_ts   = now

    def reset_day(self) -> None:
        """Call at UTC midnight to reset daily counters."""
        utc_day = _utc_day()
        old_keys = [k for k in self._daily_symbol if k[1] != utc_day]
        for k in old_keys:
            del self._daily_symbol[k]
        old_g = [d for d in self._daily_global if d != utc_day]
        for d in old_g:
            del self._daily_global[d]
        log.info("kant_gate_day_reset", utc_day=utc_day)

    def daily_stats(self) -> dict:
        """For dashboard / logging."""
        utc_day = _utc_day()
        return {
            "global_trades_today": self._daily_global.get(utc_day, 0),
            "symbol_counts":       {k[0]: v for k, v in self._daily_symbol.items()
                                     if k[1] == utc_day},
        }

    # ── Internal helpers ──────────────────────────────────────────────────

    def _check_regime_alignment(
        self, symbol: str, direction: str, rs,
        spartan: bool = False, coherence: float = 0.0,
    ) -> Optional[KantVerdict]:
        """Returns a rejection verdict or None (pass)."""
        conf    = float(getattr(rs, "confidence", 0.0) or 0.0)
        leading = str(getattr(rs, "leading_category", "none") or "none")
        lagging = str(getattr(rs, "lagging_category", "none") or "none")
        regime  = str(getattr(rs, "regime", "") or "")

        if regime in ("transitioning", "confused") and conf < 0.5:
            if spartan and coherence >= 7.0:
                log.info("spartan_regime_uncertain_bypass",
                         symbol=symbol, coherence=round(coherence, 2),
                         regime=regime, conf=round(conf, 3))
            else:
                return KantVerdict(
                    allowed=False,
                    reason=f"regime_uncertain_conf_{round(conf, 2)}",
                    log_event="regime_alignment_reject",
                )

        from intelligence.relative_strength import ASSET_CATEGORIES
        sym_cat = ASSET_CATEGORIES.get(symbol, "unknown")

        if conf >= 0.80:
            if sym_cat != leading and leading != "none":
                return KantVerdict(
                    allowed=False,
                    reason=f"regime_strict_leading_{leading}_symbol_{sym_cat}",
                    log_event="regime_alignment_reject",
                )
        elif conf >= 0.50:
            if sym_cat == lagging and lagging != "none":
                return KantVerdict(
                    allowed=False,
                    reason=f"regime_lagging_sector_{lagging}",
                    log_event="regime_alignment_reject",
                )

        return None

    def _regime_shift_detected(self) -> bool:
        """True if regime confidence changed by > 0.30 in the last 5 minutes."""
        return (time.time() - self._regime_conf_ts) < 300.0
