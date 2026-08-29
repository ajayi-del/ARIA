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
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path
from core.clock import exchange_clock

logger = structlog.get_logger(__name__)


def orphan_close_fallback_enabled() -> bool:
    return os.getenv("JOURNAL_ORPHAN_CLOSE_ENABLED", "true").lower() == "true"


def phantom_filter_enabled() -> bool:
    return os.getenv("JOURNAL_PHANTOM_FILTER_ENABLED", "true").lower() != "false"


def is_phantom_record(entry: dict) -> bool:
    """True for SPCX-USD scale-mismatch phantom closes (the 2026-08 rebase split).

    SoDEX served a pre-rebase mark scale for SPCX-USD (~5.5x split, persistent
    from mid-August), so mark-driven triggers booked impossible closes against
    the ghost scale. The journal census (2026-08-29) is cleanly bimodal:
    561 real closes ALL under $5 pnl vs 64 ghosts ALL over $100 (zero records
    between) — the $100 threshold separates the clusters exactly, on ANY date
    (the 08-24 predicate's 08-21/22 date bound caught only the first 4).
    Journals are never modified (rule #14) — this filters DERIVED reads only.
    """
    if entry.get("symbol") != "SPCX-USD":
        return False
    return abs(entry.get("pnl_usd") or entry.get("pnl_net_usd") or 0.0) > 100.0


@dataclass
class TradeRecord:
    """Schema definition for a trade journal entry.

    Used for type-checking and static analysis.  The live journal stores
    plain dicts for flexibility; TradeRecord documents the canonical field set
    including all philosophical layer fields added in v1.3+.
    """
    entry_id: str = ""
    timestamp_ms: int = 0
    symbol: str = ""
    direction: str = ""
    approved: bool = False
    coherence_score: float = 0.0
    raw_score: float = 0.0
    size_multiplier: float = 0.0
    macro_bias: str = "unknown"
    regime: str = "unknown"
    market_type: str = "unknown"
    sweep: str = "none"
    reclaim: bool = False
    imbalance: float = 0.0
    divergence: str = "none"
    funding_class: str = "neutral"
    strategy_tag: str = "unknown"
    cascade_phase: str = "none"
    # Philosophical layer fields (v1.3+)
    personality: Optional[str] = None
    kant_structure: Optional[str] = None
    conviction: Optional[float] = None
    will_state: Optional[str] = None
    order_type_used: Optional[str] = None
    # Outcome fields
    outcome: Optional[str] = None
    pnl_usd: Optional[float] = None
    pnl_net_usd: Optional[float] = None
    pnl_r: Optional[float] = None
    hold_time_ms: Optional[int] = None
    closed_at_ms: Optional[int] = None


class ARIAJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if dataclasses.is_dataclass(obj):
            return dataclasses.asdict(obj)
        if hasattr(obj, 'model_dump'):
            return obj.model_dump()
        if hasattr(obj, '__dict__'):
            return obj.__dict__
        return super().default(obj)

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
        cal_state: Any = None,    # CalendarState
        personality: str = None,  # e.g. "SCOUT", "APEX", "FLOW"
        kant_structure: str = None,   # e.g. "trend", "accumulation"
        conviction: float = None,     # 0.0–1.0
        will_state: str = None,       # e.g. "neutral", "conservative"
        order_type_used: str = None,  # "limit" | "market" | "probe"
    ) -> str:
        """
        Creates entry, puts in write queue.
        Returns entry_id.
        """
        entry_id = str(uuid.uuid4())
        # Use exchange-synced clock so journal timestamps match exchange trade history.
        # Falls back to local time if clock not yet synced (early startup entries).
        _now_ms = exchange_clock.now_ms()
        _now_iso = exchange_clock.now_iso()

        entry = {
            "entry_id": entry_id,
            "timestamp_ms": _now_ms,
            "timestamp_iso": _now_iso,
            "symbol": getattr(state, 'symbol', "UNKNOWN"),
            "direction": getattr(candidate, 'side', "none"),
            "coherence_score": getattr(state, 'weighted_score', getattr(state, 'coherence_score', 0)),
            "raw_score": getattr(state, 'raw_score', getattr(state, 'coherence_score', 0)),
            "size_multiplier": getattr(state, 'size_multiplier', 0.0),
            
            # v1.2 Quant Fields
            "cluster_validated": getattr(state, 'cluster_validated', False),
            "cluster_strength": getattr(state, 'cluster_strength', 0.0),
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

            # v1.9 Cascade Intelligence Fields
            "strategy_tag": getattr(state, "strategy_tag", "unknown"),
            "cascade_phase": getattr(state, "cascade_phase", "none"),
            "cascade_notional_usd": getattr(state, "cascade_notional_usd", 0.0),
            "cascade_direction": getattr(state, "cascade_direction", ""),
            "aftermath_signals": getattr(state, "aftermath_signals", []),
            "tier8_cascade_fired": getattr(state, "tier8_cascade_fired", False),
            "tier7_cross_venue_bonus": getattr(state, "tier7_cross_venue_bonus", 0.0),

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
            
            # Philosophical layer fields (Kant + Nietzsche)
            "personality":      personality,
            "kant_structure":   kant_structure,
            "conviction":       conviction,
            "will_state":       will_state,
            "order_type_used":  order_type_used,

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
        pnl_usd: Optional[float] = None,
        closed_at_ms: Optional[int] = None,
        pnl_net_usd: Optional[float] = None,
        exit_reason: Optional[str] = None,
    ) -> None:
        """Finds entry, updates outcome, triggers non-blocking save."""
        for entry in self.entries:
            if entry["entry_id"] == entry_id:
                entry["outcome"] = outcome
                entry["pnl_usd"] = pnl_usd
                entry["pnl_net_usd"] = pnl_net_usd if pnl_net_usd is not None else pnl_usd
                entry["closed_at_ms"] = closed_at_ms
                if exit_reason is not None:
                    entry["exit_reason"] = exit_reason

                target_pnl = entry["pnl_net_usd"]
                if target_pnl is not None and entry.get("initial_margin"):
                    entry["pnl_r"] = target_pnl / entry["initial_margin"]

                if closed_at_ms is not None:
                    entry["hold_time_ms"] = closed_at_ms - entry["timestamp_ms"]

                self.save_nonblocking()
                return

        logger.error("journal_entry_not_found", entry_id=entry_id)

    def record_partial(
        self,
        entry_id: str,
        closed_qty: float,
        pnl_usd: float,
        pnl_net_usd: Optional[float] = None,
        closed_at_ms: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Record a partial close against an OPEN entry. The entry stays open;
        partials accumulate so the final close can report true totals."""
        for entry in self.entries:
            if entry["entry_id"] == entry_id:
                partials = entry.setdefault("partials", [])
                partials.append({
                    "closed_qty": closed_qty,
                    "pnl_usd": pnl_usd,
                    "pnl_net_usd": pnl_net_usd if pnl_net_usd is not None else pnl_usd,
                    "closed_at_ms": closed_at_ms,
                    "reason": reason,
                })
                entry["realized_pnl_usd"] = round(
                    float(entry.get("realized_pnl_usd", 0.0) or 0.0) + pnl_usd, 8
                )
                self.save_nonblocking()
                return

        logger.error("journal_entry_not_found", entry_id=entry_id)

    # ── Orphan-close repair (2026-08-26) ─────────────────────────────────────
    # Positions whose entry record can't be found at close time previously
    # vanished from the journal while position_closed still logged — ARIA
    # traded, the journal forgot, and every downstream learner (Skeptic base
    # rates, personality stats, churn flags, capacity-governor journal_evidence)
    # ate the survivorship bias. Dominant mechanism: load() reads TODAY's file
    # only, so a position entered yesterday + bot restart = entry_id pop misses
    # AND the in-memory orphan scan can't find the cross-midnight entry.
    _ORPHAN_SCAN_DAYS: int = 4
    _CLOSE_DEDUP_WINDOW_MS: int = 120_000

    def find_open_entry_in_files(
        self, symbol: str, days: int = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Newest approved+open entry for `symbol` in previous day-files.

        Read-only — source files are never mutated (journal permanence, rule
        #14). Returns (entry, date_str) or (None, None).
        """
        days = days if days is not None else self._ORPHAN_SCAN_DAYS
        today = datetime.fromtimestamp(
            exchange_clock.now_ms() / 1000, timezone.utc
        ).date()
        for i in range(1, days + 1):
            d = (today - timedelta(days=i)).isoformat()
            fpath = self.log_dir / f"trade_journal_{d}.json"
            try:
                with open(fpath, "r") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                data = data.get("trades", [])
            for e in reversed(data):
                if (isinstance(e, dict) and e.get("symbol") == symbol
                        and e.get("approved")
                        and e.get("outcome") in (None, "open")):
                    return e, d
        return None, None

    def close_already_recorded(
        self, symbol: str, closed_at_ms: Optional[int],
        pnl_net_usd: Optional[float],
    ) -> bool:
        """Dedup guard: a close for this symbol within the time window carrying
        a matching pnl is the same trade — never book it twice."""
        for e in self.entries:
            if e.get("symbol") != symbol or e.get("outcome") not in ("win", "loss"):
                continue
            c = e.get("closed_at_ms")
            if not c or not closed_at_ms or abs(int(c) - int(closed_at_ms)) > self._CLOSE_DEDUP_WINDOW_MS:
                continue
            p = e.get("pnl_net_usd", e.get("pnl_usd"))
            if p is None or pnl_net_usd is None:
                return True
            if abs(abs(float(p)) - abs(float(pnl_net_usd))) <= max(0.01, 0.05 * abs(float(pnl_net_usd))):
                return True
        return False

    def record_cross_day_close(
        self,
        *,
        symbol: str,
        direction: str,
        outcome: str,
        pnl_usd: float,
        pnl_net_usd: Optional[float] = None,
        closed_at_ms: Optional[int] = None,
        exit_reason: Optional[str] = None,
        entry_price: Optional[float] = None,
        position_size: Optional[float] = None,
        initial_margin: Optional[float] = None,
        leverage: Optional[int] = None,
        opened_at_ms: Optional[int] = None,
        personality: Optional[str] = None,
        strategy_tag: Optional[str] = None,
    ) -> Optional[str]:
        """Book a close whose entry_id is unrecoverable in-memory.

        Two tiers: (1) the real entry found in a recent day-file → a migrated
        copy carrying the entry's full context (personality, margin → correct
        pnl_r and downstream personality stats) is appended to TODAY's file
        with close_migrated_from; the source file stays untouched. (2) no
        entry anywhere → a synthetic orphan_close record from the position
        object. Returns the entry_id, or None when deduped.
        """
        if self.close_already_recorded(symbol, closed_at_ms, pnl_net_usd):
            logger.info("journal_orphan_close_deduped", symbol=symbol,
                        exit_reason=exit_reason)
            return None
        src, src_date = self.find_open_entry_in_files(symbol)
        if src is not None:
            rec = dict(src)
            entry_id = rec.get("entry_id") or f"migrated-{symbol}-{closed_at_ms}"
            rec["entry_id"] = entry_id
            rec["close_migrated_from"] = src_date
        else:
            entry_id = f"orphan-{symbol}-{closed_at_ms}"
            rec = {
                "entry_id": entry_id,
                "timestamp_ms": int(opened_at_ms or closed_at_ms or 0),
                "timestamp_iso": None,
                "symbol": symbol,
                "direction": direction,
                "approved": True,
                "personality": personality,
                "strategy_tag": strategy_tag or "unknown",
                "entry_price": entry_price,
                "position_size": position_size,
                "initial_margin": initial_margin,
                "leverage": leverage,
                "orphan_close": True,
            }
        rec["outcome"] = outcome
        rec["pnl_usd"] = pnl_usd
        rec["pnl_net_usd"] = pnl_net_usd if pnl_net_usd is not None else pnl_usd
        rec["closed_at_ms"] = closed_at_ms
        if exit_reason is not None:
            rec["exit_reason"] = exit_reason
        _im = rec.get("initial_margin")
        if rec["pnl_net_usd"] is not None and _im:
            rec["pnl_r"] = rec["pnl_net_usd"] / _im
        _ts = rec.get("timestamp_ms")
        if closed_at_ms is not None and _ts:
            rec["hold_time_ms"] = closed_at_ms - _ts
        self.entries.append(rec)
        self.save_nonblocking()
        logger.info("journal_orphan_close_recorded", symbol=symbol,
                    entry_id=entry_id, migrated_from=src_date,
                    exit_reason=exit_reason)
        return entry_id

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
        current_date = exchange_clock.now_date_str()
        if current_date != self._current_date:
            self._current_date = current_date
            self._journal_file = self.log_dir / f"trade_journal_{self._current_date}.json"
        
        try:
            temp_file = self._journal_file.with_suffix(".tmp")
            async with aiofiles.open(temp_file, mode='w') as f:
                await f.write(json.dumps(self.entries, indent=2, cls=ARIAJSONEncoder))
                await f.flush()

            # Atomic rename — guard against race where another coroutine already renamed
            if temp_file.exists():
                os.replace(temp_file, self._journal_file)
        except Exception as e:
            logger.error("journal_disk_write_failed", error=str(e))

    def get_all(self) -> List[Dict[str, Any]]:
        return self.entries.copy()
    
    def get_open(self) -> List[Dict[str, Any]]:
        return [e for e in self.entries if e.get("outcome") in [None, "open"]]
    
    def get_closed(self, filter_phantoms: Optional[bool] = None) -> List[Dict[str, Any]]:
        # Only "win" / "loss" are real closed trades. "abandoned" entries are
        # phantom signals that were never actually executed — they have pnl_usd=None
        # and must never enter performance calculations.
        _closed = [e for e in self.entries if e.get("outcome") in ("win", "loss")]
        # Read-path phantom filter (2026-08-29): scale-ghost closes poison every
        # belief computed downstream (symbol edge, session pnl, daily-loss gate).
        # Files are never mutated (rule #14) — only the READ is filtered.
        # filter_phantoms=None → env default (JOURNAL_PHANTOM_FILTER_ENABLED,
        # default true); explicit False = raw legacy read.
        if filter_phantoms is None:
            filter_phantoms = phantom_filter_enabled()
        if filter_phantoms:
            _closed = [e for e in _closed if not is_phantom_record(e)]
        return _closed
    
    # Maximum entries kept in memory — protects against unbounded growth when a
    # high-frequency signal loop logs every evaluation.  Open trades are always
    # preserved; older closed/abandoned entries are trimmed on load.
    _MAX_IN_MEMORY: int = 500

    def load(self) -> None:
        """Loads today's journal synchronously at startup.

        Trims to _MAX_IN_MEMORY entries, always preserving all open trades so
        position reconciliation at startup is never affected.  Reduces load time
        from O(14 k) → O(500) and cuts memory footprint proportionally.
        """
        if self._journal_file.exists():
            try:
                with open(self._journal_file, 'r') as f:
                    all_entries = json.load(f)
                total = len(all_entries)
                # Always keep open/unresolved entries regardless of position in list
                open_entries   = [e for e in all_entries if e.get("outcome") in (None, "open")]
                closed_entries = [e for e in all_entries if e not in open_entries]
                # Trim closed history to fit budget, newest first
                budget = max(0, self._MAX_IN_MEMORY - len(open_entries))
                self.entries = open_entries + closed_entries[-budget:]
                trimmed = total - len(self.entries)
                logger.info("journal_loaded", entries=len(self.entries),
                            total_on_disk=total, trimmed=trimmed)
            except (json.JSONDecodeError, FileNotFoundError):
                self.entries = []
