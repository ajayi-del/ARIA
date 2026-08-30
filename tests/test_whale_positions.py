"""Whale Position Plane pins (2026-08-30): live-validated Aster RPC /
Hyperliquid parsers, delta engine (aged-bag silence, observed-open
confidence, emission floor, flip/close), vanish-check close detection,
tier ladder, never-raises poll."""
import pytest

from data.whale_positions import (ADDED, CLOSED, FLIPPED, OPENED,
                                  WhalePositionPlane, normalize_symbol,
                                  parse_aster_balance,
                                  parse_hl_clearinghouse, whale_features,
                                  whale_tier)

NOW = 1_700_000_000.0


# ── Parsers (live schema fixtures from the 2026-08-30 probes) ─────────────

class TestParseAster:
    def _payload(self, amount="44.835", notional="3500000.0",
                 upnl="-50000.0", symbol="BTCUSDT"):
        return {"jsonrpc": "2.0", "id": 1, "result": {
            "perpAssets": [{"asset": "USDT", "walletBalance": 69747.64}],
            "positions": [{"positions": [{
                "symbol": symbol, "positionAmount": amount,
                "positionSide": "BOTH", "notionalValue": notional,
                "unrealizedProfit": upnl, "cumRealized": "123.0"}]}]}}

    def test_long_derived_fields(self):
        out = parse_aster_balance(self._payload(), "0xABC", NOW)
        assert len(out) == 1
        p = out[0]
        assert p["venue"] == "aster" and p["symbol"] == "BTC-USD"
        assert p["side"] == "long" and p["size"] == pytest.approx(44.835)
        mark = 3_500_000.0 / 44.835
        assert p["mark_price"] == pytest.approx(mark)
        # long entry = mark − upnl/|size| (upnl negative → entry above mark)
        assert p["entry_price"] == pytest.approx(mark + 50_000.0 / 44.835)
        assert p["margin_used_usd"] is None       # native-only doctrine
        assert p["leverage"] is None
        assert p["account_value_usd"] == pytest.approx(69747.64)
        assert p["confidence"] == 0.9 and p["source"] == "aster_rpc"
        assert p["updated_at"] == NOW

    def test_short_derived_fields(self):
        out = parse_aster_balance(
            self._payload(amount="-100.0", notional="500000.0",
                          upnl="10000.0", symbol="ETHUSDT"), "0xABC", NOW)
        p = out[0]
        assert p["side"] == "short" and p["size"] == pytest.approx(100.0)
        mark = 500_000.0 / 100.0
        # short entry = mark + upnl/|size|
        assert p["entry_price"] == pytest.approx(mark + 10_000.0 / 100.0)

    def test_malformed_rows_skipped_never_fatal(self):
        payload = {"result": {"positions": [{"positions": [
            {"symbol": "BTCUSDT", "positionAmount": "0"},      # zero size
            {"symbol": "BTCUSDT", "positionAmount": "abc"},    # garbage
            {"symbol": "", "positionAmount": "5"},             # no symbol
            {"symbol": "BTCUSDT", "positionAmount": "5"},      # no notional
        ]}]}}
        assert parse_aster_balance(payload, "0xABC", NOW) == []

    def test_non_dict_result_empty(self):
        assert parse_aster_balance({"error": {"code": -32603}}, "0xA", NOW) == []
        assert parse_aster_balance({}, "0xA", NOW) == []


class TestParseHL:
    def _payload(self):
        return {"marginSummary": {"accountValue": "204818.62"},
                "assetPositions": [{"position": {
                    "coin": "ETH", "szi": "4254.378", "entryPx": "2400.5",
                    "positionValue": "10460000.0",
                    "unrealizedPnl": "253000.0", "liquidationPx": "1800.0",
                    "marginUsed": "209200.0",
                    "leverage": {"type": "cross", "value": "50"}}}]}

    def test_native_fields(self):
        out = parse_hl_clearinghouse(self._payload(), "0xDEF", NOW)
        assert len(out) == 1
        p = out[0]
        assert p["venue"] == "hyperliquid" and p["symbol"] == "ETH-USD"
        assert p["side"] == "long" and p["size"] == pytest.approx(4254.378)
        assert p["entry_price"] == pytest.approx(2400.5)
        assert p["margin_used_usd"] == pytest.approx(209200.0)   # native
        assert p["leverage"] == pytest.approx(50.0)
        assert p["liquidation_price"] == pytest.approx(1800.0)
        assert p["account_value_usd"] == pytest.approx(204818.62)
        assert p["confidence"] == 1.0 and p["source"] == "hyperliquid"

    def test_empty_positions_valid_negative(self):
        assert parse_hl_clearinghouse(
            {"marginSummary": {"accountValue": "100"}, "assetPositions": []},
            "0xDEF", NOW) == []
        assert parse_hl_clearinghouse(None, "0xDEF", NOW) == []

    def test_short_szi(self):
        pl = self._payload()
        pl["assetPositions"][0]["position"]["szi"] = "-10.0"
        out = parse_hl_clearinghouse(pl, "0xDEF", NOW)
        assert out[0]["side"] == "short" and out[0]["size"] == pytest.approx(10.0)


# ── Tiers / symbols / features ────────────────────────────────────────────

class TestTiersAndSymbols:
    def test_tier_ladder(self):
        assert whale_tier(25e6) == "LEVIATHAN"
        assert whale_tier(20e6) == "LEVIATHAN"
        assert whale_tier(6e6) == "MEGA_WHALE"
        assert whale_tier(2e6) == "LARGE_WHALE"
        assert whale_tier(7e5) == "WHALE"
        assert whale_tier(1.5e5) == "LARGE"
        assert whale_tier(50e3) == "RETAIL"
        assert whale_tier(None) == "UNKNOWN"
        assert whale_tier(0) == "UNKNOWN"

    def test_normalize(self):
        assert normalize_symbol("BTCUSDT") == "BTC-USD"
        assert normalize_symbol("eth") == "ETH-USD"
        assert normalize_symbol("1000PEPEUSDT") == "1000PEPE-USD"
        assert normalize_symbol("SOL-USD") == "SOL-USD"

    def test_features_are_a_vector_not_a_score(self):
        f = whale_features(2e6, OPENED, "high", NOW - 30, NOW)
        assert f["tier"] == "LARGE_WHALE"
        assert f["event_kind"] == OPENED
        assert f["opened_at_confidence"] == "high"
        assert f["position_age_s"] == pytest.approx(30.0)
        assert f["historical_edge"] is None      # learned — shadow journal
        assert f["market_impact_ic"] is None
        assert "score" not in f and "confidence" not in f


# ── Delta engine ──────────────────────────────────────────────────────────

def _plane(now=NOW):
    return WhalePositionPlane([], log_dir="/tmp/nonexistent-wpp-test",
                              time_fn=lambda: now)


def _pos(notional, side="long", size=10.0, symbol="BTC-USD",
         venue="aster", address="0xW"):
    return {"venue": venue, "address": address, "symbol": symbol,
            "side": side, "size": size, "notional_usd": notional,
            "margin_used_usd": None, "leverage": None,
            "entry_price": 100.0, "mark_price": 100.0,
            "liquidation_price": None, "unrealized_pnl": 0.0,
            "account_value_usd": 2e6, "source": "test", "confidence": 1.0,
            "updated_at": NOW}


class TestDeltaEngine:
    def test_first_sighting_silent_aged_bag(self):
        pl = _plane()
        assert pl._diff_one(_pos(5e6)) is None     # Hasbrouck: aged bag

    def test_observed_open_high_confidence(self):
        pl = _plane()
        key = ("aster", "0xW", "BTC-USD")
        pl._prev[key] = {**_pos(0.0), "size": 0.0}
        pl._first_seen[key] = NOW - 100
        ev = pl._diff_one(_pos(2e6))
        assert ev["kind"] == OPENED and ev["direction"] == "long"
        assert ev["opened_at_confidence"] == "high"
        assert ev["quality"] == "direct"
        assert ev["tier"] == "LARGE_WHALE"
        assert ev["features"]["event_kind"] == OPENED

    def test_add_below_floor_silent(self):
        pl = _plane()
        assert pl._diff_one(_pos(1_000_000)) is None      # baseline
        ev = pl._diff_one(_pos(1_000_000 + 5_000))        # +$5k < $10k floor
        assert ev is None

    def test_add_above_floor_emits(self):
        pl = _plane()
        pl._diff_one(_pos(1_000_000))
        ev = pl._diff_one(_pos(1_000_000 + 50_000))
        assert ev["kind"] == ADDED
        assert ev["notional_delta_usd"] == pytest.approx(50_000)

    def test_trim_below_floor_silent(self):
        pl = _plane()
        pl._diff_one(_pos(1_000_000))
        assert pl._diff_one(_pos(1_000_000 - 9_999)) is None

    def test_flip_always_emits(self):
        pl = _plane()
        pl._diff_one(_pos(100_000, side="long"))
        ev = pl._diff_one(_pos(50_000, side="short"))
        assert ev["kind"] == FLIPPED and ev["direction"] == "short"
        assert ev["opened_at_confidence"] == "high"

    def test_close_via_zero_row(self):
        pl = _plane()
        pl._diff_one(_pos(500_000))
        ev = pl._diff_one(_pos(0.0))
        assert ev["kind"] == CLOSED and ev["direction"] == "long"

    def test_vanish_check_emits_close(self):
        pl = _plane()
        pl._diff_one(_pos(500_000, symbol="ETH-USD"))
        out = pl._vanish_check("aster", "0xW", seen=set())
        assert len(out) == 1
        ev = out[0]
        assert ev["kind"] == CLOSED and ev["symbol"] == "ETH-USD"
        assert ev["notional_usd"] == 0.0
        assert ev["notional_delta_usd"] == pytest.approx(-500_000)
        # prev zeroed → a second vanish check does not re-emit
        assert pl._vanish_check("aster", "0xW", seen=set()) == []

    def test_vanish_check_scoped_to_address_and_venue(self):
        pl = _plane()
        pl._diff_one(_pos(500_000, address="0xA"))
        pl._diff_one(_pos(500_000, address="0xB"))
        out = pl._vanish_check("aster", "0xA", seen=set())
        assert len(out) == 1 and out[0]["address"] == "0xA"

    def test_event_contract_matches_whale_mirror(self):
        pl = _plane()
        pl._diff_one(_pos(1_000_000))
        ev = pl._diff_one(_pos(1_000_000 + 100_000))
        for k in ("venue", "address", "symbol", "direction", "kind",
                  "size", "prev_size", "quality", "ts"):
            assert k in ev

    @pytest.mark.asyncio
    async def test_poll_all_never_raises(self, monkeypatch):
        pl = WhalePositionPlane([{"address": "0xW", "label": "t"}],
                                log_dir="/tmp/nonexistent-wpp-test",
                                time_fn=lambda: NOW)

        async def _boom(addr):
            raise RuntimeError("network gone")

        monkeypatch.setattr(pl, "_fetch_aster", _boom)
        monkeypatch.setattr(pl, "_fetch_hl", _boom)
        assert await pl.poll_all() == []


class TestFetchNegativeAnswers:
    @pytest.mark.asyncio
    async def test_aster_account_not_exist_is_valid_negative(self):
        pl = _plane()

        class _Resp:
            status_code = 200

            def json(self):
                return {"error": {"code": -32603,
                                  "message": "account does not exist"}}

        class _Cli:
            async def post(self, *a, **kw):
                return _Resp()

        pl._client = _Cli()
        assert await pl._fetch_aster("0xUNKNOWN") == []
