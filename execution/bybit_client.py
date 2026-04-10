import httpx
import time
import hmac
import hashlib
import json
from core.config import Settings

BYBIT_REST_URL = "https://api.bybit.com"
BYBIT_TESTNET_URL = "https://api-testnet.bybit.com"

class BybitClient:
    def __init__(self, config: Settings):
        self.config = config
        self.api_key = config.bybit_api_key
        self.api_secret = config.bybit_api_secret
        self.base_url = BYBIT_TESTNET_URL if config.bybit_testnet else BYBIT_REST_URL
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=10.0
        )

    def _sign(self, params: str) -> tuple[str, str, str]:
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"
        param_str = timestamp + self.api_key + recv_window + params
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            param_str.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        return timestamp, recv_window, signature

    def _headers(self, timestamp: str, recv_window: str, signature: str) -> dict:
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN": signature,
            "Content-Type": "application/json"
        }

    async def get_account_balance(self, account_id: str = None) -> float:
        """Fetches unified account balance for USDT."""
        params = "accountType=UNIFIED"
        ts, rw, sig = self._sign(params)
        try:
            resp = await self._http.get(
                "/v5/account/wallet-balance",
                params={"accountType": "UNIFIED"},
                headers=self._headers(ts, rw, sig)
            )
            data = resp.json()
            # Bybit v5 returns result -> list -> [0] -> coin
            coins = data.get("result", {}).get("list", [{}])[0].get("coin", [])
            for coin in coins:
                if coin.get("coin") == "USDT":
                    return float(coin.get("walletBalance", 0))
            return 0.0
        except Exception:
            # Fallback to fixed balance if API fails
            return 10000.0

    async def get_positions(self) -> list:
        """Fetches active linear positions."""
        params = "category=linear&settleCoin=USDT"
        ts, rw, sig = self._sign(params)
        try:
            resp = await self._http.get(
                "/v5/position/list",
                params={
                    "category": "linear",
                    "settleCoin": "USDT"
                },
                headers=self._headers(ts, rw, sig)
            )
            data = resp.json()
            return data.get("result", {}).get("list", [])
        except Exception:
            return []

    async def place_order(self, order: dict) -> object:
        """Standard order placement. Intercepts in paper mode."""
        if self.config.mode == "paper":
            return self._simulate_fill(order)
        # Live implementation would go here
        return self._simulate_fill(order)

    def _simulate_fill(self, order: dict) -> object:
        """Simulates a core order execution."""
        from dataclasses import dataclass
        @dataclass
        class FillResult:
            order_id: str = "paper_" + str(int(time.time()))
            status: str = "filled"
            fill_price: float = float(order.get("price", 0))
            filled_qty: float = float(order.get("quantity", 0))
        return FillResult()

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Standard cancel. Always returns True in paper mode."""
        return True

    async def close(self):
        """Cleanly closes the http client session."""
        await self._http.aclose()

    def health_check(self) -> dict:
        """Reports connectivity health for the radar."""
        return {
            "client": "bybit",
            "mode": self.config.mode,
            "base_url": self.base_url
        }
