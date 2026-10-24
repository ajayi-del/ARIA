"""Squeeze radar — ABSOLUTE-positioning short-squeeze detector (zero-I/O brain).

Successor to intelligence/squeeze_scanner.py, which failed its census: every
leg was RELATIVE-to-recent-window (funding vs 7d avg, OI delta vs 6h ring,
rv_rank), and "squeeze-long = coin flip; score>=4 never precedes events".
This module replaces those relative legs with ABSOLUTE positioning
discriminators anchored on the verified IRYS 2026-10-23 anatomy ($21.4M short
liquidations, OI ~= 51% of circulating market cap):

  - funding_rate compared against ABSOLUTE negative thresholds, never against
    its own trailing average (the old scanner's blind spot: deeply negative
    funding during an already-negative regime read as "normal").
  - oi_to_float = oi_usd / supply_usd — OI as a fraction of the circulating
    market cap (CoinGecko plane), the leg that made IRYS identifiable.
  - short_ratio = 1 / long_short_ratio — whale accounts net short.
  - thin_book — execution-venue L4 spread/depth/listing-age fragility.

Department template (docs/DEPARTMENT_TEMPLATE.md):
  - ZERO I/O: no network, no files, no logging. Telemetry rides the returned
    SqueezeVerdict objects (legs / abstained).
  - All data injected via SqueezeSnapshot; all tunables constructor-injected
    via SqueezeRadarConfig.
  - Kill switch SQUEEZE_RADAR_ENABLED (env, default "true") read at CALL TIME
    by scan(); False -> scan() returns [] and the module is inert (it owns no
    other state), reproducing the pre-module system exactly.
  - Fail-closed everywhere: degenerate inputs abstain per leg; a leg whose
    required inputs are missing is unreachable, never inferred, never raises.

VSM: S4 intelligence (outside-and-then). It never gates entries itself.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

ENV_KILL_SWITCH = "SQUEEZE_RADAR_ENABLED"


def squeeze_radar_enabled() -> bool:
    """Kill switch, read at CALL TIME. Default True; False = inert (pre-module)."""
    raw = os.environ.get(ENV_KILL_SWITCH, "true")
    return str(raw).strip().lower() not in ("", "0", "false", "no", "off")


@dataclass(frozen=True)
class SqueezeRadarConfig:
    """All thresholds constructor-injected; defaults from the IRYS anatomy."""
    funding_negative: float = -3.0e-5    # tier-1 ABSOLUTE funding threshold
    funding_extreme: float = -8.0e-5     # tier-2/3 ABSOLUTE funding threshold
    oi_float_alert: float = 0.15         # tier-2 oi/supply threshold
    oi_float_extreme: float = 0.40       # tier-3 oi/supply threshold (IRYS ~0.51)
    short_ratio_min: float = 1.5         # tier-3 whale short/long threshold
    thin_spread_bps: float = 25.0        # thin-book leg: L4 spread
    thin_depth_usd: float = 15000.0      # thin-book leg: L4 depth within 10bps
    new_listing_days: float = 14.0       # thin-book leg: fresh perp listing
    trigger_pct: float = 0.08            # est. first 10x-short liq cluster
    macro_majors: frozenset = frozenset({"BTC", "ETH", "SOL", "XRP"})
    volume_spike_min: float = 5.0        # grade leg
    oi_growth_min: float = 0.20          # grade leg
    grade_base: float = 65.0
    grade_per_tier: float = 10.0


@dataclass(frozen=True)
class SqueezeSnapshot:
    """One symbol, one scan tick — the splice gathers these."""
    symbol: str
    mark_price: float
    funding_rate: Optional[float]        # live, e.g. -0.00008 (NOT a ratio to average)
    oi_usd: Optional[float]              # open interest in USD (live)
    oi_growth_3h: Optional[float]        # fractional growth over ~3h, e.g. 0.06
    supply_usd: Optional[float]          # circulating market cap (None = abstain leg)
    long_short_ratio: Optional[float]    # whale accounts long/short (None = abstain leg)
    volume_spike: Optional[float]        # (vol_1h*24)/vol_24h (None = abstain leg)
    spread_bps: Optional[float]          # execution-venue L4 spread
    depth_usd_10bps: Optional[float]     # execution-venue L4 ask+bid depth within 10bps
    listing_age_days: Optional[float]    # age of the perp listing (None = unknown)
    macro_window: bool = False           # calendar engine: PCE/CPI +/-3d
    now_s: float = 0.0


@dataclass(frozen=True)
class SqueezeVerdict:
    symbol: str
    tier: int                            # 0/1/2/3 (scan() emits tier>=1 only)
    grade: float                         # 0-100
    oi_to_float: Optional[float]
    funding: Optional[float]
    short_ratio: Optional[float]         # = 1/long_short_ratio when available
    thin_book: Optional[bool]
    trigger_price: Optional[float]       # mark x (1 + trigger_pct) — ESTIMATE
    legs: Dict[str, object] = field(default_factory=dict)
    abstained: Tuple[str, ...] = ()


def _f(x) -> Optional[float]:
    """Coerce to a finite float; None on None/NaN/inf/non-numeric. Never raises."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _base(symbol: str) -> str:
    """BTC-USD -> BTC; BTC -> BTC. Never raises."""
    try:
        return str(symbol).split("-")[0].strip().upper()
    except Exception:
        return ""


class SqueezeRadar:
    """Scores injected snapshots against absolute squeeze-positioning tiers.

    Owns no mutable scan state; every call is a pure function of its inputs
    plus the call-time kill switch.
    """

    def __init__(self, cfg: Optional[SqueezeRadarConfig] = None) -> None:
        self._cfg = cfg or SqueezeRadarConfig()

    @property
    def config(self) -> SqueezeRadarConfig:
        return self._cfg

    def scan(self, snapshots: List[SqueezeSnapshot]) -> List[SqueezeVerdict]:
        """Tier every snapshot; return tier>=1 verdicts sorted by grade desc.

        Kill switch read HERE (call time): disabled -> [] exactly. A malformed
        snapshot aborts that symbol only — never raises.
        """
        if not squeeze_radar_enabled():
            return []
        out: List[SqueezeVerdict] = []
        for snap in snapshots or []:
            try:
                verdict = self._evaluate(snap)
            except Exception:
                continue
            if verdict is not None and verdict.tier >= 1:
                out.append(verdict)
        out.sort(key=lambda v: -v.grade)
        return out

    def _evaluate(self, snap: SqueezeSnapshot) -> Optional[SqueezeVerdict]:
        cfg = self._cfg

        mark = _f(snap.mark_price)
        if mark is None or mark <= 0.0:
            return None            # degenerate core input: no verdict, never a guess

        funding = _f(snap.funding_rate)
        oi_usd = _f(snap.oi_usd)
        supply = _f(snap.supply_usd)
        ls_ratio = _f(snap.long_short_ratio)
        vol_spike = _f(snap.volume_spike)
        oi_growth = _f(snap.oi_growth_3h)
        spread = _f(snap.spread_bps)
        depth = _f(snap.depth_usd_10bps)
        age_days = _f(snap.listing_age_days)

        abstained: List[str] = []

        # oi_to_float: both legs required, both must be > 0. Fail-closed.
        oi_to_float: Optional[float] = None
        if oi_usd is not None and supply is not None and oi_usd > 0.0 and supply > 0.0:
            oi_to_float = oi_usd / supply
        else:
            abstained.append("oi_float")

        # short_ratio = 1/long_short_ratio; fail-closed.
        short_ratio: Optional[float] = None
        if ls_ratio is not None and ls_ratio > 0.0:
            short_ratio = 1.0 / ls_ratio
        else:
            abstained.append("short_ratio")

        if funding is None:
            abstained.append("funding")
        if vol_spike is None:
            abstained.append("volume_spike")
        if oi_growth is None:
            abstained.append("oi_growth_3h")

        # thin_book: informational; None when every sub-leg abstains.
        thin_book: Optional[bool] = None
        if spread is not None or depth is not None or age_days is not None:
            thin_book = bool(
                (spread is not None and spread > cfg.thin_spread_bps)
                or (depth is not None and depth < cfg.thin_depth_usd)
                or (age_days is not None and age_days >= 0.0 and age_days < cfg.new_listing_days)
            )
        else:
            abstained.append("thin_book")

        legs: Dict[str, object] = {
            "funding_negative": funding is not None and funding < cfg.funding_negative,
            "funding_extreme": funding is not None and funding < cfg.funding_extreme,
            "oi_float_alert": oi_to_float is not None and oi_to_float > cfg.oi_float_alert,
            "oi_float_extreme": oi_to_float is not None and oi_to_float > cfg.oi_float_extreme,
            "short_ratio_high": short_ratio is not None and short_ratio > cfg.short_ratio_min,
            "thin_book": thin_book,
            "macro_window": False,
            "trigger_estimate": True,   # trigger_price is a heuristic estimate
        }

        # Tier ladder — absolute thresholds only; tier 2/3 fail-closed on
        # abstained legs (never inferred).
        tier = 0
        if funding is not None and funding < cfg.funding_negative:
            tier = 1
            if (funding < cfg.funding_extreme
                    and oi_to_float is not None
                    and oi_to_float > cfg.oi_float_alert):
                tier = 2
        if (oi_to_float is not None and oi_to_float > cfg.oi_float_extreme
                and funding is not None and funding < cfg.funding_extreme
                and short_ratio is not None and short_ratio > cfg.short_ratio_min):
            tier = 3

        # Macro-window boost (August-PCE pre-positioning pattern): in-window
        # majors with absolutely-negative funding promote to tier 1.
        if (tier == 0 and bool(snap.macro_window)
                and _base(snap.symbol) in cfg.macro_majors
                and funding is not None and funding < cfg.funding_negative):
            tier = 1
            legs["macro_window"] = True

        trigger_price = mark * (1.0 + cfg.trigger_pct)

        # Grade: base + tier + bounded evidence legs; capped at 100.
        grade = cfg.grade_base + cfg.grade_per_tier * tier
        if funding is not None:
            grade += min(15.0, abs(funding) / 1e-4 * 15.0)
        if oi_to_float is not None:
            grade += min(10.0, oi_to_float * 25.0)
        if vol_spike is not None and vol_spike > cfg.volume_spike_min:
            grade += 8.0
        if oi_growth is not None and oi_growth > cfg.oi_growth_min:
            grade += 7.0
        if thin_book:
            grade += 4.0
        grade = min(100.0, grade)

        return SqueezeVerdict(
            symbol=str(snap.symbol),
            tier=tier,
            grade=grade,
            oi_to_float=oi_to_float,
            funding=funding,
            short_ratio=short_ratio,
            thin_book=thin_book,
            trigger_price=trigger_price,
            legs=legs,
            abstained=tuple(abstained),
        )
