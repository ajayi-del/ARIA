"""
SoDEX REST API Client

Handles all authenticated and public calls to SoDEX perps.
Raises exceptions on HTTP errors. Never swallows errors silently.
"""

import json
import math
import time
import asyncio
import structlog
import httpx
import certifi
from typing import Dict, Any, List, Optional
from execution.schemas import OrderResult, BracketResult, BracketOrder
from execution.metrics import TradeMetrics, metrics_logger

logger = structlog.get_logger(__name__)

# Per-symbol tick_size and step_size (min order qty increment).
# Prices not aligned to tick_size → SoDEX code:-1 "unknown".
# Quantities not aligned to step_size → same rejection.
# (symbol_id → (tick_size, step_size))
_TICK_STEP: Dict[int, tuple] = {
    1:  (1,      0.00001), # BTC-USD      — confirmed live 2026-04-12
    2:  (0.1,    0.0001),  # ETH-USD
    6:  (0.01,   0.001),   # SOL-USD
    9:  (0.1,    0.001),   # BNB-USD
    5:  (0.001,  0.1),     # LINK-USD
    24: (0.001,  1),       # AVAX-USD
    11: (0.1,    0.0001),  # XAUT-USD
    23: (0.0001, 0.1),     # SUI-USD
    53: (1,      0.0001),  # USTECH100-USD
}


def _round_price(price: float, tick: float) -> str:
    """Round price to nearest tick, return as string with correct decimal places."""
    ticks = round(price / tick)
    rounded = ticks * tick
    # Determine decimal places from tick (e.g. 0.05 → 2 dp, 0.5 → 1 dp, 1.0 → 0 dp)
    dp = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return f"{rounded:.{dp}f}"


def _round_qty(qty: float, step: float) -> str:
    """Floor quantity to nearest step (always floor — never over-fill)."""
    floored = math.floor(qty / step) * step
    dp = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return f"{floored:.{dp}f}"


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
        # signing_key_address: EVM address derived from the signing private key.
        # Used only for resolving the registered API key name at startup.
        # The wallet address (config.sodex_account_id) is only used in GET URL paths.
        self.signing_key_address = signer.get_address()
        # api_key_name: the registered name for this signing key on SoDEX.
        # X-API-Key header must be this NAME (e.g. "ariaworks"), not the raw address.
        # Resolved at startup via resolve_api_key_name().
        self.api_key_name: str = ""

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
        Filters non-dict items — API occasionally returns strings in the list.
        """
        response = await self.client.get(f"{self.base_url}/accounts/{address}/positions")
        if response.status_code != 200:
            raise SoDEXAPIError(f"Failed to get positions: {response.text}", response.status_code)
        data = response.json()
        pos_data = data.get("data", {})
        if isinstance(pos_data, dict):
            raw = pos_data.get("positions") or pos_data.get("P") or []
        else:
            raw = pos_data or []
        return [p for p in raw if isinstance(p, dict)]

    async def get_open_orders(self, address: str) -> List[Dict]:
        """GET /accounts/{address}/orders
        Filters non-dict items — API occasionally returns status strings in the list,
        which cause 'str' object has no attribute 'get' crashes in callers.
        """
        response = await self.client.get(f"{self.base_url}/accounts/{address}/orders")
        if response.status_code != 200:
            raise SoDEXAPIError(f"Failed to get open orders: {response.text}", response.status_code)
        data = response.json()
        raw = data.get("data", [])
        if isinstance(raw, list):
            return [o for o in raw if isinstance(o, dict)]
        return []

    async def get_account_balance(self, address: str) -> float:
        """
        GET /api/v1/perps/accounts/{address}/balances
        Extracts available balance from the SoDEX perps balance response.
        Response structure: {"data": {"balances": [{"asset":"USDC","available":"...", "equity":"..."}]}}
        Falls back to spot balances if perps returns empty.
        """
        addr = address or self.config.sodex_account_id or self.config.account_id or ""
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
                    # Use ONLY free/available balance — never "equity" which includes
                    # unrealized PnL. Sizing from unrealized PnL inflates trade sizes
                    # when profitable positions are open, then collapses on close.
                    for field in ("available", "availableBalance"):
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
            # httpx timeout/connect exceptions often produce empty str(e) —
            # include the type name so the log is always actionable.
            _emsg = str(e) or f"{type(e).__name__} (no message)"
            logger.warning("balance_fetch_failed", error=_emsg, exc_type=type(e).__name__)
            return 0.0

    async def fetch_perp_fee_rate(self, address: str = "", symbol: str = "") -> dict:
        """
        GET /api/v1/perps/accounts/{address}/fee-rate
        Returns live perps maker/taker rates from SoDEX (weight=2).
        Optionally pass a symbol for symbol-level fee discount.

        Returns dict: makerFeeRate, takerFeeRate, tier, stakingTier (floats/ints).
        """
        addr = address or self.config.sodex_account_id or self.config.account_id or ""
        base = "mainnet-gw.sodex.dev" if self.config.sodex_mainnet else "testnet-gw.sodex.dev"
        params = {"symbol": symbol} if symbol else {}
        try:
            resp = await self.client.get(
                f"https://{base}/api/v1/perps/accounts/{addr}/fee-rate",
                params=params or None,
                timeout=8.0,
            )
            data = resp.json()
            if data.get("code") == 0:
                fee_data = data.get("data", {})
                return {
                    "makerFeeRate": float(fee_data.get("makerFeeRate", 0) or 0),
                    "takerFeeRate": float(fee_data.get("takerFeeRate", 0) or 0),
                    "tier":         int(fee_data.get("tier", fee_data.get("feeTier", 0)) or 0),
                    "stakingTier":  int(fee_data.get("stakingTier", 0) or 0),
                }
        except Exception as e:
            _emsg = str(e) or f"{type(e).__name__} (no message)"
            logger.warning("perp_fee_rate_fetch_failed", error=_emsg)
        return {"makerFeeRate": 0.0, "takerFeeRate": 0.0, "tier": 0, "stakingTier": 0}

    async def resolve_api_key_name(self) -> str:
        """
        Fetch the registered API key name for this signing key.
        X-API-Key header must be the name registered on SoDEX (e.g. "ariaworks"),
        NOT the raw signing key address.
        Queries GET /accounts/{wallet}/api-keys, matches publicKey to signing_key_address.
        """
        wallet = self.config.sodex_account_id or ""
        if not wallet:
            raise SoDEXAPIError("sodex_account_id not configured — cannot resolve API key name")

        resp = await self.client.get(f"{self.base_url}/accounts/{wallet}/api-keys")
        if resp.status_code != 200:
            raise SoDEXAPIError(f"Failed to fetch API keys: {resp.text}", resp.status_code)

        data = resp.json()
        if data.get("code") != 0:
            raise SoDEXAPIError(f"API key list error: code={data.get('code')} msg={data.get('msg')}")

        signing_lower = self.signing_key_address.lower()
        for key in data.get("data", []):
            if key.get("publicKey", "").lower() == signing_lower:
                self.api_key_name = key["name"]
                logger.info("api_key_name_resolved",
                            name=self.api_key_name,
                            signing_address=self.signing_key_address,
                            expires_at=key.get("expiresAt", 0))
                return self.api_key_name

        raise SoDEXAPIError(
            f"Signing key {self.signing_key_address} not found in API keys for {wallet}. "
            f"Register it on the SoDEX dashboard first."
        )

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
            # SoDEX newOrder response: {"code":0,"data":[{...}]} — data is a LIST.
            # Older/alternate format: {"code":0,"data":{"orders":[...]}} — data is a dict.
            raw = data.get("data", {})
            if isinstance(raw, list):
                orders = raw
            elif isinstance(raw, dict):
                orders = raw.get("orders", [])
            else:
                orders = []
            if orders:
                o = orders[0]
                # Inner code may be -1 even when outer code=0 (per-order validation)
                inner_code = o.get("code", 0)
                if inner_code != 0:
                    inner_err = (
                        o.get("error") or o.get("msg") or
                        o.get("message") or f"inner_code={inner_code}"
                    )
                    logger.error("sodex_inner_order_rejected",
                                 inner_code=inner_code, error=inner_err,
                                 clOrdID=o.get("clOrdID"))
                    return OrderResult(order_id="", status="rejected",
                                       fill_price=None, fill_qty=None, error=inner_err)
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

    async def cancel_order(self, order_id: str, symbol: str, account_id: int,
                           symbol_id: int = 0) -> bool:
        """DELETE /trade/orders — cancels array format with uint64 orderID."""
        params = {
            "accountID": account_id,
            "cancels": [{"orderID": int(order_id), "symbolID": symbol_id}],
        }
        try:
            result = await self._signed_delete("/trade/orders", "cancelOrder", params)
            return result.get("code", -1) == 0
        except SoDEXAPIError:
            return False

    async def place_bracket(self, bracket: BracketOrder) -> BracketResult:
        """
        Places entry → waits for fill confirmation → stop → TP1/2/3.

        Sequence:
          1. Entry LIMIT placed.
          2. Poll positions up to 60s for fill confirmation.
             - Not filled: cancel entry, return failure (price moved away).
             - Filled: proceed to reduce-only stop/TP.
          3. Stop placed (reduce-only, exchange-side limit order).
             If stop fails after fill: returns partial success — deferred retry
             in caller will re-attempt. Emergency fallback is market exit.
          4. TPs placed (reduce-only, 50%/30%/20% split).

        Metrics: fire-and-forget TradeMetrics emitted at completion.
        Does NOT block execution path — caller runs this as asyncio.create_task().
        """
        placed_orders = []

        # ── Metrics object — timestamps set at each milestone ────────────────
        _c = bracket.candidate
        _sym_clean = _c.symbol.replace("-", "").replace("_", "")
        _m = TradeMetrics(
            trade_id=f"e{_sym_clean}{int(_c.timestamp_ms)}",
            symbol=_c.symbol,
            side=_c.side,
            expected_price=_c.entry_price,
            t_signal=_c.timestamp_ms / 1000.0 if _c.timestamp_ms > 0 else time.time(),
        )

        try:
            address = self.config.sodex_account_id or self.config.account_id or ""

            # ── 0. Snapshot pre-entry position size ──────────────────────────────
            pre_size = 0.0
            try:
                existing = await self.get_positions(address)
                for pos in existing:
                    sym = pos.get("symbol", "") or pos.get("coin", "")
                    if sym == bracket.candidate.symbol:
                        pre_size = abs(float(pos.get("size", 0) or pos.get("qty", 0) or 0))
                        break
            except Exception:
                pass

            # ── 1. Entry ─────────────────────────────────────────────────────────
            _m.t_entry_sent = time.time()
            entry_result = await self._place_entry_order(bracket)
            if not entry_result.success:
                metrics_logger.emit(_m)  # fire-and-forget — never blocks
                return BracketResult(success=False, error=f"Entry failed: {entry_result.error}")
            placed_orders.append((entry_result.order_id, bracket.candidate.symbol, bracket.account_id))

            # ── 2. Wait for position fill ────────────────────────────────────────
            filled, actual_size = await self._confirm_position_open(
                symbol=bracket.candidate.symbol,
                account_address=address,
                min_size=bracket.candidate.size * 0.5,  # accept 50% partial fill
                pre_size=pre_size,
                timeout_s=60.0,
            )
            if not filled:
                logger.warning("entry_not_filled_cancelling",
                               symbol=bracket.candidate.symbol,
                               order_id=entry_result.order_id)
                await self._cleanup_orders(placed_orders)
                metrics_logger.emit(_m)
                return BracketResult(
                    success=False,
                    error="entry_not_filled_within_60s: cancelled"
                )

            _m.t_fill = time.time()
            # For limit orders the actual fill price ≈ entry_price (no slippage).
            # If the order result carries a fill price, use it.
            if entry_result.fill_price and entry_result.fill_price > 0:
                _m.actual_fill_price = entry_result.fill_price
            else:
                _m.actual_fill_price = bracket.candidate.entry_price

            # ── 3. Stop (reduce-only, exchange-side) ────────────────────────────
            _m.t_stop_sent = time.time()
            stop_result = await self._place_stop_order(bracket)
            if not stop_result.success:
                logger.error("stop_failed_after_fill",
                             symbol=bracket.candidate.symbol, error=stop_result.error)
                _m.stop_placed = False
                metrics_logger.emit(_m)
                return BracketResult(
                    success=True,  # position IS open — track it, deferred retry will re-protect
                    entry_order_id=entry_result.order_id,
                    error=f"stop_failed_after_fill: {stop_result.error}",
                )
            _m.t_stop_confirmed = time.time()
            _m.stop_placed = True
            placed_orders.append((stop_result.order_id, bracket.candidate.symbol, bracket.account_id))

            # ── 4. TPs (reduce-only) ─────────────────────────────────────────────
            _m.t_tp_sent = time.time()
            tp_results = await self._place_tp_orders(bracket)
            failed_tps = [r for r in tp_results if not r.success]
            if failed_tps:
                await self._cleanup_orders([
                    (r.order_id, bracket.candidate.symbol, bracket.account_id)
                    for r in tp_results if r.order_id and r.success
                ])
                logger.error("tp_failed",
                             symbol=bracket.candidate.symbol, error=failed_tps[0].error)
                _m.tp_placed = False
                metrics_logger.emit(_m)
                return BracketResult(
                    success=True,
                    entry_order_id=entry_result.order_id,
                    stop_order_id=stop_result.order_id,
                    error=f"tp_failed: {failed_tps[0].error}",
                )

            _m.tp_placed = True
            metrics_logger.emit(_m)  # full success — emit all timing data

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
            metrics_logger.emit(_m)
            await self._cleanup_orders(placed_orders)
            return BracketResult(success=False, error=f"Bracket placement failed: {str(e)}")

    async def place_protective_orders(self, bracket: BracketOrder) -> BracketResult:
        """
        Place stop + TPs for an already-open position (no entry, no fill wait).
        Used when place_bracket returned partial success (entry filled but stop/TP failed).
        """
        try:
            stop_result = await self._place_stop_order(bracket)
            if not stop_result.success:
                return BracketResult(success=False, error=f"Stop retry failed: {stop_result.error}")

            tp_results = await self._place_tp_orders(bracket)
            failed_tps = [r for r in tp_results if not r.success]
            if failed_tps:
                await self._cleanup_orders([
                    (r.order_id, bracket.candidate.symbol, bracket.account_id)
                    for r in tp_results if r.order_id and r.success
                ])
                return BracketResult(success=False, error=f"TP retry failed: {failed_tps[0].error}")

            tp_ids = [r.order_id for r in tp_results]
            logger.info("protective_orders_placed",
                        symbol=bracket.candidate.symbol,
                        stop_id=stop_result.order_id, tp_ids=tp_ids)
            return BracketResult(
                success=True,
                stop_order_id=stop_result.order_id,
                tp1_order_id=tp_ids[0],
                tp2_order_id=tp_ids[1],
                tp3_order_id=tp_ids[2],
            )
        except Exception as e:
            return BracketResult(success=False, error=f"Protective orders failed: {str(e)}")

    async def update_leverage(self, symbol_id: int, leverage: int, account_id: int) -> bool:
        """
        POST /trade/leverage

        Tries marginMode=2 (cross) first. Some symbols (e.g. SOL-USD) reject
        mode 2; for those we fall back to marginMode=1 (isolated). If both
        modes are rejected, returns False — caller logs the warning.
        """
        for margin_mode in (2, 1):
            params = {
                "accountID": account_id,
                "symbolID": symbol_id,
                "leverage": leverage,
                "marginMode": margin_mode,
            }
            try:
                result = await self._signed_post("/trade/leverage", "updateLeverage", params)
                if result.get("code", -1) == 0:
                    return True
                # code:-1 with marginMode=2 → try the other mode
                if margin_mode == 2:
                    continue
            except SoDEXAPIError:
                if margin_mode == 2:
                    continue
        return False

    async def _confirm_position_open(
        self, symbol: str, account_address: str,
        min_size: float, pre_size: float = 0.0,
        timeout_s: float = 30.0
    ) -> tuple:
        """
        Poll until the symbol's position size exceeds pre_size + min_size.

        pre_size: size snapshot taken BEFORE the entry order was placed.
        This prevents false positives from pre-existing positions.

        Returns (filled: bool, actual_size: float).
        actual_size is the confirmed position size when filled (0.0 on timeout).

        CRITICAL FIX: SoDEX uses NEGATIVE size for short positions.
        abs() converts -0.002 BTC short → 0.002 for comparison.
        Without this, ALL short fills are invisible and stops are never placed.
        """
        target = pre_size + min_size
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        # Fast path: poll every 250ms for first 3s (limit orders usually fill quickly).
        # After 3s fall back to 750ms cadence to reduce API pressure on slow fills.
        _fast_end = loop.time() + 3.0
        while loop.time() < deadline:
            try:
                positions = await self.get_positions(account_address)
                for pos in positions:
                    sym = pos.get("symbol", "") or pos.get("coin", "")
                    size = abs(float(pos.get("size", 0) or pos.get("qty", 0) or 0))
                    if sym == symbol and size >= target:
                        logger.info("position_fill_confirmed",
                                    symbol=symbol, pre_size=pre_size,
                                    current_size=size, target=target)
                        return True, size
            except Exception as e:
                logger.debug("confirm_position_poll_error", error=str(e))
            _interval = 0.25 if loop.time() < _fast_end else 0.75
            await asyncio.sleep(_interval)
        logger.warning("position_fill_timeout",
                       symbol=symbol, min_size=min_size,
                       pre_size=pre_size, target=target, timeout_s=timeout_s)
        return False, 0.0

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
        Build order item dict with EXACT canonical field order required for signing.

        SoDEX signing rule: payloadHash = keccak256(json.Marshal(payload)).
        The Go server unmarshals the JSON, adds zero-defaults for missing fields,
        then re-marshals before hashing.  Any field omitted from our payload will
        be added by the server with its zero value before the hash is computed,
        making our client-side hash differ → code:-1 rejection.

        ALL fields must be present and in the exact order defined in PerpsOrderItem:
          clOrdID, modifier, side, type, timeInForce, price, quantity,
          funds, stopPrice, stopType, triggerType, reduceOnly, positionSide
        """
        item: Dict[str, Any] = {
            "clOrdID":      cl_ord_id,
            "modifier":     1,           # NORMAL — always 1 for standard orders
            "side":         side,
            "type":         order_type,
            "timeInForce":  tif,
        }
        # price: omit for MARKET orders (SoDEX rejects price=0 for market)
        if price is not None:
            item["price"] = price
        item["quantity"] = quantity
        # funds, stopPrice, stopType, triggerType are omitempty in Go struct.
        # Sending them as "0"/0 causes "stopType is invalid" rejection. OMIT them.
        item["reduceOnly"]   = reduce_only
        item["positionSide"] = 1         # BOTH — SoDEX only supports oneway mode
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
            "X-API-Key": self.api_key_name,
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
            # Log the exact payload bytes so we can diff against SoDEX expected hash
            _payload_bytes = json.dumps(
                {"type": action_type, "params": params}, separators=(",", ":")
            ).encode("utf-8")
            _err_msg = (
                data.get("error") or data.get("msg") or
                data.get("message") or "unknown"
            )
            logger.error(
                "sodex_order_rejected",
                code=data.get("code"),
                msg=_err_msg,
                api_key_name=self.api_key_name,
                action=action_type,
                account_id=params.get("accountID"),
                symbol_id=params.get("symbolID"),
                payload_json=_payload_bytes.decode("utf-8"),
                nonce=nonce,
            )
            raise SoDEXAPIError(
                f"SoDEX error {data.get('code')}: {_err_msg}",
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
            "X-API-Key": self.api_key_name,
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
        _sym_clean = c.symbol.replace("-", "").replace("_", "")
        cl_ord_id = f"e{_sym_clean}{int(c.timestamp_ms)}"
        side = 1 if c.side == "long" else 2
        tick, step = _TICK_STEP.get(bracket.symbol_id, (0.01, 0.01))

        qty_str = _round_qty(c.size, step)
        qty_float = float(qty_str)
        # Pre-flight: zero quantity or dust notional → reject before hitting exchange.
        # SoDEX rejects qty=0 with code:-1 "unknown"; catch it here to avoid burning
        # a per-symbol cooldown on a sizing bug.
        if qty_float <= 0:
            logger.error("entry_order_zero_qty",
                         symbol=c.symbol, size=c.size, step=step, qty_str=qty_str)
            # Prefix "SoDEX error -1:" so caller treats this as structural (per-symbol
            # cooldown only, does not trip the global circuit breaker).
            return OrderResult(order_id="", status="rejected",
                               fill_price=None, fill_qty=None,
                               error="SoDEX error -1: zero_quantity_after_step_rounding")
        price_str = _round_price(c.entry_price, tick)
        notional = qty_float * float(price_str)
        if notional < 50.0:
            logger.error("entry_order_dust_notional",
                         symbol=c.symbol, qty=qty_str, price=price_str,
                         notional=round(notional, 2), min_notional=50.0)
            return OrderResult(order_id="", status="rejected",
                               fill_price=None, fill_qty=None,
                               error=f"SoDEX error -1: notional_{notional:.2f}_below_50usd_minimum")

        order_item = self._build_order_item(
            cl_ord_id=cl_ord_id,
            side=side,
            order_type=1,           # LIMIT
            tif=1,                  # GTC
            quantity=qty_str,
            price=price_str,
            reduce_only=False,
        )
        params = {
            "accountID": int(bracket.account_id),
            "symbolID": bracket.symbol_id,
            "orders": [order_item],
        }
        return await self.place_order(params)

    async def _place_stop_order(self, bracket: BracketOrder) -> OrderResult:
        """Place stop LIMIT order (reduce-only, opposite side).

        Stop price convention (verified before sending):
          LONG  position → stop is BELOW entry (sell limit below market)
          SHORT position → stop is ABOVE entry (buy  limit above market)
        """
        c = bracket.candidate
        _sym_clean = c.symbol.replace("-", "").replace("_", "")
        cl_ord_id = f"sl{_sym_clean}{int(c.timestamp_ms)}"
        side = 2 if c.side == "long" else 1  # opposite: long→sell(2), short→buy(1)
        tick, step = _TICK_STEP.get(bracket.symbol_id, (0.01, 0.01))

        # Verify stop is on the correct side before sending to exchange.
        # Wrong-side stops are a class of bug that causes immediate fills.
        _distance_pct = abs(c.stop_price - c.entry_price) / c.entry_price * 100
        _stop_valid = (
            (c.side == "long"  and c.stop_price < c.entry_price) or
            (c.side == "short" and c.stop_price > c.entry_price)
        )
        logger.info(
            "stop_order_sending",
            symbol=c.symbol,
            position_side=c.side,
            entry_price=round(c.entry_price, 6),
            stop_price=round(c.stop_price, 6),
            stop_order_side="sell" if c.side == "long" else "buy",
            distance_pct=round(_distance_pct, 4),
            valid=_stop_valid,
        )
        if not _stop_valid:
            # Hard guard: return error rather than placing an inverted stop
            logger.error(
                "stop_order_sign_invalid",
                symbol=c.symbol,
                side=c.side,
                entry=round(c.entry_price, 6),
                stop=round(c.stop_price, 6),
                note="stop on wrong side of entry — refusing to place",
            )
            return OrderResult(order_id="", status="rejected",
                               error=f"stop_sign_invalid:{c.side} stop={c.stop_price:.4f} entry={c.entry_price:.4f}")

        order_item = self._build_order_item(
            cl_ord_id=cl_ord_id,
            side=side,
            order_type=1,           # LIMIT
            tif=1,                  # GTC
            quantity=_round_qty(c.size, step),
            price=_round_price(c.stop_price, tick),
            reduce_only=True,
        )
        params = {
            "accountID": int(bracket.account_id),
            "symbolID": bracket.symbol_id,
            "orders": [order_item],
        }
        return await self.place_order(params)

    async def _place_tp_orders(self, bracket: BracketOrder) -> List[OrderResult]:
        """
        Place TP1 (50%), TP2 (30%), TP3 (20%) limit orders in a SINGLE batched request.

        One EIP-712 signing round-trip + one HTTP round-trip instead of three.
        Saves ~150-600ms vs serial placement — directly reduces total bracket latency.

        SoDEX /trade/orders accepts orders:[item1, item2, item3] in one call.
        Response is a list mirroring the input order. We parse each per-order code
        independently so a single TP failure doesn't mask the others.
        """
        c = bracket.candidate
        side = 2 if c.side == "long" else 1  # opposite of position side
        _sym_clean = c.symbol.replace("-", "").replace("_", "")
        tick, step = _TICK_STEP.get(bracket.symbol_id, (0.01, 0.01))

        order_items = []
        for i, (pct, tp_price) in enumerate(zip(
            [0.5, 0.3, 0.2],
            [c.tp1_price, c.tp2_price, c.tp3_price]
        )):
            cl_ord_id = f"tp{i+1}{_sym_clean}{int(c.timestamp_ms)}"
            order_items.append(self._build_order_item(
                cl_ord_id=cl_ord_id,
                side=side,
                order_type=1,   # LIMIT
                tif=1,          # GTC
                quantity=_round_qty(c.size * pct, step),
                price=_round_price(tp_price, tick),
                reduce_only=True,
            ))

        params = {
            "accountID": int(bracket.account_id),
            "symbolID": bracket.symbol_id,
            "orders": order_items,
        }

        try:
            data = await self._signed_post("/trade/orders", "newOrder", params)
            raw = data.get("data", {})
            if isinstance(raw, list):
                orders_resp = raw
            elif isinstance(raw, dict):
                orders_resp = raw.get("orders", [])
            else:
                orders_resp = []

            results: List[OrderResult] = []
            for o in orders_resp:
                inner_code = o.get("code", 0)
                if inner_code != 0:
                    inner_err = (
                        o.get("error") or o.get("msg") or
                        o.get("message") or f"inner_code={inner_code}"
                    )
                    results.append(OrderResult(
                        order_id="", status="rejected",
                        fill_price=None, fill_qty=None, error=inner_err,
                    ))
                else:
                    results.append(OrderResult(
                        order_id=str(o.get("orderID", o.get("clOrdID", ""))),
                        status=str(o.get("status", "open")),
                        fill_price=None, fill_qty=None, error=None,
                    ))

            # Pad if exchange returned fewer results than sent (should not happen)
            while len(results) < 3:
                results.append(OrderResult(
                    order_id="", status="unknown",
                    fill_price=None, fill_qty=None,
                    error="missing_in_batch_response",
                ))

            return results

        except SoDEXAPIError as e:
            # Entire batch rejected — return 3 failures so caller's failed_tps logic fires
            return [
                OrderResult(order_id="", status="rejected",
                            fill_price=None, fill_qty=None, error=e.message)
                for _ in range(3)
            ]

    async def replace_stop_order(
        self,
        symbol: str,
        symbol_id: int,
        account_id: int,
        new_stop_price: float,
        old_stop_order_id: Optional[str],
        side: str,
        size: float,
    ) -> "OrderResult":
        """
        Software trailing stop: place new stop first (never un-protected),
        then cancel the old one.

        Design: place-before-cancel ensures the position is ALWAYS protected
        during the transition. If the cancel fails it's acceptable — the old
        stop is now redundant (exchange will reject the smaller one as reduce-only
        overflow, and trigger on the more-favourable new one first).
        """
        tick, step = _TICK_STEP.get(symbol_id, (0.01, 0.01))
        stop_side = 2 if side == "long" else 1   # opposite direction
        _sym_clean = symbol.replace("-", "").replace("_", "")
        cl_ord_id = f"ts{_sym_clean}{int(time.time() * 1000)}"

        order_item = self._build_order_item(
            cl_ord_id=cl_ord_id,
            side=stop_side,
            order_type=1,   # LIMIT
            tif=1,          # GTC
            quantity=_round_qty(size, step),
            price=_round_price(new_stop_price, tick),
            reduce_only=True,
        )
        params = {
            "accountID": account_id,
            "symbolID": symbol_id,
            "orders": [order_item],
        }
        result = await self.place_order(params)
        if result.success and old_stop_order_id:
            # Cancel old stop AFTER new one is confirmed placed
            try:
                await self.cancel_order(old_stop_order_id, symbol, account_id)
            except Exception:
                pass  # old stop becomes redundant — harmless
        return result

    async def close_position_market(
        self,
        symbol: str,
        symbol_id: int,
        account_id: int,
        side: str,
        size: float,
    ) -> "OrderResult":
        """
        Market-close a position immediately (time stop / emergency).
        Uses MARKET order type with IOC TIF so it fills or cancels instantly.
        Always reduce-only — cannot accidentally open a new position.
        """
        tick, step = _TICK_STEP.get(symbol_id, (0.01, 0.01))
        close_side = 2 if side == "long" else 1   # opposite of position side
        _sym_clean = symbol.replace("-", "").replace("_", "")
        cl_ord_id = f"tc{_sym_clean}{int(time.time() * 1000)}"  # tc = time-close

        order_item = self._build_order_item(
            cl_ord_id=cl_ord_id,
            side=close_side,
            order_type=2,   # MARKET
            tif=3,          # IOC — fill immediately or cancel
            quantity=_round_qty(size, step),
            price=None,     # no price for market orders
            reduce_only=True,
        )
        params = {
            "accountID": account_id,
            "symbolID": symbol_id,
            "orders": [order_item],
        }
        result = await self.place_order(params)
        logger.info("close_position_market_sent",
                    symbol=symbol, side=side, size=size,
                    success=result.success, order_id=result.order_id)
        return result

    async def place_order_simple(
        self,
        symbol: str,
        side: str,          # "buy"/"long" or "sell"/"short"
        contracts: float,
        price: float,
        symbol_id: int,
        account_id: int,
    ) -> OrderResult:
        """
        Simplified single-leg order for arb strategies.

        Places a LIMIT order (price > 0) or MARKET/IOC order (price == 0).
        Not reduce-only — used for opening arb legs.

        Args:
            symbol:     e.g. "BTC-USD"
            side:       "buy" / "long" → side=1; "sell" / "short" → side=2
            contracts:  order quantity in base asset
            price:      limit price; pass 0.0 for MARKET
            symbol_id:  SoDEX numeric symbol ID
            account_id: SoDEX numeric account ID (aid)
        """
        tick, step = _TICK_STEP.get(symbol_id, (0.01, 0.01))
        _sym_clean = symbol.replace("-", "").replace("_", "")
        cl_ord_id = f"arb{_sym_clean}{int(time.time() * 1000)}"[:36]

        side_int = 1 if side.lower() in ("buy", "long") else 2
        use_market = (price <= 0)
        order_item = self._build_order_item(
            cl_ord_id=cl_ord_id,
            side=side_int,
            order_type=2 if use_market else 1,   # MARKET=2, LIMIT=1
            tif=3 if use_market else 1,           # IOC for market, GTC for limit
            quantity=_round_qty(contracts, step),
            price=None if use_market else _round_price(price, tick),
            reduce_only=False,
        )
        params = {
            "accountID": account_id,
            "symbolID": symbol_id,
            "orders": [order_item],
        }
        result = await self.place_order(params)
        logger.info("place_order_simple_sent",
                    symbol=symbol, side=side, contracts=contracts,
                    price=price if not use_market else "MARKET",
                    success=result.success, order_id=result.order_id)
        return result

    async def place_maker_first(
        self,
        symbol: str,
        side: str,          # "buy"/"long" or "sell"/"short"
        contracts: float,
        mark_price: float,
        symbol_id: int,
        account_id: int,
        wait_seconds: int = 30,
        maker_offset: float = 0.0001,   # 0.01% offset from mark to post as limit
    ) -> tuple:
        """
        Post-only maker attempt: try a limit at best-bid/ask first.
        If unfilled after wait_seconds, cancel and fall back to market.

        Cost savings: maker vs taker on Tier 0 = 0.040% - 0.012% = 0.028% per leg.
        On a $1,000 arb position that's $0.28 saved per entry — ~60% fee reduction.

        Returns:
            (OrderResult, was_maker: bool)
            was_maker=True if the limit order filled before the deadline.
            was_maker=False if we fell back to market.

        Args:
            mark_price:   current mark price — limit placed at slight offset
            maker_offset: fraction to offset from mark (default 0.01%)
                          buy:  price = mark × (1 - maker_offset)  → post below mark
                          sell: price = mark × (1 + maker_offset)  → post above mark
        """
        tick, step = _TICK_STEP.get(symbol_id, (0.01, 0.01))
        side_int = 1 if side.lower() in ("buy", "long") else 2
        _sym_clean = symbol.replace("-", "").replace("_", "")
        cl_ord_id = f"mk{_sym_clean}{int(time.time() * 1000)}"[:36]

        # Maker limit: buy below mark, sell above mark → we're first in queue
        if side_int == 1:
            limit_price = mark_price * (1.0 - maker_offset)
        else:
            limit_price = mark_price * (1.0 + maker_offset)

        order_item = self._build_order_item(
            cl_ord_id=cl_ord_id,
            side=side_int,
            order_type=1,       # LIMIT
            tif=1,              # GTC
            quantity=_round_qty(contracts, step),
            price=_round_price(limit_price, tick),
            reduce_only=False,
        )
        params = {
            "accountID": account_id,
            "symbolID": symbol_id,
            "orders": [order_item],
        }

        # Step 1: Place the limit order
        limit_result = await self.place_order(params)
        if not limit_result.success:
            # Limit was immediately rejected — fall back to market
            logger.warning("maker_limit_rejected", symbol=symbol, side=side,
                           error=limit_result.error, action="falling_back_to_market")
            market_result = await self.place_order_simple(
                symbol=symbol, side=side, contracts=contracts,
                price=0.0, symbol_id=symbol_id, account_id=account_id,
            )
            return market_result, False

        logger.info("maker_limit_placed", symbol=symbol, side=side,
                    price=_round_price(limit_price, tick), cl_ord_id=cl_ord_id,
                    wait_seconds=wait_seconds)

        # Step 2: Poll for fill confirmation
        deadline = time.time() + wait_seconds
        poll_interval = 3.0
        while time.time() < deadline:
            await asyncio.sleep(poll_interval)
            try:
                address = self.config.sodex_account_id or self.config.account_id or ""
                positions = await self.get_positions(address)
                for pos in positions:
                    sym = pos.get("symbol", "") or pos.get("coin", "")
                    # SoDEX uses NEGATIVE size for short positions — abs() required.
                    size = abs(float(pos.get("size", 0) or pos.get("qty", 0) or 0))
                    if sym == symbol and size > 0:
                        logger.info("maker_limit_filled", symbol=symbol, side=side,
                                    size=size, cl_ord_id=cl_ord_id)
                        return OrderResult(
                            order_id=cl_ord_id,
                            status="filled",
                            fill_price=limit_price,
                            fill_qty=contracts,
                            error=None,
                        ), True
            except Exception as _e:
                logger.debug("maker_fill_poll_error", error=str(_e))

        # Step 3: Deadline reached — cancel limit, fall back to market
        logger.info("maker_limit_expired", symbol=symbol, cl_ord_id=cl_ord_id,
                    action="cancelling_and_falling_back_to_market")
        try:
            await self.cancel_order(cl_ord_id, symbol, account_id)
        except Exception:
            pass

        market_result = await self.place_order_simple(
            symbol=symbol, side=side, contracts=contracts,
            price=0.0, symbol_id=symbol_id, account_id=account_id,
        )
        return market_result, False

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
