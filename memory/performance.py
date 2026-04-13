"""
Performance Tracker

Computes real-time statistics from the trade journal.
Updates after every closed trade.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional
from math import sqrt
from .trade_journal import TradeJournal


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


class PerformanceTracker:
    """
    Computes real-time statistics from the trade journal.
    Updates after every closed trade.
    """
    
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
