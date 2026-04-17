"""
sovereign/spot_executor.py — Spot order execution for SSI index tokens.

Architecture
────────────
All SSI token spot orders go through SovereignSpotExecutor.
No other component in ARIA may place spot orders for SSI tokens.

Execution rules (hard rules from sovereign/__init__.py):
  - SELL orders execute BEFORE buy orders in every rebalance
  - Within sell pass: MEME sells FIRST (priority 9 → sorted descending → first)
  - Within buy pass:  MEME buys LAST  (priority 9 → sorted ascending → last)
  - Uses SoDEX spot EIP-712 domain (name="spot", chain ID 286623)
  - SSI tokens have no 'v' prefix — symbol format: "MAG7SSI_USDC" not "vMAG7SSI_vUSDC"
  - Aggressive limit orders (taker): buy at ask+0.5 tick, sell at bid-0.5 tick

Symbol mapping for SoDEX spot REST API:
  "MAG7SSI-USD" → "MAG7SSI_USDC"   (set by SoDEX; spot_ws_symbol from config)
  "DEFISSI-USD" → "DEFISSI_USDC"
  "MEMESSI-USD" → "MEMESSI_USDC"
  "USSI-USD"    → "USSI_USDC"
"""

from __future__ import annotations

import json
import math
import time
import asyncio
import httpx
import certifi
import structlog
from dataclasses import dataclass
from typing import Dict, List, Optional

from web3 import Web3
from eth_account import Account
from eth_account.messages import SignableMessage

from core.config import Settings

log = structlog.get_logger(__name__)

# ── SoDEX REST endpoint (spot) ────────────────────────────────────────────────
_SPOT_REST = "https://mainnet-gw.sodex.dev/api/v1/spot"

# ── EIP-712 constants (spot domain — "spot" NOT "futures") ───────────────────
_CHAIN_ID = 286623
_DOMAIN_TYPE_HASH: bytes = Web3.keccak(
    text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
_ACTION_TYPE_HASH: bytes = Web3.keccak(
    text="ExchangeAction(bytes32 payloadHash,uint64 nonce)"
)
_SPOT_DOMAIN_SEP: bytes = Web3.keccak(
    _DOMAIN_TYPE_HASH
    + Web3.keccak(text="spot")      # ← MUST be "spot", not "futures"
    + Web3.keccak(text="1")
    + _CHAIN_ID.to_bytes(32, "big")
    + b"\x00" * 32
)

# ── SSI spot symbol mapping ───────────────────────────────────────────────────
# Maps ARIA symbol → SoDEX spot trading symbol (no 'v' prefix for SSI tokens)
SSI_SPOT_SYMBOLS: Dict[str, str] = {
    "MAG7SSI-USD": "MAG7SSI_USDC",
    "DEFISSI-USD": "DEFISSI_USDC",
    "MEMESSI-USD": "MEMESSI_USDC",
    "USSI-USD":    "USSI_USDC",
}

# Price/quantity precision per SSI spot symbol
# (tick_size, step_size) — verified on first discovery run
_SSI_TICK_STEP: Dict[str, tuple] = {
    "MAG7SSI_USDC": (0.0001, 0.01),
    "DEFISSI_USDC": (0.0001, 0.01),
    "MEMESSI_USDC": (0.0001, 0.01),
    "USSI_USDC":    (0.0001, 0.01),
}

# Max retries for failed spot orders
_MAX_RETRIES = 2
_RETRY_DELAY_S = 2.0


@dataclass
class OrderResult:
    symbol:       str
    side:         str
    quantity:     float
    price:        float
    cl_ord_id:    str
    order_id:     str
    success:      bool
    code:         int
    error:        str = ""


class _SpotSigner:
    """EIP-712 signer for SoDEX SPOT domain."""

    def __init__(self, private_key: str) -> None:
        self._pk      = private_key
        self._address = Account.from_key(private_key).address

    def sign(self, payload: dict, nonce: int) -> str:
        payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
        payload_hash  = Web3.keccak(payload_bytes)
        struct_enc    = (
            _ACTION_TYPE_HASH
            + payload_hash
            + nonce.to_bytes(32, "big")
        )
        struct_hash   = Web3.keccak(struct_enc)
        signable      = SignableMessage(
            version=b"\x01",
            header=_SPOT_DOMAIN_SEP,
            body=struct_hash,
        )
        signed    = Account.sign_message(signable, self._pk)
        sig_bytes = bytearray(signed.signature)
        if sig_bytes[-1] >= 27:
            sig_bytes[-1] -= 27
        return "0x01" + bytes(sig_bytes).hex()

    @property
    def address(self) -> str:
        return self._address


class SovereignSpotExecutor:
    """
    Sovereign-exclusive SSI spot order executor.

    Only SovereignAgent instantiates this class. It handles:
      - Symbol ID discovery from SoDEX /markets/symbols
      - Aggressive limit order placement (taker pricing)
      - Rebalance execution (sells first, buys second, MEME sequencing)
      - Retry logic for transient failures
    """

    def __init__(self, config: Settings) -> None:
        self.config     = config
        self._signer    = _SpotSigner(config.sodex_private_key or config.private_key)
        self._api_key   = self._signer.address
        self._account_id: int = 0
        self._symbol_ids: Dict[str, int] = {}
        self._nonce: int = int(time.time() * 1000)
        self._http = httpx.AsyncClient(
            base_url=_SPOT_REST,
            verify=certifi.where(),
            timeout=15.0,
        )

    def set_account_id(self, account_id: int) -> None:
        self._account_id = account_id

    def _next_nonce(self) -> int:
        self._nonce = max(int(time.time() * 1000), self._nonce + 1)
        return self._nonce

    # ── Symbol discovery ──────────────────────────────────────────────────────

    async def discover_ssi_symbols(self) -> Dict[str, int]:
        """
        Fetch /markets/symbols and build SSI spot_symbol → symbol_id mapping.
        Called once at Sovereign startup.
        """
        try:
            resp = await self._http.get("/markets/symbols")
            data = resp.json()
            found: Dict[str, int] = {}
            for item in data.get("data", []):
                sym = str(item.get("symbol", "") or item.get("name", ""))
                sid = int(item.get("symbolID", item.get("id", 0)))
                if sym in SSI_SPOT_SYMBOLS.values():
                    found[sym] = sid
            self._symbol_ids = found
            log.info(
                "sovereign_spot_symbols_discovered",
                found=found,
                count=len(found),
            )
            return found
        except Exception as e:
            log.error("sovereign_spot_discovery_error", error=str(e))
            return {}

    # ── Price query ───────────────────────────────────────────────────────────

    async def get_best_price(self, aria_sym: str, side: str) -> float:
        """
        Get taker-aggressive price for an SSI token.
        Buy → ask + 0.5 tick (cross the spread)
        Sell → bid - 0.5 tick (lift with small buffer)
        """
        spot_sym = SSI_SPOT_SYMBOLS.get(aria_sym, "")
        if not spot_sym:
            return 0.0

        tick, _ = _SSI_TICK_STEP.get(spot_sym, (0.0001, 0.01))

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
                if side == "buy" and ask > 0:
                    return ask + 0.5 * tick
                elif side == "sell" and bid > 0:
                    return max(bid - 0.5 * tick, tick)
        except Exception as e:
            log.warning("sovereign_spot_price_error", symbol=aria_sym, error=str(e))

        return 0.0

    # ── Single order ──────────────────────────────────────────────────────────

    async def place_ssi_order(
        self,
        aria_sym: str,
        side: str,
        quantity: float,
        price: float,
    ) -> OrderResult:
        """
        Place a single SSI spot LIMIT order (GTC, taker-aggressive pricing).

        Args:
            aria_sym: "MAG7SSI-USD", "DEFISSI-USD", etc.
            side:     "buy" or "sell"
            quantity: token quantity
            price:    limit price (aggressive: ask+tick for buy, bid-tick for sell)
        """
        spot_sym = SSI_SPOT_SYMBOLS.get(aria_sym, "")
        if not spot_sym:
            return OrderResult(
                symbol=aria_sym, side=side, quantity=quantity, price=price,
                cl_ord_id="", order_id="", success=False, code=-1,
                error=f"unknown_symbol:{aria_sym}",
            )

        symbol_id = self._symbol_ids.get(spot_sym, 0)
        if symbol_id == 0:
            return OrderResult(
                symbol=aria_sym, side=side, quantity=quantity, price=price,
                cl_ord_id="", order_id="", success=False, code=-1,
                error=f"no_symbol_id:{spot_sym} — run discover_ssi_symbols() first",
            )

        tick, step = _SSI_TICK_STEP.get(spot_sym, (0.0001, 0.01))
        side_int   = 1 if side == "buy" else 2
        nonce      = self._next_nonce()
        sym_tag    = aria_sym.replace("-USD", "").replace("SSI", "ssi")[:8].lower()
        cl_ord_id  = f"sv{sym_tag}{nonce}"[:36]

        order_item = {
            "symbolID":  symbol_id,
            "clOrdID":   cl_ord_id,
            "side":      side_int,
            "type":      1,   # LIMIT
            "timeInForce": 1, # GTC
            "price":    _fmt_price(price, tick),
            "quantity": _fmt_qty(quantity, step),
        }
        params = {
            "accountID": self._account_id,
            "orders":    [order_item],
        }
        full_payload = {"type": "newOrder", "params": params}
        signature    = self._signer.sign(full_payload, nonce)

        headers = {
            "Content-Type": "application/json",
            "Accept":        "application/json",
            "X-API-Key":     self._api_key,
            "X-API-Sign":    signature,
            "X-API-Nonce":   str(nonce),
        }

        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp   = await self._http.post("/trade/orders/batch", json=params, headers=headers)
                result = resp.json()

                if isinstance(result, list):
                    item = result[0] if result else {}
                elif isinstance(result, dict) and "data" in result:
                    lst  = result.get("data", [])
                    item = lst[0] if lst else result
                else:
                    item = result

                code = item.get("code", result.get("code", -1))

                if code == 0:
                    log.info(
                        "sovereign_spot_order_placed",
                        symbol=aria_sym, side=side,
                        quantity=quantity, price=price, code=code,
                    )
                    return OrderResult(
                        symbol=aria_sym, side=side, quantity=quantity, price=price,
                        cl_ord_id=cl_ord_id,
                        order_id=str(item.get("orderID", "")),
                        success=True, code=0,
                    )

                err = item.get("error", f"code:{code}")
                log.warning(
                    "sovereign_spot_order_rejected",
                    symbol=aria_sym, side=side, code=code, error=err, attempt=attempt,
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_DELAY_S)

            except Exception as e:
                log.error(
                    "sovereign_spot_order_exception",
                    symbol=aria_sym, side=side, attempt=attempt, error=str(e),
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_DELAY_S)

        return OrderResult(
            symbol=aria_sym, side=side, quantity=quantity, price=price,
            cl_ord_id=cl_ord_id, order_id="", success=False, code=-1,
            error="max_retries_exceeded",
        )

    # ── Rebalance execution ───────────────────────────────────────────────────

    async def execute_rebalance(
        self,
        orders: "List[RebalanceOrder]",
    ) -> List[OrderResult]:
        """
        Execute a full rebalance:
          1. SELL pass (priority descending so MEME=9 is first)
          2. BUY pass  (priority ascending so MEME=9 is last)

        Prices are fetched fresh immediately before each order.
        """
        from sovereign.portfolio import RebalanceOrder

        sells = sorted(
            [o for o in orders if o.side == "sell"],
            key=lambda o: o.priority,
            reverse=True,   # MEME (9) first
        )
        buys = sorted(
            [o for o in orders if o.side == "buy"],
            key=lambda o: o.priority,
            reverse=False,  # MEME (9) last
        )

        results: List[OrderResult] = []

        # ── Sell pass ─────────────────────────────────────────────────────────
        for order in sells:
            price = await self.get_best_price(order.symbol, "sell")
            if price <= 0:
                log.warning("sovereign_sell_no_price", symbol=order.symbol)
                results.append(OrderResult(
                    symbol=order.symbol, side="sell",
                    quantity=order.quantity, price=0.0,
                    cl_ord_id="", order_id="", success=False, code=-1,
                    error="no_price_available",
                ))
                continue

            result = await self.place_ssi_order(
                order.symbol, "sell", order.quantity, price
            )
            results.append(result)

            if result.success:
                log.info(
                    "sovereign_rebalance_sell",
                    symbol=order.symbol,
                    quantity=order.quantity,
                    price=price,
                    reason=order.reason,
                )
            await asyncio.sleep(0.2)   # brief pause between orders

        # ── Buy pass ──────────────────────────────────────────────────────────
        for order in buys:
            price = await self.get_best_price(order.symbol, "buy")
            if price <= 0:
                log.warning("sovereign_buy_no_price", symbol=order.symbol)
                results.append(OrderResult(
                    symbol=order.symbol, side="buy",
                    quantity=order.quantity, price=0.0,
                    cl_ord_id="", order_id="", success=False, code=-1,
                    error="no_price_available",
                ))
                continue

            result = await self.place_ssi_order(
                order.symbol, "buy", order.quantity, price
            )
            results.append(result)

            if result.success:
                log.info(
                    "sovereign_rebalance_buy",
                    symbol=order.symbol,
                    quantity=order.quantity,
                    price=price,
                    reason=order.reason,
                )
            await asyncio.sleep(0.2)

        sells_ok = sum(1 for r in results if r.side == "sell" and r.success)
        buys_ok  = sum(1 for r in results if r.side == "buy"  and r.success)
        log.info(
            "sovereign_rebalance_complete",
            sells_attempted=len(sells), sells_ok=sells_ok,
            buys_attempted=len(buys),  buys_ok=buys_ok,
        )

        return results

    async def close(self) -> None:
        await self._http.aclose()


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_price(price: float, tick: float) -> str:
    """Round price to tick, return as string."""
    ticks   = round(price / tick)
    rounded = ticks * tick
    dp      = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return f"{rounded:.{dp}f}"


def _fmt_qty(qty: float, step: float) -> str:
    """Floor quantity to step, return as string."""
    floored = math.floor(qty / step) * step
    dp      = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return f"{floored:.{dp}f}"
