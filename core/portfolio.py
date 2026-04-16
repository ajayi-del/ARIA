"""
core/portfolio.py — Portfolio value calculator including staked assets.

READ-ONLY — does not affect trade execution in any way.
Purpose: include staked MAG7 (sMAG7.ssi) value in:
  1. Fee tier calculations (larger portfolio → potential Tier 1 access)
  2. SOVEREIGN personality hedge sizing (stake_balance field)
  3. Terminal display of total portfolio value

Architecture:
  Staked balances are hardcoded in config/staked_balances.json.
  No API calls are made — staking balance does not change during a session.
  Price of MAG7-USD is injected from the live price feed (WebSocket data).
  Fail-safe: if config missing or corrupt, returns 0.0 and logs a warning.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Optional

log = logging.getLogger(__name__)

_CONFIG_PATH = Path("config/staked_balances.json")


class PortfolioValue:
    """
    Tracks total portfolio value including staked assets.

    Integrates with:
      - SoDEXFeeEngine.update() for fee tier calculation
      - PersonalityContextCache.update_sovereign() for SOVEREIGN hedge sizing
      - Terminal dashboard for portfolio display

    Usage:
        pv = PortfolioValue()
        # On price update:
        staked_usd = pv.get_staked_value_usd({"MAG7": 0.5118})
        # For fee engine:
        total = pv.total_value_usd(api_balance=500.0, prices={"MAG7": 0.5118})
    """

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self._config_path = config_path or _CONFIG_PATH
        self._staked_amounts: Dict[str, float] = {}    # symbol → token amount
        self._staked_usd_estimates: Dict[str, float] = {}  # symbol → USD at last update
        self._load_staked_config()

    # ── Config loading ─────────────────────────────────────────────────────────

    def _load_staked_config(self) -> None:
        """Load hardcoded staked amounts. Fail-safe: logs warning, returns empty."""
        if not self._config_path.exists():
            log.warning("staked_config_not_found",
                        extra={"path": str(self._config_path),
                               "impact": "staked assets not included in portfolio value"})
            return

        try:
            with open(self._config_path) as f:
                config = json.load(f)

            for symbol, data in config.get("staked_assets", {}).items():
                amount = float(data["amount"])
                self._staked_amounts[symbol] = amount
                # Use last known USD value as seed until price feed updates
                self._staked_usd_estimates[symbol] = float(
                    data.get("usd_value_at_entry", 0.0)
                )
                log.info("staked_balance_loaded",
                         extra={"symbol": symbol, "amount": amount,
                                "source": str(self._config_path)})

        except Exception as e:
            log.error("staked_config_load_failed",
                      extra={"error": str(e), "path": str(self._config_path)})

    # ── Accessors ──────────────────────────────────────────────────────────────

    def get_staked_amount(self, symbol: str) -> float:
        """Token amount staked for symbol. 0.0 if not found."""
        return self._staked_amounts.get(symbol, 0.0)

    def get_staked_value_usd(self, prices: Dict[str, float]) -> float:
        """
        USD value of all staked assets given current prices.

        Parameters
        ----------
        prices : {symbol: price_in_usd}
            e.g. {"MAG7": 0.5118}  — MAG7 token price in USDC/USD
        """
        total = 0.0
        for symbol, amount in self._staked_amounts.items():
            price = prices.get(symbol, 0.0)
            if price > 0:
                usd_val = amount * price
                self._staked_usd_estimates[symbol] = usd_val  # update estimate
                total += usd_val
            else:
                # Fall back to last known estimate if price not available yet
                total += self._staked_usd_estimates.get(symbol, 0.0)
        return total

    def get_mag7_stake_usd(self, mag7_price: float = 0.0) -> float:
        """
        Convenience: USD value of MAG7 stake only.
        Used by SOVEREIGN personality to set stake_balance in context.
        Returns last estimate if price is 0.
        """
        amount = self._staked_amounts.get("MAG7", 0.0)
        if mag7_price > 0:
            val = amount * mag7_price
            self._staked_usd_estimates["MAG7"] = val
            return val
        return self._staked_usd_estimates.get("MAG7", 0.0)

    def total_value_usd(self, api_balance: float, prices: Dict[str, float]) -> float:
        """
        Total portfolio USD value = API balance (USDC + open positions) + staked assets.
        Used by fee engine for tier calculation.
        """
        staked = self.get_staked_value_usd(prices)
        total = api_balance + staked
        log.debug("portfolio_value_calculated",
                  extra={"api_balance": round(api_balance, 2),
                         "staked_value": round(staked, 2),
                         "total": round(total, 2)})
        return total

    def snapshot(self) -> Dict:
        """Diagnostic snapshot for terminal display."""
        return {
            "staked": {sym: {"amount": amt, "usd": self._staked_usd_estimates.get(sym, 0.0)}
                       for sym, amt in self._staked_amounts.items()},
            "total_staked_usd": sum(self._staked_usd_estimates.values()),
        }
