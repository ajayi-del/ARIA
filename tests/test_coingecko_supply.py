"""Pins for data/coingecko_supply.py — CoinGecko circulating-supply plane.

HTTP is mocked by injecting a fake fetch_page callable into
CoinGeckoSupplyFeed — NO live network in tests.
"""
import json
import os

import pytest

from data.coingecko_supply import (
    CoinGeckoSupplyFeed,
    _base_symbol,
    _parse_markets_payload,
    coingecko_supply_enabled,
)


NOW_S = 1_800_000_000.0          # fixed clock for all tests
HOUR = 3600.0


def _coin(cid, ticker, price, supply, mcap):
    return {"id": cid, "symbol": ticker, "current_price": price,
            "circulating_supply": supply, "market_cap": mcap}


def _pages():
    """A healthy 4-page payload."""
    return [
        [_coin("bitcoin", "btc", 100_000.0, 19_000_000.0, 1.9e12),
         _coin("ethereum", "eth", 4_000.0, 120_000_000.0, 4.8e11)],
        [_coin("solana", "sol", 200.0, 450_000_000.0, 9.0e10)],
        [_coin("ripple", "xrp", 0.5, 55_000_000_000.0, 2.75e10)],
        [_coin("dogecoin", "doge", 0.15, 145_000_000_000.0, 2.175e10)],
    ]


def _feed(tmp_path, pages=None, now_s=NOW_S, fetch_page_fn=None, **kw):
    if fetch_page_fn is None:
        data = pages if pages is not None else _pages()
        fetch_page_fn = lambda page: data[page - 1]
    return CoinGeckoSupplyFeed(log_dir=str(tmp_path),
                               fetch_page_fn=fetch_page_fn,
                               time_fn=lambda: now_s, sleep_fn=lambda s: None,
                               **kw)


@pytest.fixture(autouse=True)
def _kill_switch_on(monkeypatch):
    monkeypatch.delenv("COINGECKO_SUPPLY_ENABLED", raising=False)
    yield


# ---------------------------------------------------------------- pure brains

class TestBaseSymbol:
    def test_aria_form_extracts_base(self):
        assert _base_symbol("BTC-USD") == "BTC"
        assert _base_symbol("1000pepe-usd") == "1000PEPE"

    def test_bare_and_edge(self):
        assert _base_symbol("ETH") == "ETH"
        assert _base_symbol("") == ""
        assert _base_symbol(None) == ""


class TestParseMarketsPayload:
    def test_ticker_collision_resolves_to_highest_mcap(self):
        pages = [[_coin("real", "bat", 1.0, 100.0, 500.0),
                  _coin("imposter", "bat", 2.0, 100.0, 10.0)]]
        rows = _parse_markets_payload(pages)
        assert rows["BAT"]["cg_id"] == "real"
        assert rows["BAT"]["supply_usd"] == pytest.approx(100.0)

    def test_collision_across_pages(self):
        pages = [[_coin("small", "btc", 1.0, 10.0, 5.0)],
                 [], [],
                 [_coin("big", "btc", 2.0, 10.0, 9.0e11)]]
        rows = _parse_markets_payload(pages)
        assert rows["BTC"]["cg_id"] == "big"

    def test_null_or_zero_supply_excluded(self):
        pages = [[_coin("a", "AAA", 1.0, None, 100.0),
                  _coin("b", "BBB", 1.0, 0.0, 100.0),
                  _coin("c", "CCC", 1.0, 50.0, 100.0)]]
        rows = _parse_markets_payload(pages)
        assert "AAA" not in rows
        assert "BBB" not in rows
        assert rows["CCC"]["supply_usd"] == pytest.approx(50.0)

    def test_missing_price_excluded(self):
        pages = [[_coin("a", "AAA", None, 100.0, 100.0)]]
        assert _parse_markets_payload(pages) == {}

    def test_supply_usd_is_supply_times_price(self):
        rows = _parse_markets_payload(_pages())
        assert rows["BTC"]["supply_usd"] == pytest.approx(100_000.0 * 19_000_000.0)
        assert rows["BTC"]["mcap"] == pytest.approx(1.9e12)

    def test_missing_symbol_absent(self):
        rows = _parse_markets_payload(_pages())
        assert "NOSUCHCOIN" not in rows

    def test_malformed_page_raises(self):
        with pytest.raises(ValueError):
            _parse_markets_payload([[{"symbol": "btc"}], {"not": "a list"}])

    def test_bad_rows_kill_only_themselves(self):
        pages = [[_coin("ok", "okc", 1.0, 10.0, 5.0), None, "garbage",
                  {"symbol": "bad", "current_price": "x", "circulating_supply": 1}]]
        rows = _parse_markets_payload(pages)
        assert list(rows.keys()) == ["OKC"]


# ---------------------------------------------------------------- fetcher

class TestFeed:
    def test_refresh_populates_and_lookup(self, tmp_path):
        feed = _feed(tmp_path)
        assert feed.refresh() is True
        assert feed.circulating_supply_usd("BTC-USD") == pytest.approx(1.9e12)
        assert feed.circulating_supply_usd("ETH-USD") == pytest.approx(4.8e11)

    def test_absent_symbol_returns_none(self, tmp_path):
        feed = _feed(tmp_path)
        feed.refresh()
        assert feed.circulating_supply_usd("NOSUCHCOIN-USD") is None

    def test_atomic_write_tmp_replaced(self, tmp_path):
        feed = _feed(tmp_path)
        assert feed.refresh() is True
        cache = tmp_path / "coingecko_supply.json"
        assert cache.exists()
        assert not (tmp_path / "coingecko_supply.json.tmp").exists()
        d = json.loads(cache.read_text())
        assert d["fetched_at"] == pytest.approx(NOW_S)
        assert d["rows"]["BTC"]["cg_id"] == "bitcoin"
        assert d["rows"]["BTC"]["supply_usd"] == pytest.approx(1.9e12)

    def test_cache_loaded_on_boot_and_served_fresh(self, tmp_path):
        (tmp_path / "coingecko_supply.json").write_text(json.dumps({
            "fetched_at": NOW_S - 2 * HOUR,          # 2h old < 20h max_age
            "rows": {"BTC": {"supply_usd": 1.0e12, "mcap": 1.0e12, "cg_id": "bitcoin"}},
        }))
        calls = []
        feed = _feed(tmp_path, fetch_page_fn=lambda p: calls.append(p) or _pages()[p - 1])
        assert feed.refresh_if_stale() is False      # fresh cache, no network
        assert calls == []
        assert feed.circulating_supply_usd("BTC-USD") == pytest.approx(1.0e12)

    def test_stale_cache_triggers_refresh(self, tmp_path):
        (tmp_path / "coingecko_supply.json").write_text(json.dumps({
            "fetched_at": NOW_S - 30 * HOUR,         # 30h > 20h max_age, < 72h hard
            "rows": {"BTC": {"supply_usd": 1.0, "mcap": 1.0, "cg_id": "old"}},
        }))
        calls = []
        feed = _feed(tmp_path,
                     fetch_page_fn=lambda p: calls.append(p) or _pages()[p - 1])
        assert feed.refresh_if_stale() is True
        assert calls == [1, 2, 3, 4]
        assert feed.circulating_supply_usd("BTC-USD") == pytest.approx(1.9e12)

    def test_beyond_hard_stale_returns_none(self, tmp_path):
        (tmp_path / "coingecko_supply.json").write_text(json.dumps({
            "fetched_at": NOW_S - 73 * HOUR,
            "rows": {"BTC": {"supply_usd": 1.0e12, "mcap": 1.0e12, "cg_id": "bitcoin"}},
        }))
        feed = _feed(tmp_path, fetch_page_fn=lambda p: (_ for _ in ()).throw(RuntimeError("net down")))
        assert feed.refresh_if_stale() is False      # refresh attempted, failed
        assert feed.circulating_supply_usd("BTC-USD") is None

    def test_failed_refresh_keeps_old_cache_within_hard_stale(self, tmp_path):
        (tmp_path / "coingecko_supply.json").write_text(json.dumps({
            "fetched_at": NOW_S - 30 * HOUR,
            "rows": {"BTC": {"supply_usd": 5.0e11, "mcap": 5.0e11, "cg_id": "bitcoin"}},
        }))
        feed = _feed(tmp_path, fetch_page_fn=lambda p: (_ for _ in ()).throw(RuntimeError("net down")))
        assert feed.refresh_if_stale() is False
        assert feed.circulating_supply_usd("BTC-USD") == pytest.approx(5.0e11)

    def test_one_bad_page_aborts_refresh_old_cache_preserved(self, tmp_path):
        (tmp_path / "coingecko_supply.json").write_text(json.dumps({
            "fetched_at": NOW_S - 30 * HOUR,
            "rows": {"ETH": {"supply_usd": 4.8e11, "mcap": 4.8e11, "cg_id": "ethereum"}},
        }))

        def fetch(page):
            if page == 3:
                return {"malformed": True}          # non-list page
            return _pages()[page - 1]

        feed = _feed(tmp_path, fetch_page_fn=fetch)
        assert feed.refresh_if_stale() is False
        assert feed.circulating_supply_usd("ETH-USD") == pytest.approx(4.8e11)
        # disk untouched — never persist a partial-day map
        d = json.loads((tmp_path / "coingecko_supply.json").read_text())
        assert d["fetched_at"] == pytest.approx(NOW_S - 30 * HOUR)
        assert list(d["rows"].keys()) == ["ETH"]

    def test_page_exception_aborts_whole_refresh(self, tmp_path):
        def fetch(page):
            if page == 2:
                raise RuntimeError("boom")
            return _pages()[page - 1]

        feed = _feed(tmp_path, fetch_page_fn=fetch)
        assert feed.refresh() is False
        assert not (tmp_path / "coingecko_supply.json").exists()
        assert feed.circulating_supply_usd("BTC-USD") is None

    def test_empty_map_aborts(self, tmp_path):
        feed = _feed(tmp_path, pages=[[], [], [], []])
        assert feed.refresh() is False

    def test_kill_switch_off_lookup_none_refresh_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COINGECKO_SUPPLY_ENABLED", "false")
        assert coingecko_supply_enabled() is False
        calls = []
        feed = _feed(tmp_path,
                     fetch_page_fn=lambda p: calls.append(p) or _pages()[p - 1])
        assert feed.refresh() is False
        assert feed.refresh_if_stale() is False
        assert calls == []                            # refresh no-op, no network
        assert feed.circulating_supply_usd("BTC-USD") is None
        monkeypatch.setenv("COINGECKO_SUPPLY_ENABLED", "true")
        assert coingecko_supply_enabled() is True     # read at call time

    def test_symbol_map_and_last_fetch_age(self, tmp_path):
        feed = _feed(tmp_path)
        assert feed.last_fetch_age_s() is None        # never fetched
        feed.refresh()
        assert feed.last_fetch_age_s() == pytest.approx(0.0)
        m = feed.symbol_map()
        assert set(m) == {"BTC", "ETH", "SOL", "XRP", "DOGE"}
        assert m["SOL"]["cg_id"] == "solana"
        m["SOL"]["supply_usd"] = -1.0                 # copy — feed state untouched
        assert feed.circulating_supply_usd("SOL-USD") == pytest.approx(9.0e10)

    def test_retry_once_on_429_then_success(self, tmp_path, monkeypatch):
        """The default HTTP layer retries a 429 once, then succeeds."""
        import data.coingecko_supply as mod

        class _Resp:
            def __init__(self, status, payload=None):
                self.status_code = status
                self._payload = payload or []

            def json(self):
                return self._payload

            def raise_for_status(self):
                if self.status_code != 200:
                    raise RuntimeError(f"http {self.status_code}")

        responses = [_Resp(429), _Resp(200, _pages()[0])]
        sleeps = []

        class _Client:
            def __init__(self, timeout):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, params=None):
                return responses.pop(0)

        import httpx
        monkeypatch.setattr(httpx, "Client", _Client)
        feed = CoinGeckoSupplyFeed(log_dir=str(tmp_path),
                                   time_fn=lambda: NOW_S,
                                   sleep_fn=sleeps.append)
        out = feed._http_fetch_page(1)
        assert out == _pages()[0]
        assert sleeps == [mod._RETRY_BACKOFF_S]
