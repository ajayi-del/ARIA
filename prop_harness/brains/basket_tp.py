"""Basket conquest controller — peak-push + side-split tax epochs.

Consumes basket_graph.GraphSnapshot. Long book and short book are taxed
SEPARATELY (Dayo HOLD extension): a trending short sleeve can harvest even
when the long sleeve is red, and vice versa. Chart never inputs here —
book dispose only. Comove hedge-refuse stays on the Book plane.

HOLD axioms (encode, do not collapse)
  C1  capital_scale = verified equity first, else profit_target, else 1.0
      clamp [0.25,20]; persist scale_source + scale_epoch
  T1  book_upnl_pos; tax≥0; due≥0; no double credit; peak immutable/epoch
  T2  max_leg_peak = peak-profit protection (not current PnL)
  T3  failed close ≠ paid; credit ≤ realised; reconnect replay idempotent
  T4  new leg identity on reopen (prune leg_peak_roe)
  S1  side-split tax epochs (buy|sell) — independent due/peak/cooloff
  R1  residual: close 75% → resize stop → cancel TP ladder → never naked
  H1  individual TP credits tax once; chart cannot bypass book dispose

Pure logic — actuation in executor. Kill OFF → empty.
MUST-NOT Kant/Nietzsche/Chancellor/tiers 1–6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import basket_graph as bg


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return v


DEFAULT_RESIDUAL_FRAC = 0.25          # ARIA treasury_runner_ratio
DEFAULT_RESIDUAL_MIN_COH = 4.0
SIDE_KEYS = ("buy", "sell")

# Residual handover phases (never drop protection).
RESIDUAL_TAX_ARMED = "TAX_ARMED"
RESIDUAL_CLOSE_DONE = "CLOSE_DONE"
RESIDUAL_STOP_RESIZED = "STOP_RESIZED"
RESIDUAL_LADDER_CLEARED = "LADDER_CLEARED"
RESIDUAL_RECONCILED = "RECONCILED"


@dataclass
class HarvestTarget:
    semantic_id: str
    asset: str
    upnl: float
    risk_usd: float
    peak_usd: float
    roe: float
    coherence: float | None
    close_frac: float = 1.0
    leave_residual: bool = False
    book_side: str = ""           # buy|sell — side-split provenance


@dataclass
class BasketDecision:
    fire: bool = False
    reason: str = ""
    n: int = 0
    book_upnl: float = 0.0
    sum_risk: float = 0.0
    book_frac: float = 0.0
    peak_book_usd: float = 0.0
    peak_book_roe: float = 0.0
    tax_usd: float = 0.0
    harvest_usd: float = 0.0
    max_leg_peak_usd: float = 0.0
    payer_asset: str = ""
    payer_roe: float = 0.0
    residual_allowed: bool = False
    adaptive_arm_frac: float = 0.0
    adaptive_min_book: float = 0.0
    peak_epoch: float = 0.0
    fire_side: str = ""           # buy|sell|both
    pushes: list[str] = field(default_factory=list)
    targets: list[HarvestTarget] = field(default_factory=list)
    telemetry: dict[str, Any] = field(default_factory=dict)


def tax_bank_usd(
        book_upnl: float,
        payer: bg.LegNode | None,
        legs: list[bg.LegNode],
        *,
        harvest_ratio: float,
        tax_bank_enabled: bool,
) -> tuple[float, float]:
    """Safer tax bank (HOLD).

    book_upnl_pos = max(0, book) — never ratio-tax a red book.
    max_leg_peak = peak-profit protection (MFE peak $), NOT current PnL.
    tax ≥ 0 always. tax-bank ON → max(ratio×pos, max_leg_peak).
    """
    book_pos = max(0.0, float(book_upnl))
    ratio_tax = max(0.0, float(harvest_ratio) * book_pos)
    max_leg = 0.0
    for L in legs:
        pk = max(0.0, float(L.peak_usd or 0.0))
        if pk > max_leg:
            max_leg = pk
    if payer is not None:
        pk = max(0.0, float(payer.peak_usd or 0.0))
        if pk > max_leg:
            max_leg = pk
    if not tax_bank_enabled:
        return ratio_tax, max_leg
    return max(ratio_tax, max_leg), max_leg


def residual_ok(
        leg: bg.LegNode, *, enabled: bool, min_coh: float,
) -> bool:
    """Fail-closed: dark coh or non-green → no residual."""
    if not enabled:
        return False
    if not leg.green:
        return False
    if leg.coherence is None or leg.coherence < float(min_coh):
        return False
    return True


def select_harvest(
        legs: list[bg.LegNode],
        tax_usd: float,
        *,
        residual_enabled: bool,
        residual_frac: float,
        residual_min_coh: float,
        book_side: str = "",
) -> tuple[list[HarvestTarget], float, bool]:
    """Highest-ROE winners first until tax_usd of credited close is covered."""
    if tax_usd <= 0:
        return [], 0.0, False
    winners = sorted(
        (L for L in legs if L.green),
        key=lambda L: -float(L.roe or 0.0),
    )
    picked: list[HarvestTarget] = []
    harvested = 0.0
    any_residual = False
    res_frac = min(0.95, max(0.0, float(residual_frac)))
    for L in winners:
        if harvested >= tax_usd:
            break
        upnl = float(L.upnl or 0.0)
        leave = residual_ok(
            L, enabled=residual_enabled, min_coh=residual_min_coh)
        close_frac = (1.0 - res_frac) if leave and res_frac > 0 else 1.0
        if close_frac <= 0:
            close_frac = 1.0
            leave = False
        credited = upnl * close_frac
        picked.append(HarvestTarget(
            semantic_id=L.semantic_id, asset=L.asset,
            upnl=upnl, risk_usd=L.risk_usd, peak_usd=L.peak_usd,
            roe=float(L.roe or 0.0), coherence=L.coherence,
            close_frac=close_frac, leave_residual=leave,
            book_side=book_side or (bg.side_key(L.side) or ""),
        ))
        harvested += credited
        any_residual = any_residual or leave
    return picked, harvested, any_residual


def individual_banked_usd(
        open_brackets: list | None, *, side: str | None = None,
) -> float:
    """Σ venue-realized booked — credit ≤ realised only.

    side=buy|sell scopes to that book; None = whole open book.
    """
    total = 0.0
    want = bg.side_key(side) if side else None
    for b in open_brackets or []:
        if b.get("status") and b.get("status") != "open":
            continue
        if want is not None and bg.side_key(b.get("side")) != want:
            continue
        rb = _f(b.get("realized_booked")) or 0.0
        if rb > 0:
            total += rb
    return total


def unpaid_tax(
        tax_usd: float, state: dict, *,
        individual_banked: float = 0.0,
        taxed_key: str = "taxed_usd_in_epoch",
) -> float:
    """due = tax − max(epoch_paid, individual_banked); due≥0; replay-safe."""
    epoch = _f(state.get(taxed_key)) or 0.0
    paid = max(float(epoch), max(0.0, float(individual_banked)))
    return max(0.0, max(0.0, float(tax_usd)) - paid)


def _side_state(root: dict, side: str) -> dict:
    """Per-side tax epoch dict (create if missing)."""
    sides = root.setdefault("sides", {})
    if not isinstance(sides, dict):
        sides = {}
        root["sides"] = sides
    sk = bg.side_key(side) or side
    st = sides.get(sk)
    if not isinstance(st, dict):
        st = {
            "peak_book_roe": 0.0,
            "peak_book_usd": 0.0,
            "taxed_usd_in_epoch": 0.0,
            "taxed_peak_roe": 0.0,
            "last_fire_ts": 0.0,
        }
        sides[sk] = st
    return st


def reset_tax_epoch(state: dict, *, reason: str = "account_reset",
                    side: str | None = None) -> dict:
    """Clear peak/tax ledger. side=None clears both + legacy combined."""
    def _clear(st: dict) -> None:
        st["peak_book_roe"] = 0.0
        st["peak_book_usd"] = 0.0
        st["taxed_usd_in_epoch"] = 0.0
        st["taxed_peak_roe"] = 0.0
        st["last_fire_ts"] = 0.0

    if side is None:
        _clear(state)
        state["leg_peak_roe"] = {}
        for sk in SIDE_KEYS:
            _clear(_side_state(state, sk))
        state["scale_epoch"] = float(state.get("scale_epoch") or 0.0) + 1.0
        state["tax_epoch_reset_reason"] = reason
    else:
        _clear(_side_state(state, side))
        state["tax_epoch_reset_reason"] = f"{reason}:{side}"
    return state


def prune_leg_peaks(state: dict, open_brackets: list | None) -> None:
    """Drop peak rows for closed/reopened identities (new sid = new peak)."""
    live = {
        str(b.get("semantic_id") or "")
        for b in (open_brackets or [])
        if (not b.get("status") or b.get("status") == "open")
        and b.get("semantic_id")
    }
    prev = state.get("leg_peak_roe") or {}
    if not isinstance(prev, dict):
        state["leg_peak_roe"] = {}
        return
    state["leg_peak_roe"] = {k: v for k, v in prev.items() if k in live}


def apply_scale_epoch(
        state: dict, *,
        scale: float,
        scale_source: str,
        equity: float | None = None,
) -> bool:
    """Persist scale_source + scale; reset tax epoch on account/source jump."""
    prev_src = state.get("scale_source")
    prev_sc = _f(state.get("capital_scale"))
    prev_eq = _f(state.get("scale_equity"))
    state["scale_source"] = scale_source
    state["capital_scale"] = float(scale)
    if equity is not None and float(equity) > 0:
        state["scale_equity"] = float(equity)
    if prev_src is None and prev_sc is None:
        state.setdefault("scale_epoch", 1.0)
        return False
    reset = False
    reason = ""
    if prev_src and prev_src != scale_source:
        reset, reason = True, f"scale_source_{prev_src}_to_{scale_source}"
    elif (equity is not None and prev_eq is not None and prev_eq > 0
          and abs(float(equity) - prev_eq) / prev_eq >= 0.50):
        reset, reason = True, "equity_jump"
    elif prev_sc is not None and abs(float(scale) - prev_sc) > 1e-9 and (
            (prev_sc < 1.5 and float(scale) >= 4.0)
            or (prev_sc >= 4.0 and float(scale) < 1.5)):
        reset, reason = True, "scale_regime_jump"
    if reset:
        reset_tax_epoch(state, reason=reason or "scale_epoch")
        return True
    state.setdefault("scale_epoch", 1.0)
    return False


def handover_after_basket(
        bracket: dict, *,
        close_frac: float,
        leave_residual: bool,
        estimated_realized: float | None = None,
) -> dict:
    """Pure registry mutations after a SUCCESSFUL basket close.

    H5: does NOT stamp estimated into realized_booked (credit ≤ realised).
    """
    out = {
        "semantic_id": bracket.get("semantic_id"),
        "asset": bracket.get("asset"),
        "leave_residual": bool(leave_residual),
        "close_frac": float(close_frac),
        "needs_stop_resize": False,
        "closed": False,
        "residual_phase": RESIDUAL_TAX_ARMED,
    }
    risk = _f(bracket.get("risk_usd")) or 0.0
    already = _f(bracket.get("risk_released")) or 0.0
    frac = min(1.0, max(0.0, float(close_frac)))
    if estimated_realized is not None:
        bracket["pending_realized"] = float(estimated_realized)
        out["pending_realized"] = float(estimated_realized)
    if not leave_residual or frac >= 1.0 - 1e-12:
        bracket["status"] = "closed_external"
        bracket["basket_harvested"] = True
        bracket["ladder_reset"] = True
        bracket["residual_phase"] = RESIDUAL_RECONCILED
        release = max(0.0, risk - already)
        if release > 0:
            bracket["risk_released"] = already + release
        out["closed"] = True
        out["risk_released_delta"] = release
        out["residual_phase"] = RESIDUAL_RECONCILED
        return out

    try:
        from decimal import Decimal
        full = Decimal(str(bracket.get("quantity") or "0"))
        left = full * (Decimal("1") - Decimal(str(frac)))
        if left <= 0:
            bracket["status"] = "closed_external"
            bracket["basket_harvested"] = True
            out["closed"] = True
            out["residual_phase"] = RESIDUAL_RECONCILED
            return out
        bracket["quantity"] = format(left, "f")
    except Exception:
        pass
    target_released = risk * frac
    release = max(0.0, target_released - already)
    if release > 0:
        bracket["risk_released"] = already + release
    bracket["basket_residual"] = True
    bracket["basket_harvested"] = True
    bracket["ladder_reset"] = True
    bracket["owns_tp"] = False
    bracket["residual_phase"] = RESIDUAL_CLOSE_DONE
    out["needs_stop_resize"] = True
    out["risk_released_delta"] = release
    out["residual_qty"] = bracket.get("quantity")
    out["residual_phase"] = RESIDUAL_CLOSE_DONE
    return out


def advance_residual_phase(bracket: dict, phase: str) -> str:
    """Advance residual SM; never regresses past RECONCILED."""
    order = (
        RESIDUAL_TAX_ARMED, RESIDUAL_CLOSE_DONE, RESIDUAL_STOP_RESIZED,
        RESIDUAL_LADDER_CLEARED, RESIDUAL_RECONCILED,
    )
    cur = bracket.get("residual_phase") or RESIDUAL_TAX_ARMED
    try:
        if order.index(phase) >= order.index(cur):
            bracket["residual_phase"] = phase
    except ValueError:
        bracket["residual_phase"] = phase
    return str(bracket.get("residual_phase"))


def handover_after_individual_tp(bracket: dict) -> dict:
    """Idempotent individual TP credit stamp (reconnect replay safe)."""
    rb = _f(bracket.get("realized_booked")) or 0.0
    already = bool(bracket.get("individual_tp_credited"))
    prev = _f(bracket.get("individual_tp_credited_usd")) or 0.0
    if rb > prev + 1e-9:
        bracket["individual_tp_credited"] = True
        bracket["individual_tp_credited_usd"] = rb
    elif not already:
        bracket["individual_tp_credited"] = True
        bracket["individual_tp_credited_usd"] = rb
    return {
        "semantic_id": bracket.get("semantic_id"),
        "asset": bracket.get("asset"),
        "realized_booked": rb,
        "credited_usd": bracket.get("individual_tp_credited_usd"),
        "source": "individual_tp",
        "replay": already and abs(rb - prev) <= 1e-9,
        "book_side": bg.side_key(bracket.get("side")) or "",
    }


def _side_book_metrics(legs: list[bg.LegNode]) -> tuple[float, float, float, int]:
    """(book_upnl, sum_risk, book_roe, n) for a side's legs."""
    book = 0.0
    risk = 0.0
    n = 0
    for L in legs:
        n += 1
        if L.risk_usd > 0:
            risk += L.risk_usd
        if L.upnl is not None:
            book += L.upnl
    roe = (book / risk) if risk > 0 else 0.0
    return book, risk, roe, n


def _side_verdict(
        side: str,
        side_legs: list[bg.LegNode],
        side_st: dict,
        *,
        open_brackets: list | None,
        now: float,
        min_n: int,
        min_book: float,
        arm: float,
        harvest_ratio: float,
        giveback_frac: float,
        cooloff_s: float,
        tax_bank_enabled: bool,
        residual_pyramid_enabled: bool,
        residual_frac: float,
        residual_min_coh: float,
) -> dict:
    """Evaluate one book side. Fail-closed on empty/dark/cooloff."""
    out: dict[str, Any] = {
        "side": side, "fire": False, "reason": "", "targets": [],
        "harvest_usd": 0.0, "tax_usd": 0.0, "due": 0.0,
        "max_leg": 0.0, "payer": None, "book_upnl": 0.0,
        "sum_risk": 0.0, "book_roe": 0.0, "n": 0,
        "peak_roe": 0.0, "peak_usd": 0.0,
    }
    book, risk, roe, n = _side_book_metrics(side_legs)
    out.update(book_upnl=book, sum_risk=risk, book_roe=roe, n=n)
    # Raise-only peaks within side epoch.
    peak_roe = max(float(_f(side_st.get("peak_book_roe")) or 0.0),
                   roe if roe > 0 else 0.0)
    peak_usd = max(float(_f(side_st.get("peak_book_usd")) or 0.0),
                   book if book > 0 else 0.0)
    side_st["peak_book_roe"] = peak_roe
    side_st["peak_book_usd"] = peak_usd
    out["peak_roe"] = peak_roe
    out["peak_usd"] = peak_usd

    if n < int(min_n):
        out["reason"] = "min_n"
        return out
    if risk <= 0:
        out["reason"] = "no_risk"
        return out
    greens = [L for L in side_legs if L.green]
    if not greens:
        out["reason"] = "no_green"
        return out
    readable = sum(1 for L in side_legs if L.upnl is not None)
    if readable <= 0:
        out["reason"] = "stale_book"
        return out
    payer = max(greens, key=lambda L: float(L.roe or 0.0))
    out["payer"] = payer

    last_fire = _f(side_st.get("last_fire_ts")) or 0.0
    if now > 0 and last_fire > 0 and (now - last_fire) < float(cooloff_s):
        out["reason"] = "cooloff"
        return out

    at_border = (n >= int(min_n) and book >= min_book and roe >= arm)
    peak_border = (peak_usd >= min_book and peak_roe >= arm)
    trail_fire = (
        peak_border and book > 0
        and roe <= float(giveback_frac) * peak_roe
        and peak_roe > roe
    )
    new_peak = peak_roe > float(_f(side_st.get("_prev_peak_roe_seen")) or 0.0) + 1e-12
    # Track seen for telemetry; peak already raised above.
    side_st["_prev_peak_roe_seen"] = peak_roe

    if at_border and new_peak:
        reason = "basket_peak"
    elif at_border:
        reason = "border"
    elif trail_fire:
        reason = "trail_giveback"
    else:
        out["reason"] = "below_border"
        return out

    tax, max_leg = tax_bank_usd(
        book, payer, side_legs,
        harvest_ratio=harvest_ratio, tax_bank_enabled=tax_bank_enabled)
    ind = individual_banked_usd(open_brackets, side=side)
    due = unpaid_tax(tax, side_st, individual_banked=ind)
    out.update(tax_usd=tax, due=due, max_leg=max_leg)

    taxed_peak = _f(side_st.get("taxed_peak_roe")) or 0.0
    if (reason != "trail_giveback" and due <= 1e-6
            and taxed_peak + 1e-12 >= peak_roe):
        out["reason"] = "peak_epoch_taxed"
        return out
    if due <= 1e-6 and ind + 1e-9 >= tax and reason != "trail_giveback":
        out["reason"] = "individual_tp_paid"
        return out

    need = due if due > 1e-6 else tax
    targets, harvested, any_res = select_harvest(
        side_legs, need,
        residual_enabled=residual_pyramid_enabled,
        residual_frac=residual_frac,
        residual_min_coh=residual_min_coh,
        book_side=side,
    )
    if not targets:
        out["reason"] = "no_targets"
        return out
    out.update(
        fire=True, reason=reason, targets=targets,
        harvest_usd=harvested, residual=any_res,
        individual_banked=ind,
    )
    return out


def basket_tp_verdict(
        profile: str,
        open_brackets: list | None,
        state: dict | None,
        *,
        enabled: bool = True,
        now_ts: float | None = None,
        min_n: int = 2,
        min_book_usd: float = 10.0,
        arm_frac: float = 0.25,
        harvest_ratio: float = 0.60,
        giveback_frac: float = 0.60,
        cooloff_s: float = 90.0,
        tax_bank_enabled: bool = True,
        residual_pyramid_enabled: bool = True,
        residual_frac: float = DEFAULT_RESIDUAL_FRAC,
        residual_min_coh: float = DEFAULT_RESIDUAL_MIN_COH,
        adaptive_thresholds: bool = True,
        equity: float | None = None,
        profit_target: float | None = None,
        capital_scale_enabled: bool = True,
        side_split_enabled: bool = True,
) -> BasketDecision:
    """Controller: side-split tax → fire trending side even if peer is red."""
    d = BasketDecision()
    if not enabled or profile != "evaluation":
        d.reason = "disabled"
        return d

    st = state if isinstance(state, dict) else {}
    sc = 1.0
    scale_source = bg.SCALE_SOURCE_DEFAULT
    if capital_scale_enabled:
        sc, scale_source = bg.resolve_capital_scale(equity, profit_target)
    apply_scale_epoch(
        st, scale=sc if capital_scale_enabled else 1.0,
        scale_source=(scale_source if capital_scale_enabled
                      else bg.SCALE_SOURCE_DEFAULT),
        equity=equity)
    prune_leg_peaks(st, open_brackets)

    prev_leg_peaks = st.get("leg_peak_roe") or {}
    if not isinstance(prev_leg_peaks, dict):
        prev_leg_peaks = {}

    snap = bg.build_graph(
        open_brackets,
        prev_leg_peaks=prev_leg_peaks,
        prev_basket_peak_roe=float(_f(st.get("peak_book_roe")) or 0.0),
        prev_basket_peak_usd=float(_f(st.get("peak_book_usd")) or 0.0),
    )
    st["leg_peak_roe"] = bg.leg_peak_map(snap)
    # Combined peaks kept for telemetry / legacy; side epochs are authoritative.
    st["peak_book_roe"] = max(
        float(_f(st.get("peak_book_roe")) or 0.0), snap.basket.peak_book_roe)
    st["peak_book_usd"] = max(
        float(_f(st.get("peak_book_usd")) or 0.0), snap.basket.peak_book_usd)

    b = snap.basket
    d.n = b.n
    d.book_upnl = round(b.book_upnl, 4)
    d.sum_risk = round(b.sum_risk, 4)
    d.book_frac = round(b.book_roe, 4)
    d.peak_book_usd = round(float(st["peak_book_usd"]), 4)
    d.peak_book_roe = round(float(st["peak_book_roe"]), 4)
    d.pushes = [p.kind for p in snap.pushes]
    if snap.payer is not None:
        d.payer_asset = snap.payer.asset
        d.payer_roe = round(float(snap.payer.roe or 0.0), 4)

    arm = float(arm_frac)
    min_book = float(min_book_usd)
    # Adaptive uses combined N for base thresholds; side uses own N below.
    if adaptive_thresholds:
        arm = bg.adaptive_arm_frac(max(b.n, 2), base=arm_frac)
        min_book = bg.adaptive_min_book_usd(
            max(b.n, 2), base=min_book_usd,
            scale=sc if capital_scale_enabled else 1.0)
    elif capital_scale_enabled:
        min_book = bg.scale_usd(min_book, scale=sc)
    d.adaptive_arm_frac = round(arm, 4)
    d.adaptive_min_book = round(min_book, 4)
    d.telemetry.update({
        "capital_scale": round(sc, 4),
        "scale_source": scale_source,
        "scale_epoch": st.get("scale_epoch"),
        "side_split_enabled": bool(side_split_enabled),
    })

    if b.n < 1:
        d.reason = "min_n"
        return d
    if b.sum_risk <= 0:
        d.reason = "no_risk"
        return d
    if snap.payer is None:
        d.reason = "no_green"
        return d
    readable = sum(1 for L in snap.legs if L.upnl is not None)
    if readable <= 0:
        d.reason = "stale_book"
        return d

    now = float(now_ts) if now_ts is not None else 0.0

    # ── Side-split path (HOLD extension) ─────────────────────────────
    if side_split_enabled:
        side_results = []
        for sk in SIDE_KEYS:
            s_legs = bg.side_legs(snap.legs, sk)
            if adaptive_thresholds:
                s_arm = bg.adaptive_arm_frac(max(len(s_legs), 2), base=arm_frac)
                s_min = bg.adaptive_min_book_usd(
                    max(len(s_legs), 2), base=min_book_usd,
                    scale=sc if capital_scale_enabled else 1.0)
            else:
                s_arm, s_min = arm, min_book
            sv = _side_verdict(
                sk, s_legs, _side_state(st, sk),
                open_brackets=open_brackets, now=now,
                min_n=min_n, min_book=s_min, arm=s_arm,
                harvest_ratio=harvest_ratio, giveback_frac=giveback_frac,
                cooloff_s=cooloff_s, tax_bank_enabled=tax_bank_enabled,
                residual_pyramid_enabled=residual_pyramid_enabled,
                residual_frac=residual_frac,
                residual_min_coh=residual_min_coh,
            )
            side_results.append(sv)

        d.telemetry["sides"] = {
            r["side"]: {
                "n": r["n"], "book_upnl": round(r["book_upnl"], 4),
                "sum_risk": round(r["sum_risk"], 4),
                "book_roe": round(r["book_roe"], 4),
                "peak_roe": round(r["peak_roe"], 4),
                "peak_usd": round(r["peak_usd"], 4),
                "tax_usd": round(r["tax_usd"], 4),
                "due": round(r["due"], 4),
                "fire": r["fire"], "reason": r["reason"],
                "payer": (r["payer"].asset if r["payer"] else None),
            }
            for r in side_results
        }
        firing = [r for r in side_results if r["fire"]]
        if not firing:
            # Prefer the most actionable standdown reason.
            reasons = [r["reason"] for r in side_results if r["reason"]]
            priority = (
                "cooloff", "peak_epoch_taxed", "individual_tp_paid",
                "below_border", "no_green", "stale_book", "min_n",
                "no_risk", "no_targets",
            )
            pick = "below_border"
            for p in priority:
                if p in reasons:
                    pick = p
                    break
            d.reason = pick
            return d

        # Merge firing sides (trending side harvests even if peer red).
        all_targets: list[HarvestTarget] = []
        harvest = 0.0
        tax = 0.0
        max_leg = 0.0
        any_res = False
        sides_fired: list[str] = []
        top_payer = None
        top_roe = -1.0
        reason = firing[0]["reason"]
        for r in firing:
            sides_fired.append(r["side"])
            all_targets.extend(r["targets"])
            harvest += float(r["harvest_usd"])
            tax += float(r["tax_usd"])
            max_leg = max(max_leg, float(r["max_leg"]))
            any_res = any_res or bool(r.get("residual"))
            if r["payer"] is not None and float(r["payer"].roe or 0) > top_roe:
                top_payer = r["payer"]
                top_roe = float(r["payer"].roe or 0)
            if r["reason"] == "trail_giveback":
                reason = "trail_giveback"
            elif r["reason"] == "basket_peak" and reason != "trail_giveback":
                reason = "basket_peak"

        d.fire = True
        d.reason = reason
        d.targets = all_targets
        d.harvest_usd = round(harvest, 4)
        d.tax_usd = round(tax, 4)
        d.max_leg_peak_usd = round(max_leg, 4)
        d.residual_allowed = any_res
        d.fire_side = "both" if len(sides_fired) > 1 else sides_fired[0]
        d.peak_epoch = round(max(r["peak_roe"] for r in firing), 6)
        if top_payer is not None:
            d.payer_asset = top_payer.asset
            d.payer_roe = round(top_roe, 4)
        d.telemetry.update({
            "min_n": min_n,
            "min_book_usd": min_book,
            "arm_frac": arm,
            "harvest_ratio": harvest_ratio,
            "giveback_frac": giveback_frac,
            "tax_bank_enabled": bool(tax_bank_enabled),
            "residual_pyramid_enabled": bool(residual_pyramid_enabled),
            "residual_frac": residual_frac,
            "residual_min_coh": residual_min_coh,
            "adaptive_thresholds": bool(adaptive_thresholds),
            "capital_scale_enabled": bool(capital_scale_enabled),
            "equity": equity,
            "profit_target": profit_target,
            "n_targets": len(all_targets),
            "n_residual": sum(1 for t in all_targets if t.leave_residual),
            "fire_side": d.fire_side,
            "sides_fired": sides_fired,
            "max_leg_peak_role": "peak_profit_protection",
            "pushes": d.pushes,
            "payer": d.payer_asset,
            "payer_roe": d.payer_roe,
        })
        return d

    # ── Legacy combined path (side_split OFF) ────────────────────────
    if b.n < int(min_n):
        d.reason = "min_n"
        return d
    last_fire = _f(st.get("last_fire_ts")) or 0.0
    if now > 0 and last_fire > 0 and (now - last_fire) < float(cooloff_s):
        d.reason = "cooloff"
        return d
    at_border = (
        b.n >= int(min_n) and b.book_upnl >= min_book and b.book_roe >= arm)
    peak_border = (
        b.peak_book_usd >= min_book and b.peak_book_roe >= arm)
    trail_fire = (
        peak_border and b.book_upnl > 0
        and b.book_roe <= float(giveback_frac) * b.peak_book_roe
        and b.peak_book_roe > b.book_roe)
    new_basket_peak = any(p.kind == "basket_peak_frac" for p in snap.pushes)
    if at_border and new_basket_peak:
        reason = "basket_peak"
    elif at_border:
        reason = "border"
    elif trail_fire:
        reason = "trail_giveback"
    else:
        d.reason = "below_border"
        return d
    tax, max_leg = tax_bank_usd(
        b.book_upnl, snap.payer, snap.legs,
        harvest_ratio=harvest_ratio, tax_bank_enabled=tax_bank_enabled)
    ind_banked = individual_banked_usd(open_brackets)
    due = unpaid_tax(tax, st, individual_banked=ind_banked)
    d.tax_usd = round(tax, 4)
    d.max_leg_peak_usd = round(max_leg, 4)
    d.peak_epoch = round(b.peak_book_roe, 6)
    taxed_epoch = _f(st.get("taxed_peak_roe")) or 0.0
    if (reason != "trail_giveback" and due <= 1e-6
            and taxed_epoch + 1e-12 >= b.peak_book_roe):
        d.reason = "peak_epoch_taxed"
        return d
    if due <= 1e-6 and ind_banked + 1e-9 >= tax and reason != "trail_giveback":
        d.reason = "individual_tp_paid"
        return d
    harvest_need = due if due > 1e-6 else tax
    targets, harvested, any_res = select_harvest(
        snap.legs, harvest_need,
        residual_enabled=residual_pyramid_enabled,
        residual_frac=residual_frac,
        residual_min_coh=residual_min_coh,
    )
    if not targets:
        d.reason = "no_targets"
        return d
    d.fire = True
    d.reason = reason
    d.targets = targets
    d.harvest_usd = round(harvested, 4)
    d.residual_allowed = any_res
    d.fire_side = "combined"
    d.telemetry.update({
        "min_n": min_n, "min_book_usd": min_book, "arm_frac": arm,
        "unpaid_tax": round(due, 4),
        "individual_banked": round(ind_banked, 4),
        "max_leg_peak_role": "peak_profit_protection",
        "n_targets": len(targets),
    })
    return d


def mark_fired(
        state: dict, now_ts: float, decision: BasketDecision,
        *,
        realized_harvest_usd: float | None = None,
        realized_by_side: dict[str, float] | None = None,
) -> None:
    """Stamp after ≥1 successful close. Failed closes ≠ paid."""
    paid = (float(realized_harvest_usd)
            if realized_harvest_usd is not None
            else float(decision.harvest_usd))
    paid = max(0.0, paid)
    state["last_fire_ts"] = float(now_ts)
    state["last_fire_reason"] = decision.reason
    state["last_harvest_usd"] = paid
    state["last_tax_usd"] = float(decision.tax_usd)
    state["last_residual"] = bool(decision.residual_allowed)
    state["last_payer"] = decision.payer_asset
    state["last_fire_side"] = decision.fire_side

    # Per-side ledgers (authoritative under side-split).
    by_side = dict(realized_by_side or {})
    if not by_side and decision.fire_side in SIDE_KEYS:
        by_side[decision.fire_side] = paid
    elif not by_side and decision.fire_side == "both":
        # Split paid proportional to target upnl×close_frac by side.
        side_h = {"buy": 0.0, "sell": 0.0}
        for t in decision.targets:
            sk = t.book_side or ""
            if sk in side_h:
                side_h[sk] += float(t.upnl) * float(t.close_frac)
        total_h = sum(side_h.values()) or 1.0
        for sk, h in side_h.items():
            by_side[sk] = paid * (h / total_h)

    for sk, amt in by_side.items():
        if sk not in SIDE_KEYS:
            continue
        sst = _side_state(state, sk)
        prev = _f(sst.get("taxed_usd_in_epoch")) or 0.0
        sst["taxed_usd_in_epoch"] = float(prev) + max(0.0, float(amt))
        sst["taxed_peak_roe"] = max(
            float(_f(sst.get("taxed_peak_roe")) or 0.0),
            float(sst.get("peak_book_roe") or 0.0),
        )
        sst["last_fire_ts"] = float(now_ts)
        if decision.reason == "trail_giveback":
            reset_tax_epoch(state, reason="trail_giveback", side=sk)

    # Legacy combined ledger (telemetry / kill-switch OFF path).
    prev = _f(state.get("taxed_usd_in_epoch")) or 0.0
    state["taxed_usd_in_epoch"] = float(prev) + paid
    state["taxed_peak_roe"] = max(
        float(_f(state.get("taxed_peak_roe")) or 0.0),
        float(decision.peak_book_roe),
    )
    if decision.reason == "trail_giveback" and decision.fire_side == "combined":
        reset_tax_epoch(state, reason="trail_giveback")


# Back-compat aliases used by older call sites / pins.
def book_snapshot(open_brackets):
    snap = bg.build_graph(open_brackets)
    legs = []
    for L in snap.legs:
        if L.upnl is None:
            continue
        legs.append({
            **L.raw, "_upnl": L.upnl, "_risk": L.risk_usd,
            "_peak_usd": L.peak_usd, "_coh": L.coherence,
        })
    return legs, snap.basket.book_upnl, snap.basket.sum_risk, snap.basket.n


def ratchet_peak(state: dict, book_upnl: float) -> float:
    prev = _f(state.get("peak_book_usd")) or 0.0
    peak = max(prev, float(book_upnl) if book_upnl > 0 else 0.0)
    state["peak_book_usd"] = peak
    return peak


def ratchet_leg_peak_upnl(bracket: dict) -> float | None:
    upnl = _f(bracket.get("last_upnl"))
    if upnl is None:
        return None
    prev = _f(bracket.get("peak_upnl_usd")) or 0.0
    peak = max(prev, upnl)
    bracket["peak_upnl_usd"] = peak
    return peak
