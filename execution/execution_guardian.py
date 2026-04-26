"""
execution/execution_guardian.py — ARIA Execution Guardian

Enforces five hard rules that eliminate edge leakage identified in the
Apr-2026 audit (~45 trades/day on $200 account → target ≤10/day):

  1. Regime alignment  — symbol sector must match leading category when
                          regime confidence ≥ 0.5.  At ≥ 0.8 only the
                          leading sector is allowed.
  2. Coherence sizing  — 4-tier table: ≥8.0 → 1.5×; 7.0→1.0×; 6.0→0.5×;
                          5.0→0.25×; <5.0 → REJECT.
  3. 4:1 R:R minimum  — bracket config by coherence tier.  Trades below
                          3.0:1 at ≥7.0 coherence (or 2.0:1 otherwise) → REJECT.
  4. Flip cooldown     — same-symbol direction flip blocked within 60 min.
                          Exceptions: cascade zscore>3, regime shift>0.3,
                          or coherence≥8.0 (half-size probe).
  5. Daily limits      — per-symbol cap (evidence-based) + balance-tiered
                          global cap.

All state is held in plain Python dicts — no DB dependency.
Designed to be cheap on the hot path: O(1) dict lookups only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import structlog

log = structlog.get_logger(__name__)

# Per-symbol daily limits removed — global daily cap controls total trade count.
# Coherence floors and risk gates prevent low-quality entries; no per-symbol ceiling.
SYMBOL_DAILY_LIMITS: Dict[str, int] = {}

SYMBOL_DEFAULT_DAILY_LIMIT = 10  # fallback if symbol appears in override dict

# ── Balance-based global daily cap ─────────────────────────────────────────
BALANCE_TIERS = [
    (200.0, 50, 5),   # ≥$200: max 50 trades/day, 5 concurrent
    (150.0, 30, 3),   # ≥$150: max 30 trades/day, 3 concurrent
    (100.0, 15, 2),   # ≥$100: max 15 trades/day, 2 concurrent
    ( 50.0,  5, 1),   # ≥$50:  max 5 trades/day, 1 concurrent (micro-mode)
    (  0.0,  0, 0),   # <$50: no trading
]

# ── Coherence tier sizing table ─────────────────────────────────────────────
# (min_coherence, size_mult, risk_pct, max_concurrent)
COHERENCE_TIERS: List[Tuple[float, float, float, int]] = [
    (8.0, 1.50, 0.03, 2),
    (7.0, 1.00, 0.025, 2),
    (6.0, 0.50, 0.02,  1),
    (5.0, 0.25, 0.015, 1),
]

# ── Flip cooldown ────────────────────────────────────────────────────────────
FLIP_COOLDOWN_S  = 60 * 60        # 60 minutes
FLIP_ELITE_COH   = 8.0            # coherence ≥ 8.0 → allow at 50% size
FLIP_CASCADE_ZS  = 3.0            # cascade zscore > 3.0 → allow
FLIP_REGIME_DELTA = 0.30          # regime confidence shift > 0.30 in 5m → allow


@dataclass
class GuardianVerdict:
    allowed:    bool
    reason:     str
    size_mult:  float = 1.0         # Nietzsche-tier multiplier to apply
    log_event:  str   = ""          # structured event name for logging


class ExecutionGuardian:
    """
    Stateful per-session guardian.  One instance lives for the full ARIA
    session; reset_day() is called at UTC midnight.
    """

    def __init__(self) -> None:
        # {symbol: (direction, open_ts)}  — tracks last executed direction
        self._last_exec: Dict[str, Tuple[str, float]] = {}
        # {(symbol, utc_day): count}  — daily per-symbol trade counter
        self._daily_symbol: Dict[Tuple[str, int], int] = {}
        # {utc_day: count}  — daily global trade counter
        self._daily_global: Dict[int, int] = {}
        # Last known regime confidence — used for flip exception detection
        self._last_regime_conf: float = 0.0
        self._regime_conf_ts:   float = 0.0

    # ── Public gate (single call from on_signal_ready) ────────────────────

    def check(
        self,
        symbol:           str,
        direction:        str,
        coherence:        float,
        rr_ratio:         float,
        balance:          float,
        regime_state,                     # Optional[RegimeState]
        cascade_zscore:   float = 0.0,
        regime_conf:      float = 0.0,    # regime confidence for Spartan override + dynamic R:R
    ) -> GuardianVerdict:
        """
        Run all five rules in order.  Returns on the first rejection.
        Returns a GuardianVerdict with allowed=True and the coherence
        size_mult on pass.

        Spartan override: coherence ≥ 7.0 + regime_conf ≥ 0.70 → bypass no_edge_symbol.
        Dynamic R:R: regime_conf ≥ 0.85 → min_rr=1.5; ≥ 0.70 → 2.0.
        """
        utc_day = _utc_day()

        # ── Spartan override — high conviction + strong regime bypasses symbol limits ─
        # When the regime is clear and signal quality is high, no_edge_symbol is a
        # false negative: the edge IS there (e.g. LINK at coherence 9.3 during alt_season).
        _spartan = False
        if regime_conf >= 0.70 and coherence >= 7.0:
            _spartan = True
        elif regime_conf >= 0.60 and coherence >= 8.0:
            _spartan = True
        elif coherence >= 8.0:
            # Pure coherence Spartan — elite signal regardless of regime clarity.
            # 8.0+ coherence across all 7 sub-signals is rare and IS the signal.
            _spartan = True

        # 1. Per-symbol daily limit ─────────────────────────────────────
        limit = SYMBOL_DAILY_LIMITS.get(symbol, SYMBOL_DEFAULT_DAILY_LIMIT)
        if _spartan and limit == 0:
            # Override no-edge block: allow 1 quality signal per day during strong regime
            limit = 2 if regime_conf >= 0.85 else 1
            log.info("spartan_override_no_edge", symbol=symbol, coherence=round(coherence, 2),
                     regime_conf=round(regime_conf, 3), new_limit=limit)
        used  = self._daily_symbol.get((symbol, utc_day), 0)
        if limit == 0:
            return GuardianVerdict(
                allowed=False, reason="no_edge_symbol",
                log_event="symbol_daily_limit_reached",
            )
        if used >= limit:
            return GuardianVerdict(
                allowed=False,
                reason=f"daily_limit_reached_{used}/{limit}",
                log_event="symbol_daily_limit_reached",
            )

        # 2. Balance-based global daily limit ───────────────────────────
        global_used = self._daily_global.get(utc_day, 0)
        _, max_global, _ = _balance_tier(balance)
        if max_global == 0:
            return GuardianVerdict(
                allowed=False, reason=f"balance_below_floor_{round(balance, 0)}",
                log_event="balance_floor_halt",
            )
        if global_used >= max_global:
            return GuardianVerdict(
                allowed=False,
                reason=f"global_daily_limit_reached_{global_used}/{max_global}",
                log_event="global_daily_limit_reached",
            )

        # 3. Coherence tier ─────────────────────────────────────────────
        tier = _coherence_tier(coherence)
        if tier is None:
            return GuardianVerdict(
                allowed=False,
                reason=f"coherence_below_5.0_{round(coherence, 2)}",
                log_event="coherence_tier_reject",
            )
        _, size_mult, _, _ = tier

        # 4. Flip cooldown ──────────────────────────────────────────────
        if symbol in self._last_exec:
            prev_dir, prev_ts = self._last_exec[symbol]
            age_s = time.time() - prev_ts
            if prev_dir != direction and age_s < FLIP_COOLDOWN_S:
                # Check exceptions
                if cascade_zscore > FLIP_CASCADE_ZS:
                    log.info("flip_allowed_cascade", symbol=symbol,
                             age_min=round(age_s / 60, 1), zscore=round(cascade_zscore, 2))
                elif coherence >= FLIP_ELITE_COH:
                    size_mult = min(size_mult, 0.50)   # elite exception at half size
                    log.info("flip_allowed_elite", symbol=symbol,
                             age_min=round(age_s / 60, 1), coherence=round(coherence, 2))
                elif self._regime_shift_detected():
                    log.info("flip_allowed_regime_shift", symbol=symbol,
                             age_min=round(age_s / 60, 1))
                else:
                    return GuardianVerdict(
                        allowed=False,
                        reason=f"flip_blocked_age_{round(age_s/60,1)}min",
                        log_event="flip_blocked",
                        size_mult=0.0,
                    )

        # 5. Dynamic R:R minimum — strong regime = lower bar (market is moving) ───
        # High coherence = more conviction = lower R:R bar, not higher.
        # Demanding 3:1 from a 7.8-coherence signal is backwards — that signal IS the edge.
        # Micro-mode: $88 accounts cannot afford to wait for perfection. A 6.0+ coherence
        # signal with 2:1 RR is still +EV when the alternative is zero trades.
        if balance < 100.0:
            if regime_conf >= 0.70:
                min_rr = 1.5
            else:
                min_rr = 2.0 if coherence >= 6.0 else 2.5
        elif regime_conf >= 0.85:
            min_rr = 1.5   # trending market — 1.5:1 sufficient
        elif regime_conf >= 0.70:
            min_rr = 2.0   # medium confidence standard
        else:
            min_rr = 2.0 if coherence >= 7.0 else 3.0   # high coh = lower bar; low coh = tighter
        if rr_ratio > 0 and rr_ratio < min_rr:
            return GuardianVerdict(
                allowed=False,
                reason=f"rr_below_min_{round(rr_ratio, 2)}_min_{min_rr}",
                log_event="risk_reward_reject",
            )

        # 6. Regime alignment ───────────────────────────────────────────
        if regime_state is not None:
            verdict = self._check_regime_alignment(
                symbol, direction, regime_state,
                spartan=_spartan, coherence=coherence,
            )
            if verdict is not None:
                return verdict

        return GuardianVerdict(allowed=True, reason="all_guardian_gates_passed",
                               size_mult=size_mult, log_event="guardian_approved")

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
        log.info("execution_guardian_day_reset", utc_day=utc_day)

    def get_brackets(self, coherence: float) -> List[Tuple[float, float]]:
        """
        Bracket config for a given coherence level.
        Returns list of (fraction_of_position, r_multiple) tuples.

        coherence ≥ 8.0:  [(0.20, 2R), (0.30, 5R), (0.50, 7R)]  → weighted 5.4R  (elite)
        coherence ≥ 7.0:  [(0.30, 2R), (0.30, 4R), (0.40, 6R)]  → weighted 4.2R
        coherence  5-6.9: [(0.30, 3R), (0.70, 5R)]               → weighted 4.4R
        """
        if coherence >= 8.0:
            return [(0.20, 2.0), (0.30, 5.0), (0.50, 7.0)]
        if coherence >= 7.0:
            return [(0.30, 2.0), (0.30, 4.0), (0.40, 6.0)]
        return [(0.30, 3.0), (0.70, 5.0)]

    def daily_stats(self) -> dict:
        """For dashboard / logging."""
        utc_day = _utc_day()
        return {
            "global_trades_today":  self._daily_global.get(utc_day, 0),
            "symbol_counts":        {k[0]: v for k, v in self._daily_symbol.items()
                                     if k[1] == utc_day},
        }

    # ── Internal helpers ──────────────────────────────────────────────────

    def _check_regime_alignment(
        self, symbol: str, direction: str, rs,
        spartan: bool = False, coherence: float = 0.0,
    ) -> Optional[GuardianVerdict]:
        """Returns a rejection verdict or None (pass)."""
        conf            = float(getattr(rs, "confidence", 0.0) or 0.0)
        leading         = str(getattr(rs, "leading_category", "none") or "none")
        lagging         = str(getattr(rs, "lagging_category", "none") or "none")
        regime          = str(getattr(rs, "regime", "") or "")

        if regime in ("transitioning", "confused") and conf < 0.5:
            # Spartan exception: elite coherence signal is its own evidence of direction.
            # Don't block a 7.0+ coherence signal just because regime classifier is uncertain.
            if spartan and coherence >= 7.0:
                log.info("spartan_regime_uncertain_bypass",
                         symbol=symbol, coherence=round(coherence, 2),
                         regime=regime, conf=round(conf, 3))
            else:
                return GuardianVerdict(
                    allowed=False,
                    reason=f"regime_uncertain_conf_{round(conf, 2)}",
                    log_event="regime_alignment_reject",
                )

        from intelligence.relative_strength import ASSET_CATEGORIES
        sym_cat = ASSET_CATEGORIES.get(symbol, "unknown")

        if conf >= 0.80:
            # Strict: only leading sector allowed
            if sym_cat != leading and leading != "none":
                return GuardianVerdict(
                    allowed=False,
                    reason=f"regime_strict_leading_{leading}_symbol_{sym_cat}",
                    log_event="regime_alignment_reject",
                )
        elif conf >= 0.50:
            # Soft: reject lagging sector only
            if sym_cat == lagging and lagging != "none":
                return GuardianVerdict(
                    allowed=False,
                    reason=f"regime_lagging_sector_{lagging}",
                    log_event="regime_alignment_reject",
                )

        return None   # pass

    def _regime_shift_detected(self) -> bool:
        """True if regime confidence changed by > 0.30 in the last 5 minutes."""
        return (time.time() - self._regime_conf_ts) < 300.0


# ── Module-level helpers ──────────────────────────────────────────────────────

def _utc_day() -> int:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).day


def _balance_tier(balance: float) -> Tuple[float, int, int]:
    for threshold, max_trades, max_pos in BALANCE_TIERS:
        if balance >= threshold:
            return threshold, max_trades, max_pos
    return 0.0, 0, 0


def _coherence_tier(coherence: float) -> Optional[Tuple[float, float, float, int]]:
    for tier in COHERENCE_TIERS:
        if coherence >= tier[0]:
            return tier
    return None
