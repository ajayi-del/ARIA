"""PLANE SCORE — pre-cascade confirmation rebuilt as DISTINCT-PLANE agreement
(Governor 2026-09-15 directive: ">=3 distinct planes agreeing, not >=N points",
across four planes: cascade-onchain / funding / whale / kline).

Zero-I/O brain. None = "dark": a dark plane NEVER counts toward the bar and
is NEVER a fail — it is reported so the ledger segments structural absence
from negative evidence. This module MEASURES, it never gates: it runs as a
shadow cohort alongside the legacy 4-point score (intelligence/pre_cascade.py)
to measure the legacy's double-count; live use is a separate Governor
decision after the shadow cohort has n. The KLINE plane did not exist in the
legacy score — it is the rebuild's new evidence class (a real dip: 6h AND
24h returns both negative, not a one-bar flick).

POLEMARCH STAMPS (bind interpretation):
  - DOUBLE-COUNT: legacy legs 1&2 are ONE plane (funding) and legs 3&4 are
    correlated (whale positioning drives OI — the positioning plane), so a
    legacy 4 is ~2 distinct planes at best. The "legacy_only" cohort tag
    exists to count exactly that case.
  - BASE RATE UNKNOWN — shadow from birth, n from zero. BAR_PLANES and the
    leg thresholds are priors awaiting the cohort census, not calibrated.
  - PLANES ARE STILL NOT FULLY INDEPENDENT (a liquidation burst moves the
    kline too) — the plane count is an UPPER BOUND on independent
    confirmations, honestly stamped as such.
  - THIS MODULE NEVER GATES. Any live wiring is a separate Governor decision
    after shadow evidence.
"""

from __future__ import annotations

from typing import Optional, Sequence

BAR_PLANES = 3
WHALE_LS_THRESHOLD = 1.8
LIQ_Z_MIN = 2.0
OI_FLUSH_1H = -0.01
LEGACY_BAR = 4


def _funding_plane(funding_rates: Optional[Sequence[float]]) -> dict:
    if funding_rates is None or len(funding_rates) < 3:
        return {"verdict": "dark",
                "legs": {"latest_neg": "dark", "prior2_any_neg": "dark"}}
    last3 = list(funding_rates)[-3:]
    latest_neg = last3[-1] is not None and last3[-1] < 0
    prior_any = any(r is not None and r < 0 for r in last3[:2])
    legs = {"latest_neg": "pass" if latest_neg else "fail",
            "prior2_any_neg": "pass" if prior_any else "fail"}
    return {"verdict": "pass" if (latest_neg and prior_any) else "fail",
            "legs": legs}


def _leg(value: Optional[float], ok: bool) -> str:
    return "dark" if value is None else ("pass" if ok else "fail")


def _any_leg_plane(legs: dict) -> dict:
    if all(v == "dark" for v in legs.values()):
        verdict = "dark"
    else:
        verdict = "pass" if "pass" in legs.values() else "fail"
    return {"verdict": verdict, "legs": legs}


def _whale_plane(whale_ls: Optional[float], named_whale_net: Optional[float],
                 oi_chg_6h: Optional[float]) -> dict:
    return _any_leg_plane({
        "whale_ls": _leg(whale_ls, whale_ls is not None
                         and whale_ls > WHALE_LS_THRESHOLD),
        "named_whale_net": _leg(named_whale_net, named_whale_net is not None
                                and named_whale_net > 0),
        "oi_chg_6h": _leg(oi_chg_6h, oi_chg_6h is not None and oi_chg_6h > 0),
    })


def _kline_plane(close_now: Optional[float], close_6h_ago: Optional[float],
                 close_24h_ago: Optional[float]) -> dict:
    refs = (close_now, close_6h_ago, close_24h_ago)
    if any(v is None or v <= 0 for v in refs):
        return {"verdict": "dark",
                "legs": {"ret_6h_neg": "dark", "ret_24h_neg": "dark"}}
    r6 = close_now / close_6h_ago - 1.0
    r24 = close_now / close_24h_ago - 1.0
    legs = {"ret_6h_neg": "pass" if r6 < 0 else "fail",
            "ret_24h_neg": "pass" if r24 < 0 else "fail"}
    return {"verdict": "pass" if (r6 < 0 and r24 < 0) else "fail",
            "legs": legs}


def _cascade_plane(liq_notional_1h_usd: Optional[float],
                   liq_z: Optional[float], oi_chg_1h: Optional[float]) -> dict:
    out = _any_leg_plane({
        "liq_z": _leg(liq_z, liq_z is not None and liq_z >= LIQ_Z_MIN),
        "oi_chg_1h": _leg(oi_chg_1h, oi_chg_1h is not None
                          and oi_chg_1h <= OI_FLUSH_1H),
    })
    if liq_notional_1h_usd is not None:
        out["aux_liq_notional_1h_usd"] = liq_notional_1h_usd
    return out


def plane_verdicts(funding_rates: Optional[Sequence[float]] = None,
                   whale_ls: Optional[float] = None,
                   named_whale_net: Optional[float] = None,
                   oi_chg_6h: Optional[float] = None,
                   close_now: Optional[float] = None,
                   close_6h_ago: Optional[float] = None,
                   close_24h_ago: Optional[float] = None,
                   liq_notional_1h_usd: Optional[float] = None,
                   liq_z: Optional[float] = None,
                   oi_chg_1h: Optional[float] = None) -> dict:
    """Per-plane verdicts for one observation. Every plane reports
    {"verdict": pass/fail/dark, "legs": {...}} — dark = plane absent."""
    return {
        "funding": _funding_plane(funding_rates),
        "whale": _whale_plane(whale_ls, named_whale_net, oi_chg_6h),
        "kline": _kline_plane(close_now, close_6h_ago, close_24h_ago),
        "cascade_onchain": _cascade_plane(liq_notional_1h_usd, liq_z, oi_chg_1h),
    }


def plane_score(**kwargs) -> dict:
    """Count of confirming planes (0-4). Dark planes never count and are
    surfaced in n_dark; bar_met at plane_score >= BAR_PLANES."""
    planes = plane_verdicts(**kwargs)
    n_pass = sum(1 for p in planes.values() if p["verdict"] == "pass")
    n_dark = sum(1 for p in planes.values() if p["verdict"] == "dark")
    return {"plane_score": n_pass, "planes": planes,
            "bar_met": n_pass >= BAR_PLANES, "n_dark": n_dark}


def legacy_score(funding_latest_neg: Optional[bool],
                 funding_prior2_neg: Optional[bool],
                 oi_chg_6h: Optional[float],
                 whale_ls: Optional[float]) -> Optional[int]:
    """Legacy 4-point count from already-computed leg values (pre_cascade.py
    thresholds: oi_chg_6h > 0, whale_ls > 1.8 strictly). None leg counts 0;
    ALL four None -> None (dark). Logged side-by-side with the plane score
    so the shadow cohort can measure the double-count."""
    legs = [funding_latest_neg, funding_prior2_neg,
            None if oi_chg_6h is None else oi_chg_6h > 0,
            None if whale_ls is None else whale_ls > WHALE_LS_THRESHOLD]
    if all(l is None for l in legs):
        return None
    return sum(1 for l in legs if l is True)


def compare(plane_result: dict, legacy: Optional[int]) -> str:
    """Shadow-ledger cohort tag. "legacy_only" is the double-count cohort —
    the reason this module exists; "plane_only" is the cohort legacy missed."""
    if legacy is None and plane_result["plane_score"] < BAR_PLANES:
        return "dark"
    armed = plane_result["bar_met"]
    legacy_armed = legacy is not None and legacy >= LEGACY_BAR
    if armed and legacy_armed:
        return "agree_armed"
    if legacy_armed:
        return "legacy_only"
    if armed:
        return "plane_only"
    return "agree_calm"
