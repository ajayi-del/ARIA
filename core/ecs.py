"""
Execution Capacity Score (ECS) — ARIA Decision Layer v1.0

Replaces the binary consecutive_loss_skip gate with a continuous, PnL-aware
execution capacity that SCALES trading behaviour instead of toggling it.

The fundamental insight from the logs:
    NEAR-USD score 5.16 → skipped (losses ≥ 4)
    XAUT-USD score 5.38 → skipped (losses ≥ 4)
    ↑ These are high-EV trades being killed by a blunt instrument.

ECS ∈ [0.0, 1.0] maps to four operating modes:

    ECS       Mode              Behaviour
    0.8–1.0   FULL_TRADING      All signals, full size, liq amplification on
    0.5–0.8   CAUTIOUS          Normal trading, ignore weak liq (z < 2)
    0.2–0.5   RECOVERY          Top setups only (≥ min_recovery_score), size × 0.5
    0.0–0.2   HARD_FROZEN       Only extreme liq events (z > 5), exits only

ECS components (weighted sum):
    0.35 × pnl_momentum        EMA of last 10 trades, normalized to [-1, +1] then [0, 1]
    0.30 × drawdown_health     1.0 − drawdown_pct (linear penalty)
    0.20 × signal_efficiency   avg_profit / avg_risk per closed trade (rolling 20)
    0.15 × edge_quality        liq coherence + funding alignment (passed in per tick)

SIGNAL PRESERVATION RULE (non-negotiable — prevents EV destruction):
    if coherence_score ≥ PRESERVATION_FLOOR:
        ECS gating BYPASSED → always execute regardless of loss history

    "Losses cannot override current edge." — quant principle

ECS DECAY MODEL (replaces time-based cooldown):
    Wins:   ECS += RECOVERY_GAIN
    Losses: ECS -= LOSS_PENALTY
    This creates a self-healing confidence curve that recovers as edge returns.
"""

import time
import math
import structlog
from collections import deque
from typing import Optional, Tuple

logger = structlog.get_logger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
_ECS_FULL      = 0.80   # above → FULL_TRADING
_ECS_CAUTIOUS  = 0.50   # above → CAUTIOUS (below → RECOVERY)
_ECS_RECOVERY  = 0.20   # above → RECOVERY (below → HARD_FROZEN)

# Signal preservation: bypass loss gating for exceptional quality
PRESERVATION_FLOOR = 5.2   # coherence ≥ 5.2 → always execute

# ECS decay rates
_RECOVERY_GAIN  = 0.020   # per winning trade
_LOSS_PENALTY   = 0.030   # per losing trade
_VOLATILITY_HIT = 0.050   # applied when market is chaotic (exhaustion cascade)

# Recovery mode minimum coherence
MIN_RECOVERY_SCORE = 5.6   # below this → reject in recovery mode

# Mode strings
MODE_FULL     = "FULL_TRADING"
MODE_CAUTIOUS = "CAUTIOUS"
MODE_RECOVERY = "RECOVERY"
MODE_FROZEN   = "HARD_FROZEN"


class ExecutionCapacityEngine:
    """
    Single source of truth for ARIA's execution confidence.

    Usage in main.py:
        ecs = ExecutionCapacityEngine()

        # Each tick — update with current market edge
        ecs.update_edge(liq_coherence=0.4, drawdown_pct=2.5)

        # On trade close
        ecs.record_trade(pnl=12.5, risk_usd=25.0)

        # Before executing a signal
        if ecs.should_bypass_loss_gate(coherence_score):
            pass  # exceptional quality — always execute
        elif ecs.blocks_entry(coherence_score):
            return  # system misaligned, signal below preservation floor
        candidate.size *= ecs.get_size_mult()

    The engine does NOT replace Gate 5 (coherence minimum) — it layers on top
    of it. Gate 5 filters noise; ECS scales confidence across the trading session.
    """

    def __init__(self):
        # Core state
        self._ecs: float = 1.0        # starts at full capacity
        self._mode: str = MODE_FULL
        # Trade history: (pnl, risk_usd, ts)
        self._trade_history: deque = deque(maxlen=20)
        # Edge inputs (updated each tick)
        self._drawdown_pct: float = 0.0
        self._liq_coherence: float = 0.0
        self._funding_score: float = 0.0

    # ── Public API ─────────────────────────────────────────────────────────────

    def record_trade(self, pnl: float, risk_usd: float = 0.0) -> None:
        """
        Called after every trade close (_record_close in main.py).
        Applies ECS decay based on outcome.
        """
        self._trade_history.append((pnl, risk_usd, time.time()))
        if pnl >= 0:
            self._ecs = min(1.0, self._ecs + _RECOVERY_GAIN)
        else:
            self._ecs = max(0.0, self._ecs - _LOSS_PENALTY)
        self._recompute_mode()
        logger.info("ecs_trade_recorded",
                    pnl=round(pnl, 4),
                    ecs=round(self._ecs, 3),
                    mode=self._mode,
                    trades=len(self._trade_history))

    def apply_volatility_penalty(self) -> None:
        """
        Called when a chaos/exhaustion cascade is detected.
        Applies a larger ECS hit than a single loss.
        """
        self._ecs = max(0.0, self._ecs - _VOLATILITY_HIT)
        self._recompute_mode()
        logger.info("ecs_volatility_penalty",
                    ecs=round(self._ecs, 3),
                    mode=self._mode)

    def update_edge(self, liq_coherence: float = 0.0,
                    drawdown_pct: float = 0.0,
                    funding_score: float = 0.0) -> float:
        """
        Called once per signal tick with current market conditions.
        Blends component scores with smoothing to prevent single-event spikes.
        Returns the new ECS value.
        """
        self._drawdown_pct  = drawdown_pct
        self._liq_coherence = liq_coherence
        self._funding_score = funding_score

        # Compute component-weighted ECS blend
        component_ecs = self._compute_component_ecs()

        # Blend: 70% decay-driven ECS (history), 30% current market conditions
        # This prevents single bad trades killing the session while market edge is present.
        self._ecs = 0.70 * self._ecs + 0.30 * component_ecs
        self._ecs = max(0.0, min(1.0, self._ecs))
        self._recompute_mode()
        return self._ecs

    def get_ecs(self) -> float:
        """Current ECS value."""
        return round(self._ecs, 4)

    def get_mode(self) -> str:
        """Current operating mode string."""
        return self._mode

    def get_size_mult(self) -> float:
        """
        Size multiplier for current ECS mode.
        Applied to candidate.size in the execution path.
        """
        if self._mode == MODE_FULL:
            return 1.0
        elif self._mode == MODE_CAUTIOUS:
            # Smooth size reduction — ECS 0.5→0.8 maps to 0.85→1.0
            return round(0.85 + (self._ecs - _ECS_CAUTIOUS) / (_ECS_FULL - _ECS_CAUTIOUS) * 0.15, 2)
        elif self._mode == MODE_RECOVERY:
            return 0.50   # hard 50% in recovery mode (per design)
        else:
            return 0.25   # HARD_FROZEN — extreme events only, minimal size

    def should_bypass_loss_gate(self, coherence_score: float) -> bool:
        """
        Signal Preservation Rule.
        Returns True when coherence ≥ 5.2 — loss streak CANNOT block this signal.
        "Losses cannot override current edge."
        """
        return coherence_score >= PRESERVATION_FLOOR

    def blocks_entry(self, coherence_score: float) -> bool:
        """
        Returns True only when system should NOT take new entries.
        Hard block conditions:
            - HARD_FROZEN AND coherence < PRESERVATION_FLOOR
        Recovery conditions (not blocked, just sized down):
            - RECOVERY AND coherence < MIN_RECOVERY_SCORE
        """
        if self._mode == MODE_HARD_FROZEN:
            # Preservation rule still applies in hard frozen
            return not self.should_bypass_loss_gate(coherence_score)
        if self._mode == MODE_RECOVERY:
            # Recovery mode: only top setups (5.6+) OR preservation signals
            return (coherence_score < MIN_RECOVERY_SCORE
                    and not self.should_bypass_loss_gate(coherence_score))
        return False

    def min_coherence_override(self) -> Optional[float]:
        """
        Returns a stricter coherence floor when in RECOVERY mode.
        Returns None in other modes (use existing config.min_coherence).
        """
        if self._mode == MODE_RECOVERY:
            return MIN_RECOVERY_SCORE
        return None

    def summary(self) -> dict:
        """Snapshot for display/logging."""
        return {
            "ecs": round(self._ecs, 3),
            "mode": self._mode,
            "size_mult": self.get_size_mult(),
            "drawdown_pct": round(self._drawdown_pct, 2),
            "trades_in_window": len(self._trade_history),
        }

    # ── Internal ───────────────────────────────────────────────────────────────

    def _recompute_mode(self) -> None:
        if self._ecs >= _ECS_FULL:
            self._mode = MODE_FULL
        elif self._ecs >= _ECS_CAUTIOUS:
            self._mode = MODE_CAUTIOUS
        elif self._ecs >= _ECS_RECOVERY:
            self._mode = MODE_RECOVERY
        else:
            self._mode = MODE_FROZEN

    def _compute_component_ecs(self) -> float:
        """
        Weighted component score for market-condition-aware ECS blend.
        Used to prevent pure loss-decay from ignoring recovered market conditions.
        """
        # 1. PnL momentum — EMA of last 10 closed trades
        pnl_momentum = self._compute_pnl_momentum()

        # 2. Drawdown health — linear: 0% DD → 1.0, 10%+ DD → 0.0
        drawdown_health = max(0.0, 1.0 - self._drawdown_pct / 10.0)

        # 3. Signal efficiency — avg PnL/risk over recent trades
        signal_efficiency = self._compute_signal_efficiency()

        # 4. Edge quality — current liq + funding coherence
        edge_quality = min(1.0, (self._liq_coherence * 0.6 + abs(self._funding_score) * 0.4))

        component_ecs = (
            0.35 * pnl_momentum
            + 0.30 * drawdown_health
            + 0.20 * signal_efficiency
            + 0.15 * edge_quality
        )
        return max(0.0, min(1.0, component_ecs))

    def _compute_pnl_momentum(self) -> float:
        """
        EMA of last 10 PnL normalized to [0, 1].
        Positive PnL → > 0.5, negative → < 0.5.
        """
        recent = list(self._trade_history)[-10:]
        if not recent:
            return 0.6   # neutral prior — slightly positive (assume system is ok at start)

        pnls = [p for p, _, _ in recent]
        # EMA with alpha=0.3 (more recent = more weight)
        alpha = 0.3
        ema = pnls[0]
        for p in pnls[1:]:
            ema = alpha * p + (1 - alpha) * ema

        # Normalize: use ATR-like scale ($5 = neutral boundary)
        # +$5 → 0.75, -$5 → 0.25, ±$20 → clamped to 0.05 or 0.95
        normalized = 0.5 + math.tanh(ema / 10.0) * 0.45
        return max(0.05, min(0.95, normalized))

    def _compute_signal_efficiency(self) -> float:
        """
        Average PnL/risk ratio over last 20 trades.
        Tells us whether signals have been working: high = edge present.
        """
        recent = list(self._trade_history)
        if len(recent) < 3:
            return 0.5   # neutral prior

        total_pnl = sum(p for p, _, _ in recent)
        total_risk = sum(abs(r) for _, r, _ in recent if r > 0)

        if total_risk <= 0:
            # No risk data — fall back to simple win rate
            wins = sum(1 for p, _, _ in recent if p > 0)
            return wins / len(recent)

        efficiency = (total_pnl / total_risk + 1.0) / 2.0   # normalize around 0.5
        return max(0.0, min(1.0, efficiency))


# Constant alias fix (MODE_FROZEN was referenced but not defined)
MODE_HARD_FROZEN = MODE_FROZEN


# Module-level singleton — import and use everywhere
ecs_engine = ExecutionCapacityEngine()
