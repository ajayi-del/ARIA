"""Unit tests for execution/bybit_client.py — no network, all REST mocked."""
import hashlib
import hmac
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from execution.bybit_client import (
    BybitClient, BybitAPIError, _round_step,
    to_bybit_symbol, to_canonical_symbol,
)
from execution.schemas import BracketOrder, TradeCandidate


def _config(**over):
    base = dict(
        bybit_api_key="testkey",
        bybit_api_secret="testsecret",
        bybit_margin_pct=0.10,
        bybit_leverage=5,
        bybit_max_leverage=10,
        bybit_max_positions=2,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _candidate(**over):
    base = dict(
        symbol="ADA-USD", side="long", size=100.0, entry_price=0.50,
        stop_price=0.45, tp1_price=0.55, tp2_price=0.60, tp3_price=0.65,
        partial1_pct=0.33, partial2_pct=0.33, partial3_pct=0.34,
        order_type="market", leverage=5,
    )
    base.update(over)
    return SimpleNamespace(**base)


class TestSymbolMapping(unittest.TestCase):
    def test_roundtrip(self):
        self.assertEqual(to_bybit_symbol("ADA-USD"), "ADAUSDT")
        self.assertEqual(to_bybit_symbol("1000BONK-USD"), "1000BONKUSDT")
        self.assertEqual(to_canonical_symbol("ADAUSDT"), "ADA-USD")
        self.assertEqual(to_canonical_symbol("1000BONKUSDT"), "1000BONK-USD")
        for sym in ("HYPE-USD", "TAO-USD", "WIF-USD", "ZEC-USD"):
            self.assertEqual(to_canonical_symbol(to_bybit_symbol(sym)), sym)


class TestRoundStep(unittest.TestCase):
    def test_half_up(self):
        self.assertAlmostEqual(_round_step(0.1234, 0.01), 0.12)
        self.assertAlmostEqual(_round_step(0.125, 0.01), 0.13)

    def test_floor(self):
        self.assertAlmostEqual(_round_step(0.129, 0.01, floor=True), 0.12)

    def test_zero_step_passthrough(self):
        self.assertEqual(_round_step(1.234, 0.0), 1.234)


class TestAuthSignature(unittest.TestCase):
    def test_hmac_construction(self):
        client = BybitClient(_config())
        payload = json.dumps({"a": 1}, separators=(",", ":"))
        with patch("execution.bybit_client.time.time", return_value=1700000000.0):
            headers = client._auth_headers(payload)
        ts = "1700000000000"
        expected = hmac.new(
            b"testsecret",
            (ts + "testkey" + "5000" + payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(headers["X-BAPI-SIGN"], expected)
        self.assertEqual(headers["X-BAPI-API-KEY"], "testkey")
        self.assertEqual(headers["X-BAPI-TIMESTAMP"], ts)
        self.assertEqual(headers["X-BAPI-RECV-WINDOW"], "5000")


class TestSpecSync(unittest.IsolatedAsyncioTestCase):
    async def test_instruments_info_parsing(self):
        client = BybitClient(_config())
        client._get = AsyncMock(return_value={"list": [{
            "symbol": "ADAUSDT",
            "lotSizeFilter": {"qtyStep": "0.1", "minOrderQty": "1",
                              "minNotionalValue": "5"},
            "priceFilter": {"tickSize": "0.0001"},
        }]})
        synced = await client.sync_symbol_specs(["ADA-USD"])
        self.assertEqual(synced, 1)
        spec = client.get_spec("ADA-USD")
        self.assertEqual(spec["tick"], 0.0001)
        self.assertEqual(spec["step"], 0.1)
        self.assertEqual(spec["min_qty"], 1.0)
        self.assertEqual(spec["min_notional"], 5.0)

    async def test_missing_symbol_keeps_default(self):
        client = BybitClient(_config())
        client._get = AsyncMock(return_value={"list": []})
        synced = await client.sync_symbol_specs(["GHOST-USD"])
        self.assertEqual(synced, 0)
        self.assertEqual(client.get_spec("GHOST-USD")["min_notional"], 5.0)


class TestOrderBody(unittest.IsolatedAsyncioTestCase):
    async def test_market_order_shape(self):
        client = BybitClient(_config())
        client._specs["ADA-USD"] = {"tick": 0.0001, "step": 0.1,
                                    "min_qty": 1, "min_notional": 5}
        captured = {}
        client._post = AsyncMock(side_effect=lambda path, body: captured.update(body) or {"orderId": "oid1"})
        res = await client.place_order({"symbol": "ADA-USD", "side": "long",
                                        "qty": 25.0, "order_type": "Market"})
        self.assertTrue(res.success)
        self.assertEqual(captured["category"], "linear")
        self.assertEqual(captured["symbol"], "ADAUSDT")
        self.assertEqual(captured["side"], "Buy")
        self.assertEqual(captured["positionIdx"], 0)
        self.assertNotIn("price", captured)
        self.assertNotIn("reduceOnly", captured)

    async def test_limit_reduce_only_shape(self):
        client = BybitClient(_config())
        client._specs["ADA-USD"] = {"tick": 0.0001, "step": 0.1,
                                    "min_qty": 1, "min_notional": 5}
        captured = {}
        client._post = AsyncMock(side_effect=lambda path, body: captured.update(body) or {"orderId": "oid2"})
        await client.place_order({"symbol": "ADA-USD", "side": "short",
                                  "qty": 25.05, "order_type": "Limit",
                                  "price": 0.51234, "reduce_only": True,
                                  "link_id": "x" * 40})
        self.assertEqual(captured["side"], "Sell")
        self.assertEqual(captured["qty"], "25")        # floored to step (reduce-only)
        self.assertEqual(captured["price"], "0.5123")  # tick-rounded
        self.assertTrue(captured["reduceOnly"])
        self.assertEqual(len(captured["orderLinkId"]), 36)

    async def test_api_error_maps_to_rejected(self):
        client = BybitClient(_config())
        client._specs["ADA-USD"] = {"tick": 0, "step": 0,
                                    "min_qty": 0, "min_notional": 5}
        client._post = AsyncMock(side_effect=BybitAPIError("insufficient balance", ret_code=110007))
        res = await client.place_order({"symbol": "ADA-USD", "side": "long",
                                        "qty": 1.0, "order_type": "Market"})
        self.assertFalse(res.success)
        self.assertIn("insufficient balance", res.error)


class TestPlaceBracket(unittest.IsolatedAsyncioTestCase):
    def _armed_client(self, equity=50.0, open_positions=None):
        client = BybitClient(_config())
        client._specs["ADA-USD"] = {"tick": 0.0001, "step": 0.1,
                                    "min_qty": 1, "min_notional": 5}
        client._equity_cache = (equity, 1e12)  # fresh cache
        client.get_positions = AsyncMock(return_value=open_positions or [])
        client.place_order = AsyncMock(
            return_value=SimpleNamespace(success=True, order_id="entry1", error=None))
        client._confirm_position_open = AsyncMock(return_value=True)
        client._set_position_stop = AsyncMock(return_value="posstop-ADA-USD")
        client._place_tp_orders = AsyncMock(return_value=["tp1", "tp2", "tp3"])
        return client

    async def test_position_cap_rejects(self):
        client = self._armed_client(open_positions=[{"symbol": "X"}, {"symbol": "Y"}])
        res = await client.place_bracket(BracketOrder(candidate=_candidate(), account_id="0", symbol_id=0))
        self.assertFalse(res.success)
        self.assertIn("bybit_position_cap", res.error)

    async def test_min_notional_rejects(self):
        client = self._armed_client(equity=8.0)  # 8 * 0.10 * 5 = $4 < $5 min
        res = await client.place_bracket(BracketOrder(candidate=_candidate(), account_id="0", symbol_id=0))
        self.assertFalse(res.success)
        self.assertIn("below_bybit_min", res.error)

    async def test_pct_of_equity_sizing(self):
        # $50 equity * 10% margin * 5x = $25 notional → 50 ADA @ $0.50.
        # Candidate's SoDEX-derived size (100 ADA) must be overridden.
        client = self._armed_client(equity=50.0)
        res = await client.place_bracket(BracketOrder(candidate=_candidate(), account_id="0", symbol_id=0))
        self.assertTrue(res.success)
        entry_call = client.place_order.call_args_list[0][0][0]
        self.assertAlmostEqual(entry_call["qty"], 50.0)
        client._place_tp_orders.assert_awaited_once()
        tp_size = client._place_tp_orders.call_args[0][2]
        self.assertAlmostEqual(tp_size, 50.0)
        self.assertEqual(res.stop_order_id, "posstop-ADA-USD")
        self.assertEqual(res.tp1_order_id, "tp1")

    async def test_equity_unavailable_fails_closed(self):
        client = self._armed_client(equity=0.0)
        client.get_account_balance = AsyncMock(return_value=0.0)
        res = await client.place_bracket(BracketOrder(candidate=_candidate(), account_id="0", symbol_id=0))
        self.assertFalse(res.success)
        self.assertIn("bybit_equity_unavailable", res.error)

    async def test_sleeve_halt_at_30pct_drawdown(self):
        # Session started at $100, equity now $65 → -35% > 30% halt → reject.
        client = self._armed_client(equity=65.0)
        client._session_start_equity = 100.0
        res = await client.place_bracket(BracketOrder(candidate=_candidate(), account_id="0", symbol_id=0))
        self.assertFalse(res.success)
        self.assertIn("bybit_sleeve_halt", res.error)

    async def test_sleeve_halt_not_triggered_above_threshold(self):
        client = self._armed_client(equity=75.0)  # -25% < 30% → trades
        client._session_start_equity = 100.0
        res = await client.place_bracket(BracketOrder(candidate=_candidate(), account_id="0", symbol_id=0))
        self.assertTrue(res.success)


class TestLeverageClamp(unittest.IsolatedAsyncioTestCase):
    async def test_clamped_to_venue_max(self):
        client = BybitClient(_config(bybit_max_leverage=10))
        calls = []
        client.update_leverage = AsyncMock(
            side_effect=lambda sym, lev: calls.append(lev) or True)
        actual = await client.update_leverage_with_fallback(
            symbol="HYPE-USD", leverage=20)
        self.assertEqual(actual, 10)
        self.assertEqual(calls, [10])

    async def test_fallback_chain(self):
        client = BybitClient(_config())
        client.update_leverage = AsyncMock(side_effect=[False, False, True])
        actual = await client.update_leverage_with_fallback(
            symbol="ADA-USD", leverage=5)
        self.assertEqual(actual, 2)  # chain 5→fail, 3→fail, 2→ok (10,7 skipped: >target)


class TestPositionNormalization(unittest.IsolatedAsyncioTestCase):
    async def test_sodex_shape(self):
        client = BybitClient(_config())
        client._get = AsyncMock(return_value={"list": [
            {"symbol": "HYPEUSDT", "side": "Buy", "size": "2.5",
             "avgPrice": "40.5", "markPrice": "41.0",
             "unrealisedPnl": "1.25", "leverage": "5", "liqPrice": "30.1"},
            {"symbol": "WIFUSDT", "side": "Sell", "size": "0",
             "avgPrice": "0", "markPrice": "0", "unrealisedPnl": "0",
             "leverage": "1", "liqPrice": "0"},
        ]})
        positions = await client.get_positions()
        self.assertEqual(len(positions), 1)  # zero-size filtered
        p = positions[0]
        self.assertEqual(p["symbol"], "HYPE-USD")
        self.assertEqual(p["coin"], "HYPE-USD")
        self.assertEqual(p["side"], "long")
        self.assertEqual(p["size"], 2.5)
        self.assertEqual(p["qty"], 2.5)
        self.assertEqual(p["entry"], 40.5)
        self.assertEqual(p["avgPrice"], 40.5)
        self.assertEqual(p["venue"], "bybit")


if __name__ == "__main__":
    unittest.main()
