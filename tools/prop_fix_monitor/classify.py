"""Pure classifiers for prop_fix_monitor (no I/O). Fail-closed helpers."""

from __future__ import annotations

from typing import Any


def classify_loop_error(error: str | None) -> str:
    """Map loop_error payload → coarse class for D1 oracles."""
    err = str(error or "")
    if "403" in err and "Forbidden" in err:
        return "html_403"
    if "book_view.json.tmp" in err or (
        "Errno 2" in err and "book_view" in err
    ):
        return "book_view_tmp"
    if "429" in err:
        return "http_429"
    if "Connection" in err or "timeout" in err.lower():
        return "conn"
    return "other"


def parse_symbol_min_coh_map(raw: str | None) -> dict[str, float]:
    """Parse PROP_SYMBOL_MIN_COH_FIRE=XAUT:4.0,GOLD:4.0 → {sym: floor}."""
    out: dict[str, float] = {}
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        sym, val = part.split(":", 1)
        try:
            out[sym.strip().upper()] = float(val.strip())
        except ValueError:
            continue
    return out


def gold_floor_ok(coh_map: dict[str, float], *, keep: float = 4.0) -> dict[str, Any]:
    """GOLD/XAUT floors must be ≥ keep (schema KEEP 4.0)."""
    gold = coh_map.get("GOLD")
    xaut = coh_map.get("XAUT")
    ok = True
    notes = []
    for name, val in (("GOLD", gold), ("XAUT", xaut)):
        if val is None:
            notes.append(f"{name}=missing")
            # missing map entry → code default may apply; flag as WARN not FAIL
            continue
        if val + 1e-9 < keep:
            ok = False
            notes.append(f"{name}={val}<{keep}")
        else:
            notes.append(f"{name}={val} OK")
    return {"ok": ok, "keep": keep, "gold": gold, "xaut": xaut, "notes": notes}


def slot_geometry(
    max_positions: int | None,
    open_n: int | None,
    reserve: int | None,
) -> dict[str, Any]:
    """Compute free = max−open; probes must stand down when free ≤ reserve."""
    if max_positions is None or open_n is None or reserve is None:
        return {
            "ok": False,
            "reason": "missing_geometry",
            "max": max_positions,
            "open": open_n,
            "free": None,
            "reserve": reserve,
            "probe_must_standdown": None,
        }
    free = int(max_positions) - int(open_n)
    return {
        "ok": True,
        "max": int(max_positions),
        "open": int(open_n),
        "free": free,
        "reserve": int(reserve),
        "probe_must_standdown": free <= int(reserve),
    }


def opposite_pairs(
    exposures: list[tuple[str, str, str]],
) -> list[dict[str, str]]:
    """Find same-asset opposite sides across seats.

    exposures: list of (seat, asset_norm, side) with side in {buy,sell}.
    """
    by_asset: dict[str, list[tuple[str, str]]] = {}
    for seat, asset, side in exposures:
        a = _norm_asset(asset)
        s = (side or "").lower()
        if not a or s not in ("buy", "sell"):
            continue
        by_asset.setdefault(a, []).append((seat, s))
    out: list[dict[str, str]] = []
    for asset, legs in by_asset.items():
        buys = [seat for seat, s in legs if s == "buy"]
        sells = [seat for seat, s in legs if s == "sell"]
        if buys and sells:
            out.append({
                "asset": asset,
                "buy_seats": ",".join(sorted(set(buys))),
                "sell_seats": ",".join(sorted(set(sells))),
            })
    return out


def _norm_asset(asset: str) -> str:
    a = (asset or "").strip()
    if a.lower().startswith("xyz:"):
        a = a.split(":", 1)[1]
    return a.upper()


def metal_veto_verdict(row: dict[str, Any] | None) -> dict[str, Any]:
    """D2: metal|* should stay released (veto false / immaterial suppress)."""
    if not row:
        return {"present": False, "ok": True, "alert": False, "note": "missing_row"}
    veto = bool(row.get("veto"))
    suppressed = bool(row.get("veto_suppressed_immaterial"))
    pnl = row.get("pnl")
    try:
        abs_pnl = abs(float(pnl)) if pnl is not None else None
    except (TypeError, ValueError):
        abs_pnl = None
    # Alert if re-armed without material |pnl| (≥$10 materiality bar)
    material = abs_pnl is not None and abs_pnl >= 10.0
    alert = veto and not material
    ok = (not veto) or material
    return {
        "present": True,
        "ok": ok,
        "alert": alert,
        "veto": veto,
        "veto_suppressed_immaterial": suppressed,
        "pnl": pnl,
        "abs_pnl": abs_pnl,
        "n": row.get("n"),
        "shrunk_expectancy": row.get("shrunk_expectancy"),
    }


def seat_role(seat: str) -> str:
    """Map state-root basename → doctrine role."""
    s = seat.replace("prop_harness_state_", "").replace("prop_harness_state", "demo")
    if s in ("", "demo"):
        return "demo"
    return s


DESIGNED_DEAD_SEATS = frozenset({"turbo2"})
PRIMARY_CRYPTO = "turbo"
NEIGHBOUR_CRYPTO = "ns_crypto"
GOLD_KEEP = 4.0
HB_STALE_S = 120.0
WINDOW_S = 4 * 3600
