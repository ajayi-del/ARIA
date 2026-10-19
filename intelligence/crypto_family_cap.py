"""
intelligence/crypto_family_cap.py — crypto same-direction family concurrent cap

Mirrors the equity correlation cap (main.py equity_correlation_cap_*) for the
crypto families in intelligence/relative_strength.ASSET_CATEGORIES. Family
members share beta: 4 simultaneous alt_l1 longs is one 4x alt-beta position
wearing 4 costumes.

Doctrine (matches the equity cap's ladder, one notch tighter per the spec):
  1st concurrent same-direction in family → pass
  2nd concurrent                          → half size (×0.5, sizing-chain idiom)
  3rd+                                    → reject (signal_rejected_crypto_family_cap)

Only the five crypto families are capped; commodity/index categories carry
their own caps (equity correlation cap) or none (commodities). Unknown /
unmapped symbols pass — the cap never invents a family.

Kill switch: CRYPTO_FAMILY_CAP_ENABLED (default true). False = pre-module
system bit-for-bit (no count, no flag, no reject).
Shadow: gate "crypto_family_cap" — would-blocks recorded from birth via
record_would_block at the splice in BOTH live and disabled modes
(tp_invariant idiom).
"""
from __future__ import annotations

import os
from typing import Optional

from intelligence.relative_strength import ASSET_CATEGORIES

CRYPTO_FAMILIES = frozenset({
    "alt_l1", "defi_infra", "meme", "large_cap", "cex_ecosystem",
})

FAMILY_SIZE_MULT = 0.5


def crypto_family_cap_enabled() -> bool:
    return os.environ.get("CRYPTO_FAMILY_CAP_ENABLED", "true").strip().lower() != "false"


def crypto_family_of(symbol: str) -> Optional[str]:
    """The capped crypto family for symbol, or None (uncapped/unknown)."""
    try:
        cat = ASSET_CATEGORIES.get(str(symbol))
    except Exception:
        return None
    return cat if cat in CRYPTO_FAMILIES else None


def family_cap_verdict(open_count: int) -> str:
    """open_count = same-family same-direction positions already open.

    0 → "pass" (this is the 1st), 1 → "half" (2nd concurrent),
    ≥2 → "block" (3rd+ concurrent). Fail-closed on garbage.
    """
    try:
        n = int(open_count)
    except (TypeError, ValueError):
        return "block"
    if n <= 0:
        return "pass"
    if n == 1:
        return "half"
    return "block"
