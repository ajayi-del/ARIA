import time
import random
import asyncio
import structlog
from typing import Dict, List, Optional, Any
from execution.schemas import OrderResult, BracketResult, BracketOrder, Position, OrderRecord
from core.event_bus import event_bus, EventType, Event

logger = structlog.get_logger(__name__)

class PaperClient:
    """
    Full paper trading simulation.
    Same interface as SoDEXClient.
    Zero network calls. Runs fully offline.
    """
    
    def __init__(self, config, starting_balance: float = 200.0):
        self.config = config
        self._balance = starting_balance
        self._positions: Dict[str, List[Position]] = {}
        self._open_orders: Dict[str, OrderRecord] = {}
        self._filled_orders: List[OrderRecord] = []
        self._order_counter = 0
        self._synthetic_prices = {}
        self._events = []  # Phase 6: Event queue for alerts
        self.base_url = "http://paper-trading.aria"
        
        # v1.3 Event-driven fills
        event_bus.subscribe(EventType.MARK_PRICE_UPDATED, self._on_mark_price_updated)
        
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # IMPLEMENT ALL SoDEXClient PUBLIC METHODS
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    
    async def get_mark_price(self, symbol: str) -> float:
        """GET /markPrice?symbol={symbol}"""
        return self._synthetic_prices.get(symbol, 71000.0 if symbol == "BTC-USD" else 3000.0)
    
    async def get_orderbook(self, symbol: str, depth: int = 20) -> Dict[str, List]:
        """GET /depth?symbol={symbol}&limit={depth}"""
        price = await self.get_mark_price(symbol)
        spread = price * 0.0005  # 0.05% spread
        
        bids = []
        asks = []
        for i in range(depth):
            bid_price = price - spread * (i + 1)
            ask_price = price + spread * (i + 1)
            bids.append([bid_price, random.uniform(0.1, 5.0)])
            asks.append([ask_price, random.uniform(0.1, 5.0)])
        
        return {"bids": bids, "asks": asks}
    
    async def get_positions(self, account_id: str) -> List[Dict]:
        """GET /positions?accountID={account_id}"""
        positions = []
        for symbol, pos_list in self._positions.items():
            for pos in pos_list:
                positions.append({
                    "symbol": symbol,
                    "side": pos.side,
                    "size": pos.size,
                    "entryPrice": pos.entry_price,
                    "stopPrice": pos.stop_price,
                    "unrealizedPnl": self._calculate_pnl(pos)
                })
        return positions
    
    async def get_open_orders(self, account_id: str) -> List[Dict]:
        """GET /openOrders?accountID={account_id}"""
        orders = []
        for order in self._open_orders.values():
            orders.append({
                "orderID": order.order_id,
                "symbol": order.symbol,
                "side": order.side,
                "type": order.order_type,
                "price": order.price,
                "quantity": order.size,
                "status": order.status
            })
        return orders
    
    async def get_account_balance(self, account_id: str) -> float:
        """GET /balance?accountID={account_id}"""
        return self._balance
    
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # IMPLEMENT ALL SoDEXClient AUTHENTICATED METHODS
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    
    async def place_order(self, order_data: Dict[str, Any] = None, **kwargs) -> OrderResult:
        """Place an order (Market or Limit) - v1.3.FIXED"""
        logger.info("paper_place_order", data=order_data, kwargs=kwargs, version="v1.3.FIXED")
        self._order_counter += 1
        order_id = f"paper_{self._order_counter}"
        
        # Merge order_data and kwargs for robustness
        data = order_data or {}
        data.update(kwargs)
        
        # Extract order details
        orders = data.get("orders", [])
        if not orders and "symbol" in data:
            # Handle flattened keyword argument call style
            order_info = data
        elif orders:
            order_info = orders[0]
        else:
            return OrderResult(order_id="", status="rejected", error="No orders in payload")
        
        symbol = order_info.get("symbol", "BTC-USD")
        side_val = order_info.get("side")
        # Map string sides if provided
        if isinstance(side_val, str):
            side = 1 if side_val.lower() in ("buy", "long") else 2
        else:
            side = int(side_val or 1)

        order_type = int(order_info.get("type", 1)) # Default to limit (1); 2=market per SoDEX schema
        size = float(order_info.get("quantity", order_info.get("size", 0)))
        
        if size <= 0:
            return OrderResult(order_id=order_id, status="rejected", error="Invalid size")

        current_price = await self.get_mark_price(symbol)
        
        # Immediate fill for Market (2) or Limit (1) if price is right — 2=market per SoDEX schema
        is_market = (order_type == 2)
        order_price = float(order_info.get("price", current_price))
        
        if is_market or (side == 1 and current_price <= order_price) or (side == 2 and current_price >= order_price):
            fill_price = current_price if is_market else order_price
            
            # Simulate slippage for market orders
            if is_market:
                slippage = random.uniform(0.0001, 0.0010) # 0.01-0.1%
                fill_price = fill_price * (1 + slippage) if side == 1 else fill_price * (1 - slippage)

            fill_qty = size
            
            # Create order record
            order = OrderRecord(
                order_id=order_id,
                client_id=str(order_info.get("clOrdID", f"cl_{order_id}")),
                symbol=symbol,
                side=side,
                order_type="market" if is_market else "limit",
                price=order_price,
                size=size,
                status="filled",
                fill_price=fill_price,
                fill_qty=fill_qty,
                placed_at_ms=int(time.time() * 1000),
                filled_at_ms=int(time.time() * 1000),
                position_ref=None
            )
            
            # Update position
            await self._update_position(symbol, side, fill_price, fill_qty, order_info)
            
            self._open_orders[order_id] = order
            self._filled_orders.append(order)
            
            return OrderResult(
                order_id=order_id,
                status="filled",
                fill_price=fill_price,
                fill_qty=fill_qty,
                error=None
            )
        
        return OrderResult(order_id=order_id, status="rejected", fill_price=0.0, fill_qty=0.0, error="Paper client only supports immediate fills currently")
    
    async def cancel_order(self, order_id: str, symbol: str, account_id: str = "") -> bool:
        """Cancel an order"""
        if order_id in self._open_orders:
            order = self._open_orders[order_id]
            order.status = "cancelled"
            del self._open_orders[order_id]
            return True
        return False
    
    async def place_bracket(self, bracket: BracketOrder) -> BracketResult:
        """
        Places entry + stop + TP1 + TP2 + TP3 as separate orders in sequence.
        """
        candidate = bracket.candidate
        
        try:
            # 1. Place entry limit order
            entry_result = await self.place_order({
                "accountID": int(bracket.account_id),
                "symbolID": bracket.symbol_id,
                "orders": [{
                    "clOrdID": f"entry_{candidate.symbol}_{int(candidate.timestamp_ms)}",
                    "modifier": 1,  # post-only limit
                    "side": 1 if candidate.side == "long" else 2,
                    "type": 1,  # limit (1=limit, 2=market per SoDEX schema)
                    "timeInForce": 1,  # GTC
                    "price": str(candidate.entry_price),
                    "quantity": str(candidate.size),
                    "funds": "0",
                    "stopPrice": "0",
                    "stopType": 0,
                    "triggerType": 0,
                    "reduceOnly": False,
                    "positionSide": 1  # SoDEX one-way mode: always BOTH (1)
                }]
            })

            if entry_result.status != "filled":
                return BracketResult(success=False, error=f"Entry failed: {entry_result.error}")
            
            # Inject actual TP/stop prices from candidate into the just-created position
            sym = candidate.symbol
            if sym in self._positions and self._positions[sym]:
                pos = self._positions[sym][-1]
                pos.stop_price = candidate.stop_price
                pos.tp1_price = candidate.tp1_price
                pos.tp2_price = candidate.tp2_price
                pos.tp3_price = candidate.tp3_price

            # Create pending stop/TP order IDs (simulated — triggers handled via price events)
            ts = int(candidate.timestamp_ms)
            stop_order_id = f"stop_{sym}_{ts}"
            tp1_order_id  = f"tp1_{sym}_{ts}"
            tp2_order_id  = f"tp2_{sym}_{ts}"
            tp3_order_id  = f"tp3_{sym}_{ts}"

            return BracketResult(
                success=True,
                entry_order_id=entry_result.order_id,
                stop_order_id=stop_order_id,
                tp1_order_id=tp1_order_id,
                tp2_order_id=tp2_order_id,
                tp3_order_id=tp3_order_id
            )
            
        except Exception as e:
            return BracketResult(success=False, error=f"Bracket placement failed: {str(e)}")
    
    async def update_leverage(self, symbol: str, leverage: int) -> bool:
        """Updates leverage for symbol"""
        # Paper client doesn't enforce leverage
        return True
    
    async def set_margin_mode(self, symbol: str, mode: str = "isolated") -> bool:
        """Sets isolated margin for symbol"""
        # Paper client always uses isolated margin
        return True
    
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PAPER CLIENT SPECIFIC METHODS
    def get_events(self) -> List[Dict]:
        """Phase 6: Returns and clears event queue"""
        evs = self._events.copy()
        self._events.clear()
        return evs

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    
    async def _on_mark_price_updated(self, event: Event):
        """Event-driven fill trigger. Only checks the symbol being updated."""
        symbol = event.symbol
        price = event.data.get("mark_price")
        if not price:
            return
            
        self._synthetic_prices[symbol] = price
        
        if symbol in self._positions:
            for pos in self._positions[symbol]:
                await self._check_bracket_triggers(symbol, pos, price)

    async def update_fills(self, price_updates: Dict[str, float]):
        """Legacy shim for backward compatibility with existing loops."""
        for symbol, price in price_updates.items():
            self._synthetic_prices[symbol] = price
            if symbol in self._positions:
                for pos in self._positions[symbol]:
                    await self._check_bracket_triggers(symbol, pos, price)
    
    async def _check_bracket_triggers(self, symbol: str, position: Position, current_price: float):
        """Check if any TP or stop levels are hit"""
        if position.side == "long":
            # Long position: TP above entry, stop below
            tp_hit = current_price >= position.tp1_price
            stop_hit = current_price <= position.stop_price
        else:
            # Short position: TP below entry, stop above
            tp_hit = current_price <= position.tp1_price
            stop_hit = current_price >= position.stop_price
        
        # Mark TP1 as hit
        if tp_hit and not position.tp1_hit:
            position.tp1_hit = True
            self._events.append({
                "type": "tp1_hit",
                "symbol": symbol
            })
            
        if position.tp1_hit and not position.stop_moved:
            # Golden stop: entry + 0.5 × (TP1 - entry) for longs, mirrored for shorts
            if position.side == "long":
                golden_stop = position.entry_price + 0.5 * (position.tp1_price - position.entry_price)
                position.stop_price = max(golden_stop, position.entry_price)
            else:
                golden_stop = position.entry_price - 0.5 * (position.entry_price - position.tp1_price)
                position.stop_price = min(golden_stop, position.entry_price)
            position.golden_stop_used = True
            position.stop_moved = True

        # Check for full exit (stop hit or TP3 hit)
        if stop_hit:
             self._close_position(symbol, position, current_price, "stop_out")
        elif position.side == "long" and current_price >= position.tp3_price:
             self._close_position(symbol, position, current_price, "target_hit")
        elif position.side == "short" and current_price <= position.tp3_price:
             self._close_position(symbol, position, current_price, "target_hit")
    
    async def _update_position(self, symbol: str, side: int, fill_price: float, fill_qty: float, order_info: Dict[str, float]):
        """Updates position tracking"""
        if symbol not in self._positions:
            self._positions[symbol] = []
        
        # Determine position side
        pos_side = "long" if side == 1 else "short"
        
        # Calculate initial margin
        notional = fill_qty * fill_price
        leverage = order_info.get("leverage", 10)
        initial_margin = notional / leverage
        
        # Create position
        position = Position(
            symbol=symbol,
            side=pos_side,
            entry_price=fill_price,
            size=fill_qty,
            stop_price=float(order_info.get("stopPrice", 0)),
            tp1_price=fill_price * (1.1 if pos_side == "long" else 0.9),  # 10% TP
            tp2_price=fill_price * (1.2 if pos_side == "long" else 0.8),  # 20% TP
            tp3_price=fill_price * (1.3 if pos_side == "long" else 0.7),  # 30% TP
            liq_price=fill_price * (0.9 if pos_side == "long" else 1.1),  # 10% liq
            initial_margin=initial_margin,
            leverage=leverage,
            opened_at_ms=int(time.time() * 1000)
        )
        
        self._positions[symbol].append(position)
        
        # Deduct margin from balance
        self._balance -= initial_margin
    
    def _calculate_pnl(self, position: Position) -> float:
        """Calculate unrealized P&L for position"""
        current_price = self._synthetic_prices.get(position.symbol, position.entry_price)
        
        if position.side == "long":
            return (current_price - position.entry_price) * position.size
        else:
            return (position.entry_price - current_price) * position.size

    def _close_position(self, symbol: str, position: Position, price: float, reason: str):
        """Phase 6: Handle position closure"""
        pnl = self._calculate_pnl(position)
        self._balance += (position.initial_margin + pnl)
        
        # Remove from positions
        if symbol in self._positions:
            self._positions[symbol].remove(position)
            
        self._events.append({
            "type": "trade_closed",
            "symbol": symbol,
            "outcome": reason,
            "pnl": pnl,
            "r_multiple": pnl / position.initial_margin if position.initial_margin > 0 else 0
        })
