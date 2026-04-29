#!/usr/bin/env python3
"""
Guided UI Template Capture for Tria Bridge.

Usage:
    python capture_guided.py

This script will walk you through capturing each required template one by one.
For each template:
  1. It tells you exactly what to capture
  2. Waits for you to position your mouse at the TOP-LEFT corner
  3. Waits for you to position your mouse at the BOTTOM-RIGHT corner
  4. Automatically crops and saves the template

No thinking required. Just follow the prompts.
"""

import os
import sys
import time

# ── Dependencies check ──────────────────────────────────────────────────────
try:
    import cv2
    import numpy as np
    import mss
except ImportError:
    print("Missing dependencies. Install them now by running:")
    print("  pip install opencv-python numpy mss pyautogui")
    sys.exit(1)

try:
    import pyautogui
except ImportError:
    print("Missing pyautogui. Install it:")
    print("  pip install pyautogui")
    sys.exit(1)

# ── Configuration ───────────────────────────────────────────────────────────
ASSET_DIR = os.path.dirname(os.path.abspath(__file__))
DELAY_SECONDS = 4  # Time to move mouse between corners

TEMPLATES = [
    {
        "name": "symbol_search",
        "desc": "The SEARCH / SPOTTER input field on Tria (where you type 'BTC')",
        "tip": "Click into the search bar first so it's active, then capture the full input box.",
    },
    {
        "name": "symbol_select",
        "desc": "The FIRST dropdown result after typing a symbol",
        "tip": "Type 'BTC' in the search bar, then capture the first row that appears below.",
    },
    {
        "name": "buy_button",
        "desc": "The GREEN / LONG / BUY button",
        "tip": "Make sure your mouse is FAR away from the button (not hovering). Capture the full button.",
    },
    {
        "name": "sell_button",
        "desc": "The RED / SHORT / SELL button",
        "tip": "Make sure your mouse is FAR away (not hovering). Capture the full button.",
    },
    {
        "name": "size_field",
        "desc": "The SIZE / NOTIONAL input field",
        "tip": "Capture the entire input box including its label (e.g. 'Size').",
    },
    {
        "name": "leverage_dropdown",
        "desc": "The LEVERAGE selector (closed state)",
        "tip": "Capture the dropdown that shows your current leverage (e.g. '5x').",
    },
    {
        "name": "confirm_leverage",
        "desc": "The CONFIRM button inside the leverage popup",
        "tip": "Click the leverage dropdown, then capture the button that applies the change.",
    },
    {
        "name": "confirm_order",
        "desc": "The final PLACE ORDER / CONFIRM button",
        "tip": "After entering size, capture the big button that submits the trade.",
    },
    {
        "name": "filled_confirmed",
        "desc": "The ORDER FILLED success message / toast",
        "tip": "You may need to place a tiny trade to see this. Capture the green success popup.",
    },
]


def wait_for_key(prompt: str) -> None:
    """Block until user presses Enter."""
    try:
        input(prompt)
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        sys.exit(0)


def get_mouse_pos() -> tuple:
    """Current mouse position."""
    return pyautogui.position()


def capture_region(x1: int, y1: int, x2: int, y2: int, save_path: str) -> None:
    """Crop and save a screen region."""
    left = min(x1, x2)
    top = min(y1, y2)
    width = abs(x2 - x1)
    height = abs(y2 - y1)

    if width < 10 or height < 10:
        print(f"  WARNING: Region very small ({width}x{height}). Retake if needed.")

    with mss.mss() as sct:
        region = {"left": left, "top": top, "width": width, "height": height}
        img = np.array(sct.grab(region))
        bgr = img[:, :, :3]  # Drop alpha
        cv2.imwrite(save_path, bgr)

    print(f"  SAVED: {save_path} ({width}x{height})")
    print()


def capture_template(template: dict) -> bool:
    """Guide user through capturing one template. Returns True if captured."""
    name = template["name"]
    desc = template["desc"]
    tip = template["tip"]
    save_path = os.path.join(ASSET_DIR, f"{name}.png")

    print("=" * 60)
    print(f"TEMPLATE: {name}")
    print(f"CAPTURE:  {desc}")
    print(f"TIP:      {tip}")
    print("-" * 60)

    # Check if already exists
    if os.path.exists(save_path):
        print(f"File already exists: {save_path}")
        wait_for_key("Press Enter to OVERWRITE, or Ctrl+C to skip this template... ")
        os.remove(save_path)

    wait_for_key(
        f"STEP 1: Move your mouse to the TOP-LEFT corner of '{name}'.\n"
        f"         Then press Enter..."
    )
    x1, y1 = get_mouse_pos()
    print(f"         Top-left captured: ({x1}, {y1})")

    wait_for_key(
        f"STEP 2: Move your mouse to the BOTTOM-RIGHT corner of '{name}'.\n"
        f"         Then press Enter..."
    )
    x2, y2 = get_mouse_pos()
    print(f"         Bottom-right captured: ({x2}, {y2})")

    capture_region(x1, y1, x2, y2, save_path)
    return True


def main() -> None:
    print("=" * 60)
    print("TRIA BRIDGE — UI Template Capture Guide")
    print("=" * 60)
    print()
    print("BEFORE YOU START:")
    print("  1. Open Tria in your browser and LOG IN")
    print("  2. Navigate to the BTC/USD trading page")
    print("  3. Set browser zoom to 100% (Cmd+0 or Ctrl+0)")
    print("  4. Use the theme (light/dark) you will trade with")
    print("  5. Do NOT hover over buttons when capturing — rest state only")
    print()
    wait_for_key("Press Enter when ready to begin... ")
    print()

    captured = 0
    for template in TEMPLATES:
        try:
            if capture_template(template):
                captured += 1
        except KeyboardInterrupt:
            print("\nCapture interrupted.")
            break

    print("=" * 60)
    print(f"CAPTURE COMPLETE: {captured}/{len(TEMPLATES)} templates saved")
    print(f"Location: {ASSET_DIR}")
    print("=" * 60)

    # List what we have
    files = os.listdir(ASSET_DIR)
    pngs = [f for f in files if f.endswith(".png")]
    if pngs:
        print("\nCaptured templates:")
        for f in sorted(pngs):
            print(f"  - {f}")

    missing = [t["name"] + ".png" for t in TEMPLATES if t["name"] + ".png" not in pngs]
    if missing:
        print("\nMissing templates (re-run this script to capture):")
        for m in missing:
            print(f"  - {m}")
    else:
        print("\nAll templates captured! You can now run:")
        print("  python -m tria_bridge.orchestrator")


if __name__ == "__main__":
    main()
