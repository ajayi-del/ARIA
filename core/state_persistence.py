"""
State Persistence Helpers — ARIA v1.5

Atomic JSON read/write for cross-restart state recovery.

Design matches existing ARIA persistence patterns (DrawdownManager, FundingHistory):
  - Synchronous file I/O  (no aiofiles — keeps the pattern consistent)
  - Atomic write via temp file → os.replace()  (never leaves a partial file)
  - State lives in logs/  (consistent with all other ARIA state files)
  - max_age_s staleness guard  (stale state causes more harm than no state)

Usage:
    from core.state_persistence import atomic_save, atomic_load

    atomic_save(Path("logs/cascade_state.json"), {"phase": "blocked", ...})
    data = atomic_load(Path("logs/cascade_state.json"), max_age_s=300)
    if data:
        self._phase = data["phase"]
"""

import json
import os
import time
import structlog
from pathlib import Path
from typing import Any, Dict, Optional

log = structlog.get_logger(__name__)


def atomic_save(path: Path, data: Dict[str, Any]) -> None:
    """
    Write data as JSON to path, atomically.

    Wraps data in an envelope with a unix timestamp so atomic_load() can
    check freshness without the caller needing to manage it:
        {"saved_at": 1713200000.0, "data": {...}}

    Failures are logged but never raised — state loss is non-fatal.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump({"saved_at": time.time(), "data": data}, f)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("state_save_failed", path=str(path), error=str(e))
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def atomic_load(path: Path, max_age_s: float = 300.0) -> Optional[Dict[str, Any]]:
    """
    Load JSON state from path.

    Returns:
        dict  — the saved data dict if fresh and valid
        None  — if file missing, corrupt, or older than max_age_s

    max_age_s = 300 (5 min) is the right default for cascade/zscore state.
    Use a larger value (e.g. 86400) for slow-moving metrics like drawdown peaks.
    """
    if not path.exists():
        return None
    try:
        with open(path) as f:
            envelope = json.load(f)
        age_s = time.time() - envelope.get("saved_at", 0.0)
        if age_s > max_age_s:
            log.info("state_file_stale",
                     path=str(path),
                     age_s=round(age_s, 0),
                     max_age_s=max_age_s)
            return None
        return envelope.get("data")
    except Exception as e:
        log.warning("state_load_failed", path=str(path), error=str(e))
        return None
