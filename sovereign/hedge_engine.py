"""
sovereign/hedge_engine.py — Proxy hedge computation for SSI index positions.

Architecture
────────────
When Sovereign enters TRANSITION or BEAR phase, it opens perp shorts as a
proxy hedge against the directional exposure of MAG7.ssi and DEFI.ssi.

Hedge constraints (HARD RULES — never relaxed):

  MAG7.ssi:
    Hedgeable fraction = 67.58% (BTC + ETH + BNB + SOL constituents)
    Residual basis risk = 32.42% (XRP / DOGE / ADA — no perp hedge available)
    Proxy weights (renormalised to hedgeable fraction):
      BTC-USD: 47.16%  ETH-USD: 20.24%  BNB-USD: 16.40%  SOL-USD: 16.20%

  DEFI.ssi:
    Proxy = LINK-USD short only (LINK is the highest-liquid available on SoDEX perps)
    Basis risk = all other DeFi constituents not covered by LINK

  MEME.ssi:
    UNHEDGEABLE — no proxy available. Log warning, take no action.

  USSI:
    NEVER hedge. USSI is internally delta-neutral (holds staked + short).
    Adding external perp short = net short BTC exposure. HARD BLOCK.
"""

from __future__ import annotations

import structlog
from dataclasses import dataclass
from typing import Dict, List, Optional

log = structlog.get_logger(__name__)

# ── Hedge configuration ───────────────────────────────────────────────────────

# Fraction of MAG7.ssi exposure that can be proxy-hedged via crypto perps
MAG7_HEDGEABLE_FRACTION: float = 0.6758   # 67.58% — BTC/ETH/BNB/SOL weighted exposure

# Proxy weights within the hedgeable fraction (sum to 1.0)
MAG7_HEDGE_WEIGHTS: Dict[str, float] = {
    "BTC-USD": 0.4716,
    "ETH-USD": 0.2024,
    "BNB-USD": 0.1640,
    "SOL-USD": 0.1620,
}

# Unhedgeable basis risk (XRP/DOGE/ADA)
MAG7_BASIS_RISK_PCT: float = 1.0 - MAG7_HEDGEABLE_FRACTION   # 32.42%

# DEFI proxy: LINK only
DEFI_PROXY_SYMBOL: str = "LINK-USD"
DEFI_HEDGEABLE_FRACTION: float = 0.40   # conservative — LINK is only one DeFi constituent


@dataclass
class HedgeInstruction:
    """A single perp short instruction for a proxy hedge leg."""
    symbol:       str     # e.g. "BTC-USD"
    side:         str     # always "short" for hedge
    notional_usd: float   # target hedge notional in USD
    reason:       str     # e.g. "MAG7.ssi proxy hedge (47.16% of hedgeable 67.58%)"


@dataclass
class HedgePlan:
    """Full hedge plan for the current Sovereign position."""
    instructions:        List[HedgeInstruction]
    total_hedged_usd:    float   # sum of hedge notionals
    total_portfolio_usd: float   # portfolio value being hedged
    coverage_pct:        float   # total_hedged / total_portfolio
    residual_basis_usd:  float   # estimated unhedgeable exposure
    residual_basis_pct:  float   # unhedgeable fraction of portfolio
    notes:               List[str]


class HedgeEngine:
    """
    Computes proxy hedge instructions for Sovereign's SSI positions.

    Only called when rotation_engine signals TRANSITION or BEAR phase.
    All USSI requests are hard-blocked with a logged error.
    """

    def compute_plan(
        self,
        positions: Dict[str, "SSIPosition"],
        hedge_fraction: float = 1.0,
    ) -> HedgePlan:
        """
        Generate a full hedge plan for the current positions.

        Args:
            positions: from SovereignPortfolio.positions
            hedge_fraction: 0.0–1.0 scale for partial hedging (default = full hedge)

        Returns HedgePlan with all perp short instructions.
        """
        instructions: List[HedgeInstruction] = []
        notes: List[str] = []
        total_portfolio_usd = sum(p.current_usd for p in positions.values())
        total_hedged_usd = 0.0
        residual_basis_usd = 0.0

        for sym, pos in positions.items():
            if pos.current_usd <= 0:
                continue

            if sym == "USSI-USD":
                # HARD RULE #1: NEVER hedge USSI
                log.debug(
                    "hedge_engine_ussi_skip",
                    note="USSI is delta-neutral internally — no hedge needed or allowed",
                )
                notes.append("USSI: no hedge (internally delta-neutral)")
                continue

            elif sym == "MAG7SSI-USD":
                legs, basis, leg_notes = self._mag7_legs(pos.current_usd, hedge_fraction)
                instructions.extend(legs)
                total_hedged_usd += sum(l.notional_usd for l in legs)
                residual_basis_usd += basis
                notes.extend(leg_notes)

            elif sym == "DEFISSI-USD":
                legs, basis, leg_notes = self._defi_legs(pos.current_usd, hedge_fraction)
                instructions.extend(legs)
                total_hedged_usd += sum(l.notional_usd for l in legs)
                residual_basis_usd += basis
                notes.extend(leg_notes)

            elif sym == "MEMESSI-USD":
                # MEME is unhedgeable — log and skip
                unhedgeable = pos.current_usd
                residual_basis_usd += unhedgeable
                msg = (
                    f"MEME.ssi: ${unhedgeable:.2f} UNHEDGEABLE "
                    "(DOGE/SHIB/PEPE/PUMP/TRUMP/BONK — no liquid perp proxy on SoDEX)"
                )
                notes.append(msg)
                log.warning(
                    "hedge_engine_meme_unhedgeable",
                    notional_usd=round(unhedgeable, 2),
                    note=msg,
                )

        coverage_pct = (total_hedged_usd / total_portfolio_usd) if total_portfolio_usd > 0 else 0.0
        residual_pct = (residual_basis_usd / total_portfolio_usd) if total_portfolio_usd > 0 else 0.0

        log.info(
            "hedge_plan_computed",
            instructions=len(instructions),
            total_hedged_usd=round(total_hedged_usd, 2),
            total_portfolio_usd=round(total_portfolio_usd, 2),
            coverage_pct=round(coverage_pct * 100, 1),
            residual_basis_usd=round(residual_basis_usd, 2),
            residual_basis_pct=round(residual_pct * 100, 1),
        )

        return HedgePlan(
            instructions=instructions,
            total_hedged_usd=round(total_hedged_usd, 2),
            total_portfolio_usd=round(total_portfolio_usd, 2),
            coverage_pct=round(coverage_pct, 4),
            residual_basis_usd=round(residual_basis_usd, 2),
            residual_basis_pct=round(residual_pct, 4),
            notes=notes,
        )

    def _mag7_legs(
        self,
        notional_usd: float,
        scale: float,
    ) -> tuple:
        """Decompose MAG7.ssi notional into BTC/ETH/BNB/SOL short legs."""
        hedgeable_usd = notional_usd * MAG7_HEDGEABLE_FRACTION * scale
        basis_usd     = notional_usd * MAG7_BASIS_RISK_PCT      # always unhedgeable regardless of scale

        instructions = []
        for sym, weight in MAG7_HEDGE_WEIGHTS.items():
            leg_notional = hedgeable_usd * weight
            if leg_notional < 10.0:
                continue   # Skip dust legs
            instructions.append(HedgeInstruction(
                symbol=sym,
                side="short",
                notional_usd=round(leg_notional, 2),
                reason=(
                    f"MAG7.ssi proxy hedge "
                    f"({weight*100:.2f}% of hedgeable {MAG7_HEDGEABLE_FRACTION*100:.2f}%)"
                ),
            ))

        notes = [
            f"MAG7.ssi: ${hedgeable_usd:.2f} hedged via BTC/ETH/BNB/SOL, "
            f"${basis_usd:.2f} basis risk (XRP/DOGE/ADA = {MAG7_BASIS_RISK_PCT*100:.1f}%)"
        ]
        return instructions, basis_usd, notes

    def _defi_legs(
        self,
        notional_usd: float,
        scale: float,
    ) -> tuple:
        """DEFI.ssi → LINK-USD short (single proxy leg)."""
        hedgeable_usd = notional_usd * DEFI_HEDGEABLE_FRACTION * scale
        basis_usd     = notional_usd * (1.0 - DEFI_HEDGEABLE_FRACTION)

        instructions = []
        if hedgeable_usd >= 10.0:
            instructions.append(HedgeInstruction(
                symbol=DEFI_PROXY_SYMBOL,
                side="short",
                notional_usd=round(hedgeable_usd, 2),
                reason=(
                    f"DEFI.ssi proxy hedge via LINK "
                    f"({DEFI_HEDGEABLE_FRACTION*100:.0f}% hedgeable fraction)"
                ),
            ))

        notes = [
            f"DEFI.ssi: ${hedgeable_usd:.2f} hedged via LINK, "
            f"${basis_usd:.2f} basis risk (AAVE/UNI/MKR/CRV not available on SoDEX)"
        ]
        return instructions, basis_usd, notes

    def get_residual_basis_risk(
        self,
        positions: Dict[str, "SSIPosition"],
    ) -> float:
        """
        Compute total unhedgeable basis risk in USD.
        Logged every Sovereign cycle (hard rule #8).
        """
        total = 0.0
        for sym, pos in positions.items():
            if sym == "MAG7SSI-USD":
                total += pos.current_usd * MAG7_BASIS_RISK_PCT
            elif sym == "DEFISSI-USD":
                total += pos.current_usd * (1.0 - DEFI_HEDGEABLE_FRACTION)
            elif sym == "MEMESSI-USD":
                total += pos.current_usd  # fully unhedgeable
        return round(total, 2)
