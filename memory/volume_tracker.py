"""
VolumeTracker — Persists 14-day daily trading volume for SoDEX fee tier calculation.

Weighted volume formula: weighted_14d = perps_14d + 2 × spot_14d
(SoDEX spot counts double toward tier progression.)

Persists to logs/volume_history.json across restarts.
Format: {"days": [{"date": "2026-04-12", "perps": 0.0, "spot": 0.0}, ...]}
"""
import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
import structlog

log = structlog.get_logger(__name__)

HISTORY_FILE = "logs/volume_history.json"
WINDOW_DAYS = 14


class VolumeTracker:
    """
    Tracks daily perps and spot volume with 14-day rolling window.

    Usage:
        tracker = VolumeTracker()
        tracker.record_trade(perps_notional=10_000, spot_notional=10_000)
        weighted = tracker.get_14d_weighted()  # 30,000 (10k perps + 2×10k spot)
    """

    def __init__(self, history_file: str = HISTORY_FILE):
        self._history_file = history_file
        self._days: list = []   # [{date, perps, spot}, ...] newest last
        self._today_date: str = ""
        self._load()

    # ── Public API ─────────────────────────────────────────────────────────────

    def record_trade(self, perps_notional: float = 0.0, spot_notional: float = 0.0) -> None:
        """
        Add notional volume from a completed trade.
        Call this after each order fill (both legs for arb).

        Args:
            perps_notional: USD notional of the perps leg
            spot_notional:  USD notional of the spot leg
        """
        self._ensure_today()
        entry = self._days[-1]
        entry["perps"] += perps_notional
        entry["spot"] += spot_notional
        self._save()

    def get_14d_weighted(self) -> float:
        """
        Returns 14-day weighted volume: perps_14d + 2 × spot_14d.
        This is what SoDEX uses to compute your fee tier.
        """
        self._prune_old()
        perps_total = sum(d["perps"] for d in self._days)
        spot_total = sum(d["spot"] for d in self._days)
        return perps_total + 2.0 * spot_total

    def get_14d_raw(self) -> dict:
        """Returns raw 14-day totals (unweighted) for display."""
        self._prune_old()
        perps_total = sum(d["perps"] for d in self._days)
        spot_total = sum(d["spot"] for d in self._days)
        return {
            "perps_14d": perps_total,
            "spot_14d": spot_total,
            "weighted_14d": perps_total + 2.0 * spot_total,
            "days_tracked": len(self._days),
        }

    def get_today_volume(self) -> dict:
        """Returns today's running volume totals."""
        self._ensure_today()
        entry = self._days[-1]
        perps = entry["perps"]
        spot = entry["spot"]
        return {
            "date": entry["date"],
            "perps": perps,
            "spot": spot,
            "weighted": perps + 2.0 * spot,
        }

    def days_to_next_tier(self, fee_engine) -> Optional[int]:
        """
        Estimate days until next tier threshold given current daily run rate.
        Returns None if already max tier or insufficient data.

        Args:
            fee_engine: SoDEXFeeEngine instance with current weighted_14d_volume
        """
        gap = fee_engine.volume_to_next_tier()
        if gap <= 0:
            return None

        raw = self.get_14d_raw()
        days = raw["days_tracked"]
        if days < 1:
            return None

        daily_avg = raw["weighted_14d"] / days if days > 0 else 0.0
        if daily_avg <= 0:
            return None

        estimated = int(gap / daily_avg) + 1
        return estimated

    def staking_roi_analysis(self, fee_engine, annual_trading_volume: float) -> dict:
        """
        Compare cost of SOSO staking vs fee savings.

        Assumes SOSO price from market (not tracked here — passed in if available).
        Returns a simple cost/savings table for display.

        Args:
            fee_engine: SoDEXFeeEngine for current rates
            annual_trading_volume: estimated USD volume per year
        """
        results = {}
        current_taker = fee_engine.perps_taker_fee()
        for threshold, discount in [
            (30,      0.05),
            (300,     0.10),
            (3_000,   0.15),
            (30_000,  0.20),
            (300_000, 0.50),
        ]:
            from core.fee_engine import PERPS_TAKER
            base_rate = PERPS_TAKER[fee_engine.current_tier()]
            new_rate = base_rate * (1.0 - discount)
            savings_per_year = (current_taker - new_rate) * annual_trading_volume
            results[threshold] = {
                "soso_needed": threshold,
                "discount_pct": discount * 100,
                "new_taker_pct": new_rate * 100,
                "annual_savings_usd": round(savings_per_year, 2),
            }
        return results

    # ── Internal ────────────────────────────────────────────────────────────────

    def _today_str(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _ensure_today(self) -> None:
        today = self._today_str()
        if not self._days or self._days[-1]["date"] != today:
            self._days.append({"date": today, "perps": 0.0, "spot": 0.0})
            self._prune_old()

    def _prune_old(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
        self._days = [d for d in self._days if d["date"] >= cutoff]

    def _load(self) -> None:
        if not os.path.exists(self._history_file):
            self._days = []
            return
        try:
            with open(self._history_file, "r") as f:
                data = json.load(f)
            self._days = data.get("days", [])
            self._prune_old()
            log.debug("volume_history_loaded", days=len(self._days))
        except Exception as e:
            log.warning("volume_history_load_failed", error=str(e))
            self._days = []

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._history_file) or ".", exist_ok=True)
            with open(self._history_file, "w") as f:
                json.dump({"days": self._days}, f, indent=2)
        except Exception as e:
            log.warning("volume_history_save_failed", error=str(e))
