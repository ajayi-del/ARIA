"""PEAD-brain pins: gap +4% with 40% pullback arms LONG; <30% not yet;
>50% missed (no chase); gap <3% no setup; short mirror; structural stop
breach invalidates; >100% fill invalidates; max-hold invalidates;
unconfirmed earnings no setup; outside 0-5d window no setup; kill switch;
fail-closed garbage."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence import pead_brain as pb  # noqa: E402

D = 86400.0
T0 = 1_800_000_000.0
EARN = T0 - 2 * D          # confirmed print 2 days ago


def _long(**kw):
    """Gap +4%: anchor 100 -> gap print 104; 40% pullback = 102.4;
    base candle low 101.5 (structural stop beyond the 102.0-102.8 zone,
    above the 100 anchor)."""
    a = dict(symbol="NVDA", earnings_ts=EARN, gap_pct=4.0,
             anchor_price=100.0, gap_price=104.0, current_price=102.4,
             base_candle_extreme=101.5, now=T0, confirmed=True)
    a.update(kw)
    return pb.detect_setup(**a)


# ── Arming ────────────────────────────────────────────────────────────────────

def test_gap4_pullback40_arms_long():
    s = _long()
    assert s is not None
    assert s.direction == "long"
    assert abs(s.gap_pct - 4.0) < 1e-12
    assert abs(s.pullback_pct - 0.40) < 1e-9
    # Zone = [50% level, 30% level] = [102.0, 102.8]; entry = zone mid
    lo, hi = s.entry_zone
    assert abs(lo - 102.0) < 1e-9 and abs(hi - 102.8) < 1e-9
    assert abs(s.entry - 102.4) < 1e-9
    assert s.structural_stop == 101.5 < s.entry
    assert s.gap_fill_level == 100.0
    # TP ladder 5/10/17.5% above entry, ascending
    assert len(s.tp_ladder) == 3
    assert all(t > s.entry for t in s.tp_ladder)
    assert abs(s.tp_ladder[0] - s.entry * 1.05) < 1e-9
    assert s.max_hold_s == 7 * D
    assert s.leverage_cap == 4.0
    assert s.armed_ts == T0


def test_pullback_below_30_not_yet():
    # 25% retrace: 104 - 0.25*4 = 103.0 — not yet in the zone
    assert _long(current_price=103.0) is None


def test_pullback_above_50_missed_no_chase():
    # 62.5% retrace: 104 - 0.625*4 = 101.5 — missed, never chase
    assert _long(current_price=101.5) is None


def test_gap_below_noise_floor_no_setup():
    # Gap +2% < 3% floor: 100 -> 102, 40% pullback = 101.2, valid stop 100.8
    assert _long(gap_pct=2.0, gap_price=102.0, current_price=101.2,
                 base_candle_extreme=100.8) is None


def test_short_mirror_arms():
    # Gap -4%: anchor 100 -> 96; 40% pullback = 97.6; base candle high 98.5
    s = pb.detect_setup("AMD", EARN, -4.0, 100.0, 96.0, 97.6, 98.5, T0)
    assert s is not None and s.direction == "short"
    assert abs(s.pullback_pct - 0.40) < 1e-9
    lo, hi = s.entry_zone
    assert abs(lo - 97.2) < 1e-9 and abs(hi - 98.0) < 1e-9
    assert s.structural_stop == 98.5 > s.entry
    assert all(t < s.entry for t in s.tp_ladder)


def test_degenerate_geometry_refused():
    # Structural stop INSIDE the zone = broken bracket -> abstain
    # (103.0 and 102.5 are both above zone_lo 102.0)
    assert _long(base_candle_extreme=103.0) is None
    assert _long(base_candle_extreme=102.5) is None
    # gap_price contradicts gap_pct sign
    assert _long(gap_price=98.0) is None


# ── Entry trigger ─────────────────────────────────────────────────────────────

def test_entry_triggered_zone_membership():
    s = _long()
    assert pb.entry_triggered(s, 102.4) is True
    assert pb.entry_triggered(s, 102.0) is True      # deep edge inclusive
    assert pb.entry_triggered(s, 102.8) is True      # shallow edge inclusive
    assert pb.entry_triggered(s, 104.5) is False     # above the zone
    assert pb.entry_triggered(s, 101.0) is False     # below the zone
    assert pb.entry_triggered(s, "junk") is False
    assert pb.entry_triggered(None, 102.4) is False


# ── Invalidation ──────────────────────────────────────────────────────────────

def test_structural_stop_breach_invalidates():
    s = _long()
    assert pb.invalidated(s, 101.4, T0) == "structural_stop"   # < 101.5 stop
    assert pb.invalidated(s, 102.4, T0) is None


def test_gap_full_fill_invalidates():
    # Base candle dipped below the anchor (99.5 < 100): the stop sits below
    # the fill line, so a price between them isolates the >100% fill leg.
    s = _long(base_candle_extreme=99.5)
    assert s is not None
    assert pb.invalidated(s, 99.8, T0) == "gap_filled"
    assert pb.invalidated(s, 99.4, T0) == "structural_stop"    # through both


def test_max_hold_invalidates():
    s = _long()
    assert pb.invalidated(s, 103.5, T0 + 8 * D) == "max_hold"
    assert pb.invalidated(s, 103.5, T0 + 6 * D) is None


def test_invalidated_guards():
    assert pb.invalidated(None, 100.0, T0) == "no_setup"
    s = _long()
    assert pb.invalidated(s, "junk", T0) is None


# ── Window / confirmation legs ────────────────────────────────────────────────

def test_unconfirmed_earnings_no_setup():
    assert _long(confirmed=False) is None


def test_outside_0_to_5d_window_no_setup():
    assert _long(earnings_ts=T0 - 6 * D) is None     # too old
    assert _long(earnings_ts=T0 + 3600.0) is None    # print in the future
    assert _long(earnings_ts=T0 - 5 * D) is not None  # boundary inclusive
    assert _long(earnings_ts=T0) is not None          # same-day arms


# ── Sizing baseline ───────────────────────────────────────────────────────────

def test_position_size_mult_baseline():
    assert pb.position_size_mult(_long()) == 1.0
    assert pb.position_size_mult(None) == 0.0


# ── Kill switch / fail-closed ─────────────────────────────────────────────────

def test_kill_switch(monkeypatch):
    monkeypatch.setenv("PEAD_BRAIN_ENABLED", "false")
    assert _long() is None
    monkeypatch.setenv("PEAD_BRAIN_ENABLED", "true")
    assert _long() is not None


def test_fail_closed_garbage():
    assert pb.detect_setup("NVDA", None, 4.0, 100.0, 104.0, 102.4,
                           103.2, T0) is None
    assert pb.detect_setup("NVDA", EARN, float("nan"), 100.0, 104.0,
                           102.4, 103.2, T0) is None
    assert pb.detect_setup("NVDA", EARN, 4.0, -1.0, 104.0, 102.4,
                           103.2, T0) is None
    assert pb.detect_setup("NVDA", EARN, 4.0, 100.0, 104.0, 102.4,
                           103.2, "junk") is None
