"""
intelligence/min_viable_tp.py — minimum viable take-profit table (observer-class).

Pure zero-I/O brain + tiny CLI. No imports from main.py. Kill-switch N/A (pure
math). Cites ONE canonical number for specs, gates, and the LLM nodes.

FEE SOURCE (read these, not my arithmetic):
  core/fee_engine.py:31-32  PERPS_TAKER[0] = 0.00040, PERPS_MAKER[0] = 0.00012 (Tier 0)
  core/fee_engine.py:41-49  STAKING_DISCOUNTS — 30 SOSO -> 5% discount
  core/fee_engine.py:52-60  _staking_discount(soso_staked) step function
  core/fee_engine.py:165-175 effective = table_rate * (1 - discount), applied per side
  CLAUDE.md "SoDEX Auth Rules": SOSO_STAKED=168 -> the 5% discount is ACTIVE on ARIA.

ARIA's LIVE effective round-trip (RT) costs, Tier 0 with 168 SOSO staked (5% off):
  taker/taker        2 * 0.00040 * 0.95 = 0.00076  = 7.60 bp
  maker/maker        2 * 0.00012 * 0.95 = 0.000228 = 2.28 bp
  mixed (maker-in / taker-out) (0.00012+0.00040)*0.95 = 0.000494 = 4.94 bp
(the no-discount grid 8.0 / 2.4 / 5.2 bp is retained via soso_staked=0.)

MATH — expectancy in bp of entry, planned reward:risk R, win rate WR,
round-trip cost RT (bp):
    E(TP) = WR*TP - (1-WR)*(TP/R) - RT
    E = 0  =>  TP* = RT / (WR - (1-WR)/R)
Viability: the denominator WR - (1-WR)/R = (WR*(R+1) - 1)/R must be > 0,
i.e. WR > 1/(R+1). At or below that breakeven the strategy is negative
expectancy at EVERY tp — the cell is flagged non-viable (returns None).

SCOPE: exchange taker/maker fees ONLY. Funding, slippage, and spread cost
are NOT in RT — min_tp here is a FEE FLOOR, never an all-in cost floor.
Single-RT simplification: wins typically exit maker (resting TP limit) while
losses exit taker (stop market), so the honest RT is a WR-weighted blend;
the three table modes are bounds around that blend, not the blend itself.

House context: main.py:18010-18023 already enforces an entry-side gate at
expected_move < 3x round-trip (build_candidate_fee_reject). This module makes
the WR/R-conditioned table explicit and queryable; fee_ratio() exposes the
>0.30 fee-census retirement rule (rt / planned_tp).
"""
from __future__ import annotations

from typing import Dict, Optional

from core.fee_engine import PERPS_MAKER, PERPS_TAKER, _staking_discount

# ARIA's live staking state (CLAUDE.md SoDEX Auth Rules, 2026-09): 168 SOSO
# staked -> 5% discount tier (fee_engine STAKING_DISCOUNTS: 30 -> 0.05).
DEFAULT_SOSO_STAKED = 168.0
DEFAULT_TIER = 0

# Table axes (doctrine: fixed grids, Aronson — no free-parameter fits).
RR_AXIS = (1.5, 2.0, 3.0)
WR_AXIS = (0.40, 0.45, 0.50, 0.55, 0.60)

# Fee census retirement rule: a setup whose round-trip cost exceeds 30% of the
# planned TP distance is retired.
FEE_RATIO_RETIRE = 0.30

# Float guard: the exact-breakeven edge (wr == 1/(1+rr), e.g. rr=1.5/wr=0.40)
# leaves a denominator of ~1e-17, not exactly 0. Anything at or below this
# tolerance is the edge — non-viable by construction (E = -rt at all tp).
_DENOM_EPS = 1e-12

_EXEC_MODES = ("taker", "maker", "mixed_maker_taker")


def _validate(rt_cost_bps: float, rr: float, wr: float) -> None:
    if rt_cost_bps < 0:
        raise ValueError(f"rt_cost_bps must be >= 0, got {rt_cost_bps}")
    if rr <= 0:
        raise ValueError(f"rr must be > 0, got {rr}")
    if not (0.0 <= wr <= 1.0):
        raise ValueError(f"wr must be in [0, 1], got {wr}")


def breakeven_wr(rr: float) -> float:
    """Win rate below which no take-profit distance is viable at RR `rr`."""
    if rr <= 0:
        raise ValueError(f"rr must be > 0, got {rr}")
    return 1.0 / (1.0 + rr)


def min_viable_tp_bps(rt_cost_bps: float, rr: float, wr: float) -> Optional[float]:
    """
    Minimum TP distance (bp of entry) for expectancy = 0 net of costs.

    Returns None when the (rr, wr) combo is negative expectancy at ALL tp —
    i.e. wr*(1 + 1/rr) <= 1/rr * rr <=> wr <= 1/(1+rr) (denominator <= 0).
    Raises ValueError on degenerate inputs (rr<=0, wr outside [0,1], rt<0).
    """
    _validate(rt_cost_bps, rr, wr)
    denom = wr - (1.0 - wr) / rr
    if denom <= _DENOM_EPS:
        return None
    return rt_cost_bps / denom


def expectancy_bps(tp_bps: float, rt_cost_bps: float, rr: float, wr: float) -> float:
    """
    Per-trade expectancy in bp of entry at a planned TP distance.

    E = wr*tp - (1-wr)*(tp/rr) - rt_cost. Negative means the plan loses to
    fees + losses on average. Same input validation as min_viable_tp_bps.
    """
    _validate(rt_cost_bps, rr, wr)
    return wr * tp_bps - (1.0 - wr) * (tp_bps / rr) - rt_cost_bps


def fee_ratio(tp_bps_planned: float, rt_cost_bps: float) -> float:
    """
    rt_cost / planned_tp. The fee-census retirement rule retires setups with
    fee_ratio > 0.30 (FEE_RATIO_RETIRE).
    """
    if tp_bps_planned <= 0:
        raise ValueError(f"tp_bps_planned must be > 0, got {tp_bps_planned}")
    if rt_cost_bps < 0:
        raise ValueError(f"rt_cost_bps must be >= 0, got {rt_cost_bps}")
    return rt_cost_bps / tp_bps_planned


def default_fee_schedule(
    soso_staked: float = DEFAULT_SOSO_STAKED, tier: int = DEFAULT_TIER
) -> Dict[str, float]:
    """
    Round-trip costs in bp, recomputed from core.fee_engine's ACTUAL table
    constants with the staking discount applied (never trust hand arithmetic).

    taker:            2 * taker_rate
    maker:            2 * maker_rate
    mixed_maker_taker: maker_rate (in) + taker_rate (out)
    """
    if tier < 0 or tier >= len(PERPS_TAKER):
        raise ValueError(f"tier out of range: {tier}")
    if soso_staked < 0:
        raise ValueError(f"soso_staked must be >= 0, got {soso_staked}")
    disc = _staking_discount(soso_staked)
    taker = PERPS_TAKER[tier] * (1.0 - disc)
    maker = PERPS_MAKER[tier] * (1.0 - disc)
    return {
        "taker": 2.0 * taker * 1e4,
        "maker": 2.0 * maker * 1e4,
        "mixed_maker_taker": (maker + taker) * 1e4,
    }


def build_table(fee_schedule: Dict[str, float]) -> Dict[str, Dict[float, Dict[float, dict]]]:
    """
    Full matrix: {mode: {rr: {wr: cell}}} where cell =
      {"min_tp_bps": float|None, "viable": bool}
    viable=False flags negative expectancy at ALL tp (wr <= 1/(1+rr)).
    """
    table: Dict[str, Dict[float, Dict[float, dict]]] = {}
    for mode, rt in fee_schedule.items():
        mode_rows: Dict[float, Dict[float, dict]] = {}
        for rr in RR_AXIS:
            rr_rows: Dict[float, dict] = {}
            for wr in WR_AXIS:
                tp = min_viable_tp_bps(rt, rr, wr)
                rr_rows[wr] = {"min_tp_bps": tp, "viable": tp is not None}
            mode_rows[rr] = rr_rows
        table[mode] = mode_rows
    return table


def render_table(table: Dict[str, Dict[float, Dict[float, dict]]],
                 fee_schedule: Dict[str, float]) -> str:
    """Dense one-line-per-cell rendering for the CLI and reports."""
    lines = [
        "min_viable_tp — SoDEX Tier-0 perps (core/fee_engine.py:31-32, SOSO 5% active)",
        "TP* = RT / (WR - (1-WR)/R)   [bp of entry; main.py:18010 gate = 3x RT]",
        "fee schedule (rt bp): "
        + "  ".join(f"{m}={fee_schedule[m]:.2f}" for m in _EXEC_MODES if m in fee_schedule),
        "scope: exchange fees ONLY — funding/slippage/spread NOT in RT "
        "(fee floor, not all-in cost)",
    ]
    for mode in _EXEC_MODES:
        if mode not in table:
            continue
        rt = fee_schedule[mode]
        for rr in RR_AXIS:
            for wr in WR_AXIS:
                cell = table[mode][rr][wr]
                if cell["viable"]:
                    tp = cell["min_tp_bps"]
                    gate3x = 3.0 * rt
                    lines.append(
                        f"{mode:18s} rr={rr:.1f} wr={wr:.2f} "
                        f"min_tp={tp:7.2f}bp  (3xRT gate={gate3x:5.2f}bp)"
                    )
                else:
                    lines.append(
                        f"{mode:18s} rr={rr:.1f} wr={wr:.2f} "
                        f"min_tp=   N/A   NON-VIABLE (wr <= {breakeven_wr(rr):.3f} breakeven)"
                    )
    return "\n".join(lines)


def main() -> None:
    schedule = default_fee_schedule()
    print(render_table(build_table(schedule), schedule))


if __name__ == "__main__":
    main()
