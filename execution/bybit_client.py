"""
Bybit V5 execution client — write side of the Bybit venue.

Mirrors the SoDEXClient surface used by main.py's chokepoints so the venue
dispatch (execution/venue.py) can route order ops by symbol partition:

    place_bracket / place_protective_orders / replace_stop_order
    cancel_order / close_position_market
    get_positions / get_open_orders / get_account_balance
    update_leverage(_with_fallback) / fetch_perp_fee_rate / get_mark_price

Conventions (established by AUGUR's live Bybit path):
  - positionIdx: 0 (one-way mode) — matches ARIA's one-position-per-symbol invariant
  - tpTriggerBy / slTriggerBy: "MarkPrice" — prevents wick fills
  - Entry without attached tpsl; stop set position-level via /v5/position/trading-stop,
    TPs as reduce-only GTC limits — mirrors the SoDEX bracket structure so the
    exit machinery's order_ids {stop, tp1, tp2, tp3} assumptions hold.

Canonical symbols inside ARIA ("ADA-USD"); Bybit names ("ADAUSDT") only at
this boundary. Positions are normalized to the SoDEX dict shape (symbol/coin,
size/qty, entry/avgPrice, markPrice, side) so reconciliation code reads both
venues unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

import httpx
import structlog

from execution.schemas import BracketOrder, BracketResult, OrderResult

logger = structlog.get_logger(__name__)

BYBIT_MAINNET = "https://api.bybit.com"
BYBIT_TESTNET = "https://api-testnet.bybit.com"

_RECV_WINDOW = "5000"
_CATEGORY = "linear"
_SETTLE = "USDT"


class BybitAPIError(Exception):
    def __init__(self, message: str, ret_code: int = 0):
        super().__init__(message)
        self.ret_code = ret_code


def to_bybit_symbol(canonical: str) -> str:
    """ADA-USD → ADAUSDT. 1000PEPE-USD → 1000PEPEUSDT."""
    return canonical.replace("-USD", "USDT")


def to_canonical_symbol(bybit_sym: str) -> str:
    """ADAUSDT → ADA-USD."""
    return bybit_sym.replace("USDT", "-USD") if bybit_sym.endswith("USDT") else bybit_sym


def _round_step(value: float, step: float, floor: bool = False) -> float:
    if step <= 0:
        return value
    d_val = Decimal(str(value))
    d_step = Decimal(str(step))
    rounded = (d_val / d_step).to_integral_value(
        rounding=ROUND_DOWN if floor else ROUND_HALF_UP) * d_step
    return float(rounded.normalize())


class BybitClient:
    def __init__(self, config):
        self.config = config
        self.api_key = getattr(config, "bybit_api_key", "") or ""
        self.api_secret = getattr(config, "bybit_api_secret", "") or ""
        # Endpoint from config — flip BYBIT_TESTNET in .env to switch.
        # Keys must match the environment (testnet.bybit.com vs bybit.com).
        self.testnet = bool(getattr(config, "bybit_testnet", False))
        self.base_url = BYBIT_TESTNET if self.testnet else BYBIT_MAINNET
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=10.0)
        # symbol (canonical) → {"tick", "step", "min_qty", "min_notional"}
        self._specs: Dict[str, Dict[str, float]] = {}
        self._leverage_set: set[str] = set()
        self._equity_cache: tuple[float, float] = (0.0, 0.0)  # (equity, ts)
        self._session_start_equity: float = 0.0  # set on first successful fetch

    # ── Sleeve-level kill switch (Chancellor venue partition) ────────────────
    # A Bybit sleeve loss must never veto the whole kingdom: halt the sleeve
    # at 30% sleeve drawdown (≈5.6% of combined equity at $100/$533) — well
    # under the 8% kingdom veto. SoDEX operation is unaffected.

    def _sleeve_halted(self, equity: float) -> bool:
        start = self._session_start_equity
        if start <= 0 or equity <= 0:
            return False
        halt_pct = float(getattr(self.config, "bybit_sleeve_halt_dd_pct", 0.30) or 0.30)
        return equity < start * (1.0 - halt_pct)

    # ── Auth ─────────────────────────────────────────────────────────────────

    def _auth_headers(self, payload: str) -> Dict[str, str]:
        ts = str(int(time.time() * 1000))
        sign = hmac.new(
            self.api_secret.encode("utf-8"),
            (ts + self.api_key + _RECV_WINDOW + payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": _RECV_WINDOW,
            "X-BAPI-SIGN": sign,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _parse(resp: httpx.Response, path: str) -> dict:
        """Decode a V5 response, fail loud on auth/edge rejections.

        Bybit returns HTTP 401 with an EMPTY body for invalid/env-mismatched
        API keys — resp.json() then dies with a cryptic JSONDecodeError
        ("Expecting value: line 1 column 1") that gives no hint the keys are
        dead. Raise with the HTTP status instead.
        """
        try:
            data = resp.json()
        except Exception:
            raise BybitAPIError(
                f"{path}: HTTP {resp.status_code} with non-JSON/empty body "
                f"(auth or edge rejection — check API key vs "
                f"{'testnet' if 'testnet' in str(resp.url) else 'mainnet'} environment)",
                ret_code=resp.status_code)
        if data.get("retCode") != 0:
            raise BybitAPIError(f"{path}: {data.get('retMsg', 'unknown')}",
                                ret_code=int(data.get("retCode", -1)))
        return data.get("result", {})

    async def _post(self, path: str, body: dict) -> dict:
        payload = json.dumps(body, separators=(",", ":"))
        resp = await self._http.post(path, content=payload, headers=self._auth_headers(payload))
        return self._parse(resp, path)

    async def _get(self, path: str, params: Optional[dict] = None, auth: bool = True) -> dict:
        params = params or {}
        query = "&".join(f"{k}={v}" for k, v in params.items())
        headers = self._auth_headers(query) if auth else {}
        resp = await self._http.get(path, params=params, headers=headers)
        return self._parse(resp, path)

    # ── Specs ────────────────────────────────────────────────────────────────

    async def sync_symbol_specs(self, symbols: List[str]) -> int:
        """Fetch tick/lot/min-notional from instruments-info for canonical symbols."""
        synced = 0
        for canonical in symbols:
            try:
                result = await self._get("/v5/market/instruments-info", {
                    "category": _CATEGORY, "symbol": to_bybit_symbol(canonical),
                }, auth=False)
                items = result.get("list") or []
                if not items:
                    continue
                item = items[0]
                lot = item.get("lotSizeFilter", {})
                price_f = item.get("priceFilter", {})
                self._specs[canonical] = {
                    "tick": float(price_f.get("tickSize", 0) or 0),
                    "step": float(lot.get("qtyStep", 0) or 0),
                    "min_qty": float(lot.get("minOrderQty", 0) or 0),
                    "min_notional": float(lot.get("minNotionalValue", 5) or 5),
                }
                synced += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.warning("bybit_spec_sync_failed", symbol=canonical, error=str(e)[:120])
        if synced:
            logger.info("bybit_symbol_specs_synced", count=synced)
        return synced

    def get_spec(self, symbol: str) -> Dict[str, float]:
        return self._specs.get(
            symbol, {"tick": 0.0, "step": 0.0, "min_qty": 0.0, "min_notional": 5.0})

    # ── Account / positions ──────────────────────────────────────────────────

    async def get_account_balance(self, account_id: str = "") -> float:
        result = await self._get("/v5/account/wallet-balance",
                                 {"accountType": "UNIFIED"})
        accounts = result.get("list") or []
        if not accounts:
            return 0.0
        return float(accounts[0].get("totalEquity", 0) or 0)

    async def get_positions(self, address: str = "") -> List[Dict]:
        """Normalized to the SoDEX shape: symbol/coin, size/qty, entry/avgPrice, markPrice."""
        result = await self._get("/v5/position/list",
                                 {"category": _CATEGORY, "settleCoin": _SETTLE})
        out = []
        for p in result.get("list") or []:
            size = abs(float(p.get("size", 0) or 0))
            if size <= 0:
                continue
            canonical = to_canonical_symbol(p.get("symbol", ""))
            side = "long" if p.get("side") == "Buy" else "short"
            out.append({
                "symbol": canonical, "coin": canonical,
                "side": side,
                "size": size, "qty": size,
                "entry": float(p.get("avgPrice", 0) or 0),
                "avgPrice": float(p.get("avgPrice", 0) or 0),
                "markPrice": float(p.get("markPrice", 0) or 0),
                "upnl": float(p.get("unrealisedPnl", 0) or 0),
                "leverage": int(float(p.get("leverage", 1) or 1)),
                "liqPrice": float(p.get("liqPrice", 0) or 0),
                "venue": "bybit",
            })
        return out

    async def get_open_orders(self, address: str = "") -> List[Dict]:
        result = await self._get("/v5/order/realtime",
                                 {"category": _CATEGORY, "settleCoin": _SETTLE})
        out = []
        for o in result.get("list") or []:
            out.append({
                "orderID": o.get("orderId", ""),
                "clOrdID": o.get("orderLinkId", ""),
                "symbol": to_canonical_symbol(o.get("symbol", "")),
                "side": o.get("side", ""),
                "price": float(o.get("price", 0) or 0),
                "quantity": float(o.get("qty", 0) or 0),
                "stopPrice": float(o.get("triggerPrice", 0) or 0),
                "reduceOnly": bool(o.get("reduceOnly", False)),
                "status": o.get("orderStatus", ""),
                "venue": "bybit",
            })
        return out

    async def get_mark_price(self, symbol: str) -> float:
        result = await self._get("/v5/market/tickers", {
            "category": _CATEGORY, "symbol": to_bybit_symbol(symbol),
        }, auth=False)
        items = result.get("list") or []
        return float(items[0].get("markPrice", 0) or 0) if items else 0.0

    async def fetch_perp_fee_rate(self, address: str = "", symbol: str = "") -> dict:
        params: Dict[str, str] = {"category": _CATEGORY}
        if symbol:
            params["symbol"] = to_bybit_symbol(symbol)
        result = await self._get("/v5/account/fee-rate", params)
        items = result.get("list") or []
        if not items:
            return {}
        item = items[0]
        return {
            "maker": float(item.get("makerFeeRate", 0) or 0),
            "taker": float(item.get("takerFeeRate", 0) or 0),
        }

    # ── Leverage ─────────────────────────────────────────────────────────────

    async def update_leverage(self, symbol: str, leverage: int, account_id: int = 0) -> bool:
        try:
            await self._post("/v5/position/set-leverage", {
                "category": _CATEGORY, "symbol": to_bybit_symbol(symbol),
                "buyLeverage": str(leverage), "sellLeverage": str(leverage),
            })
            self._leverage_set.add(symbol)
            return True
        except BybitAPIError as e:
            if e.ret_code == 110043:   # leverage not modified — already at target
                self._leverage_set.add(symbol)
                return True
            logger.warning("bybit_leverage_failed", symbol=symbol,
                           leverage=leverage, error=str(e)[:120])
            return False

    async def update_leverage_with_fallback(self, symbol: str = "", leverage: int = 5,
                                            chain: tuple = (10, 7, 5, 3, 2),
                                            account_id: int = 0, **_) -> int:
        max_lev = int(getattr(self.config, "bybit_max_leverage", 10) or 10)
        target = min(leverage, max_lev)
        for lev in chain:
            if lev > target:
                continue
            if await self.update_leverage(symbol, lev):
                return lev
        return 0

    # ── Order placement ──────────────────────────────────────────────────────

    def _order_body(self, symbol: str, side: str, qty: float,
                    order_type: str = "Market", price: float = 0.0,
                    reduce_only: bool = False, time_in_force: str = "GTC",
                    link_id: str = "") -> dict:
        spec = self.get_spec(symbol)
        qty_r = _round_step(qty, spec["step"], floor=reduce_only)
        body: Dict[str, Any] = {
            "category": _CATEGORY,
            "symbol": to_bybit_symbol(symbol),
            "side": "Buy" if side == "long" else "Sell",
            "orderType": order_type,
            "qty": f"{qty_r:g}",
            "positionIdx": 0,
        }
        if reduce_only:
            body["reduceOnly"] = True
        if order_type == "Limit":
            price_r = _round_step(price, spec["tick"])
            body["price"] = f"{price_r:g}"
            body["timeInForce"] = time_in_force
        if link_id:
            body["orderLinkId"] = link_id[:36]
        return body

    async def place_order(self, order_data: Dict[str, Any]) -> OrderResult:
        symbol = order_data["symbol"]
        body = self._order_body(
            symbol=symbol,
            side=order_data["side"],
            qty=float(order_data["qty"]),
            order_type=order_data.get("order_type", "Market"),
            price=float(order_data.get("price", 0.0) or 0.0),
            reduce_only=bool(order_data.get("reduce_only", False)),
            time_in_force=order_data.get("time_in_force", "GTC"),
            link_id=order_data.get("link_id", ""),
        )
        try:
            result = await self._post("/v5/order/create", body)
            return OrderResult(order_id=result.get("orderId", ""), status="open")
        except BybitAPIError as e:
            logger.warning("bybit_order_rejected", symbol=symbol,
                           side=order_data["side"], error=str(e)[:160])
            return OrderResult(order_id="", status="rejected", error=str(e))

    async def cancel_order(self, order_id: str, symbol: str = "", account_id: int = 0,
                           **_) -> bool:
        try:
            await self._post("/v5/order/cancel", {
                "category": _CATEGORY,
                "symbol": to_bybit_symbol(symbol),
                "orderId": order_id,
            })
            return True
        except BybitAPIError as e:
            logger.warning("bybit_cancel_failed", symbol=symbol,
                           order_id=order_id, error=str(e)[:120])
            return False

    # ── Bracket (entry + native stop + TP limits) ────────────────────────────

    async def _venue_equity(self) -> float:
        """Cached (30s) venue equity — drives pct-of-equity sizing."""
        equity, ts = self._equity_cache
        if equity > 0 and time.time() - ts < 30.0:
            return equity
        equity = await self.get_account_balance()
        if equity > 0:
            self._equity_cache = (equity, time.time())
            if self._session_start_equity <= 0:
                self._session_start_equity = equity
                logger.info("bybit_session_start_equity", equity=round(equity, 2))
        return equity

    async def place_bracket(self, bracket: BracketOrder) -> BracketResult:
        c = bracket.candidate
        symbol = c.symbol
        spec = self.get_spec(symbol)

        # Venue position cap — enforced here so every entry path is covered.
        max_pos = int(getattr(self.config, "bybit_max_positions", 2) or 2)
        try:
            open_positions = await self.get_positions()
        except BybitAPIError as e:
            return BracketResult(success=False, error=f"position_check_failed: {e}")
        if len(open_positions) >= max_pos:
            return BracketResult(
                success=False,
                error=f"bybit_position_cap: {len(open_positions)} >= {max_pos}")

        # Pct-of-venue-equity sizing: margin = equity * margin_pct, notional =
        # margin * leverage. Works at $50 and scales linearly with balance.
        # The candidate's size (computed against SoDEX equity) is overridden.
        equity = await self._venue_equity()
        if equity <= 0:
            return BracketResult(success=False, error="bybit_equity_unavailable")
        if self._sleeve_halted(equity):
            logger.warning("bybit_sleeve_halt_active", symbol=symbol,
                           equity=round(equity, 2),
                           session_start=round(self._session_start_equity, 2))
            return BracketResult(success=False, error="bybit_sleeve_halt")
        margin_pct = float(getattr(self.config, "bybit_margin_pct", 0.10) or 0.10)
        leverage = min(int(getattr(c, "leverage", 5) or 5),
                       int(getattr(self.config, "bybit_max_leverage", 10) or 10))
        notional = equity * margin_pct * leverage
        size = notional / c.entry_price if c.entry_price > 0 else 0.0

        if notional < spec["min_notional"] or size < spec["min_qty"]:
            return BracketResult(
                success=False,
                error=f"below_bybit_min: notional {notional:.2f} < {spec['min_notional']} "
                      f"or qty {size:g} < {spec['min_qty']}")

        order_type = "Market" if c.order_type == "market" else "Limit"
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
            logger.warning("bybit_entry_unconfirmed", symbol=symbol,
                           order_id=entry.order_id)
            return result

        result.stop_order_id = await self._set_position_stop(symbol, c.stop_price)
        tp_ids = await self._place_tp_orders(symbol, c, size)
        result.tp1_order_id = tp_ids[0] if len(tp_ids) > 0 else None
        result.tp2_order_id = tp_ids[1] if len(tp_ids) > 1 else None
        result.tp3_order_id = tp_ids[2] if len(tp_ids) > 2 else None
        return result

    async def place_protective_orders(self, bracket: BracketOrder) -> BracketResult:
        """Re-place stop + TPs for an existing position (entry already filled)."""
        c = bracket.candidate
        size = c.size
        try:
            for p in await self.get_positions():
                if p["symbol"] == c.symbol and p["size"] > 0:
                    size = p["size"]
                    break
        except BybitAPIError:
            pass
        stop_id = await self._set_position_stop(c.symbol, c.stop_price)
        tp_ids = await self._place_tp_orders(c.symbol, c, size)
        return BracketResult(
            success=stop_id is not None or bool(tp_ids),
            stop_order_id=stop_id,
            tp1_order_id=tp_ids[0] if len(tp_ids) > 0 else None,
            tp2_order_id=tp_ids[1] if len(tp_ids) > 1 else None,
            tp3_order_id=tp_ids[2] if len(tp_ids) > 2 else None,
        )

    async def _confirm_position_open(self, symbol: str, timeout_s: float = 10.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                positions = await self.get_positions()
                if any(p["symbol"] == symbol and p["size"] > 0 for p in positions):
                    return True
            except BybitAPIError:
                pass
            await asyncio.sleep(1.0)
        return False

    async def _set_position_stop(self, symbol: str, stop_price: float) -> Optional[str]:
        """Position-level native stop (MarkPrice trigger). Returns a synthetic id."""
        spec = self.get_spec(symbol)
        stop_r = _round_step(stop_price, spec["tick"])
        try:
            await self._post("/v5/position/trading-stop", {
                "category": _CATEGORY,
                "symbol": to_bybit_symbol(symbol),
                "stopLoss": f"{stop_r:g}",
                "slTriggerBy": "MarkPrice",
                "positionIdx": 0,
            })
            return f"posstop-{symbol}"
        except BybitAPIError as e:
            logger.warning("bybit_stop_failed", symbol=symbol, error=str(e)[:120])
            return None

    async def _place_tp_orders(self, symbol: str, c, size: float) -> List[str]:
        close_side = "short" if c.side == "long" else "long"
        ids: List[str] = []
        spec = self.get_spec(symbol)
        for price, pct in ((c.tp1_price, c.partial1_pct),
                           (c.tp2_price, c.partial2_pct),
                           (c.tp3_price, c.partial3_pct)):
            if price <= 0 or pct <= 0:
                continue
            qty = size * pct
            if qty * price < spec["min_notional"] or qty < spec["min_qty"]:
                continue   # sub-minimum TP leg — skip rather than reject
            res = await self.place_order({
                "symbol": symbol, "side": close_side, "qty": qty,
                "order_type": "Limit", "price": price, "reduce_only": True,
            })
            if res.success:
                ids.append(res.order_id)
        return ids

    async def replace_stop_order(self, symbol: str = "", symbol_id: int = 0,
                                 account_id: int = 0, new_stop_price: float = 0.0,
                                 old_stop_order_id: Optional[str] = None,
                                 side: str = "", size: float = 0.0,
                                 entry_price: float = 0.0, **_) -> OrderResult:
        """Position-level stop replace — atomic on Bybit, no cancel+replace chain."""
        stop_id = await self._set_position_stop(symbol, new_stop_price)
        if stop_id:
            return OrderResult(order_id=stop_id, status="open")
        return OrderResult(order_id="", status="rejected", error="trading-stop failed")

    async def close_position_market(self, symbol: str = "", symbol_id: int = 0,
                                    account_id: int = 0, side: str = "",
                                    size: float = 0.0, **_) -> OrderResult:
        close_side = "short" if side == "long" else "long"
        return await self.place_order({
            "symbol": symbol, "side": close_side, "qty": size,
            "order_type": "Market", "reduce_only": True,
        })

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def health_check(self) -> dict:
        try:
            await self._get("/v5/market/time", auth=False)
            public_ok = True
        except Exception:
            public_ok = False
        auth_ok = False
        if self.api_key:
            try:
                await self.get_account_balance()
                auth_ok = True
            except Exception:
                pass
        return {"public": public_ok, "auth": auth_ok, "testnet": self.testnet}

    def start_keepalive(self) -> None:
        pass   # REST-only client — no WS connection to keep alive

    async def close(self) -> None:
        await self._http.aclose()
