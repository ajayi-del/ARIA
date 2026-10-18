"""
Arcus venue client — Ed25519-signed REST API (api.arcus.xyz, Robinhood Chain
mainnet beta).

Arcus is ARIA's third execution venue alongside SoDEX (major) and Aster.
Auth: the API key IS an Ed25519 public key (64 hex); the signing key is the
matching private key. Two signing schemes (docs + live API verified
2026-10-15):

  Scheme 1 — placeOrder / cancelOrder / modifyOrder (+ batch elements):
    signed bytes = canonical JSON (keys sorted, no whitespace) of the typed
    payload {"ad","ai","ct","g","m","op","p","q","r","s","t","v"} with
    optional "c" (clientId) / "id" (orderId). p = price ÷ tickSize and
    q = size ÷ stepSize as INTEGERS (top-level tickSize — never tickTiers);
    ct = X-Timestamp (unix ns); g = goodTilTime µs × 1000 (ns).
    Enums: op 1=place 2=cancel 3=modify 4=placeUntriggered(TPSL);
    s BUY=0 SELL=1; t GTT=0 FOK=1 IOC=2 ALO=3; r reduceOnly 0/1; v=1.
    `ad` is the only case-folded field (lowercased).
  Scheme 2 — cancelAllOrders / setLeverage / scheduleCancel / WS auth:
    signed bytes = X-Timestamp + action + canonical_json(body), action =
    camelCase final path segment ("cancelAllOrders", "setLeverage",
    "scheduleCancel"); signed GETs sign ts+action only. HTTP method and
    path are NOT signed.

Wire facts that bite if missed:
  - goodTilTime (epoch MICROSECONDS, ≥1 month ahead) is REQUIRED on every
    order incl. IOC/FOK.
  - MARKET orders must be IOC and carry a price = protective slippage
    bound within 10% of mark.
  - Single-order TPSL = 501; TP/SL only via POST /v1/batchPlaceOrders
    groupings.
  - Leverage is NOT in the order payload — POST /v1/setLeverage (Scheme 2).
  - 202 = ACK (async, definitive state via orders WS); 200 = definitive.
  - 502 = Transmission, order NOT submitted — safe retry. 503 = status
    UNKNOWN — reconcile via orders/positions before any retry.

Hooks SoDEX lacks (quant census 2026-10-15, 1,368 closes):
  - Maker fee 0% base tier (SoDEX 0.012% — BTC churn $70.18/30d → $0).
    BETA CAVEAT: taker 0.0225% is promo (post-beta 0.045% > SoDEX 0.04%)
    → the Arcus thesis is maker-only; never route taker for cost reasons.
  - $5 min notional everywhere (SoDEX $10).
  - 68 markets incl. 34 equity + 8 ETF perps trading 24/7 under the band
    clock (no session force-close).
  - 10 isolated subaccounts per wallet, gasless internal transfers.
  - Dead man's switch (scheduleCancel — orders only, never positions).

Defaults INERT: arcus_enabled=False / no keys / no address → nothing
registers, order paths fail closed. Keys in .env (ARCUS_API_KEY,
ARCUS_SIGNING_KEY, ARCUS_ADDRESS), never in git (issue #17).
"""

from __future__ import annotations

import json
import re
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

import httpx
import structlog

from execution.schemas import OrderResult

logger = structlog.get_logger(__name__)

ARCUS_MAINNET = "https://api.arcus.xyz"

# Scheme-1 enums (docs.arcus.xyz/guides/websocket-trading, verified 2026-10-15)
_OP_PLACE, _OP_CANCEL, _OP_MODIFY = 1, 2, 3
_SIDE = {"BUY": 0, "SELL": 1}
_TIF = {"GTT": 0, "FOK": 1, "IOC": 2, "ALO": 3}
# ARIA order-type idiom → Arcus timeInForce. ALO = post-only (maker is FREE —
# the venue's core hook). MARKET is always IOC.
_TIF_MAP = {"GTC": "GTT", "PostOnly": "ALO", "IOC": "IOC", "FOK": "FOK"}

_GTT_HORIZON_US = 40 * 86_400 * 1_000_000   # 40 days in µs (≥1 month rule)
_MARKET_SLIPPAGE_PCT = 0.05                  # protective bound, ≤10% allowed
_CLIENT_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


class ArcusAPIError(Exception):
    def __init__(self, message: str, code: int = 0,
                 retry_after_ms: int = 0, safe_retry: bool = False):
        super().__init__(message)
        self.code = code
        self.retry_after_ms = retry_after_ms
        # 502 Transmission = order never reached the engine → blind retry OK.
        self.safe_retry = safe_retry


def to_arcus_symbol(canonical: str) -> str:
    """BTC-USD → BTC-USD (Arcus marketDisplayName matches ARIA canonical).
    Overrides only for venue naming mismatches."""
    return _ARCUS_SYM_OVERRIDE.get(canonical, canonical)


def to_canonical_symbol(arcus_sym: str) -> str:
    return _CANONICAL_SYM_OVERRIDE.get(arcus_sym, arcus_sym)


_ARCUS_SYM_OVERRIDE: Dict[str, str] = {}
_CANONICAL_SYM_OVERRIDE: Dict[str, str] = {}


def _dec_str(value: float) -> str:
    """Human-readable decimal string — the wire forbids exponent notation."""
    return format(Decimal(repr(float(value))).normalize(), "f")


def _ticks(price: float, tick: float) -> int:
    """price → integer ticks. Exactness enforced by construction: callers
    round the price to the tick grid BEFORE converting."""
    if tick <= 0 or price <= 0:
        raise ArcusAPIError(f"bad tick conversion: price={price} tick={tick}")
    t = Decimal(repr(float(price))) / Decimal(repr(float(tick)))
    if t != t.to_integral_value():
        raise ArcusAPIError(f"price {price} not on tick grid {tick}")
    return int(t)


def _quantums(size: float, step: float) -> int:
    if step <= 0 or size <= 0:
        raise ArcusAPIError(f"bad step conversion: size={size} step={step}")
    q = Decimal(repr(float(size))) / Decimal(repr(float(step)))
    if q != q.to_integral_value():
        raise ArcusAPIError(f"size {size} not on step grid {step}")
    return int(q)


def _round_step(value: float, step: float, floor: bool = False,
                ceil: bool = False) -> float:
    if step <= 0:
        return value
    n = Decimal(repr(float(value))) / Decimal(repr(float(step)))
    if floor:
        n = n.to_integral_value(rounding="ROUND_FLOOR")
    elif ceil:
        n = n.to_integral_value(rounding="ROUND_CEILING")
    else:
        n = n.to_integral_value(rounding="ROUND_HALF_UP")
    return float(n * Decimal(repr(float(step))))


class ArcusClient:
    def __init__(self, config):
        self.config = config
        self.enabled = bool(getattr(config, "arcus_enabled", False))
        self.api_key = (getattr(config, "arcus_api_key", "") or "").strip()
        self.signing_key = (getattr(config, "arcus_signing_key", "") or "").strip()
        self.address = (getattr(config, "arcus_address", "") or "").strip()
        self.account_index = int(getattr(config, "arcus_account_index", 0) or 0)
        self._http = httpx.AsyncClient(base_url=ARCUS_MAINNET, timeout=10.0)
        # canonical symbol → {"market_id","tick","step","min_notional",
        #                     "min_size","max_size"}
        self._specs: Dict[str, Dict[str, Any]] = {}
        self._leverage_set: Dict[str, int] = {}
        self._last_ts: int = 0
        self._signer = None
        self._keys_ready = False
        self._init_signer()

    def _init_signer(self) -> None:
        """Fail-closed: mismatched/absent keys → order paths refuse."""
        if not (self.api_key and self.signing_key):
            return
        try:
            from nacl.signing import SigningKey
            self._signer = SigningKey(bytes.fromhex(self.signing_key))
            derived = self._signer.verify_key.encode().hex()
            if derived.lower() != self.api_key.lower():
                logger.error("arcus_key_mismatch",
                             note="signing key pubkey != ARCUS_API_KEY — refusing orders")
                self._signer = None
                return
            self._keys_ready = True
        except Exception as e:
            logger.error("arcus_signer_init_failed", error=str(e)[:160])
            self._signer = None

    # ── Auth ─────────────────────────────────────────────────────────────────

    def _ts_ns(self) -> int:
        """unix ns, strictly increasing per process."""
        t = time.time_ns()
        if t <= self._last_ts:
            t = self._last_ts + 1
        self._last_ts = t
        return t

    @staticmethod
    def _canonical(obj: Dict[str, Any]) -> str:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"))

    def _sign_bytes(self, msg: bytes) -> str:
        if self._signer is None:
            raise ArcusAPIError("arcus signer not ready (keys missing/mismatched)")
        return self._signer.sign(msg).signature.hex()

    def _order_headers(self, payload: Dict[str, Any]) -> Dict[str, str]:
        """Scheme 1: sign the canonical typed payload; ct inside == header.
        Underscore-prefixed keys are local plumbing, never signed/sent."""
        wire = {k: v for k, v in payload.items() if not k.startswith("_")}
        ts = int(wire["ct"])
        sig = self._sign_bytes(self._canonical(wire).encode())
        return {"X-API-Key": self.api_key, "X-Timestamp": str(ts),
                "X-Signature": sig}

    def _legacy_headers(self, action: str, body: Optional[Dict[str, Any]],
                        ts: int) -> Dict[str, str]:
        """Scheme 2: ed25519(ts + action + canonical_json(body))."""
        msg = str(ts) + action + (self._canonical(body) if body else "")
        sig = self._sign_bytes(msg.encode())
        return {"X-API-Key": self.api_key, "X-Timestamp": str(ts),
                "X-Signature": sig}

    async def _request(self, method: str, path: str,
                       params: Optional[dict] = None,
                       body: Optional[dict] = None,
                       headers: Optional[dict] = None) -> Any:
        resp = await self._http.request(method, path, params=params,
                                        json=body, headers=headers)
        if resp.status_code == 503:
            # Sent but no response — execution status UNKNOWN; reconcile via
            # orders/positions, never retry blind.
            raise ArcusAPIError(f"{path}: 503 status UNKNOWN — reconcile before retry",
                                code=503)
        try:
            data = resp.json()
        except Exception:
            raise ArcusAPIError(f"{path}: HTTP {resp.status_code} non-JSON body",
                                code=resp.status_code)
        if resp.status_code >= 400:
            msg = ""
            if isinstance(data, dict):
                msg = (data.get("error") or data.get("message")
                       or data.get("errorType") or str(data)[:160])
            retry_ms = 0
            if isinstance(data, dict):
                try:
                    retry_ms = int(data.get("retryAfterMs", 0) or 0)
                except (TypeError, ValueError):
                    retry_ms = 0
            raise ArcusAPIError(
                f"{path}: {msg or 'unknown'}", code=resp.status_code,
                retry_after_ms=retry_ms,
                safe_retry=(resp.status_code == 502))
        return data

    # ── Specs ────────────────────────────────────────────────────────────────

    async def sync_symbol_specs(self, symbols: List[str]) -> int:
        """Fetch tick/step/min-notional/marketId from GET /v1/markets."""
        synced = 0
        wanted = {to_arcus_symbol(s): s for s in symbols}
        try:
            data = await self._request("GET", "/v1/markets")
        except ArcusAPIError as e:
            logger.warning("arcus_spec_sync_failed", error=str(e)[:120])
            return 0
        markets = data if isinstance(data, list) else (
            data.get("markets") or data.get("data") or [])
        for item in markets:
            canonical = wanted.get(item.get("marketDisplayName", ""))
            if not canonical or item.get("status") != "ONLINE":
                continue
            try:
                self._specs[canonical] = {
                    "market_id": int(item["marketId"]),
                    "tick": float(item["tickSize"]),
                    "step": float(item["stepSize"]),
                    "min_notional": float(item.get("minOrderNotional", 5) or 5),
                    "min_size": float(item.get("minOrderSize", 0) or 0),
                    "max_size": float(item.get("maxOrderSize", 0) or 0),
                }
                synced += 1
            except (KeyError, TypeError, ValueError) as e:
                logger.warning("arcus_spec_row_bad", symbol=canonical,
                               error=str(e)[:120])
        if synced:
            logger.info("arcus_symbol_specs_synced", count=synced)
        return synced

    def get_spec(self, symbol: str) -> Dict[str, Any]:
        return self._specs.get(
            symbol, {"market_id": 0, "tick": 0.0, "step": 0.0,
                     "min_notional": 5.0, "min_size": 0.0, "max_size": 0.0})

    def market_id(self, symbol: str) -> int:
        return int(self.get_spec(symbol).get("market_id", 0))

    def listed(self, symbol: str) -> bool:
        """True only if /v1/markets confirmed this symbol ONLINE on Arcus."""
        return symbol in self._specs

    # ── Account / positions (public reads — address-parametrized) ────────────

    async def get_account_balance(self, account_index: Optional[int] = None) -> float:
        """Account equity for (address, accountIndex). venue.combined_balance
        passes the wallet ADDRESS positionally (venue-agnostic idiom) —
        coerce anything non-numeric to the configured account_index instead
        of raising (2026-10-18 venue_balance_failed spam)."""
        try:
            ai = self.account_index if account_index is None else int(account_index)
        except (TypeError, ValueError):
            ai = self.account_index
        data = await self._request(
            "GET", "/v1/account",
            params={"address": self.address, "accountIndex": ai},
            headers={"X-API-Key": self.api_key} if self.api_key else None)
        if not isinstance(data, dict):
            return 0.0
        for key in ("equity", "accountEquity", "totalEquity", "balance"):
            if data.get(key) is not None:
                try:
                    return float(data[key])
                except (TypeError, ValueError):
                    pass
        # Fall back to collateral + uPnL shapes when present
        try:
            coll = float(data.get("collateral", 0) or 0)
            upnl = float(data.get("unrealizedPnl", 0) or 0)
            return coll + upnl
        except (TypeError, ValueError):
            return 0.0

    async def get_positions(self, address: str = "",
                            account_index: Optional[int] = None) -> List[Dict]:
        ai = self.account_index if account_index is None else int(account_index)
        data = await self._request(
            "GET", "/v1/positions",
            params={"address": address or self.address, "accountIndex": ai})
        rows = data if isinstance(data, list) else (
            data.get("positions") or data.get("data") or [])
        out = []
        for p in rows:
            try:
                size = float(p.get("size", 0) or 0)
                if size == 0:
                    continue
                canonical = to_canonical_symbol(
                    p.get("marketDisplayName") or p.get("market") or "")
                side = p.get("side", "")
                out.append({
                    "symbol": canonical, "coin": canonical,
                    "side": "long" if str(side).upper() in ("LONG", "BUY") or size > 0
                            else "short",
                    "size": abs(size), "qty": abs(size),
                    "entry": float(p.get("entryPrice", 0) or 0),
                    "avgPrice": float(p.get("entryPrice", 0) or 0),
                    "markPrice": float(p.get("markPrice", 0) or 0),
                    "upnl": float(p.get("unrealizedPnl", 0) or 0),
                    "leverage": int(float(p.get("leverage", 1) or 1)),
                    "venue": "arcus",
                })
            except (TypeError, ValueError):
                continue
        return out

    async def get_open_orders(self, address: str = "",
                              account_index: Optional[int] = None) -> List[Dict]:
        ai = self.account_index if account_index is None else int(account_index)
        data = await self._request(
            "GET", "/v1/openOrders",
            params={"address": address or self.address, "accountIndex": ai})
        rows = data if isinstance(data, list) else (
            data.get("orders") or data.get("data") or [])
        out = []
        for o in rows:
            try:
                out.append({
                    "orderID": str(o.get("orderId", "")),
                    "clOrdID": o.get("clientId", ""),
                    "symbol": to_canonical_symbol(
                        o.get("marketDisplayName") or o.get("market") or ""),
                    "side": o.get("orderSide") or o.get("side", ""),
                    "price": float(o.get("price", 0) or 0),
                    "quantity": float(o.get("quantity", 0) or 0),
                    "stopPrice": float(o.get("stopPrice", 0) or 0),
                    "reduceOnly": bool(o.get("reduceOnly", False)),
                    "status": o.get("status", ""),
                    "venue": "arcus",
                })
            except (TypeError, ValueError):
                continue
        return out

    async def get_mark_price(self, symbol: str) -> float:
        data = await self._request("GET", "/v1/markets",
                                   params={"market": to_arcus_symbol(symbol)})
        markets = data if isinstance(data, list) else (
            data.get("markets") or data.get("data") or [])
        for item in markets:
            if item.get("marketDisplayName") == to_arcus_symbol(symbol):
                return float(item.get("markPrice", 0) or 0)
        return 0.0

    # ── Leverage (Scheme 2) ──────────────────────────────────────────────────

    async def update_leverage(self, symbol: str, leverage: int,
                              account_index: Optional[int] = None,
                              isolated: Optional[bool] = None) -> bool:
        if not self._keys_ready or not self.address:
            logger.warning("arcus_leverage_refused", reason="keys_or_address_missing")
            return False
        mid = self.market_id(symbol)
        if mid <= 0:
            logger.warning("arcus_leverage_failed", symbol=symbol,
                           error="unlisted_market")
            return False
        ai = self.account_index if account_index is None else int(account_index)
        body: Dict[str, Any] = {"address": self.address, "marketId": mid,
                                "leverage": int(leverage), "accountIndex": ai}
        if isolated is not None:
            body["isolated"] = bool(isolated)
        try:
            ts = self._ts_ns()
            await self._request(
                "POST", "/v1/setLeverage", params={"address": self.address},
                body=body,
                headers=self._legacy_headers("setLeverage", body, ts))
            self._leverage_set[symbol] = int(leverage)
            return True
        except ArcusAPIError as e:
            logger.warning("arcus_leverage_failed", symbol=symbol,
                           leverage=leverage, error=str(e)[:120])
            return False

    # ── Orders (Scheme 1) ────────────────────────────────────────────────────

    def _signed_order_payload(self, symbol: str, side: str, qty: float,
                              price: float, tif: str, reduce_only: bool,
                              gtt_us: int, client_id: str = "") -> Dict[str, Any]:
        spec = self.get_spec(symbol)
        # Side-aware tick rounding: post-only buys floor / sells ceil so the
        # quote never crosses the touch (the 2026-09-22 Aster doctrine).
        if tif == "ALO":
            px = _round_step(price, spec["tick"], floor=(side == "long"),
                             ceil=(side != "long"))
        else:
            px = _round_step(price, spec["tick"])
        qty_r = _round_step(qty, spec["step"], floor=True)
        payload: Dict[str, Any] = {
            "ad": self.address.lower(),
            "ai": self.account_index,
            "ct": self._ts_ns(),
            "g": int(gtt_us) * 1000,
            "m": int(spec["market_id"]),
            "op": _OP_PLACE,
            "p": _ticks(px, spec["tick"]),
            "q": _quantums(qty_r, spec["step"]),
            "r": 1 if reduce_only else 0,
            "s": _SIDE["BUY" if side == "long" else "SELL"],
            "t": _TIF[tif],
            "v": 1,
        }
        if client_id:
            payload["c"] = _CLIENT_ID_RE.sub("", client_id)[:36]
        payload["_px"], payload["_qty"] = px, qty_r
        return payload

    async def place_order(self, order_data: Dict[str, Any],
                          account_index: Optional[int] = None) -> OrderResult:
        symbol = order_data["symbol"]
        if not (self.enabled and self._keys_ready and self.address):
            return OrderResult(order_id="", status="rejected",
                               error="arcus_not_armed (enabled/keys/address)")
        spec = self.get_spec(symbol)
        if not spec.get("market_id"):
            return OrderResult(order_id="", status="rejected",
                               error="arcus_unlisted_symbol")
        side = order_data["side"]
        qty = float(order_data["qty"])
        order_type = order_data.get("order_type", "MARKET")
        reduce_only = bool(order_data.get("reduce_only", False))
        link_id = order_data.get("link_id", "")
        ai = self.account_index if account_index is None else int(account_index)
        gtt_us = int(time.time() * 1_000_000) + _GTT_HORIZON_US

        if order_type == "MARKET":
            tif = "IOC"
            price = float(order_data.get("price", 0.0) or 0.0)
            if price <= 0:
                try:
                    mark = await self.get_mark_price(symbol)
                except ArcusAPIError as e:
                    return OrderResult(order_id="", status="rejected",
                                       error=f"mark_unavailable: {e}")
                if mark <= 0:
                    return OrderResult(order_id="", status="rejected",
                                       error="mark_unavailable")
                price = mark * (1 + _MARKET_SLIPPAGE_PCT if side == "long"
                                else 1 - _MARKET_SLIPPAGE_PCT)
        else:
            tif = _TIF_MAP.get(order_data.get("time_in_force", "GTC"), "GTT")
            price = float(order_data.get("price", 0.0) or 0.0)
            if price <= 0:
                return OrderResult(order_id="", status="rejected",
                                   error="limit_price_missing")
        try:
            saved_ai = self.account_index
            self.account_index = ai
            try:
                payload = self._signed_order_payload(
                    symbol, side, qty, price, tif, reduce_only, gtt_us, link_id)
            finally:
                self.account_index = saved_ai
        except ArcusAPIError as e:
            return OrderResult(order_id="", status="rejected", error=str(e))

        body = {
            "address": self.address,
            "marketId": int(spec["market_id"]),
            "accountIndex": ai,
            "orderSide": "BUY" if side == "long" else "SELL",
            "orderType": order_type,
            "quantity": _dec_str(payload["_qty"]),
            "price": _dec_str(payload["_px"]),
            "timeInForce": tif,
            "goodTilTime": str(gtt_us),
            "timestamp": payload["ct"],
        }
        if reduce_only:
            body["reduceOnly"] = True
        if payload.get("c"):
            body["clientId"] = payload["c"]
        try:
            result = await self._request(
                "POST", "/v1/placeOrder", params={"address": self.address},
                body=body, headers=self._order_headers(payload))
            order_id = ""
            if isinstance(result, dict):
                order_id = str(result.get("orderId")
                               or (result.get("result") or {}).get("orderId", ""))
            status = "open"
            if isinstance(result, dict) and result.get("rejectionReason"):
                return OrderResult(
                    order_id=order_id, status="rejected",
                    error=str(result.get("rejectionReason")))
            return OrderResult(order_id=order_id, status=status)
        except ArcusAPIError as e:
            logger.warning("arcus_order_rejected", symbol=symbol, side=side,
                           error=str(e)[:160])
            return OrderResult(order_id="", status="rejected", error=str(e))

    async def cancel_order(self, order_id: str, symbol: str = "",
                           client_id: str = "", account_index: Optional[int] = None,
                           **_) -> bool:
        if not (self._keys_ready and self.address):
            return False
        mid = self.market_id(symbol)
        if mid <= 0:
            return False
        ai = self.account_index if account_index is None else int(account_index)
        payload: Dict[str, Any] = {
            "ad": self.address.lower(), "ai": ai, "ct": self._ts_ns(),
            "m": mid, "op": _OP_CANCEL, "v": 1,
        }
        body: Dict[str, Any] = {"address": self.address, "marketId": mid,
                                "accountIndex": ai, "timestamp": payload["ct"]}
        if client_id and not order_id:
            payload["c"] = _CLIENT_ID_RE.sub("", client_id)[:36]
            body["kind"] = "clientId"
            body["clientId"] = payload["c"]
        else:
            payload["id"] = str(order_id)
            body["kind"] = "orderId"
            body["orderId"] = str(order_id)
        try:
            await self._request(
                "POST", "/v1/cancelOrder", params={"address": self.address},
                body=body, headers=self._order_headers(payload))
            return True
        except ArcusAPIError as e:
            logger.warning("arcus_cancel_failed", symbol=symbol,
                           order_id=order_id, error=str(e)[:120])
            return False

    async def cancel_all_orders(self, symbol: str = "",
                                account_index: Optional[int] = None) -> bool:
        """Scheme-2 account-wide (or market-scoped) cancel."""
        if not (self._keys_ready and self.address):
            return False
        ai = self.account_index if account_index is None else int(account_index)
        body: Dict[str, Any] = {"address": self.address, "accountIndex": ai}
        if symbol:
            mid = self.market_id(symbol)
            if mid <= 0:
                return False
            body["marketId"] = mid
        try:
            ts = self._ts_ns()
            await self._request(
                "POST", "/v1/cancelAllOrders", params={"address": self.address},
                body=body,
                headers=self._legacy_headers("cancelAllOrders", body, ts))
            return True
        except ArcusAPIError as e:
            logger.warning("arcus_cancel_all_failed", symbol=symbol,
                           error=str(e)[:120])
            return False

    # ── Dead man's switch (Scheme 2) ─────────────────────────────────────────

    async def set_deadman_switch(self, seconds_ahead: Optional[float],
                                 symbol: str = "",
                                 account_index: Optional[int] = None) -> bool:
        """Arm/refresh (deadline = now + seconds_ahead, venue clamps to the
        5s–5min window) or disarm (seconds_ahead=None). ORDERS ONLY — the
        switch never touches positions. Venue cap: 10 auto-fires/UTC
        day/subaccount; per-market switches are independent."""
        if not (self._keys_ready and self.address):
            return False
        ai = self.account_index if account_index is None else int(account_index)
        body: Dict[str, Any] = {"address": self.address, "accountIndex": ai}
        if symbol:
            mid = self.market_id(symbol)
            if mid <= 0:
                return False
            body["marketId"] = mid
        if seconds_ahead is not None:
            body["time"] = int((time.time() + float(seconds_ahead)) * 1_000_000)
        try:
            ts = self._ts_ns()
            await self._request(
                "POST", "/v1/scheduleCancel", params={"address": self.address},
                body=body,
                headers=self._legacy_headers("scheduleCancel", body, ts))
            return True
        except ArcusAPIError as e:
            logger.warning("arcus_deadman_failed", symbol=symbol,
                           error=str(e)[:120])
            return False

    # ── Close (MARKET IOC reduce-only) ───────────────────────────────────────

    async def close_position_market(self, symbol: str = "", side: str = "",
                                    qty: float = 0.0, size: float = 0.0,
                                    account_index: Optional[int] = None,
                                    **_) -> OrderResult:
        close_side = "short" if side == "long" else "long"
        req_qty = qty or size
        return await self.place_order({
            "symbol": symbol, "side": close_side, "qty": req_qty,
            "order_type": "MARKET", "reduce_only": True,
        }, account_index=account_index)

    # ── Health ───────────────────────────────────────────────────────────────

    async def health_check(self) -> dict:
        try:
            await self._request("GET", "/v1/markets",
                                params={"market": "BTC-USD"})
            out = {"venue": "arcus", "status": "ok",
                   "keys_ready": self._keys_ready,
                   "address_set": bool(self.address),
                   "specs": len(self._specs)}
            if self.address:
                try:
                    out["equity"] = await self.get_account_balance()
                except ArcusAPIError as e:
                    out["equity_error"] = str(e)[:120]
            return out
        except Exception as e:
            return {"venue": "arcus", "status": "error", "error": str(e)[:120]}

    async def close(self) -> None:
        await self._http.aclose()
