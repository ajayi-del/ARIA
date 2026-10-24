"""Phase 1b/1c pins — extended Bybit ticker capture + 4h boot seed path.

Kill-switch contract: env read at call time; False = legacy bit-for-bit
(ticker store keeps exactly the legacy 5 keys; 4h buffer stays maxlen=50 and
seeds 50 bars).
"""
import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

import data.bybit_feed as bybit_feed_mod
from data.bybit_feed import BybitFeed
from data.candle_buffer import CandleBuffer
from data.klines_4h import KLINE_4H_WS_DEPTH

H = 3_600_000
BAR = 4 * H
T0 = 1_758_000_000_000

LEGACY_KEYS = {"funding_rate", "open_interest", "prev_open_interest",
               "prev_mark_price", "mark_price"}


def _ticker_msg(**over):
    data = {
        "markPrice": "67000.5",
        "lastPrice": "67001.0",
        "fundingRate": "0.0001",
        "openInterest": "12345.6",
        "indexPrice": "66999.9",
        "nextFundingTime": "1761235200000",
        "turnover24h": "987654.3",
    }
    data.update(over)
    return {"topic": "tickers.BTCUSDT", "data": data}


def _feed():
    stores = {"BTC-USD": {}}
    feed = BybitFeed(SimpleNamespace(assets=["BTC-USD"]),
                     {}, {}, {}, {}, bybit_ticker_stores=stores)
    return feed, stores


def test_extended_fields_captured_when_on(monkeypatch):
    monkeypatch.delenv("BYBIT_TICKER_EXTENDED_ENABLED", raising=False)
    feed, stores = _feed()
    before_ms = int(time.time() * 1000)
    asyncio.run(feed._handle(_ticker_msg()))
    st = stores["BTC-USD"]
    assert st["index_price"] == pytest.approx(66999.9)
    assert st["next_funding_time"] == 1761235200000
    assert before_ms <= st["extended_ts"]
    # legacy keys intact alongside
    assert LEGACY_KEYS <= set(st)
    assert st["mark_price"] == pytest.approx(67000.5)
    assert st["open_interest"] == pytest.approx(12345.6)


def test_extended_fields_absent_when_off(monkeypatch):
    monkeypatch.setenv("BYBIT_TICKER_EXTENDED_ENABLED", "false")
    feed, stores = _feed()
    asyncio.run(feed._handle(_ticker_msg()))
    assert set(stores["BTC-USD"]) == LEGACY_KEYS


def test_env_read_at_call_time(monkeypatch):
    feed, stores = _feed()
    monkeypatch.setenv("BYBIT_TICKER_EXTENDED_ENABLED", "false")
    asyncio.run(feed._handle(_ticker_msg()))
    assert "index_price" not in stores["BTC-USD"]
    monkeypatch.setenv("BYBIT_TICKER_EXTENDED_ENABLED", "true")
    asyncio.run(feed._handle(_ticker_msg()))
    assert stores["BTC-USD"]["index_price"] == pytest.approx(66999.9)


def test_delta_push_carries_prev_forward(monkeypatch):
    monkeypatch.delenv("BYBIT_TICKER_EXTENDED_ENABLED", raising=False)
    feed, stores = _feed()
    asyncio.run(feed._handle(_ticker_msg()))
    first_ts = stores["BTC-USD"]["extended_ts"]
    # Delta push: indexPrice/nextFundingTime omitted (unchanged venue-side)
    delta = _ticker_msg(markPrice="67100.0")
    delta["data"].pop("indexPrice")
    delta["data"].pop("nextFundingTime")
    asyncio.run(feed._handle(delta))
    st = stores["BTC-USD"]
    assert st["index_price"] == pytest.approx(66999.9)
    assert st["next_funding_time"] == 1761235200000
    assert st["extended_ts"] >= first_ts
    assert st["mark_price"] == pytest.approx(67100.0)


def test_garbage_extended_fields_fail_closed(monkeypatch):
    monkeypatch.delenv("BYBIT_TICKER_EXTENDED_ENABLED", raising=False)
    feed, stores = _feed()
    asyncio.run(feed._handle(_ticker_msg(indexPrice="junk", nextFundingTime="x")))
    st = stores["BTC-USD"]
    # feed loop never raised; legacy capture still landed; garbage not stored
    assert st["mark_price"] == pytest.approx(67000.5)
    assert "index_price" not in st
    assert "next_funding_time" not in st


# ── Phase 1c boot seed: fetch_historical depth + buffer swap ─────────────────


def _rows(n: int) -> list:
    # newest-first, as Bybit v5 answers
    return [[str(T0 + (n - 1 - i) * BAR), "100", "101", "99", "100.5", "5"]
            for i in range(n)]


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def _mock_httpx(monkeypatch, rows_240, calls):
    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            calls.append(dict(params))
            rows = rows_240 if params.get("interval") == "240" else _rows(2)
            return _Resp({"retCode": 0, "result": {"list": rows}})

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)


def _buffers():
    return {"BTC-USD": {
        "1m": CandleBuffer(symbol="BTC-USD", interval="1m"),
        "5m": CandleBuffer(symbol="BTC-USD", interval="5m"),
        "15m": CandleBuffer(symbol="BTC-USD", interval="15m"),
        "4h": CandleBuffer(symbol="BTC-USD", interval="4h", maxlen=50),
    }}


def _seed_feed(buffers):
    return BybitFeed(SimpleNamespace(assets=["BTC-USD"]),
                     {}, {}, buffers, {})


def test_boot_seed_deepens_to_120(monkeypatch):
    monkeypatch.delenv("KLINE_4H_PLANE_ENABLED", raising=False)
    calls, events = [], []

    class _Spy:
        def info(self, event, **kw):
            events.append((event, kw))

        def warning(self, *a, **kw):
            pass

    monkeypatch.setattr(bybit_feed_mod, "logger", _Spy())
    _mock_httpx(monkeypatch, _rows(120), calls)
    bufs = _buffers()
    asyncio.run(_seed_feed(bufs).fetch_historical())

    buf = bufs["BTC-USD"]["4h"]
    assert buf.maxlen == KLINE_4H_WS_DEPTH == 120
    assert buf.count() == 120
    assert buf.candles[0].open_time == T0  # chronological, oldest kept
    assert buf.candles[-1].open_time == T0 + 119 * BAR
    limit_240 = [c["limit"] for c in calls if c["interval"] == "240"]
    assert limit_240 == [120]
    seeded = [kw for ev, kw in events if ev == "kline_4h_plane_seeded"]
    assert seeded and seeded[0]["n_symbols"] == 1


def test_boot_seed_legacy_when_off(monkeypatch):
    monkeypatch.setenv("KLINE_4H_PLANE_ENABLED", "false")
    calls = []
    _mock_httpx(monkeypatch, _rows(120), calls)
    bufs = _buffers()
    asyncio.run(_seed_feed(bufs).fetch_historical())

    buf = bufs["BTC-USD"]["4h"]
    assert buf.maxlen == 50  # untouched legacy depth
    assert buf.count() == 50  # deque keeps newest 50
    assert buf.candles[-1].open_time == T0 + 119 * BAR
    limit_240 = [c["limit"] for c in calls if c["interval"] == "240"]
    assert limit_240 == [50]


def test_boot_seed_fetch_failure_never_raises(monkeypatch):
    monkeypatch.delenv("KLINE_4H_PLANE_ENABLED", raising=False)

    class _Boom:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            raise RuntimeError("network down")

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    bufs = _buffers()
    asyncio.run(_seed_feed(bufs).fetch_historical())  # must not raise
    assert bufs["BTC-USD"]["4h"].maxlen == 120  # depth swap still applied
    assert bufs["BTC-USD"]["4h"].count() == 0
