"""
intelligence/sodex_ob_swap.py — SoDEX orderbook ownership swap (live-by-order)

Governor 2026-10-20: "i want the sodex swap done live not only on shadow ...
bybit for comparison but sodex fills." Gates that read the L4 book (Gate 5
spread/depth, cascade L4 confirm, sweep/imbalance detectors) must read the
EXECUTION venue's book — SoDEX's own per-block L4 — for SoDEX-routed
crypto, not Bybit's 100ms depth (signal-plane book pricing execution-plane
decisions = the same defect class the 2026-08-20 Aster OB repair 8ed4cde
closed for aster-routed symbols; this extends that idiom to the main venue).

Mechanics (why no subscription change is needed): both feeds subscribe
their full symbol lists already — SoDEX l2Book/l4Book per config.assets,
Bybit depth per BYBIT_SYMBOL_MAP. The store dicts are write targets looked
up per message; a symbol without a store entry is silently dropped
(sodex_feed.py:631-633). The swap only changes WHICH store each feed
writes to for a swapped symbol:
  - SoDEX feed gains the canonical orderbook_stores entry → gates read
    SoDEX spreads/depth/imbalance.
  - Bybit feed gains a per-symbol shadow store → Bybit books keep flowing
    as the comparison plane, never read by gates.
Candles and trade flow are NOT swapped: Bybit 1m confirmed closes remain
the signal plane (deeper history, the interpreter's contract); marks were
already SoDEX-native for all assets.

Comparison measured, not stored: _ob_swap_diff_loop (300s) logs
sodex_bybit_ob_diff rows — execution-venue spread (the spread we actually
pay) + mid divergence vs the Bybit shadow book.

Live-by-order (same override class as the 2026-10-17 stock-arch and
2026-10-10 scalper precedents): default ON. Kill switch
SODEX_OB_SWAP_ENABLED=false = legacy bit-for-bit: Bybit owns the canonical
OB for every non-aster/arcus symbol, SoDEX feed gets only the non-Bybit
class, no shadow stores, no diff rows.
"""
from __future__ import annotations

import os
from typing import Callable, Optional

EXECUTION_VENUES = ("aster", "arcus")


def sodex_ob_swap_enabled() -> bool:
    return os.environ.get("SODEX_OB_SWAP_ENABLED", "true").strip().lower() != "false"


def swapped_symbols(assets, bybit_map, venue_for: Callable[[str], str]):
    """SoDEX-routed crypto symbols whose canonical OB store moves from the
    Bybit feed to the SoDEX feed.

    Excluded: aster/arcus-routed (their execution feeds already own their
    L4) and symbols with no live Bybit mapping (the legacy _sodex_only
    class — equities/commodities already SoDEX-owned).

    Fail-closed: any defect → [] → no swap → legacy ownership.
    """
    try:
        out = []
        for a in assets or []:
            if bybit_map.get(a, "unknown") in (None, "unknown"):
                continue
            if venue_for(a) in EXECUTION_VENUES:
                continue
            out.append(a)
        return out
    except Exception:
        return []


def bybit_ob_view(canonical: dict, shadow: dict, venue_for: Callable[[str], str]) -> dict:
    """Bybit feed OB write targets: canonical stores minus execution-venue-
    owned (aster/arcus) symbols, with swapped symbols redirected to their
    shadow comparison stores. Empty shadow = legacy bit-for-bit.

    No try/except: exception behavior identical to the legacy inline
    comprehension (same calls in the same order).
    """
    out = {}
    for a, s in canonical.items():
        if venue_for(a) in EXECUTION_VENUES:
            continue
        out[a] = shadow[a] if a in shadow else s
    return out


def sodex_ob_view(sodex_only_ob: dict, canonical: dict, swapped) -> dict:
    """SoDEX feed OB write targets: the legacy non-Bybit class plus the
    swapped symbols' canonical stores. Empty swapped = legacy bit-for-bit."""
    out = dict(sodex_only_ob)
    for a in swapped or []:
        if a in canonical:
            out[a] = canonical[a]
    return out


def ob_mid_divergence_bps(sodex_store, bybit_store) -> Optional[float]:
    """Mid-price divergence SoDEX vs Bybit shadow in bps (signed, SoDEX
    relative to Bybit). None = either book empty/unreadable — never diff a
    dark plane."""
    try:
        sb, sa, _ = sodex_store.top_of_book()
        bb, ba, _ = bybit_store.top_of_book()
        if not (sb and sa and bb and ba):
            return None
        s_mid = (sb + sa) / 2.0
        b_mid = (bb + ba) / 2.0
        if s_mid <= 0 or b_mid <= 0:
            return None
        return (s_mid - b_mid) / b_mid * 10_000.0
    except Exception:
        return None


def ob_spread_bps(store) -> Optional[float]:
    """Top-of-book spread in bps of mid. None = empty/unreadable book."""
    try:
        b, a, _ = store.top_of_book()
        if not (b and a):
            return None
        mid = (b + a) / 2.0
        if mid <= 0:
            return None
        return (a - b) / mid * 10_000.0
    except Exception:
        return None
