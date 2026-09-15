"""Aftermath two-condition gate (2026-09-15, Governor directive — Bybit AI
structural model): tier classification, time/depth/imbalance legs, verdict
ordering, fail-open abstains, never-raises, plus the shadow-journal wiring pin.
"""
import pytest

from intelligence.aftermath_gate import (
    MIN_DELAY_S, DEPTH_FLOOR, IMBALANCE_FLOOR,
    classify_tier, aftermath_verdict,
)
from intelligence.shadow_journal import REJECTION_EVENTS


# ── classify_tier: boundaries ────────────────────────────────────────────────

@pytest.mark.parametrize("notional,expected", [
    (0, "small"),
    (100_000, "small"),
    (499_999.99, "small"),
    (500_000, "medium"),          # boundary: medium starts AT 500k
    (1_999_999.99, "medium"),
    (2_000_000, "large"),         # boundary: large starts AT 2M
    (4_999_999.99, "large"),
    (5_000_000, "whale"),         # boundary: whale starts AT 5M
    (25_000_000, "whale"),
])
def test_tier_boundaries(notional, expected):
    assert classify_tier(notional) == expected


def test_tier_garbage_fails_open_to_small():
    assert classify_tier(None) == "small"
    assert classify_tier("garbage") == "small"
    assert classify_tier(float("nan")) == "small"
    assert classify_tier(-500) == "small"


def test_governor_ladders_stamped():
    assert MIN_DELAY_S == {"small": 90, "medium": 120, "large": 180, "whale": 300}
    assert DEPTH_FLOOR == {"small": 0.55, "medium": 0.60, "large": 0.65, "whale": 0.70}
    assert IMBALANCE_FLOOR == 1.20


# ── Time gate ────────────────────────────────────────────────────────────────

def test_time_gate_blocks_before_min_delay():
    v, r = aftermath_verdict(tier="medium", seconds_since_cascade=119,
                             session_mult=1.0, depth_ratio=None,
                             entry_side_imbalance=None)
    assert (v, r) == ("block", "time_gate")


def test_time_gate_passes_after_min_delay():
    v, r = aftermath_verdict(tier="medium", seconds_since_cascade=120,
                             session_mult=1.0, depth_ratio=None,
                             entry_side_imbalance=None)
    assert (v, r) == ("allow_full", "ok")


def test_time_gate_session_mult_scales():
    # asian session (1.4x) on a whale tier: 300 * 1.4 = 420s
    v, r = aftermath_verdict(tier="whale", seconds_since_cascade=419,
                             session_mult=1.4, depth_ratio=None,
                             entry_side_imbalance=None)
    assert (v, r) == ("block", "time_gate")
    v, r = aftermath_verdict(tier="whale", seconds_since_cascade=420,
                             session_mult=1.4, depth_ratio=None,
                             entry_side_imbalance=None)
    assert (v, r) == ("allow_full", "ok")


def test_time_gate_always_binds_even_when_data_dark():
    # Depth + imbalance dark: time still refuses an early entry.
    v, r = aftermath_verdict(tier="small", seconds_since_cascade=30,
                             session_mult=1.0, depth_ratio=None,
                             entry_side_imbalance=None)
    assert (v, r) == ("block", "time_gate")


# ── Depth gate ───────────────────────────────────────────────────────────────

def test_depth_gate_blocks_below_floor():
    v, r = aftermath_verdict(tier="large", seconds_since_cascade=999,
                             session_mult=1.0, depth_ratio=0.64,
                             entry_side_imbalance=None)
    assert (v, r) == ("block", "depth_gate")


def test_depth_gate_abstains_on_none():
    v, r = aftermath_verdict(tier="large", seconds_since_cascade=999,
                             session_mult=1.0, depth_ratio=None,
                             entry_side_imbalance=None)
    assert (v, r) == ("allow_full", "ok")


def test_depth_ratio_at_floor_passes():
    # Exactly at the whale floor (0.70) the depth GATE passes — the ratio is
    # in the half-size band [0.70, 0.75), so the verdict is allow_half, not a
    # block.
    v, r = aftermath_verdict(tier="whale", seconds_since_cascade=999,
                             session_mult=1.0, depth_ratio=0.70,
                             entry_side_imbalance=None)
    assert (v, r) == ("allow_half", "depth_band")


# ── allow_half band ──────────────────────────────────────────────────────────

def test_allow_half_medium_band():
    # medium floor 0.60, half ceiling 0.75: 0.60-0.75 -> allow_half
    v, r = aftermath_verdict(tier="medium", seconds_since_cascade=999,
                             session_mult=1.0, depth_ratio=0.60,
                             entry_side_imbalance=None)
    assert (v, r) == ("allow_half", "depth_band")
    v, r = aftermath_verdict(tier="medium", seconds_since_cascade=999,
                             session_mult=1.0, depth_ratio=0.7499,
                             entry_side_imbalance=None)
    assert (v, r) == ("allow_half", "depth_band")


def test_allow_full_above_half_ceiling():
    v, r = aftermath_verdict(tier="medium", seconds_since_cascade=999,
                             session_mult=1.0, depth_ratio=0.75,
                             entry_side_imbalance=None)
    assert (v, r) == ("allow_full", "ok")


# ── Imbalance gate ───────────────────────────────────────────────────────────

def test_imbalance_blocks_below_floor():
    v, r = aftermath_verdict(tier="small", seconds_since_cascade=999,
                             session_mult=1.0, depth_ratio=None,
                             entry_side_imbalance=1.19)
    assert (v, r) == ("block", "imbalance_gate")


def test_imbalance_allows_at_floor():
    v, r = aftermath_verdict(tier="small", seconds_since_cascade=999,
                             session_mult=1.0, depth_ratio=None,
                             entry_side_imbalance=1.20)
    assert (v, r) == ("allow_full", "ok")


def test_imbalance_abstains_on_none():
    v, r = aftermath_verdict(tier="small", seconds_since_cascade=999,
                             session_mult=1.0, depth_ratio=None,
                             entry_side_imbalance=None)
    assert (v, r) == ("allow_full", "ok")


def test_imbalance_floor_configurable():
    v, r = aftermath_verdict(tier="small", seconds_since_cascade=999,
                             session_mult=1.0, depth_ratio=None,
                             entry_side_imbalance=1.30, imbalance_floor=1.40)
    assert (v, r) == ("block", "imbalance_gate")


# ── Verdict ordering: time outranks depth outranks imbalance ─────────────────

def test_time_outranks_depth_and_imbalance():
    v, r = aftermath_verdict(tier="whale", seconds_since_cascade=10,
                             session_mult=1.0, depth_ratio=0.10,
                             entry_side_imbalance=0.50)
    assert (v, r) == ("block", "time_gate")


def test_depth_outranks_imbalance():
    v, r = aftermath_verdict(tier="whale", seconds_since_cascade=999,
                             session_mult=1.0, depth_ratio=0.10,
                             entry_side_imbalance=0.50)
    assert (v, r) == ("block", "depth_gate")


# ── Never raises on garbage input ────────────────────────────────────────────

@pytest.mark.parametrize("kwargs", [
    dict(tier="not_a_tier", seconds_since_cascade="x", session_mult="y",
         depth_ratio="z", entry_side_imbalance=object()),
    dict(tier=None, seconds_since_cascade=None, session_mult=None,
         depth_ratio=None, entry_side_imbalance=None),
    dict(tier=123, seconds_since_cascade=float("nan"),
         session_mult=float("inf"), depth_ratio=float("-inf"),
         entry_side_imbalance=float("nan")),
    dict(tier={"bad": 1}, seconds_since_cascade=[1], session_mult=-3,
         depth_ratio=0.9, entry_side_imbalance=2.0),
])
def test_never_raises_on_garbage(kwargs):
    v, r = aftermath_verdict(**kwargs)
    assert v in ("allow_full", "allow_half", "block")
    assert isinstance(r, str)


# ── Wiring pin: shadow journal registration ──────────────────────────────────

def test_shadow_journal_registration():
    assert REJECTION_EVENTS.get("signal_rejected_aftermath_gate") == "aftermath_gate"
