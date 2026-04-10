import math
import structlog
from typing import List, Dict, Tuple, Any

logger = structlog.get_logger(__name__)

class CorrelationEngine:
    """
    ARIA Correlation Engine v1.3
    Provides pairwise correlation lookups and portfolio VaR gating.
    v1.3 Hardened: Implements Stressed VaR to protect against correlation spikes.
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
        
        # v1.3 Stressed correlations (Historical peak correlation during crashes)
        self.stress_matrix = {
            k: min(0.98, v * 1.25) if v > 0 else v for k, v in self.matrix.items()
        }

    def get_correlation(self, symbol_a: str, symbol_b: str, stressed: bool = False) -> float:
        """Looks up pairwise correlation from appropriate matrix."""
        if symbol_a == symbol_b:
            return 1.0
        key = tuple(sorted([symbol_a, symbol_b]))
        target = self.stress_matrix if stressed else self.matrix
        return target.get(key, 0.6 if stressed else 0.5)

    def compute_portfolio_var(self, open_positions: List[Any], risk_per_trade: float, stressed: bool = False) -> float:
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
                
                rho = self.get_correlation(pos_i.symbol, pos_j.symbol, stressed=stressed)
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
        v1.3 Hardened: Uses max(normal_var, stressed_var).
        """
        projected = open_positions + [candidate]
        
        # Calculate regular VaR
        normal_var = self.compute_portfolio_var(projected, risk_amount, stressed=False)
        
        # Calculate stressed VaR
        stressed_var = self.compute_portfolio_var(projected, risk_amount, stressed=True)
        
        active_var = max(normal_var, stressed_var)
        
        logger.info("portfolio_var_check", 
                    normal=f"{normal_var:.2f}", 
                    stressed=f"{stressed_var:.2f}",
                    limit=f"{max_portfolio_var:.2f}")
        
        if active_var > max_portfolio_var:
            return False, f"PORTFOLIO_VAR_EXCEEDED: var={active_var:.2f} max={max_portfolio_var:.2f}"
            
        return True, "portfolio_var_ok"

    def update_correlations(self, journal: Any) -> None:
        """Placeholder for future dynamic updates."""
        pass

# Legacy support for functional import (used in risk_engine.py / tests)
def correlation_gate(candidate, open_positions, risk_amount, max_var):
    engine = CorrelationEngine()
    return engine.correlation_gate(candidate, open_positions, risk_amount, max_var)

def compute_portfolio_var(open_positions, risk_per_trade, stressed=False):
    engine = CorrelationEngine()
    return engine.compute_portfolio_var(open_positions, risk_per_trade, stressed)
