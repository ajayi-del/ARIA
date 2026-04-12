"""
SoDEXFeeEngine — Tier-aware fee calculator for SoDEX.

Fee structure (as of 2026-04-12):
  Perps taker: 0.040% / 0.036% / 0.032% / 0.028% / 0.026% (Tiers 0-4)
  Perps maker: 0.012% / 0.010% / 0.006% / 0.002% / 0.000% (Tiers 0-4)
  Spot  taker: 0.065% / 0.055% / 0.045% / 0.035% / 0.030% (Tiers 0-4)
  Spot  maker: 0.035% / 0.025% / 0.015% / 0.005% / 0.000% (Tiers 0-4)

Tier thresholds (14D weighted volume = perps_14d + 2×spot_14d):
  Tier 0: $0         Tier 1: $5M       Tier 2: $25M
  Tier 3: $100M      Tier 4: $500M

SOSO staking discounts applied AFTER tier-based rate lookup:
  0 SOSO → 0%    30 → 5%    300 → 10%    3,000 → 15%
  30,000 → 20%   300,000 → 50%

Key insight: spot-perp arb generates BOTH spot AND perp volume.
  Spot counts 2× toward the weighted volume threshold.
  1 arb cycle (buy spot + short perp, then unwind) generates:
    2 × spot_notional × 2 + 2 × perp_notional = 6× vs perp-only trading.
"""
import structlog
from typing import Tuple

log = structlog.get_logger(__name__)

# ── Fee tables ────────────────────────────────────────────────────────────────
PERPS_TAKER = [0.00040, 0.00036, 0.00032, 0.00028, 0.00026]
PERPS_MAKER = [0.00012, 0.00010, 0.00006, 0.00002, 0.00000]
SPOT_TAKER  = [0.00065, 0.00055, 0.00045, 0.00035, 0.00030]
SPOT_MAKER  = [0.00035, 0.00025, 0.00015, 0.00005, 0.00000]

# Tier thresholds in USD (14D weighted volume required to reach that tier)
TIER_THRESHOLDS = [0, 5_000_000, 25_000_000, 100_000_000, 500_000_000]

# SOSO staking: (tokens_staked, discount_fraction)
STAKING_DISCOUNTS = [
    (0,       0.00),
    (30,      0.05),
    (300,     0.10),
    (3_000,   0.15),
    (30_000,  0.20),
    (300_000, 0.50),
]


def _staking_discount(soso_staked: float) -> float:
    """Return the staking discount fraction for a given SOSO staked amount."""
    discount = 0.0
    for threshold, frac in STAKING_DISCOUNTS:
        if soso_staked >= threshold:
            discount = frac
        else:
            break
    return discount


def _tier_from_volume(weighted_14d: float) -> int:
    """Return tier index (0-4) from 14D weighted volume."""
    tier = 0
    for i, threshold in enumerate(TIER_THRESHOLDS):
        if weighted_14d >= threshold:
            tier = i
    return min(tier, len(TIER_THRESHOLDS) - 1)


class SoDEXFeeEngine:
    """
    Stateless fee calculator — given current tier and SOSO staked,
    returns effective rates and arb viability metrics.

    Usage:
        fee_engine = SoDEXFeeEngine(soso_staked=0, weighted_14d_volume=0)
        rate = fee_engine.perps_taker_fee()
        viable = fee_engine.is_arb_viable(funding_rate=0.001, periods=3)
    """

    def __init__(self, soso_staked: float = 0.0, weighted_14d_volume: float = 0.0):
        self.soso_staked = soso_staked
        self.weighted_14d_volume = weighted_14d_volume
        self._tier: int = _tier_from_volume(weighted_14d_volume)
        self._staking_discount: float = _staking_discount(soso_staked)

        # Live rates — set by apply_live_rates() from /fee-rate endpoint.
        # None means: use hardcoded table. Set to override with exchange-authoritative values.
        self._live_spot_taker: float = 0.0
        self._live_spot_maker: float = 0.0
        self._live_perp_taker: float = 0.0
        self._live_perp_maker: float = 0.0

    def update(self, soso_staked: float, weighted_14d_volume: float) -> None:
        """Refresh engine state from volume tracker — call daily at UTC midnight."""
        self.soso_staked = soso_staked
        self.weighted_14d_volume = weighted_14d_volume
        self._tier = _tier_from_volume(weighted_14d_volume)
        self._staking_discount = _staking_discount(soso_staked)
        log.info(
            "fee_engine_updated",
            tier=self._tier,
            weighted_14d=f"${weighted_14d_volume:,.0f}",
            soso_staked=soso_staked,
            staking_discount=f"{self._staking_discount*100:.0f}%",
        )

    def apply_live_rates(self, spot_rates: dict, perp_rates: dict = None) -> None:
        """
        Override computed rates with live rates from the SoDEX fee-rate endpoint.

        Calling this after fetching from /accounts/{addr}/fee-rate ensures we use
        the exchange's authoritative rates rather than hardcoded tables.
        Safe to call frequently (weight=2 each call).

        Args:
            spot_rates: dict from SoDEXSpotClient.fetch_fee_rate()
                        keys: makerFeeRate, takerFeeRate, tier
            perp_rates: dict from SoDEXClient.fetch_perp_fee_rate() (optional)
                        same key structure
        """
        if spot_rates.get("takerFeeRate", 0) > 0:
            self._live_spot_taker = spot_rates["takerFeeRate"]
            self._live_spot_maker = spot_rates.get("makerFeeRate", 0.0)
        if perp_rates and perp_rates.get("takerFeeRate", 0) > 0:
            self._live_perp_taker = perp_rates["takerFeeRate"]
            self._live_perp_maker = perp_rates.get("makerFeeRate", 0.0)
        # Update tier from exchange data (more authoritative than volume calculation)
        live_tier = spot_rates.get("tier", -1)
        if live_tier >= 0:
            self._tier = live_tier
        log.debug(
            "fee_engine_live_rates_applied",
            spot_taker=f"{self.spot_taker_fee()*100:.4f}%",
            spot_maker=f"{self.spot_maker_fee()*100:.4f}%",
            perp_taker=f"{self.perps_taker_fee()*100:.4f}%",
            perp_maker=f"{self.perps_maker_fee()*100:.4f}%",
            tier=self._tier,
        )

    # ── Raw rate accessors ────────────────────────────────────────────────────

    def perps_taker_fee(self) -> float:
        """Effective perps taker fee. Uses live rate from exchange if available."""
        if self._live_perp_taker > 0:
            return self._live_perp_taker
        return PERPS_TAKER[self._tier] * (1.0 - self._staking_discount)

    def perps_maker_fee(self) -> float:
        """Effective perps maker fee. Uses live rate from exchange if available."""
        if self._live_perp_maker > 0:
            return self._live_perp_maker
        return PERPS_MAKER[self._tier] * (1.0 - self._staking_discount)

    def spot_taker_fee(self) -> float:
        """Effective spot taker fee. Uses live rate from exchange if available."""
        if self._live_spot_taker > 0:
            return self._live_spot_taker
        return SPOT_TAKER[self._tier] * (1.0 - self._staking_discount)

    def spot_maker_fee(self) -> float:
        """Effective spot maker fee. Uses live rate from exchange if available."""
        if self._live_spot_maker > 0:
            return self._live_spot_maker
        return SPOT_MAKER[self._tier] * (1.0 - self._staking_discount)

    # ── Arb metrics ──────────────────────────────────────────────────────────

    def arb_round_trip_cost(self, use_maker: bool = True) -> float:
        """
        Total fee for one complete spot-perp arb cycle:
          open_spot + open_perp + close_spot + close_perp

        With maker orders: (spot_maker + perp_maker) × 2
        With taker orders: (spot_taker + perp_taker) × 2
        """
        if use_maker:
            return (self.spot_maker_fee() + self.perps_maker_fee()) * 2.0
        return (self.spot_taker_fee() + self.perps_taker_fee()) * 2.0

    def arb_break_even_funding(self, periods: int = 3, use_maker: bool = True) -> float:
        """
        Minimum funding rate per 8h period to cover the round-trip fee cost.

        break_even = round_trip_cost / periods

        Example (Tier 0, taker, 3 periods):
          round_trip = (0.00065 + 0.00040) × 2 = 0.0021
          break_even = 0.0021 / 3 = 0.0007 (0.07% per 8h)

        Example (Tier 0, maker, 3 periods):
          round_trip = (0.00035 + 0.00012) × 2 = 0.00094
          break_even = 0.00094 / 3 = 0.000313 (0.0313% per 8h)
        """
        if periods <= 0:
            return float("inf")
        return self.arb_round_trip_cost(use_maker=use_maker) / periods

    def is_arb_viable(
        self,
        funding_rate: float,
        periods: int = 3,
        use_maker: bool = True,
        safety_margin: float = 1.5,
    ) -> bool:
        """
        Returns True if |funding_rate| exceeds break-even × safety_margin.

        safety_margin=1.5 means funding must be 50% above break-even to open.
        This accounts for: funding rate decay, slippage, basis risk.

        Args:
            funding_rate: 8h rate as a fraction (e.g. 0.001 = 0.1%)
            periods: expected number of 8h collection periods before exit
            use_maker: True = assume maker fill for both legs
            safety_margin: multiplier on break-even (default 1.5×)
        """
        be = self.arb_break_even_funding(periods=periods, use_maker=use_maker)
        required = be * safety_margin
        viable = abs(funding_rate) >= required
        if not viable:
            log.debug(
                "arb_not_viable",
                funding_rate=f"{abs(funding_rate)*100:.4f}%",
                required=f"{required*100:.4f}%",
                break_even=f"{be*100:.4f}%",
                tier=self._tier,
                use_maker=use_maker,
            )
        return viable

    def directional_break_even_move(self, leverage: int = 5) -> float:
        """
        Minimum price move (as a fraction) to cover a round-trip perps trade.
        Accounts for leverage multiplying the notional.

        break_even_move = (perp_taker_fee × 2) / leverage

        Example (Tier 0, 5×): (0.00040 × 2) / 5 = 0.016% price move needed.
        """
        return (self.perps_taker_fee() * 2.0) / leverage

    # ── Tier progression ─────────────────────────────────────────────────────

    def current_tier(self) -> int:
        return self._tier

    def volume_to_next_tier(self) -> float:
        """
        Returns USD volume needed to reach the next tier.
        Returns 0.0 if already at max tier.
        """
        if self._tier >= len(TIER_THRESHOLDS) - 1:
            return 0.0
        next_threshold = TIER_THRESHOLDS[self._tier + 1]
        return max(0.0, next_threshold - self.weighted_14d_volume)

    def tier_summary(self) -> dict:
        """Human-readable summary of current fee tier and rates."""
        return {
            "tier": self._tier,
            "weighted_14d_volume": self.weighted_14d_volume,
            "volume_to_next_tier": self.volume_to_next_tier(),
            "soso_staked": self.soso_staked,
            "staking_discount_pct": self._staking_discount * 100,
            "perps_taker_pct": self.perps_taker_fee() * 100,
            "perps_maker_pct": self.perps_maker_fee() * 100,
            "spot_taker_pct": self.spot_taker_fee() * 100,
            "spot_maker_pct": self.spot_maker_fee() * 100,
            "arb_round_trip_maker_pct": self.arb_round_trip_cost(use_maker=True) * 100,
            "arb_break_even_3periods_maker_pct": self.arb_break_even_funding(3, True) * 100,
        }
