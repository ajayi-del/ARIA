"""TP ladder: personality rr_min floor + manual-trader structure snap."""
import sys
import os
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.tp_ladder import (  # noqa: E402
    floor_ladder_to_rr_min, swing_levels, structure_target,
    personality_tp_floor_enabled, structure_snap_enabled,
)


# ── floor_ladder_to_rr_min ────────────────────────────────────────────────

def test_floor_lifts_tp1_to_rr_min_long():
    # FLOW rr_min 2.0: default ladder 1/2/3 → 2/3/4
    out = floor_ladder_to_rr_min(100.0, 98.0, "long", 102.0, 104.0, 106.0, 2.0)
    assert out == (104.0, 106.0, 108.0)


def test_floor_scout_ladder():
    # SCOUT rr_min 2.5 → 2.5/3.5/4.5
    out = floor_ladder_to_rr_min(100.0, 98.0, "long", 102.0, 104.0, 106.0, 2.5)
    assert out == (105.0, 107.0, 109.0)


def test_floor_short_side():
    out = floor_ladder_to_rr_min(100.0, 102.0, "short", 98.0, 96.0, 94.0, 2.0)
    assert out == (96.0, 94.0, 92.0)


def test_floor_noop_when_tp1_already_above():
    assert floor_ladder_to_rr_min(100.0, 98.0, "long", 104.5, 106.0, 108.0, 2.0) is None


def test_floor_preserves_wider_existing_ladder():
    # TP1 below floor but TP2/TP3 already wide → rungs never move down
    out = floor_ladder_to_rr_min(100.0, 98.0, "long", 102.0, 108.0, 112.0, 2.0)
    assert out == (104.0, 108.0, 112.0)


def test_floor_rejects_doctrine_markers():
    # SHIELD rr_min 99 is a block marker, not a target
    assert floor_ladder_to_rr_min(100.0, 98.0, "long", 102.0, 104.0, 106.0, 99.0) is None
    assert floor_ladder_to_rr_min(100.0, 98.0, "long", 102.0, 104.0, 106.0, 0.0) is None


def test_floor_zero_risk_distance():
    assert floor_ladder_to_rr_min(100.0, 100.0, "long", 102.0, 104.0, 106.0, 2.0) is None


# ── swing_levels ──────────────────────────────────────────────────────────

@dataclass
class _C:
    high: float
    low: float
    close: float


def _mk(rows):
    return [_C(h, l, c) for h, l, c in rows]


def test_swing_levels_long_nearest_first():
    # swing high at index 3 (104 is max of its ±2 window), another at 8 (107)
    rows = [(100, 99, 99.5), (101, 99.5, 100), (102, 100, 101), (104, 101, 102),
            (102, 100, 101), (103, 100.5, 102), (104, 101, 103), (105, 102, 104),
            (107, 103, 104), (105, 102, 103), (103, 101, 102)]
    levels = swing_levels(_mk(rows), "long", 100.0, left_right=2)
    assert levels == [104.0, 107.0]


def test_swing_levels_short_nearest_first():
    rows = [(101, 99, 100), (100, 98, 99), (99, 97, 98), (100, 96, 97),
            (99, 97.5, 98), (100, 98, 99), (99, 95, 96), (100, 94, 95),
            (99, 96, 97), (100, 97, 98), (101, 98, 99)]
    levels = swing_levels(_mk(rows), "short", 100.0, left_right=2)
    assert levels == [96.0, 94.0]


def test_swing_levels_excludes_wrong_side_of_entry():
    rows = [(100, 99, 99.5), (101, 99.5, 100), (102, 100, 101), (104, 101, 102),
            (102, 100, 101), (103, 100.5, 102), (104, 101, 103), (105, 102, 104),
            (107, 103, 104), (105, 102, 103), (103, 101, 102)]
    # long entry at 105 — only 107 is above
    assert swing_levels(_mk(rows), "long", 105.0, left_right=2) == [107.0]


def test_swing_levels_too_few_candles():
    assert swing_levels(_mk([(100, 99, 99.5)] * 4), "long", 100.0) == []


# ── structure_target ──────────────────────────────────────────────────────

def _rows_with_swing(high_at):
    rows = [(100, 99, 99.5), (101, 99.5, 100), (102, 100, 101),
            (high_at, 101, 102), (102, 100, 101), (103, 100.5, 102),
            (103, 101, 102)]
    return _mk(rows)


def test_structure_target_snaps_line_in_band():
    # entry 100, stop 98 → risk 2. rr_min 2.0 → band [2.0, 3.5]R = [104, 107]
    # swing at 105 (2.5R) → target 105 − 0.1R buffer = 104.8
    tp = structure_target(100.0, 98.0, "long", 2.0, _rows_with_swing(105.0))
    assert abs(tp - 104.8) < 1e-9


def test_structure_target_none_when_line_below_floor():
    # swing at 103.5 (1.75R) < rr_min 2.0 → no snap (floor keeps ownership)
    assert structure_target(100.0, 98.0, "long", 2.0, _rows_with_swing(103.5)) is None


def test_structure_target_none_when_line_beyond_band():
    # swing at 108 (4R) > 2.0 + 1.5 → no snap
    assert structure_target(100.0, 98.0, "long", 2.0, _rows_with_swing(108.0)) is None


def test_structure_target_short():
    rows = [(101, 99, 100), (100, 98, 99), (99, 97, 98), (100, 95, 97),
            (99, 97.5, 98), (100, 98, 99), (100, 97, 98)]
    # entry 100, stop 102 → risk 2. swing low 95 (2.5R) → target 95 + 0.2 = 95.2
    tp = structure_target(100.0, 102.0, "short", 2.0, _mk(rows))
    assert abs(tp - 95.2) < 1e-9


def test_structure_target_never_below_rr_min():
    # buffer would pull target under the floor → clamped at rr_min exactly
    rows = _rows_with_swing(104.05)  # 2.025R; 0.1R buffer → 1.925R < 2.0 → clamp
    tp = structure_target(100.0, 98.0, "long", 2.0, rows)
    assert abs(tp - 104.0) < 1e-9


# ── kill switches ─────────────────────────────────────────────────────────

def test_kill_switches_default_true(monkeypatch):
    monkeypatch.delenv("PERSONALITY_TP_FLOOR_ENABLED", raising=False)
    monkeypatch.delenv("STRUCTURE_TP_SNAP_ENABLED", raising=False)
    assert personality_tp_floor_enabled() is True
    assert structure_snap_enabled() is True
    monkeypatch.setenv("PERSONALITY_TP_FLOOR_ENABLED", "false")
    monkeypatch.setenv("STRUCTURE_TP_SNAP_ENABLED", "false")
    assert personality_tp_floor_enabled() is False
    assert structure_snap_enabled() is False
