"""
Venue dispatch — symbol-partition routing between executors.

Every symbol trades on exactly one venue (assigned at boot). The brain never
branches on venue; order ops dispatch here:

    executor_for(symbol)  → the client that owns the symbol
    all_positions(addr)   → merged position book across venues
    combined_balance()    → total equity across venues

With bybit_enabled=False (or no keys), nothing is registered beyond the
SoDEX executor and every call resolves exactly as before — zero behavior
change on the live path.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import structlog

logger = structlog.get_logger(__name__)

_executors: Dict[str, Any] = {}
_venue_by_symbol: Dict[str, str] = {}
_DEFAULT_VENUE = "sodex"


def register_executor(venue: str, client: Any) -> None:
    _executors[venue] = client


def assign_symbols(symbols: List[str], venue: str) -> None:
    if venue not in _executors:
        logger.warning("venue_assign_without_executor", venue=venue, symbols=len(symbols))
        return
    for sym in symbols:
        _venue_by_symbol[sym] = venue
    if symbols:
        logger.info("venue_symbols_assigned", venue=venue, symbols=len(symbols))


def venue_for(symbol: str) -> str:
    return _venue_by_symbol.get(symbol, _DEFAULT_VENUE)


def executor_for(symbol: str) -> Any:
    return _executors[venue_for(symbol)]


def registered_venues() -> List[str]:
    return list(_executors.keys())


async def update_leverage(symbol: str, symbol_id: int, target_lev: int,
                          account_id: int) -> int:
    """Signature bridge: SoDEX keys on symbol_id, Bybit/Aster on symbol."""
    ex = executor_for(symbol)
    if venue_for(symbol) in ("bybit", "aster"):
        return await ex.update_leverage_with_fallback(
            symbol=symbol, leverage=target_lev)
    return await ex.update_leverage_with_fallback(
        symbol_id, target_lev, account_id)


async def all_positions(address: str = "") -> List[Dict]:
    """Merged live position book across all registered venues."""
    results = await asyncio.gather(
        *(ex.get_positions(address) for ex in _executors.values()),
        return_exceptions=True,
    )
    merged: List[Dict] = []
    for venue, res in zip(_executors.keys(), results):
        if isinstance(res, Exception):
            logger.warning("venue_positions_failed", venue=venue, error=str(res)[:120])
            continue
        merged.extend(res)
    return merged


async def combined_balance(address: str = "") -> float:
    """Total equity across venues — the number the vault watermark should track."""
    results = await asyncio.gather(
        *(ex.get_account_balance(address) for ex in _executors.values()),
        return_exceptions=True,
    )
    total = 0.0
    for venue, res in zip(_executors.keys(), results):
        if isinstance(res, Exception):
            logger.warning("venue_balance_failed", venue=venue, error=str(res)[:120])
            continue
        total += float(res or 0.0)
    return total
