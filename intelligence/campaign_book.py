"""
intelligence/campaign_book.py — Campaign Book brain (2026-09-23)

Governor directive: campaign mode re-enabled for SPCX + ETH, XRP, AMD, UNI —
"these coins fire more trades with less coherence", hedges + momentum scalps +
self-portfolio building live from today (shadow-scored from birth).

Department shape (docs/DEPARTMENT_TEMPLATE.md): zero-I/O brain. All exchange
reads arrive as injected callables/arguments; decisions leave as verdict
dataclasses. main.py owns the I/O. VSM: S1 (campaign sleeve operations) with
S2 coordination (reserve ledger, daily counter, per-symbol throttle).

Doctrine (Taleb barbell, Kahneman disposition, Aronson bounded DoF):
  - The campaign book is the convex 10-15% of the barbell: fixed premium
    (stop + fees), unbounded runner right tail via pyramid → treasury handoff.
  - Margin is budgeted, never implied: per-position margin = 12% sleeve ×
    conviction ladder, stop-risk clamped, capped per-symbol and per-book.
  - Family hedges are funded ONLY by the primary's own stop geometry
    (2026-09-20 fork doctrine) drawn against a ring-fenced reserve — never
    by principal. Reserve exhausted = fail-closed standdown; the primary
    keeps its own stop.
  - base_rate_veto binds campaign entries structurally (no exemption exists
    in the veto span; the 2026-09-02 SPCX bleed was skeptic n<10 abstention,
    bounded here by the shared daily entry counter while evidence accrues).

Kill switches: every config False state reproduces the pre-2026-09-23
single-SPCX (or campaign-off) system bit-for-bit.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple


# ── Membership ────────────────────────────────────────────────────────────────

def campaign_members(cfg) -> frozenset:
    """The campaign membership set.

    Legacy: campaign_mode_enabled + campaign_symbol (single string, SPCX).
    Multi:  campaign_symbols non-empty AND campaign_multi_symbol_enabled →
    the set. Master gate campaign_mode_enabled=False → empty set (campaign
    off — every _is_campaign_sym branch False, heartbeat silent).
    """
    if not getattr(cfg, "campaign_mode_enabled", False):
        return frozenset()
    if bool(getattr(cfg, "campaign_multi_symbol_enabled", True)):
        syms = list(getattr(cfg, "campaign_symbols", []) or [])
        if syms:
            return frozenset(syms)
    return frozenset({getattr(cfg, "campaign_symbol", "SPCX-USD")})


def is_campaign(cfg, symbol: str) -> bool:
    return symbol in campaign_members(cfg)


# ── Venue-aware floors ────────────────────────────────────────────────────────

def campaign_min_notional(cfg, symbol: str, venue: str) -> float:
    """campaign_min_notional_usd (250) is the SoDEX floor — on Aster's $1-min
    venue a $250 floor on a ~$120 sleeve is a size inversion (terminal floor
    would resize a UNI trade UP past the sleeve's sane margin). Aster floor
    defaults $3 (3 bracket legs × $1)."""
    if venue == "aster":
        return float(getattr(cfg, "campaign_venue_min_notional_aster", 3.0))
    return float(getattr(cfg, "campaign_min_notional_usd", 250.0))


# ── Margin engineering ────────────────────────────────────────────────────────

def conviction_ladder(coherence: float) -> float:
    """Same bands as the campaign conviction floor (1.0/0.75/0.5 at 4.5/3.0)."""
    if coherence >= 4.5:
        return 1.0
    if coherence >= 3.0:
        return 0.75
    return 0.5


@dataclass
class MarginBudget:
    margin: float                 # final per-position margin budget (USD)
    notional: float               # margin × leverage
    leverage: int
    clamp_reasons: Tuple[str, ...] = ()
    standdown: bool = False       # True → no trade (affordability/caps)
    standdown_reason: str = ""


def campaign_margin_budget(
    cfg,
    *,
    coherence: float,
    sleeve_equity: float,
    combined_equity: float,
    stop_dist_pct: Optional[float],
    open_campaign_margin: float,
    symbol_open_margin: float,
    min_notional: float,
) -> MarginBudget:
    """Per-position campaign margin budget (pure).

    Ladder:  frac_base × sleeve × conviction_ladder(coh)
    Clamps (innermost wins):
      1. stop-risk: 6% of sleeve ÷ stop_dist (unknown stop reads as 100%
         distance → max clamp, fail-closed — SoDEX doctrine idiom)
      2. symbol cap: 15% of sleeve minus symbol's open campaign margin
      3. book cap:  50% of combined sleeves minus ALL open campaign margin
    Leverage ≤ 8 (rule 11). Standdown when the surviving budget cannot
    clear the venue min notional — the affordability arbiter (replaces a
    recovery standdown knob: recovery's 0.5× cap shrinks the budget through
    the caller's sleeve input; unaffordable reads as standdown here).
    """
    if not getattr(cfg, "campaign_margin_budget_enabled", True):
        return MarginBudget(margin=0.0, notional=0.0, leverage=0,
                            clamp_reasons=("budget_disabled",), standdown=True,
                            standdown_reason="budget_disabled")

    lev = min(int(getattr(cfg, "campaign_leverage", 8)), 8)
    frac = float(getattr(cfg, "campaign_margin_frac_base", 0.12))
    reasons = []

    budget = frac * sleeve_equity * conviction_ladder(coherence)

    # 1. stop-risk clamp (6%-of-sleeve doctrine, raise-only unknown).
    # Binds MARGIN: risk = margin × lev × stop_dist ≤ 6% × sleeve
    # (SoDEX doctrine sibling clamps notional = sleeve×6%/stop_frac — same
    # bound divided through by lev).
    sd = stop_dist_pct if (stop_dist_pct and stop_dist_pct > 0) else 1.0
    risk_clamp = ((float(getattr(cfg, "campaign_stop_risk_clamp_pct", 0.06))
                   * sleeve_equity) / max(sd, 1e-9)) / max(lev, 1)
    if risk_clamp < budget:
        budget = risk_clamp
        reasons.append("stop_risk_clamped")

    # 2. symbol cap
    sym_cap = (float(getattr(cfg, "campaign_symbol_margin_cap", 0.15))
               * sleeve_equity) - max(0.0, symbol_open_margin)
    if sym_cap < budget:
        budget = sym_cap
        reasons.append("symbol_cap_clamped")

    # 3. book cap
    book_cap = (float(getattr(cfg, "campaign_book_margin_cap", 0.50))
                * combined_equity) - max(0.0, open_campaign_margin)
    if book_cap < budget:
        budget = book_cap
        reasons.append("book_cap_clamped")

    budget = max(0.0, budget)
    notional = budget * lev
    if notional < min_notional:
        reason = ("cap_exhausted" if budget <= 0.0 and reasons
                  else "below_venue_min_notional")
        return MarginBudget(margin=budget, notional=notional, leverage=lev,
                            clamp_reasons=tuple(reasons), standdown=True,
                            standdown_reason=reason)
    return MarginBudget(margin=budget, notional=notional, leverage=lev,
                        clamp_reasons=tuple(reasons))


# ── Daily entry counter (shared, inside Kant's 70/day) ───────────────────────

class CampaignDailyCounter:
    """UTC-day-keyed shared campaign entry counter. In-memory; restart resets
    (same idiom as _daily_global in kant_gate — the restart catch-up salvo
    is designed)."""

    def __init__(self) -> None:
        self._day: str = ""
        self._n: int = 0

    @staticmethod
    def _today(now: Optional[float] = None) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(now or time.time()))

    def count(self, now: Optional[float] = None) -> int:
        day = self._today(now)
        if day != self._day:
            return 0
        return self._n

    def increment(self, now: Optional[float] = None) -> int:
        day = self._today(now)
        if day != self._day:
            self._day, self._n = day, 0
        self._n += 1
        return self._n

    def allowed(self, cfg, now: Optional[float] = None) -> bool:
        return self.count(now) < int(getattr(cfg, "campaign_daily_entries_max", 20))


# ── Hedge reserve ledger ─────────────────────────────────────────────────────

class HedgeReserve:
    """Ring-fenced slice of the campaign pool funding family hedges.

    reserve_target = campaign_hedge_reserve_frac × book pool
                     (book pool = campaign_book_margin_cap × combined equity)
    Debits at arm, credits at harvest/standdown. In-memory; rebuilt at boot
    from open hedge registry entries (main.py splice).
    """

    def __init__(self) -> None:
        self._debited: float = 0.0

    def target(self, cfg, combined_equity: float) -> float:
        pool = (float(getattr(cfg, "campaign_book_margin_cap", 0.50))
                * combined_equity)
        return float(getattr(cfg, "campaign_hedge_reserve_frac", 0.20)) * pool

    def available(self, cfg, combined_equity: float) -> float:
        return max(0.0, self.target(cfg, combined_equity) - self._debited)

    def debit(self, amount: float) -> None:
        self._debited += max(0.0, amount)

    def credit(self, amount: float) -> None:
        self._debited = max(0.0, self._debited - max(0.0, amount))

    def rebuild(self, open_hedge_margins: float) -> None:
        self._debited = max(0.0, open_hedge_margins)

    @property
    def debited(self) -> float:
        return self._debited


# ── Family hedge verdicts ────────────────────────────────────────────────────

DEFAULT_FAMILY_HEDGE_MAP: Dict[str, str] = {
    "SPCX-USD": "USTECH100-USD",   # equity_index → tech index
    "AMD-USD": "USTECH100-USD",    # equity_tech → tech index
    "ETH-USD": "BTC-USD",          # crypto_majors → BTC
    "XRP-USD": "BTC-USD",
    "UNI-USD": "ETH-USD",          # defi_infra → ETH
}


def family_hedge_instrument(cfg, symbol: str) -> Optional[str]:
    m = dict(getattr(cfg, "campaign_family_hedge_map", None)
             or DEFAULT_FAMILY_HEDGE_MAP)
    return m.get(symbol)


@dataclass
class HedgeVerdict:
    action: str                   # "arm" | "standdown" | "hold"
    mode: str = ""                # "green" | "red"
    hedge_symbol: str = ""
    hedge_side: str = ""          # opposite of primary
    budget_usd: float = 0.0
    stop_frac: float = 0.0
    notional: float = 0.0
    leverage: int = 15
    reason: str = ""


def family_hedge_verdict(
    cfg,
    *,
    symbol: str,
    primary_side: str,
    entry_price: float,
    mark_price: float,
    stop_price: float,
    tp1_price: float,
    primary_qty: float,
    atr15: Optional[float],
    open_profit_frac: float,      # signed ROE fraction of primary (mark vs entry)
    reserve_available: float,
    harvests_done: int,
    last_harvest_ts: float,
    now: Optional[float] = None,
) -> HedgeVerdict:
    """Family-hedge arm verdict — the 2026-09-20 fork budget doctrine applied
    to FAMILY instruments on the primary's own venue (the Bybit hedge sleeve
    is Governor-eliminated 2026-09-21; never resurrected here).

    GREEN (profit-lock): primary approaching TP1 (≥0.9 of entry→TP1 distance)
      → budget = locked floor max(0, side-signed stop−entry) × qty
        + 0.7 × open profit (open profit = open_profit_frac × entry × qty).
    RED (pain-harvest): primary bleeding ≥ pain_frac 0.5 of designed risk
      → budget = 0.7 × designed risk (entry↔stop distance × qty).
    All budgets in USD. Notional = budget ÷ stop_frac (one full stop-out
    spends the budget — violent tapes self-deleverage). Leverage ≤ 15
    (locked hedge cap, supersedes rule 11 for hedge legs per 2026-09-20
    doctrine).
    """
    now = now or time.time()
    if not getattr(cfg, "campaign_family_hedge_enabled", False):
        return HedgeVerdict(action="standdown", reason="family_hedge_disabled")
    hsym = family_hedge_instrument(cfg, symbol)
    if not hsym:
        return HedgeVerdict(action="standdown", reason="no_family_instrument")
    if harvests_done >= int(getattr(cfg, "campaign_hedge_max_harvests", 4)):
        return HedgeVerdict(action="standdown", reason="harvest_cap")
    cooloff = float(getattr(cfg, "campaign_hedge_cooloff_s", 900.0))
    if now - last_harvest_ts < cooloff:
        return HedgeVerdict(action="hold", reason="cooloff")

    if entry_price <= 0 or stop_price <= 0 or mark_price <= 0:
        return HedgeVerdict(action="standdown", reason="bad_geometry")
    designed_risk_frac = abs(entry_price - stop_price) / entry_price
    if designed_risk_frac <= 0:
        return HedgeVerdict(action="standdown", reason="no_stop")

    hside = "short" if primary_side == "long" else "long"

    # GREEN arm: mark ≥ 0.9 of the way entry→TP1 (side-relative)
    green = False
    if tp1_price and tp1_price > 0:
        if primary_side == "long" and tp1_price > entry_price:
            green = (mark_price - entry_price) >= 0.9 * (tp1_price - entry_price)
        elif primary_side == "short" and tp1_price < entry_price:
            green = (entry_price - mark_price) >= 0.9 * (entry_price - tp1_price)

    # RED arm: bleeding ≥ 0.5 of designed risk
    pain_frac = float(getattr(cfg, "campaign_hedge_pain_frac", 0.5))
    red = open_profit_frac <= -pain_frac * designed_risk_frac

    if green:
        # Locked floor: by the pyramid doctrine the primary's stop is at/through
        # entry at TP1-approach — the floor is the dominant budget term.
        _side_sign = 1.0 if primary_side == "long" else -1.0
        locked_frac = max(0.0, _side_sign * (stop_price - entry_price) / entry_price)
        budget_frac = locked_frac + 0.7 * max(0.0, open_profit_frac)
        mode = "green"
    elif red:
        budget_frac = 0.7 * designed_risk_frac
        mode = "red"
    else:
        return HedgeVerdict(action="hold", reason="no_trigger")

    stop_frac = max(float(getattr(cfg, "hedge_budget_stop_pct", 0.02)),
                    (atr15 or 0.0) / mark_price if mark_price > 0 else 0.0)
    if stop_frac <= 0:
        return HedgeVerdict(action="standdown", reason="no_stop_frac")

    # Real-USD sizing (qty-aware): one full stop-out spends the budget;
    # the ring-fenced reserve bounds the margin claim directly.
    qty = max(0.0, primary_qty)
    budget_usd = budget_frac * entry_price * qty
    notional_usd = budget_usd / stop_frac
    margin_claim = notional_usd / 15.0
    if margin_claim > reserve_available:
        return HedgeVerdict(action="standdown", mode=mode,
                            reason="reserve_exhausted")

    return HedgeVerdict(action="arm", mode=mode, hedge_symbol=hsym,
                        hedge_side=hside, budget_usd=budget_usd,
                        stop_frac=stop_frac, notional=notional_usd,
                        leverage=15)


# ── Self-portfolio handoff verdict ───────────────────────────────────────────

def self_portfolio_handoff(cfg, *, symbol: str, pyramid_layers_done: int,
                           pyramid_max_layers: int, tp2_hit: bool) -> Tuple[bool, str]:
    """Campaign scalp → PYRAMIDED (existing CampaignPyramidEngine) → runner
    (existing treasury). The handoff fires when the staircase is complete OR
    TP2 banked: the treasury claims the residual as a runner (75% banked,
    25% trailing 50%-of-peak). Add gate upstream: MFE ≥ capture mult × ATR15
    AND breakeven proof (existing _pyramid_breakeven_proof idiom, evaluated
    by the caller)."""
    if not getattr(cfg, "campaign_self_portfolio_enabled", False):
        return False, "self_portfolio_disabled"
    if pyramid_layers_done >= pyramid_max_layers - 1:
        return True, "staircase_complete"
    if tp2_hit:
        return True, "tp2_banked"
    return False, "building"
