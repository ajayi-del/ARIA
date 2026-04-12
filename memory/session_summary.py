"""
Session Summary

Generates end-of-session report.
Called when ARIA shuts down (Ctrl+C).
"""

import json
from datetime import datetime, timezone
from typing import Dict, Any
from pathlib import Path
import dataclasses
from .trade_journal import TradeJournal
from .performance import PerformanceStats

class ARIAJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if dataclasses.is_dataclass(obj):
            return dataclasses.asdict(obj)
        if hasattr(obj, 'model_dump'):
            return obj.model_dump()
        if hasattr(obj, '__dict__'):
            return obj.__dict__
        return super().default(obj)


class SessionSummary:
    """
    Generates end-of-session report.
    """
    
    def __init__(self, log_dir: str = "./logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
    
    def generate(
        self,
        journal: TradeJournal,
        stats: PerformanceStats,
        session_start_ms: int
    ) -> Dict[str, Any]:
        """
        Generate session summary.
        """
        entries = journal.get_all()
        session_start = datetime.fromtimestamp(session_start_ms / 1000, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        
        # Calculate session duration
        duration_ms = int(now.timestamp() * 1000) - session_start_ms
        duration_hours = duration_ms / (1000 * 60 * 60)
        
        # Decision statistics
        evaluated = len(entries)
        approved = len([e for e in entries if e.get("approved", False)])
        rejected = evaluated - approved
        
        # Rejection reasons
        rejection_reasons = {}
        for entry in entries:
            if not entry.get("approved", False) and entry.get("reject_reason"):
                reason = entry["reject_reason"]
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
        
        top_reject_reason = max(rejection_reasons.keys(), key=lambda k: rejection_reasons[k]) if rejection_reasons else "None"
        
        # Trade statistics
        placed = approved  # Approved trades should be placed
        closed = len(journal.get_closed())
        open_trades = len(journal.get_open())
        
        # Session P&L
        session_pnl = sum(e.get("pnl_usd", 0) for e in journal.get_closed())
        initial_capital = 200.0  # Default starting capital
        session_pnl_pct = (session_pnl / initial_capital) * 100 if initial_capital > 0 else 0.0
        
        # Most active symbol
        symbol_counts = {}
        for entry in entries:
            if entry.get("approved", False):
                symbol = entry.get("symbol", "UNKNOWN")
                symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        most_active_symbol = max(symbol_counts.keys(), key=lambda k: symbol_counts[k]) if symbol_counts else "None"
        
        # Coherence distribution
        coherence_dist = {}
        for entry in entries:
            coherence = str(entry.get("coherence_score", 0))
            coherence_dist[coherence] = coherence_dist.get(coherence, 0) + 1
        
        # Signal breakdown
        signal_breakdown = {
            "sweep_trades": len([e for e in entries if e.get("sweep") in ["buy_side", "sell_side"]]),
            "divergence_trades": len([e for e in entries if e.get("divergence") not in ["none", "neutral"]]),
            "mag_active_sessions": len([e for e in entries if e.get("mag_active", False)])
        }
        
        return {
            "session_id": f"session_{int(now.timestamp())}",
            "date": now.strftime("%Y-%m-%d"),
            "duration_hours": round(duration_hours, 2),
            "trades_evaluated": evaluated,
            "trades_approved": approved,
            "trades_rejected": rejected,
            "rejection_reasons": rejection_reasons,
            "top_reject_reason": top_reject_reason,
            "trades_placed": placed,
            "trades_closed": closed,
            "session_pnl": round(session_pnl, 2),
            "session_pnl_pct": round(session_pnl_pct, 2),
            "most_active_symbol": most_active_symbol,
            "coherence_distribution": coherence_dist,
            "signal_breakdown": signal_breakdown,
            "performance": stats.to_dict()
        }
    
    def save(self, summary: Dict[str, Any]) -> None:
        """
        Saves to logs/session_{date}_{time}.json
        """
        now = datetime.now(timezone.utc)
        filename = f"session_{now.strftime('%Y-%m-%d_%H-%M-%S')}.json"
        filepath = self.log_dir / filename
        
        with open(filepath, 'w') as f:
            json.dump(summary, f, indent=2, cls=ARIAJSONEncoder)
    
    def print_to_terminal(self, summary: Dict[str, Any]) -> None:
        """
        Prints formatted summary to stdout.
        """
        print("\n" + "="*50)
        print("ARIA SESSION SUMMARY")
        print("="*50)
        print(f"Date:           {summary['date']}")
        print(f"Duration:       {summary['duration_hours']}h")
        print()
        
        print("DECISIONS")
        print(f"Evaluated:      {summary['trades_evaluated']}")
        print(f"Approved:       {summary['trades_approved']}")
        print(f"Rejected:       {summary['trades_rejected']}")
        print(f"Top reject:     {summary['top_reject_reason']}")
        print()
        
        print("TRADES")
        print(f"Placed:         {summary['trades_placed']}")
        print(f"Closed:         {summary['trades_closed']}")
        print(f"Open:           {summary['trades_placed'] - summary['trades_closed']}")
        print()
        
        print("PERFORMANCE")
        pnl_color = "+" if summary['session_pnl'] >= 0 else ""
        print(f"Session P&L:    {pnl_color}${summary['session_pnl']:.2f} ({pnl_color}{summary['session_pnl_pct']:.2f}%)")
        perf_data = summary['performance']
        print(f"Win Rate:       {perf_data.get('win_rate', 0.0):.1%}")
        print(f"Avg R:          {perf_data.get('avg_r', 0.0):.1f}R")
        print(f"Profit Factor:  {perf_data.get('profit_factor', 0.0):.1f}")
        print()
        
        print("SIGNALS")
        signals = summary['signal_breakdown']
        print(f"Sweeps fired:   {signals['sweep_trades']}")
        print(f"Divergence:     {signals['divergence_trades']}")
        print(f"MAG active:     {signals['mag_active_sessions']}")
        
        # v1.2 Calibration Table
        if 'calibration_table' in summary:
            print(summary['calibration_table'])
            if 'calibration_recommendation' in summary:
                rec = summary['calibration_recommendation']
                print(f"RECOMMENDATION: Set min score to {rec['recommended_minimum']} ({rec['confidence']} confidence)")
                if rec.get('potential_missed_trades', 0) > 0:
                    print(f"Missed trades at this score: {rec['potential_missed_trades']}")
        
        print("="*50)

    def add_calibration(self, summary: Dict[str, Any], journal: TradeJournal):
        """
        Calculates and adds calibration data to summary.
        """
        from .calibration import CoherenceCalibrator
        calibrator = CoherenceCalibrator()
        closed_entries = journal.get_closed()
        
        if len(closed_entries) >= 5: # Lower threshold for session summary specifically
            summary['calibration_table'] = calibrator.score_performance_table(closed_entries)
            rec = calibrator.recommend_minimum(closed_entries, 4) # Assume 4 is default
            if rec:
                summary['calibration_recommendation'] = rec
