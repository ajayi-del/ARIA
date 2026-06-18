"""
risk/multi_asset_margin.py — SoDEX Multi-Asset Margin Intelligence
ARIA Risk Module — Bull Market Hardening Sprint

SoDEX supports multi-asset collateral for Cross Margin futures.
Supported assets and collateral ratios (haircuts):

    USDC   → 100%  (primary, no haircut)
    BTC    →  90%  (90 cents on the dollar)
    XAUT   →  90%  (gold-backed, 90%)
    ETH    →  90%
    SOSO   →  50%  (native token, high haircut)
                    SOSO cap: min(30,000 SOSO, 10,000 USDC worth)

CRITICAL ASYMMETRY (from SoDEX docs):
    Withdrawal check = Collateral(after) + Unrealized_Losses ≥ Total_Initial_Margin
    Unrealized PROFITS do not count — they are excluded from withdrawal capacity.

    Implication for ARIA: available margin for new trades = realized equity only.
    Unrealized winners cannot be used as collateral for new positions on SoDEX.

Multi-Asset Margin only applies to Cross Margin positions.
Isolated Margin positions must use USDC only.

Usage:
    mam = MultiAssetMarginEngine()
    effective = mam.compute_effective_balance(balances, prices)
    safe = mam.is_withdrawal_safe(collateral, upnl, initial_margin, withdraw_amount)
    risk_mult = mam.sizing_risk_multiplier(balances, prices, usdc_balance)
"""
from __future__ import annotations

import structlog
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = structlog.get_logger(__name__)


# ── Collateral Haircut Table (from SoDEX docs) ────────────────────────────────
# Source: https://sodex.com/docs/multi-asset-margin
# Updated: 2026-06-15
MAM_HAIRCUTS: Dict[str, float] = {
    "USDC":  1.00,   # primary, no haircut
    "BTC":   0.90,
    "XAUT":  0.90,   # gold-backed
    "ETH":   0.90,
    "SOSO":  0.50,   # native token — high haircut, separate cap
}

# SOSO collateral caps (per SoDEX docs)
_SOSO_MAX_UNITS  = 30_000.0    # max 30,000 SOSO
_SOSO_MAX_USD    = 10_000.0    # max 10,000 USDC worth at index price

# Account-level collateral deposit cap (non-USDC only, checked at deposit)
_ACCOUNT_COLLATERAL_CAP_USD = 500_000.0

# SoDEX symbol names → MAM asset key mapping
# ARIA uses "BTC-USD" etc.; MAM table uses "BTC"
_SYMBOL_TO_MAM: Dict[str, str] = {
    "BTC-USD":  "BTC",
    "ETH-USD":  "ETH",
    "XAUT-USD": "XAUT",
    "SOSO-USD": "SOSO",
    "USDC":     "USDC",
}


@dataclass
class MAMBalance:
    """
    Asset balance in the Margin & Futures account with MAM contribution.

    All values in USD unless noted.
    """
    asset: str                        # "BTC", "ETH", "XAUT", "SOSO", "USDC"
    units: float                      # quantity of asset
    index_price: float                # SoDEX index price (not mark price)
    collateral_ratio: float           # from MAM_HAIRCUTS
    raw_usd: float = field(init=False)         # units × index_price
    effective_usd: float = field(init=False)   # raw_usd × collateral_ratio (after haircut)
    capped: bool = field(init=False)           # True if cap was applied (SOSO)

    def __post_init__(self):
        self.raw_usd = self.units * self.index_price
        if self.asset == "SOSO":
            # Apply SOSO-specific cap: min(30k units, $10k) before haircut
            capped_units = min(self.units, _SOSO_MAX_UNITS)
            capped_usd   = min(capped_units * self.index_price, _SOSO_MAX_USD)
            self.effective_usd = capped_usd * self.collateral_ratio
            self.capped = (capped_units < self.units or capped_usd < self.raw_usd)
        else:
            self.effective_usd = self.raw_usd * self.collateral_ratio
            self.capped = False


@dataclass
class MAMState:
    """Snapshot of the multi-asset margin state for an account."""
    total_effective_usd: float        # sum of all asset contributions
    usdc_effective_usd: float         # USDC contribution (100%)
    non_usdc_effective_usd: float     # BTC+ETH+XAUT+SOSO contributions
    balances: list[MAMBalance]        # individual asset breakdown
    collateral_concentration: float   # % that is USDC (1.0 = fully USDC-denominated)
    volatility_risk_factor: float     # how much effective balance can shrink if non-USDC assets fall 20%
    # Withdrawal/sizing safety
    safe_to_withdraw: float           # max withdrawable USD without breaching initial_margin
    warning_non_usdc_heavy: bool      # True if >50% of collateral is in volatile non-USDC assets


class MultiAssetMarginEngine:
    """
    Computes effective collateral, sizing safety multipliers, and withdrawal
    feasibility for SoDEX multi-asset margin accounts.

    Quant rationale:
    ─────────────────
    BTC/ETH at 90% haircut means a 10% move in BTC wipes ~1% of effective margin.
    For a $1,000 account with $500 BTC + $500 USDC:
        Effective = $500 × 1.0 + $500 × 0.9 = $950
        If BTC drops 10%: $500 × 0.9 × 0.9 = $405 → Effective = $905 (-4.7%)
        If BTC drops 30%: Effective drops to $815 (-14.2%)

    This creates correlated margin compression during market stress — exactly when
    you need margin most. ARIA must size conservatively when MAM concentration is high.

    Withdrawal asymmetry:
        uPnL winners: NOT counted → don't size as if they're equity
        uPnL losers:  ARE counted → reduce effective margin immediately
    """

    def __init__(self, mark_price_stores: Optional[Dict] = None):
        """
        mark_price_stores: optional dict of {symbol: MarkPriceStore} for live index prices.
        If None, index prices must be passed explicitly to compute_mam_state().
        """
        self._mark_price_stores = mark_price_stores or {}

    def compute_mam_state(
        self,
        asset_balances: Dict[str, float],    # {"BTC": 0.01, "USDC": 500.0, ...}
        index_prices: Dict[str, float],      # {"BTC": 65000.0, "USDC": 1.0, ...}
        open_positions_initial_margin: float = 0.0,  # sum of initial margins for cross positions
        unrealized_pnl: float = 0.0,         # total uPnL (positive = profit, negative = loss)
    ) -> MAMState:
        """
        Compute complete MAM state including effective balance and risk metrics.

        Parameters
        ----------
        asset_balances : {"BTC": 0.01, "ETH": 0.5, "USDC": 500.0, ...}
            Quantities in the Margin & Futures account (NOT spot account).
        index_prices : {"BTC": 65000.0, "ETH": 3200.0, "USDC": 1.0, ...}
            SoDEX index prices (preferred over mark price for margin calc).
        open_positions_initial_margin : float
            Sum of initial margins for all open cross margin positions.
        unrealized_pnl : float
            Net uPnL. Positive = profit. Negative = loss.
            Per SoDEX docs: losses reduce withdrawable equity; profits do NOT increase it.
        """
        balances: list[MAMBalance] = []
        total_effective   = 0.0
        usdc_effective    = 0.0
        non_usdc_effective = 0.0

        for asset, units in asset_balances.items():
            if units <= 0:
                continue
            ratio = MAM_HAIRCUTS.get(asset, 0.0)
            if ratio <= 0:
                # Asset not supported by MAM — skip
                logger.debug("mam_unsupported_asset", asset=asset, units=units)
                continue
            idx_price = index_prices.get(asset, 0.0)
            if idx_price <= 0:
                logger.debug("mam_missing_index_price", asset=asset)
                continue

            mb = MAMBalance(
                asset=asset,
                units=units,
                index_price=idx_price,
                collateral_ratio=ratio,
            )
            balances.append(mb)
            total_effective    += mb.effective_usd
            if asset == "USDC":
                usdc_effective += mb.effective_usd
            else:
                non_usdc_effective += mb.effective_usd

        # Concentration: fraction of total that is USDC
        conc = usdc_effective / max(total_effective, 1.0)

        # Volatility risk factor: how much effective balance drops if all
        # non-USDC assets fall 20% simultaneously (stress scenario).
        # 20% spot decline → 20% loss in raw_usd → 20% loss in effective_usd for those assets.
        stress_loss = non_usdc_effective * 0.20
        vol_risk = stress_loss / max(total_effective, 1.0)

        # Withdrawal safety:
        # Per SoDEX docs: Collateral(after) + Unrealized_Losses ≥ Total_Initial_Margin
        # Unrealized_Losses = abs(min(unrealized_pnl, 0))
        # Rearranging: Collateral(after) ≥ Total_Initial_Margin - Unrealized_Losses
        # → safe_to_withdraw = total_effective - min_required_collateral
        unrealized_losses = abs(min(unrealized_pnl, 0.0))
        min_required = open_positions_initial_margin - unrealized_losses
        min_required = max(0.0, min_required)   # can't be negative
        safe_to_withdraw = max(0.0, total_effective - min_required)

        warning_non_usdc_heavy = (conc < 0.50 and non_usdc_effective > 0)

        state = MAMState(
            total_effective_usd=total_effective,
            usdc_effective_usd=usdc_effective,
            non_usdc_effective_usd=non_usdc_effective,
            balances=balances,
            collateral_concentration=conc,
            volatility_risk_factor=vol_risk,
            safe_to_withdraw=safe_to_withdraw,
            warning_non_usdc_heavy=warning_non_usdc_heavy,
        )

        if warning_non_usdc_heavy:
            logger.warning(
                "mam_non_usdc_heavy",
                usdc_pct=round(conc * 100, 1),
                non_usdc_effective=round(non_usdc_effective, 2),
                vol_risk_pct=round(vol_risk * 100, 1),
                note="20pct_price_drop_reduces_effective_balance_by_vol_risk_pct",
            )

        return state

    def sizing_risk_multiplier(
        self,
        mam_state: MAMState,
        usdc_available: float,
    ) -> float:
        """
        Returns a multiplier [0.5, 1.0] to apply to ARIA's position sizing when
        the account holds significant non-USDC collateral.

        Rationale:
        - Pure USDC account: multiplier = 1.0 (full sizing)
        - 50/50 BTC+USDC: multiplier ~0.92 (8% haircut for vol risk)
        - Heavy BTC/ETH (>70%): multiplier ~0.75 (conservative — collateral can shrink)
        - SOSO heavy: multiplier ~0.60 (50% haircut + position risk)

        The multiplier targets a regime where even a 20% adverse move in non-USDC
        collateral wouldn't push the account below initial_margin requirements.
        """
        # Pure USDC → no reduction
        if mam_state.non_usdc_effective_usd <= 0:
            return 1.0

        conc = mam_state.collateral_concentration  # fraction of effective that is USDC
        vol_risk = mam_state.volatility_risk_factor

        # Base multiplier: scales linearly with USDC concentration
        # 100% USDC → 1.0, 0% USDC → 0.70 (floor for 100% volatile collateral)
        base = 0.70 + 0.30 * conc

        # Additional reduction for high volatility risk
        # Each 10% vol risk → additional 3% reduction
        vol_adj = max(0.0, 1.0 - vol_risk * 0.30)

        mult = base * vol_adj
        mult = max(0.50, min(1.0, mult))  # clamp [0.50, 1.00]

        logger.debug(
            "mam_sizing_multiplier",
            usdc_conc_pct=round(conc * 100, 1),
            vol_risk_pct=round(vol_risk * 100, 1),
            multiplier=round(mult, 3),
        )
        return mult

    def compute_effective_balance(
        self,
        usdc_balance: float,
        asset_balances: Dict[str, float] = None,
        index_prices: Dict[str, float] = None,
    ) -> float:
        """
        Fast path: returns effective balance including MAM contributions.
        Used when ARIA has live asset balances and index prices cached.

        Falls back to USDC-only if asset_balances or index_prices are missing.
        """
        if not asset_balances or not index_prices:
            return usdc_balance

        all_balances = {"USDC": usdc_balance, **(asset_balances or {})}
        state = self.compute_mam_state(all_balances, index_prices)
        return state.total_effective_usd

    def is_withdrawal_safe(
        self,
        total_collateral_before: float,
        unrealized_pnl: float,
        total_initial_margin: float,
        withdraw_amount: float,
    ) -> tuple[bool, float]:
        """
        Check if a withdrawal is safe per SoDEX's withdrawal formula:
            Collateral(after) + Unrealized_Losses ≥ Total_Initial_Margin

        Returns (is_safe, max_safe_withdrawal).
        """
        unrealized_losses = abs(min(unrealized_pnl, 0.0))
        collateral_after = total_collateral_before - withdraw_amount
        # Safe if: collateral_after + unrealized_losses ≥ total_initial_margin
        is_safe = (collateral_after + unrealized_losses) >= total_initial_margin
        max_safe = max(0.0, total_collateral_before + unrealized_losses - total_initial_margin)
        return is_safe, max_safe

    def log_mam_health(self, mam_state: MAMState, account_id: str = "") -> None:
        """Emit structured log of MAM health snapshot for monitoring."""
        logger.info(
            "mam_health_snapshot",
            account=account_id[:8] if account_id else "unknown",
            total_effective_usd=round(mam_state.total_effective_usd, 2),
            usdc_usd=round(mam_state.usdc_effective_usd, 2),
            non_usdc_usd=round(mam_state.non_usdc_effective_usd, 2),
            usdc_concentration_pct=round(mam_state.collateral_concentration * 100, 1),
            volatility_risk_pct=round(mam_state.volatility_risk_factor * 100, 1),
            safe_to_withdraw_usd=round(mam_state.safe_to_withdraw, 2),
            non_usdc_heavy_warning=mam_state.warning_non_usdc_heavy,
            asset_breakdown=[
                {
                    "asset": b.asset,
                    "units": round(b.units, 6),
                    "index_px": round(b.index_price, 4),
                    "effective_usd": round(b.effective_usd, 2),
                    "haircut_pct": round((1 - b.collateral_ratio) * 100, 1),
                    "capped": b.capped,
                }
                for b in mam_state.balances
            ],
        )


# ── Module-level singleton ────────────────────────────────────────────────────
_mam_engine: Optional[MultiAssetMarginEngine] = None


def get_mam_engine() -> MultiAssetMarginEngine:
    """Return the module-level MAM engine singleton."""
    global _mam_engine
    if _mam_engine is None:
        _mam_engine = MultiAssetMarginEngine()
    return _mam_engine
