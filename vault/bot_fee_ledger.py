"""
Per-Bot Surgical Fee Ledger
============================
Tracks performance and management fees independently for each bot instance.

Two bots:
  bot_id="aria"    — ARIA live (real USD, real trades)
  bot_id="phantom" — Phantom paper (simulated USD, paper trades)

Fee structure:
  Performance fee : 20% of net profit above High Water Mark (applied per trade close)
  Management fee  : 2% annual, accrued hourly (applied in vault_loop)

Fees are tracked in logs/fees_{bot_id}.json.
No fee is ever double-counted: HWM advances only when balance exceeds it.
"""

import json
import time
import structlog
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import List, Dict, Optional
from pathlib import Path

logger = structlog.get_logger(__name__)

# Wallet address that receives all accrued fees
FEE_RECIPIENT = "0x776bFc0a88c2e45B57Db4b831C0DEcF76bB69f88"

PERFORMANCE_FEE_PCT = 0.20   # 20% of profit above HWM
MANAGEMENT_FEE_ANNUAL = 0.02  # 2% per year


@dataclass
class FeeEvent:
    ts_ms: int
    bot_id: str
    symbol: str
    trigger: str          # "trade_close" | "hourly_mgmt"
    pnl_usd: float
    perf_fee: float
    mgmt_fee: float
    total_fee: float
    balance_before: float
    balance_after: float
    hwm_before: float
    hwm_after: float
    recipient: str


class BotFeeLedger:
    """
    Surgical per-bot fee tracker.

    Usage:
        aria_fees   = BotFeeLedger("aria",    starting_balance=real_balance)
        phantom_fees = BotFeeLedger("phantom", starting_balance=paper_balance)

        # On every trade close:
        fee = aria_fees.on_trade_closed(symbol="BTC-USD", pnl_usd=120.0, current_balance=10120.0)

        # Hourly (called from vault_loop):
        fee = aria_fees.accrue_management(current_balance=10120.0)
    """

    def __init__(self, bot_id: str, starting_balance: float, log_dir: str = "./logs"):
        self.bot_id = bot_id
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        self._ledger_file = self.log_dir / f"fees_{bot_id}.json"

        # State — will be overwritten by _load() if file exists
        self.high_water_mark: float = starting_balance
        self.total_performance_fees: float = 0.0
        self.total_management_fees: float = 0.0
        self.fee_events: List[Dict] = []
        self._last_mgmt_time: float = time.time()

        self._load()
        # If HWM wasn't persisted yet, seed from starting_balance
        if self.high_water_mark == 0.0 and starting_balance > 0:
            self.high_water_mark = starting_balance

    # ──────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────

    def on_trade_closed(
        self,
        symbol: str,
        pnl_usd: float,
        current_balance: float
    ) -> float:
        """
        Called on every trade close.
        Computes and records the exact performance fee for this close.
        Returns total fee deducted.
        """
        hwm_before = self.high_water_mark
        perf_fee = 0.0

        if current_balance > self.high_water_mark:
            profit_above_hwm = current_balance - self.high_water_mark
            perf_fee = profit_above_hwm * PERFORMANCE_FEE_PCT
            self.high_water_mark = current_balance  # advance HWM

        if perf_fee <= 0:
            return 0.0

        balance_after = current_balance - perf_fee
        self.total_performance_fees += perf_fee

        event = FeeEvent(
            ts_ms=int(time.time() * 1000),
            bot_id=self.bot_id,
            symbol=symbol,
            trigger="trade_close",
            pnl_usd=pnl_usd,
            perf_fee=perf_fee,
            mgmt_fee=0.0,
            total_fee=perf_fee,
            balance_before=current_balance,
            balance_after=balance_after,
            hwm_before=hwm_before,
            hwm_after=self.high_water_mark,
            recipient=FEE_RECIPIENT,
        )
        self._record(event)

        logger.info("fee_charged",
                    bot=self.bot_id,
                    symbol=symbol,
                    perf_fee=f"${perf_fee:.4f}",
                    hwm_before=f"${hwm_before:.2f}",
                    hwm_after=f"${self.high_water_mark:.2f}",
                    recipient=FEE_RECIPIENT)

        return perf_fee

    def accrue_management(self, current_balance: float) -> float:
        """
        Called hourly from vault_loop.
        Accrues management fee since last call.
        Returns fee deducted.
        """
        now = time.time()
        hours_elapsed = (now - self._last_mgmt_time) / 3600.0
        if hours_elapsed <= 0:
            return 0.0

        mgmt_fee = current_balance * (MANAGEMENT_FEE_ANNUAL / 8760.0) * hours_elapsed
        if mgmt_fee <= 0.001:  # sub-cent — skip
            self._last_mgmt_time = now
            return 0.0

        self._last_mgmt_time = now
        balance_after = current_balance - mgmt_fee
        self.total_management_fees += mgmt_fee

        event = FeeEvent(
            ts_ms=int(now * 1000),
            bot_id=self.bot_id,
            symbol="ALL",
            trigger="hourly_mgmt",
            pnl_usd=0.0,
            perf_fee=0.0,
            mgmt_fee=mgmt_fee,
            total_fee=mgmt_fee,
            balance_before=current_balance,
            balance_after=balance_after,
            hwm_before=self.high_water_mark,
            hwm_after=self.high_water_mark,
            recipient=FEE_RECIPIENT,
        )
        self._record(event)

        logger.info("management_fee_accrued",
                    bot=self.bot_id,
                    mgmt_fee=f"${mgmt_fee:.6f}",
                    hours=f"{hours_elapsed:.2f}")

        return mgmt_fee

    def get_summary(self) -> Dict:
        """Returns a summary dict for terminal display."""
        return {
            "bot_id": self.bot_id,
            "high_water_mark": self.high_water_mark,
            "total_performance_fees": self.total_performance_fees,
            "total_management_fees": self.total_management_fees,
            "total_fees_all": self.total_performance_fees + self.total_management_fees,
            "recipient": FEE_RECIPIENT,
            "event_count": len(self.fee_events),
        }

    # ──────────────────────────────────────────────
    # Persistence
    # ──────────────────────────────────────────────

    def _record(self, event: FeeEvent) -> None:
        self.fee_events.append(asdict(event))
        self._save()

    def _save(self) -> None:
        try:
            data = {
                "bot_id": self.bot_id,
                "recipient": FEE_RECIPIENT,
                "high_water_mark": self.high_water_mark,
                "total_performance_fees": self.total_performance_fees,
                "total_management_fees": self.total_management_fees,
                "last_mgmt_time": self._last_mgmt_time,
                "events": self.fee_events[-500:],  # keep last 500
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(self._ledger_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("fee_ledger_save_failed", bot=self.bot_id, error=str(e))

    def _load(self) -> None:
        if not self._ledger_file.exists():
            return
        try:
            with open(self._ledger_file) as f:
                data = json.load(f)
            self.high_water_mark = float(data.get("high_water_mark", 0.0))
            self.total_performance_fees = float(data.get("total_performance_fees", 0.0))
            self.total_management_fees = float(data.get("total_management_fees", 0.0))
            self._last_mgmt_time = float(data.get("last_mgmt_time", time.time()))
            self.fee_events = data.get("events", [])
            logger.info("fee_ledger_loaded",
                        bot=self.bot_id,
                        hwm=f"${self.high_water_mark:.2f}",
                        total_fees=f"${self.total_performance_fees + self.total_management_fees:.4f}")
        except Exception as e:
            logger.warning("fee_ledger_load_failed", bot=self.bot_id, error=str(e))
