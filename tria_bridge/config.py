"""
tria_bridge/config.py — All tunables. No magic numbers elsewhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ── Paths ────────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGNAL_FILE = os.getenv("ARIA_SIGNAL_FILE", os.path.join(PROJECT_ROOT, "signals", "aria_outbox.json"))
SIGNAL_DIR = os.path.dirname(SIGNAL_FILE)
ASSET_DIR = os.getenv("TRIA_ASSET_DIR", os.path.join(os.path.dirname(__file__), "assets"))
LOG_DIR = os.getenv("TRIA_LOG_DIR", os.path.join(PROJECT_ROOT, "logs", "tria_bridge"))

# ── Vision ───────────────────────────────────────────────────────────────────
DEFAULT_CONFIDENCE = 0.80          # OpenCV template match threshold
CONFIDENCE_HIGH = 0.90             # Skip retry if this confident
CONFIDENCE_LOW = 0.60              # Fail if below this after retries
SCREENSHOT_TIMEOUT_MS = 50         # MSS capture target
MATCH_TIMEOUT_S = 10.0             # Max wait for element to appear
MATCH_RETRY_INTERVAL_S = 0.3       # Sleep between retries
MATCH_MAX_RETRIES = int(MATCH_TIMEOUT_S / MATCH_RETRY_INTERVAL_S)

# Browser window region (None = full screen)
# Override with env vars if running on a fixed-resolution display
BROWSER_REGION: Optional[Tuple[int, int, int, int]] = None
if os.getenv("TRIA_BROWSER_LEFT"):
    BROWSER_REGION = (
        int(os.getenv("TRIA_BROWSER_LEFT", "0")),
        int(os.getenv("TRIA_BROWSER_TOP", "0")),
        int(os.getenv("TRIA_BROWSER_WIDTH", "1920")),
        int(os.getenv("TRIA_BROWSER_HEIGHT", "1080")),
    )

# ── Execution timing ─────────────────────────────────────────────────────────
CLICK_DELAY_S = 0.05               # Pause after click
TYPE_DELAY_S = 0.01                # Pause between keystrokes
POST_ACTION_DELAY_S = 0.5          # Wait after major action (dropdown open etc)
VERIFY_FILL_TIMEOUT_S = 30.0       # Max wait for "Order Filled" confirmation
VERIFY_FILL_INTERVAL_S = 0.5       # Poll interval for fill confirmation

# ── Safety ───────────────────────────────────────────────────────────────────
MAX_TRADES_PER_DAY = int(os.getenv("TRIA_MAX_TRADES_DAY", "20"))
MAX_NOTIONAL_PER_DAY = float(os.getenv("TRIA_MAX_NOTIONAL_DAY", "5000.0"))
COOLDOWN_BETWEEN_TRADES_S = float(os.getenv("TRIA_COOLDOWN_S", "5.0"))
CONFIRMATION_REQUIRED = os.getenv("TRIA_CONFIRMATION", "true").lower() == "true"

# Kill switch — writing "STOP" to this file halts the bridge immediately
KILL_SWITCH_FILE = os.getenv("TRIA_KILL_SWITCH", os.path.join(PROJECT_ROOT, ".tria_kill_switch"))

# ── Symbol mapping ───────────────────────────────────────────────────────────
# ARIA symbol → Tria search text (often identical, but some DEXes use variants)
SYMBOL_MAP: Dict[str, str] = {
    "BTC-USD": "BTC",
    "ETH-USD": "ETH",
    "SOL-USD": "SOL",
    "ARB-USD": "ARB",
    "OP-USD": "OP",
    "LINK-USD": "LINK",
    "AVAX-USD": "AVAX",
    "MATIC-USD": "MATIC",
    "DOGE-USD": "DOGE",
    "XAUT-USD": "XAUT",
    "PENDLE-USD": "PENDLE",
    "TIA-USD": "TIA",
    "SEI-USD": "SEI",
    "SUI-USD": "SUI",
    "ENA-USD": "ENA",
    "WIF-USD": "WIF",
    "PEPE-USD": "PEPE",
    "BONK-USD": "BONK",
    "FARTCOIN-USD": "FARTCOIN",
}

# ── UI Element filenames (must exist in assets/ dir) ─────────────────────────
TEMPLATE_SYMBOL_SEARCH = "symbol_search.png"
TEMPLATE_SYMBOL_SELECT = "symbol_select.png"
TEMPLATE_BUY_BUTTON = "buy_button.png"
TEMPLATE_SELL_BUTTON = "sell_button.png"
TEMPLATE_SIZE_FIELD = "size_field.png"
TEMPLATE_LEVERAGE_DROPDOWN = "leverage_dropdown.png"
TEMPLATE_CONFIRM_LEVERAGE = "confirm_leverage.png"
TEMPLATE_CONFIRM_ORDER = "confirm_order.png"
TEMPLATE_FILLED_CONFIRMED = "filled_confirmed.png"
TEMPLATE_CLOSE_POSITION = "close_position.png"

# ── State machine ────────────────────────────────────────────────────────────
STATE_TIMEOUTS = {
    "SEARCH_SYMBOL": 15.0,
    "SELECT_SYMBOL": 10.0,
    "SET_DIRECTION": 8.0,
    "SET_SIZE": 8.0,
    "SET_LEVERAGE": 8.0,
    "CONFIRM_ORDER": 15.0,
    "VERIFY_FILL": VERIFY_FILL_TIMEOUT_S,
}


@dataclass
class BridgeConfig:
    """Runtime config — override via env vars for different machines."""
    signal_file: str = SIGNAL_FILE
    asset_dir: str = ASSET_DIR
    log_dir: str = LOG_DIR
    confidence: float = DEFAULT_CONFIDENCE
    browser_region: Optional[Tuple[int, int, int, int]] = BROWSER_REGION
    confirmation_required: bool = CONFIRMATION_REQUIRED
    max_trades_day: int = MAX_TRADES_PER_DAY
    max_notional_day: float = MAX_NOTIONAL_PER_DAY
    cooldown_s: float = COOLDOWN_BETWEEN_TRADES_S
    kill_switch_file: str = KILL_SWITCH_FILE
    symbol_map: Dict[str, str] = field(default_factory=lambda: SYMBOL_MAP.copy())

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        return cls()
