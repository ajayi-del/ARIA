"""
tria_bridge/logger.py — Structured logging for every action.

Quant principle: if it happened, it's logged. If it's not logged, it didn't happen.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional


class BridgeLogger:
    """Append-only JSONL logger with nanosecond timing."""

    def __init__(self, log_dir: str):
        os.makedirs(log_dir, exist_ok=True)
        self._log_dir = log_dir
        self._day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._path = os.path.join(log_dir, f"tria_bridge_{self._day}.jsonl")
        self._action_counter = 0

    def _rotate_if_needed(self) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day != self._day:
            self._day = day
            self._path = os.path.join(self._log_dir, f"tria_bridge_{day}.jsonl")
            self._action_counter = 0

    def _write(self, record: Dict[str, Any]) -> None:
        self._rotate_if_needed()
        self._action_counter += 1
        record["_seq"] = self._action_counter
        record["_ts"] = datetime.now(timezone.utc).isoformat()
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def info(self, event: str, **kwargs) -> None:
        self._write({"level": "info", "event": event, **kwargs})

    def warning(self, event: str, **kwargs) -> None:
        self._write({"level": "warning", "event": event, **kwargs})

    def error(self, event: str, **kwargs) -> None:
        self._write({"level": "error", "event": event, **kwargs})

    def action(
        self,
        action: str,
        latency_ms: float,
        success: bool,
        x: Optional[int] = None,
        y: Optional[int] = None,
        confidence: Optional[float] = None,
        **kwargs,
    ) -> None:
        """Log a GUI action with full telemetry."""
        self._write({
            "level": "action",
            "action": action,
            "latency_ms": round(latency_ms, 2),
            "success": success,
            "x": x,
            "y": y,
            "confidence": round(confidence, 3) if confidence is not None else None,
            **kwargs,
        })

    def trade(self, signal: Dict[str, Any], outcome: str, latency_ms: float, **kwargs) -> None:
        self._write({
            "level": "trade",
            "signal": signal,
            "outcome": outcome,
            "latency_ms": round(latency_ms, 2),
            **kwargs,
        })
