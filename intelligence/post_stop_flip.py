"""
intelligence/post_stop_flip.py — post-stop flip gate (fakeout repair)

The wound: a stop-out whose mark recovers through the stop within minutes is
a FAKEOUT — the market wicked through the level and came back. Entering the
OPPOSITE direction right after a fakeout is buying the wick's false signal:
the level held.

Doctrine: after a stop-class exit (software_stop / native_stop), if the mark
recovers through the stop within RECOVERY_WINDOW_MS (15 min), block
OPPOSITE-direction entries for BLOCK_WINDOW_MS (60 min). Same-direction
re-entry is NOT blocked — a recovered stop on a long does not indict a fresh
long; it indicts the short the wick pretended to confirm.

Approximation (documented, accepted): tick history is not retained, so
"recovered within 15 min" is attested only at entry-evaluation moments —
the first opposite-direction evaluation that observes the mark through the
stop inside the recovery window ARMS the block (recovered_ms stamped on the
record); the block then persists until the block window expires even if the
mark un-recovers. A stop whose first opposite-direction evaluation arrives
after the recovery window can never arm — under-blocking is the chosen
direction (fail-open for entries; never blocks a legitimate trade on stale
inference). Missing/invalid mark or stop abstains (no block).

Registry: in-memory {(symbol, stopped_side): {"stop", "stopped_ms",
"recovered_ms"}}, written from _record_close on stop-class exits, TTL
REGISTRY_TTL_MS (75 min) pruned on write. Restart-amnesia is accepted:
the block window is 60 min, so a restart loses at most one hour of fakeout
memory — the same tradeoff as _direction_loss_cooldown.

Kill switch: POST_STOP_FLIP_GATE_ENABLED (default true). False = no
enforcement; the verdict is still computed at the splice sites and the
would-block is shadow-scored (gate "post_stop_flip") in BOTH modes
(tp_invariant idiom).
"""
from __future__ import annotations

import os
from typing import Optional

RECOVERY_WINDOW_MS = 900_000      # 15 min — recovery must be attested inside this
BLOCK_WINDOW_MS = 3_600_000       # 60 min — armed block lifetime
REGISTRY_TTL_MS = 4_500_000       # 75 min — prune horizon (write-side)
STOP_EXIT_REASONS = frozenset({"software_stop", "native_stop"})

_OPPOSITE = {"long": "short", "short": "long"}


def post_stop_flip_gate_enabled() -> bool:
    return os.environ.get("POST_STOP_FLIP_GATE_ENABLED", "true").strip().lower() != "false"


def record_stop(registry: dict, symbol: str, side: str,
                stop_price: float, now_ms: int) -> None:
    """Register a stop-class exit. Invalid inputs are dropped silently
    (fail-open: a bad record must never manufacture a future block)."""
    try:
        sym = str(symbol)
        sd = str(side).lower()
        sp = float(stop_price)
        now = int(now_ms)
        if not sym or sd not in _OPPOSITE or sp <= 0:
            return
        registry[(sym, sd)] = {"stop": sp, "stopped_ms": now,
                               "recovered_ms": None}
        for k in [k for k, v in registry.items()
                  if now - int(v.get("stopped_ms", 0)) > REGISTRY_TTL_MS]:
            registry.pop(k, None)
    except Exception:
        return


def flip_verdict(registry: dict, symbol: str, entry_direction: str,
                 mark: float, now_ms: int) -> Optional[dict]:
    """Verdict for an entry of entry_direction on symbol.

    Returns a verdict dict when the post-stop flip gate blocks, else None.
    Arms (stamps recovered_ms) on first attested recovery inside the window.
    Never raises; garbage in → None (no block).
    """
    try:
        opp = _OPPOSITE.get(str(entry_direction).lower())
        if opp is None:
            return None
        rec = registry.get((str(symbol), opp))
        if rec is None:
            return None
        now = int(now_ms)
        stopped_ms = int(rec.get("stopped_ms", 0))
        age = now - stopped_ms
        if age < 0 or age > BLOCK_WINDOW_MS:
            return None
        if rec.get("recovered_ms") is not None:
            return {"reason": "post_stop_flip_armed", "stopped_side": opp,
                    "age_ms": age, "recovered_ms": int(rec["recovered_ms"])}
        if age > RECOVERY_WINDOW_MS:
            return None   # recovery window lapsed unattested — never arms
        stop = float(rec.get("stop", 0.0) or 0.0)
        mk = float(mark or 0.0)
        if stop <= 0 or mk <= 0:
            return None   # abstain on missing data
        recovered = (opp == "long" and mk >= stop) or (opp == "short" and mk <= stop)
        if not recovered:
            return None
        rec["recovered_ms"] = now
        return {"reason": "post_stop_flip_armed", "stopped_side": opp,
                "age_ms": age, "recovered_ms": now}
    except Exception:
        return None
