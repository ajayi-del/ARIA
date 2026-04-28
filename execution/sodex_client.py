"""
SoDEX REST API Client

Handles all authenticated and public calls to SoDEX perps.
Raises exceptions on HTTP errors. Never swallows errors silently.
"""

import json
import math
import time
import asyncio
from decimal import Decimal, ROUND_HALF_UP
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
    1:  (1,       0.00001), # BTC-USD      — tick=1, step=0.00001 (live API 2026-04-17)
    2:  (0.1,     0.0001),  # ETH-USD      — tick=0.1, step=0.0001 (live API 2026-04-17)
    6:  (0.01,    0.001),   # SOL-USD      — tick=0.01, step=0.001 (live API 2026-04-17)
    9:  (0.1,     0.001),   # BNB-USD      — tick=0.1, step=0.001 (live API 2026-04-17)
    5:  (0.001,   0.1),     # LINK-USD     — tick=0.001, step=0.1 (live API 2026-04-17)
    24: (0.001,   1),       # AVAX-USD     — tick=0.001, step=1 (live API 2026-04-17)
    11: (0.1,     0.0001),  # XAUT-USD     — tick=0.1, step=0.0001 (live API 2026-04-17)
    23: (0.0001,  0.1),     # SUI-USD      — tick=0.0001, step=0.1 (live API 2026-04-17)
}

# Symbol-name fallback for assets whose SoDEX integer IDs aren't known statically.
# Used when bracket.symbol_id is not in _TICK_STEP (e.g. ARB, OP, NEAR fetched at runtime).
# Format: (tick_size, step_size) — same semantics as _TICK_STEP.
_TICK_STEP_BY_NAME: Dict[str, tuple] = {
    # Dynamic-ID symbols — (tick_size, step_size) from live API 2026-04-17
    "ARB-USD":      (0.00001, 0.1),    # tick=0.00001, step=0.1
    "OP-USD":       (0.00001, 0.1),    # tick=0.00001, step=0.1
    "NEAR-USD":     (0.0001,  0.1),    # tick=0.0001,  step=0.1
    "MNT-USD":      (0.0001,  1.0),    # not in live API output — estimate retained
    "1000PEPE-USD": (0.000001, 1.0),   # tick=0.000001, step=1
    "XRP-USD":      (0.0001,  0.1),    # tick=0.0001,  step=0.1
    "TRUMP-USD":    (0.0001,  0.01),   # tick=0.0001,  step=0.01
    "BASED-USD":    (0.0001,  1.0),    # tick=0.0001,  step=1
    # Commodity — live API 2026-04-17
    "CL-USD":       (0.001,   0.001),  # tick=0.001,   step=0.001
    "COPPER-USD":   (0.0001,  0.01),   # tick=0.0001,  step=0.01
    # Equity — live API 2026-04-17 (tick=0.01, step=0.001 for all)
    "TSM-USD":      (0.01,    0.001),
    "ORCL-USD":     (0.01,    0.001),
    "NVDA-USD":     (0.01,    0.001),
    "MSFT-USD":     (0.01,    0.001),
    "AAPL-USD":     (0.01,    0.001),
    "AMZN-USD":     (0.01,    0.001),
    "GOOGL-USD":    (0.01,    0.001),
    "META-USD":     (0.01,    0.001),
    "TSLA-USD":     (0.01,    0.001),
}

# Authoritative step-size override table for close/market orders.
# SoDEX returns position sizes at full float precision; these per-symbol
# steps define the quantity increment the exchange actually enforces.
# Overrides _TICK_STEP_BY_NAME for close orders to use round (not floor).
STEP_SIZES: Dict[str, float] = {
    # Crypto — live API 2026-04-17
    "BTC-USD":       0.00001,
    "ETH-USD":       0.0001,
    "SOL-USD":       0.001,
    "LINK-USD":      0.1,
    "AVAX-USD":      1.0,
    "OP-USD":        0.1,
    "ARB-USD":       0.1,
    "SUI-USD":       0.1,
    "NEAR-USD":      0.1,
    "BNB-USD":       0.001,
    "1000PEPE-USD":  1.0,
    "MNT-USD":       1.0,
    "XAUT-USD":      0.0001,
    "XRP-USD":       0.1,
    "TRUMP-USD":     0.01,
    "BASED-USD":     1.0,
    # Commodity — live API 2026-04-17
    "CL-USD":        0.001,
    "COPPER-USD":    0.01,
    # Equity — live API 2026-04-17
    "TSM-USD":       0.001,
    "ORCL-USD":      0.001,
    "NVDA-USD":      0.001,
    "MSFT-USD":      0.001,
    "AAPL-USD":      0.001,
    "AMZN-USD":      0.001,
    "GOOGL-USD":     0.001,
    "META-USD":      0.001,
    "TSLA-USD":      0.001,
}

# Minimum order quantity per symbol (close orders must meet this floor).
# Extend as needed when new symbols show minimum qty requirements different from step size.
MIN_QTY: Dict[str, float] = {}


def _get_tick_step(symbol: str, symbol_id: int) -> tuple:
    """Returns (tick_size, step_size) for a symbol.
    Priority: _TICK_STEP by ID → _TICK_STEP_BY_NAME by name → (0.01, 0.01).
    Handles symbols whose integer IDs are fetched dynamically (ARB, OP, NEAR…).
    """
    if symbol_id in _TICK_STEP:
        return _TICK_STEP[symbol_id]
    return _TICK_STEP_BY_NAME.get(symbol, (0.01, 0.01))


def _round_price(price: float, tick: float) -> str:
    """Round price to nearest tick, return as string with correct decimal places.

    Uses Decimal arithmetic to avoid float precision loss at midpoints
    (e.g. 100.005 / 0.01 = 10000.4999... in float — Decimal gives exact 10000.5).
    """
    d_price = Decimal(str(price))
    d_tick  = Decimal(str(tick))
    ticks   = (d_price / d_tick).to_integral_value(rounding=ROUND_HALF_UP)
    rounded = float(ticks * d_tick)
    dp = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return f"{rounded:.{dp}f}"


def _round_qty(qty: float, step: float, reduce_only: bool = False) -> str:
    """Round quantity to nearest step.

    Default (entry orders): floor — never over-buy.
    reduce_only=True (TP/stop/close): round() with 1-step minimum for dust.
      SoDEX caps the fill at actual position size for reduce_only, so sending
      1 step for dust < 0.5×step is always safe. This prevents "quantity is
      invalid" rejections when fractional TP splits floor to zero (e.g. SUI
      step=0.1 with size=0.1 → TP1 qty=0.05 floors to 0 without this guard).
    """
    dp = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    if reduce_only:
        rounded = round(qty / step) * step
        if rounded <= 0 and qty > 0:
            rounded = step  # dust guard — at least 1 step; reduceOnly caps fill
    else:
        rounded = math.floor(qty / step) * step
    return f"{rounded:.{dp}f}"


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
            verify=certifi.where(),
            timeout=httpx.Timeout(connect=3.0, read=8.0, write=5.0, pool=2.0),
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                keepalive_expiry=25,   # under 30s TCP idle timeout
            ),
            headers={"Accept": "application/json"},
            http2=True,               # H/2 multiplexing — reuse single TCP conn
        )

        self._keepalive_task: Optional[asyncio.Task] = None
        self._is_active = True
        self.base_url = config.sodex_rest_perps
        # symbol_id_map: id → SoDEX symbol string (populated by fetch_symbol_mapping).
        # symbol_info:   SoDEX symbol string → full market spec dict (step, tick, etc.).
        # Both start empty — populated lazily at startup if caller calls fetch_symbol_mapping.
        self.symbol_id_map: Dict[int, str] = {}
        self.symbol_info:   Dict[str, Any] = {}

    def _dynamic_step(self, symbol: str) -> float:
        """Return step size from symbol_info if available, else STEP_SIZES fallback."""
        info = self.symbol_info.get(symbol, {})
        for key in ("lotSize", "lot_size", "stepSize", "step_size", "step", "qtyStep"):
            val = info.get(key)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
        return STEP_SIZES.get(symbol, 0.01)

    def _dynamic_tick(self, symbol: str) -> float:
        """Return tick size from symbol_info if available, else _TICK_STEP fallback."""
        info = self.symbol_info.get(symbol, {})
        for key in ("tickSize", "tick_size", "tick", "priceTick"):
            val = info.get(key)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
        # Fall back to static tables
        _, tick = _get_tick_step(symbol, 0)
        return tick

    def get_tick_step(self, symbol: str, symbol_id: int = 0) -> tuple:
        """Dynamic (tick, step) using symbol_info when fetched, else static tables."""
        info = self.symbol_info.get(symbol, {})
        tick = None
        step = None
        for k in ("tickSize", "tick_size", "tick", "priceTick"):
            v = info.get(k)
            if v is not None:
                try:
                    tick = float(v)
                    break
                except (TypeError, ValueError):
                    pass
        for k in ("lotSize", "lot_size", "stepSize", "step_size", "step", "qtyStep"):
            v = info.get(k)
            if v is not None:
                try:
                    step = float(v)
                    break
                except (TypeError, ValueError):
                    pass
        if tick is not None and step is not None:
            return (tick, step)
        return _get_tick_step(symbol, symbol_id)

    def _round_qty(self, symbol: str, qty: float) -> str:
        """Step-align a close quantity using dynamic or static step, enforce min qty.

        Uses round() (not floor) so reduce-only market closes send the nearest valid
        step rather than always undershooting — SoDEX caps fill at actual position size.
        """
        step = self._dynamic_step(symbol)
        min_qty = MIN_QTY.get(symbol, 0.0)
        # Also try dynamic minQty from symbol_info
        info = self.symbol_info.get(symbol, {})
        for k in ("minQty", "min_qty", "minQuantity", "min_quantity"):
            v = info.get(k)
            if v is not None:
                try:
                    min_qty = float(v)
                    break
                except (TypeError, ValueError):
                    pass
        if step <= 0:
            step = 0.01
        if step <= 0:
            step = 0.01
        rounded = round(qty / step) * step
        # Sub-step dust guard: rounding sent "0.000" → SoDEX -1 quantity is invalid.
        # Round up to one step; reduceOnly semantics cap fill at actual position size.
        if rounded <= 0 and qty > 0:
            rounded = step
        if min_qty > 0 and rounded < min_qty:
            rounded = min_qty
        dp = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
        return f"{rounded:.{dp}f}"

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
        Returns available cross-margin USD for the perps account.

        Mirrors PHANTOM's get_balance() pattern exactly (confirmed working):
          Primary  : GET /api/v1/perps/accounts/{addr}/state → data.av
          Fallback : GET /api/v1/perps/accounts/{addr}/balances → balances[].aw/av/wm/wb/available

        SoDEX uses short field names on perps endpoints ("av", "aw", "wm", "wb").
        We do NOT fall back to the spot account — spot is a separate account and may
        contain unrelated dust that would poison DrawdownManager sizing.
        """
        addr = address or self.config.sodex_account_id or self.config.account_id or ""
        base = "mainnet-gw.sodex.dev" if self.config.sodex_mainnet else "testnet-gw.sodex.dev"

        # Primary: /state endpoint — av = available cross-margin USD (same as PHANTOM).
        # Timeout 20s: mainnet-gw is occasionally slow (observed 15-25s response times).
        for _attempt in range(2):
            try:
                resp = await self.client.get(
                    f"https://{base}/api/v1/perps/accounts/{addr}/state",
                    timeout=20.0
                )
                d = resp.json()
                if d.get("code") == 0:
                    av = d.get("data", {}).get("av")
                    if av is not None:
                        v = float(av)
                        if v > 0:
                            logger.debug("balance_from_state", av=v)
                            return v
                break   # got a valid response, no retry needed
            except Exception:
                if _attempt == 0:
                    import asyncio as _aio
                    await _aio.sleep(2)   # brief pause before retry

        # Fallback: /balances endpoint — short field names used by SoDEX perps API
        for _attempt in range(2):
            try:
                resp = await self.client.get(
                    f"https://{base}/api/v1/perps/accounts/{addr}/balances",
                    timeout=20.0
                )
                d = resp.json()
                if d.get("code") == 0:
                    for entry in d.get("data", {}).get("balances", []):
                        if not isinstance(entry, dict):
                            continue
                        for k in ("aw", "av", "available", "wm", "wb"):
                            val = entry.get(k)
                            if val is not None:
                                try:
                                    v = float(val)
                                    if v > 0:
                                        logger.debug("balance_from_balances", field=k, value=v)
                                        return v
                                except (ValueError, TypeError):
                                    pass
                break
            except Exception as e:
                if _attempt == 0:
                    import asyncio as _aio
                    await _aio.sleep(2)
                else:
                    _emsg = str(e) or f"{type(e).__name__} (no message)"
                    logger.warning("balance_fetch_failed", error=_emsg, exc_type=type(e).__name__)

        logger.warning("balance_zero_or_unfunded", addr=addr[:12])
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

    async def fetch_symbol_mapping(self) -> None:
        """
        Populate symbol_id_map and symbol_info from GET /perps/markets/symbols at startup.
        Provides dynamic ID → symbol resolution so hardcoded _TICK_STEP IDs never
        drift out of sync when SoDEX adds or re-numbers markets.
        Safe to call multiple times — overwrites on refresh.
        """
        try:
            resp = await self.client.get(f"{self.base_url}/markets/symbols")
            if resp.status_code != 200:
                logger.warning("fetch_symbol_mapping_failed",
                               status=resp.status_code, body=resp.text[:200])
                return
            data = resp.json()
            markets = data if isinstance(data, list) else data.get("data", [])
            new_id_map: Dict[int, str] = {}
            new_info:   Dict[str, Any] = {}
            for m in markets:
                mid = m.get("id") or m.get("symbolID")
                sym = m.get("symbol") or m.get("name", "")
                if mid is not None and sym:
                    new_id_map[int(mid)] = sym
                    new_info[sym] = m
            self.symbol_id_map = new_id_map
            self.symbol_info   = new_info
            logger.info("symbol_mapping_loaded", count=len(new_id_map))
        except Exception as exc:
            logger.warning("fetch_symbol_mapping_error", error=str(exc))

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
            _t_pre = time.perf_counter()
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
            _t_pre_done = time.perf_counter()
            _pre_ms = round((_t_pre_done - _t_pre) * 1000, 1)

            # ── 1. Entry ─────────────────────────────────────────────────────────
            _m.t_entry_sent = time.time()
            entry_result = await self._place_entry_order(bracket)
            _t_entry_done = time.perf_counter()
            _entry_ms = round((_t_entry_done - _t_pre_done) * 1000, 1)
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
            _t_fill_done = time.perf_counter()
            _fill_ms = round((_t_fill_done - _t_entry_done) * 1000, 1)
            logger.info("fill_latency_breakdown",
                        symbol=bracket.candidate.symbol,
                        pre_size_ms=_pre_ms,
                        entry_post_ms=_entry_ms,
                        fill_wait_ms=_fill_ms,
                        total_to_fill_ms=round((_t_fill_done - _t_pre) * 1000, 1))

            # For limit orders the actual fill price ≈ entry_price (no slippage).
            # If the order result carries a fill price, use it.
            if entry_result.fill_price and entry_result.fill_price > 0:
                _m.actual_fill_price = entry_result.fill_price
            else:
                _m.actual_fill_price = bracket.candidate.entry_price

            # ── Fill size sync — exchange is source of truth ─────────────────────
            # Always sync candidate.size to the ACTUAL exchange position before TP
            # placement.  Three cases:
            #   overfill  (>5%): duplicate order / race — warn, sync to exchange size
            #   underfill (≥1%): significant partial — cancel unfilled remainder
            #   rounding  (<1%): SoDEX step rounding — log and sync silently
            _req_size = bracket.candidate.size
            if actual_size > 0 and actual_size != _req_size:
                if actual_size > _req_size * 1.05:
                    # Exchange filled MORE than requested — possible duplicate order
                    logger.warning(
                        "overfill_detected",
                        symbol=bracket.candidate.symbol,
                        requested=round(_req_size, 6),
                        filled=round(actual_size, 6),
                        overfill_pct=round((actual_size / _req_size - 1) * 100, 2),
                        note="exchange_is_source_of_truth_syncing_to_actual",
                    )
                elif actual_size < _req_size * 0.99:
                    # Significant partial fill — cancel remaining open qty
                    logger.warning(
                        "partial_fill_cancel_remainder",
                        symbol=bracket.candidate.symbol,
                        requested=round(_req_size, 6),
                        filled=round(actual_size, 6),
                        cancelled_remainder=round(_req_size - actual_size, 6),
                    )
                    await self._cleanup_orders([
                        (entry_result.order_id, bracket.candidate.symbol, bracket.account_id)
                    ])
                    placed_orders.clear()
                else:
                    # Exchange rounding (<1% off) — sync without cancelling
                    logger.info(
                        "fill_size_adjusted_rounding",
                        symbol=bracket.candidate.symbol,
                        requested=round(_req_size, 6),
                        actual=round(actual_size, 6),
                        delta_pct=round((1 - actual_size / _req_size) * 100, 4),
                        note="TP sizes will use actual exchange fill",
                    )
                bracket.candidate.size = actual_size  # exchange is always source of truth

            # ── Sub-minimum-close guard ──────────────────────────────────────────
            # If the filled size is below STEP_SIZES minimum, we cannot place a
            # TP or close this position via the exchange API — _round_qty would
            # produce "0.000" (before the round-up fix) or rely on reduceOnly cap.
            # Better to close immediately and not track the position at all than
            # to create a zombie that fires the stop guardian on every 0.5s tick.
            _min_close = STEP_SIZES.get(bracket.candidate.symbol, 0.01)
            if actual_size < _min_close:
                _close_qty_str = self._round_qty(bracket.candidate.symbol, _min_close)
                logger.warning(
                    "fill_below_min_closeable",
                    symbol=bracket.candidate.symbol,
                    actual_size=round(actual_size, 8),
                    min_closeable=_min_close,
                    close_qty=_close_qty_str,
                    note="immediate dust-close — position below minimum API step",
                )
                try:
                    await self.close_position_market(
                        symbol=bracket.candidate.symbol,
                        symbol_id=bracket.symbol_id,
                        account_id=bracket.account_id,
                        side=bracket.candidate.side,
                        size=_min_close,   # step qty; reduceOnly caps fill at actual
                    )
                except Exception as _dc_err:
                    logger.warning("dust_close_failed", symbol=bracket.candidate.symbol,
                                   error=str(_dc_err))
                metrics_logger.emit(_m)
                return BracketResult(
                    success=False,
                    entry_order_id=entry_result.order_id,
                    error=f"fill_below_min_closeable: {actual_size} < {_min_close}",
                )

            # ── 3. Stop — software-enforced (NOT placed on exchange) ────────────
            # Root cause of immediate closes: a SELL LIMIT below the current market
            # is a taker order — the exchange fills it instantly at the best bid
            # (price improvement).  e.g. SELL LIMIT @$98 when bids are @$100 fills
            # at $100, closing the position 2-3s after entry with a hairline loss.
            #
            # SoDEX native stop-trigger fields (stopPrice/stopType/triggerType) have
            # not been confirmed with valid values — stopType=0 raises "invalid".
            # Until the correct conditional-order format is documented, stops are
            # enforced by the software stop in execution_cleanup_loop (runs every 1s,
            # fires a market close when mark crosses pos.stop_price).
            # pos.stop_price is set from candidate and propagated through TP ratchets /
            # trailing stop so the software guardian always knows the current level.
            _m.t_stop_confirmed = time.time()
            _m.stop_placed = True  # software stop is active from this moment

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
                    error=f"tp_failed: {failed_tps[0].error}",
                )

            _m.tp_placed = True
            metrics_logger.emit(_m)  # full success — emit all timing data

            tp_order_ids = [r.order_id for r in tp_results]
            return BracketResult(
                success=True,
                entry_order_id=entry_result.order_id,
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
        Place TPs for an already-open position (no entry, no fill wait).
        Stop is software-enforced — NOT placed on exchange (see place_bracket for rationale).
        Used when place_bracket returned partial success (entry filled but TPs failed).
        """
        try:
            tp_results = await self._place_tp_orders(bracket)
            failed_tps = [r for r in tp_results if not r.success]
            if failed_tps:
                await self._cleanup_orders([
                    (r.order_id, bracket.candidate.symbol, bracket.account_id)
                    for r in tp_results if r.order_id and r.success
                ])
                return BracketResult(success=False, error=f"TP retry failed: {failed_tps[0].error}")

            tp_ids = [r.order_id for r in tp_results]
            logger.info("protective_tp_orders_placed",
                        symbol=bracket.candidate.symbol, tp_ids=tp_ids)
            return BracketResult(
                success=True,
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
                                    symbol=symbol,
                                    pre_size=round(pre_size, 6),
                                    current_size=round(size, 6),
                                    min_fill_threshold=round(target, 6))
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
        _t0 = time.perf_counter()

        nonce = self.nonce_manager.next_nonce()
        full_payload = {"type": action_type, "params": params}
        signature = self.signer.sign_payload(full_payload, nonce)
        _t1 = time.perf_counter()

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
        _t2 = time.perf_counter()

        logger.debug("http_breakdown",
                     action=action_type,
                     sign_ms=round((_t1 - _t0) * 1000, 1),
                     http_ms=round((_t2 - _t1) * 1000, 1),
                     total_ms=round((_t2 - _t0) * 1000, 1))
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
        """
        Place entry order.  Order type is driven by candidate.order_type (set by Kant/Nietzsche):
          "market" — IOC market fill: type=2, tif=3, no price field.  Fills in <1s.
          "probe"  — Aggressive limit at 0.1% inside mark: type=1, tif=3 (IOC), half size.
                     Fills fast when price moves toward it; cancels if not.
          "limit"  — GTC limit at entry_price (default, existing behaviour).
        """
        c = bracket.candidate
        _sym_clean = c.symbol.replace("-", "").replace("_", "")
        cl_ord_id = f"e{_sym_clean}{int(c.timestamp_ms)}"
        side = 1 if c.side == "long" else 2
        tick, step = self.get_tick_step(bracket.candidate.symbol, bracket.symbol_id)
        _order_type_str = getattr(c, "order_type", "limit")

        # ── Probe: half-size, aggressive limit, IOC ─────────────────────────────
        if _order_type_str == "probe":
            # 0.1% inside ask (long) / bid (short) — fills fast, cancels if not
            _probe_price = (c.entry_price * 0.999 if c.side == "long"
                            else c.entry_price * 1.001)
            probe_qty_str = _round_qty(c.size * 0.50, step)  # half size
            if float(probe_qty_str) <= 0:
                probe_qty_str = _round_qty(c.size, step)     # fallback: full size
            qty_str   = probe_qty_str
            qty_float = float(qty_str)
            price_str = _round_price(_probe_price, tick)
            order_type_int = 1   # limit
            tif_int        = 3   # IOC — cancel unfilled portion immediately
            logger.info("entry_probe_order", symbol=c.symbol,
                        price=price_str, qty=qty_str, half_size=True)
        else:
            qty_str   = _round_qty(c.size, step)
            qty_float = float(qty_str)
            price_str = _round_price(c.entry_price, tick) if _order_type_str != "market" else None
            order_type_int = 2 if _order_type_str == "market" else 1  # 2=MARKET, 1=LIMIT
            tif_int        = 3 if _order_type_str == "market" else 1  # IOC for market, GTC for limit

        qty_float = float(qty_str)
        # Pre-flight: zero quantity or dust notional → reject before hitting exchange.
        # SoDEX rejects qty=0 with code:-1 "unknown"; catch it here to avoid burning
        # a per-symbol cooldown on a sizing bug.
        if qty_float <= 0:
            logger.error("entry_order_zero_qty",
                         symbol=c.symbol, size=c.size, step=step, qty_str=qty_str)
            return OrderResult(order_id="", status="rejected",
                               fill_price=None, fill_qty=None,
                               error="SoDEX error -1: zero_quantity_after_step_rounding")
        if price_str is not None:
            notional = qty_float * float(price_str)
            if notional < 10.0:
                # Micro-mode: _round_qty floored us just under the notional floor.
                # Bump up by one step so SoDEX accepts the order.
                _bumped = qty_float + step
                _bumped_str = f"{_bumped:.{max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0}f}"
                _bumped_notional = float(_bumped_str) * float(price_str)
                if _bumped_notional >= 10.0:
                    logger.warning("entry_order_notional_bumped",
                                   symbol=c.symbol, qty_before=qty_str, qty_after=_bumped_str,
                                   notional_before=round(notional, 2), notional_after=round(_bumped_notional, 2))
                    qty_str = _bumped_str
                    qty_float = float(qty_str)
                else:
                    logger.error("entry_order_dust_notional",
                                 symbol=c.symbol, qty=qty_str, price=price_str,
                                 notional=round(notional, 2), min_notional=10.0)
                    return OrderResult(order_id="", status="rejected",
                                       fill_price=None, fill_qty=None,
                                       error=f"SoDEX error -1: notional_{notional:.2f}_below_10usd_minimum")

        order_item = self._build_order_item(
            cl_ord_id=cl_ord_id,
            side=side,
            order_type=order_type_int,
            tif=tif_int,
            quantity=qty_str,
            price=price_str,        # None for market orders — omitted from payload
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
        tick, step = self.get_tick_step(bracket.candidate.symbol, bracket.symbol_id)

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
            quantity=_round_qty(c.size, step, reduce_only=True),
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
        Place TP1 (50%), TP2 (30%), TP3 (20%) limit orders as THREE individual requests.

        SoDEX rejects batched newOrder with multiple items at the top level (code:-1).
        Serial placement is ~150-600ms slower than batching, but it actually works.
        We fire TP1 first (highest priority), then TP2/TP3 in parallel to claw back latency.
        """
        c = bracket.candidate

        # Guard: all TP prices zero → position was reconciled without TP data.
        if c.tp1_price <= 0 and c.tp2_price <= 0 and c.tp3_price <= 0:
            logger.warning("tp_prices_zero_skip", symbol=c.symbol,
                           entry=c.entry_price, side=c.side)
            return [
                OrderResult(order_id="", status="rejected",
                            fill_price=None, fill_qty=None,
                            error="tp_prices_zero")
                for _ in range(3)
            ]

        side = 2 if c.side == "long" else 1  # opposite of position side
        _sym_clean = c.symbol.replace("-", "").replace("_", "")
        tick, step = self.get_tick_step(bracket.candidate.symbol, bracket.symbol_id)

        # Compute TP quantities so their sum never exceeds position size.
        tp1_qty = float(_round_qty(c.size * 0.5, step, reduce_only=True))
        tp2_qty = float(_round_qty(c.size * 0.3, step, reduce_only=True))
        tp3_raw = c.size - tp1_qty - tp2_qty
        tp3_qty = float(_round_qty(tp3_raw, step, reduce_only=True))
        if tp3_qty <= 0 and tp3_raw > 0:
            tp3_qty = step
        tp_qtys = [tp1_qty, tp2_qty, tp3_qty]
        if sum(tp_qtys) > c.size + 1e-12:
            tp_qtys[2] = max(0.0, c.size - tp_qtys[0] - tp_qtys[1])
            tp_qtys[2] = math.floor(tp_qtys[2] / step) * step
        for i in range(3):
            if tp_qtys[i] > 0 and tp_qtys[i] < step:
                tp_qtys[i] = step
        if sum(tp_qtys) > c.size + 1e-12:
            excess = sum(tp_qtys) - c.size
            for i in reversed(range(3)):
                if tp_qtys[i] >= excess + step:
                    tp_qtys[i] = math.floor((tp_qtys[i] - excess) / step) * step
                    break

        tp_prices = [c.tp1_price, c.tp2_price, c.tp3_price]

        async def _place_one(idx: int) -> OrderResult:
            cl_ord_id = f"tp{idx+1}{_sym_clean}{int(c.timestamp_ms)}"
            qty_str = _round_qty(tp_qtys[idx], step, reduce_only=True)
            price_str = _round_price(tp_prices[idx], tick)
            params = {
                "accountID": int(bracket.account_id),
                "symbolID": bracket.symbol_id,
                "orders": [self._build_order_item(
                    cl_ord_id=cl_ord_id,
                    side=side,
                    order_type=1,   # LIMIT
                    tif=1,          # GTC
                    quantity=qty_str,
                    price=price_str,
                    reduce_only=True,
                )],
            }
            return await self.place_order(params)

        # TP1 is highest-conviction (50% size) — place first, wait for result.
        r1 = await _place_one(0)
        # TP2 + TP3 can race in parallel; reduceOnly caps fill safely.
        r2, r3 = await asyncio.gather(_place_one(1), _place_one(2))
        return [r1, r2, r3]

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
        tick, step = self.get_tick_step(symbol, symbol_id)
        stop_side = 2 if side == "long" else 1   # opposite direction
        _sym_clean = symbol.replace("-", "").replace("_", "")
        cl_ord_id = f"ts{_sym_clean}{int(time.time() * 1000)}"

        order_item = self._build_order_item(
            cl_ord_id=cl_ord_id,
            side=stop_side,
            order_type=1,   # LIMIT
            tif=1,          # GTC
            quantity=_round_qty(size, step, reduce_only=True),
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

        Quantity strategy for reduce-only close:
          Use STEP_SIZES-based rounding (round, not floor) so the quantity is aligned
          to the exchange step increment.  With reduceOnly=True, SoDEX caps the fill
          at the actual position size — a slight overshoot is harmless.
          e.g. AAPL 0.40172155 → step=0.01 → "0.40"  ✓
               TSLA 0.17710484 → step=0.01 → "0.18"  ✓
               ETH  0.03672912 → step=0.001 → "0.037" ✓
               OP   797.3      → step=10.0  → "800"   ✓
        """
        close_side = 2 if side == "long" else 1   # opposite of position side
        _sym_clean = symbol.replace("-", "").replace("_", "")
        cl_ord_id = f"tc{_sym_clean}{int(time.time() * 1000)}"  # tc = time-close

        # Step-aligned quantity — always valid for SoDEX.
        quantity_str = self._round_qty(symbol, size)

        order_item = self._build_order_item(
            cl_ord_id=cl_ord_id,
            side=close_side,
            order_type=2,   # MARKET
            tif=3,          # IOC — fill immediately or cancel
            quantity=quantity_str,
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
                    quantity_str=quantity_str,
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
        tick, step = self.get_tick_step(symbol, symbol_id)
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
        tick, step = self.get_tick_step(symbol, symbol_id)
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
