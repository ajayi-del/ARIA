"""
tria_bridge/emergency_stop.py — Standalone kill switch.

Usage:
    python -m tria_bridge.emergency_stop

Actions:
  1. Writes STOP to kill switch file
  2. Logs emergency event
  3. (Optional) Attempts to close all positions on Tria via GUI

Keep this script accessible from your desktop / hotkey.
"""

from __future__ import annotations

import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from tria_bridge.config import BridgeConfig, KILL_SWITCH_FILE
from tria_bridge.logger import BridgeLogger


def write_kill_switch(reason: str = "manual") -> None:
    path = KILL_SWITCH_FILE
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("STOP")
    print(f"[EMERGENCY] Kill switch written: {path} (reason={reason})")


def close_all_positions() -> None:
    """
    Attempt to close all positions via GUI.
    This is best-effort; if the bridge is already running, it may conflict.
    Run this ONLY when the bridge is halted.
    """
    print("[EMERGENCY] Position close not yet implemented — do manually.")
    # Future: instantiate VisionEngine + Executor and click close buttons


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Tria Bridge Emergency Stop")
    parser.add_argument("--reason", default="manual", help="Reason for stop")
    parser.add_argument("--close-positions", action="store_true", help="Attempt GUI position close")
    args = parser.parse_args()

    log = BridgeLogger(os.path.join(PROJECT_ROOT, "logs", "tria_bridge"))
    log.error("emergency_stop_invoked", reason=args.reason)

    write_kill_switch(args.reason)

    if args.close_positions:
        close_all_positions()

    print("[EMERGENCY] Bridge will halt on next safety check (~1s max).")


if __name__ == "__main__":
    main()
