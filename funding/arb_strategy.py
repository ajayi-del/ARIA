import time
import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional
import structlog
from core.config import Settings
from risk.position_manager import PositionManager
from funding.radar import FundingRadar, FundingSnapshot
from funding.history import FundingHistory
from execution.schemas import TradeCandidate, Position

logger = structlog.get_logger(__name__)

# ── Minimum hold time for true delta-neutral arb (collect ≥1 funding payment) ──
_TRUE_ARB_MIN_HOLD_S = 8 * 3600   # 8 hours


@dataclass
class TrueArbPosition:
    """Open delta-neutral position: long spot + short perp (or inverse)."""
    symbol: str                     # e.g. "BTC-USD"
    spot_symbol: str                # e.g. "vBTC_vUSDC"
    direction: str                  # "long_spot_short_perp" | "short_spot_long_perp"
    spot_qty: float                 # asset quantity on spot leg
    perp_qty: float                 # contracts on perp leg
    spot_entry: float               # spot leg fill price (USD)
    perp_entry: float               # perp leg fill price (USD)
    opening_basis: float            # perp_entry - spot_entry at open
    spot_cl_ord_id: str             # spot order ID for tracking
    perp_order_id: str              # perp order ID for tracking
    opened_at: float = field(default_factory=time.time)
    funding_collected_usd: float = 0.0
    last_funding_ts: float = 0.0


class TrueDeltaNeutralArb:
    """
    SoDEX spot-perp delta-neutral arbitrage.

    Entry logic:
      • funding > +threshold  → Long spot, Short perp (collect positive funding)
      • funding < -threshold  → Short spot, Long perp (collect negative funding)

    Execution order (CRITICAL — never reverse):
      1. Place spot order first.  If it fails → abort (no perp).
      2. Place perp order.  If it fails → emergency close spot leg immediately.

    Exit logic:
      • Minimum hold: 8 hours (collect ≥1 funding payment before exit)
      • Basis convergence: abs(current_basis) <= abs(opening_basis) * 0.1
      • Rate flipped: funding sign reversed → exit both legs
      • Max hold: 72 hours

    All legs placed as LIMIT orders at best bid/ask for tightest fills.
    """

    # Minimum funding rate to enter (absolute value)
    MIN_FUNDING_RATE = 0.0002      # 0.02% per 8h = ~0.9% APR floor

    def __init__(
        self,
        config: Settings,
        perp_client,          # SoDEXClient
        spot_client,          # SoDEXSpotClient
        funding_radar: FundingRadar,
        fee_engine=None,      # SoDEXFeeEngine (optional — gate 0 if provided)
        calendar_engine=None, # CalendarEngine (optional — gates arb during macro events)
    ):
        self.config = config
        self.perp_client = perp_client
        self.spot_client = spot_client
        self.funding_radar = funding_radar
        self.fee_engine = fee_engine
        self.calendar_engine = calendar_engine
        self._open_positions: Dict[str, TrueArbPosition] = {}

        # Symbol → perp symbol_id mapping (set by caller after startup discovery)
        self._symbol_ids: Dict[str, int] = {}
        self._account_id: int = 0
        self._api_key_name: str = ""

    def set_symbol_ids(self, symbol_ids: Dict[str, int], account_id: int) -> None:
        self._symbol_ids = symbol_ids
        self._account_id = account_id

    def get_open_positions(self) -> List[TrueArbPosition]:
        return list(self._open_positions.values())

    def get_summary(self) -> Dict[str, Any]:
        total_funding = sum(p.funding_collected_usd for p in self._open_positions.values())
        return {
            "active": len(self._open_positions),
            "total_funding_collected": round(total_funding, 4),
            "symbols": list(self._open_positions.keys()),
        }

    async def evaluate_and_open(
        self,
        symbol: str,
        funding_rate: float,
        balance: float,
        cascade_active: bool = False,
    ) -> bool:
        """
        Check conditions and open a true arb position if warranted.

        Args:
            symbol: e.g. "BTC-USD"
            funding_rate: current 8h funding rate as a fraction (e.g. 0.001 = 0.1%)
            balance: current account balance in USD
            cascade_active: True → skip (liquidation cascade in progress)

        Returns True if a position was opened.
        """
        if cascade_active:
            logger.debug("true_arb_cascade_skip", symbol=symbol)
            return False

        # Gate -1: Calendar check — BLOCK suppresses all arb; CAUTION halves position
        _cal_mult = 1.0
        if self.calendar_engine is not None:
            try:
                _cal_state = await self.calendar_engine.get_state(symbol)
                if _cal_state.regime == "BLOCK":
                    logger.info(
                        "true_arb_calendar_blocked",
                        symbol=symbol,
                        reason=_cal_state.reason,
                    )
                    return False
                _cal_mult = max(0.5, _cal_state.size_multiplier)  # CAUTION: floor at 0.5×
                if _cal_mult < 1.0:
                    logger.info(
                        "true_arb_calendar_caution",
                        symbol=symbol,
                        cal_mult=round(_cal_mult, 2),
                        reason=_cal_state.reason,
                    )
            except Exception as _cal_err:
                logger.warning("true_arb_calendar_error", error=str(_cal_err))
                # Calendar unavailable — proceed at full size

        # Gate 0: Fee viability — must clear round-trip cost before anything else.
        # This is the first gate because it's O(1) and filters most low-rate opportunities.
        if self.fee_engine is not None:
            if not self.fee_engine.is_arb_viable(
                funding_rate=funding_rate,
                periods=3,          # expect to collect 3× 8h payments (24h hold)
                use_maker=True,     # assume maker fill via place_maker_first()
                safety_margin=1.5,  # must be 50% above break-even
            ):
                logger.debug(
                    "true_arb_fee_gate_blocked",
                    symbol=symbol,
                    funding_rate=f"{funding_rate*100:.4f}%",
                    break_even=f"{self.fee_engine.arb_break_even_funding(3, True)*100:.4f}%",
                )
                return False

        if abs(funding_rate) < self.MIN_FUNDING_RATE:
            return False

        if symbol in self._open_positions:
            return False   # Already have a position for this symbol

        # ── Delta-neutral arb capital math ────────────────────────────────────
        # Spot leg:  buy arb_cap USD of asset  → commits arb_cap USDC (no margin/liquidation risk)
        # Perp leg:  short same qty contracts  → perp margin = arb_cap / leverage
        # For perp margin ≥ $20 at 10x leverage: arb_cap ≥ min_trade_notional_usd ($200)
        # Total capital deployed: arb_cap (spot) + arb_cap/lev (perp margin) = arb_cap × (1 + 1/lev)
        # Effective leverage of combined position: lev/(lev+1) < 1 → fully sub-leveraged, minimal liq risk
        lev = self.config.default_leverage
        min_notional = self.config.min_trade_notional_usd
        arb_cap = balance * self.config.arb_capital_pct * _cal_mult  # calendar scales capital
        if arb_cap < min_notional:
            logger.debug(
                "true_arb_below_minimum",
                symbol=symbol,
                arb_cap=round(arb_cap, 2),
                min_notional=min_notional,
                perp_margin_if_opened=round(arb_cap / max(lev, 1), 2),
                note=f"need arb_cap≥${min_notional:.0f} for perp_margin≥${min_notional/max(lev,1):.0f} at {lev}x",
            )
            return False

        # Get spot price for sizing
        spot_bid, spot_ask = await self.spot_client.get_spot_bid_ask(symbol)
        if spot_bid <= 0 or spot_ask <= 0:
            logger.warning("true_arb_no_spot_price", symbol=symbol)
            return False

        # Determine direction
        if funding_rate > 0:
            direction = "long_spot_short_perp"
            spot_side = "buy"
            perp_side = "short"
            spot_price = spot_ask   # taker buy
        else:
            direction = "short_spot_long_perp"
            spot_side = "sell"
            perp_side = "long"
            spot_price = spot_bid   # taker sell

        qty = arb_cap / spot_price
        if qty * spot_price < min_notional:   # float precision guard
            return False

        # ── Step 1: Spot leg (MUST succeed before perp) ──────────────────────
        logger.info("true_arb_opening",
                    symbol=symbol, direction=direction,
                    spot_price=spot_price, qty=round(qty, 6),
                    funding_rate=f"{funding_rate*100:.4f}%")

        spot_result = await self.spot_client.place_spot_order(
            perp_symbol=symbol,
            side=spot_side,
            quantity=qty,
            price=spot_price,
        )
        if spot_result.get("code", -1) != 0:
            logger.error("true_arb_spot_leg_failed",
                         symbol=symbol, error=spot_result.get("error"),
                         action="aborting — no perp placed")
            return False

        spot_cl_ord_id = spot_result.get("cl_ord_id", "")

        # ── Step 2: Perp leg ──────────────────────────────────────────────────
        symbol_id = self._symbol_ids.get(symbol, 0)
        if symbol_id == 0:
            # Cannot place perp — emergency close spot
            logger.error("true_arb_no_symbol_id",
                         symbol=symbol, action="emergency_close_spot")
            await self._emergency_close_spot(symbol, spot_cl_ord_id, spot_side, qty, spot_price)
            return False

        perp_result = await self.perp_client.place_order_simple(
            symbol=symbol,
            side=perp_side,
            contracts=qty,
            price=spot_price,    # use spot price as reference; perp will re-price
            symbol_id=symbol_id,
            account_id=self._account_id,
        )
        if not perp_result.success:
            # Perp failed → emergency close spot leg immediately
            logger.error("true_arb_perp_leg_failed",
                         symbol=symbol, error=perp_result.error,
                         action="emergency_close_spot")
            await self._emergency_close_spot(symbol, spot_cl_ord_id, spot_side, qty, spot_price)
            return False

        perp_entry = spot_price   # approximate; real fill tracked via perp poll
        basis = perp_entry - spot_price  # opening basis (≈0 at market prices)

        from execution.sodex_spot_client import PERP_TO_SPOT
        pos = TrueArbPosition(
            symbol=symbol,
            spot_symbol=PERP_TO_SPOT.get(symbol, ""),
            direction=direction,
            spot_qty=qty,
            perp_qty=qty,
            spot_entry=spot_price,
            perp_entry=perp_entry,
            opening_basis=basis,
            spot_cl_ord_id=spot_cl_ord_id,
            perp_order_id=perp_result.order_id or "",
            opened_at=time.time(),
        )
        self._open_positions[symbol] = pos

        daily_yield = qty * spot_price * abs(funding_rate) * 3  # 3× 8h = 24h
        logger.info("true_arb_opened",
                    symbol=symbol, direction=direction,
                    notional_usd=round(qty * spot_price, 2),
                    funding_rate_pct=f"{funding_rate*100:.4f}%",
                    est_daily_yield_usd=round(daily_yield, 4))
        return True

    async def check_exits(
        self,
        symbol: str,
        current_funding_rate: float,
        spot_price: float,
        perp_price: float,
    ) -> bool:
        """
        Check exit conditions for an open arb position.
        Returns True if the position was closed.

        Exit triggers:
          1. Minimum hold not met → skip
          2. Basis convergence: current basis ≤ 10% of opening basis
          3. Rate flipped against us
          4. Max hold (72h)
        """
        pos = self._open_positions.get(symbol)
        if not pos:
            return False

        hold_s = time.time() - pos.opened_at

        # Gate: minimum 8h hold (collect ≥1 funding payment)
        if hold_s < _TRUE_ARB_MIN_HOLD_S:
            return False

        current_basis = perp_price - spot_price
        opening_basis = pos.opening_basis
        hours_held = hold_s / 3600

        # Exit 1: Basis convergence (closing spread tightened by ≥90%)
        basis_converged = abs(current_basis) <= abs(opening_basis) * 0.1
        if basis_converged and opening_basis != 0:
            await self._close_both_legs(symbol, "basis_converged")
            return True

        # Exit 2: Max hold time (72h)
        if hours_held >= 72:
            await self._close_both_legs(symbol, "max_hold_72h")
            return True

        # Exit 3: Rate flipped against us
        if pos.direction == "long_spot_short_perp" and current_funding_rate < -0.0001:
            await self._close_both_legs(symbol, "rate_flipped_negative")
            return True
        if pos.direction == "short_spot_long_perp" and current_funding_rate > 0.0001:
            await self._close_both_legs(symbol, "rate_flipped_positive")
            return True

        return False

    def accrue_funding(self, symbol: str, rate: float, notional_usd: float) -> None:
        """
        Accrue funding to an open position.
        Call this every 8 hours (at funding settlement time).
        """
        pos = self._open_positions.get(symbol)
        if not pos:
            return
        collected = notional_usd * abs(rate)
        pos.funding_collected_usd += collected
        pos.last_funding_ts = time.time()
        logger.info("true_arb_funding_accrued",
                    symbol=symbol, rate_pct=f"{rate*100:.4f}%",
                    collected_usd=round(collected, 4),
                    total_usd=round(pos.funding_collected_usd, 4))

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _close_both_legs(self, symbol: str, reason: str) -> None:
        """Close spot + perp legs for an open arb position."""
        pos = self._open_positions.get(symbol)
        if not pos:
            return

        logger.info("true_arb_closing",
                    symbol=symbol, reason=reason,
                    hold_hours=round((time.time() - pos.opened_at) / 3600, 2),
                    funding_collected=round(pos.funding_collected_usd, 4))

        # Determine close sides (reverse of open)
        if pos.direction == "long_spot_short_perp":
            spot_close_side = "sell"
            perp_close_side = "long"
        else:
            spot_close_side = "buy"
            perp_close_side = "short"

        # Spot close (non-critical failure — log and continue to close perp)
        try:
            spot_price = await self.spot_client.get_spot_price(symbol)
            if spot_price > 0:
                spot_res = await self.spot_client.place_spot_order(
                    perp_symbol=symbol,
                    side=spot_close_side,
                    quantity=pos.spot_qty,
                    price=spot_price,
                )
                if spot_res.get("code", -1) != 0:
                    logger.error("true_arb_spot_close_failed",
                                 symbol=symbol, error=spot_res.get("error"))
        except Exception as e:
            logger.error("true_arb_spot_close_exception", symbol=symbol, error=str(e))

        # Perp close
        symbol_id = self._symbol_ids.get(symbol, 0)
        if symbol_id:
            try:
                perp_res = await self.perp_client.place_order_simple(
                    symbol=symbol,
                    side=perp_close_side,
                    contracts=pos.perp_qty,
                    price=0.0,   # market order
                    symbol_id=symbol_id,
                    account_id=self._account_id,
                )
                if not perp_res.success:
                    logger.error("true_arb_perp_close_failed",
                                 symbol=symbol, error=perp_res.error)
            except Exception as e:
                logger.error("true_arb_perp_close_exception", symbol=symbol, error=str(e))

        del self._open_positions[symbol]

    async def _emergency_close_spot(
        self,
        symbol: str,
        cl_ord_id: str,
        open_side: str,
        qty: float,
        ref_price: float,
    ) -> None:
        """Emergency: close the spot leg when perp placement failed."""
        close_side = "sell" if open_side == "buy" else "buy"
        try:
            spot_price = await self.spot_client.get_spot_price(symbol)
            price = spot_price if spot_price > 0 else ref_price
            res = await self.spot_client.place_spot_order(
                perp_symbol=symbol,
                side=close_side,
                quantity=qty,
                price=price,
            )
            logger.info("true_arb_spot_emergency_close",
                        symbol=symbol, result_code=res.get("code", -1))
        except Exception as e:
            logger.error("true_arb_spot_emergency_close_failed",
                         symbol=symbol, error=str(e))

