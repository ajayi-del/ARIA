"""
Emergency Execution Module
v1.3 Hardened: High-speed position flattening.
Bypasses all risk checks and order managers for immediate exit.
"""

import asyncio
import httpx
import structlog
from typing import List, Dict, Any
from core.config import Settings
from execution.signer import SoDEXSigner

logger = structlog.get_logger(__name__)

class EmergencyFlatten:
    """
    Dependency-free high-speed execution for emergency shutdowns.
    Uses its own httpx client to ensure it doesn't get blocked by other IO.
    """
    def __init__(self, config: Settings, signer: SoDEXSigner):
        self.config = config
        self.signer = signer
        self.base_url = "https://testnet-gw.sodex.dev/api/v1/perps" if config.mode == "testnet" else "https://mainnet-gw.sodex.dev/api/v1/perps"
        self._client = httpx.AsyncClient(timeout=5.0)

    async def flatten_all(self):
        """
        1. Cancels all open orders.
        2. Market closes all open positions.
        """
        logger.warning("EMERGENCY_FLATTEN_STARTED")
        
        try:
            # Step 1: Cancel All
            await self._cancel_all()
            
            # Step 2: Close All
            await self._close_all()
            
            logger.info("EMERGENCY_FLATTEN_COMPLETE")
        except Exception as e:
            logger.error("EMERGENCY_FLATTEN_FAILED", error=str(e))
        finally:
            await self._client.aclose()

    async def _cancel_all(self):
        """Sends mass cancellation request."""
        payload = {"type": "cancelAll"}
        nonce = int(asyncio.get_event_loop().time() * 1000) # Simple nonce for emergency
        
        # In emergency we might not have the real nonce manager, 
        # but SoDEX requires unique nonces. We'll use high-precision timestamp.
        sig = self.signer.sign_payload(payload, nonce)
        
        headers = {
            "X-Sodex-Address": self.signer.get_address(),
            "X-Sodex-Signature": sig,
            "X-Sodex-Nonce": str(nonce)
        }
        
        response = await self._client.post(f"{self.base_url}/cancelAll", json=payload, headers=headers)
        logger.info("emergency_cancel_all_sent", status=response.status_code)

    async def _close_all(self):
        """Fetches positions and market closes them immediately."""
        # 1. Fetch positions
        pos_resp = await self._client.get(f"{self.base_url}/positions?accountID={self.signer.get_address()}")
        if pos_resp.status_code != 200:
            logger.error("emergency_positions_fetch_failed")
            return
            
        positions = pos_resp.json()
        if not positions:
            logger.info("no_positions_to_flatten")
            return
            
        # 2. Sequential Market Close
        for pos in positions:
            symbol = pos["symbol"]
            size = pos["size"]
            side = pos["side"] # 1=long, 2=short
            
            close_side = 2 if side == 1 else 1
            
            close_payload = {
                "type": "newOrder",
                "params": {
                    "orders": [{
                        "clOrdID": f"emergency_close_{symbol}",
                        "side": close_side,
                        "type": 1, # Market
                        "quantity": str(size),
                        "reduceOnly": True
                    }]
                }
            }
            
            nonce = int(asyncio.get_event_loop().time() * 1000)
            sig = self.signer.sign_payload(close_payload, nonce)
            
            headers = {
                "X-Sodex-Address": self.signer.get_address(),
                "X-Sodex-Signature": sig,
                "X-Sodex-Nonce": str(nonce)
            }
            
            resp = await self._client.post(f"{self.base_url}/order", json=close_payload, headers=headers)
            logger.info("emergency_close_sent", symbol=symbol, status=resp.status_code)
