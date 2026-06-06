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
import time
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

    AI-writable parameters (Phase 3):
        All AI params carry a TTL (expires_at timestamp).
        On expiry, the system falls back to config defaults automatically.
        Supported keys: leverage_override, stop_mult_override, atr_min_pct_override,
        blacklist, portfolio_tp_override, coherence_floor_override.
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

    # ── AI-writable parameters with TTL (Phase 3) ─────────────────────────────

    def set_ai_param(self, key: str, value, ttl_seconds: int = 3600) -> None:
        """
        Write an AI-managed parameter with TTL.
        On expiry, get_ai_param returns default (None).
        """
        if "ai_params" not in self._overrides:
            self._overrides["ai_params"] = {}
        expires_at = int(time.time()) + max(1, ttl_seconds)
        self._overrides["ai_params"][key] = {
            "value": value,
            "expires_at": expires_at,
        }
        log.info("param_store_ai_set", key=key, ttl=ttl_seconds, expires_at=expires_at)
        self._save()

    def get_ai_param(self, key: str, default=None):
        """
        Read an AI parameter if it exists and has not expired.
        Automatically purges expired entries on read.
        """
        ai_params = self._overrides.get("ai_params", {})
        entry = ai_params.get(key)
        if entry is None:
            return default
        now = int(time.time())
        if now >= entry.get("expires_at", 0):
            # Expired — clean up and return default
            ai_params.pop(key, None)
            log.info("param_store_ai_expired", key=key)
            self._save()
            return default
        return entry.get("value", default)

    def get_all_ai_params(self) -> dict:
        """Return all non-expired AI parameters as {key: value}."""
        self.expire_ai_params()
        return {
            k: v["value"]
            for k, v in self._overrides.get("ai_params", {}).items()
        }

    def expire_ai_params(self) -> None:
        """Purge all expired AI parameters. Idempotent."""
        ai_params = self._overrides.get("ai_params", {})
        if not ai_params:
            return
        now = int(time.time())
        expired = [k for k, v in ai_params.items() if now >= v.get("expires_at", 0)]
        if expired:
            for k in expired:
                ai_params.pop(k, None)
            log.info("param_store_ai_purged", count=len(expired), keys=expired)
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
                         coherence=self._overrides.get("min_coherence", "default"),
                         ai_params=len(self._overrides.get("ai_params", {})))
            except Exception as e:
                log.warning("param_store_load_error", error=str(e))
                self._overrides = {}

    def _save(self) -> None:
        try:
            STORE_PATH.write_text(json.dumps(self._overrides, indent=2))
        except Exception as e:
            log.warning("param_store_save_error", error=str(e))
