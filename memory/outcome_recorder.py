"""
memory/outcome_recorder.py — Per-agent outcome recording and calibration system.

Architecture
────────────
OutcomeRecorder is the closure of ARIA's learning loop:
  Signal → Decision → Action → Outcome → Per-agent calibration → Better signal

When a position closes, _record_close() in main.py calls OutcomeRecorder.record().
OutcomeRecorder then:
  1. Evaluates each agent's prior vote against the outcome (was it correct?)
  2. Writes the full outcome to SQLite (WAL mode, same pattern as CalendarEngine)
  3. Updates each agent's running accuracy
  4. Logs the attribution dict (agents_correct)

After 50 trades, get_calibration_recommendations() produces plain-English
calibration advice — the system diagnosing its own weaknesses.

Storage: logs/outcomes.db (SQLite, WAL)
Schema: one row per trade, one column set per agent (fired, direction, conf, correct)
"""

from __future__ import annotations

import time
import asyncio
import structlog
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

log = structlog.get_logger(__name__)

# ── SystemStats ───────────────────────────────────────────────────────────────

@dataclass
class SystemStats:
    total_trades:     int   = 0
    win_rate:         float = 0.0
    avg_r:            float = 0.0
    profit_factor:    float = 0.0
    tp1_rate:         float = 0.0
    tp2_rate:         float = 0.0
    tp3_rate:         float = 0.0
    stop_rate:        float = 0.0
    agent_accuracies: dict  = field(default_factory=dict)
    weakest_agent:    str   = ""
    weakest_accuracy: float = 0.0
    best_regime:      str   = ""
    best_regime_r:    float = 0.0


# ── OutcomeRecorder ───────────────────────────────────────────────────────────

_AGENT_NAMES = ["macro", "regime", "structure", "micro", "funding", "ssi"]

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS outcomes (
    trade_id            TEXT PRIMARY KEY,
    symbol              TEXT,
    direction           TEXT,
    net_pnl_r           REAL,
    net_pnl_usd         REAL,
    exit_reason         TEXT,
    entry_time_ms       INTEGER,
    exit_time_ms        INTEGER,
    hold_time_hours     REAL,
    calendar_regime     TEXT,
    coherence_mult      REAL,
    freshness_mult      REAL,
    calendar_mult       REAL,
    combined_mult       REAL,
    slippage_actual     REAL,
    slippage_modelled   REAL,
    funding_paid_usd    REAL,
    macro_fired         INTEGER,
    macro_direction     TEXT,
    macro_confidence    REAL,
    macro_correct       INTEGER,
    regime_fired        INTEGER,
    regime_direction    TEXT,
    regime_confidence   REAL,
    regime_correct      INTEGER,
    structure_fired     INTEGER,
    structure_direction TEXT,
    structure_confidence REAL,
    structure_correct   INTEGER,
    micro_fired         INTEGER,
    micro_direction     TEXT,
    micro_confidence    REAL,
    micro_correct       INTEGER,
    funding_fired       INTEGER,
    funding_direction   TEXT,
    funding_confidence  REAL,
    funding_correct     INTEGER,
    ssi_fired           INTEGER,
    ssi_direction       TEXT,
    ssi_confidence      REAL,
    ssi_correct         INTEGER
)
"""

_INSERT = """
INSERT OR REPLACE INTO outcomes VALUES (
    :trade_id, :symbol, :direction, :net_pnl_r, :net_pnl_usd,
    :exit_reason, :entry_time_ms, :exit_time_ms, :hold_time_hours,
    :calendar_regime, :coherence_mult, :freshness_mult, :calendar_mult,
    :combined_mult, :slippage_actual, :slippage_modelled, :funding_paid_usd,
    :macro_fired, :macro_direction, :macro_confidence, :macro_correct,
    :regime_fired, :regime_direction, :regime_confidence, :regime_correct,
    :structure_fired, :structure_direction, :structure_confidence, :structure_correct,
    :micro_fired, :micro_direction, :micro_confidence, :micro_correct,
    :funding_fired, :funding_direction, :funding_confidence, :funding_correct,
    :ssi_fired, :ssi_direction, :ssi_confidence, :ssi_correct
)
"""


class OutcomeRecorder:
    """
    Records trade outcomes and attributes correctness to each signal agent.

    Usage:
        recorder = OutcomeRecorder(agents=agents, journal=journal)
        await recorder.init()
        # On every position close:
        await recorder.record(outcome)
        # Query:
        stats = await recorder.get_agent_stats()
        recs  = await recorder.get_calibration_recommendations()
    """

    def __init__(
        self,
        agents: list,
        journal=None,
        db_path: str = "logs/outcomes.db",
    ) -> None:
        self._agents  = {a.name: a for a in agents}
        self._journal = journal
        self._db_path = db_path
        self._db      = None   # aiosqlite connection (set in init())

    async def init(self) -> None:
        """Create SQLite database and enable WAL mode."""
        try:
            import aiosqlite
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(self._db_path)
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA synchronous=NORMAL")
            await self._db.execute(_CREATE_TABLE)
            await self._db.commit()
            log.info("outcome_recorder_initialized", db_path=self._db_path)
        except ImportError:
            log.warning("aiosqlite_not_available",
                        note="Outcome recording disabled — install aiosqlite")
            self._db = None
        except Exception as e:
            log.error("outcome_recorder_init_error", error=str(e))
            self._db = None

    async def record(self, outcome) -> None:
        """
        Record a trade outcome across all 6 agents.

        Steps:
          1. Evaluate per-agent correctness (agent.is_correct(output, outcome))
          2. Update agent running accuracy
          3. Write to SQLite
          4. Update journal entry
          5. Log attribution dict
        """
        from intelligence.agents.base import TradeOutcome, AgentOutput

        # Step 1 & 2: attribute correctness to each agent
        agents_correct: dict = {}
        agent_outputs: dict = getattr(outcome, "agent_outputs", {}) or {}

        for agent_name, agent in self._agents.items():
            agent_output = agent_outputs.get(agent_name)
            if agent_output is None:
                # Agent had no output for this trade — abstained
                agents_correct[agent_name] = None
                continue
            try:
                correct = agent.is_correct(agent_output, outcome)
            except Exception as e:
                log.warning("agent_is_correct_error", agent=agent_name, error=str(e))
                correct = False

            # Only attribute if agent fired
            if getattr(agent_output, "fired", False):
                agent.record_outcome(agent_output, outcome)
                agents_correct[agent_name] = correct
            else:
                agents_correct[agent_name] = None   # unfired = not evaluated

        # Persist agents_correct back onto outcome
        if hasattr(outcome, "agents_correct"):
            outcome.agents_correct = agents_correct

        # Step 3: write to SQLite
        if self._db is not None:
            try:
                row = self._build_row(outcome, agent_outputs, agents_correct)
                await self._db.execute(_INSERT, row)
                await self._db.commit()
            except Exception as e:
                log.error("outcome_recorder_write_error", error=str(e))

        # Step 4: update journal
        if self._journal is not None:
            try:
                trade_id = getattr(outcome, "trade_id", None)
                if trade_id and hasattr(self._journal, "update_agents_correct"):
                    self._journal.update_agents_correct(trade_id, agents_correct)
            except Exception as e:
                log.debug("journal_update_agents_correct_error", error=str(e))

        # Step 5: log attribution
        log.info(
            "outcome_recorded",
            trade_id   = getattr(outcome, "trade_id", "?"),
            symbol     = getattr(outcome, "symbol", "?"),
            direction  = getattr(outcome, "direction", "?"),
            net_pnl_r  = round(getattr(outcome, "net_pnl_r", 0.0), 3),
            exit_reason = getattr(outcome, "exit_reason", "?"),
            agents_correct = {k: v for k, v in agents_correct.items() if v is not None},
        )

    async def get_agent_stats(self) -> dict:
        """Return {agent_name: AgentAccuracy} from in-memory agent records."""
        return {name: agent.get_accuracy() for name, agent in self._agents.items()}

    async def get_total_trades(self) -> int:
        if self._db is None:
            return 0
        try:
            async with self._db.execute("SELECT COUNT(*) FROM outcomes") as cur:
                row = await cur.fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    async def get_system_stats(self) -> SystemStats:
        """Compute aggregate system statistics from outcome DB."""
        if self._db is None:
            return SystemStats()
        try:
            async with self._db.execute(
                "SELECT net_pnl_r, exit_reason, direction FROM outcomes"
            ) as cur:
                rows = await cur.fetchall()

            if not rows:
                return SystemStats()

            total   = len(rows)
            wins    = sum(1 for r in rows if r[0] > 0)
            win_rate = wins / total if total > 0 else 0.0
            avg_r    = sum(r[0] for r in rows) / total

            wins_r   = sum(r[0] for r in rows if r[0] > 0)
            losses_r = abs(sum(r[0] for r in rows if r[0] < 0))
            pf       = wins_r / losses_r if losses_r > 0 else 0.0

            tp1  = sum(1 for r in rows if r[1] == "tp1")  / total
            tp2  = sum(1 for r in rows if r[1] == "tp2")  / total
            tp3  = sum(1 for r in rows if r[1] == "tp3")  / total
            stop = sum(1 for r in rows if r[1] == "stop") / total

            agent_accs = {n: a.get_accuracy().accuracy_pct
                          for n, a in self._agents.items()}
            weakest    = min(agent_accs, key=agent_accs.get) if agent_accs else ""
            weak_acc   = agent_accs.get(weakest, 0.0)

            return SystemStats(
                total_trades     = total,
                win_rate         = round(win_rate * 100, 1),
                avg_r            = round(avg_r, 3),
                profit_factor    = round(pf, 2),
                tp1_rate         = round(tp1 * 100, 1),
                tp2_rate         = round(tp2 * 100, 1),
                tp3_rate         = round(tp3 * 100, 1),
                stop_rate        = round(stop * 100, 1),
                agent_accuracies = agent_accs,
                weakest_agent    = weakest,
                weakest_accuracy = round(weak_acc, 1),
            )
        except Exception as e:
            log.error("get_system_stats_error", error=str(e))
            return SystemStats()

    async def get_calibration_recommendations(self) -> list:
        """
        Plain-English calibration recommendations.
        Only fires when total_trades >= 50. Empty list otherwise.
        """
        total = await self.get_total_trades()
        if total < 50:
            return []

        recs = []
        accs = await self.get_agent_stats()

        # Agent accuracy checks
        thresholds = {
            "micro":     (0.45, "Increase sweep validation strictness or raise cluster "
                                "confirmation requirement."),
            "macro":     (0.50, "MAG7.ssi signal may lag current regime. "
                                "Consider reducing macro tier weight."),
            "regime":    (0.55, "Regime transitions being missed. "
                                "Consider shorter lookback for strength calculation."),
            "structure": (0.50, "Market type classification below threshold. "
                                "Verify ATR baseline window is appropriate."),
            "funding":   (0.50, "Funding reversion calls underperforming. "
                                "Review extreme funding thresholds."),
            "ssi":       (0.50, "OI lead signal accuracy below threshold. "
                                "Verify OI data feed quality."),
        }

        for agent_name, (threshold, advice) in thresholds.items():
            acc = accs.get(agent_name)
            if acc and acc.total_contributing_trades >= 10:
                if acc.accuracy < threshold:
                    recs.append(
                        f"{agent_name.capitalize()}Agent accuracy {acc.accuracy_pct:.0f}% "
                        f"below {threshold*100:.0f}% threshold. {advice}"
                    )

        # Slippage check
        if self._db is not None:
            try:
                async with self._db.execute(
                    "SELECT AVG(slippage_actual), AVG(slippage_modelled) FROM outcomes "
                    "WHERE slippage_actual > 0"
                ) as cur:
                    row = await cur.fetchone()
                    if row and row[0] and row[1] and row[1] > 0:
                        ratio = row[0] / row[1]
                        if ratio > 1.5:
                            suggested = round(ratio, 1)
                            recs.append(
                                f"Actual slippage {row[0]*10000:.0f}bps averaging "
                                f"{ratio:.0f}× model ({row[1]*10000:.0f}bps). "
                                f"Increase slippage_multiplier to {suggested}×."
                            )
            except Exception:
                pass

        # Funding drag check
        if self._db is not None:
            try:
                async with self._db.execute(
                    "SELECT AVG(hold_time_hours), AVG(funding_paid_usd), AVG(net_pnl_r) FROM outcomes"
                ) as cur:
                    row = await cur.fetchone()
                    if row and row[0] and row[1] and row[2]:
                        avg_hold    = float(row[0])
                        avg_funding = float(row[1])
                        avg_r       = float(row[2])
                        if avg_hold > 8.0 and avg_r > 0 and avg_funding > avg_r * 0.5:
                            recs.append(
                                f"Average funding cost ${avg_funding:.3f} per trade consuming "
                                f">50% of {avg_r:.2f}R avg. Consider tighter TP targets "
                                f"to reduce {avg_hold:.1f}h average hold time."
                            )
            except Exception:
                pass

        return recs

    # ── Internals ────────────────────────────────────────────────────────────

    def _build_row(self, outcome, agent_outputs: dict, agents_correct: dict) -> dict:
        """Build the SQLite parameter dict for _INSERT."""
        def _agent_row(name):
            out = agent_outputs.get(name)
            cor = agents_correct.get(name)
            if out is None:
                return {
                    f"{name}_fired":      0,
                    f"{name}_direction":  "none",
                    f"{name}_confidence": 0.0,
                    f"{name}_correct":    -1,   # -1 = not evaluated
                }
            return {
                f"{name}_fired":      int(bool(getattr(out, "fired", False))),
                f"{name}_direction":  str(getattr(out, "direction", "neutral")),
                f"{name}_confidence": float(getattr(out, "confidence", 0.0)),
                f"{name}_correct":    (1 if cor is True else 0 if cor is False else -1),
            }

        row = {
            "trade_id":           getattr(outcome, "trade_id", ""),
            "symbol":             getattr(outcome, "symbol", ""),
            "direction":          getattr(outcome, "direction", ""),
            "net_pnl_r":          getattr(outcome, "net_pnl_r", 0.0),
            "net_pnl_usd":        getattr(outcome, "net_pnl_usd", 0.0),
            "exit_reason":        getattr(outcome, "exit_reason", ""),
            "entry_time_ms":      getattr(outcome, "entry_time_ms", 0),
            "exit_time_ms":       getattr(outcome, "exit_time_ms", int(time.time() * 1000)),
            "hold_time_hours":    getattr(outcome, "hold_time_hours", 0.0),
            "calendar_regime":    getattr(outcome, "calendar_regime", ""),
            "coherence_mult":     getattr(outcome, "coherence_mult", 1.0),
            "freshness_mult":     getattr(outcome, "freshness_mult", 1.0),
            "calendar_mult":      getattr(outcome, "calendar_mult", 1.0),
            "combined_mult":      getattr(outcome, "combined_mult", 1.0),
            "slippage_actual":    getattr(outcome, "slippage_actual", 0.0),
            "slippage_modelled":  getattr(outcome, "slippage_modelled", 0.0),
            "funding_paid_usd":   getattr(outcome, "funding_paid_usd", 0.0),
        }
        for agent_name in _AGENT_NAMES:
            row.update(_agent_row(agent_name))
        return row
