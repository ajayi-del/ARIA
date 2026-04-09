"""
Trade Journal

Logs every execution decision ARIA makes.
Persists to JSON file in logs/ directory.
v1.3 Hardened: Uses non-blocking write queue to prevent IO-bound races.
"""

import os
import json
import uuid
import asyncio
import aiofiles
import structlog
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from pathlib import Path

logger = structlog.get_logger(__name__)

class TradeJournal:
    """
    Logs every execution decision ARIA makes
    whether approved or rejected.
    Uses an internal asyncio.Queue to ensure non-blocking disk writes.
    """
    
    def __init__(self, log_dir: str = "./logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        
        self.entries: List[Dict[str, Any]] = []
        self._current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._journal_file = self.log_dir / f"trade_journal_{self._current_date}.json"
        
        # v1.3 Write Queue
        self._write_queue = asyncio.Queue()
        self._is_active = True
        self._writer_task: Optional[asyncio.Task] = None
        
    def start_writer(self):
        """Starts the background writer task."""
        if self._writer_task is None:
            self._writer_task = asyncio.create_task(self._write_loop())
            logger.info("trade_journal_writer_started")

    async def stop_writer(self):
        """Gracefully stops the writer, ensuring all pending writes are flushed."""
        self._is_active = False
        await self._write_queue.put("FLUSH") # Signal final flush
        if self._writer_task:
            await self._writer_task
            self._writer_task = None
        logger.info("trade_journal_writer_stopped")

    def log_decision(
        self,
        state: Any,  # MarketState
        candidate: Any,  # TradeCandidate
        approved: bool,
        reason: str,
        cal_state: Any = None # CalendarState
    ) -> str:
        """
        Creates entry, puts in write queue.
        Returns entry_id.
        """
        entry_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        
        entry = {
            "entry_id": entry_id,
            "timestamp_ms": int(now.timestamp() * 1000),
            "timestamp_iso": now.isoformat(),
            "symbol": getattr(state, 'symbol', "UNKNOWN"),
            "direction": getattr(candidate, 'side', "none"),
            "coherence_score": getattr(state, 'weighted_score', getattr(state, 'coherence_score', 0)),
            "raw_score": getattr(state, 'raw_score', getattr(state, 'coherence_score', 0)),
            "size_multiplier": getattr(state, 'size_multiplier', 0.0),
            
            # v1.2 Quant Fields
            "cluster_validated": getattr(state, 'cluster_validated', False),
            "cluster_strength": getattr(state, 'cluster_strength', 0.0),
            "ostium_lead_active": getattr(state, 'ostium_lead_active', False),
            "cross_venue_funding": getattr(state, 'cross_venue_funding', "none"),
            "market_hours_gate": getattr(state, 'market_hours_gate', True),
            "golden_stop_used": False,
            "golden_stop_price": None,
            "tp1_level_stop_used": False,

            # Signal states at time of decision
            "macro_bias": getattr(state, 'macro_bias', "unknown"),
            "regime": getattr(state, 'regime', "unknown"),
            "market_type": getattr(state, 'market_type', "unknown"),
            "sweep": getattr(state, 'sweep', "none"),
            "reclaim": getattr(state, 'reclaim', False),
            "imbalance": getattr(state, 'imbalance', 0.0),
            "divergence": getattr(state, 'divergence', "none"),
            "funding_class": getattr(state, 'funding_class', "neutral"),
            "mag_active": getattr(state, 'mag_active', False),
            
            # v1.3 Calendar Fields
            "calendar_regime": getattr(cal_state, 'regime', "unknown") if cal_state else "unknown",
            "calendar_size_mult": getattr(cal_state, 'size_multiplier', 1.0) if cal_state else 1.0,
            "calendar_stop_mult": getattr(cal_state, 'stop_atr_multiplier', 1.0) if cal_state else 1.0,
            "calendar_event_type": getattr(cal_state, 'nearest_event_type', None) if cal_state else None,
            "calendar_hours_to_event": getattr(cal_state, 'hours_to_event', None) if cal_state else None,
            "calendar_reason": getattr(cal_state, 'reason', "not_provided") if cal_state else "not_provided",
            
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
            "entry_price": getattr(candidate, 'entry_price', None) if approved else None,
            "stop_price": getattr(candidate, 'stop_price', None) if approved else None,
            "tp1_price": getattr(candidate, 'tp1_price', None) if approved else None,
            "tp2_price": getattr(candidate, 'tp2_price', None) if approved else None,
            "tp3_price": getattr(candidate, 'tp3_price', None) if approved else None,
            "position_size": getattr(candidate, 'size', None) if approved else None,
            "initial_margin": getattr(candidate, 'initial_margin', None) if approved else None,
            "leverage": getattr(candidate, 'leverage', None) if approved else None,
            
            # Outcome (filled in when trade closes):
            "outcome": None,
            "pnl_usd": None,
            "pnl_net_usd": None, 
            "pnl_r": None,
            "hold_time_ms": None,
            "closed_at_ms": None
        }
        
        self.entries.append(entry)
        self.save_nonblocking()
        
        return entry_id
    
    def update_outcome(
        self,
        entry_id: str,
        outcome: str,
        pnl_usd: Optional[float],
        closed_at_ms: Optional[int],
        pnl_net_usd: Optional[float] = None
    ) -> None:
        """Finds entry, updates outcome, triggers non-blocking save."""
        for entry in self.entries:
            if entry["entry_id"] == entry_id:
                entry["outcome"] = outcome
                entry["pnl_usd"] = pnl_usd
                entry["pnl_net_usd"] = pnl_net_usd if pnl_net_usd is not None else pnl_usd
                entry["closed_at_ms"] = closed_at_ms
                
                target_pnl = entry["pnl_net_usd"]
                if target_pnl is not None and entry.get("initial_margin"):
                    entry["pnl_r"] = target_pnl / entry["initial_margin"]
                
                if closed_at_ms is not None:
                    entry["hold_time_ms"] = closed_at_ms - entry["timestamp_ms"]
                
                self.save_nonblocking()
                return
        
        logger.error("journal_entry_not_found", entry_id=entry_id)

    def save_nonblocking(self) -> None:
        """Pushes a 'SAVE' signal to the write queue."""
        if self._is_active:
            try:
                self._write_queue.put_nowait("SAVE")
            except asyncio.QueueFull:
                logger.warning("journal_write_queue_full")

    async def _write_loop(self):
        """Background loop that handles disk writes."""
        while self._is_active or not self._write_queue.empty():
            try:
                signal = await self._write_queue.get()
                if signal in ["SAVE", "FLUSH"]:
                    await self._perform_disk_write()
                self._write_queue.task_done()
                
                if signal == "FLUSH" and not self._is_active:
                    break
            except Exception as e:
                logger.error("journal_write_loop_error", error=str(e))
                await asyncio.sleep(1)

    async def _perform_disk_write(self):
        """The actual async disk write operation."""
        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if current_date != self._current_date:
            self._current_date = current_date
            self._journal_file = self.log_dir / f"trade_journal_{self._current_date}.json"
        
        try:
            temp_file = self._journal_file.with_suffix(".tmp")
            async with aiofiles.open(temp_file, mode='w') as f:
                await f.write(json.dumps(self.entries, indent=2))
            
            # Atomic rename
            os.replace(temp_file, self._journal_file)
        except Exception as e:
            logger.error("journal_disk_write_failed", error=str(e))

    def get_all(self) -> List[Dict[str, Any]]:
        return self.entries.copy()
    
    def get_open(self) -> List[Dict[str, Any]]:
        return [e for e in self.entries if e.get("outcome") in [None, "open"]]
    
    def get_closed(self) -> List[Dict[str, Any]]:
        return [e for e in self.entries if e.get("outcome") not in [None, "open"]]
    
    def load(self) -> None:
        """Loads today's journal synchronously at startup."""
        if self._journal_file.exists():
            try:
                with open(self._journal_file, 'r') as f:
                    self.entries = json.load(f)
                logger.info("journal_loaded", entries=len(self.entries))
            except (json.JSONDecodeError, FileNotFoundError):
                self.entries = []
