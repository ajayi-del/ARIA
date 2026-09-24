"""Clamp-RR verdict pins (Governor 2026-09-18, Phase 1).

_clamp_tp_to_sodex_range runs AFTER the build_candidate min-RR gate, so
brackets with TP2 ~0.1-0.2% from entry against an ATR-sized stop (inverted
R:R ~0.02-0.07:1) reached the exchange. The clamp now returns a verdict
dict when the post-clamp TP2/stop R:R falls below config.sodex_clamp_min_rr;
the three place_bracket call sites refuse the entry on a verdict (kill
switch sodex_clamp_rr_gate_enabled). Governor 2026-09-24: the min-RR is a
bracket CONSTRUCTION rule — TP2 is floored at entry ± min_rr × actual risk
before the verdict (clamp_rr_constructive_enabled), so a TP clamp can never
push a valid bracket below the floor and clamp_rr_below_min is
unreachable-by-clamp; the verdict survives only for degenerate geometry.
All pre-existing clamp behavior —
campaign bypass caps, the 2026-07-26 never-clamp-past-entry guard, the
missing/degenerate high-low early returns — is pinned bit-for-bit here.
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


@pytest.fixture
def _cfg(monkeypatch):
    """Deterministic min-RR knob; restore the real lazy cache after."""
    saved = main._clamp_rr_config
    main._clamp_rr_config = SimpleNamespace(sodex_clamp_min_rr=1.0)
    yield main._clamp_rr_config
    main._clamp_rr_config = saved


def _cand(side="long", symbol="BTC-USD", entry=99.4, stop=98.4,
          tp1=101.0, tp2=102.0):
    return SimpleNamespace(symbol=symbol, side=side, entry_price=entry,
                           stop_price=stop, tp1_price=tp1, tp2_price=tp2)


def _state(high=100.0, low=90.0):
    return SimpleNamespace(sodex_high_24h=high, sodex_low_24h=low)


class TestVerdict:
    def test_long_at_24h_high_constructive_floor(self, _cfg):
        # Governor 2026-09-24 constructive rule: entry just under the high —
        # caps 99.5/99.8 bind, then TP2 is FLOORED at entry + min_rr × risk
        # (99.4 + 1.0 × 1.0 = 100.4). R:R preserved by construction; no verdict.
        c = _cand()
        v = main._clamp_tp_to_sodex_range(c, _state(), campaign_symbol="SPCX-USD")
        assert c.tp1_price == pytest.approx(100.0 * 0.995)   # clamped
        assert c.tp2_price == pytest.approx(100.4)           # constructive floor
        assert v is None                                     # repaired, not rejected

    def test_constructive_disabled_legacy_verdict(self, _cfg):
        # Kill switch False = pre-2026-09-24 bit-for-bit: clamp then reject.
        main._clamp_rr_config = SimpleNamespace(
            sodex_clamp_min_rr=1.0, clamp_rr_constructive_enabled=False)
        c = _cand()
        v = main._clamp_tp_to_sodex_range(c, _state(), campaign_symbol="SPCX-USD")
        assert c.tp1_price == pytest.approx(100.0 * 0.995)
        assert c.tp2_price == pytest.approx(100.0 * 0.998)
        assert v is not None
        assert v["reason"] == "clamp_rr_below_min"
        assert v["post_rr"] == pytest.approx(0.4, abs=1e-3)
        assert v["pre_rr"] == pytest.approx(2.6, abs=1e-3)
        assert v["entry"] == 99.4 and v["stop"] == 98.4
        assert v["tp2"] == pytest.approx(99.8)
        assert v["high_24h"] == 100.0 and v["low_24h"] == 90.0
        assert v["campaign"] is False

    def test_constructive_ladder_invariant(self, _cfg):
        # TP1 above the R:R floor: TP2 lifts to TP1 (flat), never crosses it.
        c = _cand(entry=99.4, stop=98.4, tp1=100.6, tp2=99.8)
        v = main._clamp_tp_to_sodex_range(c, _state(high=110.0, low=90.0))
        assert c.tp2_price == pytest.approx(100.6)
        assert v is None

    def test_long_mid_range_untouched_no_verdict(self, _cfg):
        c = _cand(entry=95.0, stop=94.0, tp1=96.0, tp2=97.0)
        v = main._clamp_tp_to_sodex_range(c, _state(), campaign_symbol="SPCX-USD")
        assert (c.tp1_price, c.tp2_price) == (96.0, 97.0)     # no mutation
        assert v is None

    def test_short_at_24h_low_mirror_constructive(self, _cfg):
        # Mirror: floor = entry − min_rr × risk = 100.6 − 1.0 = 99.6; TP2
        # lowered to min(99.6, tp1=100.5) = 99.6; no verdict.
        c = _cand(side="short", entry=100.6, stop=101.6, tp1=99.0, tp2=98.0)
        v = main._clamp_tp_to_sodex_range(c, _state(high=110.0, low=100.0),
                                          campaign_symbol="SPCX-USD")
        assert c.tp1_price == pytest.approx(100.0 * 1.005)   # clamped
        assert c.tp2_price == pytest.approx(99.6)            # constructive floor
        assert v is None

    def test_campaign_wider_caps_preserved(self, _cfg):
        # Campaign TP2 cap = high*1.010: TP2 102 clamps to 101.0; post_rr
        # 1.6 clears the floor -> clamp applied, no verdict.
        c = _cand(symbol="SPCX-USD")
        v = main._clamp_tp_to_sodex_range(c, _state(), campaign_symbol="SPCX-USD")
        assert c.tp1_price == 101.0                          # under 1.015 cap
        assert c.tp2_price == pytest.approx(100.0 * 1.010)
        assert v is None

    def test_campaign_constructive_floor(self, _cfg):
        # Entry 100.5, stop 99.5: campaign cap clamps TP2 to 101.0 (0.5R),
        # constructive floor lifts it to 100.5 + 1.0 = 101.5; no verdict.
        c = _cand(symbol="SPCX-USD", entry=100.5, stop=99.5)
        v = main._clamp_tp_to_sodex_range(c, _state(), campaign_symbol="SPCX-USD")
        assert c.tp2_price == pytest.approx(101.5)
        assert v is None

    def test_never_clamp_past_entry_guard_intact(self, _cfg):
        # Entry above both caps (2026-07-26 dust guard): TPs must NOT move.
        c = _cand(entry=100.5, stop=99.5)
        v = main._clamp_tp_to_sodex_range(c, _state(), campaign_symbol="SPCX-USD")
        assert (c.tp1_price, c.tp2_price) == (101.0, 102.0)
        assert v is None                                     # post_rr 1.5

    def test_degenerate_geometry_fail_open(self, _cfg):
        c = _cand(stop=99.4)                                 # entry == stop
        assert main._clamp_tp_to_sodex_range(c, _state()) is None
        c2 = _cand(stop=0.0)                                 # zero stop
        assert main._clamp_tp_to_sodex_range(c2, _state()) is None
        c3 = _cand(side="short", entry=99.4, stop=98.4)      # stop wrong side
        assert main._clamp_tp_to_sodex_range(c3, _state()) is None

    def test_min_rr_zero_never_fires(self, _cfg):
        main._clamp_rr_config = SimpleNamespace(sodex_clamp_min_rr=0.0)
        c = _cand()
        v = main._clamp_tp_to_sodex_range(c, _state())
        assert c.tp2_price == pytest.approx(99.8)            # clamp still ran
        assert v is None

    def test_missing_high_low_legacy(self, _cfg):
        c = _cand()
        assert main._clamp_tp_to_sodex_range(c, SimpleNamespace()) is None
        assert (c.tp1_price, c.tp2_price) == (101.0, 102.0)  # untouched
        st = SimpleNamespace(sodex_high_24h=90.0, sodex_low_24h=100.0)
        assert main._clamp_tp_to_sodex_range(c, st) is None  # high <= low
