"""Pins for the 2026-09-23 rejection-budget fix: the Kant daily-cap
reservation taken at dispatch (main.py record_execution) is RELEASED when
the intent dies at a pre-venue gate. Today's autopsy: 45 of 70 daily slots
consumed by journaled rejections (clamp_rr x39 + L4 spread x6) — the
standard path starved 9h on 25 real opens. Venue-touched failures
(bracket_failed / bracket_exception) stay consumed. Kill switch
KANT_REJECT_RELEASE_ENABLED=false = legacy bit-for-bit."""
import os

from execution.kant_gate import KantGate, _utc_day


def _gate():
    return KantGate()


def _check(g, symbol="BTC-USD", direction="long"):
    # coherence 8 + rr 2.5 passes gates 2/4 on any balance tier;
    # regime_state=None skips gate 5.
    return g.check(symbol, direction, coherence=8.0, rr_ratio=2.5,
                   balance=700.0, regime_state=None)


# ── release semantics ─────────────────────────────────────────────────────

def test_release_decrements_global_and_symbol():
    g = _gate()
    g.record_execution("BTC-USD", "long")
    g.record_execution("ETH-USD", "short")
    day = _utc_day()
    assert g._daily_global[day] == 2
    g.release_execution("BTC-USD")
    assert g._daily_global[day] == 1
    assert g._daily_symbol[("BTC-USD", day)] == 0
    assert g._daily_symbol[("ETH-USD", day)] == 1


def test_release_floors_at_zero_without_prior_record():
    g = _gate()
    g.release_execution("BTC-USD")
    day = _utc_day()
    assert g._daily_global.get(day, 0) == 0
    assert g._daily_symbol.get(("BTC-USD", day), 0) == 0


def test_cap_reopens_after_release():
    g = _gate()
    day = _utc_day()
    g._daily_global[day] = 70   # at the >=$200 tier cap
    blocked = _check(g)
    assert not blocked.allowed
    assert blocked.reason.startswith("global_daily_limit_reached")
    g.release_execution("SOL-USD")  # floors symbol side, releases global
    assert g._daily_global[day] == 69
    assert _check(g).allowed


def test_release_never_touches_other_days():
    g = _gate()
    day = _utc_day()
    g._daily_global[day - 1] = 5
    g._daily_symbol[("BTC-USD", day - 1)] = 3
    g.record_execution("BTC-USD", "long")
    g.release_execution("BTC-USD")
    assert g._daily_global[day - 1] == 5
    assert g._daily_symbol[("BTC-USD", day - 1)] == 3
    assert g._daily_global.get(day, 0) == 0


def test_flip_cooldown_not_rolled_back_on_release():
    # Fail-closed: a rejected intent still witnessed direction — the 60-min
    # flip cooldown survives the release.
    g = _gate()
    g.record_execution("BTC-USD", "long")
    g.release_execution("BTC-USD")
    assert "BTC-USD" in g._last_exec
    assert g._last_exec["BTC-USD"][0] == "long"


def test_record_release_balance_one_to_one():
    g = _gate()
    for _ in range(10):
        g.record_execution("BTC-USD", "long")
    for _ in range(10):
        g.release_execution("BTC-USD")
    day = _utc_day()
    assert g._daily_global[day] == 0
    # Extra releases stay floored — no negative drift.
    g.release_execution("BTC-USD")
    assert g._daily_global[day] == 0


# ── config / wiring pins ─────────────────────────────────────────────────

_MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "main.py")


def _src():
    with open(_MAIN) as f:
        return f.read()


def test_pre_venue_rejection_set_exact():
    src = _src()
    i = src.index("_KANT_PRE_VENUE_REJECTIONS = frozenset({")
    block = src[i:i + 500]
    for reason in ("l4_fill_quality_defer", "l4_spread_gate_deferred",
                   "portfolio_allocator_veto", "clamp_rr_below_min",
                   "cross_sleeve_veto", "direction_gate"):
        assert f'"{reason}"' in block
    # Venue-touched failures must NEVER release — they stay consumed.
    assert "bracket_failed" not in block
    assert "bracket_exception" not in block


def test_release_runs_before_journal_kill_switch_return():
    src = _src()
    i_fn = src.index("def _journal_rejected")
    i_release = src.index("release_execution", i_fn)
    i_return = src.index('JOURNAL_REJECTED_OUTCOME_ENABLED', i_fn)
    assert i_fn < i_release < i_return


def test_kill_switch_default_true_and_guardian_facade():
    src = _src()
    assert 'KANT_REJECT_RELEASE_ENABLED", "true"' in src
    assert "release_execution" in src
    import execution.execution_guardian as eg
    assert hasattr(eg.ExecutionGuardian, "release_execution")


def test_release_gated_on_membership_and_switch():
    src = _src()
    i_fn = src.index("def _journal_rejected")
    block = src[i_fn:i_fn + 1600]
    assert "_why in _KANT_PRE_VENUE_REJECTIONS" in block
    assert "KANT_REJECT_RELEASE_ENABLED" in block
