"""
SoDEX REST API Client

Handles all authenticated and public calls to SoDEX perps.
Raises exceptions on HTTP errors. Never swallows errors silently.
"""

import json
import math
import time
import asyncio
from decimal import Decimal, ROUND_HALF_UP
import structlog
import httpx
import certifi
from typing import Dict, Any, List, Optional
from execution.schemas import OrderResult, BracketResult, BracketOrder
from execution.metrics import TradeMetrics, metrics_logger
from core.config import MIN_STOP_DISTANCE_PCT, DEFAULT_MIN_STOP_DISTANCE_PCT

logger = structlog.get_logger(__name__)


# Tick-size lookup for module-level helpers (no client instance access).
_TICK_BY_NAME: Dict[str, float] = {
    "BTC-USD":        1.0,
    "ETH-USD":        0.1,
    "SOL-USD":        0.01,
    "BNB-USD":        0.1,
    "LINK-USD":       0.001,
    "AVAX-USD":       0.001,
    "SUI-USD":        0.0001,
    "NEAR-USD":       0.0001,
    "ARB-USD":        0.00001,
    "OP-USD":         0.00001,
    "XRP-USD":        0.0001,
    "DOGE-USD":       1.0,
    "HBAR-USD":       1.0,
    "COIN-USD":       0.001,
    "1000PEPE-USD":   0.000001,
    "TRUMP-USD":      0.0001,
    "BASED-USD":      0.0001,
    "LTC-USD":        0.01,
    "XAUT-USD":       0.1,
    "CL-USD":         0.001,
    "COPPER-USD":     0.0001,
    "SILVER-USD":     0.001,
    "CRCL-USD":       0.001,
    "USTECH100-USD":  0.1,
    "TSM-USD":        0.01,
    "ORCL-USD":       0.01,
    "NVDA-USD":       0.01,
    "MSFT-USD":       0.01,
    "AAPL-USD":       0.01,
    "AMZN-USD":       0.01,
    "GOOGL-USD":      0.01,
    "META-USD":       0.01,
    "TSLA-USD":       0.01,
    "SPCX-USD":       0.1,
}


def _enforce_min_stop_distance(
    symbol: str,
    stop_price: float,
    reference_price: float,
    side: str,
    multiplier: float = 1.0,
) -> float:
    """Widen stop to meet SoDEX minimum distance requirement.

    SoDEX rejects stops placed too close to mark/entry (code -1 "stopPrice is invalid").
    This helper adjusts the stop outward so the order will be accepted.

    Parameters
    ----------
    multiplier : float
        Applied to both min_distance and safety buffer.  Use 1.0 for first
        attempt, 1.5 for retry after rejection.
    """
    if reference_price <= 0:
        return stop_price
    min_pct = MIN_STOP_DISTANCE_PCT.get(symbol, DEFAULT_MIN_STOP_DISTANCE_PCT)
    min_distance = reference_price * (min_pct / 100.0) * multiplier

    # Safety buffer: covers tick-size rounding (ROUND_HALF_UP can push the
    # stop toward the reference by up to tick/2) plus typical mark-price
    # drift during network RTT (0.05% of reference).  Prevents the
    # "failed by $0.0009" rounding edge case seen live on SOL-USD.
    tick = _TICK_STEP_BY_NAME.get(symbol, (_TICK_BY_NAME.get(symbol, 0.01), 0.001))[0]
    buffer = max(tick * 2.0, reference_price * 0.0005) * multiplier
    min_distance += buffer

    if side == "long":
        # stop must be below reference by at least min_distance
        if reference_price - stop_price < min_distance:
            adjusted = reference_price - min_distance
            logger.warning("stop_widened_min_distance", symbol=symbol,
                           original=round(stop_price, 4), adjusted=round(adjusted, 4),
                           reference=round(reference_price, 4), min_pct=min_pct,
                           buffer=round(buffer, 6), multiplier=multiplier)
            return adjusted
    else:  # short
        if stop_price - reference_price < min_distance:
            adjusted = reference_price + min_distance
            logger.warning("stop_widened_min_distance", symbol=symbol,
                           original=round(stop_price, 4), adjusted=round(adjusted, 4),
                           reference=round(reference_price, 4), min_pct=min_pct,
                           buffer=round(buffer, 6), multiplier=multiplier)
            return adjusted
    return stop_price

# Per-symbol tick_size and step_size (min order qty increment).
# Prices not aligned to tick_size → SoDEX code:-1 "unknown".
# Quantities not aligned to step_size → same rejection.
# (symbol_id → (tick_size, step_size))
_TICK_STEP: Dict[int, tuple] = {
    1:  (1,       0.00001), # BTC-USD      — tick=1, step=0.00001 (live API 2026-04-17)
    2:  (0.1,     0.0001),  # ETH-USD      — tick=0.1, step=0.0001 (live API 2026-04-17)
    6:  (0.01,    0.001),   # SOL-USD      — tick=0.01, step=0.001 (live API 2026-04-17)
    9:  (0.1,     0.001),   # BNB-USD      — tick=0.1, step=0.001 (live API 2026-04-17)
    5:  (0.001,   0.1),     # LINK-USD     — tick=0.001, step=0.1 (live API 2026-04-17)
    24: (0.001,   1),       # AVAX-USD     — tick=0.001, step=1 (live API 2026-04-17)
    11: (0.1,     0.0001),  # XAUT-USD     — tick=0.1, step=0.0001 (live API 2026-04-17)
    23: (0.0001,  0.1),     # SUI-USD      — tick=0.0001, step=0.1 (live API 2026-04-17)
}

# Symbol-name fallback for assets whose SoDEX integer IDs aren't known statically.
# Used when bracket.symbol_id is not in _TICK_STEP (e.g. ARB, OP, NEAR fetched at runtime).
# Format: (tick_size, step_size) — same semantics as _TICK_STEP.
_TICK_STEP_BY_NAME: Dict[str, tuple] = {
    # Dynamic-ID symbols — (tick_size, step_size) from live API 2026-04-17
    "ARB-USD":      (0.00001, 0.1),    # tick=0.00001, step=0.1
    "OP-USD":       (0.00001, 0.1),    # tick=0.00001, step=0.1
    "NEAR-USD":     (0.0001,  0.1),    # tick=0.0001,  step=0.1
    "1000PEPE-USD": (0.000001, 1.0),   # tick=0.000001, step=1
    "XRP-USD":      (0.0001,  0.1),    # tick=0.0001,  step=0.1
    "TRUMP-USD":    (0.0001,  0.01),   # tick=0.0001,  step=0.01
    "BASED-USD":    (0.0001,  1.0),    # tick=0.0001,  step=1
    "LTC-USD":      (0.01,    0.01),   # tick=0.01,    step=0.01
    # Commodity — live API 2026-04-17
    "CL-USD":       (0.001,   0.001),  # tick=0.001,   step=0.001
    "COPPER-USD":   (0.0001,  0.01),   # tick=0.0001,  step=0.01
    "SILVER-USD":   (0.001,   0.01),   # tick=0.001,   step=0.01
    # Equity — live API 2026-04-17 (tick=0.01, step=0.001 for all)
    "TSM-USD":      (0.01,    0.001),
    "ORCL-USD":     (0.01,    0.001),
    "NVDA-USD":     (0.01,    0.001),
    "MSFT-USD":     (0.01,    0.001),
    "AAPL-USD":     (0.01,    0.001),
    "AMZN-USD":     (0.01,    0.001),
    "GOOGL-USD":    (0.01,    0.001),
    "META-USD":     (0.01,    0.001),
    "TSLA-USD":     (0.01,    0.001),
    # Equity index
    "USTECH100-USD": (0.1,    0.0001),
    "SPCX-USD":      (0.1,    0.0001),
}

# Authoritative step-size override table for close/market orders.
# SoDEX returns position sizes at full float precision; these per-symbol
# steps define the quantity increment the exchange actually enforces.
# Overrides _TICK_STEP_BY_NAME for close orders to use round (not floor).
STEP_SIZES: Dict[str, float] = {
    # Crypto — live API 2026-04-17
    "BTC-USD":       0.00001,
    "ETH-USD":       0.0001,
    "SOL-USD":       0.001,
    "LINK-USD":      0.1,
    "AVAX-USD":      1.0,
    "OP-USD":        0.1,
    "ARB-USD":       0.1,
    "SUI-USD":       0.1,
    "NEAR-USD":      0.1,
    "BNB-USD":       0.001,
    "1000PEPE-USD":  1.0,
    "XAUT-USD":      0.0001,
    "XRP-USD":       0.1,
    "TRUMP-USD":     0.01,
    "BASED-USD":     1.0,
    "LTC-USD":       0.01,
    # Commodity — live API 2026-04-17
    "CL-USD":        0.001,
    "COPPER-USD":    0.01,
    "SILVER-USD":    0.01,
    # Equity — live API 2026-04-17
    "TSM-USD":       0.001,
    "ORCL-USD":      0.001,
    "NVDA-USD":      0.001,
    "MSFT-USD":      0.001,
    "AAPL-USD":      0.001,
    "AMZN-USD":      0.001,
    "GOOGL-USD":     0.001,
    "META-USD":      0.001,
    "TSLA-USD":      0.001,
    # Equity index
    "USTECH100-USD": 0.0001,
    "SPCX-USD":      0.0001,
}

# Minimum order quantity per symbol (close orders must meet this floor).
# Extend as needed when new symbols show minimum qty requirements different from step size.
MIN_QTY: Dict[str, float] = {}


def _get_tick_step(symbol: str, symbol_id: int) -> tuple:
    """Returns (tick_size, step_size) for a symbol.
    Priority: _TICK_STEP by ID → _TICK_STEP_BY_NAME by name → (0.01, 0.01).
    Handles symbols whose integer IDs are fetched dynamically (ARB, OP, NEAR…).
    """
    if symbol_id in _TICK_STEP:
        return _TICK_STEP[symbol_id]
    return _TICK_STEP_BY_NAME.get(symbol, (0.01, 0.01))


def _canonical_decimal_str(d: Decimal) -> str:
    """Format a Decimal as canonical decimal string per SoDEX spec.

    Rules: no leading zeros, no trailing zeros, no plus sign, no exponent.
    Examples: 32.100 → 32.1, 0.500 → 0.5, 217.00 → 217, 0.00001 → 0.00001.
    """
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _round_price(price: float, tick: float) -> str:
    """Round price to nearest tick, return canonical decimal string.

    Uses Decimal arithmetic to avoid float precision loss at midpoints
    (e.g. 100.005 / 0.01 = 10000.4999... in float — Decimal gives exact 10000.5).
    """
    d_price = Decimal(str(price))
    d_tick  = Decimal(str(tick))
    ticks   = (d_price / d_tick).to_integral_value(rounding=ROUND_HALF_UP)
    rounded = ticks * d_tick
    return _canonical_decimal_str(rounded)


def _round_qty(qty: float, step: float, reduce_only: bool = False) -> str:
    """Round quantity to nearest step, return canonical decimal string.

    Default (entry orders): floor — never over-buy.
    reduce_only=True (TP/stop/close): round() with 1-step minimum for dust.
      SoDEX caps the fill at actual position size for reduce_only, so sending
      1 step for dust < 0.5×step is always safe. This prevents "quantity is
      invalid" rejections when fractional TP splits floor to zero (e.g. SUI
      step=0.1 with size=0.1 → TP1 qty=0.05 floors to 0 without this guard).
    """
    if reduce_only:
        rounded = math.floor(qty / step) * step
        if rounded <= 0 and qty > 0:
            rounded = step  # dust guard — at least 1 step; reduceOnly caps fill
    else:
        rounded = math.floor(qty / step) * step
    d_step = Decimal(str(step))
    d_rounded = Decimal(str(rounded))
    units = int((d_rounded / d_step).to_integral_value(rounding=ROUND_HALF_UP))
    return _canonical_decimal_str(Decimal(units) * d_step)


class SoDEXAPIError(Exception):
    """Custom exception for SoDEX API errors"""
    def __init__(self, message: str, status_code: int = None):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


# ── SL Gap Formula ─────────────────────────────────────────────────────────────
# SoDEX stop-limit orders need a gap between trigger and limit price.
# Without it, the stop rests unfilled during fast gap moves.

_EQUITY_SYMBOLS: set[str] = {
    "TSM-USD", "ORCL-USD", "NVDA-USD", "MSFT-USD", "AAPL-USD",
    "AMZN-USD", "GOOGL-USD", "META-USD", "TSLA-USD",
}


def compute_sl_limit(trigger_price: float, side: str, symbol: str) -> float:
    """
    Compute stop-limit price with gap buffer below trigger.
    Gap: 1.5% for equities, 0.8% for crypto/commodities.
    """
    gap_pct = 0.015 if symbol in _EQUITY_SYMBOLS else 0.008
    if side == "long":
        return trigger_price * (1.0 - gap_pct)
    return trigger_price * (1.0 + gap_pct)


# ── OCO State Manager ──────────────────────────────────────────────────────────

class OCOStateManager:
    """Tracks OCO bracket state explicitly per order."""
    STATES = ["PENDING", "ACTIVE", "PARTIAL", "FILLED", "CANCELLED"]

    def __init__(self):
        self._states: Dict[str, str] = {}          # order_id -> state
        self._fill_qty: Dict[str, float] = {}      # order_id -> filled qty
        self._ordered_qty: Dict[str, float] = {}   # order_id -> ordered qty
        self._cancel_reason: Dict[str, str] = {}   # order_id -> cancel reason

    def register(self, order_id: str, ordered_qty: float):
        self._states[order_id] = "PENDING"
        self._fill_qty[order_id] = 0.0
        self._ordered_qty[order_id] = ordered_qty

    def on_fill(self, order_id: str, fill_qty: float):
        self._fill_qty[order_id] = fill_qty
        ordered = self._ordered_qty.get(order_id, 0.0)
        if fill_qty >= ordered * 0.99:
            self._states[order_id] = "FILLED"
        elif fill_qty > 0:
            self._states[order_id] = "PARTIAL"

    def on_cancel(self, order_id: str, reason: str = "user"):
        self._states[order_id] = "CANCELLED"
        self._cancel_reason[order_id] = reason

    def state(self, order_id: str) -> str:
        return self._states.get(order_id, "PENDING")

    def action_for_partial(self, order_id: str, cancel_reason: str = None) -> str:
        """Returns recommended action when parent is partially filled + cancelled."""
        fill_qty = self._fill_qty.get(order_id, 0.0)
        ordered_qty = self._ordered_qty.get(order_id, 0.0)
        _reason = cancel_reason or self._cancel_reason.get(order_id, "user")
        if fill_qty >= ordered_qty * 0.99:
            return "ACTIVATE_TPSL_FULL"
        if fill_qty > 0 and _reason == "margin":
            return "ACTIVATE_TPSL_PARTIAL"
        if fill_qty > 0 and _reason == "user":
            return "CANCEL_TPSL_RESUBMIT_MANUAL"
        return "WAIT"


class WeightBudget:
    """
    Soft-cap weight tracker. Hard limit = 400/min (conservative vs SoDEX 1200).
    Leaves headroom for burst orders and cancels.
    Refills at ~6.67/second (400/60).
    """
    def __init__(self, limit_per_minute=400, refill_per_second=6.67):
        self._used = 0.0
        self._last_refill = time.monotonic()
        self._refill_rate = refill_per_second
        self._limit = limit_per_minute

    def consume(self, weight: int) -> bool:
        """Returns True if budget available, consumes weight."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._used = max(0.0, self._used - elapsed * self._refill_rate)
        self._last_refill = now
        if self._used + weight > self._limit:
            return False  # rate limited
        self._used += weight
        return True

    def wait_time(self) -> float:
        """Seconds to wait before next request is safe."""
        return max(0.0, (self._used - self._limit * 0.8) / self._refill_rate)


class SoDEXClient:
    """
    REST API wrapper for SoDEX.
    Handles all authenticated and public calls.
    """

    def __init__(self, config, signer, nonce_manager):
        self.config = config
        self.signer = signer
        self.nonce_manager = nonce_manager
        # signing_key_address: EVM address derived from the signing private key.
        # Used only for resolving the registered API key name at startup.
        # The wallet address (config.sodex_account_id) is only used in GET URL paths.
        self.signing_key_address = signer.get_address()
        # api_key_name: the registered name for this signing key on SoDEX.
        # X-API-Key header must be this NAME (e.g. "ariaworks"), not the raw address.
        # Resolved at startup via resolve_api_key_name().
        self.api_key_name: str = ""

        self.client = httpx.AsyncClient(
            verify=certifi.where(),
            timeout=httpx.Timeout(connect=3.0, read=8.0, write=5.0, pool=2.0),
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
                keepalive_expiry=25,   # under 30s TCP idle timeout
            ),
            headers={"Accept": "application/json"},
            http2=True,               # H/2 multiplexing — reuse single TCP conn
        )

        self._keepalive_task: Optional[asyncio.Task] = None
        self._is_active = True
        self.base_url = config.sodex_rest_perps
        self._weight_budget = WeightBudget(limit_per_minute=400, refill_per_second=6.67)
        self.oco_manager = OCOStateManager()
        # symbol_id_map: id → SoDEX symbol string (populated by fetch_symbol_mapping).
        # symbol_info:   SoDEX symbol string → full market spec dict (step, tick, etc.).
        # Both start empty — populated lazily at startup if caller calls fetch_symbol_mapping.
        self.symbol_id_map: Dict[int, str] = {}
        self.symbol_info:   Dict[str, Any] = {}

    def _dynamic_step(self, symbol: str) -> float:
        """Return step size from symbol_info if available, else STEP_SIZES fallback."""
        info = self.symbol_info.get(symbol, {})
        for key in ("lotSize", "lot_size", "stepSize", "step_size", "step", "qtyStep"):
            val = info.get(key)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
        return STEP_SIZES.get(symbol, 0.01)

    def _dynamic_tick(self, symbol: str) -> float:
        """Return tick size from symbol_info if available, else _TICK_STEP fallback."""
        info = self.symbol_info.get(symbol, {})
        for key in ("tickSize", "tick_size", "tick", "priceTick"):
            val = info.get(key)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
        # Fall back to static tables
        _, tick = _get_tick_step(symbol, 0)
        return tick

    def get_tick_step(self, symbol: str, symbol_id: int = 0) -> tuple:
        """Dynamic (tick, step) using symbol_info when fetched, else static tables."""
        info = self.symbol_info.get(symbol, {})
        tick = None
        step = None
        for k in ("tickSize", "tick_size", "tick", "priceTick"):
            v = info.get(k)
            if v is not None:
                try:
                    tick = float(v)
                    break
                except (TypeError, ValueError):
                    pass
        for k in ("lotSize", "lot_size", "stepSize", "step_size", "step", "qtyStep"):
            v = info.get(k)
            if v is not None:
                try:
                    step = float(v)
                    break
                except (TypeError, ValueError):
                    pass
        if tick is not None and step is not None:
            return (tick, step)
        return _get_tick_step(symbol, symbol_id)

    def _round_qty(self, symbol: str, qty: float) -> str:
        """Step-align a close quantity using dynamic or static step, enforce min qty.

        Uses round() (not floor) so reduce-only market closes send the nearest valid
        step rather than always undershooting — SoDEX caps fill at actual position size.
        """
        step = self._dynamic_step(symbol)
        min_qty = MIN_QTY.get(symbol, 0.0)
        # Also try dynamic minQty from symbol_info
        info = self.symbol_info.get(symbol, {})
        for k in ("minQty", "min_qty", "minQuantity", "min_quantity"):
            v = info.get(k)
            if v is not None:
                try:
                    min_qty = float(v)
                    break
                except (TypeError, ValueError):
                    pass
        if step <= 0:
            step = 0.01
        if step <= 0:
            step = 0.01
        rounded = math.floor(qty / step) * step
        # Sub-step dust guard: rounding sent "0.000" → SoDEX -1 quantity is invalid.
        # Round up to one step; reduceOnly semantics cap fill at actual position size.
        if rounded <= 0 and qty > 0:
            rounded = step
        if min_qty > 0 and rounded < min_qty:
            rounded = min_qty
        d_step = Decimal(str(step))
        d_rounded = Decimal(str(rounded))
        units = int((d_rounded / d_step).to_integral_value(rounding=ROUND_HALF_UP))
        return _canonical_decimal_str(Decimal(units) * d_step)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # PUBLIC METHODS (no auth required)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def get_mark_price(self, symbol: str) -> float:
        """GET /markets/mark-prices"""
        response = await self.client.get(
            f"{self.base_url}/markets/mark-prices",
            params={"symbol": symbol}
        )
        if response.status_code != 200:
            raise SoDEXAPIError(f"Failed to get mark price: {response.text}", response.status_code)
        data = response.json()
        # Response: {"data": [{"symbol": ..., "markPrice": "..."}]}
        items = data.get("data", [])
        if items:
            return float(items[0].get("markPrice", 0))
        return 0.0

    async def get_orderbook(self, symbol: str, depth: int = 20) -> Dict[str, List]:
        """GET /markets/{symbol}/orderbook"""
        response = await self.client.get(
            f"{self.base_url}/markets/{symbol}/orderbook",
            params={"depth": depth}
        )
        if response.status_code != 200:
            raise SoDEXAPIError(f"Failed to get orderbook: {response.text}", response.status_code)
        data = response.json()
        ob = data.get("data", data)
        return {
            "bids": ob.get("bids", []),
            "asks": ob.get("asks", [])
        }

    async def get_positions(self, address: str) -> List[Dict]:
        """GET /accounts/{address}/positions
        API returns {"data": {"positions": [...], ...}} — extract inner positions list.
        Mirrors PHANTOM's get_open_positions() parsing logic.
        Filters non-dict items — API occasionally returns strings in the list.
        """
        response = await self.client.get(f"{self.base_url}/accounts/{address}/positions")
        if response.status_code != 200:
            raise SoDEXAPIError(f"Failed to get positions: {response.text}", response.status_code)
        data = response.json()
        pos_data = data.get("data", {})
        if isinstance(pos_data, dict):
            raw = pos_data.get("positions") or pos_data.get("P") or []
        else:
            raw = pos_data or []
        return [p for p in raw if isinstance(p, dict)]

    async def get_open_orders(self, address: str) -> List[Dict]:
        """GET /accounts/{address}/orders
        Filters non-dict items — API occasionally returns status strings in the list,
        which cause 'str' object has no attribute 'get' crashes in callers.
        """
        response = await self.client.get(f"{self.base_url}/accounts/{address}/orders")
        if response.status_code != 200:
            raise SoDEXAPIError(f"Failed to get open orders: {response.text}", response.status_code)
        data = response.json()
        raw = data.get("data", [])
        if isinstance(raw, list):
            return [o for o in raw if isinstance(o, dict)]
        return []

    async def get_account_balance(self, address: str) -> float:
        """
        Returns available cross-margin USD for the perps account.

        Mirrors PHANTOM's get_balance() pattern exactly (confirmed working):
          Primary  : GET /api/v1/perps/accounts/{addr}/state → data.av
          Fallback : GET /api/v1/perps/accounts/{addr}/balances → balances[].aw/av/wm/wb/available

        SoDEX uses short field names on perps endpoints ("av", "aw", "wm", "wb").
        We do NOT fall back to the spot account — spot is a separate account and may
        contain unrelated dust that would poison DrawdownManager sizing.
        """
        addr = address or self.config.sodex_account_id or self.config.account_id or ""
        base = "mainnet-gw.sodex.dev" if self.config.sodex_mainnet else "testnet-gw.sodex.dev"

        # Primary: /state endpoint — av = available cross-margin USD (same as PHANTOM).
        # Timeout 20s: mainnet-gw is occasionally slow (observed 15-25s response times).
        for _attempt in range(2):
            try:
                resp = await self.client.get(
                    f"https://{base}/api/v1/perps/accounts/{addr}/state",
                    timeout=20.0
                )
                d = resp.json()
                if d.get("code") == 0:
                    av = d.get("data", {}).get("av")
                    if av is not None:
                        v = float(av)
                        if v > 0:
                            logger.debug("balance_from_state", av=v)
                            return v
                break   # got a valid response, no retry needed
            except Exception:
                if _attempt == 0:
                    import asyncio as _aio
                    await _aio.sleep(2)   # brief pause before retry

        # Fallback: /balances endpoint — short field names used by SoDEX perps API
        for _attempt in range(2):
            try:
                resp = await self.client.get(
                    f"https://{base}/api/v1/perps/accounts/{addr}/balances",
                    timeout=20.0
                )
                d = resp.json()
                if d.get("code") == 0:
                    for entry in d.get("data", {}).get("balances", []):
                        if not isinstance(entry, dict):
                            continue
                        for k in ("aw", "av", "available", "wm", "wb"):
                            val = entry.get(k)
                            if val is not None:
                                try:
                                    v = float(val)
                                    if v > 0:
                                        logger.debug("balance_from_balances", field=k, value=v)
                                        return v
                                except (ValueError, TypeError):
                                    pass
                break
            except Exception as e:
                if _attempt == 0:
                    import asyncio as _aio
                    await _aio.sleep(2)
                else:
                    _emsg = str(e) or f"{type(e).__name__} (no message)"
                    logger.warning("balance_fetch_failed", error=_emsg, exc_type=type(e).__name__)

        logger.warning("balance_zero_or_unfunded", addr=addr[:12])
        return 0.0

    async def get_margin_asset_balances(self, address: str) -> dict:
        """
        Fetch non-USDC asset balances from the Margin & Futures account.

        Returns dict: {"BTC": qty, "ETH": qty, "XAUT": qty, "SOSO": qty}
        with only non-zero assets present. Used by the MultiAssetMarginEngine
        to compute MAM-adjusted effective balance.

        SoDEX MAM supports: BTC (90%), XAUT (90%), ETH (90%), SOSO (50%).
        Assets in the Spot account do NOT count — must be transferred to Margin.

        Falls back to {} (pure USDC mode) on any error — non-fatal.
        """
        addr = address or self.config.sodex_account_id or self.config.account_id or ""
        base = "mainnet-gw.sodex.dev" if self.config.sodex_mainnet else "testnet-gw.sodex.dev"

        # Known non-USDC asset identifiers on SoDEX perps account endpoints
        # Maps various possible field names → MAM key
        _ASSET_MAP: dict = {
            "BTC":  "BTC",   "bitcoin": "BTC",
            "ETH":  "ETH",   "ethereum": "ETH",
            "XAUT": "XAUT",  "xaut": "XAUT", "gold": "XAUT",
            "SOSO": "SOSO",  "soso": "SOSO",
        }

        result: dict = {}
        try:
            resp = await self.client.get(
                f"https://{base}/api/v1/perps/accounts/{addr}/balances",
                timeout=15.0,
            )
            d = resp.json()
            if d.get("code") == 0:
                for entry in d.get("data", {}).get("balances", []):
                    if not isinstance(entry, dict):
                        continue
                    # Try to identify the asset from various field formats
                    asset_raw = (
                        entry.get("asset") or entry.get("coin") or
                        entry.get("symbol") or entry.get("currency") or ""
                    ).upper().strip()
                    mam_key = _ASSET_MAP.get(asset_raw)
                    if mam_key is None or mam_key == "USDC":
                        continue  # USDC handled separately; unknown assets ignored
                    # Look for quantity in various field names
                    for qty_field in ("wb", "wm", "balance", "available", "qty", "size"):
                        qty_raw = entry.get(qty_field)
                        if qty_raw is not None:
                            try:
                                qty = float(qty_raw)
                                if qty > 0:
                                    result[mam_key] = result.get(mam_key, 0.0) + qty
                            except (ValueError, TypeError):
                                pass
                            break
        except Exception as e:
            logger.debug("margin_asset_balance_fetch_error", error=str(e))

        if result:
            logger.debug("margin_asset_balances_fetched", assets=result)
        return result

    async def fetch_perp_fee_rate(self, address: str = "", symbol: str = "") -> dict:
        """
        GET /api/v1/perps/accounts/{address}/fee-rate
        Returns live perps maker/taker rates from SoDEX (weight=2).
        Optionally pass a symbol for symbol-level fee discount.

        Returns dict: makerFeeRate, takerFeeRate, tier, stakingTier (floats/ints).
        """
        addr = address or self.config.sodex_account_id or self.config.account_id or ""
        base = "mainnet-gw.sodex.dev" if self.config.sodex_mainnet else "testnet-gw.sodex.dev"
        params = {"symbol": symbol} if symbol else {}
        try:
            resp = await self.client.get(
                f"https://{base}/api/v1/perps/accounts/{addr}/fee-rate",
                params=params or None,
                timeout=8.0,
            )
            data = resp.json()
            if data.get("code") == 0:
                fee_data = data.get("data", {})
                return {
                    "makerFeeRate": float(fee_data.get("makerFeeRate", 0) or 0),
                    "takerFeeRate": float(fee_data.get("takerFeeRate", 0) or 0),
                    "tier":         int(fee_data.get("tier", fee_data.get("feeTier", 0)) or 0),
                    "stakingTier":  int(fee_data.get("stakingTier", 0) or 0),
                }
        except Exception as e:
            _emsg = str(e) or f"{type(e).__name__} (no message)"
            logger.warning("perp_fee_rate_fetch_failed", error=_emsg)
        return {"makerFeeRate": 0.0, "takerFeeRate": 0.0, "tier": 0, "stakingTier": 0}

    async def resolve_api_key_name(self) -> str:
        """
        Fetch the registered API key name for this signing key.
        X-API-Key header must be the name registered on SoDEX (e.g. "ariaworks"),
        NOT the raw signing key address.
        Queries GET /accounts/{wallet}/api-keys, matches publicKey to signing_key_address.
        """
        wallet = self.config.sodex_account_id or ""
        if not wallet:
            raise SoDEXAPIError("sodex_account_id not configured — cannot resolve API key name")

        resp = await self.client.get(f"{self.base_url}/accounts/{wallet}/api-keys")
        if resp.status_code != 200:
            raise SoDEXAPIError(f"Failed to fetch API keys: {resp.text}", resp.status_code)

        data = resp.json()
        if data.get("code") != 0:
            raise SoDEXAPIError(f"API key list error: code={data.get('code')} msg={data.get('msg')}")

        signing_lower = self.signing_key_address.lower()
        for key in data.get("data", []):
            if key.get("publicKey", "").lower() == signing_lower:
                self.api_key_name = key["name"]
                logger.info("api_key_name_resolved",
                            name=self.api_key_name,
                            signing_address=self.signing_key_address,
                            expires_at=key.get("expiresAt", 0))
                return self.api_key_name

        raise SoDEXAPIError(
            f"Signing key {self.signing_key_address} not found in API keys for {wallet}. "
            f"Register it on the SoDEX dashboard first."
        )

    async def fetch_symbol_mapping(self) -> None:
        """
        Populate symbol_id_map and symbol_info from GET /perps/markets/symbols at startup.
        Provides dynamic ID → symbol resolution so hardcoded _TICK_STEP IDs never
        drift out of sync when SoDEX adds or re-numbers markets.
        Safe to call multiple times — overwrites on refresh.
        """
        try:
            resp = await self.client.get(f"{self.base_url}/markets/symbols")
            if resp.status_code != 200:
                logger.warning("fetch_symbol_mapping_failed",
                               status=resp.status_code, body=resp.text[:200])
                return
            data = resp.json()
            markets = data if isinstance(data, list) else data.get("data", [])
            new_id_map: Dict[int, str] = {}
            new_info:   Dict[str, Any] = {}
            for m in markets:
                mid = m.get("id") or m.get("symbolID")
                sym = m.get("symbol") or m.get("name", "")
                if mid is not None and sym:
                    new_id_map[int(mid)] = sym
                    new_info[sym] = m
            self.symbol_id_map = new_id_map
            self.symbol_info   = new_info
            logger.info("symbol_mapping_loaded", count=len(new_id_map))
        except Exception as exc:
            logger.warning("fetch_symbol_mapping_error", error=str(exc))

    async def fetch_account_id(self, address: str) -> int:
        """
        Resolves numeric accountID (aid) for the given wallet address.
        Strategy:
          1. Try GET /api/v1/spot/accounts/{address}/state  (canonical per SoDEX docs)
          2. Try GET /api/v1/perps/accounts/{address}/state (fallback)
        Returns 0 if account is not yet registered on SoDEX (no deposit made).
        """
        base = "mainnet-gw.sodex.dev" if self.config.sodex_mainnet else "testnet-gw.sodex.dev"
        endpoints = [
            f"https://{base}/api/v1/spot/accounts/{address}/state",
            f"https://{base}/api/v1/perps/accounts/{address}/state",
        ]
        for url in endpoints:
            try:
                resp = await self.client.get(url, timeout=8.0)
                data = resp.json()
                if data.get("code") != 0:
                    continue
                d = data.get("data", {})
                for field in ("aid", "uid", "accountID", "id"):
                    val = d.get(field)
                    if val is not None:
                        try:
                            numeric = int(val)
                            if numeric != 0:
                                logger.info("account_id_resolved", aid=numeric, endpoint=url)
                                return numeric
                        except (ValueError, TypeError):
                            pass
            except Exception as e:
                logger.debug("account_id_fetch_attempt_failed", url=url, error=str(e))
                continue

        logger.warning(
            "account_not_registered",
            address=address,
            message="aid=0 on both endpoints. Account registers automatically on first SoDEX deposit."
        )
        return 0

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # AUTHENTICATED METHODS (EIP-712 signed)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def place_order(self, order_data: Dict[str, Any]) -> OrderResult:
        """POST /trade/orders — body = params, sign full {type,params} wrapper"""
        try:
            data = await self._signed_post("/trade/orders", "newOrder", order_data)
            # SoDEX newOrder response: {"code":0,"data":[{...}]} — data is a LIST.
            # Older/alternate format: {"code":0,"data":{"orders":[...]}} — data is a dict.
            raw = data.get("data", {})
            if isinstance(raw, list):
                orders = raw
            elif isinstance(raw, dict):
                orders = raw.get("orders", [])
            else:
                orders = []
            if orders:
                o = orders[0]
                # Inner code may be -1 even when outer code=0 (per-order validation)
                inner_code = o.get("code", 0)
                if inner_code != 0:
                    inner_err = (
                        o.get("error") or o.get("msg") or
                        o.get("message") or f"inner_code={inner_code}"
                    )
                    logger.error("sodex_inner_order_rejected",
                                 inner_code=inner_code, error=inner_err,
                                 clOrdID=o.get("clOrdID"))
                    return OrderResult(order_id="", status="rejected",
                                       fill_price=None, fill_qty=None, error=inner_err)
                return OrderResult(
                    order_id=str(o.get("orderID", o.get("clOrdID", ""))),
                    status=str(o.get("status", "open")),
                    fill_price=float(o.get("fillPrice", 0) or 0) or None,
                    fill_qty=float(o.get("fillQty", 0) or 0) or None,
                    error=None,
                )
            return OrderResult(order_id="", status="unknown", fill_price=None, fill_qty=None, error="No orders in response")
        except SoDEXAPIError as e:
            return OrderResult(order_id="", status="rejected", fill_price=None, fill_qty=None, error=e.message)

    async def cancel_order(self, order_id: str, symbol: str, account_id: int,
                           symbol_id: int = 0) -> bool:
        """DELETE /trade/orders — cancels array format with uint64 orderID."""
        params = {
            "accountID": account_id,
            "cancels": [{"orderID": int(order_id), "symbolID": symbol_id}],
        }
        try:
            result = await self._signed_delete("/trade/orders", "cancelOrder", params)
            return result.get("code", -1) == 0
        except SoDEXAPIError:
            return False

    async def place_bracket(self, bracket: BracketOrder) -> BracketResult:
        """
        Places entry → waits for fill confirmation → stop → TP1/2/3.

        Sequence:
          1. Entry LIMIT placed.
          2. Poll positions up to 60s for fill confirmation.
             - Not filled: cancel entry, return failure (price moved away).
             - Filled: proceed to reduce-only stop/TP.
          3. Stop placed (reduce-only, exchange-side limit order).
             If stop fails after fill: returns partial success — deferred retry
             in caller will re-attempt. Emergency fallback is market exit.
          4. TPs placed (reduce-only, 50%/30%/20% split).

        Metrics: fire-and-forget TradeMetrics emitted at completion.
        Does NOT block execution path — caller runs this as asyncio.create_task().
        """
        placed_orders = []

        # ── Metrics object — timestamps set at each milestone ────────────────
        _c = bracket.candidate
        _sym_clean = _c.symbol.replace("-", "").replace("_", "")
        _m = TradeMetrics(
            trade_id=f"e{_sym_clean}{int(_c.timestamp_ms)}",
            symbol=_c.symbol,
            side=_c.side,
            expected_price=_c.entry_price,
            t_signal=_c.timestamp_ms / 1000.0 if _c.timestamp_ms > 0 else time.time(),
        )

        try:
            address = self.config.sodex_account_id or self.config.account_id or ""

            # ── 0. Snapshot pre-entry position size ──────────────────────────────
            _t_pre = time.perf_counter()
            pre_size = 0.0
            try:
                existing = await self.get_positions(address)
                for pos in existing:
                    sym = pos.get("symbol", "") or pos.get("coin", "")
                    if sym == bracket.candidate.symbol:
                        pre_size = abs(float(pos.get("size", 0) or pos.get("qty", 0) or 0))
                        break
            except Exception:
                pass
            _t_pre_done = time.perf_counter()
            _pre_ms = round((_t_pre_done - _t_pre) * 1000, 1)

            # ── 1. Entry ─────────────────────────────────────────────────────────
            _m.t_entry_sent = time.time()
            entry_result = await self._place_entry_order(bracket)
            _t_entry_done = time.perf_counter()
            _entry_ms = round((_t_entry_done - _t_pre_done) * 1000, 1)
            if not entry_result.success:
                metrics_logger.emit(_m)  # fire-and-forget — never blocks
                return BracketResult(success=False, error=f"Entry failed: {entry_result.error}")
            placed_orders.append((entry_result.order_id, bracket.candidate.symbol, bracket.account_id))
            self.oco_manager.register(entry_result.order_id, bracket.candidate.size)

            # ── 2. Wait for position fill ────────────────────────────────────────
            filled, actual_size = await self._confirm_position_open(
                symbol=bracket.candidate.symbol,
                account_address=address,
                min_size=bracket.candidate.size * 0.5,  # accept 50% partial fill
                pre_size=pre_size,
                timeout_s=60.0,
            )
            if not filled:
                logger.warning("entry_not_filled_cancelling",
                               symbol=bracket.candidate.symbol,
                               order_id=entry_result.order_id)
                self.oco_manager.on_cancel(entry_result.order_id, "entry_not_filled")
                await self._cleanup_orders(placed_orders)
                metrics_logger.emit(_m)
                return BracketResult(
                    success=False,
                    error="entry_not_filled_within_60s: cancelled"
                )

            _m.t_fill = time.time()
            _t_fill_done = time.perf_counter()
            _fill_ms = round((_t_fill_done - _t_entry_done) * 1000, 1)
            logger.info("fill_latency_breakdown",
                        symbol=bracket.candidate.symbol,
                        pre_size_ms=_pre_ms,
                        entry_post_ms=_entry_ms,
                        fill_wait_ms=_fill_ms,
                        total_to_fill_ms=round((_t_fill_done - _t_pre) * 1000, 1))

            # For limit orders the actual fill price ≈ entry_price (no slippage).
            # If the order result carries a fill price, use it.
            if entry_result.fill_price and entry_result.fill_price > 0:
                _m.actual_fill_price = entry_result.fill_price
            else:
                _m.actual_fill_price = bracket.candidate.entry_price

            # ── Fill size sync — exchange is source of truth ─────────────────────
            # Always sync candidate.size to the ACTUAL exchange position before TP
            # placement.  Three cases:
            #   overfill  (>5%): duplicate order / race — warn, sync to exchange size
            #   underfill (≥1%): significant partial — cancel unfilled remainder
            #   rounding  (<1%): SoDEX step rounding — log and sync silently
            _req_size = bracket.candidate.size
            if actual_size > 0 and actual_size != _req_size:
                if actual_size > _req_size * 1.05:
                    # Exchange filled MORE than requested — possible duplicate order
                    logger.warning(
                        "overfill_detected",
                        symbol=bracket.candidate.symbol,
                        requested=round(_req_size, 6),
                        filled=round(actual_size, 6),
                        overfill_pct=round((actual_size / _req_size - 1) * 100, 2),
                        note="exchange_is_source_of_truth_syncing_to_actual",
                    )
                elif actual_size < _req_size * 0.99:
                    # Significant partial fill — cancel remaining open qty.
                    # Distinguish manual-cancel (ARIA initiates) from margin-cancel
                    # (SoDEX system-cancels due to insufficient margin).  The latter
                    # is rare because ARIA sizes conservatively, but when it happens
                    # the child protective orders (if native OCO were used) would
                    # auto-activate.  ARIA places protective orders manually, so the
                    # distinction is observational only — but logged for RCA.
                    _cancel_reason = "user"
                    try:
                        _open_orders = await self.get_open_orders(address)
                        _still_open = any(
                            str(o.get("orderID", "")) == entry_result.order_id
                            or str(o.get("clOrdID", "")) == entry_result.order_id
                            for o in _open_orders
                        )
                        if not _still_open:
                            _cancel_reason = "margin"
                    except Exception:
                        pass
                    logger.warning(
                        "partial_fill_cancel_remainder",
                        symbol=bracket.candidate.symbol,
                        requested=round(_req_size, 6),
                        filled=round(actual_size, 6),
                        cancelled_remainder=round(_req_size - actual_size, 6),
                        cancel_reason=_cancel_reason,
                        note="margin" if _cancel_reason == "margin" else "manual_cancel",
                    )
                    self.oco_manager.on_cancel(entry_result.order_id, _cancel_reason)
                    await self._cleanup_orders([
                        (entry_result.order_id, bracket.candidate.symbol, bracket.account_id)
                    ])
                    placed_orders.clear()
                else:
                    # Exchange rounding (<1% off) — sync without cancelling
                    logger.info(
                        "fill_size_adjusted_rounding",
                        symbol=bracket.candidate.symbol,
                        requested=round(_req_size, 6),
                        actual=round(actual_size, 6),
                        delta_pct=round((1 - actual_size / _req_size) * 100, 4),
                        note="TP sizes will use actual exchange fill",
                    )
                bracket.candidate.size = actual_size  # exchange is always source of truth

            self.oco_manager.on_fill(entry_result.order_id, actual_size)

            # ── Zero-fill guard ──────────────────────────────────────────────────
            # If position size is zero after fill confirmation, protective orders
            # cannot be placed.  Cancel entry and return before burning API weight.
            if actual_size <= 0:
                logger.warning("zero_fill_after_confirmation",
                               symbol=bracket.candidate.symbol,
                               order_id=entry_result.order_id)
                self.oco_manager.on_cancel(entry_result.order_id, "zero_fill")
                metrics_logger.emit(_m)
                return BracketResult(
                    success=False,
                    entry_order_id=entry_result.order_id,
                    error="zero_fill_or_no_fill_price",
                )

            # ── Sub-minimum-close guard ──────────────────────────────────────────
            # If the filled size is below STEP_SIZES minimum, we cannot place a
            # TP or close this position via the exchange API — _round_qty would
            # produce "0.000" (before the round-up fix) or rely on reduceOnly cap.
            # Better to close immediately and not track the position at all than
            # to create a zombie that fires the stop guardian on every 0.5s tick.
            _min_close = STEP_SIZES.get(bracket.candidate.symbol, 0.01)
            if actual_size < _min_close:
                _close_qty_str = self._round_qty(bracket.candidate.symbol, _min_close)
                logger.warning(
                    "fill_below_min_closeable",
                    symbol=bracket.candidate.symbol,
                    actual_size=round(actual_size, 8),
                    min_closeable=_min_close,
                    close_qty=_close_qty_str,
                    note="immediate dust-close — position below minimum API step",
                )
                try:
                    await self.close_position_market(
                        symbol=bracket.candidate.symbol,
                        symbol_id=bracket.symbol_id,
                        account_id=bracket.account_id,
                        side=bracket.candidate.side,
                        size=_min_close,   # step qty; reduceOnly caps fill at actual
                    )
                except Exception as _dc_err:
                    logger.warning("dust_close_failed", symbol=bracket.candidate.symbol,
                                   error=str(_dc_err))
                self.oco_manager.on_cancel(entry_result.order_id, "dust_close")
                metrics_logger.emit(_m)
                return BracketResult(
                    success=False,
                    entry_order_id=entry_result.order_id,
                    error=f"fill_below_min_closeable: {actual_size} < {_min_close}",
                )

            # ── 3. Native stop-loss (exchange-side, MARK_PRICE trigger) ──────────
            # Native stop-limit: type=1 (LIMIT) with price gap, not type=2 (MARKET).
            # SoDEX rejects stop-market orders with "stopPrice is invalid".
            # stopType=1 (STOP_LOSS), triggerType=2 (MARK_PRICE), tif=1 (GTC).
            # reduceOnly=True guarantees it only closes existing position.
            _m.t_stop_sent = time.time()
            stop_result = await self._place_native_stop_order(bracket)
            _m.t_stop_confirmed = time.time()
            if stop_result.success:
                _m.stop_placed = True
                placed_orders.append((stop_result.order_id, bracket.candidate.symbol, bracket.account_id))
                logger.info("native_stop_placed",
                            symbol=bracket.candidate.symbol,
                            order_id=stop_result.order_id,
                            stop_price=round(bracket.candidate.stop_price, 6))
            else:
                # Stop failed — position is live but unprotected. Log CRITICAL
                # and rely on software _stop_guardian_loop as emergency backup.
                logger.error("native_stop_failed",
                             symbol=bracket.candidate.symbol,
                             error=stop_result.error,
                             note="software_stop_guardian_active_as_fallback")
                _m.stop_placed = False

            # ── 4. TPs (native take-profit orders) ───────────────────────────────
            _m.t_tp_sent = time.time()
            tp_results = await self._place_tp_orders(bracket)
            failed_tps = [r for r in tp_results if not r.success]
            if failed_tps:
                await self._cleanup_orders([
                    (r.order_id, bracket.candidate.symbol, bracket.account_id)
                    for r in tp_results if r.order_id and r.success
                ])
                logger.error("tp_failed",
                             symbol=bracket.candidate.symbol, error=failed_tps[0].error)
                _m.tp_placed = False
                metrics_logger.emit(_m)
                return BracketResult(
                    success=True,
                    entry_order_id=entry_result.order_id,
                    error=f"tp_failed: {failed_tps[0].error}",
                )

            _m.tp_placed = True
            metrics_logger.emit(_m)  # full success — emit all timing data

            tp_order_ids = [r.order_id for r in tp_results]
            return BracketResult(
                success=True,
                entry_order_id=entry_result.order_id,
                stop_order_id=stop_result.order_id if stop_result.success else None,
                tp1_order_id=tp_order_ids[0],
                tp2_order_id=tp_order_ids[1],
                tp3_order_id=tp_order_ids[2],
            )

        except Exception as e:
            metrics_logger.emit(_m)
            await self._cleanup_orders(placed_orders)
            return BracketResult(success=False, error=f"Bracket placement failed: {str(e)}")

    async def place_protective_orders(self, bracket: BracketOrder) -> BracketResult:
        """
        Place native stop + TPs for an already-open position (no entry, no fill wait).
        Used when place_bracket returned partial success (entry filled but protective orders failed).
        """
        try:
            stop_result = await self._place_native_stop_order(bracket)
            tp_results = await self._place_tp_orders(bracket)
            failed_tps = [r for r in tp_results if not r.success]
            if failed_tps:
                await self._cleanup_orders([
                    (r.order_id, bracket.candidate.symbol, bracket.account_id)
                    for r in tp_results if r.order_id and r.success
                ])
                return BracketResult(success=False, error=f"TP retry failed: {failed_tps[0].error}")

            tp_ids = [r.order_id for r in tp_results]
            logger.info("protective_orders_placed",
                        symbol=bracket.candidate.symbol,
                        stop_order_id=stop_result.order_id if stop_result.success else None,
                        tp_ids=tp_ids)
            return BracketResult(
                success=True,
                stop_order_id=stop_result.order_id if stop_result.success else None,
                tp1_order_id=tp_ids[0],
                tp2_order_id=tp_ids[1],
                tp3_order_id=tp_ids[2],
            )
        except Exception as e:
            return BracketResult(success=False, error=f"Protective orders failed: {str(e)}")

    async def update_leverage(self, symbol_id: int, leverage: int, account_id: int) -> bool:
        """
        POST /trade/leverage

        Tries marginMode=2 (cross) first. Some symbols (e.g. SOL-USD) reject
        mode 2; for those we fall back to marginMode=1 (isolated). If both
        modes are rejected, returns False — caller logs the warning.
        """
        for margin_mode in (2, 1):
            params = {
                "accountID": account_id,
                "symbolID": symbol_id,
                "leverage": leverage,
                "marginMode": margin_mode,
            }
            try:
                result = await self._signed_post("/trade/leverage", "updateLeverage", params)
                if result.get("code", -1) == 0:
                    return True
                # code:-1 with marginMode=2 → try the other mode
                if margin_mode == 2:
                    continue
            except SoDEXAPIError:
                if margin_mode == 2:
                    continue
        return False

    async def update_leverage_with_fallback(
        self, symbol_id: int, target_leverage: int, account_id: int,
        fallback_chain: tuple = (10, 7, 5, 3, 2),
    ) -> int:
        """
        Tries target leverage, then walks fallback_chain until one succeeds.
        Returns the leverage that was actually set.  Returns 1 if all fail.

        Phase 7: Dynamic leverage for scalp entries. SoDEX caps vary by symbol;
        this guarantees we never fail an entry due to leverage mismatch.
        """
        _chain = [target_leverage] + [l for l in fallback_chain if l != target_leverage]
        for lev in _chain:
            ok = await self.update_leverage(symbol_id, lev, account_id)
            if ok:
                return lev
        return 1

    async def _confirm_position_open(
        self, symbol: str, account_address: str,
        min_size: float, pre_size: float = 0.0,
        timeout_s: float = 30.0
    ) -> tuple:
        """
        Poll until the symbol's position size exceeds pre_size + min_size.

        pre_size: size snapshot taken BEFORE the entry order was placed.
        This prevents false positives from pre-existing positions.

        Returns (filled: bool, actual_size: float).
        actual_size is the confirmed position size when filled (0.0 on timeout).

        CRITICAL FIX: SoDEX uses NEGATIVE size for short positions.
        abs() converts -0.002 BTC short → 0.002 for comparison.
        Without this, ALL short fills are invisible and stops are never placed.
        """
        target = pre_size + min_size
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        # Fast path: poll every 500ms for first 2s (was 250ms/3s — saves 50% weight).
        # After 2s fall back to 1.5s cadence (was 750ms — saves 50% weight).
        _fast_end = loop.time() + 2.0
        _poll_interval_fast = 0.5
        _poll_interval_slow = 1.5
        while loop.time() < deadline:
            _wait = self._weight_budget.wait_time()
            if _wait > 0:
                await asyncio.sleep(_wait)
            if not self._weight_budget.consume(5):  # get_positions = weight 5
                await asyncio.sleep(0.5)
                continue
            try:
                positions = await self.get_positions(account_address)
                for pos in positions:
                    sym = pos.get("symbol", "") or pos.get("coin", "")
                    size = abs(float(pos.get("size", 0) or pos.get("qty", 0) or 0))
                    if sym == symbol and size >= target:
                        logger.info("position_fill_confirmed",
                                    symbol=symbol,
                                    pre_size=round(pre_size, 6),
                                    current_size=round(size, 6),
                                    min_fill_threshold=round(target, 6))
                        return True, size
            except Exception as e:
                logger.debug("confirm_position_poll_error", error=str(e))
            _interval = _poll_interval_fast if loop.time() < _fast_end else _poll_interval_slow
            await asyncio.sleep(_interval)
        logger.warning("position_fill_timeout",
                       symbol=symbol, min_size=min_size,
                       pre_size=pre_size, target=target, timeout_s=timeout_s)
        return False, 0.0

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # INTERNAL HELPER METHODS
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _build_order_item(
        self,
        cl_ord_id: str,
        side: int,
        order_type: int,
        tif: int,
        quantity: str,
        price: str = None,
        reduce_only: bool = False,
        modifier: int = 1,
        stop_price: str = None,
        stop_type: int = None,
        trigger_type: int = None,
    ) -> Dict[str, Any]:
        """
        Build order item dict with EXACT canonical field order required for signing.

        SoDEX signing rule: payloadHash = keccak256(json.Marshal(payload)).
        The Go server unmarshals the JSON, adds zero-defaults for missing fields,
        then re-marshals before hashing.  Any field omitted from our payload will
        be added by the server with its zero value before the hash is computed,
        making our client-side hash differ → code:-1 rejection.

        Fields must be in the exact order defined in PerpsOrderItem:
          clOrdID, modifier, side, type, timeInForce, price, quantity,
          funds, stopPrice, stopType, triggerType, reduceOnly, positionSide

        stopPrice/stopType/triggerType are OMITTED when None — sending 0/"0"
        causes "stopType is invalid" rejection (confirmed live 2026-04-12).
        """
        item: Dict[str, Any] = {
            "clOrdID":      cl_ord_id,
            "modifier":     modifier,
            "side":         side,
            "type":         order_type,
            "timeInForce":  tif,
        }
        # price: omit for MARKET orders (SoDEX rejects price=0 for market)
        # DecimalString guard: coerce to str regardless of caller type.
        # Prevents float leakage from param_store overrides or manual paths.
        if price is not None:
            item["price"] = str(price)
        item["quantity"] = str(quantity)
        # funds: omit (not used)
        # stop fields: only include when explicitly set (omitempty on server)
        if stop_price is not None:
            item["stopPrice"] = str(stop_price)
        if stop_type is not None:
            item["stopType"] = stop_type
        if trigger_type is not None:
            item["triggerType"] = trigger_type
        item["reduceOnly"]   = reduce_only
        item["positionSide"] = 1         # BOTH — SoDEX only supports oneway mode
        return item

    async def _signed_post(
        self, endpoint: str, action_type: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Sign full {type,params} wrapper; send params only as body."""
        _t0 = time.perf_counter()

        nonce = self.nonce_manager.next_nonce()
        full_payload = {"type": action_type, "params": params}
        signature = self.signer.sign_payload(full_payload, nonce)
        _t1 = time.perf_counter()

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-Key": self.api_key_name,
            "X-API-Sign": signature,
            "X-API-Nonce": str(nonce),
        }

        response = await self.client.post(
            f"{self.base_url}{endpoint}", json=params, headers=headers
        )
        _t2 = time.perf_counter()

        logger.debug("http_breakdown",
                     action=action_type,
                     sign_ms=round((_t1 - _t0) * 1000, 1),
                     http_ms=round((_t2 - _t1) * 1000, 1),
                     total_ms=round((_t2 - _t0) * 1000, 1))
        if response.status_code not in (200, 201):
            raise SoDEXAPIError(f"API request failed: {response.text}", response.status_code)

        data = response.json()
        if isinstance(data, dict) and data.get("code", 0) != 0:
            # Log the exact payload bytes so we can diff against SoDEX expected hash
            _payload_bytes = json.dumps(
                {"type": action_type, "params": params}, separators=(",", ":")
            ).encode("utf-8")
            _err_msg = (
                data.get("error") or data.get("msg") or
                data.get("message") or "unknown"
            )
            logger.error(
                "sodex_order_rejected",
                code=data.get("code"),
                msg=_err_msg,
                api_key_name=self.api_key_name,
                action=action_type,
                account_id=params.get("accountID"),
                symbol_id=params.get("symbolID"),
                payload_json=_payload_bytes.decode("utf-8"),
                nonce=nonce,
            )
            raise SoDEXAPIError(
                f"SoDEX error {data.get('code')}: {_err_msg}",
                response.status_code,
            )
        return data

    async def _signed_delete(
        self, endpoint: str, action_type: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Sign full {type,params} wrapper; send params only as body with DELETE."""
        nonce = self.nonce_manager.next_nonce()
        full_payload = {"type": action_type, "params": params}
        signature = self.signer.sign_payload(full_payload, nonce)

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-Key": self.api_key_name,
            "X-API-Sign": signature,
            "X-API-Nonce": str(nonce),
        }

        response = await self.client.request(
            "DELETE", f"{self.base_url}{endpoint}", json=params, headers=headers
        )
        if response.status_code not in (200, 204):
            raise SoDEXAPIError(f"API delete failed: {response.text}", response.status_code)

        return response.json() if response.content else {}

    async def _place_entry_order(self, bracket: BracketOrder) -> OrderResult:
        """
        Place entry order.  Order type is driven by candidate.order_type (set by Kant/Nietzsche):
          "market" — IOC market fill: type=2, tif=3, no price field.  Fills in <1s.
          "probe"  — Aggressive limit at 0.1% inside mark: type=1, tif=3 (IOC), half size.
                     Fills fast when price moves toward it; cancels if not.
          "limit"  — GTC limit at entry_price (default, existing behaviour).
        """
        c = bracket.candidate
        _sym_clean = c.symbol.replace("-", "").replace("_", "")
        cl_ord_id = f"e{_sym_clean}{int(c.timestamp_ms)}"
        side = 1 if c.side == "long" else 2
        tick, step = self.get_tick_step(bracket.candidate.symbol, bracket.symbol_id)
        _order_type_str = getattr(c, "order_type", "limit")

        # ── Probe: half-size, aggressive limit, IOC ─────────────────────────────
        if _order_type_str == "probe":
            # 0.1% inside ask (long) / bid (short) — fills fast, cancels if not
            _probe_price = (c.entry_price * 0.999 if c.side == "long"
                            else c.entry_price * 1.001)
            probe_qty_str = _round_qty(c.size * 0.50, step)  # half size
            if float(probe_qty_str) <= 0:
                probe_qty_str = _round_qty(c.size, step)     # fallback: full size
            qty_str   = probe_qty_str
            qty_float = float(qty_str)
            price_str = _round_price(_probe_price, tick)
            order_type_int = 1   # limit
            tif_int        = 3   # IOC — cancel unfilled portion immediately
            logger.info("entry_probe_order", symbol=c.symbol,
                        price=price_str, qty=qty_str, half_size=True)
        else:
            qty_str   = _round_qty(c.size, step)
            qty_float = float(qty_str)
            price_str = _round_price(c.entry_price, tick) if _order_type_str != "market" else None
            order_type_int = 2 if _order_type_str == "market" else 1  # 2=MARKET, 1=LIMIT
            tif_int        = 3 if _order_type_str == "market" else 1  # IOC for market, GTC for limit

        qty_float = float(qty_str)
        # Pre-flight: zero quantity or dust notional → reject before hitting exchange.
        # SoDEX rejects qty=0 with code:-1 "unknown"; catch it here to avoid burning
        # a per-symbol cooldown on a sizing bug.
        if qty_float <= 0:
            logger.error("entry_order_zero_qty",
                         symbol=c.symbol, size=c.size, step=step, qty_str=qty_str)
            return OrderResult(order_id="", status="rejected",
                               fill_price=None, fill_qty=None,
                               error="SoDEX error -1: zero_quantity_after_step_rounding")
        if price_str is not None:
            notional = qty_float * float(price_str)
            if notional < 10.0:
                # Micro-mode: _round_qty floored us just under the notional floor.
                # Bump up by one step so SoDEX accepts the order.
                _bumped = qty_float + step
                _bumped_str = f"{_bumped:.{max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0}f}"
                _bumped_notional = float(_bumped_str) * float(price_str)
                if _bumped_notional >= 10.0:
                    logger.warning("entry_order_notional_bumped",
                                   symbol=c.symbol, qty_before=qty_str, qty_after=_bumped_str,
                                   notional_before=round(notional, 2), notional_after=round(_bumped_notional, 2))
                    qty_str = _bumped_str
                    qty_float = float(qty_str)
                else:
                    logger.error("entry_order_dust_notional",
                                 symbol=c.symbol, qty=qty_str, price=price_str,
                                 notional=round(notional, 2), min_notional=10.0)
                    return OrderResult(order_id="", status="rejected",
                                       fill_price=None, fill_qty=None,
                                       error=f"SoDEX error -1: notional_{notional:.2f}_below_10usd_minimum")

        order_item = self._build_order_item(
            cl_ord_id=cl_ord_id,
            side=side,
            order_type=order_type_int,
            tif=tif_int,
            quantity=qty_str,
            price=price_str,        # None for market orders — omitted from payload
            reduce_only=False,
        )
        params = {
            "accountID": int(bracket.account_id),
            "symbolID": bracket.symbol_id,
            "orders": [order_item],
        }
        return await self.place_order(params)

    async def _place_native_stop_order(
        self,
        bracket: BracketOrder,
        stop_price: float = None,
        size: float = None,
    ) -> OrderResult:
        """Place native stop-limit order on SoDEX (live, exchange-side trigger).

        SoDEX requires conditional orders to be LIMIT (type=1) with both
        stopPrice (trigger) and price (limit).  MARKET conditional orders
        are rejected with "stopPrice is invalid".

        LIVE FINDING (2026-06-06): modifier=4 (ATTACHED_STOP) on standalone
        orders returns "modifier is invalid".  SoDEX appears to require
        conditional legs to be placed ATOMICAALLY with a BRACKET entry
        (modifier=3) — standalone conditional orders are NOT supported.
        This function is kept as a fallback attempt; software stop guardian
        remains the primary protection mechanism.

        Uses MARK_PRICE trigger (triggerType=2) so wicks don't prematurely fill.
        Limit price is computed with a 0.8% gap (crypto) / 1.5% gap (equities)
        below/above the trigger so the stop fills immediately when triggered.
        reduceOnly=True guarantees it only closes existing position.

        Parameters
        ----------
        stop_price : float, optional
            Override stop price (for trailing stop updates). Defaults to candidate.stop_price.
        size : float, optional
            Override size (for partial closes). Defaults to candidate.size.
        """
        c = bracket.candidate
        _sym_clean = c.symbol.replace("-", "").replace("_", "")
        cl_ord_id = f"sl{_sym_clean}{int(c.timestamp_ms)}"
        side = 2 if c.side == "long" else 1  # opposite: long→sell(2), short→buy(1)
        tick, step = self.get_tick_step(c.symbol, bracket.symbol_id)

        _stop = stop_price if stop_price is not None else c.stop_price
        _size = size if size is not None else c.size

        # Verify stop is on the correct side before sending to exchange.
        _stop_valid = (
            (c.side == "long" and _stop < c.entry_price) or
            (c.side == "short" and _stop > c.entry_price)
        )
        if not _stop_valid:
            logger.error(
                "native_stop_sign_invalid",
                symbol=c.symbol, side=c.side,
                entry=round(c.entry_price, 6), stop=round(_stop, 6),
                note="stop on wrong side of entry — refusing to place",
            )
            return OrderResult(order_id="", status="rejected",
                               error=f"stop_sign_invalid:{c.side} stop={_stop:.4f} entry={c.entry_price:.4f}")

        # Enforce SoDEX minimum stop distance (prevents "stopPrice is invalid" rejections)
        # SoDEX validates against mark price, not entry — use mark as reference.
        _mark_ref = await self.get_mark_price(c.symbol)
        _ref_price = _mark_ref if _mark_ref > 0 else c.entry_price
        _stop = _enforce_min_stop_distance(c.symbol, _stop, _ref_price, c.side)

        _limit_price = compute_sl_limit(_stop, c.side, c.symbol)
        order_item = self._build_order_item(
            cl_ord_id=cl_ord_id,
            side=side,
            order_type=1,           # LIMIT — required by SoDEX for conditional orders
            tif=1,                  # GTC — stop stays active until triggered
            quantity=_round_qty(_size, step, reduce_only=True),
            price=_round_price(_limit_price, tick),
            reduce_only=True,
            stop_price=_round_price(_stop, tick),
            stop_type=1,            # STOP_LOSS
            trigger_type=2,         # MARK_PRICE — no wick fills
        )
        params = {
            "accountID": int(bracket.account_id),
            "symbolID": bracket.symbol_id,
            "orders": [order_item],
        }
        logger.info("native_stop_order_placing",
                    symbol=c.symbol, stop_price=round(_stop, 6),
                    limit_price=round(_limit_price, 6),
                    size=_size, side="sell" if c.side == "long" else "buy")
        result = await self.place_order(params)
        # Retry once on "stopPrice is invalid" — mark may have drifted during RTT.
        if not result.success and result.error and "stopPrice is invalid" in result.error:
            logger.warning("native_stop_rejected_retrying",
                           symbol=c.symbol, error=result.error,
                           note="fetching_fresh_mark_and_widening_1.5x")
            _mark_ref2 = await self.get_mark_price(c.symbol)
            _ref_price2 = _mark_ref2 if _mark_ref2 > 0 else _ref_price
            _stop_retry = _enforce_min_stop_distance(c.symbol, _stop, _ref_price2, c.side, multiplier=1.5)
            _limit_retry = compute_sl_limit(_stop_retry, c.side, c.symbol)
            order_item = self._build_order_item(
                cl_ord_id=f"{cl_ord_id}r",
                side=side,
                order_type=1,
                tif=1,
                quantity=_round_qty(_size, step, reduce_only=True),
                price=_round_price(_limit_retry, tick),
                reduce_only=True,
                stop_price=_round_price(_stop_retry, tick),
                stop_type=1,
                trigger_type=2,
            )
            params["orders"] = [order_item]
            result = await self.place_order(params)
            if not result.success:
                logger.error("native_stop_retry_failed",
                             symbol=c.symbol, error=result.error)
        return result

    # Legacy alias — kept for backward compatibility of callers
    _place_stop_order = _place_native_stop_order

    async def _place_tp_orders(self, bracket: BracketOrder) -> List[OrderResult]:
        """
        Place TP1/TP2/TP3 as native TAKE_PROFIT orders using tier-aware partials
        from tp_engine (default 50/30/20 when not set).

        Each TP is a stop-limit order: when mark price hits stopPrice,
        it activates as a LIMIT order with a small gap buffer so it fills
        immediately.  SoDEX rejects stop-market (type=2) conditional orders.
        triggerType=2 (MARK_PRICE) prevents wick-based premature fills.

        Serial placement: TP1 first (highest priority), then TP2/TP3 in parallel.
        """
        c = bracket.candidate

        # Guard: all TP prices zero → position was reconciled without TP data.
        if c.tp1_price <= 0 and c.tp2_price <= 0 and c.tp3_price <= 0:
            logger.warning("tp_prices_zero_skip", symbol=c.symbol,
                           entry=c.entry_price, side=c.side)
            return [
                OrderResult(order_id="", status="rejected",
                            fill_price=None, fill_qty=None,
                            error="tp_prices_zero")
                for _ in range(3)
            ]

        side = 2 if c.side == "long" else 1  # opposite of position side
        _sym_clean = c.symbol.replace("-", "").replace("_", "")
        tick, step = self.get_tick_step(bracket.candidate.symbol, bracket.symbol_id)

        # Tier-aware partials from tp_engine (fallback to 50/30/20).
        _p1 = getattr(c, 'partial1_pct', 0.5)
        _p2 = getattr(c, 'partial2_pct', 0.3)
        _p3 = max(0.0, 1.0 - _p1 - _p2)

        # Compute TP quantities so their sum never exceeds position size.
        tp1_qty = float(_round_qty(c.size * _p1, step, reduce_only=True))
        tp2_qty = float(_round_qty(c.size * _p2, step, reduce_only=True))
        tp3_raw = c.size - tp1_qty - tp2_qty
        tp3_qty = float(_round_qty(tp3_raw, step, reduce_only=True))
        if tp3_qty <= 0 and tp3_raw > 0:
            tp3_qty = step
        tp_qtys = [tp1_qty, tp2_qty, tp3_qty]
        if sum(tp_qtys) > c.size + 1e-12:
            tp_qtys[2] = max(0.0, c.size - tp_qtys[0] - tp_qtys[1])
            tp_qtys[2] = math.floor(tp_qtys[2] / step) * step
        for i in range(3):
            if tp_qtys[i] > 0 and tp_qtys[i] < step:
                tp_qtys[i] = step
        if sum(tp_qtys) > c.size + 1e-12:
            excess = sum(tp_qtys) - c.size
            for i in reversed(range(3)):
                if tp_qtys[i] >= excess + step:
                    tp_qtys[i] = math.floor((tp_qtys[i] - excess) / step) * step
                    break

        # Enforce effective minimum quantity per symbol. If a split piece is
        # below the exchange minimum, merge it upward into the next level.
        min_qty = MIN_QTY.get(c.symbol, 0.0)
        info = self.symbol_info.get(c.symbol, {})
        for k in ("minQty", "min_qty", "minQuantity", "min_quantity"):
            v = info.get(k)
            if v is not None:
                try:
                    min_qty = float(v)
                    break
                except (TypeError, ValueError):
                    pass
        if min_qty <= 0:
            min_qty = step
        if c.size * _p1 < min_qty:
            # Position too small to split — single TP at 100% on tp1 price
            tp_qtys = [float(_round_qty(c.size, step, reduce_only=True)), 0.0, 0.0]
        else:
            # Merge dust upward
            if 0 < tp_qtys[2] < min_qty:
                tp_qtys[1] += tp_qtys[2]
                tp_qtys[2] = 0.0
            if 0 < tp_qtys[1] < min_qty:
                tp_qtys[0] += tp_qtys[1]
                tp_qtys[1] = 0.0
            # Re-round after merge
            for i in range(3):
                if tp_qtys[i] > 0:
                    tp_qtys[i] = float(_round_qty(tp_qtys[i], step, reduce_only=True))

        # Enforce minimum distance on TP prices (SoDEX validates TPs the same way as stops).
        # Invert side: for a long position TP must be ABOVE entry, so use "short" to push UP.
        _tp_side = "short" if c.side == "long" else "long"
        tp_prices = [
            _enforce_min_stop_distance(c.symbol, c.tp1_price, c.entry_price, _tp_side) if c.tp1_price > 0 else 0.0,
            _enforce_min_stop_distance(c.symbol, c.tp2_price, c.entry_price, _tp_side) if c.tp2_price > 0 else 0.0,
            _enforce_min_stop_distance(c.symbol, c.tp3_price, c.entry_price, _tp_side) if c.tp3_price > 0 else 0.0,
        ]

        # ── Notional guard: merge TP splits below min notional upward ────────
        # SoDEX rejects "notional is invalid" when any TP leg is below $10.
        # Merge dust upward (TP3→TP2→TP1); if TP1 itself is dust, collapse to single TP.
        _min_notional = float(getattr(self.config, 'min_trade_notional_usd', 10.0))
        for i in reversed(range(3)):
            if tp_qtys[i] > 0 and tp_prices[i] > 0:
                _notional = tp_qtys[i] * tp_prices[i]
                if _notional < _min_notional:
                    if i > 0:
                        tp_qtys[i - 1] += tp_qtys[i]
                        tp_qtys[i] = 0.0
                        logger.warning("tp_dust_merged_notional",
                                       symbol=c.symbol, tp=i + 1,
                                       notional=round(_notional, 2),
                                       min_required=_min_notional)
                    else:
                        # TP1 itself is dust — can't merge up; single TP at 100%
                        tp_qtys = [float(_round_qty(c.size, step, reduce_only=True)), 0.0, 0.0]
                        logger.warning("tp1_dust_single_tp",
                                       symbol=c.symbol, notional=round(_notional, 2),
                                       min_required=_min_notional)
                        break
        for i in range(3):
            if tp_qtys[i] > 0:
                tp_qtys[i] = float(_round_qty(tp_qtys[i], step, reduce_only=True))

        async def _place_one(idx: int) -> OrderResult:
            cl_ord_id = f"tp{idx+1}{_sym_clean}{int(c.timestamp_ms)}"
            qty_str = _round_qty(tp_qtys[idx], step, reduce_only=True)
            stop_price_str = _round_price(tp_prices[idx], tick)
            _limit_price = compute_sl_limit(tp_prices[idx], c.side, c.symbol)
            params = {
                "accountID": int(bracket.account_id),
                "symbolID": bracket.symbol_id,
                "orders": [self._build_order_item(
                    cl_ord_id=cl_ord_id,
                    side=side,
                    order_type=1,   # LIMIT — required by SoDEX for conditional orders
                    tif=1,          # GTC — TP stays active until triggered
                    quantity=qty_str,
                    price=_round_price(_limit_price, tick),
                    reduce_only=True,
                    stop_price=stop_price_str,
                    stop_type=2,    # TAKE_PROFIT
                    trigger_type=2, # MARK_PRICE
                )],
            }
            result = await self.place_order(params)
            # Retry once on "stopPrice is invalid" — same validation as stop-loss.
            if not result.success and result.error and "stopPrice is invalid" in result.error:
                _tp_ref2 = await self.get_mark_price(c.symbol)
                _tp_ref2 = _tp_ref2 if _tp_ref2 > 0 else c.entry_price
                _retry_price = _enforce_min_stop_distance(
                    c.symbol, tp_prices[idx], _tp_ref2, _tp_side, multiplier=1.5
                )
                _limit_retry = compute_sl_limit(_retry_price, c.side, c.symbol)
                params["orders"] = [self._build_order_item(
                    cl_ord_id=f"{cl_ord_id}r",
                    side=side,
                    order_type=1,
                    tif=1,
                    quantity=qty_str,
                    price=_round_price(_limit_retry, tick),
                    reduce_only=True,
                    stop_price=_round_price(_retry_price, tick),
                    stop_type=2,
                    trigger_type=2,
                )]
                result = await self.place_order(params)
                if not result.success:
                    logger.error("native_tp_retry_failed",
                                 symbol=c.symbol, idx=idx+1, error=result.error)
            return result

        # TP1 is highest-conviction (50% size) — place first, wait for result.
        r1 = await _place_one(0)
        # TP2 + TP3 can race in parallel; reduceOnly caps fill safely.
        r2, r3 = await asyncio.gather(_place_one(1), _place_one(2))
        return [r1, r2, r3]

    async def replace_stop_order(
        self,
        symbol: str,
        symbol_id: int,
        account_id: int,
        new_stop_price: float,
        old_stop_order_id: Optional[str],
        side: str,
        size: float,
        entry_price: Optional[float] = None,
        mark_price: Optional[float] = None,
    ) -> "OrderResult":
        """
        Update trailing stop on exchange: place new native stop-limit first,
        then cancel the old one.

        Design: place-before-cancel ensures the position is ALWAYS protected
        during the transition. If the cancel fails it's acceptable — the old
        stop is now redundant (exchange will reject the smaller one as reduce-only
        overflow, and trigger on the more-favourable new one first).

        Native stop-limit: MARK_PRICE trigger (triggerType=2), STOP_LOSS (stopType=1),
        LIMIT (type=1) with gap buffer via compute_sl_limit().
        """
        tick, step = self.get_tick_step(symbol, symbol_id)
        stop_side = 2 if side == "long" else 1   # opposite direction
        _sym_clean = symbol.replace("-", "").replace("_", "")
        cl_ord_id = f"ts{_sym_clean}{int(time.time() * 1000)}"

        # Enforce minimum stop distance to prevent SoDEX rejections.
        # Fetch mark price if not provided — SoDEX validates against mark, not entry.
        _ref = mark_price
        if _ref is None or _ref <= 0:
            try:
                _ref = await self.get_mark_price(symbol)
            except Exception:
                _ref = entry_price if entry_price is not None else 0.0
        _adjusted_stop = _enforce_min_stop_distance(symbol, new_stop_price, _ref, side) if _ref > 0 else new_stop_price
        _limit_price = compute_sl_limit(_adjusted_stop, side, symbol)

        order_item = self._build_order_item(
            cl_ord_id=cl_ord_id,
            side=stop_side,
            order_type=1,   # LIMIT — required by SoDEX for conditional orders
            tif=1,          # GTC — stop stays active until triggered
            quantity=_round_qty(size, step, reduce_only=True),
            price=_round_price(_limit_price, tick),
            reduce_only=True,
            stop_price=_round_price(_adjusted_stop, tick),
            stop_type=1,    # STOP_LOSS
            trigger_type=2, # MARK_PRICE
        )
        logger.info("native_trailing_stop_replacing",
                    symbol=symbol, new_stop=round(new_stop_price, 6),
                    limit_price=round(_limit_price, 6),
                    old_order_id=old_stop_order_id)
        params = {
            "accountID": account_id,
            "symbolID": symbol_id,
            "orders": [order_item],
        }
        result = await self.place_order(params)
        if result.success and old_stop_order_id:
            # Cancel old stop AFTER new one is confirmed placed
            try:
                await self.cancel_order(old_stop_order_id, symbol, account_id)
            except Exception:
                pass  # old stop becomes redundant — harmless
        return result

    async def close_position_market(
        self,
        symbol: str,
        symbol_id: int,
        account_id: int,
        side: str,
        size: float,
    ) -> "OrderResult":
        """
        Market-close a position immediately (time stop / emergency).
        Uses MARKET order type with IOC TIF so it fills or cancels instantly.
        Always reduce-only — cannot accidentally open a new position.

        Quantity strategy for reduce-only close:
          Use STEP_SIZES-based rounding (round, not floor) so the quantity is aligned
          to the exchange step increment.  With reduceOnly=True, SoDEX caps the fill
          at the actual position size — a slight overshoot is harmless.
          e.g. AAPL 0.40172155 → step=0.01 → "0.40"  ✓
               TSLA 0.17710484 → step=0.01 → "0.18"  ✓
               ETH  0.03672912 → step=0.001 → "0.037" ✓
               OP   797.3      → step=10.0  → "800"   ✓
        """
        close_side = 2 if side == "long" else 1   # opposite of position side
        _sym_clean = symbol.replace("-", "").replace("_", "")
        cl_ord_id = f"tc{_sym_clean}{int(time.time() * 1000)}"  # tc = time-close

        # Step-aligned quantity — always valid for SoDEX.
        quantity_str = self._round_qty(symbol, size)

        order_item = self._build_order_item(
            cl_ord_id=cl_ord_id,
            side=close_side,
            order_type=2,   # MARKET
            tif=3,          # IOC — fill immediately or cancel
            quantity=quantity_str,
            price=None,     # no price for market orders
            reduce_only=True,
        )
        params = {
            "accountID": account_id,
            "symbolID": symbol_id,
            "orders": [order_item],
        }
        result = await self.place_order(params)
        logger.info("close_position_market_sent",
                    symbol=symbol, side=side, size=size,
                    quantity_str=quantity_str,
                    success=result.success, order_id=result.order_id)
        return result

    async def place_order_simple(
        self,
        symbol: str,
        side: str,          # "buy"/"long" or "sell"/"short"
        contracts: float,
        price: float,
        symbol_id: int,
        account_id: int,
    ) -> OrderResult:
        """
        Simplified single-leg order for arb strategies.

        Places a LIMIT order (price > 0) or MARKET/IOC order (price == 0).
        Not reduce-only — used for opening arb legs.

        Args:
            symbol:     e.g. "BTC-USD"
            side:       "buy" / "long" → side=1; "sell" / "short" → side=2
            contracts:  order quantity in base asset
            price:      limit price; pass 0.0 for MARKET
            symbol_id:  SoDEX numeric symbol ID
            account_id: SoDEX numeric account ID (aid)
        """
        tick, step = self.get_tick_step(symbol, symbol_id)
        _sym_clean = symbol.replace("-", "").replace("_", "")
        cl_ord_id = f"arb{_sym_clean}{int(time.time() * 1000)}"[:36]

        side_int = 1 if side.lower() in ("buy", "long") else 2
        use_market = (price <= 0)
        order_item = self._build_order_item(
            cl_ord_id=cl_ord_id,
            side=side_int,
            order_type=2 if use_market else 1,   # MARKET=2, LIMIT=1
            tif=3 if use_market else 1,           # IOC for market, GTC for limit
            quantity=_round_qty(contracts, step),
            price=None if use_market else _round_price(price, tick),
            reduce_only=False,
        )
        params = {
            "accountID": account_id,
            "symbolID": symbol_id,
            "orders": [order_item],
        }
        result = await self.place_order(params)
        logger.info("place_order_simple_sent",
                    symbol=symbol, side=side, contracts=contracts,
                    price=price if not use_market else "MARKET",
                    success=result.success, order_id=result.order_id)
        return result

    async def place_maker_first(
        self,
        symbol: str,
        side: str,          # "buy"/"long" or "sell"/"short"
        contracts: float,
        mark_price: float,
        symbol_id: int,
        account_id: int,
        wait_seconds: int = 30,
        maker_offset: float = 0.0001,   # 0.01% offset from mark to post as limit
    ) -> tuple:
        """
        Post-only maker attempt: try a limit at best-bid/ask first.
        If unfilled after wait_seconds, cancel and fall back to market.

        Cost savings: maker vs taker on Tier 0 = 0.040% - 0.012% = 0.028% per leg.
        On a $1,000 arb position that's $0.28 saved per entry — ~60% fee reduction.

        Returns:
            (OrderResult, was_maker: bool)
            was_maker=True if the limit order filled before the deadline.
            was_maker=False if we fell back to market.

        Args:
            mark_price:   current mark price — limit placed at slight offset
            maker_offset: fraction to offset from mark (default 0.01%)
                          buy:  price = mark × (1 - maker_offset)  → post below mark
                          sell: price = mark × (1 + maker_offset)  → post above mark
        """
        tick, step = self.get_tick_step(symbol, symbol_id)
        side_int = 1 if side.lower() in ("buy", "long") else 2
        _sym_clean = symbol.replace("-", "").replace("_", "")
        cl_ord_id = f"mk{_sym_clean}{int(time.time() * 1000)}"[:36]

        # Maker limit: buy below mark, sell above mark → we're first in queue
        if side_int == 1:
            limit_price = mark_price * (1.0 - maker_offset)
        else:
            limit_price = mark_price * (1.0 + maker_offset)

        order_item = self._build_order_item(
            cl_ord_id=cl_ord_id,
            side=side_int,
            order_type=1,       # LIMIT
            tif=1,              # GTC
            quantity=_round_qty(contracts, step),
            price=_round_price(limit_price, tick),
            reduce_only=False,
        )
        params = {
            "accountID": account_id,
            "symbolID": symbol_id,
            "orders": [order_item],
        }

        # Step 1: Place the limit order
        limit_result = await self.place_order(params)
        if not limit_result.success:
            # Limit was immediately rejected — fall back to market
            logger.warning("maker_limit_rejected", symbol=symbol, side=side,
                           error=limit_result.error, action="falling_back_to_market")
            market_result = await self.place_order_simple(
                symbol=symbol, side=side, contracts=contracts,
                price=0.0, symbol_id=symbol_id, account_id=account_id,
            )
            return market_result, False

        logger.info("maker_limit_placed", symbol=symbol, side=side,
                    price=_round_price(limit_price, tick), cl_ord_id=cl_ord_id,
                    wait_seconds=wait_seconds)

        # Step 2: Poll for fill confirmation
        deadline = time.time() + wait_seconds
        poll_interval = 3.0
        while time.time() < deadline:
            await asyncio.sleep(poll_interval)
            try:
                address = self.config.sodex_account_id or self.config.account_id or ""
                positions = await self.get_positions(address)
                for pos in positions:
                    sym = pos.get("symbol", "") or pos.get("coin", "")
                    # SoDEX uses NEGATIVE size for short positions — abs() required.
                    size = abs(float(pos.get("size", 0) or pos.get("qty", 0) or 0))
                    if sym == symbol and size > 0:
                        logger.info("maker_limit_filled", symbol=symbol, side=side,
                                    size=size, cl_ord_id=cl_ord_id)
                        return OrderResult(
                            order_id=cl_ord_id,
                            status="filled",
                            fill_price=limit_price,
                            fill_qty=contracts,
                            error=None,
                        ), True
            except Exception as _e:
                logger.debug("maker_fill_poll_error", error=str(_e))

        # Step 3: Deadline reached — cancel limit, fall back to market
        logger.info("maker_limit_expired", symbol=symbol, cl_ord_id=cl_ord_id,
                    action="cancelling_and_falling_back_to_market")
        try:
            await self.cancel_order(cl_ord_id, symbol, account_id)
        except Exception:
            pass

        market_result = await self.place_order_simple(
            symbol=symbol, side=side, contracts=contracts,
            price=0.0, symbol_id=symbol_id, account_id=account_id,
        )
        return market_result, False

    async def _cleanup_orders(self, order_tuples: List[tuple]):
        """Cancel multiple orders — (order_id, symbol, account_id)."""
        for order_id, symbol, account_id in order_tuples:
            try:
                await self.cancel_order(order_id, symbol, int(account_id))
            except Exception:
                pass

    async def close(self):
        """Shutdown persistent HTTP client."""
        self._is_active = False
        if self._keepalive_task:
            self._keepalive_task.cancel()
        await self.client.aclose()
        logger.info("persistent_http_client_closed")

    def start_keepalive(self):
        """Starts background keepalive ping loop."""
        if self._keepalive_task is None:
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
            logger.info("http_keepalive_loop_started")

    async def _keepalive_loop(self):
        """Ping the server every 20s to keep connection warm."""
        while self._is_active:
            try:
                await self.client.get(
                    f"{self.base_url}/markets/mark-prices",
                    params={"symbol": "BTC-USD"},
                    timeout=5.0,
                )
                logger.debug("http_keepalive_ping_sent")
            except Exception as e:
                logger.warning("http_keepalive_ping_failed", error=str(e))
            await asyncio.sleep(20)
