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
import os
import time
from typing import Any, Dict, List

import structlog

logger = structlog.get_logger(__name__)

_executors: Dict[str, Any] = {}
_venue_by_symbol: Dict[str, str] = {}
_DEFAULT_VENUE = "sodex"

# Throttle venue failure warnings: a dead venue key otherwise spams one
# warning pair per tick (~2.5k lines/2h), burying real signal. One line per
# (event, venue) per 5 min — same rate-limiter idiom as the interpreter.
_last_fail_log: Dict[tuple, float] = {}
_FAIL_LOG_INTERVAL_S = 300.0


def _log_venue_failure(event: str, venue: str, res: Exception) -> None:
    key = (event, venue)
    now = time.monotonic()
    if now - _last_fail_log.get(key, 0.0) >= _FAIL_LOG_INTERVAL_S:
        _last_fail_log[key] = now
        logger.warning(event, venue=venue, error=str(res)[:120])


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


def symbols_for(venue: str) -> List[str]:
    return [s for s, v in _venue_by_symbol.items() if v == venue]


def aster_recovery_exempt_enabled() -> bool:
    return os.getenv("ASTER_RECOVERY_EXEMPT_ENABLED", "true").lower() == "true"


def aster_recovery_exempt(venue_name: str, reason: str, enabled: bool) -> bool:
    """Venue-aware recovery exemption (operator directive 2026-08-26).

    A drawdown measured on the COMBINED book must not throttle a sleeve that
    isn't bleeding: the Aster sleeve self-governs through its own 30% session
    halt (aster_sleeve_halt_dd_pct), so DD-reason recovery (0.5x size cap,
    5.6 coherence floor) does not apply to aster-routed candidates.

    WR-reason recovery is edge evidence about the STRATEGY, not a venue
    balance — it always applies, on every venue.
    """
    return bool(enabled) and reason == "drawdown" and venue_name == "aster"


def _active_executors() -> Dict[str, Any]:
    """Executors that own at least one symbol. A registered-but-symbol-less
    venue (bybit post-2026-08-15 migration — zero symbols, dead key) only
    produces 5-min failure-warning noise in the gathers; its failed leg
    contributed 0.0 to combined equity anyway, so skipping changes nothing
    but the log volume. Re-adding symbols re-includes it automatically."""
    return {v: ex for v, ex in _executors.items()
            if v == _DEFAULT_VENUE
            or any(sv == v for sv in _venue_by_symbol.values())}


async def update_leverage(symbol: str, symbol_id: int, target_lev: int,
                          account_id: int) -> int:
    """Signature bridge: SoDEX keys on symbol_id, Bybit/Aster on symbol."""
    ex = executor_for(symbol)
    if venue_for(symbol) in ("bybit", "aster"):
        return await ex.update_leverage_with_fallback(
            symbol=symbol, leverage=target_lev)
    return await ex.update_leverage_with_fallback(
        symbol_id, target_lev, account_id)


# Venues whose last poll raised are tracked so consumers can fail CLOSED:
# a failed leg must never read as "zero balance" or "no positions" downstream
# (2026-08-18: one Cloudflare blip → phantom 67% DD → 12h recovery freeze,
# and failed position polls booked as fake exchange_close PnL).
_positions_failures: set = set()
_balance_failures: set = set()


def positions_failed_venues() -> frozenset:
    return frozenset(_positions_failures)


def balance_failed_venues() -> frozenset:
    return frozenset(_balance_failures)


async def all_positions(address: str = "") -> List[Dict]:
    """Merged live position book across all registered venues."""
    results = await asyncio.gather(
        *(ex.get_positions(address) for ex in _active_executors().values()),
        return_exceptions=True,
    )
    merged: List[Dict] = []
    for venue, res in zip(_active_executors().keys(), results):
        if isinstance(res, Exception):
            _positions_failures.add(venue)
            _log_venue_failure("venue_positions_failed", venue, res)
            continue
        _positions_failures.discard(venue)
        merged.extend(res)
    return merged


async def combined_balance(address: str = "") -> float:
    """Total equity across venues — the number the vault watermark should track."""
    results = await asyncio.gather(
        *(ex.get_account_balance(address) for ex in _active_executors().values()),
        return_exceptions=True,
    )
    total = 0.0
    for venue, res in zip(_active_executors().keys(), results):
        if isinstance(res, Exception):
            _balance_failures.add(venue)
            _log_venue_failure("venue_balance_failed", venue, res)
            continue
        _balance_failures.discard(venue)
        total += float(res or 0.0)
    return total


async def venue_balances(address: str = "") -> Dict[str, float]:
    """Per-venue equity. Sizing must read the EXECUTING venue's collateral —
    sizing SoDEX flow off combined equity overstates margin by the Aster leg
    (watchdog flag 2026-08-15). Kingdom-level consumers (vault, drawdown)
    keep combined_balance — a bleed anywhere is a kingdom bleed."""
    results = await asyncio.gather(
        *(ex.get_account_balance(address) for ex in _active_executors().values()),
        return_exceptions=True,
    )
    out: Dict[str, float] = {}
    for venue, res in zip(_active_executors().keys(), results):
        if isinstance(res, Exception):
            _balance_failures.add(venue)
            _log_venue_failure("venue_balance_failed", venue, res)
            out[venue] = 0.0
            continue
        _balance_failures.discard(venue)
        out[venue] = float(res or 0.0)
    return out
