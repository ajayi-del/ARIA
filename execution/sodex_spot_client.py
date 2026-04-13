"""
SoDEX Spot REST Client — ARIA v1.4

Spot endpoint: https://mainnet-gw.sodex.dev/api/v1/spot
Spot symbols use format: vBTC_vUSDC

CRITICAL: Spot EIP-712 domain uses name="spot" not "futures".
The perps signer module-level constant hardcodes "futures" so
we recompute domain separator here with "spot".

Auth headers for spot: X-API-Key / X-API-Sign / X-API-Nonce
(identical to perps — same SoDEX gateway pattern).
"""

import json
import math
import time
import asyncio
import httpx
import certifi
import structlog
from typing import Dict, Any, Optional
from web3 import Web3
from eth_account import Account
from eth_account.messages import SignableMessage

from core.config import Settings

log = structlog.get_logger(__name__)

# ── Spot REST base ────────────────────────────────────────────────────────────
SPOT_REST = "https://mainnet-gw.sodex.dev/api/v1/spot"

# ── Perp → spot symbol mapping ────────────────────────────────────────────────
PERP_TO_SPOT: Dict[str, str] = {
    "BTC-USD":  "vBTC_vUSDC",
    "ETH-USD":  "vETH_vUSDC",
    "SOL-USD":  "vSOL_vUSDC",
    "XAUT-USD": "vXAUT_vUSDC",
    "BNB-USD":  "vBNB_vUSDC",
    "LINK-USD": "vLINK_vUSDC",
    "AVAX-USD": "vAVAX_vUSDC",
}

# ── EIP-712 constants (shared with perps signer) ──────────────────────────────
_CHAIN_ID = 286623
_DOMAIN_TYPE_HASH: bytes = Web3.keccak(
    text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
_ACTION_TYPE_HASH: bytes = Web3.keccak(
    text="ExchangeAction(bytes32 payloadHash,uint64 nonce)"
)

# ── SPOT domain separator — "spot" not "futures" ──────────────────────────────
_SPOT_DOMAIN_SEP: bytes = Web3.keccak(
    _DOMAIN_TYPE_HASH
    + Web3.keccak(text="spot")           # ← THIS is the only difference from perps
    + Web3.keccak(text="1")
    + _CHAIN_ID.to_bytes(32, "big")
    + b"\x00" * 32
)


class SoDEXSpotSigner:
    """
    EIP-712 signer for SoDEX SPOT domain.
    MUST use 'spot' domain separator — not 'futures'.
    Wrong domain = signature verification failure on every order.
    """

    def __init__(self, private_key: str):
        self.private_key = private_key
        self._address = Account.from_key(private_key).address

    def sign_payload(self, payload: Dict[str, Any], nonce: int) -> str:
        """
        EIP-712 sign a SoDEX spot payload.
        Uses _SPOT_DOMAIN_SEP (name="spot").

        Args:
            payload: full payload dict {"type": "newOrder", "params": {...}}
            nonce:   millisecond-precision uint64 nonce

        Returns "0x01<130 hex chars>" for X-API-Sign header.
        """
        payload_json = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        payload_hash = Web3.keccak(payload_json)

        struct_encoded = (
            _ACTION_TYPE_HASH
            + payload_hash
            + nonce.to_bytes(32, "big")
        )
        struct_hash = Web3.keccak(struct_encoded)

        signable = SignableMessage(
            version=b"\x01",
            header=_SPOT_DOMAIN_SEP,   # ← spot domain, not futures
            body=struct_hash,
        )
        signed = Account.sign_message(signable, self.private_key)

        sig_bytes = bytearray(signed.signature)
        if sig_bytes[-1] >= 27:
            sig_bytes[-1] -= 27

        return "0x01" + bytes(sig_bytes).hex()

    def get_address(self) -> str:
        return self._address


def _round_spot_price(price: float, tick: float) -> str:
    """Round spot price to tick, return as string."""
    ticks = round(price / tick)
    rounded = ticks * tick
    dp = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return f"{rounded:.{dp}f}"


def _round_spot_qty(qty: float, step: float) -> str:
    """Floor spot quantity to step, return as string."""
    floored = math.floor(qty / step) * step
    dp = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return f"{floored:.{dp}f}"


# Spot market precision: (tick_size, step_size) per symbol_id.
# Populated after discover_spot_symbols() runs.
_SPOT_TICK_STEP: Dict[str, tuple] = {
    "vBTC_vUSDC":  (0.5,   0.001),
    "vETH_vUSDC":  (0.05,  0.01),
    "vSOL_vUSDC":  (0.01,  0.1),
    "vXAUT_vUSDC": (0.1,   0.001),
    "vBNB_vUSDC":  (0.01,  0.01),
    "vLINK_vUSDC": (0.001, 0.1),
    "vAVAX_vUSDC": (0.01,  0.1),
}


class SoDEXSpotClient:
    """
    SoDEX Spot REST client.

    Handles:
      - Symbol discovery (spot symbol IDs from /markets/symbols)
      - Price queries (best bid/ask from /markets/bookTickers)
      - Order placement (batch endpoint /trade/orders/batch)

    Used by TrueDeltaNeutralArb for the spot leg.
    """

    def __init__(self, config: Settings):
        self.config = config
        self._http = httpx.AsyncClient(
            base_url=SPOT_REST,
            verify=certifi.where(),
            timeout=10.0,
        )
        self._symbol_ids: Dict[str, int] = {}   # spot_symbol → id
        self._nonce = int(time.time() * 1000)
        self.signer = SoDEXSpotSigner(config.sodex_private_key or config.private_key)
        self._api_key = self.signer.get_address()
        # Numeric account ID (aid) — set to 0 until set_account_id() is called after
        # NUMERIC_ACCOUNT_ID is resolved at startup. The hex wallet address cannot be
        # cast to int; the aid is a separate uint64 assigned by SoDEX on first deposit.
        self._account_id: int = 0

    def set_account_id(self, account_id: int) -> None:
        """
        Set the numeric account ID (aid) after it is resolved at startup.
        Must be called before placing any spot orders.
        """
        self._account_id = account_id
        log.debug("spot_client_account_id_set", account_id=account_id)

    def _next_nonce(self) -> int:
        self._nonce = max(int(time.time() * 1000), self._nonce + 1)
        return self._nonce

    async def discover_spot_symbols(self) -> Dict[str, int]:
        """
        Fetch /markets/symbols and build spot_symbol → symbol_id mapping.
        Called once at startup.
        """
        try:
            resp = await self._http.get("/markets/symbols")
            data = resp.json()
            found: Dict[str, int] = {}
            for item in data.get("data", []):
                sym = item.get("symbol", "") or item.get("name", "")
                sid = int(item.get("symbolID", item.get("id", 0)))
                if sym in PERP_TO_SPOT.values():
                    found[sym] = sid
            self._symbol_ids = found
            log.info("spot_symbols_discovered",
                     symbols=found,
                     count=len(found))
            return found
        except Exception as e:
            log.error("spot_discovery_error", error=str(e))
            return {}

    async def get_spot_price(self, perp_symbol: str) -> float:
        """
        Get best mid price for a perp symbol's spot equivalent.
        Uses /markets/bookTickers for tightest bid/ask.
        Falls back to /markets/tickers (lastPrice) on error.
        """
        spot_sym = PERP_TO_SPOT.get(perp_symbol, "")
        if not spot_sym:
            return 0.0
        try:
            resp = await self._http.get(
                "/markets/bookTickers",
                params={"symbol": spot_sym}
            )
            data = resp.json()
            items = data.get("data", [])
            if items:
                bid = float(items[0].get("bidPrice", items[0].get("b", 0)) or 0)
                ask = float(items[0].get("askPrice", items[0].get("a", 0)) or 0)
                if bid > 0 and ask > 0:
                    return (bid + ask) / 2.0
                if bid > 0:
                    return bid
            # Fallback to ticker
            resp2 = await self._http.get(
                "/markets/tickers",
                params={"symbol": spot_sym}
            )
            d2 = resp2.json()
            items2 = d2.get("data", [])
            if items2:
                return float(
                    items2[0].get("lastPrice", items2[0].get("c", 0)) or 0
                )
        except Exception as e:
            log.warning("spot_price_error", symbol=perp_symbol, error=str(e))
        return 0.0

    async def get_spot_bid_ask(self, perp_symbol: str) -> tuple[float, float]:
        """
        Returns (bid, ask) for spot symbol.
        Used for taker entry pricing.
        """
        spot_sym = PERP_TO_SPOT.get(perp_symbol, "")
        if not spot_sym:
            return 0.0, 0.0
        try:
            resp = await self._http.get(
                "/markets/bookTickers",
                params={"symbol": spot_sym}
            )
            data = resp.json()
            items = data.get("data", [])
            if items:
                bid = float(items[0].get("bidPrice", items[0].get("b", 0)) or 0)
                ask = float(items[0].get("askPrice", items[0].get("a", 0)) or 0)
                return bid, ask
        except Exception as e:
            log.warning("spot_bid_ask_error", symbol=perp_symbol, error=str(e))
        return 0.0, 0.0

    async def place_spot_order(
        self,
        perp_symbol: str,
        side: str,         # "buy" or "sell"
        quantity: float,
        price: float,
    ) -> dict:
        """
        Place a single spot LIMIT order via the batch endpoint.

        Args:
            perp_symbol: "BTC-USD", "SOL-USD", etc.
            side: "buy" (long spot) or "sell" (sell spot)
            quantity: asset quantity
            price: limit price (USD)

        Returns dict with "code" (0=success) and "cl_ord_id" on success.

        IMPORTANT: Uses spot EIP-712 domain (name="spot").
        Wrong domain → code:-1 rejected.
        """
        spot_sym = PERP_TO_SPOT.get(perp_symbol, "")
        if not spot_sym:
            log.error("spot_order_unknown_symbol", perp_symbol=perp_symbol)
            return {"code": -1, "error": "unknown_symbol"}

        symbol_id = self._symbol_ids.get(spot_sym, 0)
        if symbol_id == 0:
            log.warning("spot_symbol_id_missing",
                        spot_sym=spot_sym,
                        note="run discover_spot_symbols() at startup")
            return {"code": -1, "error": "no_symbol_id"}

        tick, step = _SPOT_TICK_STEP.get(spot_sym, (0.01, 0.01))
        side_int = 1 if side == "buy" else 2
        nonce = self._next_nonce()
        sym_short = perp_symbol.replace("-", "")[:6].lower()
        cl_ord_id = f"sp{sym_short}{nonce}"[:36]  # max 36 chars, pattern [0-9a-zA-Z_-]

        # Batch order params — accountID at top level, each order has symbolID
        order_item = {
            "symbolID": symbol_id,
            "clOrdID": cl_ord_id,
            "side": side_int,
            "type": 1,          # LIMIT
            "timeInForce": 1,   # GTC
            "price": _round_spot_price(price, tick),
            "quantity": _round_spot_qty(quantity, step),
        }
        params = {
            "accountID": self._account_id,
            "orders": [order_item],
        }

        full_payload = {"type": "newOrder", "params": params}
        signature = self.signer.sign_payload(full_payload, nonce)

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-Key": self._api_key,
            "X-API-Sign": signature,
            "X-API-Nonce": str(nonce),
        }

        try:
            resp = await self._http.post(
                "/trade/orders/batch",
                json=params,
                headers=headers,
            )
            result = resp.json()

            # Batch returns array; extract first item
            if isinstance(result, list):
                item = result[0] if result else {}
            elif isinstance(result, dict) and "data" in result:
                data_list = result.get("data", [])
                item = data_list[0] if data_list else result
            else:
                item = result

            code = item.get("code", result.get("code", -1))
            log.info("spot_order_placed",
                     symbol=perp_symbol,
                     spot_symbol=spot_sym,
                     side=side,
                     quantity=quantity,
                     price=price,
                     code=code,
                     cl_ord_id=cl_ord_id)

            if code == 0:
                return {
                    "code": 0,
                    "cl_ord_id": cl_ord_id,
                    "order_id": item.get("orderID", ""),
                }
            return {"code": code, "error": item.get("error", "rejected")}

        except Exception as e:
            log.error("spot_order_exception",
                      symbol=perp_symbol, error=str(e))
            return {"code": -1, "error": str(e)}

    async def cancel_spot_order(
        self,
        perp_symbol: str,
        cl_ord_id: str,
    ) -> bool:
        """Cancel a spot order by client order ID."""
        spot_sym = PERP_TO_SPOT.get(perp_symbol, "")
        symbol_id = self._symbol_ids.get(spot_sym, 0)
        if not symbol_id:
            return False

        nonce = self._next_nonce()
        cancel_item = {
            "symbolID": symbol_id,
            "clOrdID": cl_ord_id,
        }
        params = {
            "accountID": self._account_id,
            "orders": [cancel_item],
        }
        full_payload = {"type": "cancelOrder", "params": params}
        signature = self.signer.sign_payload(full_payload, nonce)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-Key": self._api_key,
            "X-API-Sign": signature,
            "X-API-Nonce": str(nonce),
        }
        try:
            resp = await self._http.request(
                "DELETE",
                "/trade/orders/batch",
                json=params,
                headers=headers,
            )
            return resp.status_code in (200, 204)
        except Exception as e:
            log.warning("spot_cancel_error", cl_ord_id=cl_ord_id, error=str(e))
            return False

    async def fetch_fee_rate(self, address: str = "", symbol: str = "") -> dict:
        """
        GET /accounts/{userAddress}/fee-rate
        Returns live maker/taker rates from SoDEX (weight=2).

        Rate includes tier, staking tier, and optional symbol-level discount.
        Returns dict with keys: makerFeeRate, takerFeeRate, tier, stakingTier
        (all as floats, 0.0 on failure).
        """
        addr = address or self._api_key
        params = {}
        if symbol:
            params["symbol"] = symbol
        try:
            resp = await self._http.get(
                f"/accounts/{addr}/fee-rate",
                params=params or None,
                timeout=8.0,
            )
            data = resp.json()
            if data.get("code") == 0:
                fee_data = data.get("data", {})
                result = {
                    "makerFeeRate": float(fee_data.get("makerFeeRate", 0) or 0),
                    "takerFeeRate": float(fee_data.get("takerFeeRate", 0) or 0),
                    "tier":         int(fee_data.get("tier", fee_data.get("feeTier", 0)) or 0),
                    "stakingTier":  int(fee_data.get("stakingTier", 0) or 0),
                }
                log.debug("spot_fee_rate_fetched", **{k: f"{v:.6f}" if isinstance(v, float) else v
                                                      for k, v in result.items()})
                return result
        except Exception as e:
            _emsg = str(e) or f"{type(e).__name__} (no message)"
            log.warning("spot_fee_rate_fetch_failed", error=_emsg)
        return {"makerFeeRate": 0.0, "takerFeeRate": 0.0, "tier": 0, "stakingTier": 0}

    async def get_spot_balance(self, address: str = "") -> float:
        """
        GET /api/v1/spot/accounts/{address}/balances
        Returns the USDC available balance in the spot account.
        SoDEX spot and perps balances are INDEPENDENT — this must be
        queried separately from the perps balance in sodex_client.py.
        """
        addr = address or self._api_key  # falls back to signing key address
        # Two attempts with 20s timeout — mainnet-gw occasionally slow (15-25s observed).
        for _attempt in range(2):
            try:
                resp = await self._http.get(
                    f"/accounts/{addr}/balances",
                    timeout=20.0,
                )
                data = resp.json()
                if data.get("code") == 0:
                    bal_list = (
                        data.get("data", {}).get("balances", [])
                        or data.get("data", {}).get("B", [])
                    )
                    for item in bal_list:
                        if not isinstance(item, dict):
                            continue
                        asset = item.get("asset", item.get("a", "")).upper()
                        if asset not in ("USDC", "VUSDC", ""):
                            continue
                        for field in ("available", "availableBalance", "equity", "total", "a"):
                            v = item.get(field)
                            if v is not None:
                                try:
                                    f = float(v)
                                    if f > 0:
                                        log.debug("spot_balance_fetched", available=f, address=addr)
                                        return f
                                except (ValueError, TypeError):
                                    pass
                log.debug("spot_balance_zero_or_empty", code=data.get("code"), address=addr)
                return 0.0
            except Exception as e:
                if _attempt == 0:
                    import asyncio as _aio
                    await _aio.sleep(2)
                else:
                    _emsg = str(e) or f"{type(e).__name__} (no message)"
                    log.warning("spot_balance_fetch_failed", error=_emsg, exc_type=type(e).__name__)
        return 0.0

    async def close(self):
        await self._http.aclose()
