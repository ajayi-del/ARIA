"""KingdomView Phase 1 — shared kingdom memory (harness-wiring Law 2).

Merges ArmyState snapshots + heartbeats + loss_veto + thesis occupancy
slots into one JSON file armies can read. Writer is fail-open (partial
view marked ``incomplete:true``). Overflow consumers still fail-closed
on stale / incomplete covering peers (standby — no double-fire).

Kill: ``PROP_KINGDOM_VIEW=0`` → peer-file envy bit-for-bit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA = 1
DEFAULT_VIEW_PATH = "~/kingdom/prop_kingdom_view.json"
POSITIONS_NAME = "prop_positions.json"
HEARTBEAT_NAME = "heartbeat.json"


def thesis_id(asset: str, side: str, day: str | None = None) -> str:
    """Stable occupancy key — Phase 1 held-from-positions; reserve API later."""
    if day is None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{asset}:{side}:{day}"


def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def state_root_from_peer_path(path: Path) -> Path:
    """``…/prop_positions.json`` → parent; else treat path as state root."""
    p = path.expanduser()
    if p.name == POSITIONS_NAME:
        return p.parent
    return p


def army_id_of(snap: dict | None, fallback: str = "") -> str:
    if not snap:
        return fallback
    return str(snap.get("account_id") or fallback or "")


def derive_thesis_slots(
    armies: dict[str, dict | None],
    *,
    now: float,
    day: str | None = None,
) -> dict[str, dict]:
    """Occupancy map from open ArmyState positions (held, not reserved)."""
    slots: dict[str, dict] = {}
    for aid, snap in armies.items():
        if not snap:
            continue
        for pos in snap.get("positions") or []:
            if not isinstance(pos, dict):
                continue
            asset = pos.get("asset")
            side = pos.get("side")
            if not asset or not side:
                continue
            tid = thesis_id(str(asset), str(side), day=day)
            # First holder wins; later armies do not overwrite (no double-claim).
            if tid in slots:
                continue
            slots[tid] = {
                "army_id": aid,
                "asset": asset,
                "side": side,
                "status": "held",
                "ts": _f(pos.get("opened_ts")) or _f(snap.get("ts")) or now,
            }
    return slots


def derive_slot_economy(armies: dict[str, dict | None]) -> dict:
    by_army: dict[str, dict] = {}
    for aid, snap in armies.items():
        if not snap:
            continue
        open_n = len(snap.get("positions") or [])
        max_n = int(snap.get("max_positions") or 0)
        by_army[aid] = {
            "open": open_n,
            "max": max_n,
            "free": max(0, max_n - open_n),
        }
    return {"by_army": by_army}


def fold_army_row(
    snap: dict | None,
    heartbeat: dict | None = None,
) -> dict | None:
    """ArmyState row: geometry snap + optional heartbeat budget/dd fold."""
    if not snap or not isinstance(snap, dict):
        return None
    row = dict(snap)
    if heartbeat and isinstance(heartbeat, dict):
        row["heartbeat_ts"] = heartbeat.get("ts")
        if "budget" in heartbeat:
            row["budget"] = heartbeat.get("budget")
        if "dd" in heartbeat:
            row["dd"] = heartbeat.get("dd")
        if "enabled" in heartbeat:
            row["enabled"] = heartbeat.get("enabled")
    return row


def build_kingdom_view(
    armies: dict[str, dict | None],
    *,
    now: float,
    loss_veto: dict | None = None,
    thesis_slots: dict | None = None,
    expected_ids: list[str] | None = None,
    day: str | None = None,
) -> dict:
    """Pure aggregate. Missing expected armies → ``incomplete:true``."""
    present = {k: v for k, v in armies.items() if v}
    expected = list(expected_ids) if expected_ids is not None else list(armies.keys())
    incomplete = any(armies.get(eid) is None for eid in expected) or not present
    slots = (thesis_slots if thesis_slots is not None
             else derive_thesis_slots(present, now=now, day=day))
    return {
        "schema": SCHEMA,
        "ts": now,
        "incomplete": bool(incomplete),
        "armies": present,
        "loss_veto": loss_veto,
        "thesis_slots": slots,
        "slot_economy": derive_slot_economy(present),
    }


def load_army_bundle(
    peer_path: Path,
    *,
    read_json: Callable[[Path], Any],
) -> tuple[str, dict | None, dict | None]:
    """Load ``(army_id, positions_snap, heartbeat)`` from a peer path.

    ``peer_path`` may be a state root or a ``prop_positions.json`` file.
    Fail-open: missing files → ``(fallback_id, None, None)``.
    """
    root = state_root_from_peer_path(peer_path)
    snap = read_json(root / POSITIONS_NAME)
    hb = read_json(root / HEARTBEAT_NAME)
    if not isinstance(snap, dict):
        snap = None
    if not isinstance(hb, dict):
        hb = None
    aid = army_id_of(snap) or army_id_of(hb) or root.name
    return aid, snap, hb


def collect_armies(
    peer_paths: list[Path],
    *,
    read_json: Callable[[Path], Any],
) -> tuple[dict[str, dict | None], list[str]]:
    """Merge peer bundles into ``{army_id → folded row|None}`` + expected ids."""
    armies: dict[str, dict | None] = {}
    expected: list[str] = []
    for raw in peer_paths:
        aid, snap, hb = load_army_bundle(raw, read_json=read_json)
        expected.append(aid)
        armies[aid] = fold_army_row(snap, hb)
    return armies, expected


def publish_kingdom_view(
    path: Path,
    view: dict,
    *,
    write_json: Callable[[Path, Any], None],
) -> bool:
    """Fail-open publish. Returns False on any write fault — never raises."""
    try:
        write_json(path.expanduser(), view)
        return True
    except Exception:
        return False


def read_kingdom_view(
    path: Path,
    *,
    read_json: Callable[[Path], Any],
) -> dict | None:
    raw = read_json(path.expanduser())
    if not isinstance(raw, dict):
        return None
    if raw.get("schema") != SCHEMA:
        return None
    return raw


def peer_snaps_from_view(
    view: dict | None,
    *,
    own_account_id: str = "",
) -> list[dict] | None:
    """Extract ArmyState snaps for ``overflow_verdict``.

    Returns ``None`` when the view is unusable (caller may fall back to
    legacy peer files only if the view file is *missing* — not when stale).
    """
    if not view or view.get("schema") != SCHEMA:
        return None
    armies = view.get("armies")
    if not isinstance(armies, dict):
        return None
    out: list[dict] = []
    for snap in armies.values():
        if not isinstance(snap, dict):
            continue
        if own_account_id and snap.get("account_id") == own_account_id:
            continue
        out.append(snap)
    return out


def resolve_overflow_snaps(
    *,
    view_enabled: bool,
    view: dict | None,
    legacy_snaps: list[dict | None],
    now: float,
    stale_s: float,
    own_account_id: str = "",
) -> list[dict | None]:
    """View **or** legacy. Stale/unusable view with knob ON → empty (standby).

    Missing view file (``view is None``) while enabled → legacy envy path so
    a never-written kingdom does not freeze overflow before the writer boots.
    Stale or schema-bad view → empty list (fail-closed standby, no double-fire
    via fresher legacy peers).
    """
    if not view_enabled:
        return legacy_snaps
    if view is None:
        return legacy_snaps
    if view.get("schema") != SCHEMA:
        return []
    vts = _f(view.get("ts"))
    if vts is None or now - vts > stale_s:
        return []
    # Incomplete aggregate (missing expected army) → standby. Never fall
    # through to legacy peers that might green-light a double-fire.
    if view.get("incomplete"):
        return []
    snaps = peer_snaps_from_view(view, own_account_id=own_account_id)
    if snaps is None:
        return []
    return list(snaps)


def peer_paths_for(
    *,
    state_root: Path,
    kingdom_view_peers: str,
    overflow_peers: str,
) -> list[Path]:
    """Self + explicit kingdom peers, else overflow peer position paths."""
    paths: list[Path] = [state_root]
    seen_roots = {state_root.resolve()}
    raw = kingdom_view_peers.strip() or overflow_peers.strip()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        p = Path(part).expanduser()
        root = state_root_from_peer_path(p)
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root
        if resolved in seen_roots:
            continue
        seen_roots.add(resolved)
        paths.append(p)
    return paths
