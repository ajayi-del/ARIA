"""
Weekly Reviewer

Cold-path LLM analysis of trading journal.
NOT called during live trading.
Called manually or on weekly schedule.
"""

import json
import os
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List
from pathlib import Path
from anthropic import Anthropic
from .performance import PerformanceStats


class WeeklyReviewer:
    """
    Cold-path LLM analysis of trading journal.
    Only place Claude/LLM touches execution data.
    """
    
    def __init__(self, api_key: str, log_dir: str = "./logs"):
        self.api_key = api_key
        self.log_dir = Path(log_dir)
        self.client = Anthropic(api_key=api_key)
    
    async def generate_review(
        self,
        journal_path: str,
        stats: PerformanceStats
    ) -> str:
        """
        Generate weekly review using LLM.
        """
        # Load last 7 days of journal entries
        entries = self._load_recent_entries(journal_path, days=7)
        
        # Compute recent stats (last 7 days only)
        recent_stats = self._compute_recent_stats(entries)
        
        # Build review prompt
        prompt = self._build_review_prompt(recent_stats, entries)
        
        # Call Anthropic API
        try:
            response = await self.client.messages.create(
                model="claude-3-haiku-20240307",
                max_tokens=2000,
                messages=[{
                    "role": "user",
                    "content": prompt
                }]
            )
            
            review_text = response.content[0].text
            
            # Save to logs/weekly_review_{date}.md
            now = datetime.now(timezone.utc)
            filename = f"weekly_review_{now.strftime('%Y-%m-%d')}.md"
            filepath = self.log_dir / filename
            
            with open(filepath, 'w') as f:
                f.write(f"# ARIA Weekly Review - {now.strftime('%Y-%m-%d')}\n\n")
                f.write(review_text)
            
            return review_text
            
        except Exception as e:
            return f"Error generating review: {str(e)}"
    
    def run_review(self) -> None:
        """
        Entry point for manual review.
        """
        # Find latest trade journal file
        journal_files = list(self.log_dir.glob("trade_journal_*.json"))
        if not journal_files:
            print("No trade journal files found.")
            return
        
        latest_journal = max(journal_files, key=lambda x: x.stat().st_mtime)
        
        # Load entries and compute stats
        from .trade_journal import TradeJournal
        from .performance import PerformanceTracker
        
        journal = TradeJournal()
        journal.load()
        tracker = PerformanceTracker()
        stats = tracker.compute(journal)
        
        print("Generating weekly review...")
        
        # Run async review
        import asyncio
        review = asyncio.run(self.generate_review(str(latest_journal), stats))
        
        print("\n" + "="*60)
        print("WEEKLY REVIEW")
        print("="*60)
        print(review)
        print("="*60)
    
    def _load_recent_entries(self, journal_path: str, days: int = 7) -> List[Dict[str, Any]]:
        """
        Load last N days of journal entries.
        """
        try:
            with open(journal_path, 'r') as f:
                entries = json.load(f)
            
            # Filter by date
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
            cutoff_ms = int(cutoff_date.timestamp() * 1000)
            
            recent_entries = [
                entry for entry in entries
                if entry.get("timestamp_ms", 0) >= cutoff_ms
            ]
            
            return recent_entries
            
        except (FileNotFoundError, json.JSONDecodeError):
            return []
    
    def _compute_recent_stats(self, entries: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Compute stats for recent entries only.
        """
        if not entries:
            return {}
        
        closed_entries = [e for e in entries if e.get("outcome") not in [None, "open"]]
        
        wins = [e for e in closed_entries if e.get("pnl_usd", 0) > 0]
        total_pnl = sum(e.get("pnl_usd", 0) for e in closed_entries)
        
        # Signal breakdown
        sweep_trades = [e for e in closed_entries if e.get("sweep") in ["buy_side", "sell_side"]]
        divergence_trades = [e for e in closed_entries if e.get("divergence") not in ["none", "neutral"]]
        
        # Regime breakdown
        regime_stats = {}
        for entry in closed_entries:
            regime = entry.get("regime", "unknown")
            pnl = entry.get("pnl_usd", 0)
            
            if regime not in regime_stats:
                regime_stats[regime] = {"trades": 0, "wins": 0, "pnl": 0.0}
            
            regime_stats[regime]["trades"] += 1
            regime_stats[regime]["pnl"] += pnl
            if pnl > 0:
                regime_stats[regime]["wins"] += 1
        
        return {
            "total_trades": len(closed_entries),
            "win_rate": len(wins) / len(closed_entries) if closed_entries else 0,
            "total_pnl": total_pnl,
            "sweep_trades": len(sweep_trades),
            "divergence_trades": len(divergence_trades),
            "regime_stats": regime_stats
        }
    
    def _build_review_prompt(self, stats: Dict[str, Any], entries: List[Dict[str, Any]]) -> str:
        """
        Build review prompt for LLM.
        """
        # Get last 50 closed trades
        closed_entries = [e for e in entries if e.get("outcome") not in [None, "open"]]
        recent_trades = closed_entries[-50:] if len(closed_entries) > 50 else closed_entries
        
        prompt = f"""You are reviewing the trading journal of ARIA, an autonomous trading system for SoDEX perps.

Here are the last 7 days of trading statistics:
- Total trades: {stats.get('total_trades', 0)}
- Win rate: {stats.get('win_rate', 0):.1%}
- Total P&L: ${stats.get('total_pnl', 0):.2f}
- Sweep trades: {stats.get('sweep_trades', 0)}
- Divergence trades: {stats.get('divergence_trades', 0)}

Regime performance:
{self._format_regime_stats(stats.get('regime_stats', {}))}

Here are the last {len(recent_trades)} closed trades:
{json.dumps(recent_trades, indent=2)}

Analyze the trading data and provide specific insights:

1. Which signal combinations produced the best R-multiple outcomes?
2. Which market regimes had the worst win rate?
3. Are there patterns in rejected trades that might indicate overly strict gates?
4. What is the weakest signal in the current setup?
5. What should be adjusted to improve profit factor above 1.5?

Be specific. Reference actual trade data. Do not give generic advice. Focus on actionable insights for improving the autonomous system."""
        
        return prompt
    
    def _format_regime_stats(self, regime_stats: Dict[str, Dict[str, Any]]) -> str:
        """
        Format regime statistics for prompt.
        """
        if not regime_stats:
            return "No regime data available."
        
        lines = []
        for regime, stats in regime_stats.items():
            win_rate = stats["wins"] / stats["trades"] if stats["trades"] > 0 else 0
            lines.append(f"- {regime}: {stats['trades']} trades, {win_rate:.1%} win rate, ${stats['pnl']:.2f} P&L")
        
        return "\n".join(lines)
