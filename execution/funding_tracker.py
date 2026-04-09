import structlog
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = structlog.get_logger(__name__)

@dataclass
class FundingPayment:
    symbol: str
    amount_usd: float  # Negative if paid, positive if received
    timestamp_ms: int

class FundingTracker:
    """
    Quant Fix #2: Funding Cost Deduction.
    Tracks all funding payments to ensure net P&L accuracy.
    """
    def __init__(self):
        self._payments: List[FundingPayment] = []
        self._per_symbol_totals: Dict[str, float] = {}

    def add_payment(self, symbol: str, amount_usd: float, timestamp_ms: int):
        payment = FundingPayment(symbol, amount_usd, timestamp_ms)
        self._payments.append(payment)
        
        self._per_symbol_totals[symbol] = self._per_symbol_totals.get(symbol, 0.0) + amount_usd
        
        logger.info("funding_payment_tracked", 
                    symbol=symbol, 
                    amount=amount_usd, 
                    total=self._per_symbol_totals[symbol])

    def get_total_funding(self, symbol: Optional[str] = None) -> float:
        """Returns total funding paid/received. Received is positive."""
        if symbol:
            return self._per_symbol_totals.get(symbol, 0.0)
        return sum(self._per_symbol_totals.values())

    def get_net_pnl(self, symbol: str, gross_pnl: float) -> float:
        """
        Calculates net P&L after funding costs.
        net_pnl = gross_pnl + funding_paid (where funding_paid is negative if paying)
        """
        funding = self.get_total_funding(symbol)
        return gross_pnl + funding

# Singleton instance for the session
funding_tracker = FundingTracker()
