"""
yield/slp_tracker.py — SLP Vault & SOSO Staking Accounting Monitor.

Architecture
────────────
ARIA cannot programmatically deposit into or withdraw from the SLP vault.
Deposits and withdrawals are done manually on https://ssi.sosovalue.com/earn.

This module is accounting + monitoring only:
  - Tracks manually deposited sMAG7.ssi quantity and USD value
  - Estimates accrued yield (index appreciation + MM revenue)
  - Feeds SLP yield into the YieldTracker (SOVEREIGN budget source)
  - Tracks SOSO staking discount savings
  - Exposes a display snapshot for the terminal panel

Environment variables (set in .env):
  SLP_VAULT_SMAG7_DEPOSITED   — sMAG7.ssi tokens deposited (default 0)
  SLP_VAULT_ENTRY_DATE        — ISO date of deposit (default today)
  SLP_VAULT_ENTRY_USD         — USD value at entry (default 0)
  SOSO_STAKED                 — SOSO tokens staked (default 0, existing var)
  SOSO_FEE_SAVED_30D          — Manually updated 30d fee saving (default 0)

Yield estimation (until API is available):
  Index yield   ≈ 15% APY (SLP vault baseline)
  MM revenue    ≈ 8% APY (estimated market making revenue)
  Total APY est ≈ 23% (conservative; actual varies with vault activity)
"""

from __future__ import annotations

import os
import time
import asyncio
import structlog
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional, Callable

logger = structlog.get_logger(__name__)

# ── Yield APY estimates (updated manually or via vault API when available) ──────
_INDEX_YIELD_APY   = 0.15   # 15% — index appreciation component
_MM_REVENUE_APY    = 0.08   # 8%  — market making revenue component
_TOTAL_APY_EST     = _INDEX_YIELD_APY + _MM_REVENUE_APY

# SOSO staking discount tier
_SOSO_DISCOUNT_PCT_169 = 5.0   # 5% discount at 169 SOSO staked


@dataclass
class SLPSnapshot:
    """Point-in-time snapshot of SLP vault + SOSO staking state."""
    # Vault
    smag7_deposited:      float   # sMAG7.ssi tokens in vault
    vault_usd_at_entry:   float   # USD value at deposit time
    vault_usd_current:    float   # Estimated current USD value
    days_held:            int
    index_yield_usd:      float   # Estimated index component (30d window)
    mm_revenue_usd:       float   # Estimated MM component (30d window)
    total_yield_30d_usd:  float
    annualised_apy:       float   # Based on actual price change if available
    funding_collected_usd: float  # From hedge position (0 until perp exists)
    hedge_status:         str     # "NO_HEDGE" | "SHORT {qty} {symbol}"
    # SOSO staking
    soso_staked:          float
    soso_discount_pct:    float
    soso_saved_30d_usd:   float
    # MAG7SSI price feed (latest)
    mag7ssi_price:        float = 0.0
    mag7ssi_price_entry:  float = 0.0


class SLPVaultTracker:
    """
    Accounting monitor for the SLP vault position.

    Usage in main.py:
        slp_tracker = SLPVaultTracker(config, yield_tracker)
        # In gather loop:
        slp_tracker.monitor_loop()   # runs every 6 hours
    """

    _LOG_INTERVAL_S = 6 * 3600   # log every 6 hours

    def __init__(self, config, yield_tracker=None) -> None:
        self.config        = config
        self.yield_tracker = yield_tracker

        # ── Load from env ──────────────────────────────────────────────────────
        self._smag7_deposited   = float(os.getenv("SLP_VAULT_SMAG7_DEPOSITED", "0"))
        self._vault_entry_usd   = float(os.getenv("SLP_VAULT_ENTRY_USD",       "0"))
        self._soso_staked       = float(os.getenv("SOSO_STAKED",               "0"))
        self._soso_saved_30d    = float(os.getenv("SOSO_FEE_SAVED_30D",        "0"))

        _entry_str = os.getenv("SLP_VAULT_ENTRY_DATE", "")
        try:
            self._entry_date = date.fromisoformat(_entry_str) if _entry_str else date.today()
        except ValueError:
            self._entry_date = date.today()

        # ── Runtime state ──────────────────────────────────────────────────────
        self._mag7ssi_price_current:   float = 0.0
        self._mag7ssi_price_entry:     float = float(
            os.getenv("SLP_VAULT_SMAG7_ENTRY_PRICE", "0")
        )
        self._funding_collected_usd:   float = 0.0
        self._hedge_status:            str   = "NO_HEDGE"
        self._last_log_ts:             float = 0.0
        self._started_at:              float = time.time()

    # ── Price feed ────────────────────────────────────────────────────────────

    def update_mag7ssi_price(self, price: float) -> None:
        """Called by spot WS feed when MAG7SSI price updates."""
        if price > 0:
            self._mag7ssi_price_current = price
            if self._mag7ssi_price_entry == 0:
                self._mag7ssi_price_entry = price

    # ── Yield computation ─────────────────────────────────────────────────────

    def compute_yield_30d(self) -> tuple[float, float, float]:
        """
        Returns (index_yield_usd, mm_revenue_usd, total_usd) for a 30d window.

        Priority:
          1. If MAG7SSI price is known: actual price appreciation × quantity
          2. Otherwise: APY estimate
        """
        capital = self._vault_entry_usd or (
            self._smag7_deposited * self._mag7ssi_price_entry
        )
        if capital <= 0:
            return 0.0, 0.0, 0.0

        days = (date.today() - self._entry_date).days
        window = min(days, 30) / 365  # fraction of year for 30d window

        # Index component: actual price change if available, else APY estimate
        if self._mag7ssi_price_current > 0 and self._mag7ssi_price_entry > 0:
            price_return = (
                (self._mag7ssi_price_current - self._mag7ssi_price_entry)
                / self._mag7ssi_price_entry
            )
            index_yield = self._smag7_deposited * self._mag7ssi_price_entry * max(price_return, 0)
        else:
            index_yield = capital * _INDEX_YIELD_APY * window

        mm_revenue = capital * _MM_REVENUE_APY * window
        return round(index_yield, 4), round(mm_revenue, 4), round(index_yield + mm_revenue, 4)

    def compute_apy(self) -> float:
        """Actual APY based on total_yield / capital / time, or estimate."""
        capital = self._vault_entry_usd or (
            self._smag7_deposited * max(self._mag7ssi_price_entry, 1e-9)
        )
        if capital <= 0:
            return _TOTAL_APY_EST

        days = max(1, (date.today() - self._entry_date).days)
        _, _, total = self.compute_yield_30d()
        apy = (total / capital) * (365 / days)
        return round(apy, 4)

    # ── Snapshot ──────────────────────────────────────────────────────────────

    def get_snapshot(self) -> SLPSnapshot:
        """Return a display-ready snapshot of current vault state."""
        index_y, mm_y, total_y = self.compute_yield_30d()
        days = (date.today() - self._entry_date).days

        current_usd = self._vault_entry_usd
        if self._mag7ssi_price_current > 0 and self._smag7_deposited > 0:
            current_usd = self._smag7_deposited * self._mag7ssi_price_current

        soso_discount = (
            _SOSO_DISCOUNT_PCT_169
            if self._soso_staked >= 169
            else round(self._soso_staked / 169 * _SOSO_DISCOUNT_PCT_169, 2)
        )

        return SLPSnapshot(
            smag7_deposited       = self._smag7_deposited,
            vault_usd_at_entry    = self._vault_entry_usd,
            vault_usd_current     = round(current_usd, 2),
            days_held             = days,
            index_yield_usd       = index_y,
            mm_revenue_usd        = mm_y,
            total_yield_30d_usd   = total_y,
            annualised_apy        = self.compute_apy(),
            funding_collected_usd = self._funding_collected_usd,
            hedge_status          = self._hedge_status,
            soso_staked           = self._soso_staked,
            soso_discount_pct     = soso_discount,
            soso_saved_30d_usd    = self._soso_saved_30d,
            mag7ssi_price         = self._mag7ssi_price_current,
            mag7ssi_price_entry   = self._mag7ssi_price_entry,
        )

    # ── Async loop ────────────────────────────────────────────────────────────

    async def monitor_loop(self) -> None:
        """
        Runs every 6 hours. Logs vault status and feeds yield into YieldTracker.
        """
        while True:
            await asyncio.sleep(self._LOG_INTERVAL_S)
            try:
                await self._tick()
            except Exception as e:
                logger.error("slp_monitor_error", error=str(e))

    async def _tick(self) -> None:
        """One monitoring cycle: compute yield, log, optionally feed YieldTracker."""
        if self._smag7_deposited <= 0:
            return   # Not configured — nothing to track

        index_y, mm_y, total_y = self.compute_yield_30d()
        snap = self.get_snapshot()

        logger.info(
            "slp_vault_status",
            smag7_deposited        = snap.smag7_deposited,
            vault_usd_entry        = snap.vault_usd_at_entry,
            vault_usd_current      = snap.vault_usd_current,
            days_held              = snap.days_held,
            index_yield_usd_30d    = snap.index_yield_usd,
            mm_revenue_usd_30d     = snap.mm_revenue_usd,
            total_yield_30d_usd    = snap.total_yield_30d_usd,
            annualised_apy_pct     = round(snap.annualised_apy * 100, 1),
            funding_collected_usd  = snap.funding_collected_usd,
            hedge_status           = snap.hedge_status,
            soso_staked            = snap.soso_staked,
            soso_discount_pct      = snap.soso_discount_pct,
            soso_saved_30d_usd     = snap.soso_saved_30d_usd,
            mag7ssi_price          = snap.mag7ssi_price,
        )

        # Feed yield accrued since last cycle into YieldTracker (SOVEREIGN budget source)
        if self.yield_tracker is not None and total_y > 0:
            # Distribute proportionally: 6h / 720h (30d) = 1/120 of 30d yield per cycle
            cycle_yield = total_y / 120
            await self.yield_tracker.add_yield(cycle_yield)
            logger.debug("slp_yield_fed_to_sovereign",
                         cycle_yield_usd=round(cycle_yield, 6))

    # ── Hedge management ─────────────────────────────────────────────────────

    # Minimum funding rate (per 8h) to open a delta-neutral hedge on MAG7SSI-USD perp.
    # Below this threshold, the cost of the hedge (spread + rebate drag) exceeds benefit.
    MIN_FUNDING_TO_HEDGE   = 0.0001   # 0.01% per 8h
    CLOSE_HEDGE_THRESHOLD  = 0.00005  # 0.005% per 8h — close when funding dries up

    _HEDGE_INTERVAL_S = 60   # check every 60 seconds

    def set_hedge_callback(
        self,
        open_hedge: Callable,   # async fn(symbol, qty) → bool
        close_hedge: Callable,  # async fn(symbol) → bool
        get_funding: Callable,  # fn(symbol) → float (funding rate per 8h)
        get_atr_ratio: Callable,# fn(symbol) → float
    ) -> None:
        """
        Wire execution callbacks after tracker is created.
        Called from main.py once the perp client and funding_radar are available.
        """
        self._open_hedge    = open_hedge
        self._close_hedge   = close_hedge
        self._get_funding   = get_funding
        self._get_atr_ratio = get_atr_ratio

    def _should_hedge(self, funding_rate: float, atr_ratio: float) -> bool:
        """
        Open a delta-neutral SHORT on MAG7SSI-USD perp when:
          1. Perp funding is high enough to cover hedge cost
          2. Market is not in extreme expansion (atr_ratio < 1.5)
          3. We have a vault position to hedge
        """
        if self._smag7_deposited <= 0:
            return False
        if funding_rate < self.MIN_FUNDING_TO_HEDGE:
            return False
        if atr_ratio > 1.5:
            return False   # too volatile — don't open into chaos
        return True

    async def manage_loop(self) -> None:
        """
        Runs every 60 seconds. Checks MAG7SSI-USD perp funding rate and
        opens or closes the delta-neutral hedge when conditions are met.

        Hedge logic:
          - No hedge open + should_hedge() → open SHORT equal to deposited quantity
          - Hedge open + funding < CLOSE_HEDGE_THRESHOLD → close SHORT
          - Funding collected → update _funding_collected_usd

        Wire up by setting callbacks via set_hedge_callback() BEFORE calling this.
        If callbacks are not set, the loop runs silently (monitor-only mode).
        """
        _open_hedge    = getattr(self, "_open_hedge",    None)
        _close_hedge   = getattr(self, "_close_hedge",   None)
        _get_funding   = getattr(self, "_get_funding",   None)
        _get_atr_ratio = getattr(self, "_get_atr_ratio", None)

        while True:
            await asyncio.sleep(self._HEDGE_INTERVAL_S)
            try:
                if self._smag7_deposited <= 0:
                    continue  # vault not configured

                if _get_funding is None:
                    continue  # callbacks not wired — monitor-only mode

                funding_rate = _get_funding("MAG7SSI-USD") or 0.0
                atr_ratio    = (_get_atr_ratio("MAG7SSI-USD") or 1.0)
                hedge_open   = "SHORT" in self._hedge_status

                if hedge_open:
                    if funding_rate < self.CLOSE_HEDGE_THRESHOLD:
                        try:
                            ok = await _close_hedge("MAG7SSI-USD")
                            if ok:
                                self._hedge_status = "NO_HEDGE"
                                logger.info("slp_hedge_closed",
                                            reason="funding_below_threshold",
                                            funding_rate=round(funding_rate, 6))
                        except Exception as e:
                            logger.warning("slp_hedge_close_error", error=str(e))
                else:
                    if self._should_hedge(funding_rate, atr_ratio):
                        hedge_qty = round(self._smag7_deposited, 2)
                        try:
                            ok = await _open_hedge("MAG7SSI-USD", hedge_qty)
                            if ok:
                                self._hedge_status = f"SHORT {hedge_qty} MAG7SSI-USD"
                                logger.info("slp_hedge_opened",
                                            qty=hedge_qty,
                                            funding_rate=round(funding_rate, 6),
                                            atr_ratio=round(atr_ratio, 2))
                        except Exception as e:
                            logger.warning("slp_hedge_open_error", error=str(e))

            except Exception as e:
                logger.error("slp_manage_loop_error", error=str(e))
