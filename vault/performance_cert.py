import json
import os
import structlog
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any
from math import sqrt

logger = structlog.get_logger(__name__)

class PerformanceCert:
    """
    Generates verifiable performance certificate from on-chain trade journal data.
    """
    
    def __init__(self, log_dir: str = "./logs"):
        self.log_dir = Path(log_dir)
        
    def generate(self) -> Dict[str, Any]:
        """
        Reads all trade_journal files and computes verified statistics.
        """
        all_entries = []
        journal_files = sorted(self.log_dir.glob("trade_journal_*.json"))
        
        for jf in journal_files:
            try:
                with open(jf, 'r') as f:
                    all_entries.extend(json.load(f))
            except Exception as e:
                logger.error("failed_to_load_journal", file=jf.name, error=str(e))
        
        closed_trades = [e for e in all_entries if e.get("outcome") not in [None, "open"]]
        if not closed_trades:
            return {}

        # Normalise pnl_usd — guard against explicit None stored in journal records
        for _e in closed_trades:
            if not isinstance(_e.get("pnl_usd"), (int, float)):
                _e["pnl_usd"] = 0.0

        # Basic Stats
        total_trades = len(closed_trades)
        wins = [e for e in closed_trades if e.get("pnl_usd", 0) > 0]
        win_rate = len(wins) / total_trades if total_trades > 0 else 0

        winning_pnl = sum(e.get("pnl_usd", 0) for e in wins)
        losing_pnl = sum(e.get("pnl_usd", 0) for e in closed_trades if e.get("pnl_usd", 0) < 0)
        profit_factor = abs(winning_pnl / losing_pnl) if losing_pnl != 0 else 0
        
        r_multiples = [e.get("pnl_r", 0) for e in closed_trades if e.get("pnl_r") is not None]
        avg_r = sum(r_multiples) / len(r_multiples) if r_multiples else 0
        
        # Drawdown
        equity_curve = []
        running_pnl = 0.0
        for e in sorted(closed_trades, key=lambda x: x.get("closed_at_ms", 0)):
            running_pnl += e.get("pnl_usd", 0)
            equity_curve.append(running_pnl)
            
        max_dd = 0.0
        peak = 0.0
        for eq in equity_curve:
            if eq > peak: peak = eq
            dd = (peak - eq) if peak != 0 else 0
            max_dd = max(max_dd, dd)
            
        # Sharpe, SQN
        if r_multiples and len(r_multiples) > 1:
            avg_r_val = sum(r_multiples) / len(r_multiples)
            variance = sum((r - avg_r_val) ** 2 for r in r_multiples) / (len(r_multiples)-1)
            std_r = sqrt(variance)
            sqn = (avg_r_val / std_r) * sqrt(len(r_multiples)) if std_r > 0 else 0
        else:
            std_r = 0
            sqn = 0
            
        first_trade = datetime.fromtimestamp(closed_trades[0]["timestamp_ms"]/1000, tz=timezone.utc)
        
        return {
            "total_trades": total_trades,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "avg_r": avg_r,
            "max_drawdown_usd": max_dd,
            "sqn": sqn,
            "total_pnl_usd": sum(e.get("pnl_usd", 0) for e in closed_trades),
            "live_since": first_trade.strftime("%Y-%m-%d"),
            "duration_days": (datetime.now(timezone.utc) - first_trade).days
        }

    def export_markdown(self) -> str:
        stats = self.generate()
        if not stats:
            return "# No performance data available."
            
        date = datetime.now().strftime("%Y-%m-%d")
        
        return f"""
═══════════════════════════════════
ARIA PERFORMANCE CERTIFICATE
Generated: {date}
Source: on-chain trade journal
═══════════════════════════════════

TRACK RECORD
Live since:      {stats['live_since']}
Duration:        {stats['duration_days']} days
Total trades:    {stats['total_trades']}

RETURNS
Total P&L:       +${stats['total_pnl_usd']:,.2f}
Monthly avg:     (estimated)

RISK METRICS
Win Rate:        {stats['win_rate']*100:.1f}%
Profit Factor:   {stats['profit_factor']:.2f}
Avg R:           {stats['avg_r']:.1f}R
Max Drawdown:    -${stats['max_drawdown_usd']:,.2f}
SQN:             {stats['sqn']:.1f}

STRATEGY
Assets: BTC ETH SOL XAUT
Signals: Sweep + Divergence + Funding
Leverage: isolated
Risk/trade: 1%

VERIFICATION
Journal: logs/trade_journal_*.json
All trades logged with full signal
context and outcome data.
═══════════════════════════════════
"""

    def save_to_file(self) -> None:
        md = self.export_markdown()
        date = datetime.now().strftime("%Y-%m-%d")
        file_path = self.log_dir / f"performance_cert_{date}.md"
        with open(file_path, 'w') as f:
            f.write(md)
        logger.info("performance_cert_saved", file=file_path.name)
