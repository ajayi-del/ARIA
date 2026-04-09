"""
ARIA Execution Layer

Handles order placement, risk management, and position tracking
for SoDEX perps trading with EIP-712 signing.
"""

from execution.schemas import (
    TradeCandidate,
    BracketOrder,
    BracketResult,
    OrderResult,
    Position,
    OrderRecord,
    PerpsOrderItem
)
from .nonce_manager import NonceManager
from .signer import SoDEXSigner
from .sodex_client import SoDEXClient
from .paper_client import PaperClient
from .order_manager import OrderManager

__all__ = [
    "TradeCandidate",
    "BracketOrder", 
    "BracketResult",
    "OrderResult",
    "Position",
    "OrderRecord",
    "PerpsOrderItem",
    "NonceManager",
    "SoDEXSigner",
    "SoDEXClient",
    "PaperClient",
    "OrderManager"
]
