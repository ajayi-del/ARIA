"""
intelligence/agents — ARIA's 6-agent signal perception layer.

Each agent perceives one dimension of market reality and produces a typed
AgentOutput. The system's intelligence is the product of all 6 agents working
correctly. A failing agent degrades the system in a measurable, attributable way.

Signal agents:
  MacroAgent     — MAG7.ssi capital flow (15 min cadence)
  RegimeAgent    — relative strength / regime state (15 min cadence)
  StructureAgent — ATR/market-type classification (1 min cadence)
  MicroAgent     — sweep/VPIN/orderbook (50ms cadence — hot path)
  FundingAgent   — carry/arb/funding regime (1 hour cadence)
  SSIAgent       — OI lead, CEX divergence, MAG7 momentum (15 min cadence)

The learning loop:
  Signal → Decision → Action → Outcome → Per-agent calibration → Better signal
"""

from .base import BaseAgent, AgentOutput, AgentAccuracy, TradeOutcome
from .macro_agent import MacroAgent
from .regime_agent import RegimeAgent
from .structure_agent import StructureAgent
from .micro_agent import MicroAgent
from .funding_agent import FundingAgent
from .ssi_agent import SSIAgent

__all__ = [
    "BaseAgent",
    "AgentOutput",
    "AgentAccuracy",
    "TradeOutcome",
    "MacroAgent",
    "RegimeAgent",
    "StructureAgent",
    "MicroAgent",
    "FundingAgent",
    "SSIAgent",
]
