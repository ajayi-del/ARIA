"""Mark-scale quarantine (Workstream B): sentinel state machine, consumer
helper, phantom firewall, and wiring pins."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.mark_scale import (  # noqa: E402
    MarkScaleSentinel, LOW_RATIO, HIGH_RATIO, PERSIST_N, HEAL_N)
from intelligence.shadow_journal import REJECTION_EVENTS  # noqa: E402
import main as _main  # noqa: E402


# ── Sentinel: band + persistence ─────────────────────────────────────────────

def test_in_band_never_arms():
    s = MarkScaleSentinel()
    for _ in range(10):
        q, tr, r = s.observe("BTC-USD", 100.0, 100.5)
        assert q is False and tr is None
    assert not s.quarantined("BTC-USD")


def test_band_boundaries_are_in_band():
    s = MarkScaleSentinel()
    for _ in range(PERSIST_N + 2):
        q, tr, _ = s.observe("A", LOW_RATIO * 100.0, 100.0)     # exactly 0.70
        assert tr is None and q is False
        q, tr, _ = s.observe("B", HIGH_RATIO * 100.0, 100.0)    # exactly 1.43
        assert tr is None and q is False


def test_split_arms_after_persist_n_consecutive():
    s = MarkScaleSentinel()
    for i in range(PERSIST_N):
        q, tr, r = s.observe("SPCX-USD", 769.35, 140.5)   # 5.48x split
        if i < PERSIST_N - 1:
            assert tr is None and q is False
        else:
            assert tr == "armed" and q is True
            assert abs(r - 769.35 / 140.5) < 1e-9
    assert s.quarantined("SPCX-USD")


def test_single_split_does_not_arm():
    s = MarkScaleSentinel()
    q, tr, _ = s.observe("X", 200.0, 100.0)   # 2.0x — one observation
    assert tr is None and q is False


def test_counter_resets_on_in_band():
    s = MarkScaleSentinel()
    s.observe("X", 200.0, 100.0)
    s.observe("X", 200.0, 100.0)              # 2 splits
    s.observe("X", 100.0, 100.0)              # in-band → reset
    s.observe("X", 200.0, 100.0)
    q, tr, _ = s.observe("X", 200.0, 100.0)   # only 2 consecutive
    assert tr is None and q is False


def test_fail_open_on_invalid_inputs():
    s = MarkScaleSentinel()
    for m, k in ((0.0, 100.0), (100.0, 0.0), (-5.0, 100.0), (None, 100.0),
                 (100.0, None), ("bad", 100.0)):
        q, tr, r = s.observe("X", m, k)
        assert tr is None and q is False
    # State untouched: a fresh split still needs PERSIST_N real observations
    s.observe("X", 200.0, 100.0)
    q, tr, _ = s.observe("X", 0.0, 100.0)     # invalid — counter must NOT reset
    s.observe("X", 200.0, 100.0)
    q, tr, _ = s.observe("X", 200.0, 100.0)
    assert tr == "armed" and q is True


def test_heal_after_heal_n_in_band():
    s = MarkScaleSentinel()
    for _ in range(PERSIST_N):
        s.observe("X", 200.0, 100.0)
    assert s.quarantined("X")
    for i in range(HEAL_N):
        q, tr, _ = s.observe("X", 100.0, 100.0)
        if i < HEAL_N - 1:
            assert tr is None and q is True     # still armed while healing
        else:
            assert tr == "healed" and q is False
    assert not s.quarantined("X")


def test_heal_counter_resets_on_split():
    s = MarkScaleSentinel()
    for _ in range(PERSIST_N):
        s.observe("X", 200.0, 100.0)
    s.observe("X", 100.0, 100.0)
    s.observe("X", 100.0, 100.0)              # 2 in-band
    q, tr, _ = s.observe("X", 200.0, 100.0)   # split again → heal resets
    assert tr is None and q is True
    s.observe("X", 100.0, 100.0)
    q, tr, _ = s.observe("X", 100.0, 100.0)   # only 2 consecutive in-band
    assert tr is None and q is True


def test_per_symbol_isolation():
    s = MarkScaleSentinel()
    for _ in range(PERSIST_N):
        s.observe("SPCX-USD", 769.0, 140.0)
    assert s.quarantined("SPCX-USD")
    assert not s.quarantined("BTC-USD")


# ── Consumer helper + firewall ────────────────────────────────────────────────

class _FakePS:
    def __init__(self, val=None, raises=False, quarantined_sym="SPCX-USD"):
        self._val = val
        self._raises = raises
        self._key = f"mark_scale_quarantined:{quarantined_sym}"

    def get_ai_param(self, key, default=None):
        if self._raises:
            raise RuntimeError("disk gone")
        if key == self._key:
            return self._val if self._val is not None else default
        return default


def test_quarantine_helper_reads_param(monkeypatch):
    monkeypatch.delenv("MARK_SCALE_QUARANTINE_ENABLED", raising=False)
    assert _main._mark_scale_quarantined("SPCX-USD", ps=_FakePS({"ratio": 5.5})) is True
    assert _main._mark_scale_quarantined("SPCX-USD", ps=_FakePS(None)) is False
    assert _main._mark_scale_quarantined("SPCX-USD", ps=None) is False
    assert _main._mark_scale_quarantined("SPCX-USD", ps=_FakePS(raises=True)) is False


def test_quarantine_helper_kill_switch(monkeypatch):
    monkeypatch.setenv("MARK_SCALE_QUARANTINE_ENABLED", "false")
    assert _main._mark_scale_quarantined("SPCX-USD", ps=_FakePS({"ratio": 5.5})) is False
    pnl, suppressed = _main.apply_phantom_firewall("SPCX-USD", 792.0, ps=_FakePS({"ratio": 5.5}))
    assert pnl == 792.0 and suppressed is False


def test_phantom_firewall(monkeypatch):
    monkeypatch.delenv("MARK_SCALE_QUARANTINE_ENABLED", raising=False)
    pnl, suppressed = _main.apply_phantom_firewall("SPCX-USD", 792.0, ps=_FakePS({"ratio": 5.5}))
    assert pnl == 0.0 and suppressed is True
    pnl, suppressed = _main.apply_phantom_firewall("SPCX-USD", -649.78, ps=_FakePS({"ratio": 5.5}))
    assert pnl == 0.0 and suppressed is True
    # Unquarantined symbol flows through untouched
    pnl, suppressed = _main.apply_phantom_firewall("BTC-USD", -0.63, ps=_FakePS({"ratio": 5.5}))
    assert pnl == -0.63 and suppressed is False
    # No param_store → fail open
    pnl, suppressed = _main.apply_phantom_firewall("SPCX-USD", 792.0, ps=None)
    assert pnl == 792.0 and suppressed is False


# ── Wiring pins ───────────────────────────────────────────────────────────────

def test_shadow_gate_registered():
    assert REJECTION_EVENTS.get("entry_blocked_mark_scale") == "mark_scale_quarantine"


def test_main_source_wiring():
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "main.py")).read()
    # Five entry-path blocks: standard, momentum, aftermath, campaign, explosive
    assert src.count("entry_blocked_mark_scale") >= 5
    for path in ('path="standard"', 'path="cascade_momentum"',
                 'path="cascade_aftermath"', 'path="campaign_heartbeat"',
                 'path="explosive"'):
        assert path in src, f"missing {path}"
    # Phantom firewall + telemetry
    assert "apply_phantom_firewall(sym, pnl, ps=_param_store)" in src
    assert "phantom_close_suppressed" in src
    # Detector loop registered in the gather list
    assert '_supervise(_mark_scale_sentinel_loop,       "mark_scale_sentinel")' in src
    # Close-path skips
    assert src.count("_mark_scale_quarantined(_sym, ps=_param_store)") >= 2   # time_stop + trailing
    assert "_mark_scale_quarantined(_pp.symbol, ps=_param_store)" in src      # pnl_attribution
    assert "_mark_scale_quarantined(_adl_pp.symbol, ps=_param_store)" in src  # ADL
