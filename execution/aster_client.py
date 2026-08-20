"""
Aster DEX venue client — V3 perpetual futures API (fapi/v3).

Aster is ARIA's second execution venue alongside SoDEX (execution/venue.py
dispatches by symbol). Auth is Aster V3: every signed request carries
nonce (µs timestamp, strictly increasing) + signer (API wallet address) +
an EIP-712 signature over the urlencoded params, domain
"AsterSignTransaction" v1 chainId 1666. The V1 Binance-HMAC protocol is
legacy and rejects newly-issued API wallets (-2015) — do not revert.

Hooks SoDEX lacks (mapped to ARIA known issues):
  - MIN_NOTIONAL $1 (SoDEX $10 → dust, issue #14)
  - Maker fee 0% on all contracts (SoDEX maker 0.012%)
  - Native STOP_MARKET / TAKE_PROFIT_MARKET / TRAILING_STOP_MARKET with
    MARK_PRICE workingType (SoDEX rejects native stops, issue #10)
  - Hedge mode (dual positionSide) — true simultaneous LONG+SHORT
  - Auto-cancel-all countdown (dead-man switch if the process dies)
  - ADL quantile endpoint (issue #8 is observational-only on SoDEX)

Defaults are INERT: aster_enabled=False / no keys → nothing registers.
Keys go in .env (ASTER_API_KEY = API wallet address = signer,
ASTER_API_SECRET = API wallet private key), never in git.
"""

from __future__ import annotations

import asyncio
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx
import structlog
from eth_account import Account

from execution.schemas import BracketOrder, BracketResult, OrderResult

logger = structlog.get_logger(__name__)

ASTER_MAINNET = "https://fapi.asterdex.com"

# Aster V3 EIP-712 domain (docs: asterdex/api-docs V3, verified live 2026-08-15)
_712_DOMAIN = {
    "name": "AsterSignTransaction",
    "version": "1",
    "chainId": 1666,
    "verifyingContract": "0x0000000000000000000000000000000000000000",
}
_712_TYPES = {"Message": [{"name": "msg", "type": "string"}]}


def _encode_712(msg: str):
    """eth_account ≥0.11 exposes encode_typed_data(kwargs); older versions
    take the full-message dict via encode_structured_data."""
    try:
        from eth_account.messages import encode_typed_data
        return encode_typed_data(domain_data=_712_DOMAIN,
                                 message_types=_712_TYPES,
                                 message_data={"msg": msg})
    except (ImportError, TypeError, ValueError):
        from eth_account.messages import encode_structured_data
        return encode_structured_data({
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                **_712_TYPES,
            },
            "primaryType": "Message",
            "domain": _712_DOMAIN,
            "message": {"msg": msg},
        })


class AsterAPIError(Exception):
    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


def to_aster_symbol(canonical: str) -> str:
    """BTC-USD → BTCUSDT (crypto USDT-margined perps only in Phase 1)."""
    if canonical in _ASTER_SYM_OVERRIDE:
        return _ASTER_SYM_OVERRIDE[canonical]
    return canonical.replace("-USD", "USDT").replace("-", "")


def to_canonical_symbol(aster_sym: str) -> str:
    """BTCUSDT → BTC-USD."""
    if aster_sym in _CANONICAL_SYM_OVERRIDE:
        return _CANONICAL_SYM_OVERRIDE[aster_sym]
    if aster_sym.endswith("USDT"):
        return aster_sym[:-4] + "-USD"
    return aster_sym


# Venue naming mismatches (2026-08-16): ARIA's XAUT-USD (Tether-gold name,
# inherited from SoDEX/Bybit) is XAUUSDT on Aster. CL-USD → CLUSDT needs no
# override (default rule produces it).
_ASTER_SYM_OVERRIDE = {"XAUT-USD": "XAUUSDT"}
_CANONICAL_SYM_OVERRIDE = {"XAUUSDT": "XAUT-USD"}


def _round_step(value: float, step: float, floor: bool = False) -> float:
    if step <= 0:
        return value
    n = value / step
    n = int(n) if floor else round(n)
    return n * step


class AsterClient:
    def __init__(self, config):
        self.config = config
        self.api_key = getattr(config, "aster_api_key", "") or ""
        self.api_secret = getattr(config, "aster_api_secret", "") or ""
        self._http = httpx.AsyncClient(base_url=ASTER_MAINNET, timeout=10.0)
        # canonical symbol → {"tick", "step", "min_qty", "min_notional"}
        self._specs: Dict[str, Dict[str, float]] = {}
        self._leverage_set: set[str] = set()
        self._equity_cache: tuple[float, float] = (0.0, 0.0)
        self._session_start_equity: float = 0.0
        self.hedge_mode: bool = False   # detected at boot via positionSide/dual
        self._last_nonce: int = 0

    # ── Sleeve-level kill switch (Chancellor venue partition) ────────────────
    # Same invariant as the Bybit sleeve: an Aster bleed must never reach the
    # 8% kingdom veto — the sleeve halts itself at 30% sleeve drawdown.

    def _sleeve_halted(self, equity: float) -> bool:
        start = self._session_start_equity
        if start <= 0 or equity <= 0:
            return False
        halt_pct = float(getattr(self.config, "aster_sleeve_halt_dd_pct", 0.30) or 0.30)
        return equity < start * (1.0 - halt_pct)

    # ── Auth (Aster V3: nonce + signer + EIP-712) ────────────────────────────

    def _nonce(self) -> str:
        """µs timestamp, strictly increasing — the exchange keeps the most
        recent 100 nonces per API wallet and rejects duplicates/staleness."""
        n = int(time.time() * 1_000_000)
        if n <= self._last_nonce:
            n = self._last_nonce + 1
        self._last_nonce = n
        return str(n)

    def _sign(self, total_params: str) -> str:
        return Account.sign_message(
            _encode_712(total_params), private_key=self.api_secret
        ).signature.hex()

    def _signed_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        p = {k: v for k, v in params.items()}
        p["nonce"] = self._nonce()
        p["signer"] = self.api_key
        p["signature"] = self._sign(urllib.parse.urlencode(p))
        return p

    async def _request(self, method: str, path: str,
                       params: Optional[dict] = None, signed: bool = True) -> Any:
        params = self._signed_params(params or {}) if signed else (params or {})
        resp = await self._http.request(method, path, params=params)
        # 503 = sent but no response in timeout — execution status UNKNOWN;
        # callers must reconcile via open orders / positions, never retry blind.
        if resp.status_code == 503:
            raise AsterAPIError(f"{path}: 503 status UNKNOWN — reconcile before retry",
                                code=503)
        try:
            data = resp.json()
        except Exception:
            raise AsterAPIError(f"{path}: HTTP {resp.status_code} non-JSON body",
                                code=resp.status_code)
        if resp.status_code >= 400 or (isinstance(data, dict) and data.get("code", 0) < 0):
            code = int(data.get("code", resp.status_code)) if isinstance(data, dict) else resp.status_code
            msg = data.get("msg", "unknown") if isinstance(data, dict) else str(data)[:120]
            raise AsterAPIError(f"{path}: {msg}", code=code)
        return data

    # ── Specs ────────────────────────────────────────────────────────────────

    async def sync_symbol_specs(self, symbols: List[str]) -> int:
        """Fetch PRICE_FILTER / LOT_SIZE / MIN_NOTIONAL from exchangeInfo."""
        synced = 0
        wanted = {to_aster_symbol(s): s for s in symbols}
        try:
            data = await self._request("GET", "/fapi/v3/exchangeInfo", signed=False)
        except AsterAPIError as e:
            logger.warning("aster_spec_sync_failed", error=str(e)[:120])
            return 0
        for item in data.get("symbols") or []:
            canonical = wanted.get(item.get("symbol", ""))
            if not canonical or item.get("status") != "TRADING":
                continue
            spec = {"tick": 0.0, "step": 0.0, "min_qty": 0.0, "min_notional": 1.0}
            for f in item.get("filters") or []:
                ft = f.get("filterType")
                if ft == "PRICE_FILTER":
                    spec["tick"] = float(f.get("tickSize", 0) or 0)
                elif ft == "LOT_SIZE":
                    spec["step"] = float(f.get("stepSize", 0) or 0)
                    spec["min_qty"] = float(f.get("minQty", 0) or 0)
                elif ft == "MIN_NOTIONAL":
                    spec["min_notional"] = float(f.get("notional", 1) or 1)
            self._specs[canonical] = spec
            synced += 1
        if synced:
            logger.info("aster_symbol_specs_synced", count=synced)
        return synced

    def get_spec(self, symbol: str) -> Dict[str, float]:
        return self._specs.get(
            symbol, {"tick": 0.0, "step": 0.0, "min_qty": 0.0, "min_notional": 1.0})

    def listed(self, symbol: str) -> bool:
        """True only if exchangeInfo confirmed this symbol TRADING on Aster.
        Boot routing gates on this — never route orders to an unlisted symbol."""
        return symbol in self._specs

    # ── Account mode ─────────────────────────────────────────────────────────

    async def detect_position_mode(self) -> bool:
        """Read hedge/one-way mode at boot — orders adapt positionSide to it."""
        try:
            data = await self._request("GET", "/fapi/v3/positionSide/dual")
            self.hedge_mode = str(data.get("dualSidePosition", "false")).lower() == "true"
        except AsterAPIError as e:
            logger.warning("aster_position_mode_read_failed", error=str(e)[:120])
            self.hedge_mode = False
        logger.info("aster_position_mode", hedge_mode=self.hedge_mode)
        return self.hedge_mode

    # ── Account / positions ──────────────────────────────────────────────────

    async def get_account_balance(self, account_id: str = "") -> float:
        """Total margin equity: wallet balance + unrealized PnL (account V3)."""
        data = await self._request("GET", "/fapi/v3/accountWithJoinMargin")
        wallet = float(data.get("totalWalletBalance", 0) or 0)
        upnl = float(data.get("totalUnrealizedProfit", 0) or 0)
        return wallet + upnl

    async def get_positions(self, address: str = "") -> List[Dict]:
        """Normalized to the SoDEX shape (positionRisk V3)."""
        data = await self._request("GET", "/fapi/v3/positionRisk")
        out = []
        for p in data if isinstance(data, list) else []:
            amt = float(p.get("positionAmt", 0) or 0)
            if amt == 0:
                continue
            canonical = to_canonical_symbol(p.get("symbol", ""))
            out.append({
                "symbol": canonical, "coin": canonical,
                "side": "long" if amt > 0 else "short",
                "size": abs(amt), "qty": abs(amt),
                "entry": float(p.get("entryPrice", 0) or 0),
                "avgPrice": float(p.get("entryPrice", 0) or 0),
                "markPrice": float(p.get("markPrice", 0) or 0),
                "upnl": float(p.get("unRealizedProfit", 0) or 0),
                "leverage": int(float(p.get("leverage", 1) or 1)),
                "liqPrice": float(p.get("liquidationPrice", 0) or 0),
                "venue": "aster",
            })
        return out

    async def get_open_orders(self, address: str = "") -> List[Dict]:
        data = await self._request("GET", "/fapi/v3/openOrders")
        out = []
        for o in data if isinstance(data, list) else []:
            out.append({
                "orderID": str(o.get("orderId", "")),
                "clOrdID": o.get("clientOrderId", ""),
                "symbol": to_canonical_symbol(o.get("symbol", "")),
                "side": o.get("side", ""),
                "price": float(o.get("price", 0) or 0),
                "quantity": float(o.get("origQty", 0) or 0),
                "stopPrice": float(o.get("stopPrice", 0) or 0),
                "reduceOnly": bool(o.get("reduceOnly", False)),
                "status": o.get("status", ""),
                "venue": "aster",
            })
        return out

    async def get_mark_price(self, symbol: str) -> float:
        data = await self._request("GET", "/fapi/v3/premiumIndex",
                                   {"symbol": to_aster_symbol(symbol)}, signed=False)
        return float(data.get("markPrice", 0) or 0)

    async def get_adl_quantile(self, symbol: str = "") -> dict:
        """ADL risk quantile per symbol — SoDEX has no equivalent (issue #8)."""
        params = {"symbol": to_aster_symbol(symbol)} if symbol else {}
        try:
            return await self._request("GET", "/fapi/v3/adlQuantile", params)
        except AsterAPIError as e:
            logger.warning("aster_adl_quantile_failed", symbol=symbol, error=str(e)[:120])
            return {}

    # ── Leverage ─────────────────────────────────────────────────────────────

    async def update_leverage(self, symbol: str, leverage: int, account_id: int = 0) -> bool:
        try:
            await self._request("POST", "/fapi/v3/leverage", {
                "symbol": to_aster_symbol(symbol), "leverage": int(leverage),
            })
            self._leverage_set.add(symbol)
            return True
        except AsterAPIError as e:
            logger.warning("aster_leverage_failed", symbol=symbol,
                           leverage=leverage, error=str(e)[:120])
            return False

    async def update_leverage_with_fallback(self, symbol: str = "", leverage: int = 5,
                                            account_id: int = 0,
                                            chain: tuple = (10, 7, 5, 3, 2)) -> int:
        if symbol in self._leverage_set:
            return leverage
        for lev in [leverage] + [c for c in chain if c != leverage]:
            if await self.update_leverage(symbol, lev):
                return lev
        return 0

    # ── Orders ───────────────────────────────────────────────────────────────

    def _position_side(self, side: str, reduce_only: bool) -> str:
        """Hedge mode: entry LONG/SHORT, close is the OPPOSITE positionSide.
        One-way mode: always BOTH."""
        if not self.hedge_mode:
            return "BOTH"
        if reduce_only:
            return "SHORT" if side == "long" else "LONG"
        return "LONG" if side == "long" else "SHORT"

    def _order_params(self, symbol: str, side: str, qty: float,
                      order_type: str = "MARKET", price: float = 0.0,
                      reduce_only: bool = False, time_in_force: str = "GTC",
                      link_id: str = "") -> dict:
        spec = self.get_spec(symbol)
        qty_r = _round_step(qty, spec["step"], floor=reduce_only)
        p: Dict[str, Any] = {
            "symbol": to_aster_symbol(symbol),
            "side": "BUY" if side == "long" else "SELL",
            "type": order_type,
            "quantity": f"{qty_r:g}",
            "positionSide": self._position_side(side, reduce_only),
        }
        if reduce_only and not self.hedge_mode:
            p["reduceOnly"] = "true"
        if order_type == "LIMIT":
            p["price"] = f"{_round_step(price, spec['tick']):g}"
            # GTX = post-only (maker is FREE on Aster — the venue's core hook)
            p["timeInForce"] = "GTX" if time_in_force == "PostOnly" else time_in_force
        if link_id:
            p["newClientOrderId"] = link_id[:36]
        return p

    async def place_order(self, order_data: Dict[str, Any]) -> OrderResult:
        symbol = order_data["symbol"]
        params = self._order_params(
            symbol=symbol,
            side=order_data["side"],
            qty=float(order_data["qty"]),
            order_type=order_data.get("order_type", "MARKET"),
            price=float(order_data.get("price", 0.0) or 0.0),
            reduce_only=bool(order_data.get("reduce_only", False)),
            time_in_force=order_data.get("time_in_force", "GTC"),
            link_id=order_data.get("link_id", ""),
        )
        try:
            result = await self._request("POST", "/fapi/v3/order", params)
            return OrderResult(order_id=str(result.get("orderId", "")), status="open")
        except AsterAPIError as e:
            logger.warning("aster_order_rejected", symbol=symbol,
                           side=order_data["side"], error=str(e)[:160])
            return OrderResult(order_id="", status="rejected", error=str(e))

    async def cancel_order(self, order_id: str, symbol: str = "", account_id: int = 0,
                           **_) -> bool:
        try:
            await self._request("DELETE", "/fapi/v3/order", {
                "symbol": to_aster_symbol(symbol), "orderId": order_id,
            })
            return True
        except AsterAPIError as e:
            logger.warning("aster_cancel_failed", symbol=symbol,
                           order_id=order_id, error=str(e)[:120])
            return False

    async def set_deadman_switch(self, symbol: str, countdown_ms: int) -> bool:
        """Auto-cancel all open orders on `symbol` after countdown_ms unless
        refreshed. countdown_ms=0 cancels the timer. If ARIA dies, Aster-side
        orders die too — SoDEX has no equivalent safety hook."""
        try:
            await self._request("POST", "/fapi/v3/countdownCancelAll", {
                "symbol": to_aster_symbol(symbol), "countdownTime": int(countdown_ms),
            })
            return True
        except AsterAPIError as e:
            logger.warning("aster_deadman_failed", symbol=symbol, error=str(e)[:120])
            return False

    # ── Bracket (entry + native stop + TP limits) ────────────────────────────

    async def _venue_equity(self) -> float:
        equity, ts = self._equity_cache
        if equity > 0 and time.time() - ts < 30.0:
            return equity
        equity = await self.get_account_balance()
        if equity > 0:
            self._equity_cache = (equity, time.time())
            if self._session_start_equity <= 0:
                self._session_start_equity = equity
                logger.info("aster_session_start_equity", equity=round(equity, 2))
        return equity

    async def place_bracket(self, bracket: BracketOrder) -> BracketResult:
        c = bracket.candidate
        symbol = c.symbol
        spec = self.get_spec(symbol)

        max_pos = int(getattr(self.config, "aster_max_positions", 5) or 5)
        try:
            open_positions = await self.get_positions()
        except AsterAPIError as e:
            return BracketResult(success=False, error=f"position_check_failed: {e}")
        if len(open_positions) >= max_pos:
            return BracketResult(
                success=False,
                error=f"aster_position_cap: {len(open_positions)} >= {max_pos}")

        equity = await self._venue_equity()
        if equity <= 0:
            return BracketResult(success=False, error="aster_equity_unavailable")
        if self._sleeve_halted(equity):
            logger.warning("aster_sleeve_halt_active", symbol=symbol,
                           equity=round(equity, 2),
                           session_start=round(self._session_start_equity, 2))
            return BracketResult(success=False, error="aster_sleeve_halt")

        margin_pct = float(getattr(self.config, "aster_margin_pct", 0.10) or 0.10)
        leverage = min(int(getattr(c, "leverage", 5) or 5),
                       int(getattr(self.config, "aster_max_leverage", 10) or 10))
        notional = equity * margin_pct * leverage
        size = notional / c.entry_price if c.entry_price > 0 else 0.0

        if notional < spec["min_notional"] or size < spec["min_qty"]:
            return BracketResult(
                success=False,
                error=f"below_aster_min: notional {notional:.2f} < {spec['min_notional']} "
                      f"or qty {size:g} < {spec['min_qty']}")

        order_type = "MARKET" if c.order_type == "market" else "LIMIT"
        tif = "PostOnly" if c.order_type == "maker" else "GTC"
        entry = await self.place_order({
            "symbol": symbol, "side": c.side, "qty": size,
            "order_type": order_type, "price": c.entry_price,
            "time_in_force": tif,
        })
        if not entry.success:
            return BracketResult(success=False, error=entry.error)

        result = BracketResult(success=True, entry_order_id=entry.order_id)

        if not await self._confirm_position_open(symbol):
            logger.warning("aster_entry_unconfirmed", symbol=symbol,
                           order_id=entry.order_id)
            return result

        result.stop_order_id = await self._set_position_stop(symbol, c, size)
        tp_ids = await self._place_tp_orders(symbol, c, size)
        result.tp1_order_id = tp_ids[0] if len(tp_ids) > 0 else None
        result.tp2_order_id = tp_ids[1] if len(tp_ids) > 1 else None
        return result

    async def place_protective_orders(self, bracket: BracketOrder) -> BracketResult:
        """Re-place stop + TPs for an existing position (entry already filled)."""
        c = bracket.candidate
        symbol = c.symbol
        size = c.size
        try:
            for p in await self.get_positions():
                if p["symbol"] == symbol and p.get("size", 0) > 0:
                    size = p["size"]
                    break
        except AsterAPIError:
            pass
        stop_id = await self._set_position_stop(symbol, c, size)
        tp_ids = await self._place_tp_orders(symbol, c, size)
        return BracketResult(
            success=stop_id is not None or bool(tp_ids),
            stop_order_id=stop_id,
            tp1_order_id=tp_ids[0] if len(tp_ids) > 0 else None,
            tp2_order_id=tp_ids[1] if len(tp_ids) > 1 else None,
        )

    async def _confirm_position_open(self, symbol: str, timeout_s: float = 10.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                positions = await self.get_positions()
                if any(p["symbol"] == symbol for p in positions):
                    return True
            except AsterAPIError:
                pass
            await asyncio.sleep(1.0)
        return False

    async def _set_position_stop(self, symbol: str, c, size: float = 0.0) -> Optional[str]:
        """Native STOP_MARKET on MARK_PRICE — the hook SoDEX lacks (issue #10).
        One-way mode: closePosition=true covers the whole position. Hedge mode
        rejects closePosition — must send quantity + positionSide instead."""
        side = "short" if c.side == "long" else "long"
        spec = self.get_spec(symbol)
        params: Dict[str, Any] = {
            "symbol": to_aster_symbol(symbol),
            "side": "SELL" if c.side == "long" else "BUY",
            "type": "STOP_MARKET",
            "stopPrice": f"{_round_step(c.stop_price, spec['tick']):g}",
            "workingType": "MARK_PRICE",
            "positionSide": self._position_side(side, reduce_only=True),
        }
        if self.hedge_mode:
            qty_r = _round_step(size, spec["step"], floor=True)
            if qty_r <= 0:
                return None
            params["quantity"] = f"{qty_r:g}"
        else:
            # Binance protocol: closePosition orders must NOT send reduceOnly
            # — rejected "Parameter 'reduceOnly' sent when not required"
            # (2026-08-17: UNI/ADA shorts ran 4.5h with no native stop).
            params["closePosition"] = "true"
        try:
            result = await self._request("POST", "/fapi/v3/order", params)
            return str(result.get("orderId", ""))
        except AsterAPIError as e:
            logger.warning("aster_stop_failed", symbol=symbol, error=str(e)[:160])
            return None

    async def place_trailing_stop(self, symbol: str, side: str, quantity: float,
                                  callback_rate: float,
                                  activation_price: float = 0.0) -> Optional[str]:
        """Native TRAILING_STOP_MARKET on MARK_PRICE — the explosive-move
        weapon SoDEX lacks. side = POSITION side ("long" → SELL trail).
        quantity is required (closePosition is not supported for trailing
        stops on the Binance protocol). callback_rate in percent; Aster's
        live-accepted range is 0.1-5.0 (verified 2026-08-16: 10 rejected
        "Invalid callBack rate", 5/5.0/0.1 accepted).
        activation_price 0 = trail from placement."""
        spec = self.get_spec(symbol)
        qty_r = _round_step(quantity, spec["step"], floor=True)
        if qty_r <= 0:
            return None
        cb = min(5.0, max(0.1, float(callback_rate)))
        params: Dict[str, Any] = {
            "symbol": to_aster_symbol(symbol),
            "side": "SELL" if side == "long" else "BUY",
            "type": "TRAILING_STOP_MARKET",
            "quantity": f"{qty_r:g}",
            "callbackRate": f"{cb:g}",
            "workingType": "MARK_PRICE",
            "reduceOnly": "true",
            "positionSide": self._position_side(side, reduce_only=True),
        }
        if activation_price > 0:
            params["activationPrice"] = f"{_round_step(activation_price, spec['tick']):g}"
        try:
            result = await self._request("POST", "/fapi/v3/order", params)
            return str(result.get("orderId", ""))
        except AsterAPIError as e:
            logger.warning("aster_trailing_stop_failed", symbol=symbol,
                           error=str(e)[:160])
            return None

    async def _place_tp_orders(self, symbol: str, c, size: float) -> List[str]:
        """Reduce-only LIMIT TPs at tp1/tp2 (GTX post-only — maker is free)."""
        ids: List[str] = []
        tps = [tp for tp in (getattr(c, "tp1_price", 0.0), getattr(c, "tp2_price", 0.0)) if tp]
        if not tps:
            return ids
        share = _round_step(size / len(tps), self.get_spec(symbol)["step"], floor=True)
        if share <= 0:
            return ids
        for tp in tps:
            r = await self.place_order({
                "symbol": symbol,
                "side": "short" if c.side == "long" else "long",
                "qty": share, "order_type": "LIMIT", "price": tp,
                "reduce_only": True, "time_in_force": "GTX",
            })
            if r.success:
                ids.append(r.order_id)
        return ids

    async def replace_stop_order(self, symbol: str = "", symbol_id: int = 0,
                                 new_stop: float = 0.0, side: str = "",
                                 account_id: int = 0, new_stop_price: float = 0.0,
                                 **_) -> OrderResult:
        """Cancel existing STOP_MARKET(s) for symbol, place tightened stop.

        Venue contract (sodex/bybit): callers pass new_stop_price= and read
        .success/.order_id — new_stop= stays for the explosive path's direct
        call. The 2026-08-17 startup_stop_exception was this method swallowing
        new_stop_price= into **_ → stop 0.0 → "Stop price less than zero",
        then None.success AttributeErroring the sync."""
        stop = new_stop or new_stop_price
        try:
            orders = await self.get_open_orders()
        except AsterAPIError as e:
            return OrderResult(order_id="", status="rejected", error=str(e)[:160])
        for o in orders:
            if o["symbol"] == symbol and o.get("stopPrice"):
                await self.cancel_order(o["orderID"], symbol=symbol)
        spec = self.get_spec(symbol)
        params: Dict[str, Any] = {
            "symbol": to_aster_symbol(symbol),
            "side": "SELL" if side == "long" else "BUY",
            "type": "STOP_MARKET",
            "stopPrice": f"{_round_step(stop, spec['tick']):g}",
            "workingType": "MARK_PRICE",
            "positionSide": self._position_side(side, reduce_only=True),
        }
        if self.hedge_mode:
            try:
                positions = await self.get_positions()
                qty = next((p["size"] for p in positions if p["symbol"] == symbol), 0.0)
            except AsterAPIError as e:
                return OrderResult(order_id="", status="rejected",
                                   error=str(e)[:160])
            qty_r = _round_step(qty, spec["step"], floor=True)
            if qty_r <= 0:
                return OrderResult(order_id="", status="rejected",
                                   error="qty_below_step")
            params["quantity"] = f"{qty_r:g}"
        else:
            # closePosition + reduceOnly is rejected exchange-side — same
            # rule as _set_position_stop (2026-08-17 startup_stop_exception).
            params["closePosition"] = "true"
        try:
            result = await self._request("POST", "/fapi/v3/order", params)
            return OrderResult(order_id=str(result.get("orderId", "")),
                               status="new")
        except AsterAPIError as e:
            logger.warning("aster_stop_replace_failed", symbol=symbol, error=str(e)[:160])
            return OrderResult(order_id="", status="rejected", error=str(e)[:160])

    async def close_position_market(self, symbol: str = "", symbol_id: int = 0,
                                    side: str = "", qty: float = 0.0,
                                    account_id: int = 0, size: float = 0.0,
                                    **_) -> OrderResult:
        # Venue contract (sodex/bybit): callers pass size= and read
        # .success/.error. The 2026-08-17 storm (21k rejects, 16k guardian
        # exceptions) was this method swallowing size= into **_ → qty 0.0
        # → "Quantity less than zero", then returning a bool that
        # AttributeError'd past the circuit breaker. qty= stays for the
        # explosive time-stop's direct call.
        close_side = "short" if side == "long" else "long"
        req_qty = qty or size
        # Dust-at-source fix (2026-08-20): tracked size drifts below exchange
        # fills by rounding (VIRTUAL 164.9 tracked vs 165 actual → 0.1
        # unclosable residue re-adopted every restart — TP loop vs dust-guard
        # contradiction). One-way mode: when the request covers the live
        # position (within one step), close the EXCHANGE-reported qty — the
        # exchange's own number is step-aligned by construction, so a full
        # close leaves zero residue. (closePosition=true would be cleaner but
        # Aster V3 rejects it for MARKET orders — live-verified 2026-08-20:
        # "Target strategy invalid for orderType MARKET,closePosition true".)
        # Partial closes (treasury trims) keep the caller's exact qty.
        # Poll failure → caller's qty: a close is never blocked by a read.
        if not self.hedge_mode:
            try:
                _step = float(self.get_spec(symbol).get("step") or 0.0) or 1e-12
                for _p in await self.get_positions():
                    if _p.get("symbol") == symbol:
                        _ex_qty = abs(float(_p.get("size") or _p.get("qty") or 0.0))
                        if _ex_qty > 0 and req_qty >= _ex_qty - _step:
                            req_qty = _ex_qty
                        break
            except Exception:
                pass
        return await self.place_order({
            "symbol": symbol, "side": close_side, "qty": req_qty,
            "order_type": "MARKET", "reduce_only": True,
        })

    # ── Health ───────────────────────────────────────────────────────────────

    async def health_check(self) -> dict:
        try:
            await self._request("GET", "/fapi/v3/ping", signed=False)
            equity = await self.get_account_balance()
            return {"venue": "aster", "status": "ok", "equity": equity,
                    "hedge_mode": self.hedge_mode,
                    "sleeve_halted": self._sleeve_halted(equity)}
        except Exception as e:
            return {"venue": "aster", "status": "error", "error": str(e)[:120]}

    async def close(self) -> None:
        await self._http.aclose()
