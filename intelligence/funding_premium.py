"""intelligence/funding_premium.py — Funding-rate & premium-index brain
(pure brain, zero I/O).

VENUE MECHANICS (confirmed SoDEX doctrine 2026-09-26):
  - Funding settles EVERY HOUR (8x Bybit's cadence), hard cap 4%/hour.
  - Peer-to-peer: the venue takes nothing — one side pays the other directly.
  - 8h rate = Avg Premium Index + clamp(InterestRate − Premium, −0.05%, +0.05%);
    the hourly rate = 8h rate / 8.
  - Premium = (Impact Bid − Oracle) − (Oracle − Impact Ask), dollar-normalized
    order-book imbalance, sampled by the venue every 5 seconds (720 samples/
    hour) — the highest-frequency institutional flow signal available.
  - Impact Notional = $200 x max leverage of the instrument.

SPLICE WIRING NOTE (official docs 2026-09-26): the "oracle" in the premium
formula is the venue INDEX PRICE — the weighted median of constituent
exchange mid-prices (60s trade-staleness filter per constituent) — NOT the
mark price (the 4-source median used for margin/liquidation/uPnL). The
splice must feed the index leg if the API exposes it; feeding the mark
instead is a documented proxy, not the venue definition.

DOCTRINE:
  - Funding is a SECONDARY cost vs the stop for campaign holds (≈3% of a
    5:1-cage TP at a 4h hold) but a PRIMARY signal: a LINK-style near-cap
    print = crowded positioning = carry income on the PAID side plus a
    contrarian/mean-reversion tell.
  - Negative funding (shorts paying longs) = the clean-long "FUNDING_FLIP"
    entry class.

ARCHITECTURAL LAW: this module emits verdicts/estimates ONLY — never sizes,
never gates, never orders. Dark inputs abstain (None) — never synthesize.

Department shape (docs/DEPARTMENT_TEMPLATE.md): zero I/O. No network, no
files, no clock — every input injected per call, every tunable a getattr
knob on the injected cfg. Stateless.

Kill switches (config getattr, False = all verdicts None, carry None,
premium None — zero state mutation):
  funding_premium_enabled        True   — master gate
  funding_premium_spike_bps      5.0    — |premium_bps| >= this → pressure
                                          verdict (>= binds: exactly at the
                                          knob IS a spike)
  funding_extreme_hourly_rate    0.0008 — 8bps/hour (the ALT_L1
                                          max_funding_for_long gate);
                                          comparison is STRICTLY greater:
                                          exactly at the knob → "normal"
  funding_flip_min_samples       2      — window length required for a flip
                                          verdict
"""
from __future__ import annotations

from typing import List, Optional


def _num(x) -> Optional[float]:
    """Strict numeric coercion — NaN/inf/non-numeric → None (fail-closed)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


class FundingPremiumBrain:
    """Stateless funding/premium verdict brain. All state lives in the cfg
    and the per-call arguments; the instance carries nothing."""

    def _enabled(self, cfg) -> bool:
        return bool(getattr(cfg, "funding_premium_enabled", True))

    # ── 1. 8h → hourly arithmetic ─────────────────────────────────────────

    def hourly_from_8h(self, rate_8h) -> Optional[float]:
        """rate_8h / 8. None on degenerate input (None, non-numeric,
        NaN/inf). No master-gate check: pure arithmetic, no verdict."""
        r = _num(rate_8h)
        if r is None:
            return None
        return r / 8.0

    # ── 2. Carry accrual (EV accounting workhorse) ────────────────────────

    def carry_accrual_usd(self, cfg, *, notional_usd, hourly_rate,
                          hours) -> Optional[float]:
        """Signed USD funding over the hold: notional x hourly_rate x hours.

        Sign convention: positive = the position PAYS funding (a LONG when
        rate > 0); negative = the position RECEIVES (income). The LINK-style
        worked example — $320 notional SHORT at the 4%/hour venue cap ≈
        $12.80/hour INCOME — is the CAP as a venue bound, NOT a forecast;
        never extrapolate the cap as an expected carry.

        Degenerate inputs (None / non-numeric / notional <= 0 / hours < 0)
        → None. Master gate False → None."""
        if not self._enabled(cfg):
            return None
        n = _num(notional_usd)
        r = _num(hourly_rate)
        h = _num(hours)
        if n is None or r is None or h is None:
            return None
        if n <= 0 or h < 0:
            return None
        return n * r * h

    # ── 3. Premium index (bps of oracle) ──────────────────────────────────

    def premium_bps(self, *, impact_bid, impact_ask, oracle
                    ) -> Optional[float]:
        """premium = (impact_bid − oracle) − (oracle − impact_ask)
                = impact_bid + impact_ask − 2 x oracle = 2 x (mid − oracle),
        returned in bps of oracle. Positive = buy-side pressure (impact mid
        above oracle). Worked example: bid 100.06 / ask 100.02 / oracle
        100.0 → 0.06 − (−0.02) = 0.08 → 8 bps. Any input None / <= 0 /
        non-numeric → None. Pure arithmetic — no master-gate check (the
        verdict wrapper owns the gate)."""
        b = _num(impact_bid)
        a = _num(impact_ask)
        o = _num(oracle)
        if b is None or a is None or o is None:
            return None
        if b <= 0 or a <= 0 or o <= 0:
            return None
        prem = (b - o) - (o - a)
        return prem / o * 1e4

    # ── 4. Premium flow verdict ───────────────────────────────────────────

    def premium_flow_verdict(self, cfg, *, impact_bid, impact_ask,
                             oracle) -> Optional[dict]:
        """|premium_bps| >= funding_premium_spike_bps →
        {"side": "buy_pressure"|"sell_pressure", "premium_bps": x};
        below → {"side": "neutral", "premium_bps": x}. Boundary semantics
        DEFINED: >= BINDS — exactly at the knob IS a spike (the knob is the
        first interesting print). Dark inputs / master gate False → None."""
        if not self._enabled(cfg):
            return None
        pb = self.premium_bps(impact_bid=impact_bid, impact_ask=impact_ask,
                              oracle=oracle)
        if pb is None:
            return None
        try:
            spike = float(getattr(cfg, "funding_premium_spike_bps", 5.0))
        except (TypeError, ValueError):
            spike = 5.0
        if spike < 0:
            spike = 5.0
        if abs(pb) >= spike:
            side = "buy_pressure" if pb > 0 else "sell_pressure"
        else:
            side = "neutral"
        return {"side": side, "premium_bps": pb}

    # ── 5. Funding extreme verdict ────────────────────────────────────────

    def funding_extreme_verdict(self, cfg, *, symbol, hourly_rate
                                ) -> Optional[dict]:
        """Crowded-positioning read against funding_extreme_hourly_rate
        (default 0.0008 = 8bps/hour, the ALT_L1 max_funding_for_long gate).
        STRICTLY-greater semantics: rate > threshold → longs_crowded
        (blocks_new_longs, carry_side "short"); rate < −threshold →
        shorts_crowded (blocks_new_shorts, carry_side "long"); exactly at
        the knob → "normal". None rate / master gate False → None.

        symbol rides the verdict dict for telemetry joins; it is never
        interpreted."""
        if not self._enabled(cfg):
            return None
        r = _num(hourly_rate)
        if r is None:
            return None
        try:
            thr = float(getattr(cfg, "funding_extreme_hourly_rate", 0.0008))
        except (TypeError, ValueError):
            thr = 0.0008
        if thr < 0:
            thr = 0.0008
        if r > thr:
            return {"symbol": symbol, "verdict": "longs_crowded",
                    "blocks_new_longs": True, "carry_side": "short",
                    "hourly_rate": r}
        if r < -thr:
            return {"symbol": symbol, "verdict": "shorts_crowded",
                    "blocks_new_shorts": True, "carry_side": "long",
                    "hourly_rate": r}
        return {"symbol": symbol, "verdict": "normal", "hourly_rate": r}

    # ── 6. Funding flip (clean-long / clean-short entry class) ────────────

    def funding_flip(self, cfg, *, rates_window) -> Optional[str]:
        """Sign-flip detector over an injected window of recent hourly rates
        (oldest → newest).

          latest < 0 after a non-negative predecessor → "long_signal"
            (shorts now PAY longs — the FUNDING_FLIP clean-long class).
          latest > 0 after a non-positive predecessor → "short_signal".

        ZERO-CROSSING RULE (pinned): zero is NON-DIRECTIONAL — it belongs
        to whichever side the last NON-ZERO rate established. The flip
        compares the latest rate against the most recent non-zero rate in
        the window (excluding the latest itself); a window whose only prior
        prints are zeros has no established side → no flip.

        Needs >= funding_flip_min_samples (default 2) valid samples;
        non-numeric entries are dropped (dark prints never vote). No flip /
        insufficient samples / degenerate / master gate False → None."""
        if not self._enabled(cfg):
            return None
        if not rates_window:
            return None
        try:
            min_samples = int(getattr(cfg, "funding_flip_min_samples", 2))
        except (TypeError, ValueError):
            min_samples = 2
        if min_samples < 2:
            min_samples = 2
        vals: List[float] = []
        for x in rates_window:
            v = _num(x)
            if v is not None:
                vals.append(v)
        if len(vals) < min_samples:
            return None
        latest = vals[-1]
        prior_nonzero: Optional[float] = None
        for v in vals[:-1]:
            if v != 0:
                prior_nonzero = v
        if prior_nonzero is None:
            return None
        if latest < 0 and prior_nonzero >= 0:
            return "long_signal"
        if latest > 0 and prior_nonzero <= 0:
            return "short_signal"
        return None
