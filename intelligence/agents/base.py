"""
intelligence/agents/base.py — Abstract base for all 6 signal agents.

Philosophical premise
─────────────────────
The learning loop is:  Signal → Decision → Action → Outcome → Calibration
Without this loop, ARIA operates in an eternal present — a system with no
memory of its own judgments, no accountability, no wisdom.

Each agent perceives one dimension of market reality and votes on direction.
`record_outcome()` closes the loop: was this agent's perception correct?
This is not error tracking — it is the system's memory of its own judgment.

Kantian framing: the noumenal market reveals itself through outcomes.
Nietzschean framing: accurate agents gain Will (sizing); inaccurate ones retreat.
Bayesian framing: each outcome is a posterior update on the agent's prior confidence.
"""

from __future__ import annotations

import time
import structlog
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

log = structlog.get_logger(__name__)


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class AgentOutput:
    """
    Immutable snapshot of one agent's perception at one moment.

    `fired=False` means the agent saw the market but found no signal.
    An unfired agent is still accountable — its silence is a vote for neutral.
    """
    agent_name:        str
    symbol:            str
    timestamp_ms:      int
    fired:             bool    # True = agent produced a signal
    direction:         str     # "long", "short", "neutral"
    confidence:        float   # 0.0–1.0
    raw_data:          dict    # agent-specific fields
    invocation_reason: str     # what triggered this agent


@dataclass
class TradeOutcome:
    """
    Fully resolved outcome of a closed position.

    Created by OutcomeRecorder when a position closes.
    Carries all the context needed to evaluate each agent's vote.
    """
    trade_id:          str
    symbol:            str
    direction:         str
    entry_price:       float = 0.0
    exit_price:        float = 0.0
    entry_time_ms:     int = 0
    exit_time_ms:      int = 0
    hold_time_hours:   float = 0.0
    gross_pnl_usd:     float = 0.0
    net_pnl_usd:       float = 0.0
    net_pnl_r:         float = 0.0   # R-multiple (pnl / initial_risk)
    funding_paid_usd:  float = 0.0
    slippage_actual:   float = 0.0
    slippage_modelled: float = 0.0
    exit_reason:       str = ""      # "tp1","tp2","tp3","stop","emergency","manual"
    calendar_regime:   str = ""
    hours_to_event:    Optional[float] = None
    coherence_mult:    float = 1.0
    freshness_mult:    float = 1.0
    calendar_mult:     float = 1.0
    combined_mult:     float = 1.0
    # Agent states at time of entry (snapshot)
    agent_outputs:     dict = field(default_factory=dict)   # {agent_name: AgentOutput}
    # Computed after close by OutcomeRecorder
    agents_correct:    dict = field(default_factory=dict)   # {agent_name: bool | None}


@dataclass
class AgentAccuracy:
    """Running accuracy record for one agent."""
    agent_name:                 str
    total_invocations:          int = 0
    total_contributing_trades:  int = 0
    wins_when_fired:            int = 0
    losses_when_fired:          int = 0

    @property
    def accuracy(self) -> float:
        """Fraction of contributing trades where agent was correct."""
        if self.total_contributing_trades == 0:
            return 0.0
        return round(self.wins_when_fired / self.total_contributing_trades, 3)

    @property
    def accuracy_pct(self) -> float:
        return round(self.accuracy * 100, 1)

    @property
    def contribution_rate(self) -> float:
        """How often this agent fires and a trade is taken."""
        if self.total_invocations == 0:
            return 0.0
        return round(self.total_contributing_trades / self.total_invocations, 3)


# ── Abstract base ─────────────────────────────────────────────────────────────

class BaseAgent(ABC):
    """
    Abstract base class for all ARIA signal agents.

    Concrete agents must implement:
      - name: str property
      - natural_frequency_seconds: float property
      - symbols: list[str] property
      - perceive(symbol, **context) -> AgentOutput
      - is_correct(output, outcome) -> bool

    The base provides record_invocation / record_outcome / get_accuracy
    for the accountability layer.
    """

    def __init__(self) -> None:
        self._accuracy = AgentAccuracy(agent_name=self.name)
        self._last_outputs: dict[str, AgentOutput] = {}   # {symbol: latest output}

    # ── Abstract interface ────────────────────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable snake_case name — used as DB column prefix."""
        ...

    @property
    @abstractmethod
    def natural_frequency_seconds(self) -> float:
        """Target invocation interval in seconds."""
        ...

    @property
    @abstractmethod
    def symbols(self) -> list:
        """Symbols this agent monitors."""
        ...

    @abstractmethod
    async def perceive(self, symbol: str, **context) -> AgentOutput:
        """
        Read market data and return a typed perception.
        Must never raise — catch internally and return neutral output.
        """
        ...

    @abstractmethod
    def is_correct(self, output: AgentOutput, outcome: TradeOutcome) -> bool:
        """
        Evaluate whether this agent's direction call was correct given the outcome.
        Must never raise — returns False on any exception.
        """
        ...

    # ── Accountability ────────────────────────────────────────────────────────

    def record_invocation(self) -> None:
        """Called each time the agent is asked to perceive."""
        self._accuracy.total_invocations += 1

    def record_outcome(self, output: AgentOutput, outcome: TradeOutcome) -> None:
        """
        Called by OutcomeRecorder when a position closes.

        This is the closure of the learning loop. The agent's prior vote
        (output.direction) is evaluated against the actual result (outcome.net_pnl_r).
        Correct agents gain empirical credit; wrong agents accumulate accountability debt.

        Philosophical note: correctness is measured by outcome, not by intention.
        A confident wrong call is worse than an uncertain right one only in aggregate.
        Each individual outcome is a discrete Bernoulli trial on the agent's judgment.
        """
        if not output.fired:
            return   # silent agents are not evaluated — silence is not a claim

        try:
            correct = self.is_correct(output, outcome)
        except Exception as e:
            log.warning("agent_is_correct_error",
                        agent=self.name, error=str(e))
            correct = False

        self._accuracy.total_contributing_trades += 1
        if correct:
            self._accuracy.wins_when_fired += 1
        else:
            self._accuracy.losses_when_fired += 1

        log.debug(
            "agent_outcome_recorded",
            agent=self.name,
            symbol=outcome.symbol,
            direction=output.direction,
            outcome_r=round(outcome.net_pnl_r, 3),
            correct=correct,
            accuracy=self._accuracy.accuracy_pct,
        )

    def get_accuracy(self) -> AgentAccuracy:
        return self._accuracy

    def get_last_output(self, symbol: str) -> AgentOutput | None:
        return self._last_outputs.get(symbol)

    def _make_neutral(self, symbol: str, reason: str = "", **raw) -> AgentOutput:
        """Helper: return a neutral/unfired output."""
        return AgentOutput(
            agent_name=self.name,
            symbol=symbol,
            timestamp_ms=int(time.time() * 1000),
            fired=False,
            direction="neutral",
            confidence=0.5,
            raw_data=raw,
            invocation_reason=reason,
        )

    def _store(self, output: AgentOutput) -> AgentOutput:
        """Cache last output per symbol and return it."""
        self._last_outputs[output.symbol] = output
        return output
