import asyncio
from types import SimpleNamespace

import pytest

from data.bybit_feed import BybitFeed
from data.candle_buffer import CandleBuffer
from main import _bybit_funding_rate

H = 3_600_000
BAR15 = 15 * 60_000
T0 = 1_758_000_000_000  # fixed epoch ms, 15m-aligned


def _feed(**over):
    cfg = SimpleNamespace(assets=["BTC-USD"], core_assets=["BTC-USD"], **over)
    bufs = {
        "BTC-USD": {
            "1m": CandleBuffer(symbol="BTC-USD", interval="1m"),
            "5m": CandleBuffer(symbol="BTC-USD", interval="5m"),
            "15m": CandleBuffer(symbol="BTC-USD", interval="15m"),
            "4h": CandleBuffer(symbol="BTC-USD", interval="4h"),
        }
    }
    return BybitFeed(config=cfg, mark_price_stores={}, orderbook_stores={},
                     candle_buffers=bufs, trade_flow_stores={})


def _kline_msg(tf: str, start: int = T0, close: str = "100.5"):
    return {"topic": f"kline.{tf}.BTCUSDT", "data": [{
        "start": str(start), "open": "100", "high": "101", "low": "99",
        "close": close, "volume": "7", "end": str(start + BAR15 - 1),
        "confirm": True,
    }]}


# ── Subscription ─────────────────────────────────────────────────────────────

def test_build_topics_includes_kline15():
    feed = _feed()
    topics = feed._build_topics("BTC-USD")
    assert "kline.15.BTCUSDT" in topics
    assert "kline.1.BTCUSDT" in topics
    assert "kline.5.BTCUSDT" in topics
    assert "kline.240.BTCUSDT" in topics


# ── Handler mapping (the 1m-plane corruption guard) ──────────────────────────

def test_kline15_writes_15m_buffer_never_1m():
    feed = _feed()
    asyncio.run(feed._handle(_kline_msg("15")))
    assert feed.candle_buffers["BTC-USD"]["15m"].count() == 1
    assert feed.candle_buffers["BTC-USD"]["1m"].count() == 0
    assert feed.candle_buffers["BTC-USD"]["5m"].count() == 0
    assert feed.candle_buffers["BTC-USD"]["4h"].count() == 0
    assert feed._last_candle_cache["BTC-USD"]["15m"].close == 100.5
    assert "1m" not in feed._last_candle_cache["BTC-USD"]


def test_kline_legacy_mappings_unchanged():
    feed = _feed()
    asyncio.run(feed._handle(_kline_msg("1")))
    asyncio.run(feed._handle(_kline_msg("5")))
    asyncio.run(feed._handle(_kline_msg("240")))
    assert feed.candle_buffers["BTC-USD"]["1m"].count() == 1
    assert feed.candle_buffers["BTC-USD"]["5m"].count() == 1
    assert feed.candle_buffers["BTC-USD"]["4h"].count() == 1
    assert feed.candle_buffers["BTC-USD"]["15m"].count() == 0


# ── Funding fallback helper ──────────────────────────────────────────────────

def test_bybit_funding_rate_reads_ticker_store():
    stores = {"UNI-USD": {"funding_rate": 0.0001, "open_interest": 1e6}}
    assert _bybit_funding_rate(stores, "UNI-USD") == pytest.approx(0.0001)


def test_bybit_funding_rate_abstains_when_absent():
    assert _bybit_funding_rate({}, "UNI-USD") is None
    assert _bybit_funding_rate({"UNI-USD": {}}, "UNI-USD") is None
    assert _bybit_funding_rate({"UNI-USD": {"funding_rate": None}}, "UNI-USD") is None
    assert _bybit_funding_rate({"UNI-USD": {"funding_rate": "garbage"}},
                               "UNI-USD") is None
