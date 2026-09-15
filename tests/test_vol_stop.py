import asyncio
import time

import pytest

from data.candle_buffer import Candle
from intelligence.vol_stop import (
    abstain_reason, apply_vol_floors, atr_multiplier, wilder_atr,
)

H = 3_600_000
BAR = 4 * H
T0 = 1_758_000_000_000  # fixed epoch ms, 4h-aligned


def _bar(i: int, o=100.0, h=101.0, l=99.0, c=100.5, v=10.0) -> Candle:
    t = T0 + i * BAR
    return Candle(open_time=t, open=o, high=h, low=l, close=c, volume=v,
                  close_time=t + BAR - 1)


def _flat_series(closes):
    """h=l=o=c per bar — TR reduces to |close - prev_close|."""
    return [_bar(i, o=c, h=c, l=c, c=c) for i, c in enumerate(closes)]


def _const_tr_bars(n: int, tr: float = 1.0):
    """n bars, every true range exactly `tr` (close steps by tr each bar)."""
    return _flat_series([100.0 + i * tr for i in range(n)])


# ── Wilder ATR math ─────────────────────────────────────────────────────────

def test_wilder_atr_hand_computed_sma_and_smoothing():
    # closes step +1 for 14 bars then +15: TRs = 1x14 then 15.
    closes = [100.0 + i for i in range(15)] + [129.0]
    bars15 = _flat_series(closes[:15])
    # first ATR = SMA of first 14 TRs = 14*1/14 = 1.0
    assert wilder_atr(bars15, period=14) == pytest.approx(1.0)
    # one Wilder step: (1.0*13 + 15)/14 = 2.0
    assert wilder_atr(_flat_series(closes), period=14) == pytest.approx(2.0)


def test_wilder_atr_true_range_legs():
    # Hand-computed per-bar TR exercising all three max() legs.
    bars = [_bar(0, o=100, h=100, l=100, c=100)]
    bars.append(_bar(1, o=100, h=103, l=99, c=101))    # TR=max(4,3,1)=4
    bars.append(_bar(2, o=101, h=104, l=100, c=103))   # TR=max(4,3,1)=4
    bars.append(_bar(3, o=103, h=102, l=98, c=99))     # TR=max(4,1,5)=5
    bars.append(_bar(4, o=99, h=110, l=105, c=108))    # TR=max(5,11,6)=11
    for i in range(5, 15):                             # 10 bars, TR=2 each
        bars.append(_bar(i, o=108, h=109, l=107, c=108))
    # SMA = (4+4+5+11+10*2)/14 = 44/14
    assert wilder_atr(bars, period=14) == pytest.approx(44.0 / 14.0)
    bars.append(_bar(15, o=108, h=112, l=110, c=111))  # TR=max(2,4,2)=4
    # Wilder step: (44/14*13 + 4)/14 = 157/49
    assert wilder_atr(bars, period=14) == pytest.approx(157.0 / 49.0)


def test_wilder_atr_needs_period_plus_one_bars():
    assert wilder_atr(_const_tr_bars(14), period=14) is None
    assert wilder_atr(_const_tr_bars(15), period=14) is not None
    assert wilder_atr(None) is None
    assert wilder_atr([], period=14) is None


def test_wilder_atr_rejects_degenerate_bars():
    bars = _const_tr_bars(16)
    bars[7] = _bar(7, o=100, h=90, l=99, c=95)  # high < low
    assert wilder_atr(bars) is None


# ── Multiplier ladder ───────────────────────────────────────────────────────

def test_multiplier_ladder_boundaries():
    assert atr_multiplier(29.9) == 1.5
    assert atr_multiplier(30.0) == 2.0
    assert atr_multiplier(60.0) == 2.0
    assert atr_multiplier(60.1) == 2.5
    assert atr_multiplier(None) == 2.0   # thin data = normal regime


# ── Floor geometry ──────────────────────────────────────────────────────────

def _bars_atr1():
    return _const_tr_bars(20, tr=1.0)  # ATR = 1.0


def test_long_floor_widens_stop_and_tp():
    res = apply_vol_floors(100.0, "long", 99.6, 100.5, _bars_atr1(), None)
    assert res is not None and res.floored
    # stop floor: 2.0 x ATR 1.0 = 2.0 below entry
    assert res.floored_stop == pytest.approx(98.0)
    # TP floor: 2.5 x final stop dist 2.0 = 5.0 above entry
    assert res.floored_tp1 == pytest.approx(105.0)
    assert res.stop_dist_pct == pytest.approx(0.02)
    assert res.atr_pct == pytest.approx(0.01)
    assert res.multiplier == 2.0


def test_short_floor_mirrored():
    res = apply_vol_floors(100.0, "short", 100.4, 99.5, _bars_atr1(), None)
    assert res is not None and res.floored
    assert res.floored_stop == pytest.approx(102.0)
    assert res.floored_tp1 == pytest.approx(95.0)


def test_high_vol_rank_widens_further():
    # exactly at the floor -> untouched
    res = apply_vol_floors(100.0, "long", 97.5, 110.0, _bars_atr1(), 80.0)
    assert res is not None and not res.floored
    assert res.floored_stop == pytest.approx(97.5)      # 2.5 x 1.0 = 2.5 floor
    assert res.floored_tp1 == pytest.approx(110.0)      # 2.5 x 2.5 = 6.25 < 10
    res2 = apply_vol_floors(100.0, "long", 98.0, 103.0, _bars_atr1(), 80.0)
    assert res2.floored
    assert res2.floored_stop == pytest.approx(97.5)
    assert res2.floored_tp1 == pytest.approx(106.25)


def test_only_widens_invariant():
    # existing geometry wider than both floors -> untouched, floored=False
    res = apply_vol_floors(100.0, "long", 97.0, 108.0, _bars_atr1(), None)
    assert res is not None and not res.floored
    assert res.floored_stop == 97.0 and res.floored_tp1 == 108.0
    res_s = apply_vol_floors(100.0, "short", 103.0, 92.0, _bars_atr1(), None)
    assert res_s is not None and not res_s.floored
    assert res_s.floored_stop == 103.0 and res_s.floored_tp1 == 92.0


def test_tp_floor_raises_tight_tp_leaves_wide_stop():
    # stop already wide (3.0) but TP1 at 1R — TP1 raised to 2.5 x 3.0
    res = apply_vol_floors(100.0, "long", 97.0, 103.0, _bars_atr1(), None)
    assert res is not None and res.floored
    assert res.floored_stop == 97.0
    assert res.floored_tp1 == pytest.approx(107.5)
    res_s = apply_vol_floors(100.0, "short", 103.0, 97.0, _bars_atr1(), None)
    assert res_s is not None and res_s.floored
    assert res_s.floored_stop == 103.0
    assert res_s.floored_tp1 == pytest.approx(92.5)


# ── Abstain paths ───────────────────────────────────────────────────────────

def test_abstain_none_candles_and_thin():
    assert apply_vol_floors(100.0, "long", 99.6, 100.5, None, None) is None
    assert apply_vol_floors(100.0, "long", 99.6, 100.5, [], None) is None
    assert apply_vol_floors(100.0, "long", 99.6, 100.5,
                            _const_tr_bars(14), None) is None


def test_abstain_zero_entry_and_missing_legs():
    bars = _bars_atr1()
    assert apply_vol_floors(0.0, "long", 99.6, 100.5, bars, None) is None
    assert apply_vol_floors(100.0, "long", 0.0, 100.5, bars, None) is None
    assert apply_vol_floors(100.0, "long", 99.6, 0.0, bars, None) is None


def test_abstain_inverted_geometry_never_repairs():
    bars = _bars_atr1()
    assert apply_vol_floors(100.0, "long", 101.0, 105.0, bars, None) is None
    assert apply_vol_floors(100.0, "short", 99.0, 95.0, bars, None) is None
    assert apply_vol_floors(100.0, "long", 99.0, 99.5, bars, None) is None


def test_abstain_reason_map():
    bars = _bars_atr1()
    assert abstain_reason(100.0, "long", 99.6, 100.5, None) == "no_bars"
    assert abstain_reason(100.0, "long", 99.6, 100.5,
                          _const_tr_bars(10)) == "atr_thin"
    assert abstain_reason(0.0, "long", 99.6, 100.5, bars) == "degenerate"
    assert abstain_reason(100.0, "long", 99.6, 100.5, bars) is None


def test_abstain_scale_poisoned_plane():
    # ATR > 25% of entry = plane defect (SPCX-class rebase mid-window), not
    # volatility — abstain rather than floor a stop through zero (review P1).
    bars = [Candle(open_time=i * 14_400_000, open=100.0, high=130.0, low=70.0,
                   close=100.0, volume=1.0, close_time=i * 14_400_000 + 1)
            for i in range(1, 20)]
    assert apply_vol_floors(100.0, "long", 99.6, 100.5, bars, None) is None
    assert abstain_reason(100.0, "long", 99.6, 100.5, bars) == "degenerate"


def test_tp1_floor_never_leapfrogs_tp2():
    # TP1 would raise to 107.5 but TP2 sits at 105 — TP1 keeps the original;
    # a TP2 beyond the raise lets it through (review P2).
    res = apply_vol_floors(100.0, "long", 97.0, 103.0, _bars_atr1(), None,
                           tp2=105.0)
    assert res is not None and not res.floored
    assert res.floored_tp1 == 103.0
    res_s = apply_vol_floors(100.0, "short", 103.0, 97.0, _bars_atr1(), None,
                             tp2=95.0)
    assert res_s is not None and not res_s.floored
    assert res_s.floored_tp1 == 97.0
    res_ok = apply_vol_floors(100.0, "long", 97.0, 103.0, _bars_atr1(), None,
                              tp2=108.0)
    assert res_ok is not None and res_ok.floored
    assert res_ok.floored_tp1 == pytest.approx(107.5)


# ── Splice wiring pins ──────────────────────────────────────────────────────

class _Cand:
    symbol = "VOLTEST-USD"
    side = "long"
    entry_price = 100.0
    stop_price = 99.6
    tp1_price = 100.5
    coherence_score = 5.0


def test_splice_disabled_is_bit_for_bit(monkeypatch):
    monkeypatch.setenv("VOL_STOP_ENABLED", "false")
    from main import _vol_stop_splice
    c = _Cand()
    asyncio.run(_vol_stop_splice(c, {}, None))
    assert c.stop_price == 99.6 and c.tp1_price == 100.5


def test_splice_floors_via_cache_and_shadow_records(monkeypatch):
    monkeypatch.delenv("VOL_STOP_ENABLED", raising=False)
    import main as m
    bars = _bars_atr1()
    m._VOL_STOP_CACHE["VOLTEST-USD"] = (time.time(), bars, None)
    committed = []

    class _FakeShadow:
        def record_exit_counterfactual(self, symbol, direction, *, gate,
                                       reason="", stop=0.0, coherence=0.0,
                                       regime=""):
            committed.append((symbol, direction, gate, stop))

    monkeypatch.setattr(m, "_shadow_journal", _FakeShadow())
    c = _Cand()
    asyncio.run(m._vol_stop_splice(c, {}, None))
    assert c.stop_price == pytest.approx(98.0)
    assert c.tp1_price == pytest.approx(105.0)
    # shadow counterfactual carries the ORIGINAL tight stop under gate vol_stop
    assert committed == [("VOLTEST-USD", "long", "vol_stop", 99.6)]
    m._VOL_STOP_CACHE.pop("VOLTEST-USD", None)


# ── P1b re-size (Governor 2026-09-15, option B) ─────────────────────────────

class _SizedCand:
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


def test_p1b_resize_holds_risk_constant():
    # floor: 0.4% -> 2.0% stop (ratio 0.2); notional 500 -> 100 >= $80 floor
    import main as m
    c = _SizedCand(size=5.0)
    m._VOL_STOP_CACHE["VOLTEST-USD"] = (time.time(), _bars_atr1(), None)
    try:
        asyncio.run(m._vol_stop_splice(c, {}, None))
    finally:
        m._VOL_STOP_CACHE.pop("VOLTEST-USD", None)
    assert c.stop_price == pytest.approx(98.0)
    assert c.size == pytest.approx(1.0)            # 500 x 0.2 / 100
    assert c.initial_margin == pytest.approx(20.0)  # 1.0 x 100 / 5
    # USD risk held constant: 5.0 x 0.4 == 1.0 x 2.0
    assert c.size * abs(c.entry_price - c.stop_price) == pytest.approx(2.0)


def test_p1b_resize_floored_at_venue_min():
    # constant-risk size would be 0.2 ($20 < $80 SoDEX floor) -> floor binds
    import main as m
    c = _SizedCand(size=1.0)
    m._VOL_STOP_CACHE["VOLTEST-USD"] = (time.time(), _bars_atr1(), None)
    try:
        asyncio.run(m._vol_stop_splice(c, {}, None))
    finally:
        m._VOL_STOP_CACHE.pop("VOLTEST-USD", None)
    assert c.size == pytest.approx(0.8)            # $80 floor / 100 entry
    assert c.stop_price == pytest.approx(98.0)


def test_p1b_resize_kill_switch_bit_for_bit():
    from types import SimpleNamespace
    import main as m
    c = _SizedCand(size=5.0)
    cfg = SimpleNamespace(vol_stop_enabled=True, vol_stop_resize_enabled=False)
    m._VOL_STOP_CACHE["VOLTEST-USD"] = (time.time(), _bars_atr1(), None)
    try:
        asyncio.run(m._vol_stop_splice(c, {}, cfg))
    finally:
        m._VOL_STOP_CACHE.pop("VOLTEST-USD", None)
    assert c.stop_price == pytest.approx(98.0)     # floor still applies
    assert c.size == 5.0                            # size untouched
    assert c.initial_margin == pytest.approx(100.0)


def test_p1b_tp_only_floor_leaves_size():
    # stop already wide (97.0 = 3.0 dist) but TP1 tight -> TP floor only,
    # no stop widening -> ratio branch skipped, size untouched
    import main as m
    c = _SizedCand(size=5.0)
    c.stop_price = 97.0
    c.tp1_price = 103.0
    m._VOL_STOP_CACHE["VOLTEST-USD"] = (time.time(), _bars_atr1(), None)
    try:
        asyncio.run(m._vol_stop_splice(c, {}, None))
    finally:
        m._VOL_STOP_CACHE.pop("VOLTEST-USD", None)
    assert c.stop_price == 97.0
    assert c.tp1_price == pytest.approx(107.5)
    assert c.size == 5.0
