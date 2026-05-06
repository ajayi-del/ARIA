"""
Risk Engine v2.0 — Rebalanced Gate Architecture

Gate order (cheap-first, fail-fast):

  PRE-SIGNAL:
    Gate 8   — Daily loss limit (5%)
    Gate A   — Regime alignment (sizing multiplier only — never blocks trades)
    Gate D   — Volatility regime (ATR ratio 0.5–3.0)
    Gate 0   — Calendar, market hours, live confirm, balance floor, drawdown, basis

  SIGNAL QUALITY:
    Gate 5   — Coherence minimum (2.0 — calibrates to optimal after 50 closed trades)
    Gate 6   — R:R minimum (2.0)

  POSITION MANAGEMENT:
    Gate 1   — Portfolio VaR (40% of current balance — dynamic)
    Gate 2   — Symbol concentration cap (≤20% of balance per symbol)
    Gate 3   — Pyramid rule (only activates when existing position exists)
    Gate 4   — Direction conflict

  EXECUTION QUALITY:
    Gate 7   — Stop safety
    Gate B   — Spread / liquidity (≤0.5%)
    Gate C   — Funding alignment (never blocks; sets size multiplier)

Sizing multiplier outputs (all read by caller after validate()):
    _funding_mult  — Gate C: funding headwind/tailwind (0.7–1.3×)
    _regime_mult   — Gate A: regime alignment (0.75–1.15×)

All gate decisions are logged with gate name, symbol, approved, reason.
"""

import os
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timezone
import structlog
import time

TRIA_ONLY = os.getenv("TRIA_ONLY", "false").lower() == "true"

from execution.schemas import TradeCandidate
from .margin_engine import MarginEngine
from .position_manager import PositionManager
from intelligence.relative_strength import REGIME_ALLOWED_SYMBOLS

logger = structlog.get_logger(__name__)


class RiskEngine:
    """
    All hard gates. Called before every order.
    Returns (approved: bool, reason: str).
    """

    def __init__(
        self,
        config,
        margin_engine: MarginEngine,
        position_manager: PositionManager,
        calendar_engine,
        correlation_engine=None,
        journal=None,
        performance_tracker=None,
        market_hours=None,
        orderbook_stores=None,
        basis_tracker=None,
    ):
        self.config = config
        self.margin_engine = margin_engine
        self.position_manager = position_manager
        self.calendar_engine = calendar_engine
        self.correlation_engine = correlation_engine
        self.journal = journal
        self.performance_tracker = performance_tracker
        self.market_hours = market_hours
        self.orderbook_stores = orderbook_stores
        self.basis_tracker = basis_tracker

        self.daily_pnl: float = 0.0
        self.weekly_drawdown_paused_until: int = 0  # ms timestamp
        self.allocation: Dict[str, float] = {"directional_pct": 0.80, "arb_pct": 0.20}
        self._calendar_state = None
        # Gate C output — set during validate(), read by caller to adjust size
        self._funding_mult: float = 1.0
        # Gate A output — regime sizing multiplier (0.75 counter-trend / 1.15 aligned)
        # Never hard-blocks trades. Signal engine direction is the source of truth.
        self._regime_mult: float = 1.0
        # Calendar cache: eliminates 11ms async DB round-trip on every gate cycle.
        # TTL=45s — calendar events change on hour/day boundaries, not per-minute.
        self._calendar_cache: Dict[str, tuple] = {}  # symbol -> (mono_ts, cal_state)
        # Cascade tracker (optional) — wired from main.py
        self._cascade_tracker = None
        # Adaptive calibrator (optional) — replaces static config.min_coherence
        self._adaptive_calibrator = None
        # Directional signal history for extreme-market consensus (last 12 signals/symbol)
        self._signal_history: Dict[str, Any] = {}
        # Kant overrides — set per-call in validate(), read in _gate_coherence()
        self._kant_overrides: Dict[str, Any] = {}
        # Last rejection per symbol — read by display_refresh_loop for UI
        self._rejection_cache: Dict[str, Any] = {}

    def get_last_rejection(self, symbol: str) -> dict | None:
        """Last gate rejection for this symbol — read by display_refresh_loop."""
        return self._rejection_cache.get(symbol)

    def set_cascade_tracker(self, tracker) -> None:
        """Wire in CascadeTracker for cascade-aware gate logic."""
        self._cascade_tracker = tracker

    def set_adaptive_calibrator(self, calibrator) -> None:
        """Wire in AdaptiveCalibrator to use dynamic coherence minimum."""
        self._adaptive_calibrator = calibrator

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # MAIN ENTRY POINT
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def validate(
        self,
        candidate: TradeCandidate,
        balance: float,
        regime: str = "RANGING",
        funding_rate: float = 0.0,
        current_atr: float = 0.0,
        avg_atr: float = 0.0,
        orderbook_store=None,
        drawdown_manager=None,
        kant_overrides: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """
        Gates evaluated in cost order: cheap fail-fast first.
        Every gate decision is logged: gate, symbol, approved, reason.
        Returns (approved, reason).

        New parameters (all have safe defaults for backward compatibility):
          regime        — "BULL" | "BEAR" | "RANGING" (from relative strength classifier)
          funding_rate  — raw decimal funding rate (0.001 = 0.1% per 8h)
          current_atr   — current ATR of the symbol
          avg_atr       — 20-period average ATR (derive: candidate.atr / candidate.atr_ratio)
          orderbook_store — live L2 book for this symbol
        """
        self._funding_mult   = 1.0
        self._regime_mult    = 1.0
        self._kant_overrides = kant_overrides or {}  # stored for _gate_coherence()

        def _log(gate: str, ok: bool, reason: str) -> Tuple[bool, str]:
            logger.info(
                "gate_result",
                gate=gate,
                symbol=candidate.symbol,
                approved=ok,
                reason=reason,
            )
            if not ok:
                self._rejection_cache[candidate.symbol] = {
                    "gate": gate, "reason": reason,
                    "coherence": getattr(candidate, "coherence_score", 0.0),
                    "timestamp_ms": int(time.time() * 1000),
                }
            return ok, reason

        # ── DrawdownManager halt gate (cheapest — checked first) ──────────────
        # Absolute halt: 25% total drawdown or 5% daily drawdown exceeded.
        # When halted, NO directional trades regardless of coherence or regime.
        # Arb is still allowed (delta-neutral = no drawdown contribution).
        if drawdown_manager is not None and not drawdown_manager.can_trade_directional():
            return _log("drawdown_halt", False,
                        f"drawdown_halted:{drawdown_manager._halt_reason}")
        _log("drawdown_halt", True, "drawdown_ok")

        # ── Cascade gate — direction-aware block during BLOCKED phase ─────────────
        # When cascade tracker is BLOCKED, only suppress trades that go WITH the
        # cascade momentum direction. Counter-cascade (fade) entries are allowed —
        # they trade AGAINST the pressure and are the highest-value liq signals.
        if self._cascade_tracker is not None:
            if self._cascade_tracker.is_blocked():
                momentum_dir = self._cascade_tracker.trade_dir_momentum
                if candidate.side == momentum_dir:
                    return _log("cascade_gate", False, "cascade_blocked_momentum")
                _log("cascade_gate", True, "cascade_fade_allowed")
            else:
                _log("cascade_gate", True, "cascade_clear")

        # ── PRE-SIGNAL: cheap gates first ─────────────────────────────────

        ok, reason = self._gate_daily_loss(balance)
        if not ok:
            return _log("daily_loss", False, reason)
        _log("daily_loss", True, reason)

        ok, reason = self._gate_regime_alignment(candidate, regime)
        if not ok:
            return _log("regime", False, reason)
        _log("regime", True, reason)

        ok, reason = self._gate_regime_symbol_restriction(candidate.symbol, regime)
        if not ok:
            return _log("regime_restrict", False, reason)
        _log("regime_restrict", True, reason)

        _cur_atr = current_atr if current_atr > 0 else candidate.atr
        ok, reason = self._gate_volatility_regime(candidate.symbol, _cur_atr, avg_atr)
        if not ok:
            return _log("volatility", False, reason)
        _log("volatility", True, reason)

        # ── Gate 0 block: Calendar / hours / live / balance / drawdown / basis ──
        now_ms = int(time.time() * 1000)

        if self.basis_tracker:
            self.basis_tracker.update(candidate.symbol)
            if self.basis_tracker.is_stressed(candidate.symbol):
                basis = self.basis_tracker.get_basis(candidate.symbol)
                # Kant basis_stress_weight:
                #   ACCUMULATION 0.30 → only block if weight×1 ≥ 1.0 (i.e. weight < 1 → pass)
                #   TREND        1.00 → block as normal
                #   DISTRIBUTION 2.00 → block (doubly stressed)
                #   CHAOS        9999 → always block
                _bsw = self._kant_overrides.get("basis_stress_weight", 1.0)
                if _bsw >= 1.0:
                    return _log("basis_stress", False,
                                f"basis_stress:{basis:.4%}_venue_dislocation"
                                f"_kant_weight={_bsw:.1f}")

        if self.calendar_engine:
            # Cache calendar state per symbol — DB lookup only every 45s.
            # Calendar events change on hour/day boundaries, not per candle.
            _mono = time.monotonic()
            _cached = self._calendar_cache.get(candidate.symbol)
            if _cached and (_mono - _cached[0]) < 45.0:
                cal_state = _cached[1]
            else:
                cal_state = await self.calendar_engine.get_state(candidate.symbol)
                self._calendar_cache[candidate.symbol] = (_mono, cal_state)
            if cal_state.regime == "BLOCK":
                return _log("calendar", False, f"calendar_block:{cal_state.reason}")
            self._calendar_state = cal_state
        _log("calendar", True, "calendar_ok")

        if self.market_hours:
            ok, reason = self.market_hours.should_trade_symbol(
                candidate.symbol, datetime.now(timezone.utc)
            )
            if not ok:
                return _log("market_hours", False, reason)
        _log("market_hours", True, "hours_ok")

        if self.config.mode == "live":
            if not getattr(self.config, "live_mode_confirmed", False):
                raise RuntimeError(
                    "Live mode not confirmed. Set LIVE_MODE_CONFIRMED=true in .env"
                )

        if not TRIA_ONLY and balance < getattr(self.config, "balance_floor", 50.0):
            return _log("balance_floor", False, f"balance_floor:{balance:.2f}")
        _log("balance_floor", True, f"balance:{balance:.2f}")

        if now_ms < self.weekly_drawdown_paused_until:
            # Early release: use today's live daily_pnl, not all-time journal PnL.
            # all-time total_pnl_usd includes paper/historical trades which poison the gate
            # when ARIA transitions from paper to live on the same journal files.
            daily_loss_pct = abs(self.daily_pnl) / balance if self.daily_pnl < 0 and balance > 0 else 0.0
            if daily_loss_pct <= 0.10:
                self.weekly_drawdown_paused_until = 0
                logger.info("weekly_drawdown_pause_released",
                            daily_pnl=round(self.daily_pnl, 2),
                            balance=round(balance, 2),
                            daily_loss_pct=round(daily_loss_pct * 100, 2))
                # Fall through to remaining gates
            else:
                return _log("drawdown_pause", False, "weekly_drawdown_pause_active")

        # Trigger: lost >10% of current balance in TODAY's live PnL.
        # Uses self.daily_pnl (reset each session) not all-time journal PnL,
        # so paper-mode history cannot poison the live gate.
        if balance > 0:
            daily_loss_pct = abs(self.daily_pnl) / balance if self.daily_pnl < 0 else 0.0
            if daily_loss_pct > 0.10:
                self.weekly_drawdown_paused_until = now_ms + (48 * 60 * 60 * 1000)
                return _log("drawdown_pause", False,
                            f"weekly_drawdown_10pct_triggered_48h:daily_loss={daily_loss_pct:.1%}")

        # ── SIGNAL QUALITY ─────────────────────────────────────────────────

        ok, reason = self._gate_coherence(candidate)
        if not ok:
            return _log("coherence", False, reason)
        _log("coherence", True, reason)

        ok, reason = self._gate_rr(candidate)
        if not ok:
            return _log("rr", False, reason)
        _log("rr", True, reason)

        # ── POSITION MANAGEMENT ────────────────────────────────────────────

        ok, reason = await self._gate_var(candidate, balance)
        if not ok:
            return _log("var", False, reason)
        _log("var", True, reason)

        ok, reason = self._gate_concentration(candidate, balance)
        if not ok:
            return _log("concentration", False, reason)
        _log("concentration", True, reason)

        ok, reason = self._gate_pyramid(candidate)
        if not ok:
            return _log("pyramid", False, reason)
        _log("pyramid", True, reason)

        ok, reason = self._gate_direction(candidate)
        if not ok:
            return _log("direction", False, reason)
        _log("direction", True, reason)

        # ── EXECUTION QUALITY ──────────────────────────────────────────────

        ok, reason = self._gate_stop_safety(candidate, balance)
        if not ok:
            return _log("stop_safety", False, reason)
        _log("stop_safety", True, reason)

        # Gate B: use caller-supplied store, fall back to engine's store dict
        _ob_store = orderbook_store or (
            self.orderbook_stores.get(candidate.symbol)
            if self.orderbook_stores else None
        )
        ok, reason = await self._gate_liquidity(candidate.symbol, _ob_store)
        if not ok:
            return _log("liquidity", False, reason)
        _log("liquidity", True, reason)

        # Gate C: funding alignment — never blocks, only sets size multiplier
        ok, reason, self._funding_mult = self._gate_funding_alignment(
            candidate, funding_rate
        )
        _log("funding", True, f"{reason} size_mult={self._funding_mult:.2f}")

        return _log("all_gates", True, "all_gates_passed")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # GATE IMPLEMENTATIONS
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _gate_daily_loss(self, balance: float) -> Tuple[bool, str]:
        """Gate 8 — Daily loss circuit breaker at 5% of balance."""
        max_loss_pct = getattr(self.config, "max_daily_loss_pct",
                               getattr(self.config, "daily_loss_limit_pct", 0.05))
        limit = -(balance * max_loss_pct)
        if self.daily_pnl <= limit:
            return False, f"daily_loss_limit:{self.daily_pnl:.2f}_limit:{limit:.2f}"
        return True, f"daily_pnl:{self.daily_pnl:.2f}"

    def _gate_regime_alignment(
        self, candidate: TradeCandidate, regime: str
    ) -> Tuple[bool, str]:
        """
        Gate A — Regime as a sizing indicator. NEVER hard-blocks trades.

        Signal engine direction is the source of truth for trade decisions.
        Regime adjusts position size only — stored in self._regime_mult:

          Aligned  (BULL+long, BEAR+short) → 1.15×  structural tailwind
          Counter  (BULL+short, BEAR+long) → 0.75×  structural headwind
          RANGING or XAUT-USD              → 1.0×   neutral

        The 0.75× counter-trend penalty still allows trades — ARIA can always
        trade in any regime. Gate 5 (coherence) is the quality filter.
        """
        INVERSE_ASSETS = {"XAUT-USD"}
        direction = candidate.side  # "long" | "short"
        symbol = candidate.symbol

        # Inverse asset or ranging — no regime bias
        if symbol in INVERSE_ASSETS or regime == "RANGING":
            self._regime_mult = 1.0
            return True, f"regime_{regime.lower()}_neutral:mult=1.0"

        if regime == "BEAR":
            if direction == "long":
                # Counter-trend — reduce size but still execute
                self._regime_mult = 0.75
                return True, f"regime_bear_counter_long:mult={self._regime_mult:.2f}"
            elif direction == "short":
                # Aligned — structural tailwind → boost size
                self._regime_mult = 1.15
                return True, f"regime_bear_aligned_short:mult={self._regime_mult:.2f}"

        if regime == "BULL":
            if direction == "long":
                # Aligned — structural tailwind → boost size
                self._regime_mult = 1.15
                return True, f"regime_bull_aligned_long:mult={self._regime_mult:.2f}"
            elif direction == "short":
                # Counter-trend — reduce size but still execute
                self._regime_mult = 0.75
                return True, f"regime_bull_counter_short:mult={self._regime_mult:.2f}"

        # Fallback — unrecognised regime string
        self._regime_mult = 1.0
        return True, f"regime_{regime.lower()}_neutral:mult=1.0"

    def record_signal(self, symbol: str, direction: str, coherence: float) -> None:
        """
        Track signal direction and strength for extreme-market directional consensus.
        Called from main.py on every SIGNAL_READY event before risk validation.
        Maintains a rolling window of last 12 signals per symbol.
        """
        from collections import deque
        if symbol not in self._signal_history:
            self._signal_history[symbol] = deque(maxlen=12)
        self._signal_history[symbol].append((direction, coherence, time.time()))

    def _compute_consensus_mult(
        self, symbol: str, direction: str, atr_ratio: float
    ) -> float:
        """
        Extreme-market directional consensus multiplier.

        In high-volatility (ATR ratio > 1.5), the dominant signal direction over the
        last 10 minutes earns a size boost; the minority direction is penalised.
        Calm markets (ratio ≤ 1.5): returns 1.0 — no consensus adjustment.

        Principle: in fast markets, momentum compounds — lean harder with the flow.
        When the SignalFeedbackEngine fast-blocks a losing direction, the opposite
        direction naturally becomes dominant and receives the boost automatically.

        Returns: 1.2 (dominant), 0.8 (minority), or 1.0 (balanced / insufficient data).
        """
        history = self._signal_history.get(symbol)
        if not history or len(history) < 3:
            return 1.0  # insufficient data

        # Only apply in volatile/extreme conditions
        if atr_ratio < 1.5:
            return 1.0

        now = time.time()
        recent = [(d, c) for d, c, t in history if now - t < 600]  # 10-min recency
        if len(recent) < 3:
            return 1.0

        # Coherence-weighted direction counts — strong signals count more
        long_w = sum(c for d, c in recent if d == "long")
        short_w = sum(c for d, c in recent if d == "short")
        total = long_w + short_w
        if total == 0:
            return 1.0

        dominant_w = long_w if direction == "long" else short_w
        dominant_pct = dominant_w / total

        if dominant_pct >= 0.65:
            return 1.2   # dominant direction — lean harder with the flow
        elif dominant_pct <= 0.35:
            return 0.8   # minority direction — reduced conviction
        return 1.0        # balanced — no adjustment

    def _gate_regime_symbol_restriction(
        self, symbol: str, regime: str
    ) -> Tuple[bool, str]:
        """
        Gate E — Regime symbol whitelist.

        Certain regimes should only trade specific assets that align with the
        macro structure. e.g. risk_off → only XAUT is structurally sound.

        Uses REGIME_ALLOWED_SYMBOLS from intelligence.relative_strength.
        Regime strings are lowercase (new v2.0 classifier). Safely ignores
        unknown regimes (None → all symbols pass).
        """
        # Normalise: old uppercase regimes pass through
        regime_key = regime.lower()
        allowed = REGIME_ALLOWED_SYMBOLS.get(regime_key)
        if allowed is not None and symbol not in allowed:
            return False, (
                f"REGIME_RESTRICTED:{regime_key}"
                f"_allowed:{','.join(allowed)}"
            )
        return True, f"regime_symbol_ok:{regime_key}"

    def _gate_volatility_regime(
        self, symbol: str, current_atr: float, avg_atr: float
    ) -> Tuple[bool, str]:
        """
        Gate D — Block dead markets (fees eat edge) and extreme volatility (stop hunts).
        ATR ratio = current_atr / 20-period avg_atr.
        Derives avg_atr from candidate: avg = atr / atr_ratio (caller must pass or derive it).
        Gate is skipped if ATR data is absent.
        """
        if avg_atr <= 0 or current_atr <= 0:
            return True, "no_atr_data_skip"

        ratio = current_atr / avg_atr

        # Lower bound: use Kant's structure-aware baseline when confidence > 0.50,
        # otherwise fall back to hardcoded 0.5.  Kant can raise this (CHAOS=1.30)
        # or lower it (ACCUMULATION=0.50) based on detected structure.
        _kant_atr_min = self._kant_overrides.get("atr_baseline_min")
        _kant_conf    = self._kant_overrides.get("kant_confidence", 1.0)
        lower_bound = _kant_atr_min if (_kant_atr_min is not None and _kant_conf > 0.50) else 0.5

        if ratio < lower_bound:
            return False, f"market_too_quiet:{ratio:.2f}_floor:{lower_bound:.2f}"
        if ratio > 3.0:
            return False, f"market_too_volatile:{ratio:.2f}"

        return True, f"volatility_normal:{ratio:.2f}"

    def _gate_coherence(self, candidate: TradeCandidate) -> Tuple[bool, str]:
        """
        Gate 5 — Minimum coherence score.

        Priority order for minimum threshold:
          1. AdaptiveCalibrator (fast-loop raised threshold) if wired
          2. Kant override (structure-aware threshold) — takes max with adaptive
          3. Cascade PRIMED: reduced floor = max(cascade_min_coherence, config.min_coherence-1.0)
          4. config.min_coherence (static default 2.0)

        Kant override is only applied when confidence > 0.50 (stored on overrides dict).
        """
        # 1. Adaptive calibrator (dynamic — overrides static config)
        if self._adaptive_calibrator is not None:
            min_score = self._adaptive_calibrator.get_coherence_minimum()
        else:
            min_score = getattr(
                self.config, "min_coherence",
                getattr(self.config, "live_min_coherence", 3.0),
            )

        # 2. Kant coherence floor — take max so Kant can only RAISE the bar,
        # never lower it below what the adaptive calibrator already requires.
        # Only applied when Kant confidence > 0.50 (passed in overrides dict).
        _kant_coh = self._kant_overrides.get("coherence_min")
        _kant_conf = self._kant_overrides.get("kant_confidence", 1.0)
        if _kant_coh is not None and _kant_conf > 0.50:
            min_score = max(min_score, _kant_coh)

        # 3. Cascade PRIMED: invite entries by temporarily relaxing the coherence floor
        # Direction must match the expected recovery direction
        if (self._cascade_tracker is not None
                and self._cascade_tracker.is_primed()):
            primed_dir = self._cascade_tracker.get_primed_direction()
            if candidate.side == primed_dir:
                cascade_floor = max(
                    getattr(self.config, "cascade_min_coherence", 3.0),
                    min_score - 1.0,
                )
                min_score = cascade_floor

        if candidate.coherence_score < min_score:
            return False, f"coherence:{candidate.coherence_score:.1f}_min:{min_score:.1f}"
        return True, f"coherence:{candidate.coherence_score:.1f}_ok"

    def _gate_rr(self, candidate: TradeCandidate) -> Tuple[bool, str]:
        """Gate 6 — Minimum R:R ratio of 2.0."""
        if candidate.rr_ratio < 2.0:
            return False, f"rr:{candidate.rr_ratio:.2f}_min:2.0"
        return True, f"rr:{candidate.rr_ratio:.2f}_ok"

    async def _gate_var(
        self, candidate: TradeCandidate, balance: float
    ) -> Tuple[bool, str]:
        """
        Gate 1 — Portfolio VaR limit (5%).
        Also runs the full unified multiplier chain to compute adjusted position size
        for accurate VaR measurement.
        """
        try:
            if TRIA_ONLY:
                # Tria manages its own balance and VaR — skip SoDEX margin checks
                return True, "tria_var_bypass"

            # Unified multiplier chain (v2.0)
            # Hierarchy: coherence × freshness × calendar × allocation × regime × consensus
            coherence_mult = getattr(candidate, "size_multiplier", 1.0)

            from intelligence.freshness import compute_freshness
            freshness_mult = compute_freshness(
                candidate.signal_age_ms, candidate.atr, candidate.entry_price
            )
            calendar_mult = (
                self._calendar_state.size_multiplier if self._calendar_state else 1.0
            )
            allocation_mult = self.allocation.get("directional_pct", 0.80)

            # Gate A regime multiplier (already computed — aligned=1.15×, counter=0.75×)
            regime_mult = self._regime_mult

            # Extreme-market directional consensus multiplier
            # Only active when ATR ratio > 1.5 (volatile conditions).
            # Dominant signal direction over last 10 min earns 1.2×; minority earns 0.8×.
            atr_ratio = getattr(candidate, "atr_ratio", 1.0)
            consensus_mult = self._compute_consensus_mult(
                candidate.symbol, candidate.side, atr_ratio
            )

            combined_mult = min(
                1.5,
                coherence_mult * freshness_mult * calendar_mult * allocation_mult
                * regime_mult * consensus_mult,
            )

            # Dynamic leverage ceiling (unlocks at ≥50 trades, WR≥45%, PF≥1.2)
            current_max_lev = getattr(self.config, "default_leverage", 4)
            if self.performance_tracker and self.journal:
                stats = self.performance_tracker.compute(self.journal)
                if (
                    stats.closed_trades >= 50
                    and stats.win_rate >= 0.45
                    and stats.profit_factor >= 1.2
                ):
                    current_max_lev = 7

            target_leverage = min(candidate.leverage, current_max_lev)

            # Adjusted risk and calendar-aware stop
            base_risk = getattr(
                self.config, "risk_pct",
                getattr(self.config, "live_risk_pct", 0.01),
            )
            adjusted_risk_pct = base_risk * combined_mult

            stop_mult = (
                self._calendar_state.stop_atr_multiplier if self._calendar_state else 1.0
            )
            stop_distance = candidate.stop_price - candidate.entry_price
            adjusted_stop = candidate.entry_price + (stop_distance * stop_mult)
            atr_ratio = getattr(candidate, "atr_ratio", 1.0)

            size, _margin, _lev = self.margin_engine.compute_size(
                balance,
                adjusted_risk_pct,
                candidate.entry_price,
                adjusted_stop,
                target_leverage,
                candidate.symbol,
                atr_ratio=atr_ratio,
                min_notional_usd=getattr(self.config, 'min_trade_notional_usd', 80.0),
            )

            # Portfolio VaR gate — dynamic: max_var_pct × current balance.
            # balance is passed in from execution_cleanup_loop's cached value,
            # so max_var scales automatically as account grows or shrinks.
            max_var_pct = getattr(self.config, "max_portfolio_var_pct", 0.40)
            max_var = balance * max_var_pct

            # Pre-trade margin availability check.
            # compute_size caps initial_margin at 90% of TOTAL balance, but does not
            # account for margin already consumed by open positions. If this trade's
            # required margin exceeds the remaining free margin, SoDEX will reject
            # silently with "insufficient balance". Gate it here instead.
            used_margin = sum(
                float(pos.initial_margin)
                for sym_pos in self.position_manager._positions.values()
                for pos in sym_pos
                if pos.symbol != candidate.symbol  # same-symbol pyramid is ok
            )
            free_margin = balance - used_margin
            # Allow 5% buffer for fee accrual and funding debits
            _margin_headroom = balance * 0.05
            if _margin > (free_margin - _margin_headroom):
                return False, (
                    f"margin_insufficient:required={_margin:.2f}"
                    f"_free={free_margin:.2f}_used={used_margin:.2f}"
                )

            if self.correlation_engine:
                open_positions = []
                for sym_pos in self.position_manager._positions.values():
                    open_positions.extend(sym_pos)

                risk_amount_usd = abs(candidate.entry_price - adjusted_stop) * size

                from .correlation_engine import correlation_gate
                ok, reason = correlation_gate(
                    candidate, open_positions, risk_amount_usd, max_var
                )
                if not ok:
                    return False, f"portfolio_var:{reason}"

        except Exception as e:
            return False, f"var_calc_error:{str(e)}"

        return True, "var_ok"

    def _gate_concentration(
        self, candidate: TradeCandidate, balance: float
    ) -> Tuple[bool, str]:
        """
        Gate 2 — Symbol concentration cap (≤20% of balance, ≤$500 absolute notional).
        Checks BOTH relative exposure (% of balance) AND absolute notional.
        Two limits serve different purposes:
          - Relative cap: scales with account — protects against overweighting as balance grows
          - Absolute cap: hard floor — prevents a single symbol from eating the whole account
            even on a small balance (e.g., $200 account at 20% = $40; still meaningful).
        """
        if TRIA_ONLY:
            return True, "tria_concentration_bypass"

        max_pct = getattr(self.config, "max_symbol_concentration", 0.20)
        max_exposure = balance * max_pct

        symbol_exposure = sum(
            abs(pos.entry_price - pos.stop_price) * pos.size
            for positions in self.position_manager._positions.values()
            for pos in positions
            if pos.symbol == candidate.symbol
        )

        if symbol_exposure >= max_exposure:
            return (
                False,
                f"symbol_concentration:{symbol_exposure:.0f}_max:{max_exposure:.0f}",
            )

        # Absolute notional cap — prevents over-sizing on any single symbol regardless
        # of account balance. Default $500; can be raised via config as account scales.
        _max_notional = getattr(self.config, "max_symbol_notional_usd", 500.0)
        _trade_notional = candidate.entry_price * candidate.size
        if _trade_notional > _max_notional:
            return (
                False,
                f"notional_cap:{_trade_notional:.0f}_max:{_max_notional:.0f}",
            )

        return True, f"concentration:{symbol_exposure:.0f}_max:{max_exposure:.0f}_ok"

    def _gate_pyramid(self, candidate: TradeCandidate) -> Tuple[bool, str]:
        """
        Gate 3 — Signal-confirmed pyramid rule (v1.7).

        Conditions to add to an existing position:
          1. TP1 must have been hit (price confirmation).
          2. Current coherence ≥ entry coherence (signal still valid or stronger).

        If entry_coherence is 0.0 (pre-v1.7 positions), condition 2 is skipped.
        Fresh entries (no existing position) bypass this gate entirely.
        """
        symbol = candidate.symbol
        positions = self.position_manager._positions.get(symbol, [])

        if not positions:
            return True, "no_existing_position_skip"

        existing = positions[0]

        # Condition 1: TP1 hit
        if not getattr(existing, "tp1_hit", False):
            return False, f"pyramid_tp1_required:{symbol}"

        # Condition 2: Signal must be as strong or stronger than entry (v1.7)
        entry_coh = getattr(existing, "entry_coherence", 0.0)
        if entry_coh > 0.0 and candidate.coherence_score < entry_coh:
            return False, (
                f"pyramid_signal_weakened:{candidate.coherence_score:.2f}_<_entry:{entry_coh:.2f}"
            )

        return True, f"pyramid_ok:coh={candidate.coherence_score:.2f}_entry_coh={entry_coh:.2f}"

    def _gate_direction(self, candidate: TradeCandidate) -> Tuple[bool, str]:
        """Gate 4 — Prevent conflicting direction on the same symbol."""
        existing = self.position_manager.get(candidate.symbol)
        if existing and existing[0].side != candidate.side:
            return (
                False,
                f"direction_conflict:{candidate.symbol}_{existing[0].side}_vs_{candidate.side}",
            )
        return True, "direction_ok"

    def _gate_stop_safety(
        self, candidate: TradeCandidate, balance: float
    ) -> Tuple[bool, str]:
        """Gate 7 — Stop must be on the correct side and not at liquidation distance."""
        if TRIA_ONLY:
            return True, "tria_stop_safety_bypass"
        try:
            stop_mult = (
                self._calendar_state.stop_atr_multiplier if self._calendar_state else 1.0
            )
            stop_distance = candidate.stop_price - candidate.entry_price
            adjusted_stop = candidate.entry_price + (stop_distance * stop_mult)
            atr_ratio = getattr(candidate, "atr_ratio", 1.0)

            size, _margin, lev = self.margin_engine.compute_size(
                balance,
                getattr(self.config, "risk_pct", 0.01),
                candidate.entry_price,
                adjusted_stop,
                candidate.leverage,
                candidate.symbol,
                atr_ratio=atr_ratio,
                min_notional_usd=getattr(self.config, 'min_trade_notional_usd', 80.0),
            )

            safe, reason = self.margin_engine.stop_is_safe(
                candidate.entry_price,
                adjusted_stop,
                1 if candidate.side == "long" else -1,
                lev,
                candidate.symbol,
                size,
            )
            if not safe:
                return False, f"stop_unsafe:{reason}"
        except Exception as e:
            return False, f"stop_safety_error:{str(e)}"
        return True, "stop_safe"

    async def _gate_liquidity(
        self, symbol: str, orderbook_store
    ) -> Tuple[bool, str]:
        """
        Gate B — Block when bid-ask spread exceeds 0.5%.
        Wide spread = thin market = bad fills + high slippage.
        Gate is skipped when no orderbook data is available.
        """
        if orderbook_store is None:
            return True, "no_ob_data_skip"

        try:
            if hasattr(orderbook_store, "is_healthy") and not orderbook_store.is_healthy(5000):
                return True, "ob_stale_skip"

            if hasattr(orderbook_store, "top_of_book"):
                best_bid, best_ask, spread = orderbook_store.top_of_book()
            else:
                bids = getattr(orderbook_store, "bids", [])
                asks = getattr(orderbook_store, "asks", [])
                if not bids or not asks:
                    return True, "no_ob_levels_skip"
                best_bid = bids[0][0] if isinstance(bids[0], (list, tuple)) else float(bids[0])
                best_ask = asks[0][0] if isinstance(asks[0], (list, tuple)) else float(asks[0])
                spread = best_ask - best_bid

            mid = (best_bid + best_ask) / 2.0
            if mid <= 0:
                return True, "mid_zero_skip"

            spread_pct = spread / mid
            # Config stores threshold in basis points; convert to decimal
            max_spread_pct = getattr(self.config, "max_spread_bps", 50.0) / 10_000.0

            if spread_pct > max_spread_pct:
                return False, f"spread_too_wide:{spread_pct:.4f}_max:{max_spread_pct:.4f}"

        except Exception:
            return True, "ob_error_skip"

        return True, "liquidity_ok"

    def _gate_funding_alignment(
        self, candidate: TradeCandidate, funding_rate: float
    ) -> Tuple[bool, str, float]:
        """
        Gate C — Seykota principle: trade with structural flow.
        funding_rate: raw decimal (0.001 = 0.1% per 8h from SoDEX).
        Returns (approved, reason, size_mult).
        Never blocks trades — only adjusts position size:
          tailwind  → 1.3× size
          headwind  → 0.7× size
          neutral   → 1.0× size
        """
        direction = candidate.side  # "long" | "short"
        HIGH_FUNDING = 0.001  # 0.1% per 8h

        if funding_rate > HIGH_FUNDING:
            # Positive: longs pay shorts — structural favour for shorts
            if direction == "long":
                return True, "funding_headwind_long", 0.7
            else:
                return True, "funding_tailwind_short", 1.3

        if funding_rate < -HIGH_FUNDING:
            # Negative: shorts pay longs — structural favour for longs
            if direction == "short":
                return True, "funding_headwind_short", 0.7
            else:
                return True, "funding_tailwind_long", 1.3

        return True, "funding_neutral", 1.0

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # SIZING & ALLOCATION HELPERS
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def get_position_size(
        self, candidate: TradeCandidate, balance: float
    ) -> Tuple[float, float, int]:
        """Returns (size, initial_margin, leverage) with calendar-adjusted risk and stop."""
        cal_state = await self.calendar_engine.get_state(candidate.symbol)

        base_risk = getattr(
            self.config, "risk_pct",
            getattr(self.config, "live_risk_pct", 0.02),
        )
        adjusted_risk_pct = base_risk * cal_state.size_multiplier

        stop_distance = candidate.stop_price - candidate.entry_price
        adjusted_stop = candidate.entry_price + (
            stop_distance * cal_state.stop_atr_multiplier
        )

        return self.margin_engine.compute_size(
            balance,
            adjusted_risk_pct,
            candidate.entry_price,
            adjusted_stop,
            candidate.leverage,
            candidate.symbol,
            min_notional_usd=getattr(self.config, 'min_trade_notional_usd', 80.0),
        )

    def compute_allocation(
        self,
        funding_snapshots: Dict[str, Any],
        account_balance: float,
    ) -> Dict[str, float]:
        """Regime-driven capital split based on average carry score."""
        import numpy as np

        if not funding_snapshots:
            self.allocation = {"directional_pct": 0.90, "arb_pct": 0.10}
        else:
            carry_scores = [
                abs(getattr(snap, "carry_score", 0))
                for snap in funding_snapshots.values()
            ]
            avg_carry = float(np.mean(carry_scores)) if carry_scores else 0.0

            if avg_carry >= 2.5:
                self.allocation = {"directional_pct": 0.65, "arb_pct": 0.35}
            elif avg_carry >= 1.5:
                self.allocation = {"directional_pct": 0.75, "arb_pct": 0.25}
            elif avg_carry < 0.5:
                self.allocation = {"directional_pct": 0.90, "arb_pct": 0.10}
            else:
                self.allocation = {"directional_pct": 0.80, "arb_pct": 0.20}

        return {
            "directional": account_balance * self.allocation["directional_pct"],
            "arb": account_balance * self.allocation["arb_pct"],
        }
