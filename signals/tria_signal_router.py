#!/usr/bin/env python3
"""
signals/tria_signal_router.py — Connects ARIA outbox to Tria portfolio manager.

Watches aria_outbox.json, runs portfolio replacement logic, and writes
trade commands to tria_commands.json for Open Interpreter to execute.

Usage:
    python signals/tria_signal_router.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from tria_portfolio import PortfolioManager

OUTBOX = Path(__file__).parent / "aria_outbox.json"
COMMAND_FILE = Path(__file__).parent / "tria_commands.json"
PROCESSED_LOG = Path(__file__).parent / ".processed_ids"
POLL_INTERVAL_S = 2.0


def _load_processed() -> set[str]:
    if PROCESSED_LOG.exists():
        return set(PROCESSED_LOG.read_text().strip().splitlines())
    return set()


def _save_processed(ids: set[str]) -> None:
    PROCESSED_LOG.write_text("\n".join(sorted(ids)) + "\n")


def _write_command(cmd: dict) -> None:
    """Append command to tria_commands.json for Open Interpreter."""
    existing: list = []
    if COMMAND_FILE.exists():
        try:
            existing = json.loads(COMMAND_FILE.read_text())
            if not isinstance(existing, list):
                existing = []
        except (json.JSONDecodeError, OSError):
            existing = []
    existing.append(cmd)
    COMMAND_FILE.write_text(json.dumps(existing, indent=2))


def main() -> None:
    print("[tria_signal_router] Starting...")
    print(f"[tria_signal_router] Outbox: {OUTBOX}")
    print(f"[tria_signal_router] Commands: {COMMAND_FILE}")

    pm = PortfolioManager()
    processed = _load_processed()

    while True:
        if not OUTBOX.exists():
            time.sleep(POLL_INTERVAL_S)
            continue

        try:
            data = json.loads(OUTBOX.read_text())
        except (json.JSONDecodeError, OSError):
            time.sleep(POLL_INTERVAL_S)
            continue

        signals = data if isinstance(data, list) else [data]
        for sig in signals:
            sig_id = sig.get("id") or f"{sig.get('symbol')}_{sig.get('timestamp')}"
            if sig_id in processed:
                continue

            print(f"\n[NEW SIGNAL] {sig.get('symbol')} {sig.get('direction')} score={sig.get('coherence_score')}")
            result = pm.process_signal(sig)
            action = result.get("action")

            if action == "OPEN":
                pos = result["position"]
                cmd = {
                    "action": "OPEN",
                    "symbol": pos["symbol"],
                    "direction": pos["direction"],
                    "size": pos["size"],
                    "leverage": pos["leverage"],
                    "entry_price": pos["entry_price"],
                    "stop_price": pos["stop_price"],
                    "tp1_price": pos["tp1_price"],
                    "tp2_price": pos["tp2_price"],
                    "tp3_price": pos["tp3_price"],
                    "reason": f"score={pos['coherence_score']:.2f}",
                    "timestamp": time.time(),
                }
                _write_command(cmd)
                print(f"[COMMAND] OPEN {pos['direction']} {pos['size']} {pos['symbol']} @ {pos['entry_price']}")

            elif action == "REPLACE":
                old = result["close"]
                new = result["open"]
                cmd_close = {
                    "action": "CLOSE",
                    "symbol": old["symbol"],
                    "reason": f"replaced_by_{new['symbol']}_score_{new['coherence_score']:.2f}",
                    "timestamp": time.time(),
                }
                cmd_open = {
                    "action": "OPEN",
                    "symbol": new["symbol"],
                    "direction": new["direction"],
                    "size": new["size"],
                    "leverage": new["leverage"],
                    "entry_price": new["entry_price"],
                    "stop_price": new["stop_price"],
                    "tp1_price": new["tp1_price"],
                    "tp2_price": new["tp2_price"],
                    "tp3_price": new["tp3_price"],
                    "reason": f"score={new['coherence_score']:.2f}",
                    "timestamp": time.time(),
                }
                _write_command(cmd_close)
                _write_command(cmd_open)
                print(f"[COMMAND] CLOSE {old['symbol']} (replaced)")
                print(f"[COMMAND] OPEN {new['direction']} {new['size']} {new['symbol']} @ {new['entry_price']}")

            elif action == "HOLD":
                print(f"[HOLD] {result['reason']}")

            processed.add(sig_id)
            _save_processed(ids=processed)

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
