"""
Risk Engine

All hard gates. Called before every order.
Returns (approved: bool, reason: str).
"""

from typing import Tuple
from execution.schemas import TradeCandidate
from .margin_engine import MarginEngine
from .position_manager import PositionManager


class RiskEngine:
    """
    All hard gates. Called before every order.
    Returns (approved: bool, reason: str).
    """
    
    def __init__(self, config, margin_engine: MarginEngine, position_manager: PositionManager):
        self.config = config
        self.margin_engine = margin_engine
        self.position_manager = position_manager
        self.daily_pnl = 0.0
    
    def validate(
        self,
        candidate: TradeCandidate,
        account_balance: float
    ) -> Tuple[bool, str]:
        """
        Gates checked in order:
        First failure returns immediately.
        """
        
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
        min_score = getattr(self.config, 'live_min_coherence', 4)
        if candidate.coherence_score < min_score:
            return False, f"COHERENCE_BELOW_MIN:{candidate.coherence_score}/{min_score}"
        
        # GATE 5 — R:R minimum
        rr = abs(candidate.tp1_price - candidate.entry_price) / abs(candidate.entry_price - candidate.stop_price)
        min_rr = getattr(self.config, 'min_rr_ratio', 2.0)
        if rr < min_rr:
            return False, f"RR_BELOW_MIN:{rr:.2f}/{min_rr}"
        
        # GATE 6 — Stop safety
        try:
            size, margin, lev = self.margin_engine.compute_size(
                account_balance,
                getattr(self.config, 'live_risk_pct', 0.02),
                candidate.entry_price,
                candidate.stop_price,
                candidate.leverage,
                candidate.symbol
            )
            safe, reason = self.margin_engine.stop_is_safe(
                candidate.entry_price,
                candidate.stop_price,
                1 if candidate.side == "long" else -1,
                lev,
                candidate.symbol,
                size
            )
            if not safe:
                return False, f"STOP_UNSAFE:{reason}"
        except ValueError as e:
            return False, f"SIZE_CALCULATION_ERROR:{str(e)}"
        
        # GATE 7 — Daily loss limit
        if self.daily_pnl <= -(account_balance * 0.03):
            return False, f"DAILY_LOSS_LIMIT_HIT:{self.daily_pnl:.2f}"
        
        # GATE 8 — Max deployed capital
        deployed = sum(pos.initial_margin for positions in self.position_manager._positions.values() for pos in positions)
        if deployed / account_balance > 0.40:
            return False, f"MAX_CAPITAL_DEPLOYED:{deployed/account_balance:.2f}"
        
        # All gates passed
        return True, "APPROVED"
    
    def get_position_size(
        self,
        candidate: TradeCandidate,
        balance: float
    ) -> Tuple[float, float, int]:
        """
        Calls margin_engine.compute_size()
        Returns (size, initial_margin, leverage)
        """
        return self.margin_engine.compute_size(
            balance,
            getattr(self.config, 'live_risk_pct', 0.02),
            candidate.entry_price,
            candidate.stop_price,
            candidate.leverage,
            candidate.symbol
        )
