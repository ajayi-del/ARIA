"""intelligence/router.py — Strategy-class router (pure brain, zero I/O).

2026-09-28 (Governor doctrine, verbatim): "Router assigns class → class has
ONE exit doctrine. All other exit modules are UNSUBSCRIBED for that class.
One symbol = one class at a time. Assignment made ONCE at session start;
cannot change class mid-session; next-session reassessment allowed."

The wound this closes (2026-09-28 Aster churn audit): three exit modules
fought the same positions with no ranking authority — roe_ratchet armed a
breakeven stop +0.3% ROE ~1s after entry (304 arms/19h, noise stop-outs),
conviction_decay abandoned positions on a clock (ENA −3.23, ZEC −2.35),
and rally graduation re-entered ×2.0 size at rising entries (VIRTUAL 4th
long → portfolio_loss_cut −$2.28). Zero post-fix closes ever reached
software TP. The router is the S2 coordinator (VSM table): it assigns at
most ONE class per symbol per session and publishes it; the three exit
modules read the class and stand down.

R1 MINIMAL scope: assignment + publication + the reader only. NO executors,
NO order logic, NO new entries. band_tight_pct / regime are accepted
evidence channels for R2 — the R1 rule matrix does not consume them.

Classes:
  VOLUME_MAKER      — SoDEX campaign anticipator fleet symbols (the volume
                      engine). Exempt from roe_ratchet / conviction_decay /
                      graduation boost.
  STRUCTURAL_HOLD   — narrative longs with whale/trend evidence. Same
                      exemptions.
  STRUCTURAL_SHORT  — narrative shorts with whale/trend evidence. Same
                      exemptions.
  None              — unassigned: legacy behavior everywhere (fail-open).

Department template (docs/DEPARTMENT_TEMPLATE.md): zero I/O — no network,
no files, no logging; all external state injected. main.py owns the three
exemption splices, the boot assignment block, and the refresh loop.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

CLASS_VOLUME_MAKER = "VOLUME_MAKER"
CLASS_STRUCTURAL_HOLD = "STRUCTURAL_HOLD"
CLASS_STRUCTURAL_SHORT = "STRUCTURAL_SHORT"

#: Classes that unsubscribe the legacy exit modules for a symbol.
EXEMPT_CLASSES = frozenset({
    CLASS_VOLUME_MAKER,
    CLASS_STRUCTURAL_HOLD,
    CLASS_STRUCTURAL_SHORT,
})

_ALL_CLASSES = EXEMPT_CLASSES  # R1: every assignable class is exempt-class

#: Whale-evidence thresholds (Governor-locked R1 rule matrix).
WHALE_HOLD_MIN = 1.5    # long-share ratio at/above → structural long
WHALE_SHORT_MAX = 1.1   # below → structural short

PARAM_KEY_PREFIX = "router:class:"


def param_key(symbol: str) -> str:
    """The param_store key carrying a symbol's published class."""
    return f"{PARAM_KEY_PREFIX}{symbol}"


def assign_class(symbol: str, *, side=None, whale_ratio=None,
                 band_tight_pct=None, regime=None,
                 is_campaign_fleet_sym: bool = False,
                 now_ts: float = 0.0) -> Optional[str]:
    """Pure rule matrix — first match wins, else None (abstain = legacy).

    Priority order:
      1. Campaign-fleet membership → VOLUME_MAKER (the volume engine is its
         own doctrine; the legacy exit stack must never touch it).
      2. whale_ratio >= 1.5 with a long-compatible side → STRUCTURAL_HOLD.
      3. whale_ratio < 1.1 with a short-compatible side → STRUCTURAL_SHORT.

    side=None means "direction not yet known" — both side-gates accept it
    (the whale evidence itself carries the direction). band_tight_pct /
    regime / now_ts are R2 evidence channels — accepted, never read in R1.
    Fail-open: ANY malformed input abstains (None) rather than guessing."""
    try:
        if not symbol:
            return None
        if is_campaign_fleet_sym:
            return CLASS_VOLUME_MAKER
        if whale_ratio is None:
            return None
        _wr = float(whale_ratio)
        _side = str(side).lower() if side is not None else None
        if _wr >= WHALE_HOLD_MIN and _side in (None, "long"):
            return CLASS_STRUCTURAL_HOLD
        if _wr < WHALE_SHORT_MAX and _side in (None, "short"):
            return CLASS_STRUCTURAL_SHORT
        return None
    except (TypeError, ValueError):
        return None


class RouterAssignments:
    """Session-scoped {symbol: (cls, assigned_ts)} registry — sticky within
    the TTL: re-assignment inside the window returns the EXISTING class
    unless force=True (the Governor's "cannot change class mid-session"
    law). Memory-only by design: next-session reassessment is a fresh
    instance at the next boot; the param_store TTL keys are the cross-
    restart publication plane, not this map."""

    def __init__(self) -> None:
        self._assignments: Dict[str, Tuple[str, float]] = {}

    def assign(self, symbol: str, cls: str, *, now_ts: float,
               ttl_s: float, force: bool = False) -> str:
        """Record (or stickily return) the symbol's class. Returns the
        EFFECTIVE class — the existing one inside the TTL window."""
        _existing = self._assignments.get(symbol)
        if (_existing is not None and not force
                and (float(now_ts) - _existing[1]) < float(ttl_s)):
            return _existing[0]
        self._assignments[symbol] = (cls, float(now_ts))
        return cls

    def get(self, symbol: str) -> Optional[str]:
        _row = self._assignments.get(symbol)
        return _row[0] if _row is not None else None

    def assigned_ts(self, symbol: str) -> Optional[float]:
        _row = self._assignments.get(symbol)
        return _row[1] if _row is not None else None

    def items(self):
        """Snapshot list of (symbol, (cls, assigned_ts)) — for the refresh
        loop's re-publish pass over symbols no longer held."""
        return list(self._assignments.items())

    @staticmethod
    def publish(param_store, symbol: str, cls: str, ttl_s: float) -> bool:
        """Write router:class:{symbol} via the param_store set_ai_param
        idiom (TTL'd, disk-flushed, survives restarts until expiry).
        Fail-open False on any store error — the reader treats a dark key
        as unassigned (legacy behavior)."""
        try:
            param_store.set_ai_param(param_key(symbol), cls,
                                     ttl_seconds=max(1, int(ttl_s)))
            return True
        except Exception:
            return False


def router_class_for(param_store, symbol: str) -> Optional[str]:
    """The reader every exit-module splice calls. Fail-OPEN None on ANY
    error (broken store, None store, malformed value) — None means
    unassigned and the module acts exactly as before the router existed."""
    try:
        _v = param_store.get_ai_param(param_key(symbol), None)
    except Exception:
        return None
    return _v if isinstance(_v, str) and _v in _ALL_CLASSES else None
