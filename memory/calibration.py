import numpy as np
import structlog
from typing import Dict, List, Any, Optional

logger = structlog.get_logger(__name__)

class CoherenceCalibrator:
    """
    Analyzes historical trade performance grouped by coherence scores.
    Recommends optimal signal filter thresholds.
    """
    
    def __init__(self):
        pass
        
    def compute_breakeven_score(self, journal_entries: List[Dict[str, Any]]) -> Optional[int]:
        """
        Calculates the minimum coherence score that yields positive expectation.
        Requires at least 50 closed trades for valid statistics.
        """
        closed_trades = [e for e in journal_entries if e.get("outcome") not in [None, "open"]]
        
        if len(closed_trades) < 50:
            return None
            
        # Group performance by score
        score_performance = {}
        for trade in closed_trades:
            score = trade.get("coherence_score")
            if score is None: continue
            
            if score not in score_performance:
                score_performance[score] = {"pnl_r": [], "wins": 0, "total": 0}
            
            pnl_r = trade.get("pnl_r", 0)
            score_performance[score]["pnl_r"].append(pnl_r)
            score_performance[score]["total"] += 1
            if pnl_r > 0:
                score_performance[score]["wins"] += 1
                
        # Find lowest profitable score with win rate > 45%
        # Check scores from low to high (e.g., 3 to 7)
        for score in sorted(score_performance.keys()):
            perf = score_performance[score]
            avg_pnl = np.mean(perf["pnl_r"])
            win_rate = perf["wins"] / perf["total"]
            
            if avg_pnl > 0 and win_rate > 0.45:
                return int(score)
                
        return None

    def recommend_minimum(self, journal_entries: List[Dict[str, Any]], current_min: int) -> Optional[Dict[str, Any]]:
        """
        Returns a structured recommendation for the operator.
        """
        calibrated_score = self.compute_breakeven_score(journal_entries)
        if calibrated_score is None:
            return None
            
        closed_count = len([e for e in journal_entries if e.get("outcome") not in [None, "open"]])
        
        confidence = "low"
        if closed_count > 100: confidence = "high"
        elif closed_count > 60: confidence = "medium"
        
        # Calculate potential missed trades if calibrated is lower than current
        missed_profitable = 0
        if calibrated_score < current_min:
            missed_profitable = len([e for e in journal_entries if e.get("coherence_score") == calibrated_score])
            
        evidence = f"Score {calibrated_score} trades show positive profitability over {closed_count} samples."
        
        return {
            "recommended_minimum": calibrated_score,
            "confidence": confidence,
            "sample_size": closed_count,
            "evidence": evidence,
            "potential_missed_trades": missed_profitable if calibrated_score < current_min else 0
        }

    def score_performance_table(self, journal_entries: List[Dict[str, Any]]) -> str:
        """
        Generates a formatted text table of alignment between score and P&L.
        """
        closed_trades = [e for e in journal_entries if e.get("outcome") not in [None, "open"]]
        if not closed_trades:
            return "No historical data for calibration."
            
        score_data = {}
        for trade in closed_trades:
            s = trade.get("coherence_score", 0)
            if s not in score_data:
                score_data[s] = {"wins": 0, "count": 0, "total_r": 0.0}
            score_data[s]["count"] += 1
            score_data[s]["total_r"] += trade.get("pnl_r", 0)
            if trade.get("pnl_r", 0) > 0:
                score_data[s]["wins"] += 1
                
        header = "COHERENCE CALIBRATION ({})".format(len(closed_trades))
        table = f"\n{header}\n"
        table += "{:<6} | {:<6} | {:<5} | {:<6} | {:<10}\n".format("Score", "Trades", "Win%", "Avg R", "Verdict")
        table += "-" * 45 + "\n"
        
        for s in sorted(score_data.keys()):
            d = score_data[s]
            win_pct = (d["wins"] / d["count"] * 100) if d["count"] > 0 else 0
            avg_r = (d["total_r"] / d["count"]) if d["count"] > 0 else 0
            verdict = "✓ PROFITABLE" if avg_r > 0 and win_pct > 45 else "✗ REJECT"
            
            table += "{:<6} | {:<6} | {:>3.0f}%  | {:>+4.1f}R | {:<10}\n".format(
                s, d["count"], win_pct, avg_r, verdict
            )
            
        return table
