"""Tests for tools/cascade_census.py — the event-counted conditional cascade
probability census (Governor spec 2026-09-15).

No network: the VERBATIM live-probe fixtures (2026-09-15) are the spec. Pins:
series alignment/ffill on ragged fixtures; each score leg independently +
boundaries (funding exactly 0 NOT negative; L/S exactly 1.8 NOT >1.8);
cascade event detection at the -3%/-2% boundaries (inclusive); score>=4
gating; pre-cascade-bar scoring (no look-ahead); forward-label math;
insufficient-forward-data skip; Wilson CI at n=10 k=5 and n=20 k=10 and n=0;
thin_sample at 49 vs 50; hour histogram; pooled sums; atomic write."""
import importlib.util
import json
import os

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools", "cascade_census.py")
_spec = importlib.util.spec_from_file_location("cascade_census", _PATH)
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)

H = 3_600_000
BASE_TS = 1789459200000  # hour-aligned; (BASE_TS // H) % 24 == 8


# ── verbatim fixtures (live-probed 2026-09-15) ───────────────────────────────

KLINE_FIXTURE = {"retCode": 0, "result": {"list": [
    ["1789459200000", "2481.5", "2490.2", "2477.0", "2488.1", "1234.5",
     "3067890.1"],
    ["1789455600000", "2470.0", "2485.0", "2465.0", "2481.5", "1000.0",
     "2470000.0"]]}}

OI_FIXTURE = {"retCode": 0, "result": {"list": [
    {"openInterest": "52664.64200000", "singleOpenInterest": "26332.321",
     "timestamp": "1789458600000"},
    {"openInterest": "52652.41700000", "singleOpenInterest": "26326.209",
     "timestamp": "1789458000000"}],
    "nextPageCursor": "x"}}

FUNDING_FIXTURE = {"retCode": 0, "result": {"category": "linear", "list": [
    {"symbol": "ETHUSDT", "fundingRate": "-0.00006352",
     "fundingRateTimestamp": "1789459200000"},
    {"symbol": "ETHUSDT", "fundingRate": "0.0001",
     "fundingRateTimestamp": "1789430400000"}]}}

RATIO_FIXTURE = {"retCode": 0, "result": {"list": [
    {"symbol": "ETHUSDT", "buyRatio": "0.6784", "sellRatio": "0.3216",
     "timestamp": "1789460400000"},
    {"symbol": "ETHUSDT", "buyRatio": "1.0", "sellRatio": "0.0",
     "timestamp": "1789460400000"}],
    "nextPageCursor": "y"}}


# ── parse pins ────────────────────────────────────────────────────────────────


def test_parse_kline_fixture_flipped_ascending():
    rows = cc.parse_kline_rows(KLINE_FIXTURE)
    assert [r[0] for r in rows] == [1789455600000, 1789459200000]
    assert rows[1] == (1789459200000, 2481.5, 2490.2, 2477.0, 2488.1)


def test_parse_oi_fixture():
    rows = cc.parse_oi_rows(OI_FIXTURE)
    assert rows == [(1789458000000, 52652.417), (1789458600000, 52664.642)]


def test_parse_funding_fixture():
    rows = cc.parse_funding_rows(FUNDING_FIXTURE)
    assert rows == [(1789430400000, 0.0001), (1789459200000, -0.00006352)]


def test_parse_account_ratio_fixture_zero_sell_skipped():
    rows = cc.parse_account_ratio_rows(RATIO_FIXTURE)
    assert len(rows) == 1  # sellRatio 0.0 row skipped (infinite L/S)
    assert rows[0][0] == 1789460400000
    assert rows[0][1] == pytest.approx(0.6784 / 0.3216, abs=1e-9)


def test_normalize_ts_ms_seconds_quirk():
    assert cc.normalize_ts_ms("1789458000") == 1789458000000
    assert cc.normalize_ts_ms("1789458600000") == 1789458600000


# ── alignment / ffill ─────────────────────────────────────────────────────────


def test_ffill_ragged_grid():
    grid = [1000, 2000, 3000, 4000]
    points = [(1500, 5.0), (3500, 7.0)]
    assert cc.ffill_to_grid(grid, points) == [None, 5.0, 5.0, 7.0]
    assert cc.ffill_to_grid([500], points) == [None]  # before first point


# ── score legs, independently + boundaries ───────────────────────────────────


def test_score_leg1_latest_funding_negative():
    grid = [BASE_TS]
    s = cc.compute_scores(grid, [(BASE_TS - H, -0.0001)], [None], [None])
    assert s == [1]


def test_score_leg1_funding_exactly_zero_not_negative():
    grid = [BASE_TS]
    s = cc.compute_scores(grid, [(BASE_TS - H, 0.0)], [None], [None])
    assert s == [0]  # exactly 0 is NOT negative


def test_score_leg2_two_prior_settlements():
    grid = [BASE_TS]
    # latest -0.003; two priors -0.002/-0.001 -> leg1 + leg2
    f = [(BASE_TS - 4 * H, -0.001), (BASE_TS - 3 * H, -0.002),
         (BASE_TS - H, -0.003)]
    assert cc.compute_scores(grid, f, [None], [None]) == [2]
    # one prior non-negative -> leg2 dies, leg1 survives
    f2 = [(BASE_TS - 4 * H, -0.001), (BASE_TS - 3 * H, 0.005),
          (BASE_TS - H, -0.003)]
    assert cc.compute_scores(grid, f2, [None], [None]) == [1]
    # only two settlements total -> leg2 impossible
    f3 = [(BASE_TS - 2 * H, -0.001), (BASE_TS - H, -0.002)]
    assert cc.compute_scores(grid, f3, [None], [None]) == [1]


def test_score_leg3_oi_6h_positive_and_zero_boundary():
    grid = [BASE_TS + i * H for i in range(7)]
    flat = [100.0] * 7
    assert cc.compute_scores(grid, [], flat, [None] * 7)[6] == 0  # 0 not > 0
    up = [100.0] * 6 + [101.0]
    assert cc.compute_scores(grid, [], up, [None] * 7)[6] == 1
    # missing lookback (i < 6) -> leg absent
    assert cc.compute_scores(grid, [], up, [None] * 7)[5] == 0


def test_score_leg4_ls_strictly_greater_boundary():
    grid = [BASE_TS]
    assert cc.compute_scores(grid, [], [None], [1.8]) == [0]   # ==1.8 fails
    assert cc.compute_scores(grid, [], [None], [1.800001]) == [1]
    assert cc.compute_scores(grid, [], [None], [None]) == [0]


# ── cascade event detection, boundaries inclusive ─────────────────────────────


def test_cascade_event_boundary_inclusive():
    closes = [100.0, 98.0]          # exactly -2%
    oi = [100.0, 97.0]              # exactly -3%
    assert cc.find_cascade_events(closes, oi) == [1]


def test_cascade_event_just_inside_threshold_excluded():
    assert cc.find_cascade_events([100.0, 98.01], [100.0, 97.0]) == []
    assert cc.find_cascade_events([100.0, 98.0], [100.0, 97.01]) == []
    # missing OI never fires
    assert cc.find_cascade_events([100.0, 90.0], [None, 90.0]) == []


def test_cascade_event_lag1_settlement_detected():
    """The 2026-09-15 repair: OI settles one bar after the price dump.
    Price -2% at bar 1, OI flush at bar 2 (-3.015% vs bar 1) -> event at the
    PRICE bar. lag_bars=0 (legacy same-bar) must NOT fire here."""
    closes = [100.0, 98.0, 98.0]
    oi = [100.0, 99.5, 96.5]
    assert cc.find_cascade_events(closes, oi) == [1]
    assert cc.find_cascade_events(closes, oi, lag_bars=0) == []
    # flush further out than the lag window still misses
    assert cc.find_cascade_events([100.0, 98.0, 98.0, 98.0],
                                  [100.0, 99.5, 99.4, 96.4]) == []
    # same-bar fixtures are bit-for-bit identical across lag settings
    assert cc.find_cascade_events([100.0, 98.0], [100.0, 97.0],
                                  lag_bars=0) == [1]


# ── constructed end-to-end dataset (one qualifying trigger) ──────────────────


def make_dataset(n=200, further=False):
    """Cascade bar at i=50: OI 100->97 (-3%), close 100->98 (-2%). The
    pre-cascade bar i=49 has full score 4; the cascade bar itself scores 3
    (its own OI drop kills the +OI-6h leg), pinning pre-cascade scoring."""
    grid_ts = [BASE_TS + i * H for i in range(n)]
    closes = [100.0] * 50 + [98.0] * (n - 50)
    oi_vals = [99.0] * 44 + [100.0] * 6 + [97.0] * (n - 50)
    funding = [(grid_ts[40], -0.001), (grid_ts[42], -0.002),
               (grid_ts[48], -0.003)]
    ls_vals = [1.9] * n
    if further:
        closes[100] = 95.9  # -2.14% bar inside the horizon -> further_down
    return grid_ts, closes, funding, oi_vals, ls_vals


def test_end_to_end_single_trigger_counts():
    grid_ts, closes, funding, oi_vals, ls_vals = make_dataset()
    rep = cc.build_symbol_report(grid_ts, closes, funding, oi_vals, ls_vals, 4)
    assert rep["n_cascade_events"] == 1
    assert rep["n_qualifying_triggers"] == 1
    assert rep["n_labeled"] == 1
    assert rep["n_further_down"] == 0
    assert rep["conditional_p"] == 0.0
    assert rep["wilson95"] is not None
    assert rep["thin_sample"] is True
    assert rep["note"] == cc.THIN_NOTE
    trg = rep["triggers"][0]
    assert trg["ts"] == grid_ts[50]
    assert trg["score"] == 4
    # entry = trigger bar's OWN close (98 after the -2% bar); the forward
    # window is flat at 98, so both metrics are exactly 0.0.
    assert trg["drawdown_7d"] == pytest.approx(0.0, abs=1e-12)
    assert trg["return_7d"] == pytest.approx(0.0, abs=1e-12)


def test_score_is_pre_cascade_bar_not_trigger_bar():
    # At-bar scoring would read score=3 (OI leg killed by the drop itself)
    # and the trigger would NOT qualify at threshold 4.
    grid_ts, closes, funding, oi_vals, ls_vals = make_dataset()
    scores = cc.compute_scores(grid_ts, funding, oi_vals, ls_vals)
    assert scores[49] == 4
    assert scores[50] == 3
    rep = cc.build_symbol_report(grid_ts, closes, funding, oi_vals, ls_vals, 4)
    assert rep["n_qualifying_triggers"] == 1


def test_score_gating_threshold_3_vs_4():
    grid_ts, closes, funding, oi_vals, ls_vals = make_dataset()
    assert cc.build_symbol_report(grid_ts, closes, funding, oi_vals,
                                  ls_vals, 5)["n_qualifying_triggers"] == 0
    weak_ls = [1.7] * len(ls_vals)  # score drops to 3
    assert cc.build_symbol_report(grid_ts, closes, funding, oi_vals,
                                  weak_ls, 4)["n_qualifying_triggers"] == 0
    assert cc.build_symbol_report(grid_ts, closes, funding, oi_vals,
                                  weak_ls, 3)["n_qualifying_triggers"] == 1


# ── forward labels ────────────────────────────────────────────────────────────


def test_forward_label_math_constructed_path():
    closes = [100.0] * 11 + [100.0] * 168
    closes[30] = 96.0
    closes[31] = 94.0   # -2.08% bar -> further_down
    closes[50] = 90.0   # the min
    closes[178] = 95.0  # the end close
    lab = cc.forward_label(10, closes)
    assert lab["min_close"] == 90.0
    assert lab["max_close"] == 100.0
    assert lab["end_close"] == 95.0
    assert lab["drawdown_7d"] == pytest.approx(-0.10, abs=1e-12)
    assert lab["return_7d"] == pytest.approx(-0.05, abs=1e-12)
    assert lab["further_down"] is True
    assert lab["forward_bars"] == 168


def test_forward_label_further_down_flag_false_on_flat_path():
    closes = [100.0] * 11 + [100.0] * 168
    lab = cc.forward_label(10, closes)
    assert lab["further_down"] is False
    assert lab["drawdown_7d"] == pytest.approx(0.0)


def test_forward_label_insufficient_forward_data_skipped():
    closes = [100.0] * 60
    assert cc.forward_label(10, closes) is None  # 49 < 84 half-horizon
    assert cc.forward_label(10, [100.0] * 95) is not None  # exactly 84


# ── Wilson score interval ─────────────────────────────────────────────────────


def test_wilson_n10_k5():
    lo, hi = cc.wilson_interval(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-4)
    assert hi == pytest.approx(0.7634, abs=1e-4)


def test_wilson_n20_k10_matches_spec_handcheck():
    # The Governor's hand-check "0.299-0.701" is the n=20 k=10 interval;
    # pinned here so the n=10 k=5 typo in the spec stays documented.
    lo, hi = cc.wilson_interval(10, 20)
    assert lo == pytest.approx(0.2993, abs=1e-4)
    assert hi == pytest.approx(0.7007, abs=1e-4)


def test_wilson_n0_is_none_not_nan():
    assert cc.wilson_interval(0, 0) is None
    lo, hi = cc.wilson_interval(0, 1)
    assert lo >= 0.0 and hi <= 1.0


# ── thin sample, histogram, percentiles ───────────────────────────────────────


def test_thin_sample_49_vs_50():
    assert cc.thin_sample(49) == (True, cc.THIN_NOTE)
    assert cc.thin_sample(50) == (False, None)


def test_hour_histogram():
    ts = [2403 * H, 2403 * H, 2415 * H]  # UTC hours 3, 3, 15
    h = cc.hour_histogram(ts)
    assert h["3"] == 2
    assert h["15"] == 1
    assert h["0"] == 0 and len(h) == 24


def test_percentile_nearest_rank_and_drawdown_stats():
    assert cc.percentile_nearest_rank([1, 2, 3, 4], 0.75) == 3
    assert cc.percentile_nearest_rank([5.0], 0.75) == 5.0
    assert cc.percentile_nearest_rank([], 0.75) is None
    st = cc.drawdown_stats([-0.10, -0.20, -0.05, -0.08])
    assert st["avg"] == pytest.approx(-0.1075)
    assert st["median"] == pytest.approx(-0.09)
    assert st["p75"] == pytest.approx(-0.08)
    assert cc.drawdown_stats([]) is None


# ── pooled section ────────────────────────────────────────────────────────────


def test_pool_reports_sums():
    g1 = make_dataset(further=False)
    g2 = make_dataset(further=True)
    r1 = cc.build_symbol_report(*g1, 4)
    r2 = cc.build_symbol_report(*g2, 4)
    assert r2["n_further_down"] == 1
    pooled = cc.pool_reports({"AAAUSDT": r1, "BBBUSDT": r2})
    assert pooled["n_cascade_events"] == 2
    assert pooled["n_qualifying_triggers"] == 2
    assert pooled["n_labeled"] == 2
    assert pooled["n_further_down"] == 1
    assert pooled["conditional_p"] == pytest.approx(0.5)
    assert pooled["wilson95"] is not None
    assert pooled["trigger_hour_utc_hist"]["10"] == 2  # both at UTC hour 10
    assert pooled["drawdown_7d"]["n"] == 2
    assert pooled["symbols"] == ["AAAUSDT", "BBBUSDT"]


# ── io ────────────────────────────────────────────────────────────────────────


def test_atomic_write_json_roundtrip(tmp_path):
    p = tmp_path / "cascade_census.json"
    cc.atomic_write_json(p, {"a": 1, "b": [0.23, 0.76]})
    assert json.loads(p.read_text()) == {"a": 1, "b": [0.23, 0.76]}
    assert not list(tmp_path.glob("*.tmp"))
