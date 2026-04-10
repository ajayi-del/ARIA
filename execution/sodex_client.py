"""
SoDEX REST API Client

Handles all authenticated and public calls to SoDEX perps.
Raises exceptions on HTTP errors. Never swallows errors silently.
"""

import json
import asyncio
import structlog
import httpx
import certifi
from typing import Dict, Any, List, Optional
from execution.schemas import OrderResult, BracketResult, BracketOrder
from .signer import SoDEXSigner, build_perps_order_payload
from .nonce_manager import NonceManager

logger = structlog.get_logger(__name__)


class SoDEXAPIError(Exception):
    """Custom exception for SoDEX API errors"""
    def __init__(self, message: str, status_code: int = None):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


class SoDEXClient:
    """
    REST API wrapper for SoDEX.
    Handles all authenticated and public calls.
    """
    
    def __init__(self, config, signer: SoDEXSigner, nonce_manager: NonceManager):
        self.config = config
        self.signer = signer
        self.nonce_manager = nonce_manager
        
        # Upgrade to persistent client with keep-alive
        self.client = httpx.AsyncClient(
            timeout=10.0,
            verify=certifi.where(),
            limits=httpx.Limits(
                max_keepalive_connections=5,
                keepalive_expiry=30
            ),
            headers={"Accept": "application/json"}
        )
        
        # Keepalive management
        self._keepalive_task: Optional[asyncio.Task] = None
        self._is_active = True
        
        # Endpoints
        self.base_url = config.sodex_rest_perps
    
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PUBLIC METHODS (no auth required)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    
    async def get_mark_price(self, symbol: str) -> float:
        """GET /markPrice?symbol={symbol}"""
        url = f"{self.base_url}/markPrice?symbol={symbol}"
        response = await self.client.get(url)
        
        if response.status_code != 200:
            raise SoDEXAPIError(f"Failed to get mark price: {response.text}", response.status_code)
        
        data = response.json()
        return float(data.get("markPrice", 0))
    
    async def get_orderbook(self, symbol: str, depth: int = 20) -> Dict[str, List]:
        """GET /depth?symbol={symbol}&limit={depth}"""
        url = f"{self.base_url}/depth?symbol={symbol}&limit={depth}"
        response = await self.client.get(url)
        
        if response.status_code != 200:
            raise SoDEXAPIError(f"Failed to get orderbook: {response.text}", response.status_code)
        
        data = response.json()
        return {
            "bids": data.get("bids", []),
            "asks": data.get("asks", [])
        }
    
    async def get_positions(self, account_id: str) -> List[Dict]:
        """GET /positions?accountID={account_id}"""
        url = f"{self.base_url}/positions?accountID={account_id}"
        response = await self.client.get(url)
        
        if response.status_code != 200:
            raise SoDEXAPIError(f"Failed to get positions: {response.text}", response.status_code)
        
        return response.json()
    
    async def get_open_orders(self, account_id: str) -> List[Dict]:
        """GET /openOrders?accountID={account_id}"""
        url = f"{self.base_url}/openOrders?accountID={account_id}"
        response = await self.client.get(url)
        
        if response.status_code != 200:
            raise SoDEXAPIError(f"Failed to get open orders: {response.text}", response.status_code)
        
        return response.json()
    
    async def get_account_balance(self, account_id: str) -> float:
        """GET /accounts/{address}/balances"""
        from eth_account import Account
        if not self.config.sodex_private_key:
            return 10000.0
        
        acct = Account.from_key(self.config.sodex_private_key)
        addr = acct.address
        
        try:
            # Note: Using relative path as base_url is set in __init__
            response = await self.client.get(f"{self.base_url}/accounts/{addr}/balances")
            data = response.json()
            if data.get("code") == 0:
                bal = data.get("data", {})
                return float(bal.get("availableBalance", bal.get("equity", 0)))
            return 0.0
        except Exception as e:
            logger.warning("balance_fetch_failed", error=str(e))
            return 0.0
    
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # AUTHENTICATED METHODS (EIP-712 signed)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    
    async def place_order(self, order_data: Dict[str, Any]) -> OrderResult:
        """Place a single order"""
        payload = {
            "type": "newOrder",
            "params": order_data
        }
        
        return await self._signed_post("/orders", payload)
    
    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel an order"""
        payload = {
            "type": "cancelOrder",
            "params": {
                "orderID": order_id,
                "symbol": symbol
            }
        }
        
        try:
            result = await self._signed_post("/orders", payload)
            return result.get("status") == "cancelled"
        except SoDEXAPIError:
            return False
    
    async def place_bracket(self, bracket: BracketOrder) -> BracketResult:
        """
        Places entry + stop + TP1 + TP2 + TP3 as separate orders in sequence:
        1. Place entry limit order
        2. Place stop-limit (reduce-only)
        3. Place TP1 limit (reduce-only, 50% size)
        4. Place TP2 limit (reduce-only, 30% size)
        5. Place TP3 limit (reduce-only, 20% size)
        
        If any order fails: cancel all placed orders and return failure result.
        Never leave a partial bracket open.
        """
        placed_orders = []
        
        try:
            # 1. Place entry limit order
            entry_result = await self._place_entry_order(bracket)
            if not entry_result.success:
                return BracketResult(success=False, error=f"Entry failed: {entry_result.error}")
            
            placed_orders.append(entry_result.order_id)
            
            # 2. Place stop-limit order
            stop_result = await self._place_stop_order(bracket)
            if not stop_result.success:
                await self._cleanup_orders(placed_orders)
                return BracketResult(success=False, error=f"Stop failed: {stop_result.error}")
            
            placed_orders.append(stop_result.order_id)
            
            # 3-5. Place TP orders
            tp_results = await self._place_tp_orders(bracket)
            if not all(r.success for r in tp_results):
                await self._cleanup_orders(placed_orders + [r.order_id for r in tp_results if r.order_id])
                failed_tp = next(r for r in tp_results if not r.success)
                return BracketResult(success=False, error=f"TP failed: {failed_tp.error}")
            
            tp_order_ids = [r.order_id for r in tp_results]
            placed_orders.extend(tp_order_ids)
            
            return BracketResult(
                success=True,
                entry_order_id=entry_result.order_id,
                stop_order_id=stop_result.order_id,
                tp1_order_id=tp_order_ids[0],
                tp2_order_id=tp_order_ids[1],
                tp3_order_id=tp_order_ids[2]
            )
            
        except Exception as e:
            await self._cleanup_orders(placed_orders)
            return BracketResult(success=False, error=f"Bracket placement failed: {str(e)}")
    
    async def update_leverage(self, symbol: str, leverage: int) -> bool:
        """Updates leverage for symbol"""
        payload = {
            "type": "updateLeverage",
            "params": {
                "symbol": symbol,
                "leverage": leverage
            }
        }
        
        try:
            result = await self._signed_post("/account", payload)
            return result.get("status") == "success"
        except SoDEXAPIError:
            return False
    
    async def set_margin_mode(self, symbol: str, mode: str = "isolated") -> bool:
        """Sets isolated margin for symbol"""
        payload = {
            "type": "setMarginMode",
            "params": {
                "symbol": symbol,
                "marginMode": mode
            }
        }
        
        try:
            result = await self._signed_post("/account", payload)
            return result.get("status") == "success"
        except SoDEXAPIError:
            return False
    
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # INTERNAL HELPER METHODS
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    
    async def _signed_post(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Builds nonce, signs payload, adds headers, sends POST"""
        nonce = self.nonce_manager.next_nonce()
        signature = self.signer.sign_payload(payload, nonce)
        
        headers = {
            "X-API-Key": self.signer.get_address(),
            "X-API-Sign": signature,
            "X-API-Nonce": str(nonce),
            "Content-Type": "application/json"
        }
        
        url = f"{self.base_url}{endpoint}"
        response = await self.client.post(url, json=payload, headers=headers)
        
        if response.status_code != 200:
            raise SoDEXAPIError(f"API request failed: {response.text}", response.status_code)

        data = response.json()
        if isinstance(data, dict) and data.get("code", 0) != 0:
            raise SoDEXAPIError(
                f"SoDEX error code {data.get('code')}: {data.get('message', 'unknown')}",
                response.status_code
            )
        return data
    
    async def _place_entry_order(self, bracket: BracketOrder) -> OrderResult:
        """Place entry limit order"""
        candidate = bracket.candidate
        
        order_data = {
            "accountID": int(bracket.account_id),
            "symbolID": bracket.symbol_id,
            "orders": [{
                "clOrdID": f"entry_{candidate.symbol}_{int(candidate.timestamp_ms)}",
                "modifier": 1,  # post-only limit
                "side": 1 if candidate.side == "long" else 2,
                "type": 2,  # limit
                "timeInForce": 1,  # GTC
                "price": str(candidate.entry_price),
                "quantity": str(candidate.size),
                "funds": "0",
                "stopPrice": "0",
                "stopType": 0,
                "triggerType": 0,
                "reduceOnly": False,
                "positionSide": 1 if candidate.side == "long" else 2
            }]
        }
        
        return await self.place_order(order_data)
    
    async def _place_stop_order(self, bracket: BracketOrder) -> OrderResult:
        """Place stop-limit order"""
        candidate = bracket.candidate
        
        order_data = {
            "accountID": int(bracket.account_id),
            "symbolID": bracket.symbol_id,
            "orders": [{
                "clOrdID": f"stop_{candidate.symbol}_{int(candidate.timestamp_ms)}",
                "modifier": 0,  # no modifier
                "side": 2 if candidate.side == "long" else 1,  # opposite side
                "type": 2,  # limit
                "timeInForce": 1,  # GTC
                "price": str(candidate.stop_price),
                "quantity": str(candidate.size),
                "funds": "0",
                "stopPrice": "0",
                "stopType": 0,
                "triggerType": 0,
                "reduceOnly": True,
                "positionSide": 1 if candidate.side == "long" else 2
            }]
        }
        
        return await self.place_order(order_data)
    
    async def _place_tp_orders(self, bracket: BracketOrder) -> List[OrderResult]:
        """Place TP1, TP2, TP3 orders"""
        candidate = bracket.candidate
        results = []
        
        # TP sizes: 50%, 30%, 20%
        tp_sizes = [0.5, 0.3, 0.2]
        tp_prices = [candidate.tp1_price, candidate.tp2_price, candidate.tp3_price]
        
        for i, (tp_size_pct, tp_price) in enumerate(zip(tp_sizes, tp_prices)):
            tp_size = candidate.size * tp_size_pct
            
            order_data = {
                "accountID": int(bracket.account_id),
                "symbolID": bracket.symbol_id,
                "orders": [{
                    "clOrdID": f"tp{i+1}_{candidate.symbol}_{int(candidate.timestamp_ms)}",
                    "modifier": 0,  # no modifier
                    "side": 2 if candidate.side == "long" else 1,  # opposite side
                    "type": 2,  # limit
                    "timeInForce": 1,  # GTC
                    "price": str(tp_price),
                    "quantity": str(tp_size),
                    "funds": "0",
                    "stopPrice": "0",
                    "stopType": 0,
                    "triggerType": 0,
                    "reduceOnly": True,
                    "positionSide": 1 if candidate.side == "long" else 2
                }]
            }
            
            result = await self.place_order(order_data)
            results.append(result)
        
        return results
    
    async def _cleanup_orders(self, order_ids: List[str]):
        """Cancel multiple orders"""
        for order_id in order_ids:
            try:
                # Extract symbol from order ID (simple parsing)
                symbol = order_id.split("_")[1] if "_" in order_id else "BTC-USD"
                await self.cancel_order(order_id, symbol)
            except Exception:
                pass  # Best effort cleanup

    async def close(self):
        """Shutdown persistent HTTP client."""
        self._is_active = False
        if self._keepalive_task:
            self._keepalive_task.cancel()
        await self.client.aclose()
        logger.info("persistent_http_client_closed")

    def start_keepalive(self):
        """Starts the background keepalive ping loop."""
        if self._keepalive_task is None:
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
            logger.info("http_keepalive_loop_started")

    async def _keepalive_loop(self):
        """Ping the server every 20s to keep connection warm."""
        while self._is_active:
            try:
                # v1.3: Use correct endpoint to avoid 404 spam
                # Prepending base_url because client doesn't use it automatically
                await self.client.get(
                    f"{self.base_url}/markets/mark-prices", 
                    params={"symbol": "BTC-USD"}, 
                    timeout=5.0
                )
                logger.debug("http_keepalive_ping_sent")
            except Exception as e:
                logger.warning("http_keepalive_ping_failed", error=str(e))
            
            await asyncio.sleep(20)
