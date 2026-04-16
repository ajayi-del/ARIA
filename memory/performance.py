"""
Performance Tracker

Computes real-time statistics from the trade journal.
Updates after every closed trade.
"""

import os
import json
import glob
import structlog
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from math import sqrt
from .trade_journal import TradeJournal

logger = structlog.get_logger(__name__)


class SessionDrawdownTracker:
    """
    Session-level drawdown tracker with regime gating.

    Regimes (thresholds configurable via .env):
      normal    — full bracket, no restrictions
      caution   — TP1 only (no runners), normal size        [DD_CAUTION_PCT]
      defensive — TP1 at 50% distance, normal size          [DD_DEFENSIVE_PCT]
      halt      — no new entries                            [DD_HALT_PCT]

    Consecutive loss gate: skip next signal if streak >= MAX_CONSECUTIVE_LOSSES.
    """

    def __init__(self):
        self.peak_equity: float = 0.0
        self.current_equity: float = 0.0
        self.session_drawdown_pct: float = 0.0
        self.consecutive_losses: int = 0
        self.drawdown_regime: str = "normal"

        # Thresholds — read from env or use defaults
        self._caution_pct   = float(os.getenv("DD_CAUTION_PCT",   "3.0"))
        self._defensive_pct = float(os.getenv("DD_DEFENSIVE_PCT", "6.0"))
        self._halt_pct      = float(os.getenv("DD_HALT_PCT",      "10.0"))
        self._max_consec    = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "4"))

    def update_drawdown(self, current_equity: float) -> str:
        """
        Called after every closed trade or on every balance refresh.
        Updates peak, drawdown %, and regime. Returns new regime string.
        """
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity

        if self.peak_equity > 0:
            self.session_drawdown_pct = (
                (self.peak_equity - current_equity) / self.peak_equity
            ) * 100
        else:
            self.session_drawdown_pct = 0.0

        self.current_equity = current_equity

        if self.session_drawdown_pct >= self._halt_pct:
            self.drawdown_regime = "halt"
        elif self.session_drawdown_pct >= self._defensive_pct:
            self.drawdown_regime = "defensive"
        elif self.session_drawdown_pct >= self._caution_pct:
            self.drawdown_regime = "caution"
        else:
            self.drawdown_regime = "normal"

        logger.info(
            "drawdown_update",
            drawdown_pct=round(self.session_drawdown_pct, 2),
            regime=self.drawdown_regime,
            peak=round(self.peak_equity, 4),
            current=round(current_equity, 4),
        )
        return self.drawdown_regime

    def on_trade_closed(self, pnl: float) -> None:
        """Update consecutive loss counter after a closed trade."""
        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def is_halted(self) -> bool:
        return self.drawdown_regime == "halt"

    def too_many_losses(self) -> bool:
        return self.consecutive_losses >= self._max_consec

    def tp_multipliers(self) -> tuple:
        """
        Returns (tp1_mult, include_tp2, include_tp3) for the current regime.
          normal    → (1.0, True,  True)
          caution   → (1.0, False, False)   TP1 only, no runners
          defensive → (0.5, False, False)   TP1 at half distance
          halt      → (0.0, False, False)   should not reach here
        """
        if self.drawdown_regime == "defensive":
            return (0.5, False, False)
        if self.drawdown_regime == "caution":
            return (1.0, False, False)
        return (1.0, True, True)  # normal / halt (halt is blocked before this)


@dataclass
class PerformanceStats:
    """
    Performance statistics computed from trade journal.
    """
    total_trades: int = 0
    open_trades: int = 0
    closed_trades: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_r: float = 0.0
    total_pnl_usd: float = 0.0
    max_drawdown_pct: float = 0.0
    current_streak: int = 0
    best_trade_r: float = 0.0
    worst_trade_r: float = 0.0
    avg_hold_time_h: float = 0.0
    sqn: float = 0.0
    
    # Per signal stats
    sweep_win_rate: float = 0.0
    divergence_win_rate: float = 0.0
    by_symbol: Dict[str, Dict[str, float]] = None
    by_regime: Dict[str, Dict[str, float]] = None
    
    def __post_init__(self):
        if self.by_symbol is None:
            self.by_symbol = {}
        if self.by_regime is None:
            self.by_regime = {}

    def to_dict(self) -> dict:
        import dataclasses
        return dataclasses.asdict(self)


@dataclass
class PersonalityStats:
    """Per-personality performance stats, restored from journal on startup."""
    personality:    str
    total_trades:   int   = 0
    wins:           int   = 0
    losses:         int   = 0
    win_rate:       float = 0.0
    total_pnl_usd:  float = 0.0
    avg_win_r:      float = 0.0   # average R-multiple on winning trades
    avg_loss_r:     float = 0.0   # average abs(R-multiple) on losing trades
    profit_factor:  float = 0.0
    current_streak: int   = 0     # +N wins, -N losses (most recent run)

    @property
    def win_streak(self) -> int:
        return max(0, self.current_streak)

    @property
    def loss_streak(self) -> int:
        return max(0, -self.current_streak)


class PerformanceTracker:
    """
    Computes real-time statistics from the trade journal.
    Updates after every closed trade.

    Persistent memory: call restore_from_journal() at startup to load
    historical per-personality stats from all available journal files.
    After restoration, get_win_rate(personality) and get_streaks(personality)
    return journal-backed values that survive bot restarts.
    """

    def __init__(self) -> None:
        # Per-personality stats restored from journal files at startup
        self._personality_stats: Dict[str, PersonalityStats] = {}
        # Global streak restored from journal (decomposed into win/loss)
        self._global_streak: int = 0
        # Recovery mode: True when overall win rate < 50%
        self._recovery_mode: bool = False
        # Session-only stats (since last restart, not journal-backed)
        self._session_stats: Dict[str, Dict] = {}

    def restore_from_journal(self, log_dir: str = "./logs") -> None:
        """
        Load all closed trades from every journal file in log_dir.
        Group by personality, compute per-personality stats.
        Call once at startup BEFORE trading loops begin.

        Reads today's file + any previous-day files matching the
        trade_journal_*.json pattern. Journal entries without a
        'personality' field are attributed to "SCOUT" (default).

        Idempotent — safe to call multiple times.
        """
        pattern = os.path.join(log_dir, "trade_journal_*.json")
        files   = sorted(glob.glob(pattern))

        all_closed: List[Dict] = []
        for fpath in files:
            try:
                with open(fpath, "r") as fh:
                    raw = json.load(fh)
                for entry in raw:
                    if entry.get("outcome") in ("win", "loss"):
                        all_closed.append(entry)
            except (json.JSONDecodeError, OSError):
                continue

        if not all_closed:
            logger.info("performance_restore_no_data",
                        log_dir=log_dir, files_checked=len(files))
            return

        # Sort chronologically — streak computation depends on order
        all_closed.sort(key=lambda e: e.get("closed_at_ms") or e.get("timestamp_ms") or 0)

        # Group by personality
        by_personality: Dict[str, List[Dict]] = {}
        for entry in all_closed:
            p = (entry.get("personality") or "SCOUT").upper()
            by_personality.setdefault(p, []).append(entry)

        total_across = 0
        for p_name, entries in by_personality.items():
            stats = self._compute_personality_stats(p_name, entries)
            self._personality_stats[p_name] = stats
            total_across += stats.total_trades

        # Global streak from all trades ordered by time
        self._global_streak = self._calc_streak(all_closed)

        overall_wins = sum(s.wins for s in self._personality_stats.values())
        overall_wr   = overall_wins / total_across if total_across > 0 else 0.0

        # Set recovery mode: True when win rate < 50%, persists across restarts
        self._recovery_mode = (total_across >= 10) and (overall_wr < 0.50)

        logger.info(
            "performance_restored",
            personalities=list(self._personality_stats.keys()),
            total_trades=total_across,
            overall_wr=round(overall_wr, 3),
            global_streak=self._global_streak,
            recovery_mode=self._recovery_mode,
        )

    def _compute_personality_stats(
        self, name: str, entries: List[Dict]
    ) -> PersonalityStats:
        """Compute aggregates for one personality from its closed trades."""
        wins   = [e for e in entries if e.get("outcome") == "win"]
        losses = [e for e in entries if e.get("outcome") == "loss"]

        def _pnl(e: dict) -> float:
            v = e.get("pnl_net_usd")
            if v is None: v = e.get("pnl_usd")
            return float(v) if v is not None else 0.0

        def _r(e: dict) -> float:
            v = e.get("pnl_r")
            return float(v) if v is not None else 0.0

        n          = len(entries)
        win_count  = len(wins)
        win_rate   = win_count / n if n > 0 else 0.0
        total_pnl  = sum(_pnl(e) for e in entries)

        win_rs  = [_r(e) for e in wins  if e.get("pnl_r") is not None]
        loss_rs = [abs(_r(e)) for e in losses if e.get("pnl_r") is not None]
        avg_win_r  = sum(win_rs)  / len(win_rs)  if win_rs  else 0.0
        avg_loss_r = sum(loss_rs) / len(loss_rs) if loss_rs else 0.0

        win_sum  = sum(_pnl(e) for e in wins)
        loss_sum = abs(sum(_pnl(e) for e in losses))
        pf       = win_sum / loss_sum if loss_sum > 0 else (float("inf") if win_sum > 0 else 0.0)

        streak = self._calc_streak(entries)

        return PersonalityStats(
            personality    = name,
            total_trades   = n,
            wins           = win_count,
            losses         = len(losses),
            win_rate       = round(win_rate, 3),
            total_pnl_usd  = round(total_pnl, 4),
            avg_win_r      = round(avg_win_r, 3),
            avg_loss_r     = round(avg_loss_r, 3),
            profit_factor  = round(pf, 3),
            current_streak = streak,
        )

    @staticmethod
    def _calc_streak(entries: List[Dict]) -> int:
        """
        Most-recent consecutive run: +N = N wins, -N = N losses.
        Entries must be in chronological order.
        """
        streak = 0
        for entry in reversed(entries):
            outcome = entry.get("outcome")
            if outcome == "win":
                if streak >= 0: streak += 1
                else: break
            elif outcome == "loss":
                if streak <= 0: streak -= 1
                else: break
            else:
                break
        return streak

    # ── Query API (called by Nietzsche engine) ────────────────────────────────

    def get_win_rate(self, personality: str) -> float:
        """Persistent win rate for `personality`. Returns 0.5 if unknown."""
        stats = self._personality_stats.get(personality.upper())
        return stats.win_rate if stats else 0.50

    def get_streaks(self, personality: str) -> tuple[int, int]:
        """Returns (win_streak, loss_streak) for `personality`."""
        stats = self._personality_stats.get(personality.upper())
        if stats:
            return stats.win_streak, stats.loss_streak
        return 0, 0

    def get_personality_stats(self, personality: str) -> Optional[PersonalityStats]:
        return self._personality_stats.get(personality.upper())

    def get_all_stats(self) -> Dict[str, PersonalityStats]:
        return dict(self._personality_stats)

    def get_session_stats(self) -> Dict[str, Dict]:
        """Return session-level (since last restart) stats per personality."""
        return dict(self._session_stats)

    @property
    def recovery_mode(self) -> bool:
        """True when overall win rate < 50% based on journal data."""
        return self._recovery_mode

    def compute(self, journal: TradeJournal) -> PerformanceStats:
        """
        Compute performance statistics from journal.
        """
        entries = journal.get_all()
        closed_entries = journal.get_closed()
        open_entries = journal.get_open()
        
        # Basic counts
        total_trades = len(entries)
        open_trades = len(open_entries)
        closed_trades = len(closed_entries)
        
        def _pnl(e: dict) -> float:
            """Safe pnl extractor — returns 0.0 for missing or null values."""
            v = e.get("pnl_net_usd")
            if v is None:
                v = e.get("pnl_usd")
            return float(v) if v is not None else 0.0

        # Win rate (v1.3 uses Net P&L)
        wins = [e for e in closed_entries if _pnl(e) > 0]
        win_rate = len(wins) / closed_trades if closed_trades > 0 else 0.0

        # Profit factor
        winning_pnl = sum(_pnl(e) for e in wins)
        losing_pnl = sum(_pnl(e) for e in closed_entries if _pnl(e) < 0)
        profit_factor = abs(winning_pnl / losing_pnl) if losing_pnl != 0 else float('inf') if winning_pnl > 0 else 0.0

        # Average R-multiple
        r_multiples = [float(e["pnl_r"]) for e in closed_entries if e.get("pnl_r") is not None]
        avg_r = sum(r_multiples) / len(r_multiples) if r_multiples else 0.0

        # Total P&L (v1.3 Total Net P&L)
        total_pnl_usd = sum(_pnl(e) for e in closed_entries)

        # Max drawdown
        max_drawdown_pct = self._calculate_max_drawdown(closed_entries)

        # Current streak
        current_streak = self._calculate_current_streak(closed_entries)

        # Best/worst trades
        if r_multiples:
            best_trade_r = max(r_multiples)
            worst_trade_r = min(r_multiples)
        else:
            best_trade_r = 0.0
            worst_trade_r = 0.0

        # Average hold time
        hold_times = [e.get("hold_time_ms", 0) for e in closed_entries if e.get("hold_time_ms") is not None]
        avg_hold_time_h = sum(hold_times) / len(hold_times) / (1000 * 60 * 60) if hold_times else 0.0

        # System Quality Number (SQN)
        if len(r_multiples) > 1:
            avg_r_val = sum(r_multiples) / len(r_multiples)
            variance = sum((r - avg_r_val) ** 2 for r in r_multiples) / (len(r_multiples) - 1)
            std_r = sqrt(variance)
            sqn = (avg_r_val / std_r) * sqrt(len(r_multiples)) if std_r > 0 else 0.0
        else:
            sqn = 0.0

        # Signal-specific stats
        sweep_trades = [e for e in closed_entries if e.get("sweep") in ["buy_side", "sell_side"]]
        sweep_wins = [e for e in sweep_trades if _pnl(e) > 0]
        sweep_win_rate = len(sweep_wins) / len(sweep_trades) if sweep_trades else 0.0

        divergence_trades = [e for e in closed_entries if e.get("divergence") not in ["none", "neutral"]]
        divergence_wins = [e for e in divergence_trades if _pnl(e) > 0]
        divergence_win_rate = len(divergence_wins) / len(divergence_trades) if divergence_trades else 0.0
        
        # By symbol stats
        by_symbol = self._compute_by_symbol_stats(closed_entries)
        
        # By regime stats
        by_regime = self._compute_by_regime_stats(closed_entries)
        
        return PerformanceStats(
            total_trades=total_trades,
            open_trades=open_trades,
            closed_trades=closed_trades,
            win_rate=win_rate,
            profit_factor=profit_factor,
            avg_r=avg_r,
            total_pnl_usd=total_pnl_usd,
            max_drawdown_pct=max_drawdown_pct,
            current_streak=current_streak,
            best_trade_r=best_trade_r,
            worst_trade_r=worst_trade_r,
            avg_hold_time_h=avg_hold_time_h,
            sqn=sqn,
            sweep_win_rate=sweep_win_rate,
            divergence_win_rate=divergence_win_rate,
            by_symbol=by_symbol,
            by_regime=by_regime
        )
    
    def _calculate_max_drawdown(self, closed_entries: List[Dict]) -> float:
        """
        Walk through equity curve to find max drawdown.
        """
        if not closed_entries:
            return 0.0
        
        # Sort by closing time
        sorted_entries = sorted(closed_entries, key=lambda x: x.get("closed_at_ms", 0))
        
        equity_curve = []
        running_pnl = 0.0
        
        for entry in sorted_entries:
            running_pnl += entry.get("pnl_net_usd", entry.get("pnl_usd", 0))
            equity_curve.append(running_pnl)
        
        if not equity_curve:
            return 0.0
        
        # Calculate drawdown
        peak = equity_curve[0]
        max_drawdown = 0.0
        
        for equity in equity_curve:
            if equity > peak:
                peak = equity
            
            drawdown = (peak - equity) / peak if peak != 0 else 0.0
            max_drawdown = max(max_drawdown, drawdown)
        
        return max_drawdown * 100  # Convert to percentage
    
    def _calculate_current_streak(self, closed_entries: List[Dict]) -> int:
        """
        Count consecutive wins (positive) or losses (negative) from most recent.
        """
        if not closed_entries:
            return 0
        
        # Sort by closing time, get most recent
        sorted_entries = sorted(closed_entries, key=lambda x: x.get("closed_at_ms", 0), reverse=True)
        
        streak = 0
        for entry in sorted_entries:
            pnl = entry.get("pnl_usd", 0)
            if pnl > 0:
                if streak >= 0:  # Continuing or starting win streak
                    streak += 1
                else:  # Switching from loss to win
                    streak = 1
                    break
            elif pnl < 0:
                if streak <= 0:  # Continuing or starting loss streak
                    streak -= 1
                else:  # Switching from win to loss
                    streak = -1
                    break
            else:  # Break even, stop counting
                break
        
        return streak
    
    def _compute_by_symbol_stats(self, closed_entries: List[Dict]) -> Dict[str, Dict[str, float]]:
        """
        Compute statistics per symbol.
        """
        by_symbol = {}
        
        for entry in closed_entries:
            symbol = entry.get("symbol", "UNKNOWN")
            pnl = entry.get("pnl_net_usd", entry.get("pnl_usd", 0))

            if symbol not in by_symbol:
                by_symbol[symbol] = {"trades": 0, "wins": 0, "pnl": 0.0}

            by_symbol[symbol]["trades"] += 1
            by_symbol[symbol]["pnl"] += pnl
            if pnl > 0:
                by_symbol[symbol]["wins"] += 1
        
        return by_symbol
    
    def _compute_by_regime_stats(self, closed_entries: List[Dict]) -> Dict[str, Dict[str, float]]:
        """
        Compute statistics per market regime.
        """
        by_regime = {}
        
        for entry in closed_entries:
            regime = entry.get("regime", "unknown")
            pnl = entry.get("pnl_net_usd", entry.get("pnl_usd", 0))

            if regime not in by_regime:
                by_regime[regime] = {"trades": 0, "wins": 0, "pnl": 0.0}

            by_regime[regime]["trades"] += 1
            by_regime[regime]["pnl"] += pnl
            if pnl > 0:
                by_regime[regime]["wins"] += 1

        return by_regime

    def get_optimal_min_coherence(self, journal) -> float:
        """
        After 50+ closed trades, computes optimal minimum coherence threshold
        by finding the score band with highest positive expectancy.
        Returns the recommended min_coherence float.
        """
        closed = journal.get_closed()
        if len(closed) < 50:
            return 4.0  # Default before enough data

        bands = {}
        for entry in closed:
            score = entry.get("coherence_score", 0.0)
            pnl = entry.get("pnl_net_usd", entry.get("pnl_usd", 0.0))
            band = round(score * 2) / 2  # round to nearest 0.5
            if band not in bands:
                bands[band] = []
            bands[band].append(pnl)

        best_band = 4.0
        best_expectancy = 0.0
        for band, pnls in sorted(bands.items()):
            if len(pnls) < 5:
                continue
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p < 0]
            win_rate = len(wins) / len(pnls)
            avg_win = sum(wins) / len(wins) if wins else 0
            avg_loss = abs(sum(losses) / len(losses)) if losses else 0.01
            expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
            if expectancy > best_expectancy:
                best_expectancy = expectancy
                best_band = band

        return max(3.5, best_band)
