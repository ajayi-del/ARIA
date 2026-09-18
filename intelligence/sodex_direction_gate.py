"""
intelligence/sodex_direction_gate.py — SoDEX sleeve direction-confluence
gate (Governor 2026-09-18 sleeve audit).

The wounds: ETH was shorted into a 7% bull day and OP was shorted at 2.4:1
whale-long positioning — counter-trend entries taken while a SECOND,
independent plane was already screaming the same warning. Single-plane
gates (trend-day veto, counter_trend) exist, but each plane alone is
stale-prone; the audit class is counter-trend entries confirmed by another
extreme.

Design: confluence, never one plane. A block requires counter-trend PLUS
at least (min_agree - 1) confirming extremes (funding carry, whale
long/short positioning). One stale plane never blocks; abstaining legs
(None) never count either way.

SHADOW-FIRST (Governor directive 2026-09-18): this is a NEW alpha-touching
gate — it must measure itself before it binds. The live flag exists but
defaults OFF; enabled-not-live logs + shadow-records the would-block and
the entry PROCEEDS. Kill switch off = zero code runs.

Pure, zero-I/O — callers inject the three plane reads. Fail-open:
unknown/None planes abstain, never block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple


@dataclass(frozen=True)
class GateDecision:
    block: bool
    reasons: Tuple[str, ...] = ()
    detail: dict = field(default_factory=dict)


def _norm_side(side: str) -> Optional[str]:
    s = str(side or "").strip().lower()
    if s in ("long", "buy"):
        return "long"
    if s in ("short", "sell"):
        return "short"
    return None


def evaluate(
    symbol: str,
    side: str,
    *,
    trend_4h: Optional[str],
    funding_bp: Optional[float],
    whale_ls_ratio: Optional[float],
    funding_extreme_bp: float = 8.0,
    whale_ratio_min: float = 2.0,
    min_agree: int = 2,
) -> GateDecision:
    """Counter-trend + confluence block. Legs are True/False/None (abstain).

    counter_trend:  side fights trend_4h ("up"/"down"; anything else = None)
    funding_extreme: short into funding >= +threshold, long into funding
        <= -threshold (don't fight the carry either way)
    whale_extreme:  short into whale L/S >= min, long into <= 1/min

    Block: counter_trend is True AND >= (min_agree - 1) confirming legs
    True. counter_trend None -> never block (abstain); confirming-leg None
    doesn't count either way.
    """
    sd = _norm_side(side)
    trend = str(trend_4h).strip().lower() if trend_4h is not None else None

    if sd is None or trend not in ("up", "down"):
        counter_trend: Optional[bool] = None
    else:
        counter_trend = (sd == "long" and trend == "down") or (
            sd == "short" and trend == "up")

    if funding_bp is None or sd is None:
        funding_extreme: Optional[bool] = None
    else:
        fb = float(funding_bp)
        funding_extreme = (sd == "short" and fb >= funding_extreme_bp) or (
            sd == "long" and fb <= -funding_extreme_bp)

    if whale_ls_ratio is None or sd is None or whale_ratio_min <= 0:
        whale_extreme: Optional[bool] = None
    else:
        wr = float(whale_ls_ratio)
        whale_extreme = (sd == "short" and wr >= whale_ratio_min) or (
            sd == "long" and wr <= 1.0 / whale_ratio_min)

    legs = {
        "counter_trend": counter_trend,
        "funding_extreme": funding_extreme,
        "whale_extreme": whale_extreme,
    }
    planes_present = tuple(
        p for p, v in (("trend", trend_4h), ("funding", funding_bp),
                       ("whale_ls", whale_ls_ratio)) if v is not None)

    confirming = sum(
        1 for v in (funding_extreme, whale_extreme) if v is True)
    need = max(0, int(min_agree) - 1)
    block = counter_trend is True and confirming >= need

    reasons = tuple(k for k, v in legs.items() if v is True)
    detail = {
        "trend_4h": trend if trend in ("up", "down") else None,
        "funding_bp": (round(float(funding_bp), 3)
                       if funding_bp is not None else None),
        "whale_ls_ratio": (round(float(whale_ls_ratio), 3)
                           if whale_ls_ratio is not None else None),
        "planes_present": ",".join(planes_present) or "none",
        "leg_counter_trend": counter_trend,
        "leg_funding_extreme": funding_extreme,
        "leg_whale_extreme": whale_extreme,
        "funding_extreme_bp": float(funding_extreme_bp),
        "whale_ratio_min": float(whale_ratio_min),
        "min_agree": int(min_agree),
    }
    return GateDecision(block=block, reasons=reasons, detail=detail)
