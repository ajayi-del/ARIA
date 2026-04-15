"""
Order Manager

Tracks lifecycle of every order.
Links orders to their parent positions.
Monitors fills via account WebSocket updates.
"""

import structlog
from typing import List, Dict, Optional
from execution.schemas import OrderRecord, Position

logger = structlog.get_logger(__name__)


class OrderManager:
    """
    Tracks lifecycle of every order.
    Links orders to their parent positions.
    """
    
    def __init__(self):
        self._orders: Dict[str, OrderRecord] = {}
        self._order_counter = 0
    
    def track(self, order: OrderRecord) -> None:
        """Track a new order"""
        self._orders[order.order_id] = order
        self._order_counter += 1
    
    def update_fill(self, order_id: str, fill_price: float, fill_qty: float, ts: int) -> None:
        """Update order with fill information"""
        if order_id in self._orders:
            order = self._orders[order_id]
            order.fill_price = fill_price
            order.fill_qty = fill_qty
            order.filled_at_ms = ts
            order.status = "filled"
    
    def cancel(self, order_id: str) -> None:
        """Mark order as cancelled"""
        if order_id in self._orders:
            self._orders[order_id].status = "cancelled"
    
    def get_open(self) -> List[OrderRecord]:
        """Get all open orders"""
        return [order for order in self._orders.values() if order.status in ["pending", "open"]]
    
    def get_filled(self) -> List[OrderRecord]:
        """Get all filled orders"""
        return [order for order in self._orders.values() if order.status == "filled"]
    
    def summary(self) -> Dict[str, int]:
        """Get order summary for terminal display"""
        total = len(self._orders)
        pending = len([o for o in self._orders.values() if o.status == "pending"])
        open_orders = len([o for o in self._orders.values() if o.status == "open"])
        filled = len([o for o in self._orders.values() if o.status == "filled"])
        cancelled = len([o for o in self._orders.values() if o.status == "cancelled"])
        
        return {
            "total": total,
            "pending": pending,
            "open": open_orders,
            "filled": filled,
            "cancelled": cancelled
        }
    
    def on_tp1_fill(self, symbol: str) -> None:
        """
        Called when TP1 order fills.
        Notifies PositionManager to mark tp1_hit.
        """
        logger.info("tp1_filled", symbol=symbol)
    
    def get_by_client_id(self, client_id: str) -> Optional[OrderRecord]:
        """Find order by client ID"""
        for order in self._orders.values():
            if order.client_id == client_id:
                return order
        return None
