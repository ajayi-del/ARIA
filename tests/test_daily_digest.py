"""Tests for tools/daily_digest.py — the deterministic EV precompute the
watchdog reads. Pure functions only; network sections untested by design."""
import importlib.util
import os
from collections import Counter

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools", "daily_digest.py")
_spec = importlib.util.spec_from_file_location("daily_digest", _PATH)
dd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dd)


def _rec(symbol="ETH-USD", outcome="win", pnl_usd=1.0, pnl_net_usd=None,
         hold_ms=600000, coherence=7.5, ts_ms=None, **kw):
    r = {"symbol": symbol, "outcome": outcome, "pnl_usd": pnl_usd,
         "pnl_net_usd": pnl_net_usd, "hold_time_ms": hold_ms,
         "coherence_score": coherence, "timestamp_ms": ts_ms or 1755400000000}
    r.update(kw)
    return r


# ── expectancy_by_symbol ─────────────────────────────────────────────────────

def test_expectancy_basic_math():
    recs = [_rec(pnl_net_usd=2.0), _rec(outcome="loss", pnl_net_usd=-1.0)]
    out = dd.expectancy_by_symbol(recs)
    e = out["ETH-USD"]
    assert e["n"] == 2 and e["wr"] == 0.5
    assert e["avg_win"] == 2.0 and e["avg_loss"] == -1.0
    assert e["expectancy"] == 0.5 and e["pnl_sum"] == 1.0
    assert e["flag"] == ""


def test_expectancy_churn_leak_flag():
    recs = [_rec(outcome="loss", pnl_net_usd=-0.03) for _ in range(10)]
    out = dd.expectancy_by_symbol(recs)
    assert out["ETH-USD"]["flag"] == "churn_leak"


def test_expectancy_no_flag_under_10_trades():
    recs = [_rec(outcome="loss", pnl_net_usd=-0.03) for _ in range(9)]
    out = dd.expectancy_by_symbol(recs)
    assert out["ETH-USD"]["flag"] == ""


def test_expectancy_falls_back_to_gross_pnl():
    recs = [_rec(pnl_usd=1.5, pnl_net_usd=None)]
    out = dd.expectancy_by_symbol(recs)
    assert out["ETH-USD"]["pnl_sum"] == 1.5


def test_expectancy_abandoned_counted_not_scored():
    recs = [_rec(), _rec(outcome="abandoned"), _rec(outcome="abandoned")]
    out = dd.expectancy_by_symbol(recs)
    assert out["ETH-USD"]["n"] == 1
    assert out["ETH-USD"]["abandoned"] == 2


# ── size_chain ───────────────────────────────────────────────────────────────

def test_size_chain_chokepoint_is_min_mean_mult():
    recs = [_rec(allocation_mult=0.9, coherence_mult=0.8, size_multiplier=0.35,
                 position_size=0.01, entry_price=4000.0)]
    out = dd.size_chain(recs, balance=500.0)
    assert out["chokepoint"] == "size_multiplier"
    assert out["mean_mults"]["size_multiplier"] == 0.35
    assert out["median_notional"] == 40.0


def test_size_chain_leak_flag():
    recs = [_rec(position_size=0.01, entry_price=4000.0)]
    out = dd.size_chain(recs, balance=500.0)
    assert out["flag"].startswith("size_leak")


def test_size_chain_no_flag_when_notional_healthy():
    recs = [_rec(position_size=0.05, entry_price=4000.0)]  # $200
    out = dd.size_chain(recs, balance=500.0)
    assert out["flag"] == ""


def test_size_chain_empty_records():
    out = dd.size_chain([], balance=500.0)
    assert out["mean_mults"] == {} and out["chokepoint"] == ""
    assert out["median_notional"] == 0.0 and out["flag"] == ""


def test_size_chain_per_venue_flag_uses_venue_equity():
    # The 2026-08-23 false positive: median $65.5 vs 15% of $754 COMBINED
    # flagged a leak, while the Aster median is ~35% of the $188 sleeve —
    # healthy. Per-venue references must replace the combined flag.
    venue_of = lambda s: "aster" if s.startswith(("UNI", "XAUT")) else "bybit"
    recs = [_rec(symbol="UNI-USD", position_size=1.0, entry_price=50.0),   # $50 aster
            _rec(symbol="ETH-USD", position_size=0.05, entry_price=4000.0)]  # $200 sodex
    out = dd.size_chain(recs, balance=754.0, venue_of=venue_of,
                        venue_equity={"aster": 188.0, "sodex": 566.0})
    assert out["median_by_venue"] == {"aster": 50.0, "sodex": 200.0}
    assert out["flag"] == ""          # 50 > 0.15×188=28.2 and 200 > 0.15×566=84.9
    assert out["venue_equity"] == {"aster": 188.0, "sodex": 566.0}


def test_size_chain_per_venue_flag_fires_on_real_leak():
    venue_of = lambda s: "aster" if s.startswith("UNI") else "bybit"
    recs = [_rec(symbol="UNI-USD", position_size=0.2, entry_price=50.0),   # $10 aster
            _rec(symbol="ETH-USD", position_size=0.05, entry_price=4000.0)]
    out = dd.size_chain(recs, balance=754.0, venue_of=venue_of,
                        venue_equity={"aster": 188.0, "sodex": 566.0})
    assert out["flag"].startswith("size_leak[aster]")


def test_size_chain_without_venue_args_keeps_legacy_flag():
    recs = [_rec(position_size=0.01, entry_price=4000.0)]
    out = dd.size_chain(recs, balance=500.0)
    assert out["flag"].startswith("size_leak")
    assert "median_by_venue" not in out


# ── hold_asymmetry ───────────────────────────────────────────────────────────

def test_hold_asymmetry_flag():
    recs = [_rec(hold_ms=10 * 60000),
            _rec(outcome="loss", hold_ms=60 * 60000)]
    out = dd.hold_asymmetry(recs)
    assert out["median_win_min"] == 10.0
    assert out["median_loss_min"] == 60.0
    assert out["flag"] == "cut_winners_ride_losers"


def test_hold_asymmetry_no_flag_when_symmetric():
    recs = [_rec(hold_ms=30 * 60000), _rec(outcome="loss", hold_ms=30 * 60000)]
    assert dd.hold_asymmetry(recs)["flag"] == ""


# ── fee_drag ─────────────────────────────────────────────────────────────────

def test_fee_drag_math():
    recs = [_rec(pnl_usd=1.0, pnl_net_usd=0.9),
            _rec(outcome="loss", pnl_usd=-0.5, pnl_net_usd=-0.6)]
    out = dd.fee_drag(recs)
    assert out["gross"] == 0.5
    assert out["net"] == 0.3
    assert out["drag"] == pytest.approx(-0.2, abs=1e-4)


# ── exit_pareto ──────────────────────────────────────────────────────────────

def test_exit_pareto_parses_string_pnl():
    events = [
        {"exit_reason": "software_stop", "pnl": "$0.6156"},
        {"exit_reason": "software_stop", "pnl": "$-0.30"},
        {"exit_reason": "time_stop", "pnl": "+$0.10"},
        {"exit_reason": "time_stop", "pnl": 0.05},
    ]
    out = dd.exit_pareto(events)
    assert out["software_stop"]["n"] == 2
    assert out["software_stop"]["pnl"] == pytest.approx(0.3156, abs=1e-3)
    assert out["time_stop"]["pnl"] == pytest.approx(0.15, abs=1e-3)


def test_exit_pareto_unknown_reason_and_bad_pnl():
    out = dd.exit_pareto([{"pnl": "garbage"}, {}])
    assert out["unknown"]["n"] == 2
    assert out["unknown"]["pnl"] == 0.0


def test_exit_pareto_sorted_worst_first():
    events = [{"exit_reason": "a", "pnl": 1.0},
              {"exit_reason": "b", "pnl": -2.0}]
    keys = list(dd.exit_pareto(events).keys())
    assert keys == ["b", "a"]


# ── silence_census ───────────────────────────────────────────────────────────

def test_silence_census_data_vs_gate_kind():
    assets = ["AAA-USD", "BBB-USD", "CCC-USD", "DDD-USD"]
    ready = Counter({"DDD-USD": 5})
    vetoes = Counter({("AAA-USD", "signal_stale_data"): 42,
                      ("BBB-USD", "coherence_tier_reject"): 7,
                      ("BBB-USD", "signal_stale_data"): 3})
    out = dd.silence_census(assets, ready, vetoes)
    bysym = {d["symbol"]: d for d in out}
    assert "DDD-USD" not in bysym                       # had signals — not silent
    assert bysym["AAA-USD"]["kind"] == "data"
    assert bysym["BBB-USD"]["top_veto"] == "coherence_tier_reject"
    assert bysym["BBB-USD"]["kind"] == "gate"
    assert bysym["CCC-USD"]["top_veto"] == "no_events_at_all"
    assert out[0]["symbol"] == "AAA-USD"                # sorted by veto_count desc


# ── coherence_calibration ────────────────────────────────────────────────────

def test_coherence_buckets():
    recs = [_rec(coherence=5.5, pnl_net_usd=0.1),
            _rec(coherence=6.5, pnl_net_usd=0.2),
            _rec(coherence=7.5, pnl_net_usd=0.3),
            _rec(coherence=9.0, pnl_net_usd=0.4),
            _rec(coherence=8.0, outcome="loss", pnl_net_usd=-0.4)]
    out = dd.coherence_calibration(recs)
    assert set(out) == {"5-6", "6-7", "7-8", "8+"}
    assert out["8+"]["n"] == 2
    assert out["8+"]["wr"] == 0.5
    assert out["8+"]["expectancy"] == 0.0


def test_coherence_empty_buckets_omitted():
    out = dd.coherence_calibration([_rec(coherence=9.5)])
    assert set(out) == {"8+"}


# ── session attribution ──────────────────────────────────────────────────────

def test_session_of_boundaries():
    assert dd.session_of(0) == "asia" and dd.session_of(6) == "asia"
    assert dd.session_of(7) == "london" and dd.session_of(11) == "london"
    assert dd.session_of(12) == "us" and dd.session_of(20) == "us"
    assert dd.session_of(21) == "off_hours" and dd.session_of(23) == "off_hours"


def test_session_attribution_buckets():
    # 2025-08-17 08:00 UTC = london; 2025-08-17 14:00 UTC = us
    import calendar
    london_ts = calendar.timegm((2025, 8, 17, 8, 0, 0)) * 1000
    us_ts = calendar.timegm((2025, 8, 17, 14, 0, 0)) * 1000
    recs = [_rec(ts_ms=london_ts, pnl_net_usd=0.5),
            _rec(ts_ms=us_ts, outcome="loss", pnl_net_usd=-0.2)]
    out = dd.session_attribution(recs)
    assert out["london"]["n"] == 1 and out["london"]["pnl"] == 0.5
    assert out["us"]["wr"] == 0.0


# ── slippage ─────────────────────────────────────────────────────────────────

def test_slippage_bps_minute_alignment_and_fallback():
    fills = [{"ts_ms": 1755400061000, "price": 101.0},   # minute 1755400020000... use exact
             {"ts_ms": 1755400062000, "price": 99.0}]
    m0 = 1755400061000 // 60000 * 60000
    kc = {m0: 100.0, m0 - 60000: 50.0}
    out = dd.slippage_bps(fills, kc)
    assert len(out) == 2
    assert out[0] == pytest.approx(100.0, abs=0.1)    # paid 1% above ref
    assert out[1] == pytest.approx(-100.0, abs=0.1)


def test_slippage_bps_prev_minute_fallback():
    ts = 1755400061000
    m0 = ts // 60000 * 60000
    out = dd.slippage_bps([{"ts_ms": ts, "price": 100.0}], {m0 - 60000: 100.0})
    assert out == [0.0]


def test_slippage_bps_skips_missing_reference():
    out = dd.slippage_bps([{"ts_ms": 1755400061000, "price": 100.0}], {})
    assert out == []


def test_summarize_slippage_flag_and_skipped():
    per_venue = {"bybit": [20.0, -15.0], "yahoo": [1.0]}
    out = dd.summarize_slippage(per_venue, {"SSI-USD": 3})
    assert out["bybit"]["flag"] == "systematic_slippage"   # avg 17.5 > 10
    assert out["bybit"]["avg_abs_bps"] == 17.5
    assert out["bybit"]["mean_signed_bps"] == 2.5
    assert out["yahoo"]["flag"] == ""
    assert out["_skipped"] == {"SSI-USD": 3}


# ── trend_capture (2026-08-20 — the MISSED_TREND verdict the watchdog reads) ──

def test_trend_capture_unknown_without_evidence():
    out = dd.trend_capture([], None, {}, 400.0)
    assert out["verdict"] == "unknown"


def test_trend_capture_quiet_day_below_thresholds():
    recs = [_rec(direction="long")]
    out = dd.trend_capture(recs, 1.2, {"BTC": 0.8, "ETH": 1.1}, 400.0)
    assert out["verdict"] == "quiet_day"


def test_trend_capture_missed_uptrend_counter_traded():
    recs = [_rec(direction="short", outcome="loss", pnl_net_usd=-2.0),
            _rec(direction="short", outcome="loss", pnl_net_usd=-1.0)]
    out = dd.trend_capture(recs, 8.0, {}, 400.0)
    assert out["verdict"] == "MISSED_TREND"
    assert out["trend_direction"] == "long"
    assert out["trend_side_pnl_usd"] == 0.0
    assert out["counter_side_pnl_usd"] == -3.0
    assert out["counter_traded"] is True
    assert out["counter_side_trades"] == 2


def test_trend_capture_ok_when_trend_side_positive():
    recs = [_rec(direction="long", pnl_net_usd=1.5),
            _rec(direction="short", outcome="loss", pnl_net_usd=-0.5)]
    out = dd.trend_capture(recs, 5.5, {}, 400.0)
    assert out["verdict"] == "ok"
    assert out["trend_side_pnl_usd"] == 1.5


def test_trend_capture_downtrend_is_a_trend_day():
    # Symmetric: a -6% day is a trend day; shorts are the trend side.
    recs = [_rec(direction="short", pnl_net_usd=2.0)]
    out = dd.trend_capture(recs, -6.0, {}, 400.0)
    assert out["verdict"] == "ok"
    assert out["trend_direction"] == "short"

    recs_long = [_rec(direction="long", outcome="loss", pnl_net_usd=-1.0)]
    out2 = dd.trend_capture(recs_long, -6.0, {}, 400.0)
    assert out2["verdict"] == "MISSED_TREND"
    assert out2["trend_direction"] == "short"
    assert out2["counter_traded"] is True


def test_trend_capture_4h_thrust_when_daily_bar_ambiguous():
    # Daily bar small (1.5%) but a synchronized -3% 4h thrust = trend evidence.
    recs = [_rec(direction="long", outcome="loss", pnl_net_usd=-1.0)]
    out = dd.trend_capture(recs, 1.5,
                           {"BTC": -3.0, "ETH": -3.4, "SOL": -2.6}, 400.0)
    assert out["verdict"] == "MISSED_TREND"
    assert out["trend_direction"] == "short"
    assert out["trend_magnitude_pct"] == 3.0


def test_trend_capture_day_boundary_at_3pct():
    assert dd.trend_capture([], 3.0, {}, 400.0)["verdict"] == "MISSED_TREND"
    assert dd.trend_capture([], 2.99, {}, 400.0)["verdict"] == "quiet_day"


def test_trend_capture_side_normalization_and_unresolved():
    # BUY/SELL shapes normalize; unknown directions are ignored, not misread.
    recs = [_rec(direction="Buy", pnl_net_usd=1.0),
            _rec(direction="none", outcome="loss", pnl_net_usd=-9.0)]
    out = dd.trend_capture(recs, 4.0, {}, 400.0)
    assert out["verdict"] == "ok"
    assert out["trend_side_trades"] == 1
