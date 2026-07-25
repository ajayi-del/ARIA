"""
risk/chancellor.py — The Chancellor: final, absolute capital governance.

Philosophy: Kant asks "is this trade structurally sound?", Nietzsche asks
"how convicted am I?". The Chancellor asks the last question: "may the
kingdom risk this much capital right now?" — and its answer cannot be
overridden by any agent, bypass, campaign, rally, or personality.

Constitution (all thresholds come from config — nothing hardcoded here;
defaults shown are the documented spec):

  emergency_halt_balance_usd   150.0   balance below this → VETO everything
  veto_drawdown_pct            8.0     session drawdown (PERCENT scale) → VETO
  max_daily_loss_pct           0.05    realized daily loss fraction → VETO
  max_symbol_exposure_pct      0.15    margin per symbol / balance → clamp
  max_kingdom_exposure_pct     0.60    total margin / balance → clamp

Exposure is measured on MARGIN (initial margin = notional / leverage),
matching SoDEX margining rules — notional caps would be meaningless under
leverage, and it is margin that is lost on liquidation.

Clamps (exposure caps) reduce size; vetoes (halt, drawdown, daily loss)
reject outright. Every decision is logged — governance you cannot see is
governance you don't have.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import structlog

log = structlog.get_logger(__name__)


@dataclass
class ChancellorDecision:
    approved: bool
    final_margin_usd: float          # margin allowed after clamping
    reason: str
    vetoed: bool = False             # True when rejected outright
    clamped: bool = False            # True when size was reduced
    details: dict = field(default_factory=dict)


class Chancellor:
    """Final governor. One instance, consulted after ALL sizing, before
    EVERY order submission — main pipeline and cascade fast paths alike."""

    def __init__(self, config=None):
        g = lambda name, default: float(getattr(config, name, default)) if config is not None else default
        self.emergency_halt_balance_usd = g("chancellor_emergency_halt_balance", 150.0)
        self.veto_drawdown_pct          = g("chancellor_veto_drawdown_pct", 8.0)   # PERCENT
        self.max_daily_loss_pct         = g("chancellor_max_daily_loss_pct", 0.05)
        self.max_symbol_exposure_pct    = g("chancellor_max_symbol_exposure_pct", 0.15)
        self.max_kingdom_exposure_pct   = g("chancellor_max_kingdom_exposure_pct", 0.60)
        self.min_margin_usd             = g("chancellor_min_margin_usd", 2.0)
        # State
        self._halted: bool = False
        self._day_start_balance: float = 0.0
        self._day_stamp: str = ""
        self._daily_realized_pnl: float = 0.0

    # ── Daily tracking ────────────────────────────────────────────────────────

    def _roll_day(self, balance: float) -> None:
        stamp = time.strftime("%Y-%m-%d", time.gmtime())
        if stamp != self._day_stamp:
            self._day_stamp = stamp
            self._day_start_balance = balance
            self._daily_realized_pnl = 0.0
            log.info("chancellor_day_rolled", day=stamp,
                     start_balance=round(balance, 2))

    def record_close(self, pnl_usd: float) -> None:
        """Feed realized (net) PnL of every closed trade."""
        self._daily_realized_pnl += pnl_usd

    # ── The gate ──────────────────────────────────────────────────────────────

    def assess(
        self,
        *,
        symbol: str,
        proposed_margin_usd: float,
        balance: float,
        drawdown_pct: float,                      # PERCENT scale (8.0 = 8%)
        open_margins: List[Tuple[str, float]],    # [(symbol, margin_usd), ...]
    ) -> ChancellorDecision:
        self._roll_day(balance)

        # 1. Emergency halt — the kingdom is nearly dead. Full stop.
        if balance < self.emergency_halt_balance_usd:
            if not self._halted:
                self._halted = True
                log.warning("chancellor_emergency_halt",
                            balance=round(balance, 2),
                            floor=self.emergency_halt_balance_usd,
                            note="balance below emergency floor — ALL trading vetoed")
            return ChancellorDecision(False, 0.0, "emergency_halt_balance",
                                      vetoed=True,
                                      details={"balance": balance,
                                               "floor": self.emergency_halt_balance_usd})
        elif self._halted and balance >= self.emergency_halt_balance_usd * 1.10:
            # Hysteresis: resume only 10% above the floor, never flap.
            self._halted = False
            log.warning("chancellor_halt_lifted", balance=round(balance, 2))
        if self._halted:
            return ChancellorDecision(False, 0.0, "emergency_halt_latched",
                                      vetoed=True, details={"balance": balance})

        # 2. Drawdown veto (PERCENT scale per constitution)
        if drawdown_pct >= self.veto_drawdown_pct:
            log.warning("chancellor_veto_drawdown", symbol=symbol,
                        drawdown_pct=round(drawdown_pct, 2),
                        veto_at=self.veto_drawdown_pct)
            return ChancellorDecision(False, 0.0, "veto_drawdown", vetoed=True,
                                      details={"drawdown_pct": drawdown_pct})

        # 3. Daily loss veto
        if self._day_start_balance > 0:
            _daily_loss = -self._daily_realized_pnl / self._day_start_balance
            if _daily_loss >= self.max_daily_loss_pct:
                log.warning("chancellor_veto_daily_loss", symbol=symbol,
                            daily_loss_pct=round(_daily_loss * 100, 2),
                            veto_at=round(self.max_daily_loss_pct * 100, 2))
                return ChancellorDecision(False, 0.0, "veto_daily_loss",
                                          vetoed=True,
                                          details={"daily_loss_pct": _daily_loss})

        # 4. Kingdom exposure clamp (total margin / balance)
        total_open = sum(m for _, m in open_margins)
        kingdom_cap = self.max_kingdom_exposure_pct * balance
        allowed = proposed_margin_usd
        clamped = False
        if total_open + allowed > kingdom_cap:
            allowed = max(0.0, kingdom_cap - total_open)
            clamped = True

        # 5. Symbol exposure clamp (symbol margin / balance)
        sym_open = sum(m for s, m in open_margins if s == symbol)
        sym_cap = self.max_symbol_exposure_pct * balance
        if sym_open + allowed > sym_cap:
            allowed = max(0.0, sym_cap - sym_open)
            clamped = True

        if allowed < self.min_margin_usd:
            log.warning("chancellor_veto_exposure", symbol=symbol,
                        proposed=round(proposed_margin_usd, 2),
                        allowed=round(allowed, 2),
                        symbol_open=round(sym_open, 2),
                        kingdom_open=round(total_open, 2),
                        note="exposure caps leave sub-minimum margin")
            return ChancellorDecision(False, 0.0, "veto_exposure_cap",
                                      vetoed=True,
                                      details={"allowed": allowed,
                                               "symbol_open": sym_open,
                                               "kingdom_open": total_open})

        if clamped:
            log.info("chancellor_clamped", symbol=symbol,
                     proposed=round(proposed_margin_usd, 2),
                     allowed=round(allowed, 2),
                     symbol_open=round(sym_open, 2),
                     kingdom_open=round(total_open, 2))

        return ChancellorDecision(True, allowed,
                                  "clamped" if clamped else "approved",
                                  clamped=clamped,
                                  details={"allowed": allowed})
