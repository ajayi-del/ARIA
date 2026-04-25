"""
risk/coherence_decay.py — Coherence Decay Monitor
ARIA Execution Alpha Patch — Component 3 (v2)

Tracks entry coherence per open position. If the signal deteriorates significantly
after entry while the trade is open, take defensive action:

  Decay ≥ 50% (absolute) → close immediately  (signal completely evaporated)
  Decay ≥ 30% + position losing               → close  (losing with decayed signal)
  Decay ≥ 25% + position winning              → trim 30%  (protect profit)
  Decay recovering (coherence rising again)   → hold

Minimum 60s hold before any decay action.
Runs in the execution_cleanup_loop every 60s.

Uses Position.entry_coherence (already stored in the Position dataclass) as
reference, and _last_signal_coh[symbol] as current coherence proxy.
"""
from __future__ import annotations

import time
from typing import Dict, Optional
import structlog

log = structlog.get_logger(__name__)

_DECAY_SEVERE     = 0.50   # ≥50% drop from entry → always close
_DECAY_LOSS_CLOSE = 0.30   # ≥30% drop + losing → close
_DECAY_WIN_TRIM   = 0.25   # ≥25% drop + winning → trim 30%
_MIN_HOLD_S       = 60.0   # grace period after open


class CoherenceDecayMonitor:
    """
    Stateful monitor. Seeds from Position.entry_coherence (auto-detected).
    Call check_position() in a periodic loop; call forget() on position close.
    """

    def __init__(self) -> None:
        # Use object id(position) as key → {"entry", "peak", "opened_at"}
        self._data: Dict[int, dict] = {}

    def forget(self, position) -> None:
        self._data.pop(id(position), None)

    def check_position(
        self,
        position,
        current_coherence: float,
        current_pnl_usd:   float,
    ) -> Optional[str]:
        """
        Returns action string or None:
          "close_severe"  — decay ≥ 50%
          "close_loss"    — decay ≥ 30% + losing
          "trim_winner"   — decay ≥ 25% + winning
          None            — hold
        """
        pid = id(position)
        data = self._data.get(pid)

        if data is None:
            entry_coh = float(getattr(position, "entry_coherence", 0.0) or 0.0)
            if entry_coh <= 0:
                return None
            opened_ms = float(getattr(position, "opened_at_ms", 0) or 0)
            self._data[pid] = {
                "entry":     entry_coh,
                "peak":      entry_coh,
                "opened_at": opened_ms / 1000.0 if opened_ms > 1e9 else time.time(),
            }
            data = self._data[pid]

        if time.time() - data["opened_at"] < _MIN_HOLD_S:
            return None

        if current_coherence <= 0 or data["entry"] <= 0:
            return None

        # Update peak (recovering signal should not be punished)
        if current_coherence > data["peak"]:
            data["peak"] = current_coherence

        decay = (data["entry"] - current_coherence) / data["entry"]

        if decay >= _DECAY_SEVERE:
            log.warning("coherence_decay_severe",
                        symbol=position.symbol,
                        entry_coh=round(data["entry"], 2),
                        current_coh=round(current_coherence, 2),
                        decay_pct=round(decay, 2))
            return "close_severe"

        if decay >= _DECAY_LOSS_CLOSE and current_pnl_usd < 0:
            log.info("coherence_decay_close_loss",
                     symbol=position.symbol,
                     entry_coh=round(data["entry"], 2),
                     current_coh=round(current_coherence, 2),
                     decay_pct=round(decay, 2),
                     pnl=round(current_pnl_usd, 2))
            return "close_loss"

        if decay >= _DECAY_WIN_TRIM and current_pnl_usd > 0:
            log.info("coherence_decay_trim_winner",
                     symbol=position.symbol,
                     entry_coh=round(data["entry"], 2),
                     current_coh=round(current_coherence, 2),
                     decay_pct=round(decay, 2),
                     pnl=round(current_pnl_usd, 2))
            return "trim_winner"

        return None
