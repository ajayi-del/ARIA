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


class TestPositionIdxMapping(unittest.IsolatedAsyncioTestCase):
    """Hedge mandate P1 — positionIdx: 0 one-way (legacy bit-for-bit),
    1 = long position side, 2 = short position side when hedge mode on."""

    def _capture_client(self, hedge=False):
        client = BybitClient(_config(bybit_hedge_mode=hedge))
        client._specs["ADA-USD"] = {"tick": 0.0001, "step": 0.1,
                                    "min_qty": 1, "min_notional": 5}
        captured = {}
        client._post = AsyncMock(
            side_effect=lambda path, body: captured.update(body) or {"orderId": "oid"})
        return client, captured

    async def test_oneway_mode_position_idx_zero(self):
        # No bybit_hedge_mode in config at all → legacy 0 payload, bit-for-bit.
        client, captured = self._capture_client(hedge=False)
        self.assertFalse(client.hedge_mode)
        await client.place_order({"symbol": "ADA-USD", "side": "long",
                                  "qty": 10.0, "order_type": "Market"})
        self.assertEqual(captured["positionIdx"], 0)
        await client.place_order({"symbol": "ADA-USD", "side": "short",
                                  "qty": 10.0, "order_type": "Market",
                                  "reduce_only": True})
        self.assertEqual(captured["positionIdx"], 0)

    async def test_hedge_mode_entry_idx(self):
        client, captured = self._capture_client(hedge=True)
        await client.place_order({"symbol": "ADA-USD", "side": "long",
                                  "qty": 10.0, "order_type": "Market"})
        self.assertEqual(captured["positionIdx"], 1)
        await client.place_order({"symbol": "ADA-USD", "side": "short",
                                  "qty": 10.0, "order_type": "Market"})
        self.assertEqual(captured["positionIdx"], 2)

    async def test_hedge_mode_close_uses_position_side(self):
        # close_position_market long → Sell reduce-only on positionIdx 1;
        # close short → Buy reduce-only on positionIdx 2.
        client, captured = self._capture_client(hedge=True)
        await client.close_position_market(symbol="ADA-USD", side="long", size=10.0)
        self.assertEqual(captured["side"], "Sell")
        self.assertTrue(captured["reduceOnly"])
        self.assertEqual(captured["positionIdx"], 1)
        await client.close_position_market(symbol="ADA-USD", side="short", size=10.0)
        self.assertEqual(captured["side"], "Buy")
        self.assertEqual(captured["positionIdx"], 2)

    async def test_hedge_mode_tp_reduce_only_idx(self):
        # TP leg on a LONG position: close_side Sell + reduce_only → idx 1.
        client, captured = self._capture_client(hedge=True)
        await client.place_order({"symbol": "ADA-USD", "side": "short",
                                  "qty": 10.0, "order_type": "Limit",
                                  "price": 0.55, "reduce_only": True})
        self.assertEqual(captured["positionIdx"], 1)

    async def test_oneway_position_stop_idx_zero(self):
        client, captured = self._capture_client(hedge=False)
        stop_id = await client._set_position_stop("ADA-USD", 0.45, side="long")
        self.assertEqual(stop_id, "posstop-ADA-USD")
        self.assertEqual(captured["positionIdx"], 0)

    async def test_hedge_position_stop_idx(self):
        client, captured = self._capture_client(hedge=True)
        await client._set_position_stop("ADA-USD", 0.45, side="long")
        self.assertEqual(captured["positionIdx"], 1)
        await client._set_position_stop("ADA-USD", 0.55, side="short")
        self.assertEqual(captured["positionIdx"], 2)

    async def test_place_bracket_stop_carries_side(self):
        # place_bracket → _set_position_stop with the candidate's side.
        client = BybitClient(_config(bybit_hedge_mode=True))
        client._specs["ADA-USD"] = {"tick": 0.0001, "step": 0.1,
                                    "min_qty": 1, "min_notional": 5}
        client._equity_cache = (50.0, 1e12)
        client.get_positions = AsyncMock(return_value=[])
        client.place_order = AsyncMock(
            return_value=SimpleNamespace(success=True, order_id="entry1", error=None))
        client._confirm_position_open = AsyncMock(return_value=True)
        client._set_position_stop = AsyncMock(return_value="posstop-ADA-USD")
        client._place_tp_orders = AsyncMock(return_value=["tp1"])
        res = await client.place_bracket(
            BracketOrder(candidate=_candidate(side="short"), account_id="0", symbol_id=0))
        self.assertTrue(res.success)
        kwargs = client._set_position_stop.call_args[1]
        self.assertEqual(kwargs["side"], "short")


class TestTrailingStop(unittest.IsolatedAsyncioTestCase):
    def _capture_client(self):
        client = BybitClient(_config())
        client._specs["ADA-USD"] = {"tick": 0.0001, "step": 0.1,
                                    "min_qty": 1, "min_notional": 5}
        captured = {}

        async def _post(path, body):
            captured["path"] = path
            captured.update(body)
            return {}
        client._post = AsyncMock(side_effect=_post)
        return client, captured

    async def test_set_trailing_stop_payload(self):
        client, captured = self._capture_client()
        ok = await client.set_trailing_stop("ADA-USD", 1, 0.0256,
                                            active_price=0.51234)
        self.assertTrue(ok)
        self.assertEqual(captured["path"], "/v5/position/trading-stop")
        self.assertEqual(captured["category"], "linear")
        self.assertEqual(captured["symbol"], "ADAUSDT")
        self.assertEqual(captured["trailingStop"], "0.0256")  # absolute distance
        self.assertEqual(captured["activePrice"], "0.5123")   # tick-rounded
        self.assertEqual(captured["tpslMode"], "Full")
        self.assertEqual(captured["positionIdx"], 1)

    async def test_set_trailing_stop_no_active_price(self):
        client, captured = self._capture_client()
        ok = await client.set_trailing_stop("ADA-USD", 2, 0.03)
        self.assertTrue(ok)
        self.assertEqual(captured["positionIdx"], 2)
        self.assertNotIn("activePrice", captured)

    async def test_clear_trailing_stop_payload(self):
        client, captured = self._capture_client()
        ok = await client.clear_trailing_stop("ADA-USD", 1)
        self.assertTrue(ok)
        self.assertEqual(captured["path"], "/v5/position/trading-stop")
        self.assertEqual(captured["trailingStop"], "0")
        self.assertEqual(captured["positionIdx"], 1)

    async def test_trailing_stop_api_error_returns_false(self):
        client, _ = self._capture_client()
        client._post = AsyncMock(side_effect=BybitAPIError("bad", ret_code=10001))
        self.assertFalse(await client.set_trailing_stop("ADA-USD", 1, 0.03))
        self.assertFalse(await client.clear_trailing_stop("ADA-USD", 1))


class TestAmendOrder(unittest.IsolatedAsyncioTestCase):
    def _capture_client(self):
        client = BybitClient(_config())
        client._specs["ADA-USD"] = {"tick": 0.0001, "step": 0.1,
                                    "min_qty": 1, "min_notional": 5}
        captured = {}

        async def _post(path, body):
            captured["path"] = path
            captured.update(body)
            return {}
        client._post = AsyncMock(side_effect=_post)
        return client, captured

    async def test_amend_price_only(self):
        client, captured = self._capture_client()
        ok = await client.amend_order("ADA-USD", "oid9", new_price=0.51234)
        self.assertTrue(ok)
        self.assertEqual(captured["path"], "/v5/order/amend")
        self.assertEqual(captured["category"], "linear")
        self.assertEqual(captured["symbol"], "ADAUSDT")
        self.assertEqual(captured["orderId"], "oid9")
        self.assertEqual(captured["price"], "0.5123")  # tick-rounded
        self.assertNotIn("qty", captured)

    async def test_amend_qty_only(self):
        client, captured = self._capture_client()
        ok = await client.amend_order("ADA-USD", "oid9", new_qty=25.05)
        self.assertTrue(ok)
        self.assertEqual(captured["qty"], "25.1")  # step-rounded (half-up)
        self.assertNotIn("price", captured)

    async def test_amend_both(self):
        client, captured = self._capture_client()
        ok = await client.amend_order("ADA-USD", "oid9",
                                      new_price=0.51234, new_qty=25.05)
        self.assertTrue(ok)
        self.assertEqual(captured["price"], "0.5123")
        self.assertEqual(captured["qty"], "25.1")

    async def test_amend_neither_is_noop(self):
        client, _ = self._capture_client()
        self.assertFalse(await client.amend_order("ADA-USD", "oid9"))
        client._post.assert_not_awaited()

    async def test_amend_api_error_returns_false(self):
        client, _ = self._capture_client()
        client._post = AsyncMock(side_effect=BybitAPIError("gone", ret_code=110001))
        self.assertFalse(await client.amend_order("ADA-USD", "oid9", new_price=0.5))


class TestModeDetection(unittest.IsolatedAsyncioTestCase):
    async def test_hedge_account_detected(self):
        client = BybitClient(_config(bybit_hedge_mode=True))
        client._get = AsyncMock(return_value={"list": [
            {"symbol": "ADAUSDT", "positionIdx": 1},
            {"symbol": "HYPEUSDT", "positionIdx": 2},
        ]})
        with patch("execution.bybit_client.logger") as log:
            mode = await client.detect_position_mode()
        self.assertTrue(mode)
        log.info.assert_called_once()
        event, kwargs = log.info.call_args[0][0], log.info.call_args[1]
        self.assertEqual(event, "bybit_position_mode")
        self.assertEqual(kwargs["mode"], "hedge")
        self.assertTrue(kwargs["account_hedge_mode"])
        log.warning.assert_not_called()  # config == account → no warn

    async def test_oneway_account_detected(self):
        client = BybitClient(_config())
        client._get = AsyncMock(return_value={"list": [
            {"symbol": "ADAUSDT", "positionIdx": 0},
        ]})
        with patch("execution.bybit_client.logger") as log:
            mode = await client.detect_position_mode()
        self.assertFalse(mode)
        self.assertEqual(log.info.call_args[1]["mode"], "oneway")
        log.warning.assert_not_called()

    async def test_mismatch_warns_loud(self):
        # Config hedge, account one-way (empty book reads one-way) → warn.
        client = BybitClient(_config(bybit_hedge_mode=True))
        client._get = AsyncMock(return_value={"list": []})
        with patch("execution.bybit_client.logger") as log:
            mode = await client.detect_position_mode()
        self.assertTrue(mode)  # config is the contract; never auto-flipped
        warn_events = [c[0][0] for c in log.warning.call_args_list]
        self.assertIn("bybit_position_mode_mismatch", warn_events)

    async def test_mismatch_reverse_warns_loud(self):
        # Config one-way, account hedge → warn.
        client = BybitClient(_config())
        client._get = AsyncMock(return_value={"list": [
            {"symbol": "ADAUSDT", "positionIdx": 2},
        ]})
        with patch("execution.bybit_client.logger") as log:
            await client.detect_position_mode()
        warn_events = [c[0][0] for c in log.warning.call_args_list]
        self.assertIn("bybit_position_mode_mismatch", warn_events)

    async def test_read_failure_keeps_config(self):
        client = BybitClient(_config(bybit_hedge_mode=True))
        client._get = AsyncMock(side_effect=BybitAPIError("auth", ret_code=10003))
        with patch("execution.bybit_client.logger") as log:
            mode = await client.detect_position_mode()
        self.assertTrue(mode)
        warn_events = [c[0][0] for c in log.warning.call_args_list]
        self.assertIn("bybit_position_mode_read_failed", warn_events)


class TestHedgeCredentialOverride(unittest.TestCase):
    """P5 — hedge sub-account: explicit ctor creds win; absent = legacy config."""

    def test_explicit_creds_override_config(self):
        client = BybitClient(_config(), api_key="hedgekey", api_secret="hedgesecret")
        self.assertEqual(client.api_key, "hedgekey")
        self.assertEqual(client.api_secret, "hedgesecret")

    def test_no_override_reads_config_bit_for_bit(self):
        client = BybitClient(_config())
        self.assertEqual(client.api_key, "testkey")
        self.assertEqual(client.api_secret, "testsecret")

    def test_config_defaults(self):
        from core.config import Settings
        fields = Settings.model_fields
        self.assertEqual(fields["bybit_hedge_api_key"].default, "")
        self.assertEqual(fields["bybit_hedge_api_secret"].default, "")
        self.assertEqual(fields["hedge_account_low_margin_usd"].default, 30.0)


if __name__ == "__main__":
    unittest.main()
