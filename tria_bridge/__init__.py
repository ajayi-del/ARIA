"""
tria_bridge — Deterministic GUI automation from ARIA signals to Tria execution.

Quant principles:
  • Deterministic state machine — no LLM in the hot path
  • Sub-100ms per action (MSS + OpenCV + pyautogui)
  • Safety-first: daily limits, kill switch, confirmation gate
  • Every click logged with timestamp, coordinate, confidence
"""

__version__ = "1.0.0"
