"""Propr REST client — thin wrapper, venue semantics only.

Verified against docs/api.md (XBorgLabs/propr-docs, 2026-11-06):
- auth: X-API-Key header, 1200 req/min
- cancel returns 201 (treat 200/201 as success; 400 = already gone -> None)
- zero-quantity positions linger with status "open" -> filtered
- 5xx/timeout leaves order state UNKNOWN -> caller reconciles by intentId
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import requests


class ProprAPIError(Exception):
    def __init__(self, status_code: int, code: int | None, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"[{status_code}] {code}: {message}")


class ProprUnknownState(Exception):
    """5xx/timeout: the order may or may not exist. Reconcile by intentId."""


class ProprClient:
    def __init__(self, api_key: str, base_url: str, timeout: int = 30):
        if not api_key:
            raise ValueError("PROP_API_KEY required (app.propr.xyz/settings)")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.account_id: str = ""
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        })

    def _request(self, method: str, path: str,
                 params: dict | None = None, payload: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.request(
                method, url, params=params, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ProprUnknownState(str(exc)) from exc
        if resp.status_code >= 500:
            raise ProprUnknownState(f"{resp.status_code} {resp.text[:200]}")
        if resp.status_code >= 400:
            try:
                body = resp.json()
                code, message = body.get("code"), body.get("message", "unknown_error")
            except ValueError:
                code, message = None, resp.text[:200] or "unknown_error"
            raise ProprAPIError(resp.status_code, code, message)
        return resp.json()

    def _get(self, path: str, params: dict | None = None) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, payload: dict | None = None) -> Any:
        return self._request("POST", path, payload=payload)

    def _acct(self, suffix: str) -> str:
        if not self.account_id:
            raise ValueError("account_id not set — call setup() first")
        return f"/accounts/{self.account_id}{suffix}"

    # ── discovery ──

    def health(self) -> dict:
        return self._get("/health")

    def users_me(self) -> dict:
        return self._get("/users/me")

    def challenge_attempts(self, status: str | None = "active") -> list[dict]:
        params: dict[str, Any] = {"limit": 50}
        if status:
            params["status"] = status
        return self._get("/challenge-attempts", params=params).get("data", [])

    def setup(self, account_id: str | None = None) -> str:
        if account_id:
            self.account_id = account_id
            return account_id
        attempts = self.challenge_attempts(status="active")
        if not attempts:
            raise ValueError("no active challenge attempt on this key")
        self.account_id = attempts[0]["accountId"]
        return self.account_id

    def get_account(self) -> dict:
        return self._get(self._acct(""))

    def get_equity(self) -> Decimal:
        """Equity = balance + totalUnrealizedPnl + isolatedPositionMargin."""
        acct = self.get_account()
        equity = Decimal(str(acct.get("balance", "0")))
        equity += Decimal(str(acct.get("totalUnrealizedPnl", "0")))
        equity += Decimal(str(acct.get("isolatedPositionMargin", "0")))
        return equity

    # ── orders ──

    def get_orders(self, status: str | None = None, base: str | None = None,
                   limit: int = 100) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if base:
            params["base"] = base
        return self._get(self._acct("/orders"), params=params).get("data", [])

    def get_open_orders(self, base: str | None = None) -> list[dict]:
        """Resting orders = status open OR pending.

        Conditional legs (stop/take_profit) rest in `pending` after the entry
        fills — filtering on 'open' alone is blind to live TPSL. Terminal
        states (filled/cancelled/expired/rejected) excluded client-side.
        """
        orders = self.get_orders(status=None, base=base, limit=100)
        return [o for o in orders if o.get("status") in ("open", "pending")]

    def place_orders(self, body: dict) -> list[dict]:
        orders = body.get("orders", [])
        if len(orders) > 1 and not body.get("orderGroupId"):
            raise ValueError("batched orders require orderGroupId")
        return self._post(self._acct("/orders"), payload=body).get("data", [])

    def cancel_order(self, order_id: str) -> dict | None:
        try:
            return self._post(self._acct(f"/orders/{order_id}/cancel"))
        except ProprAPIError as exc:
            if exc.status_code == 400:
                return None  # already filled/cancelled/expired
            raise

    def cancel_all_open(self, base: str | None = None) -> int:
        n = 0
        for order in self.get_open_orders(base=base):
            self.cancel_order(order["orderId"])
            n += 1
        return n

    def find_by_intent(self, intent_id: str) -> dict | None:
        for order in self.get_orders(limit=100):
            if order.get("intentId") == intent_id:
                return order
        return None

    # ── positions / trades ──

    def get_positions(self, status: str | None = "open",
                      base: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"limit": 100}
        if status:
            params["status"] = status
        if base:
            params["base"] = base
        positions = self._get(self._acct("/positions"), params=params).get("data", [])
        return [p for p in positions if Decimal(str(p.get("quantity", "0"))) > 0]

    def close_position(self, asset: str) -> list[dict]:
        positions = self.get_positions(status="open", base=asset)
        if not positions:
            return []
        pos = positions[0]
        side = pos["positionSide"]
        return self.place_orders({"orders": [{
            "accountId": self.account_id,
            "intentId": self._close_intent(asset),
            "exchange": "hyperliquid",
            "type": "market",
            "side": "sell" if side == "long" else "buy",
            "positionSide": side,
            "productType": "perp",
            "timeInForce": "IOC",
            "asset": asset,
            "base": asset,
            "quote": "USDC",
            "quantity": pos["quantity"],
            "reduceOnly": True,
            "closePosition": True,
        }]})

    def _close_intent(self, asset: str) -> str:
        import time
        from ..ids import intent_ulid
        return intent_ulid(self.account_id, f"{asset}-close", int(time.time() * 1000))

    def get_trades(self, base: str | None = None, limit: int = 100) -> list[dict]:
        """Fills, newest first. Closing legs carry type="reduce"/"close" and
        a string realizedPnl (shape pinned live 2026-11-08)."""
        params: dict[str, Any] = {"limit": limit}
        if base:
            params["base"] = base
        return self._get(self._acct("/trades"), params=params).get("data", [])

    # ── leverage / margin ──

    def leverage_limits(self) -> dict:
        return self._get("/leverage-limits/effective")

    def get_margin_config(self, asset: str) -> dict:
        return self._get(self._acct(f"/margin-config/{asset}"))

    def set_leverage(self, asset: str, leverage: int,
                     margin_mode: str | None = None) -> dict:
        config = self.get_margin_config(asset)
        # Venue pins xyz: assets isolated-only (400 13305 on "cross",
        # probed live 2026-11-08). Preserve the existing config's mode;
        # prefix default only when the asset has no config yet.
        mode = (margin_mode or config.get("marginMode")
                or ("isolated" if asset.startswith("xyz:") else "cross"))
        return self._request("PUT", self._acct(f"/margin-config/{config['configId']}"),
                             payload={
                                 "exchange": "hyperliquid",
                                 "asset": asset,
                                 "marginMode": mode,
                                 "leverage": leverage,
                             })
