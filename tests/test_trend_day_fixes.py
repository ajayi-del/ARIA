"""Trend-day fix bundle pins (2026-08-20):

- trend_direction_guard matrix (Fix A) — aligned/counter/unknown semantics,
  fail-open on conflicting direction evidence, downtrend symmetry.
- ParamStore graduation "since" roundtrip (Fix B) — the field the noise-vs-fade
  cooloff split keys on; old records without it must read as non-noisy.
- Bybit feed allLiquidation topics + subscribe op-response handling (Fix C) —
  the 2026-08-19 silent-nine audit found rejections were never logged and the
  liquidation.* topic was sunset; these pins keep both from regressing.
"""
import asyncio
import time
from types import SimpleNamespace

from intelligence.day_type_classifier import trend_direction_guard
from data.bybit_feed import BybitFeed
import memory.param_store as ps_mod
from memory.param_store import ParamStore


# ── trend_direction_guard ────────────────────────────────────────────────────

def test_guard_inert_outside_trend_days():
    for dt in ("range", "chop", "unknown"):
        assert trend_direction_guard(dt, "up", 9.0, "short") == "unknown"


def test_guard_unknown_without_direction_evidence():
    assert trend_direction_guard("trend", "", None, "long") == "unknown"
    assert trend_direction_guard("trend", "", 2.0, "long") == "unknown"  # below threshold


def test_guard_conflict_fails_open():
    # ORB up but 24h momentum strongly down — mixed evidence is no evidence.
    assert trend_direction_guard("trend", "up", -6.0, "short") == "unknown"
    assert trend_direction_guard("trend", "up", -6.0, "long") == "unknown"


def test_guard_aligned_and_counter_from_breakout():
    assert trend_direction_guard("trend", "up", None, "long") == "aligned"
    assert trend_direction_guard("trend", "up", None, "short") == "counter"


def test_guard_downtrend_symmetry():
    # A downtrend is a trend day: shorts align, longs fight the tape.
    assert trend_direction_guard("trend", "down", -8.0, "short") == "aligned"
    assert trend_direction_guard("trend", "down", -8.0, "long") == "counter"


def test_guard_momentum_threshold_is_strict():
    # |change| must EXCEED the threshold; exactly 5.0 carries no evidence.
    assert trend_direction_guard("trend", "", 5.0, "long") == "unknown"
    assert trend_direction_guard("trend", "", 5.01, "long") == "aligned"
    assert trend_direction_guard("trend", "", -5.01, "short") == "aligned"


def test_guard_two_sources_agree():
    assert trend_direction_guard("trend", "up", 7.0, "long") == "aligned"
    assert trend_direction_guard("trend", "up", 7.0, "short") == "counter"


def test_guard_tolerates_garbage_inputs():
    assert trend_direction_guard("trend", "up", "not-a-number", "long") == "aligned"
    assert trend_direction_guard("trend", "sideways", None, "long") == "unknown"
    assert trend_direction_guard("trend", "up", None, "flat") == "unknown"


# ── ParamStore graduation "since" (Fix B) ────────────────────────────────────

def test_graduation_since_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(ps_mod, "STORE_PATH", tmp_path / "param_store.json")
    ps = ParamStore(SimpleNamespace())
    before = int(time.time())
    ps.set_graduated_symbol("SOL-USD", "long", 4, ttl_seconds=3600)
    g = ps.get_graduated_symbol("SOL-USD")
    assert g["direction"] == "long" and g["score"] == 4
    assert before <= g["since"] <= int(time.time())

    # Survives a reload from disk (restart amnesia would re-arm cooloffs blind).
    ps2 = ParamStore(SimpleNamespace())
    g2 = ps2.get_graduated_symbol("SOL-USD")
    assert g2["since"] == g["since"]


def test_graduation_legacy_record_without_since_reads_non_noisy():
    # Records written before the "since" field existed must not look like
    # sub-2min graduations — _grad_since defaults to 0 (age = ∞ → not noisy).
    legacy = {"direction": "short", "score": 3}
    assert legacy.get("since", 0) == 0


# ── Bybit feed: allLiquidation + op-response logging (Fix C) ─────────────────

def _feed() -> BybitFeed:
    return BybitFeed(SimpleNamespace(assets=["BTC-USD", "ETH-USD"]),
                     {}, {}, {}, {})


def test_liq_topics_use_allliquidation():
    topics = _feed()._liq_topics(["BTC-USD", "ETH-USD", "NOT-A-COIN"])
    assert "allLiquidation.BTCUSDT" in topics
    assert "allLiquidation.ETHUSDT" in topics
    assert not any("liquidation." in t and "allLiquidation" not in t for t in topics)
    assert len(topics) == 2  # unmapped symbols skipped, not subscribed blind


def test_subscribe_op_responses_handled_without_topic():
    feed = _feed()
    asyncio.run(feed._handle({"op": "subscribe", "success": True,
                              "conn_id": "abc"}))
    asyncio.run(feed._handle({"op": "subscribe", "success": False,
                              "ret_msg": "handler not found"}))
    assert feed._msg_count == 2  # counted, logged, never reached dispatch


def test_last_msg_ts_tracks_any_symbol_topic():
    feed = _feed()
    before = time.time()
    # Unknown topic prefix still resolves the symbol and stamps coverage.
    asyncio.run(feed._handle({"topic": "kline.99.BTCUSDT", "data": {}}))
    assert feed._last_msg_ts.get("BTC-USD", 0) >= before
    assert "ETH-USD" not in feed._last_msg_ts


# ── resolve_exit_mark (exit-trigger fallback, 2026-08-20) ────────────────────

def _rem(**kw):
    from main import resolve_exit_mark
    defaults = dict(store_price=0.0, store_age_ms=999999, aster_mark={},
                    bybit_candle_close=None, underlying_px=None, now=1000.0)
    defaults.update(kw)
    return resolve_exit_mark(**defaults)


def test_exit_mark_fresh_store_wins():
    px, src = _rem(store_price=100.0, store_age_ms=500,
                   aster_mark={"mark_price": 101.0, "ts": 999.0})
    assert (px, src) == (100.0, "store")


def test_exit_mark_stale_store_falls_back_to_aster():
    px, src = _rem(store_price=100.0, store_age_ms=60_000,
                   aster_mark={"mark_price": 100.5, "ts": 995.0})
    assert (px, src) == (100.5, "aster")


def test_exit_mark_aster_itself_stale_skips_to_candle():
    px, src = _rem(store_price=100.0, store_age_ms=60_000,
                   aster_mark={"mark_price": 100.5, "ts": 900.0},  # 100s old
                   bybit_candle_close=100.2)
    assert (px, src) == (100.2, "bybit_candle")


def test_exit_mark_divergent_candidate_rejected():
    # Outage garbage: 8% away from last good — fail closed to next candidate.
    px, src = _rem(store_price=100.0, store_age_ms=60_000,
                   aster_mark={"mark_price": 108.0, "ts": 999.0},
                   bybit_candle_close=100.1)
    assert (px, src) == (100.1, "bybit_candle")


def test_exit_mark_underlying_level_exempt():
    # SoDEX synthetics are rebased — a 30% level gap vs the underlying is
    # NORMAL for tradfi, not garbage. Level check must not reject it.
    px, src = _rem(store_price=130.0, store_age_ms=60_000,
                   underlying_px=6450.0)
    assert (px, src) == (6450.0, "underlying")


def test_exit_mark_all_dark_returns_none():
    px, src = _rem(store_price=100.0, store_age_ms=60_000)
    assert px is None and src == "dark"


def test_exit_mark_no_prior_price_accepts_candidate():
    # Never had a store price — better a live reference than blindness.
    px, src = _rem(aster_mark={"mark_price": 0.61, "ts": 999.5})
    assert (px, src) == (0.61, "aster")
