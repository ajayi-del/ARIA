"""
Margin Engine

Computes liquidation prices and validates stop placement for all four assets.
"""

from typing import Tuple


class MarginEngine:
    """
    Computes liquidation prices and validates stop placement.
    """
    
    # MARGIN TIERS — hardcoded from SoDEX docs
    TIERS = {
        "BTC-USD": [
            {"max_notional": 4_000_000, "max_leverage": 25, "mmr": 0.02},     # maintenance margin rate
            {"max_notional": float('inf'), "max_leverage": 10, "mmr": 0.05}
        ],
        "ETH-USD": [
            {"max_notional": 4_000_000, "max_leverage": 20, "mmr": 0.025},
            {"max_notional": float('inf'), "max_leverage": 10, "mmr": 0.05}
        ],
        "SOL-USD": [
            {"max_notional": 4_000_000, "max_leverage": 20, "mmr": 0.025},
            {"max_notional": float('inf'), "max_leverage": 10, "mmr": 0.05}
        ],
        "XAUT-USD": [
            {"max_notional": 4_000_000, "max_leverage": 25, "mmr": 0.02},
            {"max_notional": float('inf'), "max_leverage": 10, "mmr": 0.05}
        ],
        "BNB-USD": [
            {"max_notional": 1_000_000, "max_leverage": 20, "mmr": 0.025},
            {"max_notional": float('inf'), "max_leverage": 10, "mmr": 0.05}
        ],
        "LINK-USD": [
            {"max_notional": 1_000_000, "max_leverage": 20, "mmr": 0.025},
            {"max_notional": float('inf'), "max_leverage": 10, "mmr": 0.05}
        ],
        "AVAX-USD": [
            {"max_notional": 1_000_000, "max_leverage": 20, "mmr": 0.025},
            {"max_notional": float('inf'), "max_leverage": 10, "mmr": 0.05}
        ],
        "USTECH-USD": [
            {"max_notional": 2_000_000, "max_leverage": 20, "mmr": 0.025},
            {"max_notional": float('inf'), "max_leverage": 10, "mmr": 0.05}
        ],
        "USTECH100-USD": [
            {"max_notional": 2_000_000, "max_leverage": 10, "mmr": 0.05},
            {"max_notional": float('inf'), "max_leverage": 5,  "mmr": 0.10}
        ]
    }
    # Default tier used when a symbol is not in TIERS (safe fallback)
    _DEFAULT_TIER = {"max_notional": float('inf'), "max_leverage": 10, "mmr": 0.05}
    
    def get_tier(self, symbol: str, notional: float) -> dict:
        """Returns matching tier for notional size. Falls back to _DEFAULT_TIER for unknown symbols."""
        tiers = self.TIERS.get(symbol, [])
        for tier in tiers:
            if notional <= tier["max_notional"]:
                return tier
        return tiers[-1] if tiers else self._DEFAULT_TIER
    
    def compute_liquidation_price(
        self,
        symbol: str,
        entry_price: float,
        side: int,        # 1=long, -1=short
        leverage: int,
        size: float
    ) -> float:
        """
        Formula from SoDEX docs:
        margin_available = (size * entry_price) / leverage
        l = 1 / maintenance_leverage
        liq_price = entry_price - side * (
            margin_available / (size * (1 - l * side))
        )
        """
        tier = self.get_tier(symbol, size * entry_price)
        maintenance_leverage = 1 / tier["mmr"]
        
        margin_available = (size * entry_price) / leverage
        l = 1 / maintenance_leverage
        liq_price = entry_price - side * (
            margin_available / (size * (1 - l * side))
        )
        
        return liq_price
    def stop_is_safe(
        self,
        entry_price: float,
        stop_price: float,
        side: int,
        leverage: int,
        symbol: str,
        size: float,
        atr_ratio: float = 1.0
    ) -> Tuple[bool, str]:
        """
        Validates stop price is above liquidation with dynamic buffer.
        """
        # Minimum 3% buffer between stop and liquidation — institutional floor.
        # Old value was 0.3–0.6% (base_buffer=0.003), which is within normal bid/ask
        # noise at 10–20× leverage: LINK can gap 2% in seconds, meaning stop=liq+0.6%
        # was effectively the same as no buffer at all.
        # 3% buffer = 15% margin loss at 5×, 30% at 10× — this is the real protection zone.
        base_buffer = 0.03   # 3% minimum between stop and liquidation price
        dynamic_buffer = base_buffer * max(1.0, atr_ratio)

        liq = self.compute_liquidation_price(symbol, entry_price, side, leverage, size)

        # For long (side=1):
        if side == 1:
            safe = stop_price > liq * (1 + dynamic_buffer)
        # For short (side=-1):
        else:
            safe = stop_price < liq * (1 - dynamic_buffer)

        if safe:
            return True, f"safe: liq={liq:.2f}"
        else:
            return False, f"UNSAFE: stop={stop_price:.2f} liq={liq:.2f}"
    
    def compute_size(
        self,
        account_balance: float,
        risk_pct: float,
        entry_price: float,
        stop_price: float,
        leverage: int,
        symbol: str,
        atr_ratio: float = 1.0,
        min_notional_usd: float = 200.0,
    ) -> Tuple[float, float, int]:
        """
        Returns (size, initial_margin, safe_leverage).

        Raises ValueError if notional is below min_notional_usd.
        Notional is capped dynamically at 90% of account equity × leverage —
        no static dollar ceiling so sizing scales with the account.
        """
        risk_amount = account_balance * risk_pct
        risk_per_unit = abs(entry_price - stop_price)

        if risk_per_unit == 0:
            raise ValueError("Entry and stop prices cannot be equal")

        size = risk_amount / risk_per_unit
        notional = size * entry_price

        # Minimum notional guard — prevents dust trades
        if notional < min_notional_usd:
            raise ValueError(
                f"Trade notional ${notional:.2f} below minimum ${min_notional_usd:.2f} "
                f"(balance=${account_balance:.2f}, risk_pct={risk_pct})"
            )

        # Dynamic cap: margin can never exceed 90% of balance — scales with account.
        # Eliminates static dollar ceiling so $250 account gets proportionally larger
        # trades than $99 without needing a config change.
        equity_cap = account_balance * leverage * 0.90
        if notional > equity_cap:
            notional = equity_cap
            size = notional / entry_price

        tier = self.get_tier(symbol, notional)
        safe_leverage = min(leverage, tier["max_leverage"])
        initial_margin = notional / safe_leverage

        # Final guard: if tier reduced leverage, initial_margin may still exceed balance.
        # Clamp to 90% of balance (same safety buffer as equity_cap above).
        max_allowed_margin = account_balance * 0.90
        if initial_margin > max_allowed_margin:
            initial_margin = max_allowed_margin
            notional = initial_margin * safe_leverage
            size = notional / entry_price

        # Validate stop safety with dynamic buffer
        safe, reason = self.stop_is_safe(
            entry_price,
            stop_price,
            1 if stop_price < entry_price else -1,
            safe_leverage,
            symbol,
            size,
            atr_ratio=atr_ratio
        )

        if not safe:
            # Try reducing leverage from current down to 1 — use capped notional
            for test_leverage in range(safe_leverage, 0, -1):
                test_notional = min(notional, account_balance * test_leverage * 0.90)
                test_size = test_notional / entry_price
                test_margin = test_notional / test_leverage
                safe, _ = self.stop_is_safe(
                    entry_price,
                    stop_price,
                    1 if stop_price < entry_price else -1,
                    test_leverage,
                    symbol,
                    test_size,
                    atr_ratio=atr_ratio
                )
                if safe:
                    return test_size, test_margin, test_leverage

            raise ValueError(f"Stop too tight for any leverage: {reason}")

        return (size, initial_margin, safe_leverage)
