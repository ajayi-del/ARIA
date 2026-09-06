"""Session-drawdown external-flow propagation (f455ead completion, 2026-09-06).

Pins: SessionDrawdownTracker.adjust_peak / reset_peak shift the peak for
attested external flows so withdrawals never read as trading drawdown,
and the regime gate re-derives immediately (update_drawdown only fires
on closes).
"""
import os

os.environ.setdefault("DD_CAUTION_PCT", "3.0")
os.environ.setdefault("DD_DEFENSIVE_PCT", "6.0")
os.environ.setdefault("DD_HALT_PCT", "10.0")

from memory.performance import SessionDrawdownTracker


def _tracker(peak=700.0, current=626.5):
    t = SessionDrawdownTracker()
    t.update_drawdown(peak)
    t.update_drawdown(current)
    return t


def test_withdrawal_adjust_relieves_phantom_halt():
    # 700 peak, 626.5 current = 10.5% phantom DD -> halt (the 2026-09-05 live case)
    t = _tracker()
    assert t.drawdown_regime == "halt"
    t.adjust_peak(-70.0, reason="external_withdrawal_detected")
    # peak 630 vs current 626.5 -> ~0.56% -> normal
    assert t.drawdown_regime == "normal"
    assert t.session_drawdown_pct < 1.0
    assert t.peak_equity == 630.0


def test_adjust_peak_recomputes_regime_immediately():
    # current_equity only refreshes on closes; the regime must not wait
    t = _tracker(peak=683.36, current=661.78)  # 3.16% -> caution
    assert t.drawdown_regime == "caution"
    t.adjust_peak(-14.54, reason="external_withdrawal_openbook")
    assert t.drawdown_regime == "normal"


def test_reset_peak_clears_to_balance():
    t = _tracker()
    t.reset_peak(626.5, reason="reset_drawdown.flag")
    assert t.peak_equity == 626.5
    assert t.session_drawdown_pct == 0.0
    assert t.drawdown_regime == "normal"


def test_adjust_peak_uninitialized_is_noop_safe():
    t = SessionDrawdownTracker()
    t.adjust_peak(-50.0, reason="external_withdrawal_detected")
    assert t.peak_equity == 0.0
    assert t.drawdown_regime == "normal"


def test_organic_ratchet_untouched():
    # adjust_peak never lowers the bar for organic gains: a higher balance
    # still ratchets the peak up via update_drawdown as before
    t = _tracker(peak=700.0, current=690.0)
    t.adjust_peak(-5.0, reason="external_withdrawal_detected")
    t.update_drawdown(710.0)
    assert t.peak_equity == 710.0
    assert t.session_drawdown_pct == 0.0
