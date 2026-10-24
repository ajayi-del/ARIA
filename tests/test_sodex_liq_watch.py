"""tests/test_sodex_liq_watch.py — pins for intelligence/sodex_liq_engine_watch.py.

Doctrine pins: kill switch off = total abstention + zero state; ~20%/~30s
cadence detection is mechanical; verdict emits once at confirm and only
re-emits on extension; decay evicts; book nudge bounded; fail-closed never
raises.
"""
import math

import pytest

from intelligence.sodex_liq_engine_watch import (
    LiqWatchConfig,
    LiqWatchVerdict,
    SodexLiqEngineWatch,
    sodex_liq_watch_enabled,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SODEX_LIQ_WATCH_ENABLED", raising=False)
    yield


def mk(cfg=None):
    return SodexLiqEngineWatch(cfg or LiqWatchConfig())


# ── kill switch ─────────────────────────────────────────────────────────────

def test_enabled_default_true():
    assert sodex_liq_watch_enabled() is True


def test_enabled_falsy_values(monkeypatch):
    for v in ("0", "false", "no", "off", "FALSE"):
        monkeypatch.setenv("SODEX_LIQ_WATCH_ENABLED", v)
        assert sodex_liq_watch_enabled() is False


def test_enabled_read_at_call_time(monkeypatch):
    monkeypatch.setenv("SODEX_LIQ_WATCH_ENABLED", "0")
    assert sodex_liq_watch_enabled() is False
    monkeypatch.setenv("SODEX_LIQ_WATCH_ENABLED", "true")
    assert sodex_liq_watch_enabled() is True


def test_kill_off_on_trade_returns_none(monkeypatch):
    monkeypatch.setenv("SODEX_LIQ_WATCH_ENABLED", "0")
    w = mk()
    assert w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 1000.0) is None
    assert w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 1030.0) is None


def test_kill_off_holds_no_state(monkeypatch):
    monkeypatch.setenv("SODEX_LIQ_WATCH_ENABLED", "0")
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 1000.0)
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 1030.0)
    snap = w.state_snapshot()
    assert snap["symbols"] == 0
    assert snap["active_runs"] == 0
    assert snap["prints_tracked"] == 0


def test_kill_off_on_book_and_decay_noop(monkeypatch):
    monkeypatch.setenv("SODEX_LIQ_WATCH_ENABLED", "0")
    w = mk()
    assert w.on_book("BTC-USD", 100.0, 900.0, 1000.0) is None
    assert w.collect_decayed(2000.0) == []
    assert w.state_snapshot()["symbols"] == 0


# ── happy path ──────────────────────────────────────────────────────────────

def test_two_regular_sells_confirm_long_liq():
    w = mk()
    assert w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 1000.0) is None
    v = w.on_trade("BTC-USD", "sell", 20400.0, 100000.0, 1030.0)
    assert isinstance(v, LiqWatchVerdict)
    assert v.side == "long_liq"
    assert v.state == "confirmed"
    assert v.n_slices == 2


def test_two_regular_buys_confirm_short_liq():
    w = mk()
    w.on_trade("ETH-USD", "buy", 25000.0, 3000.0, 100.0)
    v = w.on_trade("ETH-USD", "buy", 25000.0, 3000.0, 130.0)
    assert v is not None and v.side == "short_liq"


def test_side_mapping_case_insensitive():
    w = mk()
    w.on_trade("BTC-USD", "SELL", 20000.0, 100000.0, 0.0)
    v = w.on_trade("BTC-USD", "Sell", 20000.0, 100000.0, 30.0)
    assert v is not None and v.side == "long_liq"


def test_verdict_estimates_math():
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 1000.0)
    v = w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 1030.0)
    # est total = 20000/0.2 = 100000; remaining = 100000 - 2*20000 = 60000
    assert v.slice_notional_usd == pytest.approx(20000.0)
    assert v.est_remaining_usd == pytest.approx(60000.0)
    # advance warning = 30s × (5 - 2) = 90s
    assert v.advance_warning_s == pytest.approx(90.0)


def test_verdict_first_last_seen():
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 500.0)
    v = w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 530.0)
    assert v.first_seen_s == 500.0
    assert v.last_seen_s == 530.0


def test_confidence_bounded_and_regular():
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    v = w.on_trade("BTC-USD", "sell", 20400.0, 100000.0, 30.0)
    assert 0.0 <= v.confidence <= 1.0
    # perfect interval + 1% size deviation -> high regularity
    assert v.confidence == pytest.approx(1.0 * (1.0 - (0.009901 / 0.03)), abs=0.02)


def test_per_symbol_isolation():
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    v = w.on_trade("ETH-USD", "sell", 20000.0, 3000.0, 30.0)
    assert v is None  # different symbol — no cross-symbol run


# ── regularity negatives ────────────────────────────────────────────────────

def test_irregular_sizes_no_verdict():
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    assert w.on_trade("BTC-USD", "sell", 22000.0, 100000.0, 30.0) is None  # +10%
    assert w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 60.0) is None


def test_irregular_interval_45s_no_verdict():
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    assert w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 45.0) is None
    assert w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 90.0) is None


def test_interval_too_fast_no_verdict():
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 10.0)  # noise, below 25s floor
    assert w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 40.0) is None  # 40 > 35


def test_interval_edge_of_tolerance_confirms():
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    v = w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 35.0)  # upper edge inclusive
    assert v is not None


def test_below_min_notional_no_run():
    w = mk()
    assert w.on_trade("BTC-USD", "sell", 19999.0, 100000.0, 0.0) is None
    assert w.on_trade("BTC-USD", "sell", 19999.0, 100000.0, 30.0) is None
    assert w.state_snapshot()["active_runs"] == 0


def test_mixed_sides_no_verdict():
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    w.on_trade("BTC-USD", "buy", 20000.0, 100000.0, 30.0)
    assert w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 60.0) is None  # 60s gap


def test_single_print_no_verdict():
    w = mk()
    assert w.on_trade("BTC-USD", "sell", 50000.0, 100000.0, 0.0) is None


# ── emission discipline ─────────────────────────────────────────────────────

def test_reemit_on_extension():
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    v2 = w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 30.0)
    v3 = w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 60.0)
    assert v2 is not None and v2.n_slices == 2
    assert v3 is not None and v3.n_slices == 3
    assert v3.est_remaining_usd == pytest.approx(40000.0)
    assert v3.advance_warning_s == pytest.approx(60.0)


def test_no_duplicate_verdict_without_extension():
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    assert w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 30.0) is not None
    # book events and sub-floor noise between slices must not re-emit
    w.on_book("BTC-USD", 100.0, 900.0, 40.0)
    assert w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 45.0) is None
    assert w.state_snapshot()["verdicts_emitted"] == 1


def test_five_slices_remaining_zero():
    w = mk()
    v = None
    for i in range(5):
        v = w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, float(i * 30))
    assert v is not None and v.n_slices == 5
    assert v.est_remaining_usd == pytest.approx(0.0)
    assert v.advance_warning_s == pytest.approx(0.0)


def test_verdicts_emitted_counter():
    w = mk()
    for i in range(4):
        w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, float(i * 30))
    assert w.state_snapshot()["verdicts_emitted"] == 3  # n = 2, 3, 4


# ── decay ───────────────────────────────────────────────────────────────────

def test_decay_returns_decayed_verdict():
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 30.0)
    # deadline = last(30) + 30 + 5 + 30 = 95 -> decayed at 96
    out = w.collect_decayed(96.0)
    assert len(out) == 1
    v = out[0]
    assert v.state == "decayed"
    assert v.symbol == "BTC-USD" and v.side == "long_liq"
    assert v.n_slices == 2


def test_no_decay_before_deadline():
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 30.0)
    assert w.collect_decayed(95.0) == []


def test_decay_evicts_run():
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 30.0)
    w.collect_decayed(96.0)
    snap = w.state_snapshot()
    assert snap["active_runs"] == 0
    assert snap["decayed_emitted"] == 1
    # second sweep does not re-emit
    assert w.collect_decayed(200.0) == []


def test_unconfirmed_run_decays_silently():
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)  # single print, never confirmed
    out = w.collect_decayed(200.0)
    assert out == []
    assert w.state_snapshot()["active_runs"] == 0


def test_decay_alias_matches_collect():
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 30.0)
    out = w.decay(100.0)
    assert len(out) == 1 and out[0].state == "decayed"


# ── book imbalance ──────────────────────────────────────────────────────────

def test_book_agree_raises_confidence():
    w_no, w_yes = mk(), mk()
    for w in (w_no, w_yes):
        w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    w_yes.on_book("BTC-USD", 100.0, 900.0, 15.0)  # ask-heavy: bids pulled
    v_no = w_no.on_trade("BTC-USD", "sell", 20400.0, 100000.0, 30.0)
    v_yes = w_yes.on_trade("BTC-USD", "sell", 20400.0, 100000.0, 30.0)
    assert v_yes.confidence == pytest.approx(min(1.0, v_no.confidence + 0.1))


def test_book_disagree_lowers_confidence():
    w_no, w_yes = mk(), mk()
    for w in (w_no, w_yes):
        w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    w_yes.on_book("BTC-USD", 900.0, 100.0, 15.0)  # bid-heavy: contradicts long_liq
    v_no = w_no.on_trade("BTC-USD", "sell", 20400.0, 100000.0, 30.0)
    v_yes = w_yes.on_trade("BTC-USD", "sell", 20400.0, 100000.0, 30.0)
    assert v_yes.confidence == pytest.approx(max(0.0, v_no.confidence - 0.1))


def test_book_nudge_clamped_at_one():
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    w.on_book("BTC-USD", 0.0, 1000.0, 15.0)  # max agreement
    v = w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 30.0)  # perfect regularity
    assert v.confidence == 1.0


def test_book_nudge_clamped_at_zero():
    # regularity at the very edge of both tolerances -> base confidence 0.0
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    w.on_book("BTC-USD", 1000.0, 0.0, 15.0)  # max disagreement
    v = w.on_trade("BTC-USD", "sell", 20600.0, 100000.0, 35.0)  # +3% size, +5s interval
    assert v is not None
    assert v.confidence == 0.0


def test_book_bad_inputs_ignored():
    w = mk()
    assert w.on_book("BTC-USD", -1.0, 100.0, 15.0) is None
    assert w.on_book("", 100.0, 100.0, 15.0) is None
    assert w.on_book("BTC-USD", float("nan"), 100.0, 15.0) is None
    assert w.state_snapshot()["symbols"] == 0


def test_balanced_book_no_nudge():
    w_no, w_bal = mk(), mk()
    for w in (w_no, w_bal):
        w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    w_bal.on_book("BTC-USD", 500.0, 500.0, 15.0)  # 50/50 — below imbalance_min
    v_no = w_no.on_trade("BTC-USD", "sell", 20400.0, 100000.0, 30.0)
    v_bal = w_bal.on_trade("BTC-USD", "sell", 20400.0, 100000.0, 30.0)
    assert v_bal.confidence == pytest.approx(v_no.confidence)


# ── window eviction ─────────────────────────────────────────────────────────

def test_window_evicts_old_prints():
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 400.0)  # window 300: first evicted
    assert w.state_snapshot()["prints_tracked"] == 1


def test_stale_run_replaced_by_fresh_anchor():
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 400.0)  # 400s gap -> new run
    snap = w.state_snapshot()
    assert snap["active_runs"] == 1
    assert snap["confirmed_runs"] == 0


def test_custom_config_respected():
    cfg = LiqWatchConfig(min_slice_notional_usd=1000.0, slice_interval_s=10.0,
                         interval_tolerance_s=2.0)
    w = mk(cfg)
    w.on_trade("BTC-USD", "sell", 1500.0, 100000.0, 0.0)
    v = w.on_trade("BTC-USD", "sell", 1500.0, 100000.0, 10.0)
    assert v is not None
    assert v.advance_warning_s == pytest.approx(30.0)  # 10s × 3 remaining


# ── fail-closed ─────────────────────────────────────────────────────────────

def test_bad_inputs_never_raise():
    w = mk()
    assert w.on_trade(None, "sell", 20000.0, 1.0, 0.0) is None
    assert w.on_trade("BTC-USD", "hold", 20000.0, 1.0, 0.0) is None
    assert w.on_trade("BTC-USD", "sell", float("nan"), 1.0, 0.0) is None
    assert w.on_trade("BTC-USD", "sell", float("inf"), 1.0, 0.0) is None
    assert w.on_trade("BTC-USD", "sell", -5.0, 1.0, 0.0) is None
    assert w.on_trade("BTC-USD", "sell", 20000.0, 0.0, 0.0) is None
    assert w.on_trade("BTC-USD", "sell", 20000.0, 1.0, -1.0) is None
    assert w.state_snapshot()["active_runs"] == 0


def test_collect_decayed_bad_now_s():
    w = mk()
    assert w.collect_decayed(float("nan")) == []
    assert w.collect_decayed(-5.0) == []


def test_state_snapshot_shape():
    w = mk()
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 0.0)
    w.on_trade("BTC-USD", "sell", 20000.0, 100000.0, 30.0)
    snap = w.state_snapshot()
    for k in ("enabled", "symbols", "prints_tracked", "active_runs",
              "confirmed_runs", "verdicts_emitted", "decayed_emitted", "errors"):
        assert k in snap
    assert snap["enabled"] is True
    assert snap["confirmed_runs"] == 1
    assert snap["errors"] == 0
