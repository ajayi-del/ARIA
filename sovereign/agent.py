"""
sovereign/agent.py — ARIA Sovereign: long-term wealth accumulation agent.

Architecture
────────────
SovereignAgent is the top-level orchestrator for Sovereign's 6-hour cycle.
It composes all sub-modules (portfolio, rotation, hedge, yield, executor)
and exposes a single coroutine for main.py to launch.

Six-step cycle:
  1. Refresh prices  — update portfolio from signal_price_stores
  2. Evaluate phase  — rotation_engine determines MarketPhase from SSI momentum
  3. Compute yield   — attribute yield components and log residual basis risk
  4. Plan rebalance  — portfolio generates RebalanceOrders for target weights
  5. Plan hedges     — hedge_engine generates perp short instructions (if TRANSITION/BEAR)
  6. Execute         — spot_executor runs the rebalance (sells → buys)
                       (perp hedges are advisory-only in v1 — sent to display, not executed)

Philosophical framing:
  Sovereign operates on wealth-time scales. Its "will" (Nietzsche) is to
  accumulate index exposure through every market cycle. Its "structure"
  (Kant) is the phase system preventing panic-sell. Its "conviction" is the
  3-timeframe momentum alignment that validates each phase transition.

  Sovereign never panics. It has a plan for every phase.
"""

from __future__ import annotations

import asyncio
import os
import time
import structlog
from typing import Dict, List, Optional, TYPE_CHECKING

from sovereign.portfolio    import SovereignPortfolio
from sovereign.rotation_engine import RotationEngine, MarketPhase, PhaseDecision
from sovereign.hedge_engine    import HedgeEngine, HedgePlan
from sovereign.yield_tracker   import SovereignYieldTracker, YieldComponents
from sovereign.spot_executor   import SovereignSpotExecutor

if TYPE_CHECKING:
    from funding.radar import FundingRadar

log = structlog.get_logger(__name__)

# Cycle interval — 6 hours
_CYCLE_INTERVAL_S: float = 6 * 3600

# Advisory-only flag: set to True to execute spot rebalances, False = plan only
# Changed to True when the user has confirmed Sovereign is authorised to trade
_SOVEREIGN_EXECUTE: bool = os.getenv("SOVEREIGN_EXECUTE", "false").lower() == "true"

# Minimum spot balance required before executing rebalances.
# The true arb strategy (spot + perp) uses spot for execution fees;
# if fee reserve is insufficient, trades are advisory-only until replenished.
_FEE_RESERVE_USD: float = 5.0   # hard floor in USD


class SovereignAgent:
    """
    ARIA's long-term portfolio accumulation agent.

    Lifecycle:
        agent = SovereignAgent(config)
        agent.set_dependencies(funding_radar, signal_price_stores, slp_tracker)
        await agent.sovereign_loop()   # runs forever at 6h intervals
    """

    # Minimum spot balance required to cover execution fees before rebalancing.
    # The true arb strategy runs spot + perp together; spot funds the fee side.
    _fee_reserve_usd: float = _FEE_RESERVE_USD

    def __init__(self, config) -> None:
        self.config    = config
        self.portfolio = SovereignPortfolio(config)
        self.rotation  = RotationEngine()
        self.hedge_eng = HedgeEngine()
        self.yield_trk = SovereignYieldTracker()
        self.executor  = SovereignSpotExecutor(config)

        # Injected by set_dependencies()
        self._funding_radar           = None
        self._signal_price_stores:    dict = {}
        self._slp_tracker             = None

        # Cycle state for display
        self._last_phase_decision:    Optional[PhaseDecision] = None
        self._last_hedge_plan:        Optional[HedgePlan]    = None
        self._last_yield_components:  Optional[YieldComponents] = None
        self._last_cycle_ts:          float = 0.0
        self._cycle_count:            int   = 0
        self._started_at:             float = time.time()
        # Fee reserve tracking — updated externally via set_spot_balance()
        self._spot_balance_usd:       float = 0.0
        self._fee_reserve_ok:         bool  = False

    def set_dependencies(
        self,
        funding_radar,
        signal_price_stores: dict,
        slp_tracker=None,
    ) -> None:
        """Wire external dependencies after construction."""
        self._funding_radar        = funding_radar
        self._signal_price_stores  = signal_price_stores
        self._slp_tracker          = slp_tracker

    def set_account_id(self, account_id: int) -> None:
        self.executor.set_account_id(account_id)

    def set_spot_balance(self, spot_balance_usd: float) -> None:
        """Update the latest spot balance. Called by balance_monitor_loop in main.py."""
        self._spot_balance_usd = spot_balance_usd
        self._fee_reserve_ok = spot_balance_usd >= self._fee_reserve_usd

    def _ensure_fee_reserve(self) -> bool:
        """
        Check that spot balance has enough to cover trading fees.

        The true arb strategy uses spot + perp together. Spot side funds
        execution fees. Returns True if fee reserve is adequate.
        Logs a warning when insufficient.
        """
        min_required = max(
            self._fee_reserve_usd,
            self._spot_balance_usd * 0.01,  # at least 1% of spot portfolio
        )
        adequate = self._spot_balance_usd >= min_required
        if not adequate:
            log.warning(
                "sovereign_fee_reserve_low",
                spot_balance_usd=round(self._spot_balance_usd, 4),
                min_required_usd=round(min_required, 4),
                action="rebalance deferred until fee reserve replenished",
            )
        return adequate

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def sovereign_loop(self) -> None:
        """
        Main Sovereign coroutine. Runs every 6 hours.
        Launched via asyncio.gather() in main.py.
        """
        # Discovery — fetch SSI spot symbol IDs once at startup
        await self.executor.discover_ssi_symbols()

        # Stagger first cycle slightly to let price feeds warm up
        await asyncio.sleep(30)

        while True:
            try:
                await self._run_cycle()
            except Exception as e:
                log.error("sovereign_cycle_error", error=str(e), exc_info=True)

            await asyncio.sleep(_CYCLE_INTERVAL_S)

    # ── Cycle ─────────────────────────────────────────────────────────────────

    async def _run_cycle(self) -> None:
        """Execute one full 6-step Sovereign cycle."""
        cycle_start = time.time()
        self._cycle_count += 1

        log.info(
            "sovereign_cycle_start",
            cycle=self._cycle_count,
            phase=self.rotation.current_phase().value,
            phase_age_h=round(self.rotation.phase_age_hours(), 1),
            execute_enabled=_SOVEREIGN_EXECUTE,
            spot_balance_usd=round(self._spot_balance_usd, 4),
            fee_reserve_ok=self._fee_reserve_ok,
        )

        # ── Step 1: Refresh prices ────────────────────────────────────────────
        self.portfolio.update_prices(self._signal_price_stores)

        # ── Step 2: Evaluate phase ─────────────────────────────────────────────
        carry_score = self._avg_carry_score()
        phase_decision = self.rotation.evaluate(
            self._signal_price_stores,
            carry_score=carry_score,
        )
        self._last_phase_decision = phase_decision
        self.portfolio.set_target_weights(phase_decision.allocations)

        log.info(
            "sovereign_phase_evaluated",
            phase=phase_decision.phase.value,
            confidence=phase_decision.confidence,
            hedge_active=phase_decision.hedge_active,
            reason=phase_decision.reason,
        )

        # ── Step 3: Compute yield ──────────────────────────────────────────────
        slp_yield = self._get_slp_cycle_yield()
        yield_components = self.yield_trk.compute_cycle_yield(
            portfolio=self.portfolio,
            avg_carry_score=carry_score,
            slp_yield_usd=slp_yield,
            cycle_hours=6.0,
        )
        self._last_yield_components = yield_components

        # Log residual basis risk EVERY cycle (hard rule #8)
        residual_basis = self.hedge_eng.get_residual_basis_risk(self.portfolio.positions)
        total_usd      = self.portfolio.total_value_usd()
        log.info(
            "sovereign_basis_risk",
            residual_basis_usd=residual_basis,
            total_portfolio_usd=round(total_usd, 2),
            residual_basis_pct=round(residual_basis / total_usd * 100, 1) if total_usd > 0 else 0.0,
            note="XRP/DOGE/ADA unhedgeable (MAG7) + non-LINK DeFi + all MEME",
        )

        # ── Step 4: Plan rebalance ─────────────────────────────────────────────
        rebalance_orders = self.portfolio.get_rebalance_orders()

        if rebalance_orders:
            log.info(
                "sovereign_rebalance_planned",
                orders=len(rebalance_orders),
                sells=[o.symbol for o in rebalance_orders if o.side == "sell"],
                buys=[o.symbol for o in rebalance_orders if o.side == "buy"],
            )

        # ── Step 5: Plan hedges ───────────────────────────────────────────────
        if phase_decision.hedge_active:
            hedge_plan = self.hedge_eng.compute_plan(self.portfolio.positions)
            self._last_hedge_plan = hedge_plan
            log.info(
                "sovereign_hedge_plan",
                phase=phase_decision.phase.value,
                instructions=len(hedge_plan.instructions),
                coverage_pct=round(hedge_plan.coverage_pct * 100, 1),
                residual_basis_pct=round(hedge_plan.residual_basis_pct * 100, 1),
            )
        else:
            self._last_hedge_plan = None

        # ── Step 6: Execute ───────────────────────────────────────────────────
        # Fee reserve check: spot balance must cover fees before executing.
        # The true arb strategy uses spot + perp; spot side funds exchange fees.
        _fee_ok = self._ensure_fee_reserve()
        if _SOVEREIGN_EXECUTE and rebalance_orders and _fee_ok:
            results = await self.executor.execute_rebalance(rebalance_orders)
            success_count = sum(1 for r in results if r.success)
            log.info(
                "sovereign_execution_complete",
                total=len(results), success=success_count,
                spot_balance_usd=round(self._spot_balance_usd, 4),
            )
        elif _SOVEREIGN_EXECUTE and rebalance_orders and not _fee_ok:
            log.warning(
                "sovereign_execution_skipped_fee_reserve",
                orders=len(rebalance_orders),
                spot_balance_usd=round(self._spot_balance_usd, 4),
                fee_reserve_min=self._fee_reserve_usd,
                note="increase spot balance to replenish fee reserve",
            )
        elif rebalance_orders:
            log.info(
                "sovereign_rebalance_advisory",
                note="SOVEREIGN_EXECUTE=false — plan computed but NOT executed",
                orders=len(rebalance_orders),
            )

        # ── Cycle complete ────────────────────────────────────────────────────
        self._last_cycle_ts = time.time()
        elapsed = time.time() - cycle_start
        log.info(
            "sovereign_cycle_complete",
            cycle=self._cycle_count,
            elapsed_s=round(elapsed, 2),
            portfolio_usd=round(self.portfolio.total_value_usd(), 2),
            phase=phase_decision.phase.value,
            net_yield_usd=yield_components.net_yield_usd,
        )

    # ── Display ───────────────────────────────────────────────────────────────

    def get_display_data(self) -> dict:
        """
        Return current state as a flat dict for the terminal panel.
        Called from display/terminal.py during each render tick.
        """
        pd = self._last_phase_decision
        yc = self._last_yield_components
        hp = self._last_hedge_plan

        yield_30d = self.yield_trk.get_summary_30d()
        portfolio_snap = self.portfolio.get_display_snapshot()

        return {
            # Phase
            "phase":          pd.phase.value if pd else self.rotation.current_phase().value,
            "phase_age_h":    round(self.rotation.phase_age_hours(), 1),
            "confidence":     round(pd.confidence, 2) if pd else 0.0,
            "hedge_active":   pd.hedge_active if pd else False,

            # Portfolio snapshot
            "portfolio":      portfolio_snap,

            # Yield
            "current_ussi_apy": round(yc.ussi_apy * 100, 1) if yc else 0.0,
            "net_yield_usd":    yc.net_yield_usd if yc else 0.0,
            "yield_30d_usd":    yield_30d.net_30d_usd,
            "holding_cost_usd": yc.holding_cost_usd if yc else 0.0,
            "avg_carry_score":  yc.avg_carry_score if yc else 0.0,

            # Hedge plan
            "hedge_instructions": [
                {
                    "symbol":       h.symbol,
                    "side":         h.side,
                    "notional_usd": h.notional_usd,
                    "reason":       h.reason,
                }
                for h in (hp.instructions if hp else [])
            ],
            "coverage_pct":      round(hp.coverage_pct * 100, 1) if hp else 0.0,
            "residual_basis_pct": round(
                self.hedge_eng.get_residual_basis_risk(self.portfolio.positions)
                / max(self.portfolio.total_value_usd(), 0.01) * 100, 1
            ),

            # Metadata
            "cycle_count":    self._cycle_count,
            "last_cycle_ts":  self._last_cycle_ts,
            "execute_enabled": _SOVEREIGN_EXECUTE,
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _avg_carry_score(self) -> float:
        """
        Average funding carry score across BTC/ETH/SOL/BNB from FundingRadar.
        Used to derive dynamic USSI APY estimate.
        Returns 0.0 if radar not available.
        """
        if self._funding_radar is None:
            return 0.0
        radar = self._funding_radar
        symbols = ["BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD"]
        scores = []
        for sym in symbols:
            snap = radar._snapshots.get(sym)
            if snap is not None:
                scores.append(snap.carry_score)
        return round(sum(scores) / len(scores), 3) if scores else 0.0

    def _get_slp_cycle_yield(self) -> float:
        """
        Get SLP vault yield for this 6h cycle from SLPVaultTracker.
        Returns 0.0 if tracker not wired.
        """
        if self._slp_tracker is None:
            return 0.0
        try:
            _, _, total_30d = self._slp_tracker.compute_yield_30d()
            # 6h / 720h (30d) = 1/120 of 30d yield per cycle
            return total_30d / 120.0
        except Exception:
            return 0.0
