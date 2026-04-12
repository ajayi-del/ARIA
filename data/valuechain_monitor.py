"""
ValueChain RPC Monitor — ARIA v1.4  (Tier 6 On-Chain Intelligence)

Monitors SoDEX chain liquidation events via EVM JSON-RPC.
Chain ID: 286623 — block time 2-3s, same EVM ABI as Ethereum.

Signal logic:
  Long liquidation  → short-side pressure  → bearish signal
  Short liquidation → long-side pressure   → bullish signal
  3+ liquidations in 60s → cascade → DO NOT TRADE

Design principles:
  - Non-fatal: any RPC failure logs a warning and continues; Tiers 1-5 still run.
  - Uses eth_getLogs polling (no persistent WebSocket dependency).
  - Falls back through multiple RPC endpoints on failure.
  - Cascade guard is mandatory and never bypassed.
"""

import asyncio
import time
import httpx
import certifi
import structlog
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

log = structlog.get_logger(__name__)

# ── RPC endpoints (try in order) ─────────────────────────────────────────────
_RPC_ENDPOINTS = [
    "https://rpc.sodex.dev",
    "https://mainnet-rpc.sodex.dev",
    "https://chain-rpc.sodex.dev",
]
_CHAIN_ID = 286623
_POLL_INTERVAL_S = 3.0        # 1 block ≈ 2-3s
_LOOKBACK_BLOCKS = 5          # How many blocks back to scan on reconnect
_CASCADE_WINDOW_S = 60.0      # Window for cascade detection
_CASCADE_THRESHOLD = 3        # ≥N liquidations in window = cascade


@dataclass
class LiquidationEvent:
    """Parsed liquidation event from the SoDEX chain."""
    block_number: int
    tx_hash: str
    symbol: str           # perp symbol (e.g. "BTC-USD") or "" if unknown
    side: str             # "long" or "short" (the liquidated side)
    notional_usd: float   # approximate USD value liquidated
    timestamp: float      # unix timestamp (from block)
    raw_topics: List[str] = field(default_factory=list)


@dataclass
class LiquidationSignal:
    """Signal derived from a liquidation event."""
    symbol: str           # "" means all-market (relevant to all symbols)
    direction: str        # "bearish" (from long liq) or "bullish" (from short liq)
    cascade: bool         # True → DO NOT TRADE (too many liquidations)
    notional_usd: float
    timestamp: float
    event_count_60s: int  # How many liquidations in the last 60s


# ── Topic hashes for known SoDEX liquidation event signatures ────────────────
# We listen for any event matching known topic0 hashes for liquidation events.
# If the contract ABI is not yet known, we scan all events and parse by shape.
_LIQUIDATION_TOPIC0 = {
    # Standard futures liquidation topic (keccak256 of event signature)
    # We include common variants — if none match we fall back to heuristic parsing
    "0x" + "4b39c36da05c8b97aa06bd12a57a1a47d27dc3d52cf07c1d76ee53f98dac2b6c",  # Liquidate(address,uint256,uint256,bool)
    "0x" + "298637f684da70674f26509b10f07ec2fbc77a335ab1e7d6215a4b2484d8bb52",  # PositionLiquidated(...)
    "0x" + "3238d0da3c8d2d7ab4b56d3cc2cde7f07b88bba65e78ce7a5e36c60cc4d1a4a7",  # ForceLiquidation(...)
}

# Address → perp symbol mapping (populated heuristically or from ABI discovery)
# Key = lowercase contract address, value = "BTC-USD" etc.
_CONTRACT_TO_SYMBOL: Dict[str, str] = {}

# Fallback: Scan any address — we won't filter by contract address unless known
_FILTER_BY_ADDRESS = False


class ValueChainMonitor:
    """
    Polls SoDEX chain for liquidation events and publishes LiquidationSignal
    objects to registered callbacks.

    Usage:
        vc = ValueChainMonitor()
        vc.add_listener(my_callback)   # async def my_callback(sig: LiquidationSignal)
        asyncio.create_task(vc.run())

    Failure is non-fatal: any RPC error → log + retry next poll cycle.
    """

    def __init__(self):
        self._listeners: List = []
        self._recent_events: List[LiquidationEvent] = []  # sliding window
        self._last_block: int = 0
        self._rpc_index: int = 0          # which endpoint we're currently using
        self._healthy: bool = False
        self._last_block_time: float = 0.0
        self._consecutive_failures: int = 0
        self._http: Optional[httpx.AsyncClient] = None

    def add_listener(self, callback) -> None:
        """Register an async callback: async def cb(sig: LiquidationSignal)"""
        self._listeners.append(callback)

    def is_healthy(self) -> bool:
        return self._healthy and (time.time() - self._last_block_time) < 30.0

    def get_status(self) -> Dict:
        now = time.time()
        recent_60s = [e for e in self._recent_events if now - e.timestamp < 60.0]
        return {
            "healthy": self.is_healthy(),
            "last_block": self._last_block,
            "rpc_endpoint": _RPC_ENDPOINTS[self._rpc_index % len(_RPC_ENDPOINTS)],
            "events_60s": len(recent_60s),
            "cascade_active": len(recent_60s) >= _CASCADE_THRESHOLD,
            "consecutive_failures": self._consecutive_failures,
        }

    def is_cascade_active(self) -> bool:
        """True if ≥CASCADE_THRESHOLD liquidations occurred in the last 60s."""
        now = time.time()
        recent = [e for e in self._recent_events if now - e.timestamp < _CASCADE_WINDOW_S]
        return len(recent) >= _CASCADE_THRESHOLD

    async def run(self) -> None:
        """Main polling loop. Runs forever; never raises."""
        self._http = httpx.AsyncClient(
            verify=certifi.where(),
            timeout=8.0,
        )
        log.info("valuechain_monitor_started", chain_id=_CHAIN_ID)
        try:
            await self._run_loop()
        except asyncio.CancelledError:
            log.info("valuechain_monitor_cancelled")
        except Exception as e:
            log.error("valuechain_monitor_fatal", error=str(e))
        finally:
            if self._http:
                await self._http.aclose()

    async def _run_loop(self) -> None:
        while True:
            try:
                await self._poll_once()
                self._consecutive_failures = 0
                self._healthy = True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._consecutive_failures += 1
                self._healthy = False
                backoff = min(30.0, _POLL_INTERVAL_S * self._consecutive_failures)
                log.warning(
                    "valuechain_poll_failed",
                    error=str(e),
                    consecutive_failures=self._consecutive_failures,
                    next_retry_s=round(backoff, 1),
                )
                # Rotate RPC endpoint after 3 consecutive failures
                if self._consecutive_failures % 3 == 0:
                    self._rpc_index += 1
                    new_ep = _RPC_ENDPOINTS[self._rpc_index % len(_RPC_ENDPOINTS)]
                    log.warning("valuechain_rpc_rotate", new_endpoint=new_ep)
                await asyncio.sleep(backoff)
                continue

            await asyncio.sleep(_POLL_INTERVAL_S)

    async def _poll_once(self) -> None:
        """Fetch latest block, scan for liquidation logs, emit signals."""
        rpc = _RPC_ENDPOINTS[self._rpc_index % len(_RPC_ENDPOINTS)]

        # 1. Get latest block number
        latest = await self._rpc_call(rpc, "eth_blockNumber", [])
        if not isinstance(latest, str):
            raise ValueError(f"Unexpected eth_blockNumber result: {latest!r}")
        latest_block = int(latest, 16)

        # First run: start from recent blocks
        if self._last_block == 0:
            self._last_block = max(0, latest_block - _LOOKBACK_BLOCKS)
            log.info("valuechain_sync_start",
                     latest_block=latest_block,
                     scan_from=self._last_block)

        if latest_block <= self._last_block:
            return  # No new blocks

        # 2. Fetch logs for the block range
        from_hex = hex(self._last_block + 1)
        to_hex = hex(latest_block)
        log_filter: Dict = {
            "fromBlock": from_hex,
            "toBlock":   to_hex,
        }
        # If we know specific contract addresses, filter by them
        if _FILTER_BY_ADDRESS and _CONTRACT_TO_SYMBOL:
            log_filter["address"] = list(_CONTRACT_TO_SYMBOL.keys())

        raw_logs = await self._rpc_call(rpc, "eth_getLogs", [log_filter])
        self._last_block = latest_block
        self._last_block_time = time.time()

        if not isinstance(raw_logs, list) or not raw_logs:
            return

        # 3. Parse logs → liquidation events
        events = []
        for raw in raw_logs:
            ev = self._parse_log(raw, latest_block)
            if ev is not None:
                events.append(ev)

        if not events:
            return

        log.info("valuechain_liquidations_detected",
                 count=len(events),
                 blocks=f"{from_hex}..{to_hex}")

        # 4. Record events and prune sliding window
        now = time.time()
        self._recent_events.extend(events)
        self._recent_events = [
            e for e in self._recent_events
            if now - e.timestamp < _CASCADE_WINDOW_S * 2  # keep 2x window for safety
        ]

        # 5. Determine cascade state
        recent_60s = [e for e in self._recent_events if now - e.timestamp < _CASCADE_WINDOW_S]
        cascade = len(recent_60s) >= _CASCADE_THRESHOLD

        if cascade:
            log.warning("valuechain_cascade_detected",
                        events_60s=len(recent_60s),
                        threshold=_CASCADE_THRESHOLD)

        # 6. Emit signals for each event
        for ev in events:
            # Long liquidation → bearish pressure; Short liquidation → bullish
            direction = "bearish" if ev.side == "long" else "bullish"
            sig = LiquidationSignal(
                symbol=ev.symbol,
                direction=direction,
                cascade=cascade,
                notional_usd=ev.notional_usd,
                timestamp=ev.timestamp,
                event_count_60s=len(recent_60s),
            )
            for cb in self._listeners:
                try:
                    await cb(sig)
                except Exception as cb_err:
                    log.warning("valuechain_listener_error", error=str(cb_err))

    def _parse_log(self, raw: Dict, latest_block: int) -> Optional[LiquidationEvent]:
        """
        Parse a raw eth_getLogs entry.

        SoDEX liquidation events are identified by:
        - topic0 matching known liquidation event hashes, OR
        - Heuristic: log has ≥3 topics and data that can be decoded as amounts

        Since we may not have the exact ABI, we apply heuristic parsing.
        Returns None if not a liquidation event.
        """
        topics = raw.get("topics", [])
        if not topics:
            return None

        topic0 = topics[0].lower() if topics else ""

        # Check if topic0 matches known liquidation signatures
        is_known_liq = topic0 in _LIQUIDATION_TOPIC0

        # Heuristic fallback: any log with 3-4 topics from a contract we don't know
        # is treated as a potential liquidation if data is non-empty
        data = raw.get("data", "0x")
        has_data = data not in ("0x", "")

        if not is_known_liq and (len(topics) < 3 or not has_data):
            return None

        # Try to determine symbol from contract address
        address = raw.get("address", "").lower()
        symbol = _CONTRACT_TO_SYMBOL.get(address, "")

        # Try to determine side from topics/data
        # topic1 often encodes the liquidated address, topic2 might encode direction
        # Without full ABI this is heuristic — we alternate or default to "long"
        # for maximum usefulness (long liquidations are more common in bull markets)
        side = "long"
        if len(topics) >= 3:
            # Last nibble of topic2 as heuristic for side: odd = long, even = short
            try:
                t2_int = int(topics[2], 16)
                side = "long" if (t2_int % 2 == 1) else "short"
            except (ValueError, IndexError):
                pass

        # Estimate notional from data field (first 32-byte word)
        notional = 0.0
        try:
            if has_data:
                word = data[2:66]  # first 32 bytes
                if len(word) == 64:
                    val = int(word, 16)
                    # Assume 6 decimals (USDC) or 18 decimals; clamp to reasonable range
                    notional_6d = val / 1e6
                    notional_18d = val / 1e18
                    if 10.0 <= notional_6d <= 100_000_000:
                        notional = notional_6d
                    elif 10.0 <= notional_18d <= 100_000_000:
                        notional = notional_18d
        except Exception:
            pass

        block_num = int(raw.get("blockNumber", "0x0"), 16) if raw.get("blockNumber") else latest_block

        return LiquidationEvent(
            block_number=block_num,
            tx_hash=raw.get("transactionHash", ""),
            symbol=symbol,
            side=side,
            notional_usd=notional,
            timestamp=time.time(),
            raw_topics=topics,
        )

    async def _rpc_call(self, rpc: str, method: str, params: list):
        """Make a JSON-RPC call. Raises on HTTP error or RPC error."""
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1,
        }
        resp = await self._http.post(rpc, json=payload)
        if resp.status_code != 200:
            raise ConnectionError(f"RPC HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if "error" in data:
            raise ValueError(f"RPC error: {data['error']}")
        return data.get("result")
