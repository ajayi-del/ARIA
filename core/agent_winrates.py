"""
core/agent_winrates.py — Persistent per-agent win/loss tracking.

Survives restarts via JSON file in logs/. Each ARIA agent (personality) accumulates
its own win/loss record. Displayed in the Agents panel and used by AdaptiveCalibrator.

Architecture:
  - Loads from disk at startup (or starts fresh if file missing)
  - Updated on every trade close via record_outcome()
  - Saves to disk immediately on each update (no data loss on crash)
  - Thread/async safe: no locks needed (single-threaded asyncio writes)
"""

from __future__ import annotations

import json
import structlog
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional

log = structlog.get_logger(__name__)

_DEFAULT_PATH = Path("logs/agent_winrates.json")

# Known ARIA personalities — all start at 0/0
KNOWN_AGENTS: list = [
    "SHIELD", "SOVEREIGN", "AFTERMATH", "APEX", "FLOW", "COIL", "SCOUT",
]


@dataclass
class AgentRecord:
    wins:      int   = 0
    losses:    int   = 0
    total_pnl: float = 0.0
    # Running streak for display
    streak:    int   = 0   # positive = win streak, negative = loss streak

    @property
    def trades(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        """0.0–100.0 percentage."""
        return round(self.wins / self.trades * 100, 1) if self.trades > 0 else 0.0

    @property
    def avg_pnl(self) -> float:
        return round(self.total_pnl / self.trades, 4) if self.trades > 0 else 0.0


class AgentWinrates:
    """
    Persistent per-agent win/loss tracker.

    Usage:
        wr = AgentWinrates()          # loads from disk
        wr.record_outcome("APEX", won=True, pnl=12.5)
        record = wr.get("APEX")       # AgentRecord
        display_data = wr.all()       # {agent: AgentRecord}
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path    = path or _DEFAULT_PATH
        self._records: Dict[str, AgentRecord] = {a: AgentRecord() for a in KNOWN_AGENTS}
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            log.debug("agent_winrates_no_file", path=str(self._path))
            return
        try:
            data = json.loads(self._path.read_text())
            for agent, rec in data.items():
                self._records[agent] = AgentRecord(
                    wins      = int(rec.get("wins",      0)),
                    losses    = int(rec.get("losses",    0)),
                    total_pnl = float(rec.get("total_pnl", 0.0)),
                    streak    = int(rec.get("streak",    0)),
                )
            log.info("agent_winrates_loaded", path=str(self._path),
                     agents={a: f"{r.wins}/{r.losses}" for a, r in self._records.items()})
        except Exception as e:
            log.warning("agent_winrates_load_failed", error=str(e))

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(exist_ok=True)
            data = {
                agent: asdict(rec)
                for agent, rec in self._records.items()
            }
            self._path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            log.warning("agent_winrates_save_failed", error=str(e))

    # ── Mutations ─────────────────────────────────────────────────────────────

    def record_outcome(self, agent: str, won: bool, pnl: float = 0.0) -> None:
        """
        Record a trade outcome for an agent personality.

        Args:
            agent:  personality name e.g. "APEX", "FLOW", "SOVEREIGN"
            won:    True if trade was profitable
            pnl:    realised P&L in USD (can be negative for losses)
        """
        if agent not in self._records:
            self._records[agent] = AgentRecord()

        rec = self._records[agent]
        if won:
            rec.wins  += 1
            rec.streak = max(rec.streak + 1, 1)
        else:
            rec.losses += 1
            rec.streak  = min(rec.streak - 1, -1)
        rec.total_pnl += pnl
        self._save()
        log.debug("agent_outcome_recorded",
                  agent=agent, won=won, pnl=round(pnl, 4),
                  winrate=rec.win_rate, streak=rec.streak)

    def reset_agent(self, agent: str) -> None:
        """Reset an agent's record (e.g. after a strategy overhaul)."""
        self._records[agent] = AgentRecord()
        self._save()

    # ── Queries ───────────────────────────────────────────────────────────────

    def get(self, agent: str) -> AgentRecord:
        return self._records.get(agent, AgentRecord())

    def all(self) -> Dict[str, AgentRecord]:
        return dict(self._records)

    def best_agent(self) -> Optional[str]:
        """Agent with highest win rate (min 5 trades)."""
        qualified = {a: r for a, r in self._records.items() if r.trades >= 5}
        if not qualified:
            return None
        return max(qualified, key=lambda a: qualified[a].win_rate)

    def total_trades(self) -> int:
        return sum(r.trades for r in self._records.values())
