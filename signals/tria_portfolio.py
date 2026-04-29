#!/usr/bin/env python3
"""
signals/tria_portfolio.py — Top-5 signal basket manager for Tria execution.

Rule: maintain maximum 5 open positions. When a new signal arrives with a
higher coherence_score than the worst existing position, close the oldest
of the worst and open the new one. Stop-loss and take-profit are set at
order entry time via Tria's UI.

Usage:
    from signals.tria_portfolio import PortfolioManager
    pm = PortfolioManager()
    cmd = pm.process_signal({
        "symbol": "BTC-USD",
        "direction": "LONG",
        "size": 0.001,
        "leverage": 5,
        "coherence_score": 8.5,
        "stop_price": 75000,
        "tp1_price": 77000,
        "tp2_price": 78000,
        "tp3_price": 79000,
    })
    # cmd is a dict describing what action to take
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

PORTFOLIO_FILE = Path(__file__).parent / "tria_portfolio.json"
MAX_POSITIONS = 5


@dataclass
class Position:
    symbol: str
    direction: str
    size: float
    leverage: int
    entry_price: float
    stop_price: float
    tp1_price: float
    tp2_price: float
    tp3_price: float
    coherence_score: float
    opened_at: float = field(default_factory=time.time)
    signal_id: str = ""

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "size": self.size,
            "leverage": self.leverage,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "tp1_price": self.tp1_price,
            "tp2_price": self.tp2_price,
            "tp3_price": self.tp3_price,
            "coherence_score": self.coherence_score,
            "opened_at": self.opened_at,
            "signal_id": self.signal_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(**d)


class PortfolioManager:
    """Maintains top-N positions ranked by coherence_score."""

    def __init__(self, max_positions: int = MAX_POSITIONS):
        self.max_positions = max_positions
        self.positions: Dict[str, Position] = {}
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if PORTFOLIO_FILE.exists():
            try:
                data = json.loads(PORTFOLIO_FILE.read_text())
                self.positions = {
                    k: Position.from_dict(v) for k, v in data.get("positions", {}).items()
                }
            except (json.JSONDecodeError, TypeError):
                self.positions = {}

    def _save(self) -> None:
        PORTFOLIO_FILE.write_text(
            json.dumps(
                {"positions": {k: v.to_dict() for k, v in self.positions.items()}},
                indent=2,
            )
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def process_signal(self, signal: dict) -> dict:
        """
        Evaluate a new signal against current portfolio.

        Returns a command dict:
          {"action": "OPEN", "position": Position(...)}
          {"action": "REPLACE", "close": Position(...), "open": Position(...)}
          {"action": "HOLD", "reason": "score too low or duplicate symbol"}
        """
        sym = signal.get("symbol", "")
        score = float(signal.get("coherence_score", 0.0))
        sig_id = f"{sym}_{signal.get('timestamp', time.time())}"

        # If symbol already open, reject duplicate
        if sym in self.positions:
            existing = self.positions[sym]
            if score <= existing.coherence_score:
                return {"action": "HOLD", "reason": f"duplicate_symbol_lower_score:{existing.coherence_score}"}
            # Higher score on same symbol → close existing, open new
            old = self.positions.pop(sym)
            new_pos = self._signal_to_position(signal, sig_id)
            self.positions[sym] = new_pos
            self._save()
            return {"action": "REPLACE", "close": old.to_dict(), "open": new_pos.to_dict()}

        # Portfolio not full → open directly
        if len(self.positions) < self.max_positions:
            new_pos = self._signal_to_position(signal, sig_id)
            self.positions[sym] = new_pos
            self._save()
            return {"action": "OPEN", "position": new_pos.to_dict()}

        # Portfolio full → compare against worst position
        worst_sym, worst_pos = min(self.positions.items(), key=lambda kv: kv[1].coherence_score)
        if score <= worst_pos.coherence_score:
            return {
                "action": "HOLD",
                "reason": f"score_{score:.2f}_below_worst_{worst_pos.coherence_score:.2f}",
            }

        # Replace worst (oldest if tie) with new signal
        old = self.positions.pop(worst_sym)
        new_pos = self._signal_to_position(signal, sig_id)
        self.positions[sym] = new_pos
        self._save()
        return {"action": "REPLACE", "close": old.to_dict(), "open": new_pos.to_dict()}

    def close_position(self, symbol: str) -> Optional[dict]:
        """Manually close a position. Returns the removed position or None."""
        pos = self.positions.pop(symbol, None)
        if pos:
            self._save()
            return {"action": "CLOSE", "position": pos.to_dict()}
        return None

    def list_positions(self) -> List[dict]:
        """Return current positions sorted by coherence_score desc."""
        return [p.to_dict() for p in sorted(self.positions.values(), key=lambda x: x.coherence_score, reverse=True)]

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _signal_to_position(signal: dict, sig_id: str) -> Position:
        return Position(
            symbol=signal.get("symbol", ""),
            direction=signal.get("direction", "LONG").upper(),
            size=float(signal.get("size", 0.0)),
            leverage=int(signal.get("leverage", 5)),
            entry_price=float(signal.get("entry_price", signal.get("mark_price", 0.0))),
            stop_price=float(signal.get("stop_price", 0.0)),
            tp1_price=float(signal.get("tp1_price", 0.0)),
            tp2_price=float(signal.get("tp2_price", 0.0)),
            tp3_price=float(signal.get("tp3_price", 0.0)),
            coherence_score=float(signal.get("coherence_score", 0.0)),
            opened_at=time.time(),
            signal_id=sig_id,
        )


if __name__ == "__main__":
    # Quick CLI test
    import sys

    pm = PortfolioManager()
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        print(json.dumps(pm.list_positions(), indent=2))
    else:
        print(f"Positions: {len(pm.positions)}/{pm.max_positions}")
        for p in pm.list_positions():
            print(f"  {p['symbol']} {p['direction']} score={p['coherence_score']:.2f} size={p['size']}")
