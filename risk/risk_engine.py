"""
Risk Engine v2.0 — Rebalanced Gate Architecture

Gate order (cheap-first, fail-fast):

  PRE-SIGNAL:
    Gate 8   — Daily loss limit (5%)
    Gate A   — Regime alignment (BEAR blocks longs on correlated assets)
    Gate D   — Volatility regime (ATR ratio 0.5–3.0)
    Gate 0   — Calendar, market hours, live confirm, balance floor, drawdown, basis

  SIGNAL QUALITY:
    Gate 5   — Coherence minimum (2.0 — calibrates to optimal after 50 closed trades)
    Gate 6   — R:R minimum (2.0)

  POSITION MANAGEMENT:
    Gate 1   — Portfolio VaR (5%)
    Gate 2   — Symbol concentration cap (≤20% of balance per symbol)
    Gate 3   — Pyramid rule (only activates when existing position exists)
    Gate 4   — Direction conflict

  EXECUTION QUALITY:
    Gate 7   — Stop safety
    Gate B   — Spread / liquidity (≤0.5%)
    Gate C   — Funding alignment (never blocks; sets size multiplier)

All gate decisions are logged with gate name, symbol, approved, reason.
"""

from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timezone
import structlog
import time

from execution.schemas import TradeCandidate
from .margin_engine import MarginEngine
from .position_manager import PositionManager

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
        self._funding_mult = 1.0

        def _log(gate: str, ok: bool, reason: str) -> Tuple[bool, str]:
            logger.info(
                "gate_result",
                gate=gate,
                symbol=candidate.symbol,
                approved=ok,
                reason=reason,
            )
            return ok, reason

        # ── PRE-SIGNAL: cheap gates first ─────────────────────────────────

        ok, reason = self._gate_daily_loss(balance)
        if not ok:
            return _log("daily_loss", False, reason)
        _log("daily_loss", True, reason)

        ok, reason = self._gate_regime_alignment(candidate, regime)
        if not ok:
            return _log("regime", False, reason)
        _log("regime", True, reason)

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
                return _log("basis_stress", False, f"basis_stress:{basis:.4%}_venue_dislocation")

        if self.calendar_engine:
            cal_state = await self.calendar_engine.get_state(candidate.symbol)
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

        if balance < getattr(self.config, "balance_floor", 50.0):
            return _log("balance_floor", False, f"balance_floor:{balance:.2f}")
        _log("balance_floor", True, f"balance:{balance:.2f}")

        if now_ms < self.weekly_drawdown_paused_until:
            return _log("drawdown_pause", False, "weekly_drawdown_pause_active")

        if self.performance_tracker and self.journal:
            stats = self.performance_tracker.compute(self.journal)
            if stats.max_drawdown_pct > 10.0:
                self.weekly_drawdown_paused_until = now_ms + (48 * 60 * 60 * 1000)
                return _log("drawdown_pause", False, "weekly_drawdown_10pct_triggered_48h")

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
        Gate A — Livermore principle: trade with the primary trend, not against it.
        regime: "BULL" | "BEAR" | "RANGING"
        XAUT-USD is inverse-correlated — longs allowed even in BEAR.
        BULL: shorts allowed but coherence gate acts as filter.
        RANGING: both directions permitted.
        """
        INVERSE_ASSETS = {"XAUT-USD"}
        direction = candidate.side  # "long" | "short"
        symbol = candidate.symbol

        if regime == "BEAR":
            if direction == "long" and symbol not in INVERSE_ASSETS:
                return False, f"regime_bear_no_longs:{symbol}"

        # BULL regime: shorts are not hard-blocked (coherence filters them)
        # RANGING: no direction restriction
        return True, f"regime_{regime.lower()}_aligned"

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

        if ratio < 0.5:
            return False, f"market_too_quiet:{ratio:.2f}"
        if ratio > 3.0:
            return False, f"market_too_volatile:{ratio:.2f}"

        return True, f"volatility_normal:{ratio:.2f}"

    def _gate_coherence(self, candidate: TradeCandidate) -> Tuple[bool, str]:
        """
        Gate 5 — Minimum coherence score.
        Default 2.0 (temporary floor — coherence calibrator raises this threshold
        automatically after 50 closed trades based on actual win-rate data).
        """
        min_score = getattr(
            self.config, "min_coherence",
            getattr(self.config, "live_min_coherence", 2.0),
        )
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
            # Unified multiplier chain (v1.3)
            coherence_mult = getattr(candidate, "size_multiplier", 1.0)

            from intelligence.freshness import compute_freshness
            freshness_mult = compute_freshness(
                candidate.signal_age_ms, candidate.atr, candidate.entry_price
            )
            calendar_mult = (
                self._calendar_state.size_multiplier if self._calendar_state else 1.0
            )
            allocation_mult = self.allocation.get("directional_pct", 0.80)

            combined_mult = min(
                1.5,
                coherence_mult * freshness_mult * calendar_mult * allocation_mult,
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
            )

            # Portfolio VaR gate — 5% limit
            max_var_pct = getattr(self.config, "max_portfolio_var_pct", 0.05)
            max_var = balance * max_var_pct

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
        Gate 2 — Symbol concentration cap (≤20% of balance).
        Measures actual risk exposure, not trade count.
        Mathematically superior: two small trades are fine, one large one may be blocked.
        """
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
        return True, f"concentration:{symbol_exposure:.0f}_max:{max_exposure:.0f}_ok"

    def _gate_pyramid(self, candidate: TradeCandidate) -> Tuple[bool, str]:
        """
        Gate 3 — Pyramid rule: requires TP1 hit before adding to a position.
        Only activates when an existing position on this symbol already exists.
        Fresh symbol entries bypass this gate completely.
        """
        symbol = candidate.symbol
        positions = self.position_manager._positions.get(symbol, [])

        if not positions:
            return True, "no_existing_position_skip"

        existing = positions[0]
        if not getattr(existing, "tp1_hit", False):
            return False, f"pyramid_tp1_required:{symbol}"

        return True, "pyramid_tp1_hit_ok"

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
