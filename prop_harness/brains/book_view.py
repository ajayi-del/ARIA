"""Cross-sleeve book view — conflict TELEMETRY only (prop harness).

Shared program/book_view.json plane (same pattern as loss_veto.json):
each sleeve atomically writes its open (asset, side) exposure under its
own sleeve key every reconcile tick; all sleeves read all keys. On
detecting another sleeve holding the OPPOSITE side of the same asset
(xyz: prefix normalized) the harness logs company_conflict_open once per
pair per 300s. This plane NEVER blocks an entry — it is the evidence
register for the Governor's formation correlation cap.

Kill switch PROP_BOOK_VIEW=0 → no writes, no reads (legacy bit-for-bit).
"""

from __future__ import annotations

import time
from typing import Any

STALE_S = 120.0          # a silent sleeve's rows stop conflicting after 2 min
THROTTLE_S = 300.0       # one company_conflict_open per pair per window

EVENT = "company_conflict_open"
SCHEMA = 1

# WHY: registry rows carry buy/sell, external views may carry long/short —
# both spellings are the same exposure for conflict reads.
_OPPOSITE = {"buy": "sell", "sell": "buy", "long": "short", "short": "long"}


def norm_asset(asset: str | None) -> str:
    """WHY: xyz:GOLD and GOLD are the same exposure for conflict reads."""
    a = (asset or "").strip()
    return a.split(":", 1)[1] if a.startswith("xyz:") else a


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def build_sleeve_entry(rows: list[dict] | None,
                       now: float | None = None) -> dict[str, Any]:
    """Open (asset, side) exposure from this sleeve's registry rows."""
    ts = float(now if now is not None else time.time())
    exposure: list[dict[str, str]] = []
    for row in (rows or []):
        if not isinstance(row, dict) or row.get("status") != "open":
            continue
        asset = norm_asset(row.get("asset") or row.get("symbol"))
        side = str(row.get("side") or "").lower()
        if not asset or side not in _OPPOSITE:
            continue
        exposure.append({"asset": asset, "side": side})
    return {"ts": ts, "exposure": exposure}


def merge_sleeve(view: dict[str, Any] | None,
                 sleeve: str,
                 entry: dict[str, Any]) -> dict[str, Any]:
    out = dict(view or {})
    sleeves = dict(out.get("sleeves") or {})
    sleeves[str(sleeve)] = entry
    out["sleeves"] = sleeves
    out["schema"] = SCHEMA
    return out


def conflicts(view: dict[str, Any] | None,
              own_sleeve: str,
              now: float | None = None) -> list[dict[str, Any]]:
    """Opposite-side pairs between own sleeve and every other live sleeve."""
    ts = float(now if now is not None else time.time())
    if not isinstance(view, dict):
        return []
    sleeves = view.get("sleeves")
    if not isinstance(sleeves, dict):
        return []
    own = sleeves.get(str(own_sleeve))
    if not isinstance(own, dict):
        return []
    own_exposure = own.get("exposure")
    if not isinstance(own_exposure, list) or not own_exposure:
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for other_sleeve, other in sleeves.items():
        if str(other_sleeve) == str(own_sleeve):
            continue
        if not isinstance(other, dict):
            continue
        other_ts = _f(other.get("ts"))
        if other_ts is None or (ts - other_ts) > STALE_S:
            continue  # a silent sleeve stops conflicting (fail-open)
        other_exposure = other.get("exposure")
        if not isinstance(other_exposure, list):
            continue
        for mine in own_exposure:
            for theirs in other_exposure:
                if (mine.get("asset") == theirs.get("asset")
                        and mine.get("side") in _OPPOSITE
                        and theirs.get("side") in _OPPOSITE
                        and _OPPOSITE[mine["side"]] == theirs["side"]):
                    key = (str(other_sleeve), mine["asset"], mine["side"])
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({
                        "asset": mine["asset"],
                        "own_sleeve": str(own_sleeve),
                        "own_side": mine["side"],
                        "other_sleeve": str(other_sleeve),
                        "other_side": theirs["side"],
                        "other_ts": other_ts,
                    })
    return out


def throttle_allows(last_logged: dict[str, float] | None,
                    key: str,
                    now: float | None = None) -> bool:
    """One log per key per THROTTLE_S window."""
    ts = float(now if now is not None else time.time())
    if not isinstance(last_logged, dict):
        return True
    last = _f(last_logged.get(key))
    if last is None:
        return True
    return (ts - last) >= THROTTLE_S


def note_throttle(last_logged: dict[str, float],
                  key: str,
                  now: float | None = None) -> dict[str, float]:
    last_logged[str(key)] = float(now if now is not None else time.time())
    return last_logged
