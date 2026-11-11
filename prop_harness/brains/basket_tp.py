"""Basket conquest controller — peak-push state machine.

Consumes basket_graph.GraphSnapshot pushes. On every new basket % peak
(or trail giveback after a cleared peak): full recalculation of
  (a) highest-ROE payer
  (b) tax-bank size (= max(ratio×book, payer peak $))
  (c) residual-pyramid eligibility (thesis + high coh)
  (d) cooloff / peak-epoch idempotence (no double-tax of same peak)

State machine
  IDLE ──border/peak──► ARMED ──actuate──► COOLOFF ──ttl──► IDLE
           │                ▲
           │                │ basket_peak_frac push
           └──── RECALC ────┘  (full tax recompute; fire only if unpaid)

Arm uses adaptive thresholds from basket_graph (N∈[2,7]). Tax/residual
do not change the arm border — they size and shape the harvest once
armed. Every evaluation sleeve shares this controller.

Pure logic — actuation (closes) lives in executor. Kill OFF → empty.
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
    """Tax ≥ ratio×book; with tax-bank ON also ≥ highest payer/leg peak $."""
    ratio_tax = max(0.0, float(harvest_ratio) * float(book_upnl))
    max_leg = 0.0
    for L in legs:
        if L.peak_usd > max_leg:
            max_leg = L.peak_usd
    if payer is not None and payer.peak_usd > max_leg:
        max_leg = payer.peak_usd
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
        ))
        harvested += credited
        any_residual = any_residual or leave
    return picked, harvested, any_residual


def individual_banked_usd(open_brackets: list | None) -> float:
    """Σ realized_booked on open legs — individual TP fills already paid."""
    total = 0.0
    for b in open_brackets or []:
        if b.get("status") and b.get("status") != "open":
            continue
        rb = _f(b.get("realized_booked")) or 0.0
        if rb > 0:
            total += rb
    return total


def unpaid_tax(
        tax_usd: float, state: dict, *,
        individual_banked: float = 0.0,
) -> float:
    """Idempotence: tax not yet paid by basket epoch OR individual TPs.

    paid = max(taxed_usd_in_epoch, individual_banked) — avoids double-count
    when venue sync later stamps the same dollars into realized_booked.
    """
    epoch = _f(state.get("taxed_usd_in_epoch")) or 0.0
    paid = max(float(epoch), max(0.0, float(individual_banked)))
    return max(0.0, float(tax_usd) - paid)


def handover_after_basket(
        bracket: dict, *,
        close_frac: float,
        leave_residual: bool,
        estimated_realized: float | None = None,
) -> dict:
    """Pure registry mutations after a basket close (caller persists).

    Axioms:
      H1  Full close → status closed_external (zombie prevention).
      H2  Residual → qty×(1-close_frac), basket_residual=True, ladder_reset
          (individual TPs cancelled at actuation; residual = stop/BE/trail).
      H3  risk_released advances by close_frac × birth risk.
      H4  Never invents a stop — actuation must resize_protection after.
    """
    out = {
        "semantic_id": bracket.get("semantic_id"),
        "asset": bracket.get("asset"),
        "leave_residual": bool(leave_residual),
        "close_frac": float(close_frac),
        "needs_stop_resize": False,
        "closed": False,
    }
    risk = _f(bracket.get("risk_usd")) or 0.0
    already = _f(bracket.get("risk_released")) or 0.0
    frac = min(1.0, max(0.0, float(close_frac)))
    if not leave_residual or frac >= 1.0 - 1e-12:
        bracket["status"] = "closed_external"
        bracket["basket_harvested"] = True
        bracket["ladder_reset"] = True
        release = max(0.0, risk - already)
        if release > 0:
            bracket["risk_released"] = already + release
        out["closed"] = True
        out["risk_released_delta"] = release
        return out

    # Residual runner — quantity shrink + risk release; stop re-arm required.
    try:
        from decimal import Decimal
        full = Decimal(str(bracket.get("quantity") or "0"))
        left = full * (Decimal("1") - Decimal(str(frac)))
        if left <= 0:
            bracket["status"] = "closed_external"
            bracket["basket_harvested"] = True
            out["closed"] = True
            return out
        bracket["quantity"] = format(left, "f")
    except Exception:
        pass
    target_released = risk * frac
    release = max(0.0, target_released - already)
    if release > 0:
        bracket["risk_released"] = already + release
    if estimated_realized is not None:
        prev = _f(bracket.get("realized_booked")) or 0.0
        # Raise-only estimate; venue sync may replace with truth.
        if float(estimated_realized) > prev:
            bracket["realized_booked"] = float(estimated_realized)
    bracket["basket_residual"] = True
    bracket["basket_harvested"] = True
    bracket["ladder_reset"] = True  # birth TP ladder cancelled — reset
    bracket["owns_tp"] = False       # residual = stop/BE/trail/sharpen only
    out["needs_stop_resize"] = True
    out["risk_released_delta"] = release
    out["residual_qty"] = bracket.get("quantity")
    return out


def handover_after_individual_tp(bracket: dict) -> dict:
    """Tag that an individual TP fill credited the basket tax ledger.

    sync_bracket_size already resized stop + risk_released + realized_booked.
    This only stamps provenance so the controller's individual_banked
    credit is attributable (no double harvest of the same dollars).
    """
    rb = _f(bracket.get("realized_booked")) or 0.0
    bracket["individual_tp_credited"] = True
    return {
        "semantic_id": bracket.get("semantic_id"),
        "asset": bracket.get("asset"),
        "realized_booked": rb,
        "source": "individual_tp",
    }


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
) -> BasketDecision:
    """Controller tick: build graph → absorb pushes → recalc → maybe fire."""
    d = BasketDecision()
    if not enabled or profile != "evaluation":
        d.reason = "disabled"
        return d

    st = state if isinstance(state, dict) else {}
    prev_leg_peaks = st.get("leg_peak_roe") or {}
    if not isinstance(prev_leg_peaks, dict):
        prev_leg_peaks = {}

    snap = bg.build_graph(
        open_brackets,
        prev_leg_peaks=prev_leg_peaks,
        prev_basket_peak_roe=float(_f(st.get("peak_book_roe")) or 0.0),
        prev_basket_peak_usd=float(_f(st.get("peak_book_usd")) or 0.0),
    )
    # Persist raise-only peaks into durable state (caller writes file).
    st["leg_peak_roe"] = bg.leg_peak_map(snap)
    st["peak_book_roe"] = snap.basket.peak_book_roe
    st["peak_book_usd"] = snap.basket.peak_book_usd

    b = snap.basket
    d.n = b.n
    d.book_upnl = round(b.book_upnl, 4)
    d.sum_risk = round(b.sum_risk, 4)
    d.book_frac = round(b.book_roe, 4)
    d.peak_book_usd = round(b.peak_book_usd, 4)
    d.peak_book_roe = round(b.peak_book_roe, 4)
    d.pushes = [p.kind for p in snap.pushes]
    if snap.payer is not None:
        d.payer_asset = snap.payer.asset
        d.payer_roe = round(float(snap.payer.roe or 0.0), 4)

    # Adaptive thresholds: N∈[2,7] fractions + capital scale on $ floors.
    arm = float(arm_frac)
    min_book = float(min_book_usd)
    sc = 1.0
    if capital_scale_enabled:
        sc = bg.capital_scale(equity, profit_target)
    if adaptive_thresholds:
        arm = bg.adaptive_arm_frac(b.n, base=arm_frac)
        min_book = bg.adaptive_min_book_usd(
            b.n, base=min_book_usd, scale=sc if capital_scale_enabled else 1.0)
    elif capital_scale_enabled:
        min_book = bg.scale_usd(min_book, scale=sc)
    d.adaptive_arm_frac = round(arm, 4)
    d.adaptive_min_book = round(min_book, 4)
    d.telemetry["capital_scale"] = round(sc, 4)

    if b.n < int(min_n):
        d.reason = "min_n"
        return d
    if b.sum_risk <= 0:
        d.reason = "no_risk"
        return d
    if snap.payer is None:
        d.reason = "no_green"
        return d

    now = float(now_ts) if now_ts is not None else 0.0
    last_fire = _f(st.get("last_fire_ts")) or 0.0
    if now > 0 and last_fire > 0 and (now - last_fire) < float(cooloff_s):
        d.reason = "cooloff"
        d.telemetry["cooloff_remaining_s"] = round(
            float(cooloff_s) - (now - last_fire), 1)
        return d

    at_border = (
        b.n >= int(min_n)
        and b.book_upnl >= min_book
        and b.book_roe >= arm
    )
    peak_border = (
        b.peak_book_usd >= min_book
        and b.peak_book_roe >= arm
    )
    trail_fire = (
        peak_border
        and b.book_upnl > 0
        and b.book_roe <= float(giveback_frac) * b.peak_book_roe
        and b.peak_book_roe > b.book_roe
    )
    new_basket_peak = any(p.kind == "basket_peak_frac" for p in snap.pushes)

    if at_border and new_basket_peak:
        reason = "basket_peak"
    elif at_border:
        reason = "border"
    elif trail_fire:
        reason = "trail_giveback"
    else:
        d.reason = "below_border"
        d.telemetry.update({
            "min_book_usd": min_book, "arm_frac": arm,
            "at_border": False, "peak_border": peak_border,
            "new_basket_peak": new_basket_peak,
        })
        return d

    # Full recalculation on every qualifying tick (peak push or trail).
    tax, max_leg = tax_bank_usd(
        b.book_upnl, snap.payer, snap.legs,
        harvest_ratio=harvest_ratio,
        tax_bank_enabled=tax_bank_enabled,
    )
    # Scale tax dollar floors implicitly via book (larger accounts → larger
    # notionals). Absolute max_leg peak already in $. Credit individual TPs.
    ind_banked = individual_banked_usd(open_brackets)
    due = unpaid_tax(tax, st, individual_banked=ind_banked)
    d.tax_usd = round(tax, 4)
    d.max_leg_peak_usd = round(max_leg, 4)
    d.peak_epoch = round(b.peak_book_roe, 6)

    # Peak-epoch idempotence: same peak already fully taxed → no re-fire
    # unless trail giveback (realize before giveback completes).
    taxed_epoch = _f(st.get("taxed_peak_roe")) or 0.0
    if (reason != "trail_giveback"
            and due <= 1e-6
            and taxed_epoch + 1e-12 >= b.peak_book_roe):
        d.reason = "peak_epoch_taxed"
        d.telemetry["taxed_peak_roe"] = taxed_epoch
        d.telemetry["taxed_usd_in_epoch"] = st.get("taxed_usd_in_epoch")
        d.telemetry["individual_banked"] = round(ind_banked, 4)
        return d
    if due <= 1e-6 and ind_banked + 1e-9 >= tax and reason != "trail_giveback":
        d.reason = "individual_tp_paid"
        d.telemetry["individual_banked"] = round(ind_banked, 4)
        d.telemetry["tax_usd"] = round(tax, 4)
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
    d.telemetry.update({
        "min_n": min_n,
        "min_book_usd": min_book,
        "arm_frac": arm,
        "base_arm_frac": arm_frac,
        "base_min_book_usd": min_book_usd,
        "harvest_ratio": harvest_ratio,
        "giveback_frac": giveback_frac,
        "tax_bank_enabled": bool(tax_bank_enabled),
        "residual_pyramid_enabled": bool(residual_pyramid_enabled),
        "residual_frac": residual_frac,
        "residual_min_coh": residual_min_coh,
        "adaptive_thresholds": bool(adaptive_thresholds),
        "capital_scale_enabled": bool(capital_scale_enabled),
        "capital_scale": round(sc, 4),
        "equity": equity,
        "profit_target": profit_target,
        "n_targets": len(targets),
        "n_residual": sum(1 for t in targets if t.leave_residual),
        "unpaid_tax": round(due, 4),
        "individual_banked": round(ind_banked, 4),
        "pushes": d.pushes,
        "payer": d.payer_asset,
        "payer_roe": d.payer_roe,
    })
    return d


def mark_fired(state: dict, now_ts: float, decision: BasketDecision) -> None:
    """Stamp after ≥1 successful close. Advances peak-epoch tax ledger."""
    state["last_fire_ts"] = float(now_ts)
    state["last_fire_reason"] = decision.reason
    state["last_harvest_usd"] = float(decision.harvest_usd)
    state["last_tax_usd"] = float(decision.tax_usd)
    state["last_residual"] = bool(decision.residual_allowed)
    state["last_payer"] = decision.payer_asset
    # Epoch ledger: accumulate taxed $; remember peak_roe taxed through.
    prev = _f(state.get("taxed_usd_in_epoch")) or 0.0
    state["taxed_usd_in_epoch"] = float(prev) + float(decision.harvest_usd)
    state["taxed_peak_roe"] = max(
        float(_f(state.get("taxed_peak_roe")) or 0.0),
        float(decision.peak_book_roe),
    )
    if decision.reason == "trail_giveback":
        # Trail realizes the remnant — clear epoch for a fresh conquest.
        state["peak_book_roe"] = 0.0
        state["peak_book_usd"] = 0.0
        state["taxed_usd_in_epoch"] = 0.0
        state["taxed_peak_roe"] = 0.0
        state["leg_peak_roe"] = {}


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
