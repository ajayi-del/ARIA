"""CrossSideScanner: intent consumption, standing scan, budget clamp, level
geometry, dedup registry — doctrine pins. Zero I/O, no main.py import."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.cross_side_scanner import (  # noqa: E402
    CrossSideScanner, CounterProbeSpec, SCAN_ROE_RUNG, STOP_BEYOND_PCT,
    TP_RR,
)


def _cfg(**kw):
    base = dict(cross_side_scanner_enabled=True,
                cross_side_budget_frac=0.30,
                cross_side_budget_floor_usd=2.0,
                cross_side_budget_cap_usd=8.0,
                cross_side_level_band_pct=2.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _intent(kind="cross_side", position_id="P1"):
    return SimpleNamespace(kind=kind, position_id=position_id)


def _winner(**kw):
    base = dict(symbol="BTC-USD", side="long", entry=100.0, mark=100.0,
                initial_margin=100.0, unrealized_pnl=10.0)
    base.update(kw)
    return base


# ── 1. Master gate ───────────────────────────────────────────────────────────

def test_disabled_intent_none():
    sc = CrossSideScanner()
    assert sc.on_cross_side_intent(
        _cfg(cross_side_scanner_enabled=False), intent=_intent(),
        winner=_winner(), levels=[101.0]) is None


def test_disabled_intent_no_registry_mutation():
    sc = CrossSideScanner()
    sc.on_cross_side_intent(_cfg(cross_side_scanner_enabled=False),
                            intent=_intent(), winner=_winner(),
                            levels=[101.0])
    assert sc.live_probes() == 0


def test_disabled_scan_empty_no_mutation():
    sc = CrossSideScanner()
    out = sc.scan(_cfg(cross_side_scanner_enabled=False),
                  positions=[_winner(roe_pct=50.0)], marks={"BTC-USD": 100.0},
                  levels_by_symbol={"BTC-USD": [101.0]}, now_ts=1.0)
    assert out == []
    assert sc.live_probes() == 0


# ── 2. Intent path ───────────────────────────────────────────────────────────

def test_wrong_intent_kind_none():
    sc = CrossSideScanner()
    assert sc.on_cross_side_intent(_cfg(), intent=_intent(kind="pyramid"),
                                   winner=_winner(), levels=[101.0]) is None


def test_intent_arms_short_probe_for_long_winner():
    sc = CrossSideScanner()
    spec = sc.on_cross_side_intent(_cfg(), intent=_intent(),
                                   winner=_winner(), levels=[101.0])
    assert isinstance(spec, CounterProbeSpec)
    assert spec.side == "short"
    assert spec.source == "intent"
    assert spec.winner_position_id == "P1"


def test_intent_upnl_zero_none():
    sc = CrossSideScanner()
    assert sc.on_cross_side_intent(
        _cfg(), intent=_intent(), winner=_winner(unrealized_pnl=0.0),
        levels=[101.0]) is None


def test_intent_upnl_negative_none():
    sc = CrossSideScanner()
    assert sc.on_cross_side_intent(
        _cfg(), intent=_intent(), winner=_winner(unrealized_pnl=-3.0),
        levels=[101.0]) is None


def test_intent_winner_missing_keys_none():
    sc = CrossSideScanner()
    assert sc.on_cross_side_intent(_cfg(), intent=_intent(),
                                   winner={"symbol": "BTC-USD"},
                                   levels=[101.0]) is None
    assert sc.on_cross_side_intent(_cfg(), intent=_intent(), winner=None,
                                   levels=[101.0]) is None


# ── 3. Budget clamp ──────────────────────────────────────────────────────────

def test_budget_raw_frac():
    sc = CrossSideScanner()
    spec = sc.on_cross_side_intent(_cfg(), intent=_intent(),
                                   winner=_winner(unrealized_pnl=20.0),
                                   levels=[101.0])
    assert spec.budget_usd == 6.0


def test_budget_floor():
    # Re-encoded 2026-09-26 (cross-review P1): the floor is a STANDDOWN
    # floor, never a top-up — frac×uPnL below the floor means the winner is
    # not winning enough to fund the probe; topping up would spend principal
    # ($2 against $0.30 of actual open profit). Old pin expected the top-up.
    sc = CrossSideScanner()
    spec = sc.on_cross_side_intent(_cfg(), intent=_intent(),
                                   winner=_winner(unrealized_pnl=1.0),
                                   levels=[101.0])
    assert spec is None


def test_budget_cap():
    sc = CrossSideScanner()
    spec = sc.on_cross_side_intent(_cfg(), intent=_intent(),
                                   winner=_winner(unrealized_pnl=100.0),
                                   levels=[101.0])
    assert spec.budget_usd == 8.0


def test_budget_knobs_respected():
    sc = CrossSideScanner()
    spec = sc.on_cross_side_intent(
        _cfg(cross_side_budget_frac=0.5, cross_side_budget_cap_usd=4.0,
             cross_side_budget_floor_usd=1.0),
        intent=_intent(), winner=_winner(unrealized_pnl=20.0),
        levels=[101.0])
    assert spec.budget_usd == 4.0


# ── 4. Level geometry ────────────────────────────────────────────────────────

def test_long_winner_needs_level_above_mark():
    sc = CrossSideScanner()
    # short probe: levels below the mark are the wrong side
    assert sc.on_cross_side_intent(_cfg(), intent=_intent(),
                                   winner=_winner(), levels=[99.0]) is None


def test_short_winner_needs_level_below_mark():
    sc = CrossSideScanner()
    spec = sc.on_cross_side_intent(_cfg(), intent=_intent(),
                                   winner=_winner(side="short"),
                                   levels=[99.0])
    assert spec.side == "long"
    assert spec.entry_price == 99.0


def test_no_levels_none():
    sc = CrossSideScanner()
    assert sc.on_cross_side_intent(_cfg(), intent=_intent(),
                                   winner=_winner(), levels=[]) is None
    assert sc.on_cross_side_intent(_cfg(), intent=_intent(),
                                   winner=_winner(), levels=None) is None


def test_level_outside_band_none():
    sc = CrossSideScanner()
    # 5% away with a 2% band
    assert sc.on_cross_side_intent(_cfg(), intent=_intent(),
                                   winner=_winner(), levels=[105.0]) is None


def test_level_at_band_edge_accepted():
    sc = CrossSideScanner()
    spec = sc.on_cross_side_intent(_cfg(), intent=_intent(),
                                   winner=_winner(), levels=[102.0])
    assert spec.entry_price == 102.0


def test_band_knob_respected():
    sc = CrossSideScanner()
    assert sc.on_cross_side_intent(
        _cfg(cross_side_level_band_pct=1.0), intent=_intent(),
        winner=_winner(), levels=[101.5]) is None


def test_nearest_level_chosen():
    sc = CrossSideScanner()
    spec = sc.on_cross_side_intent(_cfg(), intent=_intent(),
                                   winner=_winner(),
                                   levels=[101.8, 100.5, 101.2])
    assert spec.entry_price == 100.5


def test_dict_levels_accepted():
    sc = CrossSideScanner()
    spec = sc.on_cross_side_intent(
        _cfg(), intent=_intent(), winner=_winner(),
        levels=[{"price": 101.0, "strength": 3}])
    assert spec.entry_price == 101.0


def test_nonpositive_levels_ignored():
    sc = CrossSideScanner()
    assert sc.on_cross_side_intent(_cfg(), intent=_intent(),
                                   winner=_winner(),
                                   levels=[0.0, -5.0, "x", None]) is None


def test_short_probe_stop_and_tp_geometry():
    sc = CrossSideScanner()
    spec = sc.on_cross_side_intent(_cfg(), intent=_intent(),
                                   winner=_winner(), levels=[101.0])
    risk = 101.0 * STOP_BEYOND_PCT / 100.0
    assert abs(spec.stop_price - (101.0 + risk)) < 1e-9
    assert abs(spec.take_profit_price - (101.0 - TP_RR * risk)) < 1e-9
    assert spec.rr >= 3.0
    assert spec.stop_price > spec.entry_price > spec.take_profit_price


def test_long_probe_stop_and_tp_geometry():
    sc = CrossSideScanner()
    spec = sc.on_cross_side_intent(_cfg(), intent=_intent(),
                                   winner=_winner(side="short"),
                                   levels=[99.0])
    risk = 99.0 * STOP_BEYOND_PCT / 100.0
    assert abs(spec.stop_price - (99.0 - risk)) < 1e-9
    assert abs(spec.take_profit_price - (99.0 + TP_RR * risk)) < 1e-9
    assert spec.stop_price < spec.entry_price < spec.take_profit_price


# ── 5. Standing scan ─────────────────────────────────────────────────────────

def test_scan_arms_for_roe_winner():
    sc = CrossSideScanner()
    out = sc.scan(_cfg(), positions=[_winner()], marks={"BTC-USD": 100.0},
                  levels_by_symbol={"BTC-USD": [101.0]}, now_ts=5.0)
    assert len(out) == 1
    assert out[0].source == "scan"
    assert out[0].created_ts == 5.0


def test_scan_below_roe_rung_skipped():
    sc = CrossSideScanner()
    out = sc.scan(_cfg(),
                  positions=[_winner(unrealized_pnl=5.0,
                                     initial_margin=100.0)],
                  marks={"BTC-USD": 100.0},
                  levels_by_symbol={"BTC-USD": [101.0]}, now_ts=5.0)
    assert out == []


def test_scan_roe_computed_from_upnl_over_margin():
    # 10% of margin exactly → arms
    sc = CrossSideScanner()
    out = sc.scan(_cfg(),
                  positions=[_winner(unrealized_pnl=10.0,
                                     initial_margin=100.0)],
                  marks={"BTC-USD": 100.0},
                  levels_by_symbol={"BTC-USD": [101.0]}, now_ts=5.0)
    assert len(out) == 1
    assert SCAN_ROE_RUNG == 10.0


def test_scan_zero_margin_falls_back_to_roe_field():
    sc = CrossSideScanner()
    pos = _winner(initial_margin=0.0, roe_pct=25.0)
    out = sc.scan(_cfg(), positions=[pos], marks={"BTC-USD": 100.0},
                  levels_by_symbol={"BTC-USD": [101.0]}, now_ts=5.0)
    assert len(out) == 1


def test_scan_zero_margin_no_roe_field_skipped():
    sc = CrossSideScanner()
    pos = _winner(initial_margin=0.0)
    assert sc.scan(_cfg(), positions=[pos], marks={"BTC-USD": 100.0},
                   levels_by_symbol={"BTC-USD": [101.0]}, now_ts=5.0) == []


def test_scan_marks_dict_overrides_position_mark():
    # position mark stale at 100; live mark 110 → level 111 in band of 110
    sc = CrossSideScanner()
    pos = _winner(mark=100.0, unrealized_pnl=10.0)
    out = sc.scan(_cfg(), positions=[pos], marks={"BTC-USD": 110.0},
                  levels_by_symbol={"BTC-USD": [111.0]}, now_ts=5.0)
    assert len(out) == 1
    assert out[0].entry_price == 111.0


def test_scan_missing_mark_skipped():
    sc = CrossSideScanner()
    pos = _winner()
    del pos["mark"]
    assert sc.scan(_cfg(), positions=[pos], marks={},
                   levels_by_symbol={"BTC-USD": [101.0]}, now_ts=5.0) == []


def test_scan_no_levels_for_symbol_skipped():
    sc = CrossSideScanner()
    assert sc.scan(_cfg(), positions=[_winner()], marks={"BTC-USD": 100.0},
                   levels_by_symbol={}, now_ts=5.0) == []


def test_scan_multiple_symbols_multiple_probes():
    sc = CrossSideScanner()
    positions = [_winner(), _winner(symbol="ETH-USD", side="short")]
    out = sc.scan(_cfg(), positions=positions,
                  marks={"BTC-USD": 100.0, "ETH-USD": 50.0},
                  levels_by_symbol={"BTC-USD": [101.0],
                                    "ETH-USD": [49.5]},
                  now_ts=5.0)
    assert {(s.symbol, s.side) for s in out} == \
        {("BTC-USD", "short"), ("ETH-USD", "long")}


# ── 6. Dedup registry ────────────────────────────────────────────────────────

def test_scan_dedup_same_symbol_counter_side():
    sc = CrossSideScanner()
    positions = [_winner(position_id="A"), _winner(position_id="B")]
    out = sc.scan(_cfg(), positions=positions, marks={"BTC-USD": 100.0},
                  levels_by_symbol={"BTC-USD": [101.0]}, now_ts=5.0)
    assert len(out) == 1
    # second scan with the positions still open → nothing new
    out2 = sc.scan(_cfg(), positions=positions, marks={"BTC-USD": 100.0},
                   levels_by_symbol={"BTC-USD": [101.0]}, now_ts=6.0)
    assert out2 == []
    assert sc.live_probes() == 1


def test_intent_registration_blocks_scan_dedup():
    sc = CrossSideScanner()
    sc.on_cross_side_intent(_cfg(), intent=_intent(), winner=_winner(),
                            levels=[101.0])
    out = sc.scan(_cfg(), positions=[_winner()], marks={"BTC-USD": 100.0},
                  levels_by_symbol={"BTC-USD": [101.0]}, now_ts=5.0)
    assert out == []
    assert sc.is_live("BTC-USD", "short")


def test_release_frees_slot():
    sc = CrossSideScanner()
    sc.on_cross_side_intent(_cfg(), intent=_intent(), winner=_winner(),
                            levels=[101.0])
    assert sc.release("BTC-USD", "short") is True
    out = sc.scan(_cfg(), positions=[_winner()], marks={"BTC-USD": 100.0},
                  levels_by_symbol={"BTC-USD": [101.0]}, now_ts=6.0)
    assert len(out) == 1


def test_release_unknown_key_false():
    sc = CrossSideScanner()
    assert sc.release("NOPE-USD", "short") is False


def test_registry_bounded_fifo_eviction():
    sc = CrossSideScanner(max_live=3)
    for i in range(5):
        sym = f"S{i}-USD"
        sc.scan(_cfg(), positions=[_winner(symbol=sym)],
                marks={sym: 100.0}, levels_by_symbol={sym: [101.0]},
                now_ts=5.0)
    assert sc.live_probes() == 3
    assert not sc.is_live("S0-USD", "short")
    assert sc.is_live("S4-USD", "short")


def test_constants_locked():
    assert STOP_BEYOND_PCT == 0.4
    assert TP_RR == 3.0
