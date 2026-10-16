"""Pins for tools/guardian_replay.py — causality, fill math, fees, cache, journal, CI."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import guardian_replay as gr  # noqa: E402

STEP = 60_000
T0 = 1_800_000_000_000


def mk_bars(ohlcs, start=T0, step=STEP):
    """Synthetic ascending kline series [t,o,h,l,c]."""
    return [[start + i * step, float(o), float(h), float(l), float(c)]
            for i, (o, h, l, c) in enumerate(ohlcs)]


# ── 1+12. Causality: no same-bar arm+fill; fill earliest on the bar AFTER arming ──
def test_causality_no_same_bar_mfe_exit():
    # long entry 100, r=0.30, f=0.50. Bar0: high 100.5 (MFE 0.5% -> would arm floor
    # at 100.25) AND low 100.10 (below that would-be floor). Pre-bar floor is None,
    # so bar0 can NOT fill. Bar1 dips below 100.25 -> fills there.
    bars = mk_bars([(100.0, 100.5, 100.10, 100.4),
                    (100.4, 100.45, 100.20, 100.3)])
    res = gr.replay_floor(bars, entry=100.0, direction="long", r=0.30, f=0.50,
                          slippage_bps=0.0)
    assert res["armed"] is True
    assert res["filled"] is True
    assert res["fill_idx"] == 1  # never the arming bar itself
    assert res["fill_price"] == pytest.approx(100.25)  # entry*(1+0.5*0.5/100)


def test_fill_only_after_arming_bar():
    # A single bar that both crosses r and breaches the implied floor: no fill at all.
    bars = mk_bars([(100.0, 101.0, 99.0, 100.9)])  # MFE 1.0%, floor would be 100.5
    res = gr.replay_floor(bars, entry=100.0, direction="long", r=0.30, f=0.50,
                          slippage_bps=0.0)
    assert res["filled"] is False
    assert res["armed"] is True


# ── 2. Floor is tighten-only ───────────────────────────────────────────────────
def test_floor_tighten_only():
    # MFE rises in steps; later bar has lower excursion. Floor must never fall.
    bars = mk_bars([(100.0, 100.4, 99.9, 100.3),   # mfe 0.4 -> floor 100*(1+.5*.4/100)=100.20
                    (100.3, 100.8, 100.3, 100.7),   # mfe 0.8 -> floor 100.40 (low stays above)
                    (100.7, 100.75, 100.6, 100.7)]) # lower excursion -> floor stays 100.40
    res = gr.replay_floor(bars, entry=100.0, direction="long", r=0.30, f=0.50,
                          slippage_bps=0.0)
    floors = [t["floor"] for t in res["trace"] if t["floor"] is not None]
    assert floors == sorted(floors)  # monotonically non-decreasing
    assert res["trace"][-1]["floor"] == pytest.approx(100.40)
    assert res["filled"] is False  # lows never breach the floor


# ── 3+11. Fill price math: min(open, floor) long / max(open, floor) short ──────
def test_long_fill_min_open_floor():
    # Arm floor at 100.25 via bar0, then:
    #  case A: open 100.30 > floor, low breaches -> fill at floor
    bars_a = mk_bars([(100.0, 100.5, 100.3, 100.4),
                      (100.30, 100.35, 100.20, 100.25)])
    ra = gr.replay_floor(bars_a, 100.0, "long", 0.30, 0.50, slippage_bps=0.0)
    assert ra["fill_price"] == pytest.approx(min(100.30, 100.25))
    #  case B: gap-down open 100.10 < floor -> fill at the open, never better
    bars_b = mk_bars([(100.0, 100.5, 100.3, 100.4),
                      (100.10, 100.12, 100.05, 100.08)])
    rb = gr.replay_floor(bars_b, 100.0, "long", 0.30, 0.50, slippage_bps=0.0)
    assert rb["fill_price"] == pytest.approx(100.10)


def test_short_fill_max_open_floor_mirror():
    # short entry 100, r=0.30, f=0.50. Bar0 low 99.5 -> MFE 0.5%, floor 99.75.
    bars_a = mk_bars([(100.0, 100.1, 99.5, 99.6),
                      (99.70, 99.85, 99.65, 99.8)])  # open 99.70 < floor; high breaches
    ra = gr.replay_floor(bars_a, 100.0, "short", 0.30, 0.50, slippage_bps=0.0)
    assert ra["fill_idx"] == 1
    assert ra["fill_price"] == pytest.approx(max(99.70, 99.75))
    # gap-up open above floor -> fill at the open
    bars_b = mk_bars([(100.0, 100.1, 99.5, 99.6),
                      (99.90, 99.95, 99.85, 99.9)])
    rb = gr.replay_floor(bars_b, 100.0, "short", 0.30, 0.50, slippage_bps=0.0)
    assert rb["fill_price"] == pytest.approx(99.90)


def test_slippage_applied_adversely():
    bars = mk_bars([(100.0, 100.5, 100.3, 100.4),
                    (100.30, 100.35, 100.20, 100.25)])
    long_res = gr.replay_floor(bars, 100.0, "long", 0.30, 0.50, slippage_bps=10.0)
    assert long_res["fill_price"] == pytest.approx(100.25 * (1 - 10 / 1e4))
    bars_s = mk_bars([(100.0, 100.1, 99.5, 99.6),
                      (99.70, 99.85, 99.65, 99.8)])
    short_res = gr.replay_floor(bars_s, 100.0, "short", 0.30, 0.50, slippage_bps=10.0)
    assert short_res["fill_price"] == pytest.approx(99.75 * (1 + 10 / 1e4))


# ── 4. Never armed / never breached -> control tail ────────────────────────────
def test_never_armed_control_tail():
    bars = mk_bars([(100.0, 100.1, 99.9, 100.0)] * 5)  # MFE 0.1% < r=0.30
    res = gr.replay_floor(bars, 100.0, "long", 0.30, 0.50, slippage_bps=0.0)
    assert res["filled"] is False and res["armed"] is False

    trade = {"entry_price": 100.0, "direction": "long", "position_size": 2.0,
             "closed_at_ms": T0 + 5 * STEP, "timestamp_ms": T0,
             "pnl_net_usd": -1.25, "exit_reason": "software_stop"}
    row = gr.eval_trade_policy({"kind": "floor", "name": "x", "r": 0.30, "f": 0.50},
                               trade, bars, slippage_bps=2.0)
    assert row["pnl"] == pytest.approx(-1.25)  # control pnl untouched
    assert row["fills"] == 0


# ── 5. partial_tp banks exactly size_frac at the touch price ──────────────────
def test_partial_tp_banks_size_frac_at_touch():
    # entry 100 long, tp1 102, frac_of_tp1 0.5 -> target 101. Bar touches 101.5.
    bars = mk_bars([(100.0, 101.5, 99.9, 101.2)])
    trade = {"entry_price": 100.0, "direction": "long", "position_size": 10.0,
             "tp1_price": 102.0, "closed_at_ms": T0 + STEP, "timestamp_ms": T0,
             "pnl_net_usd": 8.0, "exit_reason": "tp2"}
    row = gr.eval_trade_policy(
        {"kind": "partial_tp", "name": "p", "frac_of_tp1": 0.5, "size_frac": 0.5},
        trade, bars, slippage_bps=2.0)
    banked_gross = (101.0 - 100.0) * 10.0 * 0.5              # 5.0 at touch price 101
    banked_fee = 101.0 * 5.0 * gr.MAKER_BPS / 1e4            # maker on banked notional
    expected = banked_gross - banked_fee + 8.0 * 0.5         # remainder rides control
    assert row["pnl"] == pytest.approx(expected)
    assert row["partial_touched"] is True
    assert row["fills"] == 1


# ── 6. Fee application on floor fills ──────────────────────────────────────────
def test_fee_application_taker_plus_entry():
    bars = mk_bars([(100.0, 100.5, 100.3, 100.4),
                    (100.30, 100.35, 100.20, 100.25)])
    trade = {"entry_price": 100.0, "direction": "long", "position_size": 2.0,
             "closed_at_ms": T0 + 2 * STEP, "timestamp_ms": T0,
             "pnl_net_usd": 0.10, "exit_reason": "software_stop"}
    row = gr.eval_trade_policy({"kind": "floor", "name": "x", "r": 0.30, "f": 0.50},
                               trade, bars, slippage_bps=0.0)
    fill = 100.25
    expected = ((fill - 100.0) * 2.0
                - fill * 2.0 * gr.TAKER_BPS / 1e4       # exit taker fee
                - 100.0 * 2.0 * gr.TAKER_BPS / 1e4)     # entry taker fee
    assert row["pnl"] == pytest.approx(expected)
    assert row["pnl"] != pytest.approx((fill - 100.0) * 2.0)  # fees are not zero


def test_fill_pnl_helper_exact():
    pnl = gr.fill_pnl(100.0, 101.0, 3.0, "long",
                      exit_fee_bps=4.0, entry_fee_bps=4.0)
    assert pnl == pytest.approx(3.0 - 101.0 * 3.0 * 4e-4 - 100.0 * 3.0 * 4e-4)


# ── 7. Cache roundtrip + one-bad-line tolerance ───────────────────────────────
def test_cache_roundtrip_and_bad_line(tmp_path):
    bars = mk_bars([(100, 101, 99, 100.5), (100.5, 102, 100, 101.5)])
    path = tmp_path / "cache" / "BTC-USD_1m_20261015.json"
    gr.save_cache(str(path), bars)
    loaded = gr.load_cache(str(path))
    assert loaded == bars
    # corrupt exactly one line -> that line is skipped, the rest survive
    with open(path, "a") as fh:
        fh.write("THIS IS NOT JSON\n")
    loaded2 = gr.load_cache(str(path))
    assert loaded2 == bars


def test_cache_get_bars_reuses_covering_cache(tmp_path):
    bars = mk_bars([(100 + i * 0.1, 100.5 + i * 0.1, 99.5 + i * 0.1, 100.2 + i * 0.1)
                    for i in range(10)])
    cache_dir = str(tmp_path / "replay_cache")
    path = gr.cache_path(cache_dir, "ETH-USD", "1m", "20261015")
    gr.save_cache(path, bars)
    calls = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("fetch must not be called when cache covers")

    out, gaps = gr.get_bars("ETH-USD", "1m", bars[1][0], bars[8][0],
                            cache_dir, "20261015", fetcher=boom)
    assert calls == []
    assert out == bars[1:9]  # sliced to the requested span
    assert gaps == []


# ── 8. Dedup key (entry_id, closed_at_ms) ─────────────────────────────────────
def test_dedup_key():
    rec = {"entry_id": "abc", "closed_at_ms": 123, "outcome": "win"}
    dup = dict(rec)
    other = {"entry_id": "abc", "closed_at_ms": 124, "outcome": "win"}
    out = gr.dedup([rec, dup, other])
    assert len(out) == 2
    assert (out[0]["entry_id"], out[0]["closed_at_ms"]) == ("abc", 123)


# ── 9. Journal filter ──────────────────────────────────────────────────────────
def test_journal_filter_excludes_rejected_and_open():
    recs = [
        {"outcome": "rejected", "closed_at_ms": 1, "entry_price": 100, "direction": "long"},
        {"outcome": None, "closed_at_ms": 1, "entry_price": 100, "direction": "long"},
        {"outcome": "win", "closed_at_ms": None, "entry_price": 100, "direction": "long"},
        {"outcome": "win", "closed_at_ms": 1, "entry_price": 0, "direction": "long"},
        {"outcome": "loss", "closed_at_ms": 1, "entry_price": 100, "direction": "short"},
    ]
    out = gr.filter_closed(recs)
    assert len(out) == 1
    assert out[0]["direction"] == "short"


# ── 10. Bootstrap CI determinism ───────────────────────────────────────────────
def test_bootstrap_ci_deterministic():
    deltas = [0.5, -1.2, 0.3, 2.1, -0.4, 0.9, -0.1, 1.4]
    a = gr.bootstrap_ci(deltas, resamples=2000, seed=42)
    b = gr.bootstrap_ci(deltas, resamples=2000, seed=42)
    assert a == b
    assert a[0] <= sum(deltas) / len(deltas) <= a[1]


# ── Extra: breakeven arm carries the maker-fee buffer ─────────────────────────
def test_breakeven_arm_fee_buffer():
    # r=0.30, f=0.0 + maker buffer: floor = entry*(1 + MAKER_BPS/100/100 * mfe-independent)
    bars = mk_bars([(100.0, 100.4, 99.9, 100.3)])
    res = gr.replay_floor(bars, 100.0, "long", 0.30, 0.0, slippage_bps=0.0,
                          fee_buffer_bps=gr.MAKER_BPS)
    floor = res["trace"][-1]["floor"]
    assert floor == pytest.approx(100.0 * (1 + gr.MAKER_BPS / 100.0 / 100.0))
    assert floor > 100.0  # a breakeven stop sits above entry, never at a loss


# ── Extra: end-to-end metrics on synthetic rows ───────────────────────────────
def test_metrics_shape_and_splits():
    rows = []
    for i, pnl in enumerate([1.0, -2.0, 3.0]):
        rows.append({"pnl": pnl, "control_pnl": 0.5, "fills": 1, "armed": True,
                     "floor_filled": pnl > 0, "mfe_pct": 1.0, "realized_pct": 0.5,
                     "tp1_touch": True, "exit_reason": "software_stop" if i else "tp1",
                     "hold_ms": (3 * 60_000) if i == 0 else (30 * 60_000),
                     "closed_at_ms": T0 + i * STEP})
    m = gr.compute_metrics("floor_r0.3_f0.5", rows)
    assert m["n"] == 3
    assert m["net_pnl"] == pytest.approx(2.0)
    assert m["delta_vs_control"] == pytest.approx(2.0 - 1.5)
    assert m["arm_rate"] == pytest.approx(1.0)
    assert m["floor_fill_rate"] == pytest.approx(round(2 / 3, 4))
    assert m["turnover_fills"] == 3
    assert m["max_seq_drawdown"] == pytest.approx(-2.0)  # 1 -> -1 trough
    assert m["per_exit_reason"]["tp1"]["n"] == 1
    assert m["per_hold_class"]["<6min"]["n"] == 1
    assert m["per_hold_class"]["6-60min"]["n"] == 2
    assert len(m["delta_ci95"]) == 2
