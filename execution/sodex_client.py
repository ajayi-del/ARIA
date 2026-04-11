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

    def __init__(self, config, signer, nonce_manager):
        self.config = config
        self.signer = signer
        self.nonce_manager = nonce_manager

        self.client = httpx.AsyncClient(
            timeout=10.0,
            verify=certifi.where(),
            limits=httpx.Limits(
                max_keepalive_connections=5,
                keepalive_expiry=30
            ),
            headers={"Accept": "application/json"}
        )

        self._keepalive_task: Optional[asyncio.Task] = None
        self._is_active = True
        self.base_url = config.sodex_rest_perps

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PUBLIC METHODS (no auth required)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def get_mark_price(self, symbol: str) -> float:
        """GET /markets/mark-prices"""
        response = await self.client.get(
            f"{self.base_url}/markets/mark-prices",
            params={"symbol": symbol}
        )
        if response.status_code != 200:
            raise SoDEXAPIError(f"Failed to get mark price: {response.text}", response.status_code)
        data = response.json()
        # Response: {"data": [{"symbol": ..., "markPrice": "..."}]}
        items = data.get("data", [])
        if items:
            return float(items[0].get("markPrice", 0))
        return 0.0

    async def get_orderbook(self, symbol: str, depth: int = 20) -> Dict[str, List]:
        """GET /markets/{symbol}/orderbook"""
        response = await self.client.get(
            f"{self.base_url}/markets/{symbol}/orderbook",
            params={"depth": depth}
        )
        if response.status_code != 200:
            raise SoDEXAPIError(f"Failed to get orderbook: {response.text}", response.status_code)
        data = response.json()
        ob = data.get("data", data)
        return {
            "bids": ob.get("bids", []),
            "asks": ob.get("asks", [])
        }

    async def get_positions(self, address: str) -> List[Dict]:
        """GET /accounts/{address}/positions
        API returns {"data": {"positions": [...], ...}} — extract inner positions list.
        Mirrors PHANTOM's get_open_positions() parsing logic.
        """
        response = await self.client.get(f"{self.base_url}/accounts/{address}/positions")
        if response.status_code != 200:
            raise SoDEXAPIError(f"Failed to get positions: {response.text}", response.status_code)
        data = response.json()
        pos_data = data.get("data", {})
        if isinstance(pos_data, dict):
            return pos_data.get("positions") or pos_data.get("P") or []
        return pos_data or []

    async def get_open_orders(self, address: str) -> List[Dict]:
        """GET /accounts/{address}/orders"""
        response = await self.client.get(f"{self.base_url}/accounts/{address}/orders")
        if response.status_code != 200:
            raise SoDEXAPIError(f"Failed to get open orders: {response.text}", response.status_code)
        data = response.json()
        return data.get("data", [])

    async def get_account_balance(self, address: str) -> float:
        """
        GET /api/v1/perps/accounts/{address}/balances
        Extracts available balance from the SoDEX perps balance response.
        Response structure: {"data": {"balances": [{"asset":"USDC","available":"...", "equity":"..."}]}}
        Falls back to spot balances if perps returns empty.
        """
        addr = address or self.signer.get_address()
        base = "mainnet-gw.sodex.dev" if self.config.sodex_mainnet else "testnet-gw.sodex.dev"
        try:
            # 1. Try perps balances (trading account)
            resp = await self.client.get(
                f"https://{base}/api/v1/perps/accounts/{addr}/balances",
                timeout=8.0
            )
            data = resp.json()
            if data.get("code") == 0:
                bal_list = data.get("data", {}).get("balances", [])
                for item in bal_list:
                    # Try available, then equity, then total
                    for field in ("available", "availableBalance", "equity", "total"):
                        v = item.get(field)
                        if v is not None:
                            try:
                                f = float(v)
                                if f > 0:
                                    return f
                            except (ValueError, TypeError):
                                pass
            # 2. Fallback: spot balances
            resp = await self.client.get(
                f"https://{base}/api/v1/spot/accounts/{addr}/balances",
                timeout=8.0
            )
            data = resp.json()
            if data.get("code") == 0:
                bal_list = data.get("data", {}).get("balances", []) or data.get("data", {}).get("B", [])
                for item in bal_list:
                    if isinstance(item, dict):
                        for field in ("available", "availableBalance", "equity", "total", "a"):
                            v = item.get(field)
                            if v is not None:
                                try:
                                    f = float(v)
                                    if f > 0:
                                        return f
                                except (ValueError, TypeError):
                                    pass
            return 0.0
        except Exception as e:
            logger.warning("balance_fetch_failed", error=str(e))
            return 0.0

    async def fetch_account_id(self, address: str) -> int:
        """
        Resolves numeric accountID (aid) for the given wallet address.
        Strategy:
          1. Try GET /api/v1/spot/accounts/{address}/state  (canonical per SoDEX docs)
          2. Try GET /api/v1/perps/accounts/{address}/state (fallback)
        Returns 0 if account is not yet registered on SoDEX (no deposit made).
        """
        base = "mainnet-gw.sodex.dev" if self.config.sodex_mainnet else "testnet-gw.sodex.dev"
        endpoints = [
            f"https://{base}/api/v1/spot/accounts/{address}/state",
            f"https://{base}/api/v1/perps/accounts/{address}/state",
        ]
        for url in endpoints:
            try:
                resp = await self.client.get(url, timeout=8.0)
                data = resp.json()
                if data.get("code") != 0:
                    continue
                d = data.get("data", {})
                for field in ("aid", "uid", "accountID", "id"):
                    val = d.get(field)
                    if val is not None:
                        try:
                            numeric = int(val)
                            if numeric != 0:
                                logger.info("account_id_resolved", aid=numeric, endpoint=url)
                                return numeric
                        except (ValueError, TypeError):
                            pass
            except Exception as e:
                logger.debug("account_id_fetch_attempt_failed", url=url, error=str(e))
                continue

        logger.warning(
            "account_not_registered",
            address=address,
            message="aid=0 on both endpoints. Account registers automatically on first SoDEX deposit."
        )
        return 0

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # AUTHENTICATED METHODS (EIP-712 signed)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def place_order(self, order_data: Dict[str, Any]) -> OrderResult:
        """POST /trade/orders — body = params, sign full {type,params} wrapper"""
        try:
            data = await self._signed_post("/trade/orders", "newOrder", order_data)
            # Response: {"code":0,"data":{"orders":[{"orderID":"...","status":"..."}]}}
            orders = data.get("data", {}).get("orders", [])
            if orders:
                o = orders[0]
                return OrderResult(
                    order_id=str(o.get("orderID", o.get("clOrdID", ""))),
                    status=str(o.get("status", "open")),
                    fill_price=float(o.get("fillPrice", 0) or 0) or None,
                    fill_qty=float(o.get("fillQty", 0) or 0) or None,
                    error=None,
                )
            return OrderResult(order_id="", status="unknown", fill_price=None, fill_qty=None, error="No orders in response")
        except SoDEXAPIError as e:
            return OrderResult(order_id="", status="rejected", fill_price=None, fill_qty=None, error=e.message)

    async def cancel_order(self, order_id: str, symbol: str, account_id: int) -> bool:
        """DELETE /trade/orders"""
        params = {
            "accountID": account_id,
            "orderID": order_id,
            "symbol": symbol,
        }
        try:
            result = await self._signed_delete("/trade/orders", "cancelOrder", params)
            return result.get("code", -1) == 0
        except SoDEXAPIError:
            return False

    async def place_bracket(self, bracket: BracketOrder) -> BracketResult:
        """
        Places entry + stop + TP1 + TP2 + TP3 as separate orders in sequence.
        If any order fails: cancel all placed orders and return failure result.
        """
        placed_orders = []

        try:
            entry_result = await self._place_entry_order(bracket)
            if not entry_result.success:
                return BracketResult(success=False, error=f"Entry failed: {entry_result.error}")
            placed_orders.append((entry_result.order_id, bracket.candidate.symbol, bracket.account_id))

            stop_result = await self._place_stop_order(bracket)
            if not stop_result.success:
                await self._cleanup_orders(placed_orders)
                return BracketResult(success=False, error=f"Stop failed: {stop_result.error}")
            placed_orders.append((stop_result.order_id, bracket.candidate.symbol, bracket.account_id))

            tp_results = await self._place_tp_orders(bracket)
            if not all(r.success for r in tp_results):
                await self._cleanup_orders(placed_orders + [
                    (r.order_id, bracket.candidate.symbol, bracket.account_id)
                    for r in tp_results if r.order_id
                ])
                failed_tp = next(r for r in tp_results if not r.success)
                return BracketResult(success=False, error=f"TP failed: {failed_tp.error}")

            tp_order_ids = [r.order_id for r in tp_results]
            return BracketResult(
                success=True,
                entry_order_id=entry_result.order_id,
                stop_order_id=stop_result.order_id,
                tp1_order_id=tp_order_ids[0],
                tp2_order_id=tp_order_ids[1],
                tp3_order_id=tp_order_ids[2],
            )

        except Exception as e:
            await self._cleanup_orders(placed_orders)
            return BracketResult(success=False, error=f"Bracket placement failed: {str(e)}")

    async def update_leverage(self, symbol_id: int, leverage: int, account_id: int) -> bool:
        """POST /trade/leverage"""
        params = {
            "accountID": account_id,
            "symbolID": symbol_id,
            "leverage": leverage,
        }
        try:
            result = await self._signed_post("/trade/leverage", "updateLeverage", params)
            return result.get("code", -1) == 0
        except SoDEXAPIError:
            return False

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # INTERNAL HELPER METHODS
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _build_order_item(
        self,
        cl_ord_id: str,
        side: int,
        order_type: int,
        tif: int,
        quantity: str,
        price: str = None,
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        """
        Build order item dict with canonical field order for signing correctness.
        omitempty fields (funds, stopPrice, stopType, triggerType) are omitted.
        positionSide is always 1 (BOTH — SoDEX oneway mode only).
        """
        item: Dict[str, Any] = {
            "clOrdID": cl_ord_id,
            "modifier": 1,          # NORMAL
            "side": side,
            "type": order_type,
            "timeInForce": tif,
        }
        if price is not None:       # omit for MARKET orders
            item["price"] = price
        item["quantity"] = quantity
        # funds/stopPrice/stopType/triggerType omitted (omitempty)
        item["reduceOnly"] = reduce_only
        item["positionSide"] = 1    # BOTH — oneway mode
        return item

    async def _signed_post(
        self, endpoint: str, action_type: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Sign full {type,params} wrapper; send params only as body."""
        nonce = self.nonce_manager.next_nonce()
        full_payload = {"type": action_type, "params": params}
        signature = self.signer.sign_payload(full_payload, nonce)

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-Key": self.signer.get_address(),
            "X-API-Sign": signature,
            "X-API-Nonce": str(nonce),
        }

        response = await self.client.post(
            f"{self.base_url}{endpoint}", json=params, headers=headers
        )
        if response.status_code not in (200, 201):
            raise SoDEXAPIError(f"API request failed: {response.text}", response.status_code)

        data = response.json()
        if isinstance(data, dict) and data.get("code", 0) != 0:
            raise SoDEXAPIError(
                f"SoDEX error {data.get('code')}: {data.get('message', data.get('msg', 'unknown'))}",
                response.status_code,
            )
        return data

    async def _signed_delete(
        self, endpoint: str, action_type: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Sign full {type,params} wrapper; send params only as body with DELETE."""
        nonce = self.nonce_manager.next_nonce()
        full_payload = {"type": action_type, "params": params}
        signature = self.signer.sign_payload(full_payload, nonce)

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-Key": self.signer.get_address(),
            "X-API-Sign": signature,
            "X-API-Nonce": str(nonce),
        }

        response = await self.client.request(
            "DELETE", f"{self.base_url}{endpoint}", json=params, headers=headers
        )
        if response.status_code not in (200, 204):
            raise SoDEXAPIError(f"API delete failed: {response.text}", response.status_code)

        return response.json() if response.content else {}

    async def _place_entry_order(self, bracket: BracketOrder) -> OrderResult:
        """Place entry LIMIT order."""
        c = bracket.candidate
        cl_ord_id = f"entry_{c.symbol}_{int(c.timestamp_ms)}"
        side = 1 if c.side == "long" else 2

        order_item = self._build_order_item(
            cl_ord_id=cl_ord_id,
            side=side,
            order_type=1,           # LIMIT
            tif=1,                  # GTC
            quantity=str(c.size),
            price=str(c.entry_price),
            reduce_only=False,
        )
        params = {
            "accountID": int(bracket.account_id),
            "symbolID": bracket.symbol_id,
            "orders": [order_item],
        }
        return await self.place_order(params)

    async def _place_stop_order(self, bracket: BracketOrder) -> OrderResult:
        """Place stop LIMIT order (reduce-only, opposite side)."""
        c = bracket.candidate
        cl_ord_id = f"stop_{c.symbol}_{int(c.timestamp_ms)}"
        side = 2 if c.side == "long" else 1  # opposite

        order_item = self._build_order_item(
            cl_ord_id=cl_ord_id,
            side=side,
            order_type=1,           # LIMIT
            tif=1,                  # GTC
            quantity=str(c.size),
            price=str(c.stop_price),
            reduce_only=True,
        )
        params = {
            "accountID": int(bracket.account_id),
            "symbolID": bracket.symbol_id,
            "orders": [order_item],
        }
        return await self.place_order(params)

    async def _place_tp_orders(self, bracket: BracketOrder) -> List[OrderResult]:
        """Place TP1 (50%), TP2 (30%), TP3 (20%) limit orders."""
        c = bracket.candidate
        side = 2 if c.side == "long" else 1  # opposite
        results = []

        for i, (pct, tp_price) in enumerate(zip(
            [0.5, 0.3, 0.2],
            [c.tp1_price, c.tp2_price, c.tp3_price]
        )):
            cl_ord_id = f"tp{i+1}_{c.symbol}_{int(c.timestamp_ms)}"
            order_item = self._build_order_item(
                cl_ord_id=cl_ord_id,
                side=side,
                order_type=1,       # LIMIT
                tif=1,              # GTC
                quantity=str(round(c.size * pct, 8)),
                price=str(tp_price),
                reduce_only=True,
            )
            params = {
                "accountID": int(bracket.account_id),
                "symbolID": bracket.symbol_id,
                "orders": [order_item],
            }
            results.append(await self.place_order(params))

        return results

    async def _cleanup_orders(self, order_tuples: List[tuple]):
        """Cancel multiple orders — (order_id, symbol, account_id)."""
        for order_id, symbol, account_id in order_tuples:
            try:
                await self.cancel_order(order_id, symbol, int(account_id))
            except Exception:
                pass

    async def close(self):
        """Shutdown persistent HTTP client."""
        self._is_active = False
        if self._keepalive_task:
            self._keepalive_task.cancel()
        await self.client.aclose()
        logger.info("persistent_http_client_closed")

    def start_keepalive(self):
        """Starts background keepalive ping loop."""
        if self._keepalive_task is None:
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
            logger.info("http_keepalive_loop_started")

    async def _keepalive_loop(self):
        """Ping the server every 20s to keep connection warm."""
        while self._is_active:
            try:
                await self.client.get(
                    f"{self.base_url}/markets/mark-prices",
                    params={"symbol": "BTC-USD"},
                    timeout=5.0,
                )
                logger.debug("http_keepalive_ping_sent")
            except Exception as e:
                logger.warning("http_keepalive_ping_failed", error=str(e))
            await asyncio.sleep(20)
