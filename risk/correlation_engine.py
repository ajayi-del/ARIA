import math
import structlog
from typing import List, Dict, Tuple, Any

logger = structlog.get_logger(__name__)

class CorrelationEngine:
    """
    ARIA Correlation Engine v1.3
    Provides pairwise correlation lookups and portfolio VaR gating.
    Now standardized as CorrelationEngine.
    """
    def __init__(self):
        # Historical 30-day rolling correlation (Hardcoded initial values)
        self.matrix = {
            tuple(sorted(["BTC-USD", "ETH-USD"])):    0.88,
            tuple(sorted(["BTC-USD", "SOL-USD"])):    0.82,
            tuple(sorted(["BTC-USD", "AVAX-USD"])):   0.79,
            tuple(sorted(["BTC-USD", "BNB-USD"])):    0.75,
            tuple(sorted(["BTC-USD", "LINK-USD"])):   0.71,
            tuple(sorted(["BTC-USD", "XAUT-USD"])):   0.15,
            tuple(sorted(["BTC-USD", "USTECH-USD"])): 0.52,
            tuple(sorted(["ETH-USD", "SOL-USD"])):    0.85,
            tuple(sorted(["ETH-USD", "AVAX-USD"])):   0.81,
            tuple(sorted(["ETH-USD", "BNB-USD"])):    0.73,
            tuple(sorted(["ETH-USD", "LINK-USD"])):   0.76,
            tuple(sorted(["ETH-USD", "XAUT-USD"])):   0.12,
            tuple(sorted(["ETH-USD", "USTECH-USD"])): 0.48,
            tuple(sorted(["SOL-USD", "AVAX-USD"])):   0.83,
            tuple(sorted(["SOL-USD", "BNB-USD"])):    0.70,
            tuple(sorted(["SOL-USD", "LINK-USD"])):   0.72,
            tuple(sorted(["SOL-USD", "XAUT-USD"])):   0.08,
            tuple(sorted(["SOL-USD", "USTECH-USD"])): 0.44,
            tuple(sorted(["AVAX-USD", "BNB-USD"])):   0.71,
            tuple(sorted(["AVAX-USD", "LINK-USD"])):  0.69,
            tuple(sorted(["AVAX-USD", "XAUT-USD"])):  0.07,
            tuple(sorted(["AVAX-USD", "USTECH-USD"])): 0.42,
            tuple(sorted(["BNB-USD", "LINK-USD"])):   0.68,
            tuple(sorted(["BNB-USD", "XAUT-USD"])):   0.10,
            tuple(sorted(["BNB-USD", "USTECH-USD"])): 0.40,
            tuple(sorted(["LINK-USD", "XAUT-USD"])):  0.05,
            tuple(sorted(["LINK-USD", "USTECH-USD"])): 0.55,
            tuple(sorted(["XAUT-USD", "USTECH-USD"])): 0.22,
        }

    def get_correlation(self, symbol_a: str, symbol_b: str) -> float:
        """Looks up pairwise correlation from static matrix."""
        if symbol_a == symbol_b:
            return 1.0
        key = tuple(sorted([symbol_a, symbol_b]))
        return self.matrix.get(key, 0.5)

    def compute_portfolio_var(self, open_positions: List[Any], risk_per_trade: float) -> float:
        """
        Computes portfolio Value at Risk (VaR) in USD using pairwise correlations.
        VaR = sqrt(sum(r_i^2) + 2 * sum(r_i * r_j * rho_ij))
        """
        if not open_positions:
            return 0.0
            
        sum_r_sq = 0.0
        for pos in open_positions:
            risk = risk_per_trade
            if hasattr(pos, 'initial_risk_usd'):
                risk = pos.initial_risk_usd
            elif hasattr(pos, 'entry_price') and hasattr(pos, 'stop_price') and hasattr(pos, 'size'):
                risk = abs(pos.entry_price - pos.stop_price) * pos.size
                
            sum_r_sq += (risk ** 2)
            
        sum_cross = 0.0
        for i in range(len(open_positions)):
            for j in range(i + 1, len(open_positions)):
                pos_i = open_positions[i]
                pos_j = open_positions[j]
                
                # Fetch risk i
                risk_i = risk_per_trade
                if hasattr(pos_i, 'initial_risk_usd'): risk_i = pos_i.initial_risk_usd
                elif hasattr(pos_i, 'entry_price'): risk_i = abs(pos_i.entry_price - pos_i.stop_price) * pos_i.size
                
                # Fetch risk j
                risk_j = risk_per_trade
                if hasattr(pos_j, 'initial_risk_usd'): risk_j = pos_j.initial_risk_usd
                elif hasattr(pos_j, 'entry_price'): risk_j = abs(pos_j.entry_price - pos_j.stop_price) * pos_j.size
                
                rho = self.get_correlation(pos_i.symbol, pos_j.symbol)
                sum_cross += (risk_i * risk_j * rho)
                
        var = math.sqrt(sum_r_sq + 2 * sum_cross)
        return var

    def correlation_gate(
        self,
        candidate: Any,
        open_positions: List[Any],
        risk_amount: float,
        max_portfolio_var: float
    ) -> Tuple[bool, str]:
        """
        Gates candidates if adding them exceeds total portfolio risk.
        """
        projected = open_positions + [candidate]
        projected_var = self.compute_portfolio_var(projected, risk_amount)
        
        if projected_var > max_portfolio_var:
            return False, f"PORTFOLIO_VAR_EXCEEDED: projected={projected_var:.2f} max={max_portfolio_var:.2f}"
            
        return True, "portfolio_var_ok"

    def update_correlations(self, journal: Any) -> None:
        """Placeholder for future dynamic updates."""
        pass

# Legacy support for functional import (used in risk_engine.py)
def correlation_gate(candidate, open_positions, risk_amount, max_var):
    engine = CorrelationEngine()
    return engine.correlation_gate(candidate, open_positions, risk_amount, max_var)
