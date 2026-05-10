#!/usr/bin/env python3
"""
signals/tria_executor.py — Execute Tria commands via the Tria bridge state machine.

Polls tria_commands.json, executes new OPEN commands.
CLOSE commands are logged for manual action (Tria bridge state machine
is currently open-only; extend this script to handle closes via
counter-trade or close-position template when ready).

Usage:
    TRIA_CONFIRMATION=false python signals/tria_executor.py   # auto-execute
    python signals/tria_executor.py                           # confirm each trade

Requires Tria web page to be visible on screen.
Set TRIA_BROWSER_LEFT/TOP/WIDTH/HEIGHT env vars if Tria is not fullscreen.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tria_bridge.config import BridgeConfig
from tria_bridge.executor import Executor
from tria_bridge.logger import BridgeLogger
from tria_bridge.state_machine import TradeSignal, TradeStateMachine
from tria_bridge.vision import VisionEngine

COMMAND_FILE = Path(__file__).parent / "tria_commands.json"
PROCESSED_LOG = Path(__file__).parent / ".tria_executed_ids"
POLL_INTERVAL_S = 3.0

# Hard requirement: abort if SL/TP templates missing and prices are present
REQUIRE_SL_TP = os.getenv("TRIA_REQUIRE_SL_TP", "true").lower() == "true"


def _load_processed() -> set[str]:
    if PROCESSED_LOG.exists():
        return set(PROCESSED_LOG.read_text().strip().splitlines())
    return set()


def _save_processed(ids: set[str]) -> None:
    PROCESSED_LOG.write_text("\n".join(sorted(ids)) + "\n")


def _command_to_signal(cmd: dict) -> TradeSignal:
    """Convert tria_commands.json entry to TradeSignal."""
    _tp = cmd.get("tp1_price") if cmd.get("tp1_price") is not None else cmd.get("tp_price")
    return TradeSignal(
        symbol=str(cmd.get("symbol", "")),
        direction=str(cmd.get("direction", "LONG")).upper(),
        size=float(cmd.get("size", 0.0)),
        leverage=float(cmd["leverage"]) if cmd.get("leverage") is not None else None,
        stop_price=float(cmd["stop_price"]) if cmd.get("stop_price") is not None else None,
        tp_price=float(_tp) if _tp is not None else None,
        notional_usd=float(cmd["notional_usd"]) if cmd.get("notional_usd") is not None else None,
        source=str(cmd.get("source", "aria")),
        timestamp=float(cmd.get("timestamp", time.time())),
    )


def _check_templates(cfg: BridgeConfig) -> list[str]:
    """Return list of missing critical template files."""
    import tria_bridge.config as cfg_mod
    missing = []
    for name in (
        cfg_mod.TEMPLATE_SYMBOL_SEARCH,
        cfg_mod.TEMPLATE_SYMBOL_SELECT,
        cfg_mod.TEMPLATE_BUY_BUTTON,
        cfg_mod.TEMPLATE_SELL_BUTTON,
        cfg_mod.TEMPLATE_SIZE_FIELD,
        cfg_mod.TEMPLATE_CONFIRM_ORDER,
    ):
        path = Path(cfg.asset_dir) / name
        if not path.exists():
            missing.append(name)
    return missing


def main() -> None:
    print("[tria_executor] Starting Tria bridge executor...")
    print(f"[tria_executor] Watching: {COMMAND_FILE}")

    cfg = BridgeConfig.from_env()
    log = BridgeLogger(cfg.log_dir)
    vis = VisionEngine(cfg, log)
    exe = Executor(cfg, log)

    # Check critical templates
    missing = _check_templates(cfg)
    if missing:
        print(f"[tria_executor] WARNING: missing critical templates: {missing}")
        print("[tria_executor] Trades may fail. Capture templates in tria_bridge/assets/")

    # Check SL/TP templates
    import tria_bridge.config as cfg_mod
    sl_missing = not (Path(cfg.asset_dir) / cfg_mod.TEMPLATE_STOP_LOSS_FIELD).exists()
    tp_missing = not (Path(cfg.asset_dir) / cfg_mod.TEMPLATE_TAKE_PROFIT_FIELD).exists()
    if sl_missing or tp_missing:
        print(f"[tria_executor] WARNING: SL/TP templates missing — SL={sl_missing} TP={tp_missing}")
        if REQUIRE_SL_TP:
            print("[tria_executor] REQUIRE_SL_TP=true: trades with SL/TP will ABORT.")
            print("[tria_executor] Capture the missing templates or set TRIA_REQUIRE_SL_TP=false")

    processed = _load_processed()
    print(f"[tria_executor] Confirmation required: {cfg.confirmation_required}")
    print(f"[tria_executor] Press Ctrl+C to stop.\n")

    while True:
        if not COMMAND_FILE.exists():
            time.sleep(POLL_INTERVAL_S)
            continue

        try:
            with open(COMMAND_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            time.sleep(POLL_INTERVAL_S)
            continue

        commands = data if isinstance(data, list) else [data]
        for cmd in commands:
            cmd_id = f"{cmd.get('symbol','')}_{cmd.get('timestamp',0)}_{cmd.get('action','')}_{cmd.get('direction','')}"
            if cmd_id in processed:
                continue

            action = cmd.get("action", "")
            symbol = cmd.get("symbol", "")

            if action == "CLOSE":
                pos_dir = str(cmd.get("direction", "")).upper()
                pos_size = float(cmd.get("size", 0))
                if not pos_dir or pos_size <= 0:
                    print(f"\n[SKIP CLOSE] {symbol} — missing direction or size")
                    processed.add(cmd_id)
                    _save_processed(processed)
                    continue

                counter_dir = "SHORT" if pos_dir == "LONG" else "LONG"
                close_signal = TradeSignal(
                    symbol=symbol,
                    direction=counter_dir,
                    size=pos_size,
                    leverage=float(cmd["leverage"]) if cmd.get("leverage") is not None else None,
                    stop_price=None,
                    tp_price=None,
                    source="tria_close",
                    timestamp=float(cmd.get("timestamp", time.time())),
                )
                err = close_signal.validate()
                if err:
                    print(f"\n[INVALID CLOSE] {err}")
                    processed.add(cmd_id)
                    _save_processed(processed)
                    continue

                print(f"\n[CLOSE COMMAND] {symbol} {pos_dir} -> counter-{counter_dir} size={pos_size}")

                if cfg.confirmation_required:
                    confirm = input("  Execute close on Tria? [y/N/q]: ").strip().lower()
                    if confirm == "q":
                        print("[tria_executor] Quit.")
                        return
                    if confirm != "y":
                        print("  [SKIPPED]")
                        processed.add(cmd_id)
                        _save_processed(processed)
                        continue

                sm = TradeStateMachine(cfg, log, vis, exe)
                result = sm.execute(close_signal)

                if result.success:
                    print(f"  [SUCCESS] Close initiated in {result.latency_ms:.0f}ms")
                    if not result.fill_confirmed:
                        print("  [WARNING] Fill not visually confirmed — verify on Tria")
                else:
                    print(f"  [FAILED] {result.error}")

                processed.add(cmd_id)
                _save_processed(processed)
                continue

            if action != "OPEN":
                processed.add(cmd_id)
                _save_processed(processed)
                continue

            signal = _command_to_signal(cmd)
            err = signal.validate()
            if err:
                print(f"\n[INVALID SIGNAL] {err}")
                processed.add(cmd_id)
                _save_processed(processed)
                continue

            # Abort if SL/TP required but templates missing
            if REQUIRE_SL_TP and (signal.stop_price or signal.tp_price):
                if sl_missing or tp_missing:
                    print(f"\n[ABORT] {symbol} {signal.direction}")
                    print(f"  SL={signal.stop_price} TP={signal.tp_price}")
                    print(f"  Missing templates prevent safe SL/TP entry.")
                    print(f"  Capture templates or set TRIA_REQUIRE_SL_TP=false")
                    processed.add(cmd_id)
                    _save_processed(processed)
                    continue

            print(f"\n[OPEN COMMAND] {symbol} {signal.direction}")
            print(f"  size={signal.size} leverage={signal.leverage}")
            print(f"  SL={signal.stop_price} TP={signal.tp_price}")
            print(f"  notional=${signal.notional_usd}")

            if cfg.confirmation_required:
                confirm = input("  Execute on Tria? [y/N/q]: ").strip().lower()
                if confirm == "q":
                    print("[tria_executor] Quit.")
                    return
                if confirm != "y":
                    print("  [SKIPPED]")
                    processed.add(cmd_id)
                    _save_processed(processed)
                    continue

            sm = TradeStateMachine(cfg, log, vis, exe)
            result = sm.execute(signal)

            if result.success:
                print(f"  [SUCCESS] {result.state_reached.name} in {result.latency_ms:.0f}ms")
                if not result.fill_confirmed:
                    print("  [WARNING] Fill not visually confirmed — verify on Tria")
            else:
                print(f"  [FAILED] {result.error}")

            processed.add(cmd_id)
            _save_processed(processed)

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
