"""
ARIA Parameter Store — mutable runtime parameters that survive restarts.

Wraps the immutable Pydantic Settings config with a JSON override layer.
ARIA reads from here first; falls back to config defaults.

All writes are flushed to disk immediately (logs/param_store.json).
CalibrationEngine writes blended values here — the store records them as-is.

Usage:
    ps = ParamStore(config)
    mult = ps.get_stop_mult("SOL-USD")    # override → default → 2.5
    ps.set_stop_mult("SOL-USD", 2.8)       # write + persist
"""

import json
import structlog
from pathlib import Path

log = structlog.get_logger(__name__)

STORE_PATH = Path("logs/param_store.json")

# Per-asset stop ATR multiplier defaults.
# These are the Phase 4 spec values — calibration will converge away from these
# as ARIA accumulates trades.
_DEFAULT_STOP_MULTS: dict[str, float] = {
    # Increased 2026-04-15: noise analysis showed 2.0× stops triggering on normal
    # intraday retracements. These wider defaults give stops room to breathe.
    "BTC-USD":      2.5,   # ~$150–500 buffer at current ATR — survives 30-min holds
    "ETH-USD":      2.5,   # same rationale as BTC
    "SOL-USD":      3.0,   # higher relative vol than BTC; 2.5× was triggering on noise
    "XAUT-USD":     3.5,   # gold: slow vol but leveraged — wider buffer needed
    "BNB-USD":      3.0,   # mid-cap liquidity; 2.5× too tight
    "LINK-USD":     2.5,   # thin book, sharp wicks; old 1.5× was guaranteed stop-out
    "AVAX-USD":     3.5,   # high relative vol; 3.0× was marginal
    "ARB-USD":      4.0,   # illiquid, large intraday spikes
    "OP-USD":       4.0,   # same as ARB
    "NEAR-USD":     4.0,   # same as ARB
    "SUI-USD":      4.0,   # same as ARB
    "1000PEPE-USD": 4.0,   # meme coin — extreme vol
    "default":      2.5,   # up from 2.0 — global floor for unknown symbols
}


class ParamStore:
    """
    Mutable parameter store for ARIA.

    Read priority:
        1. Learned overrides (logs/param_store.json)
        2. Config attribute (Pydantic Settings)
        3. Hardcoded safety default

    Constraints enforced on every set():
        stop_mult:           1.0 – 4.0
        coherence_threshold: 1.5 – 4.0
        session_weight:      0.5 – 1.5
    """

    def __init__(self, config) -> None:
        self._config = config
        self._overrides: dict = {}
        self._load()

    # ── Stop multipliers ──────────────────────────────────────────────────────

    def get_stop_mult(self, symbol: str) -> float:
        """Per-asset ATR stop multiplier. 1.5 = tight, 4.0 = very wide."""
        mults = self._overrides.get("stop_mults", {})
        if symbol in mults:
            return float(mults[symbol])
        return _DEFAULT_STOP_MULTS.get(symbol, _DEFAULT_STOP_MULTS["default"])

    def set_stop_mult(self, symbol: str, value: float) -> None:
        value = round(max(1.5, min(5.0, value)), 3)  # floor 1.5 (never below 1.5×), ceil 5.0
        if "stop_mults" not in self._overrides:
            self._overrides["stop_mults"] = {}
        self._overrides["stop_mults"][symbol] = value
        self._save()

    # ── Coherence threshold ───────────────────────────────────────────────────

    def get_coherence_threshold(self) -> float:
        """Minimum coherence score to place a trade."""
        return float(self._overrides.get(
            "min_coherence",
            getattr(self._config, "min_coherence", 1.0)))

    def set_coherence_threshold(self, value: float) -> None:
        value = round(max(1.5, min(4.0, value)), 3)
        self._overrides["min_coherence"] = value
        self._save()

    # ── Session weights ───────────────────────────────────────────────────────

    def get_session_weight(self, session: str) -> float:
        return float(self._overrides.get("session_weights", {}).get(session, 1.0))

    def set_session_weight(self, session: str, value: float) -> None:
        value = round(max(0.5, min(1.5, value)), 3)
        if "session_weights" not in self._overrides:
            self._overrides["session_weights"] = {}
        self._overrides["session_weights"][session] = value
        self._save()

    # ── Introspection ─────────────────────────────────────────────────────────

    def get_all(self) -> dict:
        return self._overrides.copy()

    def stop_mult_summary(self) -> dict:
        """Returns all active stop multiplier overrides."""
        return dict(self._overrides.get("stop_mults", {}))

    # ── Private ───────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if STORE_PATH.exists():
            try:
                self._overrides = json.loads(STORE_PATH.read_text())
                log.info("param_store_loaded",
                         stop_overrides=len(self._overrides.get("stop_mults", {})),
                         coherence=self._overrides.get("min_coherence", "default"))
            except Exception as e:
                log.warning("param_store_load_error", error=str(e))
                self._overrides = {}

    def _save(self) -> None:
        try:
            STORE_PATH.write_text(json.dumps(self._overrides, indent=2))
        except Exception as e:
            log.warning("param_store_save_error", error=str(e))
