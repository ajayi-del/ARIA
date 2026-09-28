"""Rebalance-front-run pins: window boundaries (index_add: day 9 arms,
day 11 dark, day 4 max-strength zone boundary; index_delete: day 4 arms,
day 6 dark), strength ramp 0.5->1.0, expiry at effective_ts, unconfirmed
template abstains, kill switch, fail-closed garbage."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence import rebalance_frontrun as rf  # noqa: E402
from risk_calendar.equity_events import Event, INDEX_ADD, INDEX_DELETE  # noqa: E402

D = 86400.0
T0 = 1_800_000_000.0


def _add(days, confirmed=True):
    return Event(symbol="COIN", event_type=INDEX_ADD,
                 ts_utc=T0 + days * D, confirmed=confirmed,
                 index_name="SP500")


def _del(days, confirmed=True):
    return Event(symbol="MSTR", event_type=INDEX_DELETE,
                 ts_utc=T0 + days * D, confirmed=confirmed,
                 index_name="NASDAQ100")


# ── index_add window: 10d outer, 5d inner ────────────────────────────────────

def test_add_day9_arms():
    s = rf.frontrun_signal("COIN", _add(9), T0)
    assert s is not None and s.direction == "long"
    assert abs(s.days_to_effective - 9.0) < 1e-9
    # strength = 0.5 + 0.5 * (10-9)/(10-5) = 0.6
    assert abs(s.strength - 0.6) < 1e-9
    assert s.effective_ts == T0 + 9 * D


def test_add_day11_dark():
    assert rf.frontrun_signal("COIN", _add(11), T0) is None


def test_add_day10_outer_boundary():
    s = rf.frontrun_signal("COIN", _add(10), T0)
    assert s is not None and s.strength == 0.5


def test_add_day5_inner_boundary():
    s = rf.frontrun_signal("COIN", _add(5), T0)
    assert s is not None and s.strength == 1.0


def test_add_day4_max_strength_zone_boundary():
    s = rf.frontrun_signal("COIN", _add(4), T0)
    assert s is not None and s.strength == 1.0     # clamped at inner edge


# ── index_delete window: 5d outer, 3d inner ──────────────────────────────────

def test_delete_day4_arms():
    s = rf.frontrun_signal("MSTR", _del(4), T0)
    assert s is not None and s.direction == "short"
    # strength = 0.5 + 0.5 * (5-4)/(5-3) = 0.75
    assert abs(s.strength - 0.75) < 1e-9


def test_delete_day6_dark():
    assert rf.frontrun_signal("MSTR", _del(6), T0) is None


def test_delete_day3_inner_and_day2_max():
    assert rf.frontrun_signal("MSTR", _del(3), T0).strength == 1.0
    assert rf.frontrun_signal("MSTR", _del(2), T0).strength == 1.0


def test_delete_day5_outer_boundary():
    s = rf.frontrun_signal("MSTR", _del(5), T0)
    assert s is not None and s.strength == 0.5


# ── Expiry at the effective print ─────────────────────────────────────────────

def test_signal_expired_at_effective_ts():
    s = rf.frontrun_signal("COIN", _add(2), T0)
    assert rf.signal_expired(s, T0) is False
    assert rf.signal_expired(s, s.effective_ts - 1.0) is False
    assert rf.signal_expired(s, s.effective_ts) is True       # print = over
    assert rf.signal_expired(s, s.effective_ts + D) is True


def test_past_effective_no_signal():
    assert rf.frontrun_signal("COIN", _add(-1), T0) is None


def test_signal_expired_fail_closed():
    assert rf.signal_expired(None, T0) is True
    s = rf.frontrun_signal("COIN", _add(2), T0)
    assert rf.signal_expired(s, "junk") is True


# ── Unconfirmed template abstains ─────────────────────────────────────────────

def test_unconfirmed_template_abstains():
    assert rf.frontrun_signal("COIN", _add(7, confirmed=False), T0) is None
    assert rf.frontrun_signal("MSTR", _del(4, confirmed=False), T0) is None


# ── Kill switch / fail-closed ─────────────────────────────────────────────────

def test_kill_switch(monkeypatch):
    monkeypatch.setenv("REBALANCE_FRONTRUN_ENABLED", "false")
    assert rf.frontrun_signal("COIN", _add(7), T0) is None
    monkeypatch.setenv("REBALANCE_FRONTRUN_ENABLED", "true")
    assert rf.frontrun_signal("COIN", _add(7), T0) is not None


def test_fail_closed_garbage():
    assert rf.frontrun_signal("COIN", None, T0) is None
    assert rf.frontrun_signal("COIN", _add(7), "junk") is None
    assert rf.frontrun_signal("COIN", object(), T0) is None
    ev = Event(symbol="COIN", event_type="earnings", ts_utc=T0 + 5 * D)
    assert rf.frontrun_signal("COIN", ev, T0) is None
