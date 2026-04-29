"""
tria_bridge/executor.py — Low-level mouse + keyboard primitives.

Quant principle: every action is atomic, logged, and recoverable.
"""

from __future__ import annotations

import time
from typing import Optional, Tuple

try:
    import pyautogui
except ImportError:
    pyautogui = None  # type: ignore

from tria_bridge.config import BridgeConfig
from tria_bridge.logger import BridgeLogger


class Executor:
    """Deterministic mouse/keyboard execution with retry logic."""

    def __init__(self, config: BridgeConfig, logger: BridgeLogger):
        self.cfg = config
        self.log = logger
        if pyautogui is None:
            raise RuntimeError("pyautogui not installed: pip install pyautogui")
        # Safety: fail-safe to top-left corner abort
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.0  # We handle delays manually for timing precision

    # ── Mouse ────────────────────────────────────────────────────────────────

    def move_to(self, x: int, y: int, duration: float = 0.1) -> None:
        t0 = time.perf_counter()
        pyautogui.moveTo(x, y, duration=duration)
        self.log.action(
            action="move_to",
            latency_ms=(time.perf_counter() - t0) * 1000,
            success=True,
            x=x,
            y=y,
        )

    def click(self, x: int, y: int, clicks: int = 1) -> None:
        t0 = time.perf_counter()
        self.move_to(x, y, duration=0.05)
        pyautogui.click(clicks=clicks)
        time.sleep(self.cfg.CLICK_DELAY_S)
        self.log.action(
            action="click",
            latency_ms=(time.perf_counter() - t0) * 1000,
            success=True,
            x=x,
            y=y,
            clicks=clicks,
        )

    def double_click(self, x: int, y: int) -> None:
        self.click(x, y, clicks=2)

    def triple_click(self, x: int, y: int) -> None:
        self.click(x, y, clicks=3)

    # ── Keyboard ─────────────────────────────────────────────────────────────

    def type_text(self, text: str, interval: Optional[float] = None) -> None:
        t0 = time.perf_counter()
        pyautogui.write(str(text), interval=interval or self.cfg.TYPE_DELAY_S)
        self.log.action(
            action="type_text",
            latency_ms=(time.perf_counter() - t0) * 1000,
            success=True,
            text_len=len(str(text)),
        )

    def hotkey(self, *keys: str) -> None:
        t0 = time.perf_counter()
        pyautogui.hotkey(*keys)
        self.log.action(
            action="hotkey",
            latency_ms=(time.perf_counter() - t0) * 1000,
            success=True,
            keys=list(keys),
        )

    def select_all_and_type(self, x: int, y: int, text: str) -> None:
        """Triple-click field, then type (replaces existing content)."""
        self.triple_click(x, y)
        time.sleep(0.05)
        self.type_text(text)

    # ── Browser helpers ──────────────────────────────────────────────────────

    def bring_browser_to_front(self) -> None:
        """Alt-Tab to browser window (assumes it's the last active window)."""
        self.hotkey("alt", "tab")
        time.sleep(0.2)

    # ── Coordinated actions ──────────────────────────────────────────────────

    def click_template(
        self,
        vision,
        filename: str,
        timeout_s: float = 10.0,
        confidence: Optional[float] = None,
    ) -> bool:
        """Wait for template, then click center. Returns success."""
        t0 = time.perf_counter()
        loc = vision.wait_for_template(filename, timeout_s=timeout_s, confidence=confidence)
        if loc is None:
            self.log.error("click_template_timeout", template=filename, timeout_s=timeout_s)
            return False
        cx, cy, conf = loc
        self.click(cx, cy)
        self.log.action(
            action="click_template",
            latency_ms=(time.perf_counter() - t0) * 1000,
            success=True,
            x=cx,
            y=cy,
            confidence=round(conf, 3),
            template=filename,
        )
        return True

    def fill_field(
        self,
        vision,
        field_template: str,
        value: str,
        timeout_s: float = 8.0,
    ) -> bool:
        """Click field, select all, type value."""
        t0 = time.perf_counter()
        loc = vision.wait_for_template(field_template, timeout_s=timeout_s)
        if loc is None:
            self.log.error("fill_field_timeout", template=field_template, value=value)
            return False
        cx, cy, _ = loc
        self.select_all_and_type(cx, cy, value)
        self.log.action(
            action="fill_field",
            latency_ms=(time.perf_counter() - t0) * 1000,
            success=True,
            x=cx,
            y=cy,
            value=value,
            template=field_template,
        )
        return True
