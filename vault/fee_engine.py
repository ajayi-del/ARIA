import structlog
from datetime import datetime, timezone
from typing import Dict

logger = structlog.get_logger(__name__)

class FeeEngine:
    """
    Calculates performance and management fees.
    Performance: 20% of profit above High Water Mark.
    Management: 2% annual (accrued hourly).
    """
    
    def __init__(self, performance_fee_pct: float = 0.20, management_fee_annual_pct: float = 0.02):
        self.performance_fee_pct = performance_fee_pct
        self.management_fee_annual_pct = management_fee_annual_pct
        
    def compute_performance_fee(self, current_nav: float, high_water_mark: float) -> float:
        """
        20% of profit above high water mark.
        """
        if current_nav > high_water_mark:
            profit = current_nav - high_water_mark
            return profit * self.performance_fee_pct
        return 0.0

    def compute_management_fee(self, vault_value: float, hours_since_last: int = 1) -> float:
        """
        2% annual fee, accrued hourly.
        fee = value * (0.02 / 8760) * hours
        """
        hourly_rate = self.management_fee_annual_pct / 8760
        return vault_value * hourly_rate * hours_since_last

    def process_vault_fees(self, current_nav: float, high_water_mark: float) -> Dict[str, float]:
        """
        Helper to return all due fees.
        """
        perf_fee = self.compute_performance_fee(current_nav, high_water_mark)
        mgmt_fee = self.compute_management_fee(current_nav)
        
        return {
            "performance_fee": perf_fee,
            "management_fee": mgmt_fee,
            "total_fees": perf_fee + mgmt_fee
        }
