"""
Execution layer schemas and data structures
"""

from dataclasses import dataclass
from typing import Literal, Optional


@dataclass
class TradeCandidate:
    symbol: str
    side: Literal["long", "short"]
    entry_price: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    tp3_price: float
    size: float
    initial_margin: float
    leverage: int
    rr_ratio: float
    coherence_score: float
    size_multiplier: float
    signal_reason: str
    invalidation: str
    timestamp_ms: int
    signal_age_ms: int = 0
    atr: float = 0.0
    atr_ratio: float = 1.0
    liq_price: float = 0.0


@dataclass
class BracketOrder:
    candidate: TradeCandidate
    account_id: str
    symbol_id: int


@dataclass
class BracketResult:
    success: bool
    entry_order_id: Optional[str] = None
    stop_order_id: Optional[str] = None
    tp1_order_id: Optional[str] = None
    tp2_order_id: Optional[str] = None
    tp3_order_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class OrderResult:
    order_id: str
    status: str
    fill_price: Optional[float] = None
    fill_qty: Optional[float] = None
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        """True when the order was placed without error."""
        return self.error is None


@dataclass
class Position:
    symbol: str
    side: Literal["long", "short"]
    entry_price: float
    size: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    tp3_price: float
    liq_price: float
    initial_margin: float
    leverage: int
    opened_at_ms: int
    order_ids: dict = None        # {entry, stop, tp1, tp2, tp3} order IDs
    tp1_hit: bool = False
    tp2_hit: bool = False
    stop_moved: bool = False
    golden_stop_used: bool = False
    tp1_level_stop_used: bool = False
    atr: float = 0.0             # ATR at entry — used for trailing stop distance
    initial_size: float = 0.0    # Original size at entry — used for TP1/TP2 detection


@dataclass
class OrderRecord:
    order_id: str
    client_id: str  # clOrdID
    symbol: str
    side: int
    order_type: str  # entry/stop/tp1/tp2/tp3
    price: float
    size: float
    status: str  # pending/open/filled/cancelled/rejected
    fill_price: Optional[float]
    fill_qty: Optional[float]
    placed_at_ms: int
    filled_at_ms: Optional[int]
    position_ref: Optional[Position]


# SoDEX Perps Order Item Schema
# Optional fields (funds, stopPrice, stopType, triggerType) must be OMITTED
# when not applicable — they are omitempty in Go struct. Sending them as 0/"0"
# causes "stopType is invalid" rejection (confirmed live 2026-04-12).
@dataclass
class PerpsOrderItem:
    clOrdID: str       # Client order ID
    modifier: int       # 1=NORMAL — always 1 for standard orders
    side: int          # 1=buy, 2=sell
    type: int          # 1=limit, 2=market
    timeInForce: int   # 1=GTC, 3=IOC
    price: str         # DecimalString; omit for MARKET orders
    quantity: str      # DecimalString
    reduceOnly: bool
    positionSide: int  # 1=BOTH — SoDEX only supports oneway mode
    # funds, stopPrice, stopType, triggerType: omit unless explicitly needed
