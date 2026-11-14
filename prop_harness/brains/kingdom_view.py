"""KingdomView Phase 1 — shared kingdom memory (harness-wiring Law 2).

Merges ArmyState snapshots + heartbeats + loss_veto + thesis occupancy
slots into one JSON file armies can read. Army position legs carry the
sniper/overflow geometry contract (side/entry/stop + ``be_armed`` /
``r_moved``) so overflow freshness and the future prop→SoDEX sniper
evidence path share one read.

Consumer laws (graph-synthesis):
- Writer publish — fail-open (partial view, ``incomplete:true``).
- Overflow route — fail-closed on stale/incomplete (standby, no double-fire).
- Sniper evidence — fail-open on stale/missing (empty list → disarm; never
  blocks trading). Read-only — never mutates harness policy.

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
# Match intelligence.prop_trend_bridge.SNAPSHOT_FRESH_S — harness publishes
# every ~15s; 8 misses = dead evidence. Kept local to avoid ARIA import.
SNIPER_EVIDENCE_STALE_S = 120.0


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


def normalize_position(pos: Any) -> dict | None:
    """Geometry leg for overflow + sniper evidence. Always exposes R fields.

    ``r_moved`` / ``be_armed`` may be None/False when the publishing army
    had no mark — ARIA recomputes confirmation from its OWN marks; these
    are reference telemetry only (never risk math).
    """
    if not isinstance(pos, dict):
        return None
    asset = pos.get("asset")
    side = pos.get("side")
    if not asset or not side:
        return None
    entry = _f(pos.get("entry_ref"))
    stop = _f(pos.get("stop_price"))
    if entry is None or stop is None:
        return None
    r_moved = pos.get("r_moved", None)
    if r_moved is not None:
        r_moved = _f(r_moved)
    qty = _f(pos.get("quantity"))
    risk = pos.get("risk_usd")
    try:
        risk_usd = float(risk) if risk is not None else None
    except (TypeError, ValueError):
        risk_usd = None
    return {
        "asset": asset,
        "side": side,
        "entry_ref": entry,
        "stop_price": stop,
        "quantity": qty if qty is not None else 0.0,
        "risk_usd": risk_usd if risk_usd is not None else pos.get("risk_usd"),
        "opened_ts": _f(pos.get("opened_ts")),
        "be_armed": bool(pos.get("be_armed")),
        "r_moved": r_moved,
    }


def normalize_army_snap(snap: dict | None) -> dict | None:
    """Ensure ArmyState row carries normalized position+R legs."""
    if not snap or not isinstance(snap, dict):
        return None
    row = dict(snap)
    legs: list[dict] = []
    for pos in snap.get("positions") or []:
        norm = normalize_position(pos)
        if norm is not None:
            legs.append(norm)
    row["positions"] = legs
    return row


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
                "be_armed": bool(pos.get("be_armed")),
                "r_moved": _f(pos.get("r_moved")),
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
    row = normalize_army_snap(snap)
    if row is None:
        return None
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
    crisis: dict | None = None,
) -> dict:
    """Pure aggregate. Missing expected armies → ``incomplete:true``.

    ``armies[*].positions`` always carry ``be_armed`` / ``r_moved`` keys
    (normalized) for overflow freshness + sniper evidence consumers.
    Optional ``crisis`` plane (ATS-26) is observe+act-hint only — never
    owns TP geometry; callers compute via ``crisis_valve.crisis_plane``.
    """
    present = {
        k: (normalize_army_snap(v) or v)
        for k, v in armies.items() if v
    }
    expected = list(expected_ids) if expected_ids is not None else list(armies.keys())
    incomplete = any(armies.get(eid) is None for eid in expected) or not present
    slots = (thesis_slots if thesis_slots is not None
             else derive_thesis_slots(present, now=now, day=day))
    out = {
        "schema": SCHEMA,
        "ts": now,
        "incomplete": bool(incomplete),
        "armies": present,
        "loss_veto": loss_veto,
        "thesis_slots": slots,
        "slot_economy": derive_slot_economy(present),
        # Flat read-only evidence plane (Governor + future sniper path).
        # Duplicate of army positions — never authoritative for risk.
        "evidence_legs": evidence_legs_from_armies(present),
    }
    if crisis is not None:
        out["crisis"] = crisis
    return out


def evidence_legs_from_armies(armies: dict[str, dict | None]) -> list[dict]:
    """Flatten army positions into sniper-shaped evidence rows (+ R)."""
    legs: list[dict] = []
    for aid, snap in armies.items():
        if not snap:
            continue
        snap_ts = _f(snap.get("ts"))
        for pos in snap.get("positions") or []:
            norm = normalize_position(pos)
            if norm is None:
                continue
            legs.append({
                "army_id": aid,
                "account_id": snap.get("account_id") or aid,
                "sleeve": snap.get("sleeve"),
                "snap_ts": snap_ts,
                **norm,
            })
    return legs


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


def sniper_evidence_snaps(
    view: dict | None,
    *,
    now: float,
    stale_s: float = SNIPER_EVIDENCE_STALE_S,
) -> list[dict]:
    """Read-only ArmyState snaps for PropTrendBridge / sniper integrator.

    Fail-OPEN: missing, schema-bad, or stale view → ``[]`` (disarm path).
    Incomplete views still yield present armies (partial evidence > freeze).
    Never raises. Does not write harness state. Compatible with
    ``load_snapshots`` / bridge ``_legs`` (``ts``, ``sleeve``, ``positions``
    with entry/stop/side; ``be_armed``/``r_moved`` pass through as telemetry).
    """
    try:
        if not view or view.get("schema") != SCHEMA:
            return []
        vts = _f(view.get("ts"))
        if vts is None or now - vts > stale_s:
            return []
        armies = view.get("armies")
        if not isinstance(armies, dict):
            return []
        out: list[dict] = []
        for snap in armies.values():
            norm = normalize_army_snap(snap if isinstance(snap, dict) else None)
            if norm is None:
                continue
            # Per-army freshness: stale individual ArmyState drops out;
            # remaining peers still arm (fail-open aggregate).
            sts = _f(norm.get("ts"))
            if sts is None or now - sts > stale_s:
                continue
            out.append(norm)
        return out
    except Exception:
        return []


def sniper_evidence_legs(
    view: dict | None,
    *,
    now: float,
    stale_s: float = SNIPER_EVIDENCE_STALE_S,
) -> list[dict]:
    """Flat position+R evidence from the view. Fail-open → ``[]``."""
    try:
        snaps = sniper_evidence_snaps(view, now=now, stale_s=stale_s)
        if not snaps:
            return []
        # Rebuild from normalized snaps so stale armies are excluded.
        return evidence_legs_from_armies({
            (s.get("account_id") or f"army{i}"): s
            for i, s in enumerate(snaps)
        })
    except Exception:
        return []


def read_sniper_evidence(
    path: Path,
    *,
    now: float,
    read_json: Callable[[Path], Any],
    stale_s: float = SNIPER_EVIDENCE_STALE_S,
) -> list[dict]:
    """File helper for future integrator: view path → ArmyState snaps.

    Fail-open on IO/parse/stale — empty list, never raises.
    """
    try:
        view = read_kingdom_view(path, read_json=read_json)
        return sniper_evidence_snaps(view, now=now, stale_s=stale_s)
    except Exception:
        return []


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
