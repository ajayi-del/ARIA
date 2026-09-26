"""
intelligence/obob_governor.py — the OBOB governor (Optimal Bound Of daily trades).

Governor doctrine (locked 2026-09-26): OBOB = the daily trade count that
maximizes (profit x probability) - (fees x count) - (funding x hold time),
subject to two hard constraints: daily_loss <= $30 AND the campaign pool
keeps >= $20 reserve. This module is the zero-I/O brain that turns those
constraints into daily budgets; the splice decides.

THE TWO-SYSTEM LAW (the paste's core conclusion — NEVER conflate):
  PROFIT TRADES: 4-12/day — the main book, Kelly-sized, Kant-gated, high EV
    ($20-80 margin per trade).
  VOLUME PLACEMENTS: 200-600/day — the campaign pool, $7-15 margin,
    limit-only, MOST EXPIRE UNFILLED. UNFILLED PLACEMENTS ARE FREE. Only
    fills count as volume. Expected campaign fills: 12-25/day; total fills
    16-37/day.
The volume objective is met by PLACEMENT DENSITY (~46/hour is feasible),
not by fill count. The P&L objective is protected by the loss cap.

THE OBOB TABLE (doctrine 2026-09-26):
  profit trades       4-12 / day
  campaign fills      12-25 / day
  volume placements   200-600 / day (unfilled = free)
  total fills         16-37 / day

FEE-RATE RECONCILIATION NOTE: the OBOB paste quotes doc-headline rates
(maker 0.02% / taker 0.05% per side). The fast_cycle engine's verified
defaults are maker 0.0114% / taker 0.038% (measured + SOSO_STAKED 5%
discount). Rule 9: the exchange is truth — a live fee probe at splice time
decides. ALL rates here are knobs, never constants.

NEVER-SIZE / NEVER-GATE LAW: the governor emits BUDGETS. It never sizes an
order, never vetoes a signal, never touches the venue. The splice reads the
verdict and decides.

Department shape (docs/DEPARTMENT_TEMPLATE.md): zero-I/O pure brain — no
network, no files, no clock; everything injected per call. The class is
stateless; every tunable is a getattr knob on the injected cfg.

Config knobs (injected cfg via getattr; coordinator adds to core/config.py):
  obob_governor_enabled=True            — False: every verdict is None
                                          (the pre-module system bit-for-bit)
  obob_daily_loss_cap_usd=30.0          — hard daily loss constraint
  obob_pool_reserve_usd=20.0            — campaign pool floor (never spent)
  obob_margin_per_trade_usd=8.0         — campaign margin per placement
  obob_est_fill_rate=0.35               — expected placement->fill rate
  obob_daily_volume_target_usd=71500.0  — daily volume target. NOTE: the
      prior locked target was $1.0-1.6M cumulative over 20 weeks
      (~$50k-80k/week); 71500.0 is the Governor's 2026-09-26 updated figure
      ($5M over 10 weeks, midpoint pace per day). Carried for the splice.
"""
from __future__ import annotations

import math
from typing import Optional

# ── Knob defaults (single source — getattr falls back here) ─────────────────
DEFAULT_ENABLED = True
DEFAULT_LOSS_CAP_USD = 30.0
DEFAULT_POOL_RESERVE_USD = 20.0
DEFAULT_MARGIN_PER_TRADE_USD = 8.0
DEFAULT_EST_FILL_RATE = 0.35
DEFAULT_DAILY_VOLUME_TARGET_USD = 71500.0


def _f(x) -> Optional[float]:
    """Finite float or None (NaN/inf/non-numeric all fail closed)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _enabled(cfg) -> bool:
    return bool(getattr(cfg, "obob_governor_enabled", DEFAULT_ENABLED))


class ObobGovernor:
    """Stateless OBOB verdict brain. All methods take cfg and injected
    numbers; nothing is stored between calls (the master-gate None pin
    depends on zero state mutation)."""

    # ── EV per trade ──────────────────────────────────────────────────────

    @staticmethod
    def ev_per_trade(cfg, *, win_rate: float, avg_win_usd: float,
                     avg_loss_usd: float, fee_rt_usd: float,
                     carry_usd: float = 0.0) -> Optional[float]:
        """EV = w x W - (1-w) x L - fee - carry (USD per trade).

        Paste's worked example (docstring reference): w=0.30, W=$11.20,
        L=$2.24, fee=$0.112, carry=$0.112 ->
            0.30 x 11.20 - 0.70 x 2.24 - 0.112 - 0.112
          = 3.36 - 1.568 - 0.224 = +1.568
        (the paste prints +$1.566 — hand-arithmetic rounding; IEEE gives
        +1.568, ~= +$1.57 either way. Pinned with tolerance.)

        Fail-closed: win_rate outside [0,1], negative W/L/fee/carry, or
        any non-finite input -> None. Master gate False -> None.
        """
        if not _enabled(cfg):
            return None
        w = _f(win_rate)
        W = _f(avg_win_usd)
        L = _f(avg_loss_usd)
        fee = _f(fee_rt_usd)
        carry = _f(carry_usd)
        if None in (w, W, L, fee, carry):
            return None
        if not (0.0 <= w <= 1.0):
            return None
        if W < 0.0 or L < 0.0 or fee < 0.0 or carry < 0.0:
            return None
        return w * W - (1.0 - w) * L - fee - carry

    # ── loss-cap trade bound ──────────────────────────────────────────────

    @staticmethod
    def max_trades_by_loss_cap(cfg, *, loss_cap_usd=None,
                               avg_loss_usd: float,
                               loss_frac: float) -> Optional[int]:
        """Max daily trades before the expected loss spend breaches the cap.

        Conservative FLOOR CHAIN (pinned, doctrine 2026-09-26):
            losers_max = floor(loss_cap / avg_loss)
            trades_max = floor(losers_max / loss_frac)
        Pin: $30 / $2.24 / 0.70 -> floor(13.39)=13 losers -> floor(13/0.70)
        = floor(18.57) = 18 trades.
        Why 18 and not 19: the naive order floor(loss_cap/avg_loss/
        loss_frac) = floor(19.13) = 19 spends the FRACTIONAL loser budget
        twice — the 0.39 of a loser left over from the first division is
        re-multiplied by 1/0.70 as if it were a whole trade. Flooring the
        loser count first is fail-closed: 13 losers is the hard budget, and
        at a 70% loss rate 13 losers arrive inside 18 trades, not 19.

        loss_cap_usd None -> knob obob_daily_loss_cap_usd (default 30.0).
        Degenerate (avg_loss<=0, loss_frac outside (0,1], cap<0) -> None.
        Master gate False -> None.
        """
        if not _enabled(cfg):
            return None
        if loss_cap_usd is None:
            loss_cap_usd = getattr(cfg, "obob_daily_loss_cap_usd",
                                   DEFAULT_LOSS_CAP_USD)
        cap = _f(loss_cap_usd)
        L = _f(avg_loss_usd)
        frac = _f(loss_frac)
        if cap is None or L is None or frac is None:
            return None
        if cap < 0.0 or L <= 0.0 or not (0.0 < frac <= 1.0):
            return None
        losers_max = math.floor(cap / L)
        return int(math.floor(losers_max / frac))

    # ── placement density for the volume target ───────────────────────────

    @staticmethod
    def placements_for_volume(cfg, *, volume_remaining_usd: float,
                              avg_rt_volume_per_fill_usd: float,
                              est_fill_rate: float) -> Optional[int]:
        """Placements needed to fill the remaining daily volume target.

        fills_needed = ceil(volume_remaining / avg_rt_volume_per_fill)
        placements   = ceil(fills_needed / est_fill_rate)

        Pin (the paste's example): $65,000 remaining, $280 RT/fill, 0.35
        fill rate -> ceil(232.14)=233 fills -> ceil(233/0.35)=666 placements.

        volume_remaining <= 0 -> 0 (target met — nothing to place).
        Degenerate (fill_rate outside (0,1], rt_volume<=0, volume<0) ->
        None. Master gate False -> None.
        """
        if not _enabled(cfg):
            return None
        vol = _f(volume_remaining_usd)
        rt = _f(avg_rt_volume_per_fill_usd)
        fr = _f(est_fill_rate)
        if vol is None or rt is None or fr is None:
            return None
        if vol < 0.0 or rt <= 0.0 or not (0.0 < fr <= 1.0):
            return None
        if vol == 0.0:
            return 0
        fills_needed = math.ceil(vol / rt)
        return int(math.ceil(fills_needed / fr))

    # ── the governor's daily verdict ──────────────────────────────────────

    @staticmethod
    def daily_budget(cfg, *, volume_remaining_usd: float,
                     avg_rt_volume_per_fill_usd: float,
                     est_fill_rate: float,
                     loss_cap_remaining_usd: float,
                     avg_loss_usd: float,
                     loss_frac: float,
                     pool_free_usd: float,
                     margin_per_trade_usd=None) -> Optional[dict]:
        """The OBOB verdict: the three bounds and which one binds.

        Returns:
          {
            "placements": p,            # advisory for the scheduler —
                                        # placements are FREE (unfilled
                                        # placements cost nothing); p may
                                        # exceed the trade bounds
            "fills_volume": f,          # fills the volume target still needs
            "max_trades_loss_cap": t,   # hard P&L bound (floor chain)
            "max_trades_pool": m,       # floor((pool_free - reserve)/margin)
            "binding": "volume" | "loss_cap" | "pool",
            "budget": min(f, t, m),     # caps FILLS, never placements
          }
        "budget" caps FILLS. placements_needed stays advisory: the
        scheduler may place far beyond the budget because unfilled
        placements are free; only fills consume the loss cap and the pool.
        reserve = knob obob_pool_reserve_usd (default 20.0);
        margin_per_trade_usd None -> knob obob_margin_per_trade_usd
        (default 8.0). A pool below reserve reads as 0 trades (fail-closed
        clamp — never negative).

        Degenerate required input (non-finite, avg_loss<=0, loss_frac
        outside (0,1], fill_rate outside (0,1], rt_volume<=0,
        loss_cap_remaining<0, pool_free<0, margin<=0, volume<0) -> None.
        volume_remaining == 0 -> volume bound 0 (binding "volume").
        Master gate False -> None.
        """
        if not _enabled(cfg):
            return None
        if margin_per_trade_usd is None:
            margin_per_trade_usd = getattr(cfg, "obob_margin_per_trade_usd",
                                           DEFAULT_MARGIN_PER_TRADE_USD)
        reserve = _f(getattr(cfg, "obob_pool_reserve_usd",
                             DEFAULT_POOL_RESERVE_USD))
        margin = _f(margin_per_trade_usd)
        pool = _f(pool_free_usd)
        if reserve is None or margin is None or pool is None:
            return None
        if margin <= 0.0 or pool < 0.0:
            return None

        t = ObobGovernor.max_trades_by_loss_cap(
            cfg, loss_cap_usd=loss_cap_remaining_usd,
            avg_loss_usd=avg_loss_usd, loss_frac=loss_frac)
        p = ObobGovernor.placements_for_volume(
            cfg, volume_remaining_usd=volume_remaining_usd,
            avg_rt_volume_per_fill_usd=avg_rt_volume_per_fill_usd,
            est_fill_rate=est_fill_rate)
        if t is None or p is None:
            return None

        vol = _f(volume_remaining_usd)
        rt = _f(avg_rt_volume_per_fill_usd)
        if vol is None or rt is None or rt <= 0.0:
            return None
        fills_volume = 0 if vol == 0.0 else int(math.ceil(vol / rt))

        m = max(0, int(math.floor((pool - reserve) / margin)))

        bounds = (("volume", fills_volume),
                  ("loss_cap", t),
                  ("pool", m))
        binding, budget = min(bounds, key=lambda kv: kv[1])
        return {
            "placements": p,
            "fills_volume": fills_volume,
            "max_trades_loss_cap": t,
            "max_trades_pool": m,
            "binding": binding,
            "budget": int(budget),
        }
