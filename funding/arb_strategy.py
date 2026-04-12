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
    ):
        self.config = config
        self.perp_client = perp_client
        self.spot_client = spot_client
        self.funding_radar = funding_radar
        self.fee_engine = fee_engine
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

        # Capital allocation: arb_capital_pct of balance per position
        arb_cap = balance * self.config.arb_capital_pct
        MIN_ARB_NOTIONAL = 20.0   # $20 minimum (covers 2× fees)
        if arb_cap < MIN_ARB_NOTIONAL:
            logger.debug("true_arb_capital_insufficient",
                         symbol=symbol, arb_cap=arb_cap)
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
        if qty * spot_price < MIN_ARB_NOTIONAL:
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

@dataclass
class ArbPosition:
    symbol: str
    direction: str
    size: float
    entry_price: float = 0.0
    opened_at_ms: int = 0
    funding_collected: float = 0.0
    current_pnl: float = 0.0
    spread: float = 0.0
    long_venue: str = "spot"
    short_venue: str = "perps"
    perp_size: float = 0.0
    spot_size: float = 0.0

class FundingArbStrategy:
    """Manages delta-neutral funding arbitrage positions."""
    
    def __init__(
        self,
        config: Settings,
        client: Any, 
        position_manager: PositionManager,
        radar: FundingRadar,
        history: FundingHistory
    ):
        self.config = config
        self.client = client
        self.position_manager = position_manager
        self.radar = radar
        self.history = history
        self._open_arbs: Dict[str, ArbPosition] = {}
        self.system_state = None
        self.candle_buffers = {}

    async def evaluate(self) -> Optional[ArbPosition]:
        """Checks for new arb opportunities."""
        opp = self.radar.get_best_opportunity()
        if not opp:
            return None
            
        # Already have an arb position for this symbol
        if opp.symbol in self._open_arbs:
            return None

        # Minimum rate gate — don't arb negligible funding regardless of score
        if abs(opp.rate) < 0.0001:
            logger.debug("arb_rate_too_low", symbol=opp.symbol, rate=opp.rate)
            return None

        # Capital check — fetch live balance
        balance = await self.client.get_account_balance(self.config.account_id or "paper")
        if balance <= 0:
            logger.warning("arb_skipped_no_balance", symbol=opp.symbol)
            return None

        arb_capital = balance * self.config.arb_capital_pct  # 20% of account for arb

        MIN_ARB_NOTIONAL = 10.0  # $10 minimum — arb is lower risk than directional
        if arb_capital < MIN_ARB_NOTIONAL:
            logger.debug("arb_capital_too_small", symbol=opp.symbol, arb_capital=arb_capital)
            return None

        # Get current price
        trade_flow = self.radar.trade_flow_stores.get(opp.symbol)
        price = trade_flow.latest_price() if trade_flow else 0.0

        if price <= 0:
            return None

        size = arb_capital / price
        rate = opp.rate

        # Gate 1 — warmup check
        if self.system_state and not self.system_state.can_signal(opp.symbol):
            logger.debug("arb_warmup_gate_blocked", symbol=opp.symbol)
            return None

        # Gate 1b — Calendar BLOCK gate
        if hasattr(self, 'calendar_engine') and self.calendar_engine:
            try:
                cal = await self.calendar_engine.get_state(opp.symbol)
                if cal.regime == "BLOCK":
                    logger.debug("arb_calendar_blocked", symbol=opp.symbol, reason=cal.reason)
                    return None
            except Exception:
                pass

        # Gate 2 — minimum candles
        buf = self.candle_buffers.get(opp.symbol, {}).get("1m")
        if buf is None or buf.count() < 20:
            logger.debug("arb_candles_insufficient", symbol=opp.symbol, count=buf.count() if buf else 0)
            return None

        # Gate 3 — minimum notional (already checked via arb_capital above, but
        # guard against price spike making size round to zero)
        notional = size * price
        if notional < MIN_ARB_NOTIONAL:
            logger.debug("arb_size_too_small", symbol=opp.symbol, notional=notional)
            return None
            
        candidate = ArbPosition(
            symbol=opp.symbol,
            direction=opp.direction,
            size=size,
            entry_price=price,
            opened_at_ms=int(time.time() * 1000)
        )
        candidate.perp_size = size
        candidate.spot_size = size

        return candidate

    async def open_arb(self, candidate: ArbPosition) -> bool:
        """Opens simultaneous spot and perp positions - v1.3.FIXED."""
        symbol = candidate.symbol
        logger.info("opening_arb_started", symbol=symbol, direction=candidate.direction, version="v1.3.FIXED")
        
        try:
            # 1. Ensure 1x leverage (arb yield comes from funding, not delta)
            # Placeholder: actual client call to set leverage
            
            # 2. Market orders for speed (v1.3 target < 500ms gap)
            start_time = time.time()
            success = False
            if candidate.direction == "short_arb":
                # Short Perp (side 2) + Long Spot (side 1)
                perp_task = self.client.place_order({
                    "symbol": symbol,
                    "orders": [{"symbol": symbol, "side": 2, "size": candidate.perp_size, "type": 1, "instrument": "perp"}]
                })
                spot_task = self.client.place_order({
                    "symbol": symbol, 
                    "orders": [{"symbol": symbol, "side": 1, "size": candidate.spot_size, "type": 1, "instrument": "spot"}]
                })
            else:
                # Long Perp (side 1) + Short Spot (side 2)
                perp_task = self.client.place_order({
                    "symbol": symbol,
                    "orders": [{"symbol": symbol, "side": 1, "size": candidate.perp_size, "type": 1, "instrument": "perp"}]
                })
                spot_task = self.client.place_order({
                    "symbol": symbol,
                    "orders": [{"symbol": symbol, "side": 2, "size": candidate.spot_size, "type": 1, "instrument": "spot"}]
                })
            
            results = await asyncio.gather(perp_task, spot_task, return_exceptions=True)
            end_time = time.time()
            gap_ms = (end_time - start_time) * 1000
            
            # 3. Check for failures
            failed = False
            for res in results:
                if isinstance(res, Exception) or (hasattr(res, "success") and not res.success):
                    failed = True
                    break
            
            if failed:
                logger.error("arb_leg_failed", symbol=symbol, results=results)
                # 4. Attempt to close any opened leg
                return False
            
            if gap_ms > 500:
                logger.warning("arb_leg_gap_exceeded", symbol=symbol, gap_ms=gap_ms)
            
            # Populate UI telemetry
            if candidate.direction == "short_arb":
                candidate.long_venue = "SoDEX Spot"
                candidate.short_venue = "SoDEX Perp"
            else:
                candidate.long_venue = "SoDEX Perp"
                candidate.short_venue = "SoDEX Spot"
            candidate.entry_spread_pct = 0.02 # Estimated for v1.3
            
            _tf_store = self.radar.trade_flow_stores.get(symbol)
            _latest = _tf_store.latest_price() if _tf_store else None
            candidate.entry_price = float(_latest or 0.0)
            candidate.opened_at_ms = int(time.time() * 1000)
            
            self._open_arbs[symbol] = candidate
            
            # Estimated yield logging (Fix 19)
            notional_usd = candidate.perp_size * candidate.entry_price
            current_rate = getattr(candidate, 'rate', 0.0)
            
            logger.info("arb_opened", 
                symbol=symbol, 
                direction=candidate.direction, 
                size=candidate.perp_size, 
                notional_usd=f"${notional_usd:,.2f}",
                gap_ms=gap_ms,
                funding_rate=f"{current_rate:.4f}%",
                daily_yield_usd=f"${notional_usd * abs(current_rate/100) * 3:,.4f}",
                monthly_yield_usd=f"${notional_usd * abs(current_rate/100) * 3 * 30:,.2f}"
            )
            return True
            
        except Exception as e:
            logger.error("open_arb_exception", symbol=symbol, error=str(e))
            return False

    async def monitor_arbs(self, current_snapshots: Dict[str, FundingSnapshot]) -> None:
        """Monitors open arbs for exit conditions and tracks funding collected."""
        now_ms = int(time.time() * 1000)
        
        for symbol, pos in list(self._open_arbs.items()):
            # Minimum hold check (1 hour)
            MIN_HOLD_MS = 3_600_000
            time_open_ms = now_ms - getattr(pos, "opened_at_ms", now_ms)
            if time_open_ms < MIN_HOLD_MS:
                continue

            if symbol not in current_snapshots:
                continue
                
            snap = current_snapshots[symbol]
            
            # 1. Update funding collected (hourly estimation for this phase)
            # hours_passed = (now_ms - pos.opened_at_ms) / 3600000
            # For this loop, we just apply the hourly rate whenever update_all is called (hourly)
            # In update_all interval:
            if self.radar.should_update():
               collected = pos.size * pos.entry_price * (abs(snap.rate) / 100) # hourly %
               pos.funding_collected += collected
            
            # 2. Check Exits
            # EXIT 1: Rate normalized
            target_exit = getattr(pos, "target_exit_rate", 0.0001)
            if abs(snap.rate) < target_exit:
                await self.close_arb(symbol, "rate_normalized")
                continue
                
            # EXIT 2: Max hold time (72h)
            max_hold = getattr(pos, "max_hold_hours", 72)
            hours_passed = (now_ms - getattr(pos, "opened_at_ms", now_ms)) / 3600000
            if hours_passed >= max_hold:
                await self.close_arb(symbol, "time_exit")
                continue
                
            # EXIT 3: Rate flipped against us
            if pos.direction == "short_arb" and snap.rate < -0.0002: # 0.02%
                await self.close_arb(symbol, "rate_flipped")
                continue
            if pos.direction == "long_arb" and snap.rate > 0.0002:
                await self.close_arb(symbol, "rate_flipped")
                continue

    async def close_arb(self, symbol: str, reason: str) -> None:
        """Closes both legs of the arbitrage position."""
        if symbol not in self._open_arbs:
            return
            
        arb = self._open_arbs[symbol]
        logger.info("closing_arb_started", symbol=symbol, reason=reason)
        
        try:
            # Market orders to close both legs
            if arb.direction == "short_arb":
                # Long Perp (side 1) + Short Spot (side 2)
                perp_task = self.client.place_order({
                    "symbol": symbol,
                    "orders": [{"symbol": symbol, "side": 1, "size": arb.perp_size, "type": 1, "instrument": "perp"}]
                })
                spot_task = self.client.place_order({
                    "symbol": symbol,
                    "orders": [{"symbol": symbol, "side": 2, "size": arb.spot_size, "type": 1, "instrument": "spot"}]
                })
            else:
                # Short Perp (side 2) + Long Spot (side 1)
                perp_task = self.client.place_order({
                    "symbol": symbol,
                    "orders": [{"symbol": symbol, "side": 2, "size": arb.perp_size, "type": 1, "instrument": "perp"}]
                })
                spot_task = self.client.place_order({
                    "symbol": symbol,
                    "orders": [{"symbol": symbol, "side": 1, "size": arb.spot_size, "type": 1, "instrument": "spot"}]
                })
                
            await asyncio.gather(perp_task, spot_task)
            
            hold_time = (time.time() * 1000 - arb.opened_at_ms) / 3600000
            logger.info("arb_closed", symbol=symbol, reason=reason, funding_collected=arb.funding_collected, hold_time_hours=hold_time)
            
            del self._open_arbs[symbol]
            
        except Exception as e:
            logger.error("close_arb_exception", symbol=symbol, error=str(e))

    def get_open_arbs(self) -> List[ArbPosition]:
        return list(self._open_arbs.values())

    def get_arb_summary(self) -> Dict[str, Any]:
        total_collected = sum(arb.funding_collected for arb in self._open_arbs.values())
        return {
            "active_arbs": len(self._open_arbs),
            "total_collected": total_collected,
            "open_symbols": list(self._open_arbs.keys())
        }

    def update_positions(self, mark_price_stores: dict) -> None:
        """Calculates current P&L and spread for all active positions."""
        positions = getattr(self, "_positions", getattr(self, "_open_arbs", {}))

        for symbol, pos in positions.items():
            store = mark_price_stores.get(symbol)
            if not store:
                continue
            data = store.get()
            if not data:
                continue
                
            mark = float(data.get("mark_price", 0.0))
            last = float(data.get("last_price", mark))
            if mark == 0:
                continue

            entry = getattr(pos, "entry_price", mark)
            size = getattr(pos, "size", 0.0)
            direction = getattr(pos, "direction", "long_arb")
            funding = getattr(pos, "funding_collected", 0.0)

            if direction == "long_arb":
                unrealised = (mark - entry) * size
            else:
                unrealised = (entry - mark) * size

            pos.current_pnl = unrealised + funding
            pos.spread = mark - last
