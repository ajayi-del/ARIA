"""
Trade Journal

Logs every execution decision ARIA makes.
Persists to JSON file in logs/ directory.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from pathlib import Path


class TradeJournal:
    """
    Logs every execution decision ARIA makes
    whether approved or rejected.
    """
    
    def __init__(self, log_dir: str = "./logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        
        self.entries: List[Dict[str, Any]] = []
        self._current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._journal_file = self.log_dir / f"trade_journal_{self._current_date}.json"
        
    def log_decision(
        self,
        state: Any,  # MarketState
        candidate: Any,  # TradeCandidate
        approved: bool,
        reason: str,
        cal_state: Any = None # CalendarState
    ) -> str:
        """
        Creates entry, saves to file.
        Returns entry_id.
        """
        entry_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        
        entry = {
            "entry_id": entry_id,
            "timestamp_ms": int(now.timestamp() * 1000),
            "timestamp_iso": now.isoformat(),
            "symbol": state.symbol if hasattr(state, 'symbol') else "UNKNOWN",
            "direction": candidate.side if hasattr(candidate, 'side') else "none",
            "coherence_score": state.weighted_score if hasattr(state, 'weighted_score') else (state.coherence_score if hasattr(state, 'coherence_score') else 0),
            "raw_score": state.raw_score if hasattr(state, 'raw_score') else (state.coherence_score if hasattr(state, 'coherence_score') else 0),
            "size_multiplier": state.size_multiplier if hasattr(state, 'size_multiplier') else 0.0,
            
            # v1.2 Quant Fields
            "cluster_validated": state.cluster_validated if hasattr(state, 'cluster_validated') else False,
            "cluster_strength": state.cluster_strength if hasattr(state, 'cluster_strength') else 0.0,
            "ostium_lead_active": state.ostium_lead_active if hasattr(state, 'ostium_lead_active') else False,
            "cross_venue_funding": state.cross_venue_funding if hasattr(state, 'cross_venue_funding') else "none",
            "market_hours_gate": state.market_hours_gate if hasattr(state, 'market_hours_gate') else True,
            "golden_stop_used": False,
            "golden_stop_price": None,
            "tp1_level_stop_used": False,

            # Signal states at time of decision
            "macro_bias": state.macro_bias if hasattr(state, 'macro_bias') else "unknown",
            "regime": state.regime if hasattr(state, 'regime') else "unknown",
            "market_type": state.market_type if hasattr(state, 'market_type') else "unknown",
            "sweep": state.sweep if hasattr(state, 'sweep') else "none",
            "reclaim": state.reclaim if hasattr(state, 'reclaim') else False,
            "imbalance": state.imbalance if hasattr(state, 'imbalance') else 0.0,
            "divergence": state.divergence if hasattr(state, 'divergence') else "none",
            "funding_class": state.funding_class if hasattr(state, 'funding_class') else "neutral",
            "mag_active": state.mag_active if hasattr(state, 'mag_active') else False,
            
            # v1.3 Calendar Fields
            "calendar_regime": cal_state.regime if cal_state else "unknown",
            "calendar_size_mult": cal_state.size_multiplier if cal_state else 1.0,
            "calendar_stop_mult": cal_state.stop_atr_multiplier if cal_state else 1.0,
            "calendar_event_type": cal_state.nearest_event_type if cal_state else None,
            "calendar_hours_to_event": cal_state.hours_to_event if cal_state else None,
            "calendar_reason": cal_state.reason if cal_state else "not_provided",
            
            # v1.3 Unified Multiplier Chain
            "coherence_mult": getattr(state, "coherence_mult", 1.0),
            "freshness_mult": getattr(state, "freshness_mult", 1.0),
            "calendar_mult": getattr(state, "calendar_mult", 1.0),
            "allocation_mult": getattr(state, "allocation_mult", 1.0),
            
            # v1.3 Quant Fix Metadata
            "slippage_expected_usd": getattr(state, "slippage_expected_usd", 0.0),
            "funding_cost_est_usd": getattr(state, "funding_cost_est_usd", 0.0),
            
            # Execution result
            "approved": approved,
            "reject_reason": reason if not approved else None,
            
            # If approved and placed:
            "entry_price": candidate.entry_price if approved else None,
            "stop_price": candidate.stop_price if approved else None,
            "tp1_price": candidate.tp1_price if approved else None,
            "tp2_price": candidate.tp2_price if approved else None,
            "tp3_price": candidate.tp3_price if approved else None,
            "position_size": candidate.size if approved else None,
            "initial_margin": candidate.initial_margin if approved else None,
            "leverage": candidate.leverage if approved else None,
            
            # Outcome (filled in when trade closes):
            "outcome": None,
            "pnl_usd": None,
            "pnl_net_usd": None, # New: pnl + funding
            "pnl_r": None,
            "hold_time_ms": None,
            "closed_at_ms": None
        }
        
        self.entries.append(entry)
        self.save()
        
        return entry_id
    
    def update_outcome(
        self,
        entry_id: str,
        outcome: str,
        pnl_usd: Optional[float],
        closed_at_ms: Optional[int],
        pnl_net_usd: Optional[float] = None
    ) -> None:
        """
        Finds entry by ID, updates outcome fields.
        Rewrites journal file.
        """
        for entry in self.entries:
            if entry["entry_id"] == entry_id:
                entry["outcome"] = outcome
                entry["pnl_usd"] = pnl_usd
                entry["pnl_net_usd"] = pnl_net_usd if pnl_net_usd is not None else pnl_usd
                entry["closed_at_ms"] = closed_at_ms
                
                # Calculate R-multiple if we have P&L and initial margin
                # Using net P&L for R-multiple in v1.3
                target_pnl = entry["pnl_net_usd"]
                if target_pnl is not None and entry.get("initial_margin"):
                    entry["pnl_r"] = target_pnl / entry["initial_margin"]
                
                # Calculate hold time
                if closed_at_ms is not None:
                    entry["hold_time_ms"] = closed_at_ms - entry["timestamp_ms"]
                
                self.save()
                return
        
        raise ValueError(f"Entry ID {entry_id} not found in journal")
    
    def get_all(self) -> List[Dict[str, Any]]:
        """
        Returns all entries from journal file.
        """
        return self.entries.copy()
    
    def get_open(self) -> List[Dict[str, Any]]:
        """
        Returns entries where outcome is None or "open".
        """
        return [
            entry for entry in self.entries
            if entry.get("outcome") is None or entry.get("outcome") == "open"
        ]
    
    def get_closed(self) -> List[Dict[str, Any]]:
        """
        Returns entries with real outcome.
        """
        return [
            entry for entry in self.entries
            if entry.get("outcome") not in [None, "open"]
        ]
    
    def save(self) -> None:
        """
        Writes journal to logs/trade_journal_{date}.json
        """
        # Check if we need to rotate to a new date file
        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if current_date != self._current_date:
            self._current_date = current_date
            self._journal_file = self.log_dir / f"trade_journal_{self._current_date}.json"
        
        with open(self._journal_file, 'w') as f:
            json.dump(self.entries, f, indent=2)
    
    def load(self) -> None:
        """
        Reads existing journal from logs/ dir.
        Merges with current session entries.
        """
        # Load today's journal if it exists
        if self._journal_file.exists():
            try:
                with open(self._journal_file, 'r') as f:
                    self.entries = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                self.entries = []
        
        # Also try to load previous days' entries for reference
        # (but don't merge them into main entries to avoid confusion)
        pass
