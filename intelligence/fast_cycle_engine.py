"""
intelligence/fast_cycle_engine.py — Fast-Cycle campaign brain (2026-09-26)

Governor doctrine (locked 2026-09-26): a self-funding "fast-cycle" campaign
trades an isolated $40 margin pool on SoDEX for SoPoints volume farming.
Per trade: $8 margin, isolated margin mode, leverage 15-38x scaled by cage
quality and clamped to the per-symbol exchange tier, maker-only
resting-limit entries (placement is the caller's job — this brain emits
order SPECS only). Cage ratio = take-profit distance / stop distance, floor
3.0 (break-even win rate 25% at 3:1, ~27% after fees). At most 6 concurrent
positions; the $40 pool at $8/trade binds first at 5. Stop-out → immediate
re-entry is DESIGNED: each stop-out is a volume event and the pool recycles.

Fee governor ("the campaign pays for the fees — pure maths"): SoDEX tier-0
with the 5% SOSO staking discount — maker 0.0114%, taker 0.038% of notional
per side. Net cost = cumulative gross fees − cumulative campaign realized
PnL must stay < $7.00 per $100,000 of cumulative filled volume (breach is
STRICTLY greater; exactly $7.00 is OK). On breach the engine STANDS DOWN
new entries (reason=fee_budget_breach) until the ratio heals. Exits are
never gated. Volume counts both sides of the round trip (entry + exit
fills), accumulated at close.

Department shape (docs/DEPARTMENT_TEMPLATE.md): zero-I/O brain. Time,
config, and open positions are injected; the engine holds only the pool
ledger and the cumulative fee/pnl/volume counters. Pool accounting mirrors
the HedgeReserve pattern (intelligence/campaign_book.py:198-229): debit on
entry, credit on close, rebuild(open_margins) at boot. Fail-CLOSED on
geometry, fail-OPEN nowhere in this module.

Kill switch: fast_cycle_enabled=False → every verdict is a standdown
(reason=disabled) and NO state mutates (the pre-module system bit-for-bit).

Telemetry (emitted by the coordinator, never by this module):
  fast_cycle_entry_approved  — verdict approve (symbol, side, margin_usd,
      leverage, notional_usd, cage_ratio, est_roundtrip_fee_usd)
  fast_cycle_entry_standdown — verdict standdown (symbol, side, reason)
  fast_cycle_fee_gauge       — periodic fee_gauge() snapshot
      (cumulative_volume_usd, cumulative_fees_usd,
       cumulative_realized_pnl_usd, net_cost_per_100k, budget_ok)

Config knobs (injected cfg via getattr; coordinator adds to core/config.py):
  fast_cycle_enabled=False
  fast_cycle_pool_usd=40.0
  fast_cycle_margin_per_trade=8.0
  fast_cycle_max_concurrent=6
  fast_cycle_cage_min=3.0
  fast_cycle_fee_budget_per_100k=7.00
  fast_cycle_maker_fee_rate=0.000114
  fast_cycle_taker_fee_rate=0.00038
  fast_cycle_stop_fee_floor_mult=3.0   — stop_dist/entry >= mult x
      (maker+taker) or the fee leg dominates EV (reason=fee_floor)
  fast_cycle_taker_exit_frac=0.75      — exit-side taker share for the
      honest fee blend (SoDEX stops fire taker on trigger)
  fast_cycle_fee_min_volume_usd=1000.0 — budget gate abstains below this
      cumulative volume (tiny-denominator standdown ratchet guard)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


# ── Per-symbol exchange-tier leverage caps (hard ceiling) ───────────────────
# Symbols not in this table are ineligible: the engine does not run on them.
# SoDEX margin tiers (docs 2026-09-26): BTC 40x; ETH/XAUT/USTECH100 25x;
# SOL/SILVER/US500/CL/COPPER 20x; BCH/DOGE/NEAR/XRP/ZEC + mid-caps 10x.
# The campaign class caps BELOW the exchange tier (15-38x by cage) — these
# entries are the exchange-tier clamp. ETH doc said 25 but the Governor
# confirmed 2026-09-28 ("eth has 40x available on sodex its a new
# development" — docs changed): table now carries 40.
LEVERAGE_CAPS: Dict[str, int] = {
    "BTC": 38,
    "ETH": 40,
    "XAUT": 25,
    "USTECH100": 25,
    "SOL": 20,
    "SILVER": 20,
    "US500": 20,
    "CL": 20,
    "COPPER": 20,
    # 10x mid-cap tier (cross-review P1: XRP/DOGE/LINK/ARB were silently
    # ineligible though campaign-intended). ARB clamped 10→5 on 2026-09-26:
    # live probe (tools/sodex_leverage_fee_probe.py) — venue max is 5x.
    "XRP": 10,
    "DOGE": 10,
    "LINK": 10,
    "ARB": 5,
    "BCH": 10,
    "NEAR": 10,
    "ZEC": 10,
    # Governor 2026-09-28 volume campaign ladder adds TRX:50. Live venue
    # probe 2026-09-28 (GET /perps/markets/symbols, public): TRX-USD id 35,
    # maxLeverage 5, tick 0.00001, step 1 — the assumed 10x tier produced a
    # leverage_set_failed standdown loop every anticipator tick (target 10
    # actual 5), leaving the Governor's TRX row inert.
    "TRX": 5,
    # 2026-09-30 Governor add. Live venue probe (GET /perps/markets/symbols):
    # QNT-USD maxLeverage 10, tick 0.01, step 0.01.
    "QNT": 10,
}

POOL_NAME = "fast_cycle"


def _normalize_symbol(symbol: str) -> str:
    """BTC-USD / btc / BTC-USD.P → BTC (venue suffixes stripped)."""
    return str(symbol).split("-")[0].split(".")[0].upper()


# ── Pure helpers ─────────────────────────────────────────────────────────────

def cage_ratio(entry: float, stop: float, tp: float) -> float:
    """|tp − entry| / |stop − entry|. Raises ZeroDivisionError when the stop
    sits exactly on the entry — callers fail closed (bad_geometry)."""
    return abs(tp - entry) / abs(stop - entry)


def max_leverage_for(cfg, symbol: str) -> int:
    """Flat fast_cycle_max_leverage with per-symbol overrides from
    fast_cycle_max_leverage_by_symbol ("ETH-USD:40,BTC-USD:20" — Governor
    2026-09-28: ETH 40x new SoDEX tier while BTC campaigns at 20x). The
    override WINS over the flat cap in either direction; empty/malformed
    knob = flat cap bit-for-bit."""
    cap = int(getattr(cfg, "fast_cycle_max_leverage", 15))
    raw = str(getattr(cfg, "fast_cycle_max_leverage_by_symbol", "") or "")
    want = _normalize_symbol(symbol)
    for part in raw.split(","):
        k, _, v = part.partition(":")
        if k.strip() and _normalize_symbol(k.strip()) == want and v.strip():
            try:
                return int(float(v.strip()))
            except (TypeError, ValueError):
                return cap
    return cap


def leverage_for_cage(cage: float, symbol: str,
                      max_leverage: int = 0) -> Optional[int]:
    """Cage-scaled band min the symbol's exchange-tier cap.

    Bands (caller has already gated cage ≥ cage_min):
      cage < 4.0  → 15x
      cage < 6.0  → 20x
      cage < 8.0  → 28x
      cage ≥ 8.0  → 38x
    Returns None for symbols outside LEVERAGE_CAPS (ineligible).
    max_leverage > 0 = flat ceiling over the ladder (Governor 2026-09-27:
    15x max); 0 = legacy ladder bit-for-bit.
    """
    cap = LEVERAGE_CAPS.get(_normalize_symbol(symbol))
    if cap is None:
        return None
    if cage < 4.0:
        band = 15
    elif cage < 6.0:
        band = 20
    elif cage < 8.0:
        band = 28
    else:
        band = 38
    lev = min(band, cap)
    if max_leverage > 0:
        lev = min(lev, int(max_leverage))
    return lev


def est_roundtrip_fee(notional: float, maker_rate: float, taker_rate: float,
                      maker_share: float = 1.0) -> float:
    """Estimated round-trip fee in USD.

    maker_share = expected fraction of the round trip's filled volume
    executing as maker (1.0 = both sides maker — the doctrine default for
    maker-only resting-limit entries; 0.5 = one maker side + one taker
    side, e.g. maker entry + taker stop-out; 0.0 = both sides taker).
    """
    blend = maker_share * maker_rate + (1.0 - maker_share) * taker_rate
    return 2.0 * notional * blend


# ── Verdict ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EntryVerdict:
    """Order SPEC (approve) or standdown. The caller places orders; this
    brain never touches the venue."""
    action: str                      # "approve" | "standdown"
    reason: str = ""
    margin_usd: float = 0.0
    leverage: int = 0
    notional_usd: float = 0.0
    cage_ratio: float = 0.0
    est_roundtrip_fee_usd: float = 0.0


def _standdown(reason: str, cage: float = 0.0) -> EntryVerdict:
    return EntryVerdict(action="standdown", reason=reason, cage_ratio=cage)


# ── Engine ───────────────────────────────────────────────────────────────────

class FastCycleEngine:
    """Pool ledger + cumulative fee/pnl/volume counters for the fast-cycle
    campaign. In-memory; rebuilt at boot from the open-position census
    (rebuild), exactly like HedgeReserve."""

    def __init__(self) -> None:
        self._debited: float = 0.0
        self._cum_volume: float = 0.0   # filled volume, both sides (USD)
        self._cum_fees: float = 0.0     # gross fees paid (USD)
        self._cum_pnl: float = 0.0      # campaign realized PnL (USD)
        self._cfg = None                # last cfg seen (for fee_gauge())

    # ── pool accounting ──────────────────────────────────────────────────

    def on_entry(self, symbol: str, margin_usd: float) -> None:
        """Debit the pool when an approved entry fills."""
        self._debited += max(0.0, float(margin_usd))

    def on_close(self, symbol: str, margin_usd: float,
                 realized_pnl_usd: float, fee_usd: float,
                 notional_usd: float) -> dict:
        """Credit the pool and accumulate the campaign counters on close.
        Returns the current fee gauge.

        CONTRACT (cross-review P1): realized_pnl_usd is GROSS (before fees)
        and fee_usd covers BOTH legs of the round trip — passing journal-
        convention net-of-fee PnL double-counts the fees and breaches the
        budget ~2x early."""
        self._debited = max(0.0, self._debited - max(0.0, float(margin_usd)))
        self._cum_pnl += float(realized_pnl_usd)
        self._cum_fees += max(0.0, float(fee_usd))
        # Volume counts both sides of the round trip (entry + exit fills).
        self._cum_volume += 2.0 * max(0.0, float(notional_usd))
        return self.fee_gauge()

    def rebuild(self, open_margins_usd: float) -> None:
        """Boot recovery: re-debit the margins of still-open fast-cycle
        positions. Counters are append-only history and are NOT rebuilt
        here (the journal owns them)."""
        self._debited = max(0.0, float(open_margins_usd))

    def restore_counters(self, *, volume_usd: float, fees_usd: float,
                         realized_pnl_usd: float) -> None:
        """Boot recovery for the fee governor (cross-review P0): the NET
        doctrine is cumulative over the campaign's life, and this engine's
        counters are memory-only — without restore, every restart resets
        the governor to budget_ok=True regardless of lifetime breach.
        The splice feeds the VolumeLedger's persisted campaign-pool totals
        (same arithmetic as on_close accumulates: gross pnl, both-leg fees,
        2x notional volume)."""
        self._cum_volume = max(0.0, float(volume_usd))
        self._cum_fees = max(0.0, float(fees_usd))
        self._cum_pnl = float(realized_pnl_usd)

    @property
    def debited(self) -> float:
        return self._debited

    # ── fee governor ─────────────────────────────────────────────────────

    def _net_cost_per_100k(self) -> float:
        if self._cum_volume <= 0.0:
            return 0.0
        # Multiply before divide: keeps the $7.00/100k boundary exact in
        # IEEE arithmetic (the strictly-greater breach pin depends on it).
        return ((self._cum_fees - self._cum_pnl)
                * 100000.0) / self._cum_volume

    def fee_gauge(self, cfg=None) -> dict:
        if cfg is None:
            cfg = self._cfg
        budget = (float(getattr(cfg, "fast_cycle_fee_budget_per_100k", 7.00))
                  if cfg is not None else 7.00)
        per100k = self._net_cost_per_100k()
        return {
            "cumulative_volume_usd": self._cum_volume,
            "cumulative_fees_usd": self._cum_fees,
            "cumulative_realized_pnl_usd": self._cum_pnl,
            "net_cost_per_100k": per100k,
            # Breach is STRICTLY greater — exactly budget is OK.
            "budget_ok": per100k <= budget,
        }

    # ── entry verdict ────────────────────────────────────────────────────

    def entry_verdict(self, cfg, *, symbol: str, side: str,
                      entry_price: float, stop_price: float, tp_price: float,
                      open_positions, now_ts,
                      proposed_margin_usd: Optional[float] = None,
                      size_mult: float = 1.0) -> EntryVerdict:
        """Checks in doctrine order: kill switch → symbol eligibility →
        geometry (fail-closed) → fee governor → concurrency → pool.

        Read-only: this method NEVER mutates pool or counter state (the
        disabled pin depends on it). now_ts is injected for future TTLs —
        currently unused by design (zero-I/O contract).

        proposed_margin_usd (2026-09-28 ladder wiring): the caller's
        ladder-scaled margin (plan_fleet qty_margin_usd / probe budget).
        When provided and > 0 it REPLACES the flat
        fast_cycle_margin_per_trade read — before this kwarg the ladder was
        dead code (every approval minted the global 55.0). None/<=0 =
        legacy bit-for-bit.

        size_mult (2026-09-30 fleet balance scaling): the caller's equity
        float clamp(equity/ref, min, max). Multiplies the POOL (the
        fixed-USD fast_cycle_pool_usd envelope floats with the balance)
        and the flat fallback margin (only read when proposed_margin_usd
        is absent — a proposed margin arrives pre-scaled from plan_fleet).
        1.0 = legacy bit-for-bit (IEEE-exact: x * 1.0 == x).
        """
        _ = now_ts
        size_mult = max(0.0, float(size_mult))
        # 1. Kill switch — stand down with ZERO state mutation.
        if not bool(getattr(cfg, "fast_cycle_enabled", False)):
            return _standdown("disabled")
        self._cfg = cfg

        # 2. Symbol eligibility (exchange-tier table).
        cap = LEVERAGE_CAPS.get(_normalize_symbol(symbol))
        if cap is None:
            return _standdown("symbol_ineligible")

        # 3. Geometry — fail CLOSED on any degeneracy.
        side_l = str(side).lower()
        if side_l == "buy":
            side_l = "long"
        elif side_l == "sell":
            side_l = "short"
        stop_dist = abs(stop_price - entry_price)
        if side_l not in ("long", "short") or stop_dist <= 0.0:
            return _standdown("bad_geometry")
        if side_l == "long" and not (stop_price < entry_price):
            return _standdown("bad_geometry")
        if side_l == "short" and not (stop_price > entry_price):
            return _standdown("bad_geometry")
        cage = abs(tp_price - entry_price) / stop_dist
        cage_min = float(getattr(cfg, "fast_cycle_cage_min", 3.0))
        if cage < cage_min:
            return _standdown("bad_geometry", cage=cage)

        # 3b. Fee floor (cross-review P0; Governor's 2026-09-23 fill anchor:
        # measured taker round trip 7.5bps killed a WINNING 3-min scalp).
        # Stop distance must clear stop_floor_mult x the modeled round-trip
        # cost rate or the fee leg dominates EV regardless of cage.
        maker_rate = float(getattr(cfg, "fast_cycle_maker_fee_rate", 0.000114))
        taker_rate = float(getattr(cfg, "fast_cycle_taker_fee_rate", 0.00038))
        floor_mult = float(getattr(cfg, "fast_cycle_stop_fee_floor_mult", 3.0))
        stop_frac = stop_dist / entry_price
        roundtrip_rate = maker_rate + taker_rate  # maker entry + taker stop
        if stop_frac < floor_mult * roundtrip_rate:
            return _standdown("fee_floor", cage=cage)

        # 4. Fee governor — exits never gated, new entries stand down.
        # Abstain below a minimum sample (cross-review P0): on a tiny volume
        # denominator one stop-out prints a huge ratio and permanently
        # ratchets the campaign into standdown.
        budget = float(getattr(cfg, "fast_cycle_fee_budget_per_100k", 7.00))
        min_vol = float(getattr(cfg, "fast_cycle_fee_min_volume_usd", 1000.0))
        if self._cum_volume >= min_vol and self._net_cost_per_100k() > budget:
            return _standdown("fee_budget_breach", cage=cage)

        # 5. Concurrency (positions belonging to THIS pool only).
        max_conc = int(getattr(cfg, "fast_cycle_max_concurrent", 6))
        n_open = sum(1 for p in (open_positions or [])
                     if getattr(p, "pool", None) == POOL_NAME)
        if n_open >= max_conc:
            return _standdown("concurrency_cap", cage=cage)

        # 6. Pool free margin.
        if proposed_margin_usd is not None and proposed_margin_usd > 0:
            margin = float(proposed_margin_usd)
        else:
            margin = (float(getattr(cfg, "fast_cycle_margin_per_trade", 8.0))
                      * size_mult)
        pool = float(getattr(cfg, "fast_cycle_pool_usd", 40.0)) * size_mult
        if (pool - self._debited) < margin:
            return _standdown("pool_exhausted", cage=cage)

        lev = leverage_for_cage(
            cage, symbol,
            max_leverage=max_leverage_for(cfg, symbol))
        if lev is None:  # unreachable post-eligibility, fail closed anyway
            return _standdown("symbol_ineligible", cage=cage)
        notional = margin * lev
        # Honest blend (cross-review P1): SoDEX stops fire TAKER on trigger,
        # and at cage 3:1 most exits are the stop leg. Model maker entry +
        # taker_exit_frac of the exit side at taker, not maker-both-sides
        # (the 2.8x understatement).
        taker_exit_frac = float(
            getattr(cfg, "fast_cycle_taker_exit_frac", 0.75))
        maker_share = max(0.0, min(1.0, 1.0 - taker_exit_frac))
        fee_est = est_roundtrip_fee(notional, maker_rate, taker_rate,
                                    maker_share=maker_share)
        return EntryVerdict(
            action="approve", reason="approved",
            margin_usd=margin, leverage=lev, notional_usd=notional,
            cage_ratio=cage, est_roundtrip_fee_usd=fee_est,
        )
