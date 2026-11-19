"""State-gated offense — bank right-tail under HB ``state`` (2026-11-19).

Dayo dual mandate ATS-197/198: capture Mid/Runner MFE that journals green
then scrapes red; size/scale only as f(heartbeat state). Weak-stop birth
floor + early-tighten remain authoritative on stop geometry — this module
never widens/loosens stops and never undoes those gates.

Acts (each kill-switched, fail-OPEN on dark/stale state):
  - capital_scale   — size_mult / rem clip from rung/phase/scrape/twin
  - r_partial       — bank qty frac at MFE R-rungs before giveback
  - trail_after_mfe — trail only after Mid proof + state allows
  - sticky_apex     — twin_hot → single seat owns aggressive capture

R4/R5 remain stamp-only (would_rung) until Governor GO
(``offense_scale_loosen_enabled``). Scrape / R0–R1 never scale up.
"""

from __future__ import annotations

from typing import Any

# Mid proof → bank 25%; 1R → another 25%; 2R runner tax 25% of residual.
# Fractions apply to CURRENT remaining qty; runner keeps the rest.
DEFAULT_PARTIAL_RUNGS: tuple[tuple[float, float], ...] = (
    (0.5, 0.25),
    (1.0, 0.25),
    (2.0, 0.25),
)

# Aggressive capture acts that sticky-apex may monopolize under twin_hot.
APEX_ACTS = frozenset({
    "partial", "harvest", "live_peak", "mfe_sharpen", "apex",
})
# Trail/BE remain available to the non-apex twin (defense under conquest).
TRAIL_ACTS = frozenset({"trail", "breakeven", "bank"})

RUNG_MULT = {
    "R0": 0.0, "R1": 0.70, "R2": 0.85, "R3": 1.00, "R4": 1.15, "R5": 1.30,
}


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def _s(x: Any) -> str:
    return str(x or "").strip()


def parse_partial_rungs(raw: Any) -> tuple[tuple[float, float], ...]:
    """Parse ``0.5:0.25,1.0:0.25`` → ((trigger_r, frac), ...). Fail → default."""
    if not raw:
        return DEFAULT_PARTIAL_RUNGS
    if isinstance(raw, (list, tuple)):
        out = []
        for row in raw:
            try:
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    out.append((float(row[0]), float(row[1])))
            except (TypeError, ValueError):
                continue
        return tuple(out) if out else DEFAULT_PARTIAL_RUNGS
    parts = []
    for tok in str(raw).split(","):
        tok = tok.strip()
        if not tok or ":" not in tok:
            continue
        a, b = tok.split(":", 1)
        try:
            parts.append((float(a), float(b)))
        except (TypeError, ValueError):
            continue
    return tuple(parts) if parts else DEFAULT_PARTIAL_RUNGS


def build_hb_state(
        *,
        band: Any = None,
        headroom: Any = None,
        soft_box: Any = None,
        day_realized: Any = None,
        day_budget: Any = None,
        committed: Any = None,
        loss_latches_armed: Any = 0,
        twin_assets: Any = None,
        cooloff_post_loss: Any = None,
        equity: Any = None,
) -> dict[str, Any]:
    """Build an HB-shaped ``state`` from live planes (no I/O).

    Self-contained rung/scrape/phase so offense works even when the
    harness has not yet published ``_state_vector``. Tighten-only ladder
    mirrors the live rung doctrine; R4/R5 only as would_rung stamps.
    """
    hr = _f(headroom)
    box = _f(soft_box)
    h = (hr / box) if (hr is not None and box and box > 0) else None
    pi_day = _f(day_realized)
    budget = _f(day_budget)
    band_s = _s(band).upper()
    try:
        latches = int(loss_latches_armed or 0)
    except (TypeError, ValueError):
        latches = 0
    scrape = ((pi_day is not None and budget is not None and budget > 0
               and pi_day <= -0.5 * budget)
              or latches >= 2)
    if band_s in ("LOCKED", "BREACHED") or (h is not None and h <= 0.20):
        rung = "R0"
    elif scrape:
        rung = "R1"
    elif band_s == "CAUTIOUS":
        rung = "R2"
    else:
        rung = "R3"
    would = None
    twin_list = []
    if isinstance(twin_assets, (list, tuple, set, frozenset)):
        twin_list = [{"asset": _s(a)} for a in twin_assets if _s(a)]
    elif twin_assets:
        twin_list = [{"asset": _s(twin_assets)}]
    twin_hot = bool(twin_list)
    if (h is not None and h > 0.90 and pi_day is not None and pi_day >= 0
            and not twin_hot and not scrape):
        would = "R5" if h > 0.95 else "R4"
    if rung in ("R0", "R1"):
        phase = "P0"
    elif rung == "R2":
        phase = "P1"
    elif would == "R5":
        phase = "P4-eligible"
    else:
        phase = "P2" if (pi_day or 0.0) >= 0 else "P1"
    rem = None
    committed_v = _f(committed) or 0.0
    if budget is not None:
        rem = budget - committed_v - max(0.0, -(pi_day or 0.0))
    plc = cooloff_post_loss if isinstance(cooloff_post_loss, dict) else {}
    return {
        "box": {"equity": _f(equity), "headroom": hr, "soft_box": box,
                "h": h, "band": band_s or None, "breached": band_s in (
                    "LOCKED", "BREACHED")},
        "budget": {"day_budget": budget, "committed": committed_v,
                   "realized": pi_day, "risk_remaining": rem},
        "book": {"open": [], "n_open": 0},
        "rung": {"rung_id": rung, "size_mult": RUNG_MULT[rung],
                 "would_rung": would, "scrape_day": scrape, "h": h},
        "cooloff": {"post_loss": plc, "post_win": {}},
        "company": {"twin": twin_list, "opposite": []},
        "phase": phase,
    }


def normalize_state(state: Any) -> dict[str, Any]:
    """Flatten HB ``state`` / partial dict into a stable gate register.

    Fail-OPEN shape: missing planes → None fields; callers treat None as
    abstain (never invent a tighten-or-loosen from garbage).
    """
    if not isinstance(state, dict):
        return {
            "ok": False, "rung_id": None, "size_mult": None,
            "would_rung": None, "scrape_day": None, "h": None,
            "phase": None, "band": None, "risk_remaining": None,
            "twin_hot": False, "twin_assets": frozenset(),
            "cooloff_hot": False, "error": "no_state",
        }
    if state.get("error"):
        return {
            "ok": False, "rung_id": None, "size_mult": None,
            "would_rung": None, "scrape_day": None, "h": None,
            "phase": None, "band": None, "risk_remaining": None,
            "twin_hot": False, "twin_assets": frozenset(),
            "cooloff_hot": False, "error": str(state.get("error"))[:120],
        }
    rung = state.get("rung") if isinstance(state.get("rung"), dict) else {}
    box = state.get("box") if isinstance(state.get("box"), dict) else {}
    budget = state.get("budget") if isinstance(state.get("budget"), dict) else {}
    company = state.get("company") if isinstance(state.get("company"), dict) else {}
    cooloff = state.get("cooloff") if isinstance(state.get("cooloff"), dict) else {}
    twin_rows = company.get("twin") or company.get("twin_hot") or []
    twin_assets = frozenset()
    if isinstance(twin_rows, (list, tuple)):
        twin_assets = frozenset(
            _s(r.get("asset") if isinstance(r, dict) else r).upper()
            for r in twin_rows if r)
    elif twin_rows:
        twin_assets = frozenset({_s(twin_rows).upper()})
    plc = cooloff.get("post_loss") or {}
    cooloff_hot = bool(isinstance(plc, dict) and len(plc) > 0)
    rung_id = _s(rung.get("rung_id")).upper() or None
    size_mult = _f(rung.get("size_mult"))
    if size_mult is None and rung_id in RUNG_MULT:
        size_mult = RUNG_MULT[rung_id]
    return {
        "ok": True,
        "rung_id": rung_id,
        "size_mult": size_mult,
        "would_rung": _s(rung.get("would_rung")).upper() or None,
        "scrape_day": bool(rung.get("scrape_day")) if rung.get("scrape_day") is not None else None,
        "h": _f(rung.get("h") if rung.get("h") is not None else box.get("h")),
        "phase": _s(state.get("phase")) or None,
        "band": _s(box.get("band")).upper() or None,
        "risk_remaining": _f(budget.get("risk_remaining")),
        "day_budget": _f(budget.get("day_budget")),
        "realized": _f(budget.get("realized")),
        "twin_hot": bool(twin_assets),
        "twin_assets": twin_assets,
        "cooloff_hot": cooloff_hot,
        "error": None,
    }


def capital_scale_for_state(
        state: Any, *,
        enabled: bool = True,
        loosen_enabled: bool = False,
) -> dict[str, Any]:
    """Capital scale law: ``size_mult`` / rem posture = f(state).

    Tighten-only by default (mult ≤ 1.0). R4/R5 surface as ``would_rung``
    only unless ``loosen_enabled`` (Governor GO). Scrape / R0 / R1 never
    scale up. Fail-OPEN → mult 1.0 (legacy admitted risk) when state dark
    or kill OFF — never invent a haircut from unreadable state.
    """
    detail: dict[str, Any] = {
        "size_mult": 1.0, "would_rung": None, "rung_id": None,
        "scrape_day": False, "scaled": False, "reason": "ok",
        "rem_cap_frac": 1.0,
    }
    if not enabled:
        detail["reason"] = "kill_off"
        return detail
    g = normalize_state(state)
    if not g["ok"]:
        detail["reason"] = "state_dark_fail_open"
        return detail
    detail["rung_id"] = g["rung_id"]
    detail["would_rung"] = g["would_rung"]
    detail["scrape_day"] = bool(g["scrape_day"])
    detail["phase"] = g["phase"]
    detail["h"] = g["h"]
    detail["twin_hot"] = g["twin_hot"]
    detail["risk_remaining"] = g["risk_remaining"]

    mult = g["size_mult"] if g["size_mult"] is not None else 1.0
    rung = g["rung_id"] or "R3"

    # Law: scrape / R0 / R1 never scale up (ceiling 1.0 always here).
    if g["scrape_day"] or rung in ("R0", "R1"):
        mult = min(float(mult), RUNG_MULT.get(rung, 0.70))
        detail["rem_cap_frac"] = 0.50 if rung == "R1" else (0.0 if rung == "R0" else 0.50)
        detail["reason"] = "scrape_or_r0r1"
    elif rung == "R2" or g["phase"] in ("P0", "P1"):
        mult = min(float(mult), 0.85)
        detail["rem_cap_frac"] = 0.75
        detail["reason"] = "cautious_phase"
    else:
        mult = min(float(mult), 1.0)
        detail["rem_cap_frac"] = 1.0
        detail["reason"] = "base"

    # Twin hot: primary may hold ≤1.0; never climb; rem compressed.
    if g["twin_hot"]:
        mult = min(float(mult), 1.0)
        detail["rem_cap_frac"] = min(float(detail["rem_cap_frac"]), 0.50)
        detail["reason"] = "twin_hot_cap"

    # R4/R5 stamp-only unless Governor GO.
    if loosen_enabled and g["would_rung"] in ("R4", "R5") and not g["scrape_day"] and not g["twin_hot"]:
        want = RUNG_MULT[g["would_rung"]]
        if want > mult:
            mult = want
            detail["reason"] = "loosen_go"
            detail["scaled"] = True
    else:
        # Never bind would_rung into size_mult without GO.
        mult = min(float(mult), 1.0)

    detail["size_mult"] = float(mult)
    if mult < 1.0 - 1e-12:
        detail["scaled"] = True
    return detail


def apply_rem_clip(
        risk: float, state: Any, *,
        enabled: bool = True,
        loosen_enabled: bool = False,
) -> tuple[float, dict[str, Any]]:
    """Clip candidate risk by scale mult and rem×frac. Fail-OPEN keep risk."""
    detail = capital_scale_for_state(
        state, enabled=enabled, loosen_enabled=loosen_enabled)
    try:
        r = float(risk)
    except (TypeError, ValueError):
        return risk, {**detail, "reason": "bad_risk_fail_open"}
    if not enabled or r <= 0:
        return r, detail
    scaled = r * float(detail["size_mult"])
    rem = detail.get("risk_remaining")
    frac = float(detail.get("rem_cap_frac") or 1.0)
    if rem is not None and rem >= 0 and frac < 1.0 - 1e-12:
        scaled = min(scaled, float(rem) * frac)
    elif rem is not None and rem >= 0:
        scaled = min(scaled, float(rem))
    detail["risk_before"] = r
    detail["risk_after"] = max(0.0, float(scaled))
    return detail["risk_after"], detail


def offense_act_allowed(
        state: Any, act: str, *,
        enabled: bool = True,
        asset: Any = None,
        is_apex_seat: bool = True,
        sticky_apex_enabled: bool = True,
) -> tuple[bool, dict[str, Any]]:
    """Gate an offensive act against HB state.

    Fail-OPEN (allow) when kill OFF or state dark — never freeze the
    existing conquest stack on a publish fault. Fail-CLOSED (deny) only
    on explicit scrape/twin/rung predicates.
    """
    act_s = _s(act).lower()
    detail: dict[str, Any] = {"act": act_s, "reason": "ok"}
    if not enabled:
        detail["reason"] = "kill_off"
        return True, detail
    g = normalize_state(state)
    if not g["ok"]:
        detail["reason"] = "state_dark_fail_open"
        return True, detail
    detail.update({
        "rung_id": g["rung_id"], "phase": g["phase"],
        "scrape_day": g["scrape_day"], "twin_hot": g["twin_hot"],
        "h": g["h"], "band": g["band"],
    })

    # Size-up acts: absolute refuse on scrape / R0 / R1 / cooloff trauma.
    if act_s in ("size_up", "scale_up", "loosen"):
        if g["scrape_day"] or g["rung_id"] in ("R0", "R1") or g["cooloff_hot"]:
            detail["reason"] = "no_scale_scrape_or_r0r1"
            return False, detail
        if g["twin_hot"]:
            detail["reason"] = "no_scale_twin_hot"
            return False, detail
        return True, detail

    # Sticky apex: under twin_hot, only the apex seat may run APEX_ACTS.
    asset_u = _s(asset).upper()
    twin_hit = bool(
        g["twin_hot"] and (
            not asset_u
            or asset_u in g["twin_assets"]
            or any(asset_u.endswith(a) or a.endswith(asset_u)
                   for a in g["twin_assets"])
        )
    )
    if sticky_apex_enabled and twin_hit and act_s in APEX_ACTS and not is_apex_seat:
        detail["reason"] = "sticky_apex_standdown"
        detail["apex"] = False
        return False, detail

    # Scrape day: still allow partials/trail (bank under conquest) but
    # refuse harvest/sharpen aggression that re-risks the residual.
    if g["scrape_day"] and act_s in ("harvest", "mfe_sharpen", "live_peak"):
        # Partials + trail still OK; harvest-class stands down on scrape.
        detail["reason"] = "scrape_no_aggressive_harvest"
        return False, detail

    if g["rung_id"] == "R0" and act_s in APEX_ACTS:
        detail["reason"] = "r0_deny_apex"
        return False, detail

    detail["apex"] = bool(is_apex_seat)
    return True, detail


def sticky_apex_verdict(
        state: Any, *,
        asset: Any,
        seat_is_primary: bool = True,
        peer_mfe_r: Any = None,
        own_mfe_r: Any = None,
        enabled: bool = True,
) -> dict[str, Any]:
    """Who owns aggressive capture under twin_hot.

    Bank-proof preference: if peer mfe_peak_r is materially higher, the
    neighbour keeps apex (Alpha sticky-apex — don't first-fill-kill the
    banked soldier). Otherwise primary crypto seat (turbo) owns apex.
    """
    out = {
        "is_apex": True, "reason": "ok", "twin_hot": False,
        "asset": _s(asset).upper() or None,
    }
    if not enabled:
        out["reason"] = "kill_off"
        return out
    g = normalize_state(state)
    if not g["ok"]:
        out["reason"] = "state_dark_fail_open"
        return out
    asset_u = out["asset"]
    twin_hit = bool(
        g["twin_hot"] and asset_u and (
            asset_u in g["twin_assets"]
            or any(asset_u.endswith(a) or a.endswith(asset_u)
                   for a in g["twin_assets"])
        )
    )
    out["twin_hot"] = twin_hit
    if not twin_hit:
        out["reason"] = "no_twin"
        out["is_apex"] = True
        return out
    own = _f(own_mfe_r)
    peer = _f(peer_mfe_r)
    if own is not None and peer is not None and peer > own + 0.25:
        # Peer has the bank-proof Mid — stand down apex on this seat.
        out["is_apex"] = False
        out["reason"] = "peer_bank_proof"
        return out
    out["is_apex"] = bool(seat_is_primary) or (
        own is not None and peer is not None and own >= peer)
    out["reason"] = "primary_apex" if out["is_apex"] else "neighbour_standdown"
    return out


def trail_after_mfe_ok(
        bracket: Any, state: Any, *,
        enabled: bool = True,
        arm_r: float = 0.5,
) -> tuple[bool, dict[str, Any]]:
    """Trail may fire only after Mid MFE proof when kill ON.

    Composes WITH early_tighten (age + mfe) — this adds the state.phase /
    scrape posture: on P0 scrape we still allow trail (defense) once Mid
    is proven; without Mid proof we stand down. Fail-OPEN if state dark
    or kill OFF (legacy trail path).
    """
    detail: dict[str, Any] = {"arm_r": float(arm_r), "reason": "ok"}
    if not enabled:
        detail["reason"] = "kill_off"
        return True, detail
    if not isinstance(bracket, dict):
        detail["reason"] = "no_bracket_fail_open"
        return True, detail
    mfe = _f(bracket.get("mfe_peak_r"))
    detail["mfe_peak_r"] = mfe
    g = normalize_state(state)
    if not g["ok"]:
        detail["reason"] = "state_dark_fail_open"
        return True, detail
    detail["phase"] = g["phase"]
    detail["scrape_day"] = g["scrape_day"]
    detail["rung_id"] = g["rung_id"]
    if mfe is None:
        detail["reason"] = "missing_mfe_fail_open"
        return True, detail
    if mfe < float(arm_r):
        detail["reason"] = "mfe_below_arm"
        return False, detail
    return True, detail


def r_partial_verdict(
        bracket: Any, state: Any, *,
        enabled: bool = True,
        rungs: Any = None,
        is_apex_seat: bool = True,
        sticky_apex_enabled: bool = True,
) -> dict[str, Any] | None:
    """Return a partial reduce plan when MFE hits the next unpaid R-rung.

    None = no-op (already banked this rung / gates refuse / dark inputs).
    Never mutates bracket. Caller executes reduce + stamps
    ``offense_partial_rungs`` on success.
    """
    if not enabled:
        return None
    if not isinstance(bracket, dict):
        return None
    if bracket.get("status") not in (None, "open"):
        # absent status treated as open (registry rows)
        if bracket.get("status") not in (None, "open") and bracket.get("status"):
            if bracket.get("status") != "open":
                return None
    mfe = _f(bracket.get("mfe_peak_r"))
    qty = _f(bracket.get("quantity"))
    if mfe is None or qty is None or qty <= 0 or mfe <= 0:
        return None

    asset = bracket.get("asset")
    ok, gate = offense_act_allowed(
        state, "partial", enabled=True, asset=asset,
        is_apex_seat=is_apex_seat,
        sticky_apex_enabled=sticky_apex_enabled)
    if not ok:
        return None

    paid = bracket.get("offense_partial_rungs") or []
    if not isinstance(paid, list):
        paid = []
    paid_set = {round(float(x), 4) for x in paid if _f(x) is not None}

    ladder = parse_partial_rungs(rungs)
    chosen = None
    for trigger_r, frac in ladder:
        key = round(float(trigger_r), 4)
        if key in paid_set:
            continue
        if mfe + 1e-12 >= float(trigger_r):
            chosen = (float(trigger_r), float(frac))
            break  # nearest unpaid rung only (one partial per tick)
    if chosen is None:
        return None
    trigger_r, frac = chosen
    if frac <= 0 or frac >= 1.0:
        return None
    reduce_qty = qty * frac
    if reduce_qty <= 0:
        return None
    return {
        "trigger_r": trigger_r,
        "frac": frac,
        "reduce_qty": reduce_qty,
        "mfe_peak_r": mfe,
        "qty_before": qty,
        "gate": gate,
        "rung_id": gate.get("rung_id"),
        "phase": gate.get("phase"),
        "scrape_day": gate.get("scrape_day"),
    }
