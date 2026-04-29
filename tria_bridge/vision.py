"""
tria_bridge/vision.py — Fast screenshot + OpenCV template matching.

Quant targets:
  Screenshot capture: <20 ms (MSS)
  Template match:     <10 ms (OpenCV grayscale)
  Total per action:   <50 ms
"""

from __future__ import annotations

import os
import time
from typing import Optional, Tuple

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None  # type: ignore
    np = None  # type: ignore

try:
    import mss
except ImportError:
    mss = None  # type: ignore

from tria_bridge.config import BridgeConfig
from tria_bridge.logger import BridgeLogger


class VisionEngine:
    """Screenshot + template matching using MSS + OpenCV."""

    def __init__(self, config: BridgeConfig, logger: BridgeLogger):
        self.cfg = config
        self.log = logger
        self._mss = mss.mss() if mss is not None else None
        self._template_cache: dict = {}

    def screenshot(self) -> "np.ndarray":
        """Capture screen (or browser region) as BGR numpy array."""
        if self._mss is None:
            raise RuntimeError("mss not installed: pip install mss")
        region = self.cfg.browser_region
        if region is None:
            mon = self._mss.monitors[1]  # primary monitor
            region = (mon["left"], mon["top"], mon["width"], mon["height"])
        left, top, width, height = region
        sct_img = self._mss.grab({"left": left, "top": top, "width": width, "height": height})
        if np is None:
            raise RuntimeError("numpy not installed: pip install numpy")
        return np.array(sct_img)[:, :, :3]  # BGRA→BGR (drop alpha)

    def _load_template(self, filename: str) -> "np.ndarray":
        """Cache grayscale template images."""
        if filename in self._template_cache:
            return self._template_cache[filename]
        path = os.path.join(self.cfg.asset_dir, filename)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Template missing: {path}")
        if cv2 is None:
            raise RuntimeError("opencv-python not installed: pip install opencv-python")
        tpl = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if tpl is None:
            raise ValueError(f"Could not load template: {path}")
        self._template_cache[filename] = tpl
        return tpl

    def find_template(
        self,
        filename: str,
        confidence: Optional[float] = None,
    ) -> Optional[Tuple[int, int, float]]:
        """
        Locate template on screen. Returns (center_x, center_y, match_confidence) or None.
        Coordinates are relative to the browser_region origin (or screen origin if None).
        """
        conf = confidence or self.cfg.confidence
        t0 = time.perf_counter()
        try:
            haystack_bgr = self.screenshot()
        except Exception as exc:
            self.log.error("screenshot_failed", error=str(exc))
            return None

        haystack = cv2.cvtColor(haystack_bgr, cv2.COLOR_BGR2GRAY)
        template = self._load_template(filename)
        result = cv2.matchTemplate(haystack, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        latency_ms = (time.perf_counter() - t0) * 1000
        if max_val >= conf:
            h, w = template.shape[:2]
            cx = max_loc[0] + w // 2
            cy = max_loc[1] + h // 2
            # Adjust for browser region offset
            if self.cfg.browser_region is not None:
                cx += self.cfg.browser_region[0]
                cy += self.cfg.browser_region[1]
            self.log.action(
                action="template_match",
                latency_ms=latency_ms,
                success=True,
                x=cx,
                y=cy,
                confidence=round(float(max_val), 3),
                template=filename,
            )
            return (cx, cy, float(max_val))

        self.log.action(
            action="template_match",
            latency_ms=latency_ms,
            success=False,
            confidence=round(float(max_val), 3),
            template=filename,
        )
        return None

    def wait_for_template(
        self,
        filename: str,
        timeout_s: float = 10.0,
        interval_s: float = 0.3,
        confidence: Optional[float] = None,
    ) -> Optional[Tuple[int, int, float]]:
        """Poll until template appears or timeout."""
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            loc = self.find_template(filename, confidence=confidence)
            if loc is not None:
                return loc
            time.sleep(interval_s)
        return None
