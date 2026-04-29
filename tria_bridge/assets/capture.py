#!/usr/bin/env python3
"""
Quick template capture script for Tria Bridge.

Usage:
    1. Position your mouse at the TOP-LEFT of the element you want to capture
    2. Run: python capture.py --name buy_button
    3. You have 3 seconds to move the mouse to the BOTTOM-RIGHT of the element
    4. The cropped template is saved to assets/<name>.png

Or capture the full screen and crop later:
    python capture.py --fullscreen --name raw_screenshot
"""

import argparse
import os
import sys
import time

import cv2
import numpy as np

try:
    import mss
except ImportError:
    print("pip install mss opencv-python numpy")
    sys.exit(1)


def get_mouse_pos():
    try:
        from AppKit import NSEvent, NSScreen
        import Quartz
        pos = NSEvent.mouseLocation()
        scale = NSScreen.mainScreen().backingScaleFactor()
        # NSEvent returns points with origin at bottom-left; convert to top-left pixels for mss
        height = Quartz.CGDisplayPixelsHigh(Quartz.CGMainDisplayID())
        x = int(pos.x * scale)
        y = int(pos.y * scale)
        return x, int(height - y)
    except ImportError:
        try:
            import pyautogui
            return pyautogui.position()
        except ImportError:
            print("pip install pyautogui  (or manually enter coordinates)")
            sys.exit(1)


def capture_region(left, top, width, height, save_path):
    with mss.MSS() as sct:
        region = {"left": left, "top": top, "width": width, "height": height}
        img = np.array(sct.grab(region))
        # Convert BGRA → BGR (drop alpha)
        bgr = img[:, :, :3]
        cv2.imwrite(save_path, bgr)
        print(f"Saved {width}x{height} template to {save_path}")


def main():
    parser = argparse.ArgumentParser(description="Capture UI templates for Tria Bridge")
    parser.add_argument("--name", required=True, help="Output filename (no extension)")
    parser.add_argument("--fullscreen", action="store_true", help="Capture entire screen")
    parser.add_argument("--delay", type=int, default=3, help="Seconds to position mouse")
    parser.add_argument("--left", type=int, help="Manual left coordinate")
    parser.add_argument("--top", type=int, help="Manual top coordinate")
    parser.add_argument("--width", type=int, help="Manual width")
    parser.add_argument("--height", type=int, help="Manual height")
    args = parser.parse_args()

    asset_dir = os.path.dirname(os.path.abspath(__file__))
    save_path = os.path.join(asset_dir, f"{args.name}.png")

    if args.fullscreen:
        with mss.MSS() as sct:
            mon = sct.monitors[1]
            capture_region(mon["left"], mon["top"], mon["width"], mon["height"], save_path)
        return

    if args.left is not None and args.top is not None and args.width is not None and args.height is not None:
        capture_region(args.left, args.top, args.width, args.height, save_path)
        return

    print(f"Move mouse to TOP-LEFT of '{args.name}' in {args.delay}s...")
    time.sleep(args.delay)
    x1, y1 = get_mouse_pos()
    print(f"Top-left: ({x1}, {y1})")

    print(f"Now move mouse to BOTTOM-RIGHT of '{args.name}' in {args.delay}s...")
    time.sleep(args.delay)
    x2, y2 = get_mouse_pos()
    print(f"Bottom-right: ({x2}, {y2})")

    left = min(x1, x2)
    top = min(y1, y2)
    width = abs(x2 - x1)
    height = abs(y2 - y1)

    capture_region(left, top, width, height, save_path)


if __name__ == "__main__":
    main()
