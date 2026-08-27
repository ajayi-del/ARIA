"""Pure evidence-math helpers for the dispersion self-move exemption +
Hugo alt-breadth tiebreak (2026-08-27)."""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import (  # noqa: E402
    _alt_breadth_vote,
    rank_pctile,
    sigma_from_closes,
    vol_ratio_from_volumes,
)


# ── sigma_from_closes ────────────────────────────────────────────────────────

def test_sigma_none_on_thin_input():
    assert sigma_from_closes([]) is None
    assert sigma_from_closes([100.0] * 30) is None


def test_sigma_none_on_constant_series():
    assert sigma_from_closes([100.0] * 60) is None   # var = 0 → degenerate


def test_sigma_known_value():
    # alternating ±ln(1.01) log returns → σ ≈ ln(1.01)
    closes, px = [], 100.0
    for i in range(60):
        closes.append(px)
        px = px * 1.01 if i % 2 == 0 else px / 1.01
    s = sigma_from_closes(closes)
    assert s is not None
    assert abs(s - math.log(1.01)) < 1e-3


def test_sigma_skips_nonpositive_prices():
    closes = [100.0, 0.0] + [100.0 * (1.001 ** i) for i in range(40)]
    assert sigma_from_closes(closes) is not None
    assert sigma_from_closes([0.0] * 60) is None


# ── vol_ratio_from_volumes ───────────────────────────────────────────────────

def test_vol_ratio_none_when_thin():
    assert vol_ratio_from_volumes([1.0] * 100) is None   # 100 < 60 + 60


def test_vol_ratio_doubled_volume():
    vols = [1.0] * 140 + [2.0] * 60
    assert abs(vol_ratio_from_volumes(vols) - 2.0) < 1e-9


def test_vol_ratio_none_on_zero_base():
    vols = [0.0] * 140 + [5.0] * 60
    assert vol_ratio_from_volumes(vols) is None


def test_vol_ratio_recent_window_only():
    # old spike outside the recent window must not move the numerator
    vols = [1.0] * 60 + [10.0] * 80 + [1.0] * 60
    r = vol_ratio_from_volumes(vols)
    assert r is not None and r < 1.0   # base mean 3.57 > recent 1.0


# ── rank_pctile ──────────────────────────────────────────────────────────────

def test_rank_none_below_min_population():
    assert rank_pctile(5.0, [1.0, 2.0, 3.0]) is None


def test_rank_inclusive_fraction():
    pop = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert rank_pctile(5.0, pop) == 0.5
    assert rank_pctile(10.0, pop) == 1.0
    assert rank_pctile(0.5, pop) == 0.0


def test_rank_top_decile_cut():
    pop = [float(i) for i in range(1, 21)]   # 20 values
    assert rank_pctile(19.0, pop) == 0.95    # top decile


# ── _alt_breadth_vote ────────────────────────────────────────────────────────

def test_breadth_long_quorum():
    moves = {f"A{i}": 6.0 + i for i in range(5)}
    assert _alt_breadth_vote(moves, min_n=5, move_pct=5.0) == 1


def test_breadth_short_quorum():
    moves = {f"A{i}": -6.0 - i for i in range(6)}
    assert _alt_breadth_vote(moves, min_n=5, move_pct=5.0) == -1


def test_breadth_split_tape_abstains():
    moves = {f"L{i}": 7.0 for i in range(5)} | {f"S{i}": -7.0 for i in range(5)}
    assert _alt_breadth_vote(moves, min_n=5, move_pct=5.0) == 0


def test_breadth_below_quorum():
    moves = {f"A{i}": 9.0 for i in range(4)}
    assert _alt_breadth_vote(moves, min_n=5, move_pct=5.0) == 0


def test_breadth_ignores_none_and_small_moves():
    moves = {"A": None, "B": 4.9, "C": -4.9}
    assert _alt_breadth_vote(moves, min_n=1, move_pct=5.0) == 0
