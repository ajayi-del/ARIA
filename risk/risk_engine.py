"""
Risk Engine

All hard gates. Called before every order.
Returns (approved: bool, reason: str).
"""

from typing import List, Dict, Optional, Tuple, Any
from execution.schemas import TradeCandidate
from .margin_engine import MarginEngine
from .position_manager import PositionManager


class RiskEngine:
    """
    All hard gates. Called before every order.
    Returns (approved: bool, reason: str).
    """
    
    def __init__(self, config, margin_engine: MarginEngine, position_manager: PositionManager, calendar_engine, correlation_engine=None, journal=None, performance_tracker=None, market_hours=None):
        self.config = config
        self.margin_engine = margin_engine
        self.position_manager = position_manager
        self.calendar_engine = calendar_engine
        self.correlation_engine = correlation_engine
        self.journal = journal
        self.performance_tracker = performance_tracker
        self.market_hours = market_hours
        self.daily_pnl = 0.0
        self.weekly_drawdown_paused_until = 0  # timestamp in ms
        self.allocation = {"directional_pct": 0.80, "arb_pct": 0.20}
        self._calendar_state = None
    
    async def validate(
        self,
        candidate: TradeCandidate,
        account_balance: float
    ) -> Tuple[bool, str]:
        """
        Gates checked in order:
        First failure returns immediately.
        """
        import time
        now_ms = int(time.time() * 1000)
        from datetime import datetime, timezone

        # GATE 0 — Calendar (High impact protection)
        if self.calendar_engine:
            cal_state = await self.calendar_engine.get_state(candidate.symbol)
            if cal_state.regime == "BLOCK":
                return False, f"CALENDAR_BLOCK: {cal_state.reason}"
            self._calendar_state = cal_state
        
        # GATE 0 — Market Hours (Pre-gated for USTECH/XAUT)
        if self.market_hours:
            ok, reason = self.market_hours.should_trade_symbol(candidate.symbol, datetime.now(timezone.utc))
            if not ok:
                return False, reason

        # GATE 0 — Live confirmation
        if self.config.mode == "live":
            if not getattr(self.config, 'live_mode_confirmed', False):
                raise RuntimeError("Live mode not confirmed. Set LIVE_MODE_CONFIRMED=true in .env")

        # GATE 1 — Balance floor
        if account_balance < getattr(self.config, 'balance_floor', 500):
            return False, "BALANCE_FLOOR_HIT"

        # GATE 2 — Weekly drawdown pause
        if now_ms < self.weekly_drawdown_paused_until:
             return False, "WEEKLY_DRAWDOWN_PAUSE_ACTIVE"
        
        if self.performance_tracker and self.journal:
            # Check for weekly drawdown > 10%
            stats = self.performance_tracker.compute(self.journal)
            if stats.max_drawdown_pct > 10.0:
                 # Pause for 48 hours
                 self.weekly_drawdown_paused_until = now_ms + (48 * 60 * 60 * 1000)
                 return False, "WEEKLY_DRAWDOWN_PAUSE_TRIGGERED"
        
        # GATE 1 — Symbol trade count
        if self.position_manager.count(candidate.symbol) >= 2:
            return False, f"MAX_TRADES_REACHED:{candidate.symbol}"
        
        # GATE 2 — Pyramid rule
        if self.position_manager.count(candidate.symbol) == 1:
            if not self.position_manager.can_pyramid(candidate.symbol):
                return False, f"PYRAMID_BLOCKED:TP1_NOT_HIT:{candidate.symbol}"
        
        # GATE 3 — Direction conflict
        existing = self.position_manager.get(candidate.symbol)
        if existing and existing[0].side != candidate.side:
            return False, f"DIRECTION_CONFLICT:{candidate.symbol}"
        
        # GATE 4 — Coherence minimum
        min_score = getattr(self.config, 'min_coherence', getattr(self.config, 'live_min_coherence', 4))
        if candidate.coherence_score < min_score:
            return False, f"COHERENCE_BELOW_MIN:{candidate.coherence_score}/{min_score}"
        
        # GATE 5 — R:R minimum
        rr = abs(candidate.tp1_price - candidate.entry_price) / abs(candidate.entry_price - candidate.stop_price)
        min_rr = getattr(self.config, 'min_rr_ratio', 2.0)
        if rr < min_rr:
            return False, f"RR_BELOW_MIN:{rr:.2f}/{min_rr}"
        
        # GATE 6 — Unified Sizing & Portfolio VaR
        try:
            # 6a. Compute Combined Multiplier (v1.3 Unified Chain)
            # Combined = Coherence × Freshness × Calendar × Allocation
            
            # Coherence (from signal generator / market state)
            coherence_mult = getattr(candidate, 'size_multiplier', 1.0)
            
            # Freshness (Derived from signal_age_ms)
            from intelligence.freshness import compute_freshness
            freshness_mult = compute_freshness(candidate.signal_age_ms, candidate.atr, candidate.entry_price)
            
            # Calendar Multiplier
            calendar_mult = self._calendar_state.size_multiplier if self._calendar_state else 1.0
            
            # Allocation Multiplier (directional vs arb)
            allocation_mult = self.allocation.get("directional_pct", 0.80)
            
            combined_mult = coherence_mult * freshness_mult * calendar_mult * allocation_mult
            
            # Hard Clamp
            combined_mult = min(1.5, combined_mult)
            
            # 6b. Calculate Max Leverage
            current_max_leverage = getattr(self.config, 'default_leverage', 4)
            if self.performance_tracker and self.journal:
                stats = self.performance_tracker.compute(self.journal)
                if stats.closed_trades >= 50 and stats.win_rate >= 0.45 and stats.profit_factor >= 1.2:
                    current_max_leverage = 7
            
            target_leverage = min(candidate.leverage, current_max_leverage)
            
            # 6c. Apply Sizing
            base_risk = getattr(self.config, 'risk_pct', getattr(self.config, 'live_risk_pct', 0.01))
            adjusted_risk_pct = base_risk * combined_mult
            
            # Width Adjustment (Calendar-aware stops)
            stop_mult = self._calendar_state.stop_atr_multiplier if self._calendar_state else 1.0
            stop_distance = candidate.stop_price - candidate.entry_price
            adjusted_stop = candidate.entry_price + (stop_distance * stop_mult)

            # ATR ratio for dynamic stop buffer
            # We assume candidate.atr_ratio is present; if not default to 1.0
            atr_ratio = getattr(candidate, 'atr_ratio', 1.0)

            size, margin, lev = self.margin_engine.compute_size(
                account_balance,
                adjusted_risk_pct,
                candidate.entry_price,
                adjusted_stop,
                target_leverage,
                candidate.symbol,
                atr_ratio=atr_ratio
            )
            
            # 6d. Portfolio VaR Gate
            if self.correlation_engine:
                open_positions = []
                for sym_pos in self.position_manager._positions.values():
                    open_positions.extend(sym_pos)
                
                risk_amount_usd = abs(candidate.entry_price - adjusted_stop) * size
                max_var = account_balance * 0.03  # 3% Portfolio VaR limit (spec)
                
                from .correlation_engine import correlation_gate
                ok, reason = correlation_gate(candidate, open_positions, risk_amount_usd, max_var)
                if not ok:
                    return False, f"PORTFOLIO_VAR_GATE:{reason}"

            # 6e. Stop Safety Check
            safe, reason = self.margin_engine.stop_is_safe(
                candidate.entry_price,
                adjusted_stop,
                1 if candidate.side == "long" else -1,
                lev,
                candidate.symbol,
                size
            )
            if not safe:
                return False, f"STOP_UNSAFE:{reason}"
                
        except Exception as e:
            return False, f"RISK_CALCULATION_ERROR:{str(e)}"
        
        # GATE 7 — Daily loss limit
        if self.daily_pnl <= -(account_balance * 0.03):
            return False, f"DAILY_LOSS_LIMIT_HIT:{self.daily_pnl:.2f}"
        
        # GATE 8 — Max deployed capital
        deployed = sum(pos.initial_margin for positions in self.position_manager._positions.values() for pos in positions)
        if deployed / account_balance > 0.40:
            return False, f"MAX_CAPITAL_DEPLOYED:{deployed/account_balance:.2f}"
        
        # All gates passed
        return True, "APPROVED"
    
    async def get_position_size(
        self,
        candidate: TradeCandidate,
        balance: float
    ) -> Tuple[float, float, int]:
        """
        Calls margin_engine.compute_size() with calendar adjustments
        Returns (size, initial_margin, leverage)
        """
        cal_state = await self.calendar_engine.get_state(candidate.symbol)
        
        # Apply calendar-adjusted risk
        base_risk = getattr(self.config, 'risk_pct', getattr(self.config, 'live_risk_pct', 0.02))
        adjusted_risk_pct = base_risk * cal_state.size_multiplier
        
        # Apply calendar-adjusted stop (wider stop during uncertain periods)
        stop_distance = candidate.stop_price - candidate.entry_price
        adjusted_stop = candidate.entry_price + (stop_distance * cal_state.stop_atr_multiplier)
        
        return self.margin_engine.compute_size(
            balance,
            adjusted_risk_pct,
            candidate.entry_price,
            adjusted_stop,
            candidate.leverage,
            candidate.symbol
        )

    def compute_allocation(
        self,
        funding_snapshots: Dict[str, Any],
        account_balance: float
    ) -> Dict[str, float]:
        """
        Regime-driven capital allocation based on average carry score.
        """
        import numpy as np
        if not funding_snapshots:
            self.allocation = {"directional_pct": 0.90, "arb_pct": 0.10}
        else:
            # Calculate average carry score
            carry_scores = [abs(getattr(snap, 'carry_score', 0)) for snap in funding_snapshots.values()]
            avg_carry = float(np.mean(carry_scores)) if carry_scores else 0.0
            
            if avg_carry >= 2.5:
                self.allocation = {"directional_pct": 0.65, "arb_pct": 0.35}
                print(f"HIGH_ARB_REGIME: avg_carry={avg_carry:.1f}")
            elif avg_carry >= 1.5:
                self.allocation = {"directional_pct": 0.75, "arb_pct": 0.25}
            elif avg_carry < 0.5:
                self.allocation = {"directional_pct": 0.90, "arb_pct": 0.10}
                print(f"LOW_ARB_REGIME: deploying more directional")
            else:
                self.allocation = {"directional_pct": 0.80, "arb_pct": 0.20}
        
        return {
            "directional": account_balance * self.allocation["directional_pct"],
            "arb": account_balance * self.allocation["arb_pct"]
        }
