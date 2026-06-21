"""
intelligence/dispersion_gate.py — Dispersion-Gated Asset Selection
ARIA Execution Alpha Patch — Component 4 (v2)

Uses RegimeState.dispersion (cross-sectional std of momentum scores, already computed
by the regime classifier) to gate which assets are tradeable:

  Low  (<0.002): assets correlated → alts have no independent edge, only BTC/ETH
  Mid  (0.002–0.004): normal market → all assets tradeable
  High (>0.004): strong divergence → only leading sector + large caps

This prevents trading alts during correlated sell-offs where alt signals are
just noise copies of BTC, and forces focus on leaders during dispersion events.
"""
from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)

LOW_DISP  = 0.015
HIGH_DISP = 0.040

_LARGE_CAP = frozenset({"BTC-USD", "ETH-USD"})


class DispersionGate:
    """Filter asset tradability based on current cross-sectional momentum dispersion."""

    def should_trade(
        self,
        symbol:         str,
        dispersion:     float,
        leading_sector: str = "",   # RegimeState.leading_category
        asset_category: str = "",   # ASSET_CONFIG[symbol]["category"]
    ) -> tuple[bool, str]:
        """
        Returns (allowed: bool, reason: str).
        Always allows large caps. Filters alts in low/high dispersion regimes.
        """
        is_large_cap = symbol in _LARGE_CAP

        if dispersion < LOW_DISP:
            if not is_large_cap:
                return False, f"low_dispersion_{round(dispersion, 4)}_alts_no_edge"
            return True, "large_cap_always_ok"

        if dispersion > HIGH_DISP:
            if not is_large_cap and asset_category != leading_sector and leading_sector:
                return (
                    False,
                    f"high_dispersion_not_leader_{asset_category}_vs_{leading_sector}",
                )
            return True, "leader_or_large_cap"

        return True, "mid_dispersion_normal"
