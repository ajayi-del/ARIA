"""
sovereign/portfolio.py — SSI position accounting and rebalance order generation.

Architecture
────────────
SovereignPortfolio is the data layer: it knows what we hold, what we target,
and what orders are needed to close the gap. It does NOT execute.

Positions are initialised from env vars at startup; current USD values are
refreshed each cycle from signal_price_stores (live SSI spot prices).

Rebalance orders follow hard rules from sovereign/__init__.py:
  - Sells before buys
  - MEME exits first (lowest priority sell = first to execute)
  - MEME enters last (lowest priority buy = last to execute)
"""

from __future__ import annotations

import os
import math
import structlog
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = structlog.get_logger(__name__)

# ── SSI token registry ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SSITokenMeta:
    symbol:       str    # ARIA symbol e.g. "MAG7SSI-USD"
    display:      str    # Short display name e.g. "MAG7.ssi"
    spot_symbol:  str    # SoDEX spot trading symbol e.g. "MAG7SSI_USDC"
    spot_step:    float  # Minimum order quantity step
    spot_tick:    float  # Price tick size

SSI_TOKENS: Dict[str, SSITokenMeta] = {
    "MAG7SSI-USD": SSITokenMeta(
        symbol="MAG7SSI-USD", display="MAG7.ssi",
        spot_symbol="MAG7SSI_USDC", spot_step=0.01, spot_tick=0.0001,
    ),
    "DEFISSI-USD": SSITokenMeta(
        symbol="DEFISSI-USD", display="DEFI.ssi",
        spot_symbol="DEFISSI_USDC", spot_step=0.01, spot_tick=0.0001,
    ),
    "MEMESSI-USD": SSITokenMeta(
        symbol="MEMESSI-USD", display="MEME.ssi",
        spot_symbol="MEMESSI_USDC", spot_step=0.01, spot_tick=0.0001,
    ),
    "USSI-USD": SSITokenMeta(
        symbol="USSI-USD", display="USSI",
        spot_symbol="USSI_USDC", spot_step=0.01, spot_tick=0.0001,
    ),
}

# MEME rebalance priority — determines order within sell/buy pass
# Higher value = executed later in its pass
REBALANCE_PRIORITY: Dict[str, int] = {
    "MAG7SSI-USD": 2,
    "DEFISSI-USD": 2,
    "USSI-USD":    2,
    "MEMESSI-USD": 9,  # sells first (priority 9 = sorted last = sent first in sell pass)
                       # buys last  (priority 9 = sorted last = sent last in buy pass)
}

# Correlation matrix for portfolio VaR — ρ between SSI basket returns
# MAG7/DEFI ρ=0.85, MAG7/MEME ρ=0.75, DEFI/MEME ρ=0.80, all vs USSI ρ=0.02
_CORR: Dict[tuple, float] = {
    ("MAG7SSI-USD", "DEFISSI-USD"): 0.85,
    ("MAG7SSI-USD", "MEMESSI-USD"): 0.75,
    ("MAG7SSI-USD", "USSI-USD"):    0.02,
    ("DEFISSI-USD", "MEMESSI-USD"): 0.80,
    ("DEFISSI-USD", "USSI-USD"):    0.02,
    ("MEMESSI-USD", "USSI-USD"):    0.02,
}

# Individual daily volatility estimates (σ_daily)
_DAILY_VOL: Dict[str, float] = {
    "MAG7SSI-USD": 0.025,   # 2.5%/day — tech index
    "DEFISSI-USD": 0.035,   # 3.5%/day — DeFi more volatile
    "MEMESSI-USD": 0.055,   # 5.5%/day — meme most volatile
    "USSI-USD":    0.008,   # 0.8%/day — broad equity index, stable
}

# Daily holding cost rate: 0.01% of notional/day
HOLDING_COST_DAILY_RATE: float = 0.0001

# Minimum rebalance threshold — don't trade if drift is below this
MIN_REBALANCE_DRIFT: float = 0.02   # 2% weight drift before we act


@dataclass
class SSIPosition:
    """Live position record for one SSI token."""
    symbol:         str
    quantity:       float    # Token quantity held
    entry_usd:      float    # USD value at entry (historical cost basis)
    current_price:  float    # Latest price from signal_price_stores
    current_usd:    float    # current_price × quantity
    target_weight:  float    # 0.0–1.0 (set by rotation engine)
    actual_weight:  float    # current_usd / total_portfolio_usd
    drift_1h:       float    # 1h price drift from signal_price_stores


@dataclass
class RebalanceOrder:
    """Single spot order required to reach target allocation."""
    symbol:      str
    side:        str    # "buy" or "sell"
    quantity:    float
    notional_usd: float
    priority:    int    # Lower = execute sooner within its pass (sell pass / buy pass)
    reason:      str


class SovereignPortfolio:
    """
    SSI position accounting and rebalance order generation.

    Positions are loaded from env vars at init. Current values are refreshed
    by calling update_prices() with live signal_price_stores data each cycle.
    """

    def __init__(self, config) -> None:
        self.config = config
        self.positions: Dict[str, SSIPosition] = {}
        self._total_capital: float = float(os.getenv("SOVEREIGN_CAPITAL", "500.0"))
        self._load_from_env()

    def _load_from_env(self) -> None:
        """
        Initialise positions from env vars.

        Expected env vars (all optional, default 0):
          SOVEREIGN_MAG7_QTY   — MAG7.ssi token quantity held
          SOVEREIGN_MAG7_ENTRY — USD cost basis at entry
          SOVEREIGN_DEFI_QTY
          SOVEREIGN_DEFI_ENTRY
          SOVEREIGN_MEME_QTY
          SOVEREIGN_MEME_ENTRY
          SOVEREIGN_USSI_QTY
          SOVEREIGN_USSI_ENTRY

        SLP vault (MAG7.ssi) is loaded via SLP_VAULT_SMAG7_DEPOSITED as the
        definitive quantity — SOVEREIGN_MAG7_QTY is a redundant override.
        """
        qty_keys   = {
            "MAG7SSI-USD": ("SOVEREIGN_MAG7_QTY",   "SLP_VAULT_SMAG7_DEPOSITED"),
            "DEFISSI-USD": ("SOVEREIGN_DEFI_QTY",   None),
            "MEMESSI-USD": ("SOVEREIGN_MEME_QTY",   None),
            "USSI-USD":    ("SOVEREIGN_USSI_QTY",   None),
        }
        entry_keys = {
            "MAG7SSI-USD": "SOVEREIGN_MAG7_ENTRY",
            "DEFISSI-USD": "SOVEREIGN_DEFI_ENTRY",
            "MEMESSI-USD": "SOVEREIGN_MEME_ENTRY",
            "USSI-USD":    "SOVEREIGN_USSI_ENTRY",
        }

        for sym in SSI_TOKENS:
            primary_key, fallback_key = qty_keys[sym]
            qty = float(os.getenv(primary_key, "0"))
            if qty == 0 and fallback_key:
                qty = float(os.getenv(fallback_key, "0"))

            entry_usd = float(os.getenv(entry_keys[sym], "0"))

            self.positions[sym] = SSIPosition(
                symbol=sym, quantity=qty, entry_usd=entry_usd,
                current_price=0.0, current_usd=entry_usd,
                target_weight=0.0, actual_weight=0.0, drift_1h=0.0,
            )

        log.info(
            "sovereign_portfolio_loaded",
            mag7_qty=self.positions["MAG7SSI-USD"].quantity,
            defi_qty=self.positions["DEFISSI-USD"].quantity,
            meme_qty=self.positions["MEMESSI-USD"].quantity,
            ussi_qty=self.positions["USSI-USD"].quantity,
            total_capital=self._total_capital,
        )

    def update_prices(self, signal_price_stores: dict) -> None:
        """
        Refresh position values from live signal_price_stores.

        signal_price_stores format: {symbol: {"price": float, "drift_1h": float, "ts_ms": int}}
        """
        total_usd = 0.0
        for sym, pos in self.positions.items():
            store = signal_price_stores.get(sym, {})
            price = float(store.get("price", 0.0) or 0.0)
            drift = float(store.get("drift_1h", 0.0) or 0.0)

            if price > 0 and pos.quantity > 0:
                pos.current_price = price
                pos.current_usd   = pos.quantity * price
                pos.drift_1h      = drift
            elif pos.entry_usd > 0:
                # No live price yet — use cost basis
                pos.current_usd = pos.entry_usd

            total_usd += pos.current_usd

        # Recompute actual weights
        if total_usd > 0:
            for pos in self.positions.values():
                pos.actual_weight = pos.current_usd / total_usd

        self._total_capital = max(total_usd, self._total_capital)

    def set_target_weights(self, weights: Dict[str, float]) -> None:
        """Apply target allocation weights from rotation engine."""
        for sym, w in weights.items():
            if sym in self.positions:
                self.positions[sym].target_weight = w

    def total_value_usd(self) -> float:
        return sum(p.current_usd for p in self.positions.values())

    def compute_var(self) -> float:
        """
        Portfolio 1-day 95% VaR via variance-covariance method.

        VaR = 1.645 × σ_portfolio (95% one-tailed normal)
        σ_portfolio² = Σᵢ Σⱼ wᵢ wⱼ σᵢ σⱼ ρᵢⱼ

        Falls back to projected VaR when no live positions exist:
        uses target weights if set, otherwise equal-weight across all 4 tokens.
        """
        syms  = [s for s in SSI_TOKENS if self.positions[s].current_usd > 0]
        total = self.total_value_usd()

        if total > 0 and len(syms) >= 2:
            # Live portfolio: use actual weights
            weights = {s: self.positions[s].actual_weight for s in syms}
            active  = syms
        else:
            # Projected portfolio: target weights or equal-weight fallback
            active = [s for s in SSI_TOKENS if self.positions[s].target_weight > 0]
            if len(active) >= 2:
                weights = {s: self.positions[s].target_weight for s in active}
            else:
                # No targets set yet — equal-weight all 4 tokens
                active  = list(SSI_TOKENS.keys())
                weights = {s: 0.25 for s in active}

        var_sq = 0.0
        for a in active:
            wa = weights[a]
            sa = _DAILY_VOL.get(a, 0.03)
            for b in active:
                wb = weights[b]
                sb = _DAILY_VOL.get(b, 0.03)
                rho = _CORR.get((a, b), _CORR.get((b, a), 1.0 if a == b else 0.3))
                var_sq += wa * wb * sa * sb * rho

        sigma = math.sqrt(max(var_sq, 0.0))
        return sigma * 1.645  # 95th percentile

    def compute_holding_cost_per_day(self) -> float:
        """Daily holding cost in USD (0.01% of total notional)."""
        return self.total_value_usd() * HOLDING_COST_DAILY_RATE

    def get_rebalance_orders(self) -> List[RebalanceOrder]:
        """
        Generate the minimum set of spot orders to hit target weights.

        Rules:
          - Only generate orders if drift exceeds MIN_REBALANCE_DRIFT
          - Sell orders have priority 1; buy orders have priority 2
            (caller sorts and executes sell pass before buy pass)
          - Within each pass, MEME has priority 9 (executed last in sell pass
            = sold first since caller reverses, executed last in buy pass)

        Wait — let me be explicit:
          SELL PASS: sort ascending by priority → MEME (9) last in list = reversed = first executed
          BUY PASS:  sort ascending by priority → MEME (9) last in list = last executed

        Actually simpler: caller receives two separate lists from us:
          sells = [order for order in orders if order.side == "sell"],
            sorted by priority DESCENDING so MEME (9) is first
          buys  = [order for order in orders if order.side == "buy"],
            sorted by priority ASCENDING so MEME (9) is last
        """
        orders: List[RebalanceOrder] = []
        total_usd = self.total_value_usd()
        if total_usd <= 0:
            return orders

        for sym, pos in self.positions.items():
            target_w = pos.target_weight
            actual_w = pos.actual_weight

            drift = abs(target_w - actual_w)
            if drift < MIN_REBALANCE_DRIFT:
                continue

            target_usd = total_usd * target_w
            delta_usd  = target_usd - pos.current_usd

            if delta_usd < 0:
                # Need to sell
                sell_usd = abs(delta_usd)
                price    = pos.current_price if pos.current_price > 0 else 1.0
                qty      = sell_usd / price
                qty      = _floor_to_step(qty, SSI_TOKENS[sym].spot_step)
                if qty > 0:
                    orders.append(RebalanceOrder(
                        symbol=sym, side="sell", quantity=qty,
                        notional_usd=qty * price,
                        priority=REBALANCE_PRIORITY[sym],
                        reason=f"overweight {actual_w:.1%} → {target_w:.1%}",
                    ))
            else:
                # Need to buy
                buy_usd = delta_usd
                price   = pos.current_price if pos.current_price > 0 else 1.0
                qty     = buy_usd / price
                qty     = _floor_to_step(qty, SSI_TOKENS[sym].spot_step)
                if qty > 0:
                    orders.append(RebalanceOrder(
                        symbol=sym, side="buy", quantity=qty,
                        notional_usd=qty * price,
                        priority=REBALANCE_PRIORITY[sym],
                        reason=f"underweight {actual_w:.1%} → {target_w:.1%}",
                    ))

        return orders

    def get_display_snapshot(self) -> dict:
        """Return dict for terminal panel rendering."""
        return {
            "positions": {sym: {
                "display":       SSI_TOKENS[sym].display,
                "quantity":      pos.quantity,
                "current_usd":   round(pos.current_usd, 2),
                "target_weight": pos.target_weight,
                "actual_weight": pos.actual_weight,
                "drift_1h":      pos.drift_1h,
                "current_price": pos.current_price,
            } for sym, pos in self.positions.items()},
            "total_usd":         round(self.total_value_usd(), 2),
            "var_pct":           round(self.compute_var() * 100, 1),
            "holding_cost_usd":  round(self.compute_holding_cost_per_day(), 4),
        }


def _floor_to_step(qty: float, step: float) -> float:
    """Floor quantity to nearest step size."""
    if step <= 0:
        return qty
    return math.floor(qty / step) * step
