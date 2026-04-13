"""
Cross-Venue Funding Arbitrage Signal — Tier 7 Coherence Intelligence

Bybit funding leads SoDEX because Bybit has higher volume and price discovery.
When the funding spread between venues diverges, it signals which direction
SoDEX will equilibrate toward.

Signal logic:
  spread = bybit_rate - sodex_rate   (both as 8h decimal rates)

  spread > +2bps  → Bybit longs paying more = Bybit crowd over-leveraged long
                  → SoDEX will follow → fade long → LEAD_SHORT signal
  spread < -2bps  → Bybit shorts paying more = Bybit crowd over-leveraged short
                  → SoDEX will follow → fade short → LEAD_LONG signal
  |spread| < 2bps → Neutral — no edge

Confidence scales linearly from 0.0 (at 2bps) to 1.0 (at 5bps extreme).
Coherence bonus = confidence × 0.5 (max +0.5 when extreme spread).

This is Tier 7 in the coherence engine — lower ceiling than on-chain liquidation
(Tier 6) because it is a predictive rather than confirmed signal.
"""

from dataclasses import dataclass


SPREAD_THRESHOLD_BPS = 2.0   # Minimum spread to generate a signal
EXTREME_SPREAD_BPS   = 5.0   # Maximum bonus (confidence = 1.0) at this spread
MAX_BONUS            = 0.5   # Max coherence contribution from this tier


@dataclass
class CrossVenueSignal:
    symbol: str
    direction: str        # "lead_long" | "lead_short" | "neutral"
    spread_bps: float     # bybit_rate - sodex_rate in basis points (1bps = 0.0001)
    confidence: float     # 0.0–1.0
    bonus: float          # 0.0–MAX_BONUS coherence contribution


def compute_cross_venue_signal(
    symbol: str,
    sodex_rate: float,
    bybit_rate: float,
) -> CrossVenueSignal:
    """
    Compute cross-venue funding spread signal.

    Args:
        symbol: Trading symbol (e.g. "BTC-USD")
        sodex_rate: SoDEX 8h funding rate as decimal (0.001 = 0.1%)
        bybit_rate: Bybit 8h funding rate as decimal

    Returns:
        CrossVenueSignal with direction and coherence bonus.
    """
    spread_decimal = bybit_rate - sodex_rate
    spread_bps = spread_decimal * 10_000

    if abs(spread_bps) < SPREAD_THRESHOLD_BPS:
        return CrossVenueSignal(
            symbol=symbol,
            direction="neutral",
            spread_bps=round(spread_bps, 3),
            confidence=0.0,
            bonus=0.0,
        )

    # Confidence: linear from 0 at SPREAD_THRESHOLD to 1.0 at EXTREME_SPREAD
    confidence = min(
        1.0,
        (abs(spread_bps) - SPREAD_THRESHOLD_BPS)
        / (EXTREME_SPREAD_BPS - SPREAD_THRESHOLD_BPS),
    )
    bonus = round(confidence * MAX_BONUS, 4)

    # Direction: fade the over-leveraged venue
    # Positive spread: Bybit longs paying more → Bybit over-leveraged long → fade → SHORT
    # Negative spread: Bybit shorts paying more → Bybit over-leveraged short → fade → LONG
    direction = "lead_short" if spread_bps > 0 else "lead_long"

    return CrossVenueSignal(
        symbol=symbol,
        direction=direction,
        spread_bps=round(spread_bps, 3),
        confidence=round(confidence, 4),
        bonus=bonus,
    )


def cross_venue_direction_matches(signal: CrossVenueSignal, trade_direction: str) -> bool:
    """True when trade_direction aligns with the cross-venue signal's implied edge."""
    if signal.direction == "neutral":
        return False
    expected = "short" if signal.direction == "lead_short" else "long"
    return trade_direction == expected
