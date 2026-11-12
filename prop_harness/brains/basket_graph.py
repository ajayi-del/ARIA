"""Basket peak-push graph — excellence spine for prop conquest.

Graph law (Dayo 2026-11-11): different nodes push the basket when a trade
peaks. Ranking is by highest ROE (per leg). Each new basket % peak forces
a full recalculation of tax/harvest obligations — adaptive, not one-shot.

Nodes
  LegNode     — one open bracket (roe = upnl/risk)
  BasketNode  — sleeve aggregator (book_roe = book_upnl/Σrisk, N∈[2,7])

Push events (edges into the controller)
  leg_peak_roe      — leg.roe made a new local peak
  basket_peak_frac  — basket book_roe made a new % peak

Invariants (O(legs) per mark tick when peaks update)
  I1  book_roe = book_upnl/Σrisk when Σrisk>0 else 0
  I2  peak_* are raise-only within an open epoch
  I3  pushes emit only on strict peak raises (idempotent no-ops otherwise)
  I4  dark upnl excludes a leg from book_upnl but still counts in N/Σrisk
  I5  payer = argmax leg.roe among legs with readable upnl>0

Pure — no I/O. MUST-NOT Kant/Nietzsche/Chancellor/tiers 1–6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    return v


@dataclass
class LegNode:
    semantic_id: str
    asset: str
    side: str
    risk_usd: float
    upnl: float | None
    roe: float | None          # upnl/risk; None if dark/unusable
    peak_roe: float            # raise-only local peak
    peak_usd: float            # mfe_peak_r×risk | peak_upnl | upnl
    coherence: float | None
    quantity: float | None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def green(self) -> bool:
        return self.upnl is not None and self.upnl > 0 and self.roe is not None


@dataclass
class BasketNode:
    n: int
    book_upnl: float
    sum_risk: float
    book_roe: float            # = book_frac = book_upnl/Σrisk
    peak_book_roe: float       # raise-only basket % peak
    peak_book_usd: float


@dataclass
class PushEvent:
    kind: str                  # leg_peak_roe | basket_peak_frac
    semantic_id: str = ""
    asset: str = ""
    value: float = 0.0
    prev: float = 0.0


@dataclass
class GraphSnapshot:
    legs: list[LegNode]
    basket: BasketNode
    pushes: list[PushEvent] = field(default_factory=list)
    payer: LegNode | None = None   # highest-ROE green leg

    @property
    def n(self) -> int:
        return self.basket.n


def leg_peak_usd(bracket: dict, *, upnl: float | None, risk: float) -> float:
    peak_r = _f(bracket.get("mfe_peak_r"))
    if peak_r is not None and peak_r > 0 and risk > 0:
        return float(peak_r) * float(risk)
    stamped = _f(bracket.get("peak_upnl_usd"))
    if stamped is not None and stamped > 0:
        return float(stamped)
    if upnl is not None and upnl > 0:
        return float(upnl)
    return 0.0


# Capital law — one scale for $5k→$500 eval and $25k→$2500 (10% hurdle).
REF_EQUITY_USD = 5000.0
REF_PROFIT_TARGET_USD = 500.0  # = 0.10 × REF_EQUITY


def capital_scale(
        equity: float | None = None,
        profit_target: float | None = None,
        *,
        ref_equity: float = REF_EQUITY_USD,
        ref_profit_target: float = REF_PROFIT_TARGET_USD,
) -> float:
    """Dimensionless capital scale. Prefer equity; else profit_target.

    $5k equity or $500 target → 1.0; $25k or $2500 → 5.0. Clamped
    [0.25, 20] so dust accounts don't vanish and whales don't explode.
    """
    scale = None
    if equity is not None and float(equity) > 0 and ref_equity > 0:
        scale = float(equity) / float(ref_equity)
    elif (profit_target is not None and float(profit_target) > 0
          and ref_profit_target > 0):
        scale = float(profit_target) / float(ref_profit_target)
    if scale is None:
        return 1.0
    return max(0.25, min(20.0, scale))


def adaptive_arm_frac(n: int, base: float = 0.25) -> float:
    """Scale arm fraction with N∈[2,7] — thicker books conquer earlier.

    N=2 → base+0.05 (more conviction on thin book)
    N=7 → base-0.10 (correlated mid-green fires sooner)
    Clamped to [0.12, 0.40]. Fraction of Σrisk — capital-invariant.
    """
    n = max(2, min(7, int(n)))
    # linear: n=2 → +0.05, n=7 → −0.10
    delta = 0.05 - (n - 2) * (0.15 / 5.0)
    return max(0.12, min(0.40, float(base) + delta))


def adaptive_min_book_usd(
        n: int, base: float = 10.0, *,
        equity: float | None = None,
        profit_target: float | None = None,
        scale: float | None = None,
) -> float:
    """Absolute $ book floor: N-step + capital scale.

    N=2 → base; N=7 → base+(N-2). Then × capital_scale so a $25k book
    needing $2500 profit arms at 5× the $5k/$500 eval floor.
    """
    n = max(2, min(7, int(n)))
    n_adj = float(base) + max(0.0, (n - 2) * 1.0)
    sc = float(scale) if scale is not None else capital_scale(
        equity, profit_target)
    return max(1.0, n_adj * sc)


def scale_usd(base: float, *, equity: float | None = None,
              profit_target: float | None = None,
              scale: float | None = None) -> float:
    """Scale an absolute-$ rung (sharpen/bank floors) by capital_scale."""
    sc = float(scale) if scale is not None else capital_scale(
        equity, profit_target)
    return max(0.0, float(base) * sc)


def build_graph(
        open_brackets: Iterable[dict] | None,
        *,
        prev_leg_peaks: dict[str, float] | None = None,
        prev_basket_peak_roe: float = 0.0,
        prev_basket_peak_usd: float = 0.0,
) -> GraphSnapshot:
    """Build nodes + detect peak-push edges. O(legs).

    prev_* carry durable raise-only peaks across ticks (controller state).
    """
    prev_leg = dict(prev_leg_peaks or {})
    legs: list[LegNode] = []
    book = 0.0
    risk_sum = 0.0
    n = 0
    pushes: list[PushEvent] = []

    for b in open_brackets or []:
        if b.get("status") and b.get("status") != "open":
            continue
        n += 1
        sid = str(b.get("semantic_id") or "")
        asset = str(b.get("asset") or "")
        risk = _f(b.get("risk_usd")) or 0.0
        if risk > 0:
            risk_sum += risk
        upnl = _f(b.get("last_upnl"))
        # Ratchet peak_upnl_usd on the live bracket dict (caller persists).
        if upnl is not None:
            prev_u = _f(b.get("peak_upnl_usd")) or 0.0
            if upnl > prev_u:
                b["peak_upnl_usd"] = upnl
        roe = None
        if upnl is not None and risk > 0:
            roe = upnl / risk
            book += upnl
        peak_roe_prev = float(prev_leg.get(sid, 0.0) or 0.0)
        peak_roe = peak_roe_prev
        if roe is not None and roe > peak_roe:
            pushes.append(PushEvent(
                kind="leg_peak_roe", semantic_id=sid, asset=asset,
                value=roe, prev=peak_roe_prev))
            peak_roe = roe
        qty = _f(b.get("quantity"))
        coh = _f(b.get("coherence_at_entry", b.get("coherence")))
        legs.append(LegNode(
            semantic_id=sid, asset=asset,
            side=str(b.get("side") or ""),
            risk_usd=risk, upnl=upnl, roe=roe,
            peak_roe=peak_roe,
            peak_usd=leg_peak_usd(b, upnl=upnl, risk=risk),
            coherence=coh, quantity=qty, raw=b,
        ))

    book_roe = (book / risk_sum) if risk_sum > 0 else 0.0
    peak_book_roe = max(float(prev_basket_peak_roe or 0.0),
                        book_roe if book_roe > 0 else 0.0)
    peak_book_usd = max(float(prev_basket_peak_usd or 0.0),
                        book if book > 0 else 0.0)
    if book_roe > float(prev_basket_peak_roe or 0.0) + 1e-12:
        pushes.append(PushEvent(
            kind="basket_peak_frac", value=book_roe,
            prev=float(prev_basket_peak_roe or 0.0)))

    greens = [L for L in legs if L.green]
    payer = max(greens, key=lambda L: float(L.roe or 0.0)) if greens else None

    return GraphSnapshot(
        legs=legs,
        basket=BasketNode(
            n=n, book_upnl=book, sum_risk=risk_sum, book_roe=book_roe,
            peak_book_roe=peak_book_roe, peak_book_usd=peak_book_usd,
        ),
        pushes=pushes,
        payer=payer,
    )


def leg_peak_map(snapshot: GraphSnapshot) -> dict[str, float]:
    """Durable per-leg peak_roe map for controller state."""
    return {L.semantic_id: L.peak_roe for L in snapshot.legs if L.semantic_id}
