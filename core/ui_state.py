"""
core/ui_state.py — Shared state bridge between trading loop and UI.

Trading loop writes via update_*() — non-blocking dict replacement.
UI endpoint reads via snapshot() — returns a shallow copy.

No locks needed: Python GIL protects simple dict writes.
Values are replaced atomically, not mutated in-place.
"""

from __future__ import annotations

import time
from typing import Any


class UIState:
    """
    In-memory state shared between the trading loop and the UI HTTP endpoint.

    All writes are O(1) dict replacements — safe on the hot path.
    All reads return a shallow copy suitable for JSON serialisation.
    """

    def __init__(self) -> None:
        self._state: dict[str, Any] = {
            "ts":               0,
            "kant":             {},
            "nietzsche":        {},
            "predictions":      [],
            "active_bets":      [],
            "resolved_bets":    [],
            "conviction":       {},
            "regime":           "CONFUSED",
            "will_state":       "NEUTRAL",
            "market_structure": "TREND",
            "feed":             [],   # last 30 decisions
            "session":          {},
            "positions":        [],
            "balance":          0.0,
        }

    # ── Writers (called from trading hot path) ────────────────────────────────

    def update_kant(
        self,
        symbol:        str,
        structure:     str,
        confidence:    float,
        coherence_min: float,
        order_type:    str,
        size_cap:      float,
    ) -> None:
        self._state["kant"] = {
            "symbol":        symbol,
            "structure":     structure,
            "confidence":    confidence,
            "coherence_min": coherence_min,
            "order_type":    order_type,
            "size_cap":      size_cap,
            "ts":            time.time(),
        }
        self._state["market_structure"] = structure

    def update_nietzsche(
        self,
        symbol:     str,
        will_state: str,
        size_mult:  float,
        order_type: str,
        reason:     str,
        conviction: float,
    ) -> None:
        self._state["nietzsche"] = {
            "symbol":     symbol,
            "will_state": will_state,
            "size_mult":  size_mult,
            "order_type": order_type,
            "reason":     reason,
            "conviction": conviction,
            "ts":         time.time(),
        }
        self._state["will_state"] = will_state

    def update_conviction(
        self,
        symbol:     str,
        conviction: float,
        coherence:  float,
        hist_wr:    float,
    ) -> None:
        self._state["conviction"] = {
            "symbol":     symbol,
            "conviction": conviction,
            "coherence":  coherence,
            "hist_wr":    hist_wr,
            "ts":         time.time(),
        }

    def add_feed_entry(
        self,
        agent:          str,
        symbol:         str,
        direction:      str,
        score:          float,
        result:         str,
        reason:         str | None,
        personality:    str,
        kant_structure: str | None,
        will_state:     str | None,
        conviction:     float | None,
        ml_prob:        float | None,
    ) -> None:
        entry = {
            "ts":             time.time(),
            "time_str":       time.strftime("%H:%M:%S"),
            "agent":          agent,
            "symbol":         symbol,
            "direction":      direction,
            "score":          score,
            "result":         result,
            "reason":         reason,
            "personality":    personality,
            "kant_structure": kant_structure,
            "will_state":     will_state,
            "conviction":     conviction,
            "ml_prob":        ml_prob,
        }
        feed: list = self._state["feed"]
        feed.insert(0, entry)
        self._state["feed"] = feed[:30]   # keep last 30

    def update_predictions(
        self,
        active:    list,
        bets:      list,
        resolved:  list,
        accuracy:  float,
    ) -> None:
        self._state["predictions"]   = active
        self._state["active_bets"]   = bets
        self._state["resolved_bets"] = resolved
        self._state["pred_accuracy"] = accuracy

    def update_regime(
        self,
        regime:     str,
        confidence: float,
    ) -> None:
        self._state["regime"] = regime.upper()
        self._state["regime_confidence"] = round(confidence, 3)

    def update_session(
        self,
        balance:         float,
        wr:              float,
        trades:          int,
        pnl:             float,
        drawdown_pct:    float,
        will_state:      str,
        open_positions:  int,
    ) -> None:
        self._state["session"] = {
            "balance":        balance,
            "wr":             wr,
            "trades":         trades,
            "pnl":            pnl,
            "drawdown_pct":   drawdown_pct,
            "will_state":     will_state,
            "open_positions": open_positions,
            "ts":             time.time(),
        }
        self._state["balance"] = balance

    # ── Reader (called from HTTP handler) ─────────────────────────────────────

    def snapshot(self) -> dict:
        """Shallow copy — safe for JSON serialisation."""
        s = dict(self._state)
        s["ts"] = time.time()
        return s


# Singleton — import this wherever state needs to be written or read.
ui_state = UIState()
