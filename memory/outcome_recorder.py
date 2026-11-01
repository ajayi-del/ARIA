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


def _int_or_none(v):
    """SQLite INTEGER for bool/int flags; None stays NULL (additive doctrine)."""
    if v is None:
        return None
    return int(bool(v))

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
    regime              TEXT,
    strategy_type       TEXT,
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

# Move 1 — blocked_signals table: every gate rejection with mark price for counterfactual analysis
_CREATE_BLOCKED_TABLE = """
CREATE TABLE IF NOT EXISTS blocked_signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT,
    direction      TEXT,
    coherence      REAL,
    gate_reason    TEXT,
    mark_price     REAL,
    regime         TEXT,
    strategy_type  TEXT,
    timestamp_ms   INTEGER
)
"""

# Schema census 2026-10-31 — throttle/rejection storms were log-only
# (margin_budget ×8,646, coherence_tier ×9,998, close-path rejects ×7,997).
# This table persists them (additive; old DBs gain it via CREATE IF NOT EXISTS).
_CREATE_THROTTLE_TABLE = """
CREATE TABLE IF NOT EXISTS throttle_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type      TEXT     NOT NULL,   -- entry_standdown | close_rejected | close_failed
    sub_type        TEXT     NOT NULL,   -- margin_budget | drawdown_halt_10pct | ...
    symbol          TEXT,
    venue           TEXT,
    direction       TEXT,
    coherence       REAL,
    mark_price      REAL,
    reason_detail   TEXT,
    related_entry_id TEXT,
    timestamp_ms    INTEGER NOT NULL
)
"""

# Brain census 2026-10-31 — pyramid/treasury/ratchet decisions were log-only;
# the policy wrongs (PW-1..3) are unprovable without the verdicts persisted.
# PW-1/PW-2 columns (tp1_alt_gate_*, mark_vs_tp1_pct, single_book_roe) are
# baked into the CREATE — the table is new, no migration needed.
_CREATE_BRAIN_VERDICTS_TABLE = """
CREATE TABLE IF NOT EXISTS brain_verdicts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    brain_module         TEXT    NOT NULL,  -- pyramid | treasury | roe_ratchet
                                            -- | campaign_pyramid | early_arm
    verdict_type         TEXT    NOT NULL,  -- add | unwind | cluster_harvest
                                            -- | runner_register | ratchet_rung
                                            -- | early_arm_check | cluster_activation
                                            -- | solo_book_skip
    symbol               TEXT,
    venue                TEXT,
    entry_id_uuid        TEXT,
    related_trade_id     TEXT,
    allowed              INTEGER NOT NULL,  -- 1=allowed/fired, 0=blocked
    reason               TEXT    NOT NULL,
    tp1_cleared          INTEGER,
    tp1_hit_at_ms        INTEGER,
    trigger_px           REAL,
    mark_at_check        REAL,
    coherence            REAL,
    rv_rank              REAL,              -- NULL = pillar dark
    oi_delta_pct         REAL,              -- NULL = pillar dark
    funding_rate         REAL,              -- NULL = pillar dark (key kill)
    warmup_frac          REAL,
    trend_verdict        TEXT,
    peak_roe             REAL,
    current_roe          REAL,
    cluster_size         INTEGER,
    cluster_roe          REAL,
    legs_done            INTEGER,
    mfe_at_check         REAL,
    atr_ratio            REAL,
    projected_add_qty    REAL,
    floor_price          REAL,
    tp1_alt_gate_price_cleared INTEGER,     -- 1 if mark had crossed tp1_price
    tp1_price_at_check         REAL,
    mark_vs_tp1_pct            REAL,        -- signed (mark - tp1) / tp1
    single_book_roe            REAL,        -- PW-2: ROE when treasury saw a solo book
    timestamp_ms         INTEGER NOT NULL
)
"""

# Brain census Fix 5 — CampaignPyramidEngine._states was memory-only; every
# restart zeroed layer state and adopted positions never re-registered.
_CREATE_CAMPAIGN_PYRAMID_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS campaign_pyramid_state (
    symbol           TEXT PRIMARY KEY,
    layers           INTEGER NOT NULL DEFAULT 0,
    base_size        REAL,
    base_entry       REAL,
    side             TEXT,
    last_layer_ms    INTEGER,
    total_size       REAL,
    entry_id_uuid    TEXT,
    updated_at_ms    INTEGER NOT NULL
)
"""

# Brain census Fix 6 — per-pillar health: WHY the pyramid add gate blocks
# (funding/rv_rank/oi_delta null or stale), aggregated by symbol and cause.
_CREATE_PILLAR_HEALTH_TABLE = """
CREATE TABLE IF NOT EXISTS pyramid_pillar_health (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol           TEXT NOT NULL,
    pillar           TEXT NOT NULL,   -- funding | rv_rank | oi_delta | coherence | ...
    status           TEXT NOT NULL,   -- ok | null | stale | venue_miss
    value            REAL,
    miss_reason      TEXT,
    timestamp_ms     INTEGER NOT NULL
)
"""

# Outcomes-table migration columns (2026-10-31 census, additive-only).
# ORDER IS LOAD-BEARING: _INSERT's positional VALUES tail must match this
# list exactly (both fresh and migrated DBs end with CREATE order + this order).
_OUTCOMES_MIGRATION_COLS = (
    # Leak fix: excursion plane
    ("mfe_usd", "REAL"), ("mfe_pct", "REAL"), ("mfe_at_stop_usd", "REAL"),
    ("mae_usd", "REAL"), ("mae_pct", "REAL"),
    # Leak fix: peak timing
    ("peak_pnl_usd", "REAL"), ("peak_pnl_ms", "INTEGER"),
    # Leak fix: venue + close-path + carry
    ("venue", "TEXT"),
    ("close_path_attempts", "INTEGER"), ("close_path_rejected", "INTEGER"),
    ("close_path_first_fail", "TEXT"),
    ("funding_verdict", "TEXT"), ("funding_rate_entry", "REAL"),
    # Brain census Fix 4: winner-machine engagement
    ("pyramid_legs_done", "INTEGER"), ("pyramid_tp1_cleared", "INTEGER"),
    ("ratchet_rung_reached", "TEXT"), ("ratchet_peak_roe", "REAL"),
    ("ratchet_early_arm_fired", "INTEGER"),
    ("treasury_cluster_engaged", "INTEGER"),
    ("campaign_pyramid_layers", "INTEGER"), ("brain_score", "REAL"),
    # Brain census PW-3: green-then-losing proof fields (computed at record)
    ("green_then_lost", "INTEGER"), ("green_then_lost_usd", "REAL"),
    ("early_arm_would_save_usd", "REAL"),
)

_BLOCKED_MIGRATION_COLS = (
    ("venue", "TEXT"), ("session_name", "TEXT"), ("personality", "TEXT"),
)

# Explicit column list (2026-10-31 census repair): positional VALUES relied on
# CREATE-order == ALTER-append-order, which silently diverges on migrated DBs.
# Named columns are order-immune on both fresh and migrated DBs.
_INSERT = """
INSERT OR REPLACE INTO outcomes (
    trade_id, symbol, direction, net_pnl_r, net_pnl_usd,
    exit_reason, entry_time_ms, exit_time_ms, hold_time_hours,
    calendar_regime, coherence_mult, freshness_mult, calendar_mult,
    combined_mult, slippage_actual, slippage_modelled, funding_paid_usd,
    regime, strategy_type,
    macro_fired, macro_direction, macro_confidence, macro_correct,
    regime_fired, regime_direction, regime_confidence, regime_correct,
    structure_fired, structure_direction, structure_confidence, structure_correct,
    micro_fired, micro_direction, micro_confidence, micro_correct,
    funding_fired, funding_direction, funding_confidence, funding_correct,
    ssi_fired, ssi_direction, ssi_confidence, ssi_correct,
    mfe_usd, mfe_pct, mfe_at_stop_usd, mae_usd, mae_pct,
    peak_pnl_usd, peak_pnl_ms, venue,
    close_path_attempts, close_path_rejected, close_path_first_fail,
    funding_verdict, funding_rate_entry,
    pyramid_legs_done, pyramid_tp1_cleared,
    ratchet_rung_reached, ratchet_peak_roe, ratchet_early_arm_fired,
    treasury_cluster_engaged, campaign_pyramid_layers, brain_score,
    green_then_lost, green_then_lost_usd, early_arm_would_save_usd
) VALUES (
    :trade_id, :symbol, :direction, :net_pnl_r, :net_pnl_usd,
    :exit_reason, :entry_time_ms, :exit_time_ms, :hold_time_hours,
    :calendar_regime, :coherence_mult, :freshness_mult, :calendar_mult,
    :combined_mult, :slippage_actual, :slippage_modelled, :funding_paid_usd,
    :regime, :strategy_type,
    :macro_fired, :macro_direction, :macro_confidence, :macro_correct,
    :regime_fired, :regime_direction, :regime_confidence, :regime_correct,
    :structure_fired, :structure_direction, :structure_confidence, :structure_correct,
    :micro_fired, :micro_direction, :micro_confidence, :micro_correct,
    :funding_fired, :funding_direction, :funding_confidence, :funding_correct,
    :ssi_fired, :ssi_direction, :ssi_confidence, :ssi_correct,
    :mfe_usd, :mfe_pct, :mfe_at_stop_usd, :mae_usd, :mae_pct,
    :peak_pnl_usd, :peak_pnl_ms, :venue,
    :close_path_attempts, :close_path_rejected, :close_path_first_fail,
    :funding_verdict, :funding_rate_entry,
    :pyramid_legs_done, :pyramid_tp1_cleared,
    :ratchet_rung_reached, :ratchet_peak_roe, :ratchet_early_arm_fired,
    :treasury_cluster_engaged, :campaign_pyramid_layers, :brain_score,
    :green_then_lost, :green_then_lost_usd, :early_arm_would_save_usd
)
"""

_INSERT_BLOCKED = """
INSERT INTO blocked_signals
    (symbol, direction, coherence, gate_reason, mark_price, regime, strategy_type,
     venue, session_name, personality, timestamp_ms)
VALUES
    (:symbol, :direction, :coherence, :gate_reason, :mark_price, :regime, :strategy_type,
     :venue, :session_name, :personality, :timestamp_ms)
"""

_INSERT_THROTTLE = """
INSERT INTO throttle_events
    (event_type, sub_type, symbol, venue, direction, coherence, mark_price,
     reason_detail, related_entry_id, timestamp_ms)
VALUES
    (:event_type, :sub_type, :symbol, :venue, :direction, :coherence, :mark_price,
     :reason_detail, :related_entry_id, :timestamp_ms)
"""

_INSERT_BRAIN_VERDICT = """
INSERT INTO brain_verdicts
    (brain_module, verdict_type, symbol, venue, entry_id_uuid, related_trade_id,
     allowed, reason,
     tp1_cleared, tp1_hit_at_ms, trigger_px, mark_at_check, coherence,
     rv_rank, oi_delta_pct, funding_rate, warmup_frac, trend_verdict,
     peak_roe, current_roe, cluster_size, cluster_roe, legs_done,
     mfe_at_check, atr_ratio, projected_add_qty, floor_price,
     tp1_alt_gate_price_cleared, tp1_price_at_check, mark_vs_tp1_pct,
     single_book_roe, timestamp_ms)
VALUES
    (:brain_module, :verdict_type, :symbol, :venue, :entry_id_uuid, :related_trade_id,
     :allowed, :reason,
     :tp1_cleared, :tp1_hit_at_ms, :trigger_px, :mark_at_check, :coherence,
     :rv_rank, :oi_delta_pct, :funding_rate, :warmup_frac, :trend_verdict,
     :peak_roe, :current_roe, :cluster_size, :cluster_roe, :legs_done,
     :mfe_at_check, :atr_ratio, :projected_add_qty, :floor_price,
     :tp1_alt_gate_price_cleared, :tp1_price_at_check, :mark_vs_tp1_pct,
     :single_book_roe, :timestamp_ms)
"""

_INSERT_PILLAR_HEALTH = """
INSERT INTO pyramid_pillar_health
    (symbol, pillar, status, value, miss_reason, timestamp_ms)
VALUES
    (:symbol, :pillar, :status, :value, :miss_reason, :timestamp_ms)
"""

_UPSERT_CAMPAIGN_PYRAMID_STATE = """
INSERT INTO campaign_pyramid_state
    (symbol, layers, base_size, base_entry, side, last_layer_ms, total_size,
     entry_id_uuid, updated_at_ms)
VALUES
    (:symbol, :layers, :base_size, :base_entry, :side, :last_layer_ms, :total_size,
     :entry_id_uuid, :updated_at_ms)
ON CONFLICT(symbol) DO UPDATE SET
    layers=excluded.layers, base_size=excluded.base_size,
    base_entry=excluded.base_entry, side=excluded.side,
    last_layer_ms=excluded.last_layer_ms, total_size=excluded.total_size,
    entry_id_uuid=excluded.entry_id_uuid, updated_at_ms=excluded.updated_at_ms
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
        """Create SQLite database, enable WAL mode, migrate schema if needed."""
        try:
            import aiosqlite
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(self._db_path)
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA synchronous=NORMAL")
            await self._db.execute(_CREATE_TABLE)
            await self._db.execute(_CREATE_BLOCKED_TABLE)
            await self._db.execute(_CREATE_THROTTLE_TABLE)
            await self._db.execute(_CREATE_BRAIN_VERDICTS_TABLE)
            await self._db.execute(_CREATE_CAMPAIGN_PYRAMID_STATE_TABLE)
            await self._db.execute(_CREATE_PILLAR_HEALTH_TABLE)
            # Move 2 migration: add regime + strategy_type to existing DBs
            for col, coltype in (("regime", "TEXT"), ("strategy_type", "TEXT")):
                try:
                    await self._db.execute(
                        f"ALTER TABLE outcomes ADD COLUMN {col} {coltype}"
                    )
                except Exception:
                    pass  # column already exists — safe to ignore
            # Census 2026-10-31 migrations (additive-only; duplicate ALTER is a
            # no-op via the same except-pass idiom as Move 2).
            for col, coltype in _OUTCOMES_MIGRATION_COLS:
                try:
                    await self._db.execute(
                        f"ALTER TABLE outcomes ADD COLUMN {col} {coltype}"
                    )
                except Exception:
                    pass
            for col, coltype in _BLOCKED_MIGRATION_COLS:
                try:
                    await self._db.execute(
                        f"ALTER TABLE blocked_signals ADD COLUMN {col} {coltype}"
                    )
                except Exception:
                    pass
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
            "regime":             getattr(outcome, "regime", "") or "",
            "strategy_type":      getattr(outcome, "strategy_type", "") or "",
        }
        for agent_name in _AGENT_NAMES:
            row.update(_agent_row(agent_name))

        # ── Census 2026-10-31 plane (additive; None = never tracked) ────────
        _mfe_at_stop = getattr(outcome, "mfe_at_stop_usd", None)
        _net_usd     = getattr(outcome, "net_pnl_usd", 0.0) or 0.0
        _green_then_lost = None
        if _mfe_at_stop is not None:
            _green_then_lost = int(_mfe_at_stop > 0 and _net_usd < 0)

        _legs  = getattr(outcome, "pyramid_legs_done", None)
        _rung  = getattr(outcome, "ratchet_rung_reached", None)
        _treas = getattr(outcome, "treasury_cluster_engaged", None)
        _camp  = getattr(outcome, "campaign_pyramid_layers", None)
        _brain_score = None
        if any(v is not None for v in (_legs, _rung, _treas, _camp)):
            _brain_score = round(
                (0.35 if (_legs or 0) > 0 else 0.0)
                + (0.30 if _rung not in (None, "", "none") else 0.0)
                + (0.20 if _treas else 0.0)
                + (0.15 if (_camp or 0) > 0 else 0.0),
                3,
            )

        row.update({
            "mfe_usd":               getattr(outcome, "mfe_usd", None),
            "mfe_pct":               getattr(outcome, "mfe_pct", None),
            "mfe_at_stop_usd":       _mfe_at_stop,
            "mae_usd":               getattr(outcome, "mae_usd", None),
            "mae_pct":               getattr(outcome, "mae_pct", None),
            "peak_pnl_usd":          getattr(outcome, "peak_pnl_usd", None),
            "peak_pnl_ms":           getattr(outcome, "peak_pnl_ms", None),
            "venue":                 getattr(outcome, "venue", None),
            "close_path_attempts":   getattr(outcome, "close_path_attempts", None),
            "close_path_rejected":   getattr(outcome, "close_path_rejected", None),
            "close_path_first_fail": getattr(outcome, "close_path_first_fail_reason", None),
            "funding_verdict":       getattr(outcome, "funding_verdict", None),
            "funding_rate_entry":    getattr(outcome, "funding_rate_at_entry", None),
            "pyramid_legs_done":     _legs,
            "pyramid_tp1_cleared":   _int_or_none(getattr(outcome, "pyramid_tp1_cleared", None)),
            "ratchet_rung_reached":  _rung,
            "ratchet_peak_roe":      getattr(outcome, "ratchet_peak_roe", None),
            "ratchet_early_arm_fired": _int_or_none(getattr(outcome, "ratchet_early_arm_fired", None)),
            "treasury_cluster_engaged": _int_or_none(_treas),
            "campaign_pyramid_layers": _camp,
            "brain_score":           _brain_score,
            "green_then_lost":       _green_then_lost,
            # The gift given back = MFE at stop; the early-arm estimate =
            # loss avoided had the stop been dragged to breakeven (~ -net).
            "green_then_lost_usd":   (_mfe_at_stop if _green_then_lost else None),
            "early_arm_would_save_usd": (round(-_net_usd, 6) if _green_then_lost else None),
        })
        return row

    async def record_blocked(
        self,
        symbol:        str,
        direction:     str,
        coherence:     float,
        gate_reason:   str,
        mark_price:    float,
        regime:        str = "",
        strategy_type: str = "",
        venue:         str = "",
        session_name:  str = "",
        personality:   str = "",
    ) -> None:
        """Move 1 — record every gate rejection for counterfactual gate attribution."""
        if self._db is None:
            return
        try:
            await self._db.execute(_INSERT_BLOCKED, {
                "symbol":        symbol,
                "direction":     direction,
                "coherence":     round(coherence, 3),
                "gate_reason":   gate_reason,
                "mark_price":    mark_price,
                "regime":        regime,
                "strategy_type": strategy_type,
                "venue":         venue,
                "session_name":  session_name,
                "personality":   personality,
                "timestamp_ms":  int(time.time() * 1000),
            })
            await self._db.commit()
        except Exception as e:
            log.debug("record_blocked_error", error=str(e))

    # ── Census 2026-10-31 writers (all "never raise") ───────────────────────

    async def record_throttle_event(
        self,
        event_type:       str,
        sub_type:         str,
        symbol:           str = "",
        venue:            str = "",
        direction:        str = "",
        coherence:        float = 0.0,
        mark_price:       float = 0.0,
        reason_detail:    str = "",
        related_entry_id: str = "",
        timestamp_ms:     int = 0,
    ) -> None:
        """Persist one throttle/rejection event (leak-census Surface 4)."""
        if self._db is None:
            return
        try:
            await self._db.execute(_INSERT_THROTTLE, {
                "event_type":       event_type,
                "sub_type":         sub_type,
                "symbol":           symbol,
                "venue":            venue,
                "direction":        direction,
                "coherence":        coherence,
                "mark_price":       mark_price,
                "reason_detail":    reason_detail[:500] if reason_detail else "",
                "related_entry_id": related_entry_id,
                "timestamp_ms":     timestamp_ms or int(time.time() * 1000),
            })
            await self._db.commit()
        except Exception as e:
            log.debug("record_throttle_event_error", error=str(e))

    async def record_brain_verdict(
        self,
        brain_module:     str,
        verdict_type:     str,
        allowed:          bool,
        reason:           str,
        symbol:           str = "",
        venue:            str = "",
        entry_id_uuid:    Optional[str] = None,
        related_trade_id: Optional[str] = None,
        tp1_cleared:      Optional[bool] = None,
        tp1_hit_at_ms:    Optional[int] = None,
        trigger_px:       Optional[float] = None,
        mark_at_check:    Optional[float] = None,
        coherence:        Optional[float] = None,
        rv_rank:          Optional[float] = None,
        oi_delta_pct:     Optional[float] = None,
        funding_rate:     Optional[float] = None,
        warmup_frac:      Optional[float] = None,
        trend_verdict:    Optional[str] = None,
        peak_roe:         Optional[float] = None,
        current_roe:      Optional[float] = None,
        cluster_size:     Optional[int] = None,
        cluster_roe:      Optional[float] = None,
        legs_done:        Optional[int] = None,
        mfe_at_check:     Optional[float] = None,
        atr_ratio:        Optional[float] = None,
        projected_add_qty: Optional[float] = None,
        floor_price:      Optional[float] = None,
        tp1_alt_gate_price_cleared: Optional[bool] = None,
        tp1_price_at_check:         Optional[float] = None,
        mark_vs_tp1_pct:            Optional[float] = None,
        single_book_roe:            Optional[float] = None,
        timestamp_ms:     int = 0,
    ) -> None:
        """Persist one brain decision (pyramid/treasury/ratchet/campaign)."""
        if self._db is None:
            return
        try:
            await self._db.execute(_INSERT_BRAIN_VERDICT, {
                "brain_module":     brain_module,
                "verdict_type":     verdict_type,
                "symbol":           symbol,
                "venue":            venue,
                "entry_id_uuid":    entry_id_uuid,
                "related_trade_id": related_trade_id,
                "allowed":          int(bool(allowed)),
                "reason":           reason[:200] if reason else "",
                "tp1_cleared":      _int_or_none(tp1_cleared),
                "tp1_hit_at_ms":    tp1_hit_at_ms,
                "trigger_px":       trigger_px,
                "mark_at_check":    mark_at_check,
                "coherence":        coherence,
                "rv_rank":          rv_rank,
                "oi_delta_pct":     oi_delta_pct,
                "funding_rate":     funding_rate,
                "warmup_frac":      warmup_frac,
                "trend_verdict":    trend_verdict,
                "peak_roe":         peak_roe,
                "current_roe":      current_roe,
                "cluster_size":     cluster_size,
                "cluster_roe":      cluster_roe,
                "legs_done":        legs_done,
                "mfe_at_check":     mfe_at_check,
                "atr_ratio":        atr_ratio,
                "projected_add_qty": projected_add_qty,
                "floor_price":      floor_price,
                "tp1_alt_gate_price_cleared": _int_or_none(tp1_alt_gate_price_cleared),
                "tp1_price_at_check":         tp1_price_at_check,
                "mark_vs_tp1_pct":            mark_vs_tp1_pct,
                "single_book_roe":            single_book_roe,
                "timestamp_ms":     timestamp_ms or int(time.time() * 1000),
            })
            await self._db.commit()
        except Exception as e:
            log.debug("record_brain_verdict_error", error=str(e))

    async def record_pillar_health(
        self,
        symbol:      str,
        pillar:      str,
        status:      str,
        value:       Optional[float] = None,
        miss_reason: str = "",
        timestamp_ms: int = 0,
    ) -> None:
        """Persist one per-pillar health reading (brain-census Fix 6)."""
        if self._db is None:
            return
        try:
            await self._db.execute(_INSERT_PILLAR_HEALTH, {
                "symbol":      symbol,
                "pillar":      pillar,
                "status":      status,
                "value":       value,
                "miss_reason": miss_reason[:200] if miss_reason else "",
                "timestamp_ms": timestamp_ms or int(time.time() * 1000),
            })
            await self._db.commit()
        except Exception as e:
            log.debug("record_pillar_health_error", error=str(e))

    async def save_campaign_pyramid_state(
        self,
        symbol:        str,
        layers:        int,
        base_size:     Optional[float] = None,
        base_entry:    Optional[float] = None,
        side:          str = "",
        last_layer_ms: Optional[int] = None,
        total_size:    Optional[float] = None,
        entry_id_uuid: Optional[str] = None,
    ) -> None:
        """Upsert campaign-pyramid layer state (brain-census Fix 5)."""
        if self._db is None:
            return
        try:
            await self._db.execute(_UPSERT_CAMPAIGN_PYRAMID_STATE, {
                "symbol":        symbol,
                "layers":        int(layers),
                "base_size":     base_size,
                "base_entry":    base_entry,
                "side":          side,
                "last_layer_ms": last_layer_ms,
                "total_size":    total_size,
                "entry_id_uuid": entry_id_uuid,
                "updated_at_ms": int(time.time() * 1000),
            })
            await self._db.commit()
        except Exception as e:
            log.debug("save_campaign_pyramid_state_error", error=str(e))

    async def load_campaign_pyramid_states(self) -> list:
        """Read all persisted campaign-pyramid states for boot restore."""
        if self._db is None:
            return []
        try:
            async with self._db.execute(
                "SELECT symbol, layers, base_size, base_entry, side,"
                " last_layer_ms, total_size, entry_id_uuid, updated_at_ms"
                " FROM campaign_pyramid_state"
            ) as cur:
                rows = await cur.fetchall()
            return [
                {
                    "symbol": r[0], "layers": r[1], "base_size": r[2],
                    "base_entry": r[3], "side": r[4], "last_layer_ms": r[5],
                    "total_size": r[6], "entry_id_uuid": r[7],
                    "updated_at_ms": r[8],
                }
                for r in rows
            ]
        except Exception as e:
            log.debug("load_campaign_pyramid_states_error", error=str(e))
            return []
