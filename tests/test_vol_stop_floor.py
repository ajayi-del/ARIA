"""Floor-blocked vol_stop resize -> stop tighten (2026-09-19 OP/ARB audit)
plus the post-kelly sub-floor observability event.

Defect 1: the constant-risk resize target landed below the venue min-
notional floor; the floored size came out >= the current size, the
`0 < new < orig` guard failed, and the resize silently skipped — the full
position rode the wide vol stop at 2.3-3.1x intended USD risk with zero
telemetry. Fix: tighten the stop to the constant-risk distance at the
CURRENT size (tighten-only), emit vol_stop_floor_tightened /
vol_stop_resize_blocked.

Defect 2 (observability only): kelly runs after the venue floor, so the
final post-kelly notional can land under the floor -> sizing_below_floor.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from data.candle_buffer import Candle

H = 3_600_000
BAR = 4 * H
T0 = 1_758_000_000_000


def _bar(i: int, o=100.0, h=101.0, l=99.0, c=100.5, v=10.0) -> Candle:
    t = T0 + i * BAR
    return Candle(open_time=t, open=o, high=h, low=l, close=c, volume=v,
                  close_time=t + BAR - 1)


def _bars_atr1():
    # constant TR 1.0 per bar -> ATR(14) = 1.0; floor = 2.0 x ATR = 2.0 dist
    closes = [100.0 + i for i in range(20)]
    return [_bar(i, o=c, h=c, l=c, c=c) for i, c in enumerate(closes)]


class _FakeLogger:
    def __init__(self):
        self.calls = []

    def info(self, event, **kw):
        self.calls.append((event, kw))

    def warning(self, event, **kw):
        self.calls.append((event, kw))


class _Cand:
    symbol = "VOLTEST-USD"
    side = "long"
    entry_price = 100.0
    stop_price = 99.6
    tp1_price = 100.5
    coherence_score = 5.0
    leverage = 5.0

    def __init__(self, size):
        self.size = size
        self.initial_margin = round(size * self.entry_price / self.leverage, 8)


def _cfg(**over):
    base = dict(vol_stop_enabled=True, vol_stop_resize_enabled=True,
                vol_stop_resize_min_ratio=0.0, vol_stop_max_floor_ratio=0.0,
                vol_stop_floor_tighten_enabled=True,
                min_trade_notional_usd=80.0, default_leverage=5)
    base.update(over)
    return SimpleNamespace(**base)


def _run(c, cfg, fake_logger=None, monkeypatch=None):
    import main as m
    if fake_logger is not None and monkeypatch is not None:
        monkeypatch.setattr(m, "logger", fake_logger)
        monkeypatch.setattr("intelligence.sizing_decorrelation.log_audit",
                            lambda *a, **k: None)
    m._VOL_STOP_CACHE["VOLTEST-USD"] = (time.time(), _bars_atr1(), None)
    try:
        asyncio.run(m._vol_stop_splice(c, {}, cfg))
    finally:
        m._VOL_STOP_CACHE.pop("VOLTEST-USD", None)


# ── Defect 1: floor-blocked resize tightens the stop ─────────────────────────

def test_floor_blocked_resize_tightens_stop_long(monkeypatch):
    # size 0.75 ($75 notional): constant-risk target 0.15 ($15) < $80 floor
    # -> floored 0.8 >= current 0.75 -> guard fails -> tighten instead.
    fake = _FakeLogger()
    c = _Cand(size=0.75)
    _run(c, _cfg(), fake, monkeypatch)
    # stop tightened back to the constant-risk distance at current size:
    # intended risk = 0.75 x 0.4 = $0.30 -> dist 0.4 -> stop 99.6 (the
    # original tight stop), NOT the floored 98.0 wide stop.
    assert c.stop_price == pytest.approx(99.6)
    assert c.size == pytest.approx(0.75)            # size untouched
    assert c.tp1_price == pytest.approx(105.0)      # TP floor still applies
    # constant-dollar risk held: 0.75 x 0.4 = $0.30
    assert c.size * abs(c.entry_price - c.stop_price) == pytest.approx(0.30)
    hits = [kw for ev, kw in fake.calls if ev == "vol_stop_floor_tightened"]
    assert len(hits) == 1
    assert hits[0]["symbol"] == "VOLTEST-USD"
    assert hits[0]["side"] == "long"
    assert hits[0]["old_stop"] == pytest.approx(98.0)
    assert hits[0]["new_stop"] == pytest.approx(99.6)
    assert hits[0]["intended_risk_usd"] == pytest.approx(0.30)
    # never silent, never blocked on this path
    assert not [1 for ev, _ in fake.calls if ev == "vol_stop_resize_blocked"]
    # the floored telemetry discloses the tighten
    floored = [kw for ev, kw in fake.calls if ev == "vol_stop_floored"]
    assert len(floored) == 1
    assert floored[0]["floor_tightened"] is True
    assert floored[0]["resized"] is False
    assert floored[0]["stop_dist_pct_after"] == pytest.approx(0.4)


def test_floor_blocked_resize_tightens_stop_short(monkeypatch):
    fake = _FakeLogger()
    c = _Cand(size=0.75)
    c.side = "short"
    c.stop_price = 100.4
    c.tp1_price = 99.5
    _run(c, _cfg(), fake, monkeypatch)
    # mirrored: floored stop 102.0 -> tightened to constant-risk 100.4
    assert c.stop_price == pytest.approx(100.4)
    assert c.size == pytest.approx(0.75)
    assert c.tp1_price == pytest.approx(95.0)
    assert c.size * abs(c.entry_price - c.stop_price) == pytest.approx(0.30)
    hits = [kw for ev, kw in fake.calls if ev == "vol_stop_floor_tightened"]
    assert len(hits) == 1
    assert hits[0]["old_stop"] == pytest.approx(102.0)
    assert hits[0]["new_stop"] == pytest.approx(100.4)


def test_floor_blocked_min_ratio_clamp_still_tightens(monkeypatch):
    # FIX A clamp 0.75: target 0.75 x $75 = $56.25 < $80 floor -> floored
    # 0.8 >= 0.75 -> still floor-blocked -> tighten fires.
    fake = _FakeLogger()
    c = _Cand(size=0.75)
    _run(c, _cfg(vol_stop_resize_min_ratio=0.75), fake, monkeypatch)
    assert c.stop_price == pytest.approx(99.6)
    assert c.size == pytest.approx(0.75)
    assert [1 for ev, _ in fake.calls if ev == "vol_stop_floor_tightened"]


def test_knob_off_legacy_silent_skip(monkeypatch):
    # knob False = pre-fix bit-for-bit: wide stop rides the full size, no
    # tighten, no blocked event (the legacy silence).
    fake = _FakeLogger()
    c = _Cand(size=0.75)
    _run(c, _cfg(vol_stop_floor_tighten_enabled=False), fake, monkeypatch)
    assert c.stop_price == pytest.approx(98.0)      # wide floor stop applies
    assert c.size == pytest.approx(0.75)            # resize skipped
    assert not [1 for ev, _ in fake.calls
                if ev == "vol_stop_floor_tightened"]
    assert not [1 for ev, _ in fake.calls
                if ev == "vol_stop_resize_blocked"]


def test_resize_success_path_untouched(monkeypatch):
    # floored size still BELOW current size -> normal resize, no tighten.
    fake = _FakeLogger()
    c = _Cand(size=1.0)
    _run(c, _cfg(), fake, monkeypatch)
    assert c.size == pytest.approx(0.8)             # $80 floor / $100 entry
    assert c.stop_price == pytest.approx(98.0)      # wide stop kept
    assert not [1 for ev, _ in fake.calls
                if ev == "vol_stop_floor_tightened"]
    floored = [kw for ev, kw in fake.calls if ev == "vol_stop_floored"]
    assert floored[0]["resized"] is True
    assert floored[0]["floor_tightened"] is False


# ── Tighten-only invariant (pure helper) ─────────────────────────────────────

def test_tighten_never_loosens_live_stop():
    from main import _vol_stop_floor_tighten
    # computed constant-risk stop (99.6) is WIDER than the live stop (99.8)
    # -> live stop kept, long side.
    assert _vol_stop_floor_tighten(100.0, "long", 99.8, 1.0, 0.4) == \
        pytest.approx(99.8)
    # short mirror: computed 100.4 wider than live 100.2 -> keep live.
    assert _vol_stop_floor_tighten(100.0, "short", 100.2, 1.0, 0.4) == \
        pytest.approx(100.2)
    # BUY/SELL side aliases behave identically.
    assert _vol_stop_floor_tighten(100.0, "buy", 99.8, 1.0, 0.4) == \
        pytest.approx(99.8)
    assert _vol_stop_floor_tighten(100.0, "sell", 100.2, 1.0, 0.4) == \
        pytest.approx(100.2)


def test_tighten_computed_price_and_none_paths():
    from main import _vol_stop_floor_tighten
    # tighter than live -> computed price returned (both sides).
    assert _vol_stop_floor_tighten(100.0, "long", 98.0, 0.75, 0.3) == \
        pytest.approx(99.6)
    assert _vol_stop_floor_tighten(100.0, "short", 102.0, 0.75, 0.3) == \
        pytest.approx(100.4)
    # impossible tightens -> None (blocked, caller must log).
    assert _vol_stop_floor_tighten(0.0, "long", 98.0, 0.75, 0.3) is None
    assert _vol_stop_floor_tighten(100.0, "long", 98.0, 0.0, 0.3) is None
    assert _vol_stop_floor_tighten(100.0, "long", 98.0, 0.75, 0.0) is None
    # risk so large the stop falls through zero -> None.
    assert _vol_stop_floor_tighten(100.0, "long", 98.0, 1.0, 200.0) is None
    assert _vol_stop_floor_tighten(None, "long", 98.0, 0.75, 0.3) is None


# ── Defect 2: post-kelly sub-floor observability ─────────────────────────────

def test_sizing_below_floor_event_fires():
    from main import _emit_sizing_below_floor
    fake = _FakeLogger()
    # final post-kelly notional $49.71 under the $75 floor, kelly x0.66.
    assert _emit_sizing_below_floor(fake, "OP-USD", 49.71, 75.0, 0.66) is True
    hits = [kw for ev, kw in fake.calls if ev == "sizing_below_floor"]
    assert len(hits) == 1
    assert hits[0]["symbol"] == "OP-USD"
    assert hits[0]["final_notional"] == pytest.approx(49.71)
    assert hits[0]["floor"] == pytest.approx(75.0)
    assert hits[0]["kelly_factor"] == pytest.approx(0.66)


def test_sizing_below_floor_silent_at_or_above_floor():
    from main import _emit_sizing_below_floor
    fake = _FakeLogger()
    assert _emit_sizing_below_floor(fake, "OP-USD", 75.0, 75.0, 0.66) is False
    assert _emit_sizing_below_floor(fake, "OP-USD", 120.0, 75.0, 1.0) is False
    assert _emit_sizing_below_floor(fake, "OP-USD", 0.0, 75.0, 0.66) is False
    assert _emit_sizing_below_floor(fake, "OP-USD", None, 75.0, 0.66) is False
    assert fake.calls == []
