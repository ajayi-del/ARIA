"""
ARIA Trade Database — permanent append-only record of every closed trade.

Written when a position closes. Never modified. Never deleted.
Feeds CalibrationEngine every 20 trades and at midnight.

Format: JSON Lines (one record per line) at logs/trade_db.jsonl.
"""

import json
import time
import structlog
from dataclasses import dataclass, asdict, field
from pathlib import Path

log = structlog.get_logger(__name__)

DB_PATH = Path("logs/trade_db.jsonl")


@dataclass
class TradeRecord:
    # ── Identity ──────────────────────────────────────────────────────────────
    trade_id: str
    symbol: str
    side: str                       # "long" | "short"
    timestamp_open_ms: int
    timestamp_close_ms: int

    # ── Signal features at entry ──────────────────────────────────────────────
    # All optional — populated via getattr on Position; defaults used if absent.
    coherence_score: float
    tiers_fired: list
    htf_regime: str
    session_name: str
    session_mult: float

    # ── Execution ─────────────────────────────────────────────────────────────
    entry_price: float
    exit_price: float
    notional_usd: float
    leverage: int
    stop_price: float
    tp1_price: float
    atr: float

    # ── Outcome ───────────────────────────────────────────────────────────────
    hold_seconds: float
    directional_pnl: float
    net_pnl: float
    max_adverse_excursion: float    # price units, absolute (e.g. $0.82 on SOL)
    max_favourable_excursion: float  # price units, absolute
    exit_reason: str                # "exchange_close" | "sub_notional" | "time_stop"

    # ── Derived properties ────────────────────────────────────────────────────

    @property
    def win(self) -> bool:
        return self.net_pnl > 0

    @property
    def mae_pct(self) -> float:
        """MAE as % of entry price — used for stop multiplier calibration."""
        return (self.max_adverse_excursion / self.entry_price * 100) if self.entry_price > 0 else 0.0

    @property
    def mfe_pct(self) -> float:
        return (self.max_favourable_excursion / self.entry_price * 100) if self.entry_price > 0 else 0.0

    @property
    def atr_multiple_at_stop(self) -> float:
        """Stop distance / ATR at entry — measures stop width in ATR units."""
        if self.atr <= 0 or self.entry_price <= 0 or self.stop_price <= 0:
            return 0.0
        return abs(self.entry_price - self.stop_price) / self.atr

    @property
    def was_stopped_early(self) -> bool:
        """
        True when MAE > stop distance AND MFE > MAE — price punched through
        the stop then recovered. Indicates stop was too tight.
        """
        if self.entry_price <= 0 or self.stop_price <= 0:
            return False
        stop_dist_pct = abs(self.entry_price - self.stop_price) / self.entry_price
        return (self.mae_pct / 100 > stop_dist_pct) and (self.mfe_pct > self.mae_pct)


class TradeDatabase:
    """
    Append-only trade record store. ARIA's permanent trading memory.

    New records are appended to DB_PATH on each close.
    On startup the file is read back into memory for calibration.
    If the file is missing or corrupt the in-memory list starts empty —
    ARIA keeps trading uninterrupted.
    """

    def __init__(self) -> None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._records: list[dict] = []
        self._load()

    # ── Public interface ──────────────────────────────────────────────────────

    def record(self, trade: TradeRecord) -> None:
        """Append a closed trade to permanent storage. Never raises."""
        d = asdict(trade)
        d["win"] = trade.win
        d["mae_pct"] = round(trade.mae_pct, 5)
        d["mfe_pct"] = round(trade.mfe_pct, 5)
        d["was_stopped_early"] = trade.was_stopped_early
        d["atr_multiple_at_stop"] = round(trade.atr_multiple_at_stop, 3)
        self._records.append(d)
        try:
            with open(DB_PATH, "a") as f:
                f.write(json.dumps(d) + "\n")
        except Exception as e:
            log.error("trade_db_write_error", error=str(e))
        log.info("trade_recorded",
                 trade_id=trade.trade_id,
                 symbol=trade.symbol,
                 side=trade.side,
                 net_pnl=round(trade.net_pnl, 4),
                 mae_pct=round(trade.mae_pct, 3),
                 hold_s=round(trade.hold_seconds),
                 exit=trade.exit_reason,
                 total=len(self._records))

    def get_all(self) -> list[dict]:
        return self._records.copy()

    def get_recent(self, n: int = 200) -> list[dict]:
        return self._records[-n:] if self._records else []

    def get_by_symbol(self, symbol: str) -> list[dict]:
        return [r for r in self._records if r.get("symbol") == symbol]

    def get_stats(self) -> dict:
        if not self._records:
            return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                    "total_pnl": 0.0, "profit_factor": 0.0, "avg_hold_s": 0}
        wins = [r for r in self._records if r.get("win")]
        losses = [r for r in self._records if not r.get("win")]
        total_win = sum(r.get("net_pnl", 0.0) for r in wins)
        total_loss = abs(sum(r.get("net_pnl", 0.0) for r in losses))
        return {
            "total": len(self._records),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(self._records) * 100, 1),
            "profit_factor": round(total_win / total_loss, 3) if total_loss > 0 else 999.0,
            "total_pnl": round(sum(r.get("net_pnl", 0.0) for r in self._records), 4),
            "avg_hold_s": round(sum(r.get("hold_seconds", 0.0) for r in self._records) / len(self._records)),
            "pct_stopped_early": round(
                sum(1 for r in self._records if r.get("was_stopped_early")) / len(self._records) * 100, 1),
        }

    # ── Private ───────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not DB_PATH.exists():
            return
        try:
            with open(DB_PATH, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self._records.append(json.loads(line))
            log.info("trade_db_loaded", count=len(self._records))
        except Exception as e:
            log.error("trade_db_load_error", error=str(e))
            self._records = []
