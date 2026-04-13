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
# ValueChain mainnet — Chain ID 286623 (0x45f9f)
# Block explorer: https://main-scan.valuechain.xyz
# RPC endpoints rotated on failure — valuechain.xyz domain confirmed by user
_RPC_ENDPOINTS = [
    "https://rpc.valuechain.xyz",           # confirmed HTTP 200 in logs
    "https://main-scan.valuechain.xyz/api/eth-rpc",
    "https://mainnet.valuechain.xyz/rpc",   # explicit /rpc path
    "https://mainnet.valuechain.xyz",
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


_CASCADE_COOLDOWN_MS = 90_000   # 90s between cascade signal emissions


class ValueChainMonitor:
    """
    Polls SoDEX chain for liquidation events and publishes LiquidationSignal
    objects to registered callbacks.

    v1.8: Cascade deduplication — cascade signal fires ONCE per 90s batch,
    not once per individual liquidation event. Prevents 30× signal flooding.

    Usage:
        vc = ValueChainMonitor()
        vc.add_listener(my_callback)   # async def my_callback(sig: LiquidationSignal)
        asyncio.create_task(vc.run())

    Failure is non-fatal: any RPC error → log + retry next poll cycle.
    """

    def __init__(self, calendar_engine=None):
        self._listeners: List = []
        self._recent_events: List[LiquidationEvent] = []  # sliding window
        self._last_block: int = 0
        self._rpc_index: int = 0          # which endpoint we're currently using
        self._healthy: bool = False
        self._last_block_time: float = 0.0
        self._consecutive_failures: int = 0
        self._http: Optional[httpx.AsyncClient] = None
        self._calendar = calendar_engine  # Optional CalendarEngine — gates signal emission
        self._last_cascade_signal_ms: int = 0  # Dedup: track last cascade signal time
        # On-chain position flow tracking (v1.7)
        from collections import deque
        self._position_flow: dict = {}  # symbol -> deque({side, size_usd, ts_ms})
        self._flow_signals: dict = {}   # symbol -> {direction, score, ts_ms}

    def add_listener(self, callback) -> None:
        """Register an async callback: async def cb(sig: LiquidationSignal)"""
        self._listeners.append(callback)

    def is_healthy(self) -> bool:
        return self._healthy and (time.time() - self._last_block_time) < 30.0

    def get_status(self) -> Dict:
        now = time.time()
        recent_60s = [e for e in self._recent_events if now - e.timestamp < 60.0]
        now_ms_ts = int(now * 1000)
        active_signals = [
            {
                "symbol": sym,
                "source": "oi_flow",
                "direction": sig.get("direction", "none"),
                "strength": round(sig.get("score", 0.0), 2),
                "age_s": round((now_ms_ts - sig.get("ts_ms", now_ms_ts)) / 1000),
            }
            for sym, sig in self._flow_signals.items()
            if now_ms_ts - sig.get("ts_ms", 0) < 300_000
        ]
        return {
            "healthy": self.is_healthy(),
            "last_block": self._last_block,
            "rpc_endpoint": _RPC_ENDPOINTS[self._rpc_index % len(_RPC_ENDPOINTS)],
            "events_60s": len(recent_60s),
            "cascade_active": len(recent_60s) >= _CASCADE_THRESHOLD,
            "consecutive_failures": self._consecutive_failures,
            "active_signals": active_signals,
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

        # 3a. Ingest position flow from all raw logs (position flow tracker)
        self._ingest_position_logs(raw_logs)

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

        # 6. Emit signals
        # ── CASCADE: emit ONCE per 90s batch with AGGREGATE notional ──────────
        # This prevents 30× signal flooding when many liquidations arrive in one
        # poll cycle. Only one cascade signal is fired per cooldown window.
        if cascade:
            now_ms = int(now * 1000)
            if (now_ms - self._last_cascade_signal_ms) >= _CASCADE_COOLDOWN_MS:
                self._last_cascade_signal_ms = now_ms
                # Aggregate across all events in the cascade window
                total_notional = sum(e.notional_usd for e in recent_60s)
                # Direction: majority vote on liquidation side
                bearish_count = sum(1 for e in recent_60s if e.side == "long")
                bullish_count = len(recent_60s) - bearish_count
                agg_direction = "bearish" if bearish_count >= bullish_count else "bullish"
                # Symbol: use "" for market-wide cascade (individual symbols may vary)
                cascade_sig = LiquidationSignal(
                    symbol="",
                    direction=agg_direction,
                    cascade=True,
                    notional_usd=total_notional,
                    timestamp=now,
                    event_count_60s=len(recent_60s),
                )
                for cb in self._listeners:
                    try:
                        await cb(cascade_sig)
                    except Exception as cb_err:
                        log.warning("valuechain_cascade_listener_error", error=str(cb_err))
                log.info("valuechain_cascade_signal_emitted",
                         direction=agg_direction,
                         total_notional_usd=round(total_notional, 0),
                         events=len(recent_60s))
            else:
                log.debug("valuechain_cascade_cooldown",
                          remaining_ms=_CASCADE_COOLDOWN_MS - (int(now * 1000) - self._last_cascade_signal_ms))
            return  # Do not emit individual signals during a cascade

        # ── NON-CASCADE: emit per-event signals as before ─────────────────────
        for ev in events:
            sym = ev.symbol  # may be "" for market-wide

            # Calendar gate: BLOCK suppresses signal; CAUTION attenuates notional strength
            _notional = ev.notional_usd
            if self._calendar is not None and sym:
                try:
                    _cal = await self._calendar.get_state(sym)
                    if _cal.regime == "BLOCK":
                        log.info(
                            "liq_signal_blocked_calendar",
                            symbol=sym,
                            regime="BLOCK",
                            reason=_cal.reason,
                        )
                        continue  # Skip this liquidation event entirely
                    if _cal.regime == "CAUTION":
                        _notional *= _cal.size_multiplier  # reduce effective notional strength
                        log.debug(
                            "liq_signal_caution_attenuated",
                            symbol=sym,
                            cal_mult=round(_cal.size_multiplier, 2),
                            original_notional=ev.notional_usd,
                            attenuated_notional=round(_notional, 0),
                        )
                except Exception:
                    pass  # Calendar unavailable — emit at full strength

            # Long liquidation → bearish pressure; Short liquidation → bullish
            direction = "bearish" if ev.side == "long" else "bullish"
            sig = LiquidationSignal(
                symbol=sym,
                direction=direction,
                cascade=False,
                notional_usd=_notional,
                timestamp=ev.timestamp,
                event_count_60s=len(recent_60s),
            )
            for cb in self._listeners:
                try:
                    await cb(sig)
                except Exception as cb_err:
                    log.warning("valuechain_listener_error", error=str(cb_err))

    def _price_to_symbol(self, price: float) -> str:
        """Map price magnitude to trading symbol."""
        if 50000 <= price <= 150000: return "BTC-USD"
        if 1000  <= price <= 6000:   return "ETH-USD"
        if 50    <= price <= 300:    return "SOL-USD"
        if 1500  <= price <= 4000:   return "XAUT-USD"
        if 200   <= price <= 1000:   return "BNB-USD"
        if 5     <= price <= 35:     return "LINK-USD"
        if 5     <= price <= 100:    return "AVAX-USD"
        return ""

    def _parse_log_for_position(self, log_entry: dict):
        """
        Heuristically extract (symbol, side, size_usd) from an EVM log.
        Returns None if not a recognisable position event.
        """
        data = log_entry.get("data", "0x")
        if len(data) < 66:
            return None
        raw = data[2:]
        words = [raw[i:i+64] for i in range(0, len(raw), 64) if len(raw[i:i+64]) == 64]
        if len(words) < 2:
            return None
        price = size = 0.0
        for word in words:
            v = int(word, 16)
            if v > 2**255:
                v = v - 2**256
            v18 = abs(v) / 1e18
            v8  = abs(v) / 1e8
            if 1 <= v18 <= 200000 and price == 0.0:
                price = v18
            elif 0.0001 <= v8 <= 100000 and size == 0.0 and abs(v8 - price) > 0.01:
                size = v8
        if price < 1 or size < 0.0001:
            return None
        size_usd = size * price
        if size_usd < 1.0:
            return None
        symbol = self._price_to_symbol(price)
        if not symbol:
            return None
        side = "short" if int(words[0], 16) > 2**255 else "long"
        return symbol, side, size_usd

    def _ingest_position_logs(self, logs: list) -> None:
        """Feed raw EVM logs into position flow tracker."""
        from collections import deque
        now_ms = int(time.time() * 1000)
        for entry in logs:
            result = self._parse_log_for_position(entry)
            if not result:
                continue
            symbol, side, size_usd = result
            if symbol not in self._position_flow:
                self._position_flow[symbol] = deque(maxlen=100)
            self._position_flow[symbol].append({"side": side, "size_usd": size_usd, "ts_ms": now_ms})
        # Recompute flow signals
        for symbol, flow in self._position_flow.items():
            cutoff = now_ms - 300_000  # 5-min window
            recent = [p for p in flow if p["ts_ms"] > cutoff]
            if len(recent) < 3:
                continue
            long_vol  = sum(p["size_usd"] for p in recent if p["side"] == "long")
            short_vol = sum(p["size_usd"] for p in recent if p["side"] == "short")
            total = long_vol + short_vol
            if total < 200:
                continue
            net = (long_vol - short_vol) / total
            if abs(net) < 0.60:
                continue
            direction = "long" if net > 0 else "short"
            strength  = min(1.5, abs(net) * 1.5)
            # Whale amplifier: single position ≥ $5K in last 30s in same direction
            whale_same = any(
                p["size_usd"] >= 5000 and p["side"] == direction and now_ms - p["ts_ms"] < 30_000
                for p in recent
            )
            if whale_same:
                strength = min(1.5, strength + 0.4)
            self._flow_signals[symbol] = {"direction": direction, "score": strength, "ts_ms": now_ms}

    def get_onchain_score(self, symbol: str) -> float:
        """On-chain position flow score (0.0–1.5) for use as Tier 4/6 bonus."""
        sig = self._flow_signals.get(symbol)
        if not sig:
            return 0.0
        if int(time.time() * 1000) - sig["ts_ms"] > 300_000:
            return 0.0
        return float(sig.get("score", 0.0))

    def get_onchain_direction(self, symbol: str) -> str:
        """Returns 'long', 'short', or 'none'."""
        sig = self._flow_signals.get(symbol)
        if not sig:
            return "none"
        if int(time.time() * 1000) - sig["ts_ms"] > 300_000:
            return "none"
        return sig.get("direction", "none")

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
        """Make a JSON-RPC call. Raises on HTTP error, non-JSON body, or RPC error.

        Strict validation added because mainnet.valuechain.xyz returns HTTP 200
        with a non-JSON body (or missing 'result' field), causing the failure
        counter to increment continuously despite appearing healthy in HTTP logs.
        """
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1,
        }
        resp = await self._http.post(rpc, json=payload)
        if resp.status_code != 200:
            raise ConnectionError(f"RPC HTTP {resp.status_code}: {resp.text[:200]}")

        # Strict content-type check — HTML/plain-text 200 responses must not pass
        ct = resp.headers.get("content-type", "")
        if "json" not in ct.lower():
            raise ValueError(f"RPC non-JSON content-type ({ct!r}): {resp.text[:100]}")

        try:
            data = resp.json()
        except Exception as _je:
            raise ValueError(f"RPC JSON parse error: {_je} body={resp.text[:100]}")

        if "error" in data:
            raise ValueError(f"RPC error: {data['error']}")

        # For eth_blockNumber, result must be a valid hex string
        result = data.get("result")
        if method == "eth_blockNumber":
            if not isinstance(result, str) or not result.startswith("0x"):
                raise ValueError(f"RPC eth_blockNumber invalid result: {result!r}")
            # Confirm it's parseable as an integer
            int(result, 16)

        return result
