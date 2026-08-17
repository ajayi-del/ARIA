"""Unit tests for execution/aster_client.py + data/aster_feed.py — no network."""
import asyncio
import time
import unittest
import urllib.parse
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from eth_account import Account

from execution.aster_client import (AsterClient, AsterAPIError, _encode_712,
                                    _round_step, to_aster_symbol,
                                    to_canonical_symbol)
from data.aster_feed import AsterFeed

# Demo API wallet from the Aster V3 docs (asterdex/api-docs) — valid key
# material so signing paths run in tests.
_DOCS_SIGNER = "0x21cF8Ae13Bb72632562c6Fff438652Ba1a151bb0"
_DOCS_PRIVKEY = ("0x4fd0a42218f3eae43a6ce26d22544e986139a01e5b34a62db5"
                 "3757ffca81bae1")


def _client(**cfg):
    defaults = dict(aster_api_key=_DOCS_SIGNER, aster_api_secret=_DOCS_PRIVKEY,
                    aster_sleeve_halt_dd_pct=0.30, aster_margin_pct=0.10,
                    aster_max_leverage=10, aster_max_positions=5)
    defaults.update(cfg)
    return AsterClient(SimpleNamespace(**defaults))


class TestSignature(unittest.TestCase):
    def test_docs_vector(self):
        # Exact demo wallet from the Aster V3 docs — if this passes, our
        # EIP-712 construction matches the exchange's expected scheme.
        c = _client()
        self.assertEqual(Account.from_key(c.api_secret).address, _DOCS_SIGNER)
        msg = ("symbol=ASTERUSDT&type=LIMIT&side=BUY&timeInForce=GTC"
               "&quantity=20&price=0.5&nonce=1748310859508867"
               "&signer=0x21cF8Ae13Bb72632562c6Fff438652Ba1a151bb0")
        sig = c._sign(msg)
        self.assertEqual(
            sig.lower().removeprefix("0x"),
            "d82a6784f5eff00b95ecb88145cf7a5fa6803ea6f67e0217262073da628750a74"
            "2bb3ba555af418320e55029afe358957808d4f810d73ea663d9c91852cd6d941c")

    def test_signed_params_adds_nonce_signer_signature(self):
        c = _client()
        p = c._signed_params({"symbol": "BTCUSDT"})
        self.assertIn("nonce", p)
        self.assertEqual(p["signer"], _DOCS_SIGNER)
        self.assertIn("signature", p)
        self.assertNotIn("timestamp", p)       # V1 relic — V3 signs nonce only
        # signature is computed over the urlencoded params WITHOUT itself
        msg = urllib.parse.urlencode(
            {k: v for k, v in p.items() if k != "signature"})
        self.assertEqual(
            Account.recover_message(_encode_712(msg), signature=p["signature"]),
            _DOCS_SIGNER)

    def test_nonces_strictly_increase(self):
        c = _client()
        n1 = int(c._signed_params({})["nonce"])
        n2 = int(c._signed_params({})["nonce"])
        self.assertGreater(n2, n1)


class TestSymbolMap(unittest.TestCase):
    def test_roundtrip(self):
        self.assertEqual(to_aster_symbol("BTC-USD"), "BTCUSDT")
        self.assertEqual(to_aster_symbol("1000BONK-USD"), "1000BONKUSDT")
        self.assertEqual(to_canonical_symbol("BTCUSDT"), "BTC-USD")
        self.assertEqual(to_canonical_symbol("1000BONKUSDT"), "1000BONK-USD")

    def test_xaut_override(self):
        # ARIA's XAUT-USD is XAUUSDT on Aster (2026-08-16 XAUT/CL migration)
        self.assertEqual(to_aster_symbol("XAUT-USD"), "XAUUSDT")
        self.assertEqual(to_canonical_symbol("XAUUSDT"), "XAUT-USD")
        self.assertEqual(to_aster_symbol("CL-USD"), "CLUSDT")
        self.assertEqual(to_canonical_symbol("CLUSDT"), "CL-USD")

    def test_round_step(self):
        self.assertAlmostEqual(_round_step(0.12345, 0.001), 0.123)
        self.assertAlmostEqual(_round_step(0.12345, 0.001, floor=True), 0.123)
        self.assertAlmostEqual(_round_step(0.1239, 0.001), 0.124)
        self.assertEqual(_round_step(1.5, 0.0), 1.5)   # step 0 → untouched


class TestOrderParams(unittest.TestCase):
    def test_oneway_mode(self):
        c = _client()
        c.hedge_mode = False
        c._specs["BTC-USD"] = {"tick": 0.1, "step": 0.001,
                               "min_qty": 0.001, "min_notional": 1.0}
        p = c._order_params("BTC-USD", "long", 0.1234, "LIMIT",
                            price=63123.46, time_in_force="PostOnly")
        self.assertEqual(p["side"], "BUY")
        self.assertEqual(p["positionSide"], "BOTH")
        self.assertEqual(p["timeInForce"], "GTX")        # post-only → GTX
        self.assertEqual(p["quantity"], "0.123")
        self.assertEqual(p["price"], "63123.5")          # rounded to 0.1 tick
        self.assertNotIn("reduceOnly", p)

    def test_oneway_close_has_reduce_only(self):
        c = _client()
        c.hedge_mode = False
        p = c._order_params("BTC-USD", "short", 0.5, "MARKET", reduce_only=True)
        self.assertEqual(p["side"], "SELL")
        self.assertEqual(p["positionSide"], "BOTH")
        self.assertEqual(p["reduceOnly"], "true")

    def test_hedge_mode_entry_and_close(self):
        c = _client()
        c.hedge_mode = True
        entry = c._order_params("BTC-USD", "long", 0.5, "MARKET")
        self.assertEqual(entry["positionSide"], "LONG")
        self.assertNotIn("reduceOnly", entry)            # invalid in hedge mode
        close = c._order_params("BTC-USD", "short", 0.5, "MARKET", reduce_only=True)
        self.assertEqual(close["positionSide"], "LONG")  # closing a LONG position
        self.assertNotIn("reduceOnly", close)


class TestSleeveHalt(unittest.TestCase):
    def test_halt_at_30pct_drawdown(self):
        c = _client()
        c._session_start_equity = 100.0
        self.assertFalse(c._sleeve_halted(71.0))
        self.assertTrue(c._sleeve_halted(69.9))
        self.assertFalse(c._sleeve_halted(0.0))          # unread equity ≠ halt

    def test_no_baseline_no_halt(self):
        c = _client()
        self.assertFalse(c._sleeve_halted(50.0))


class TestApiShapes(unittest.IsolatedAsyncioTestCase):
    async def test_spec_sync_parses_filters(self):
        c = _client()
        c._request = AsyncMock(return_value={"symbols": [{
            "symbol": "BTCUSDT", "status": "TRADING",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "1"},
            ]}]})
        n = await c.sync_symbol_specs(["BTC-USD"])
        self.assertEqual(n, 1)
        spec = c.get_spec("BTC-USD")
        self.assertEqual(spec["tick"], 0.10)
        self.assertEqual(spec["min_notional"], 1.0)      # the $1 hook

    async def test_positions_normalized(self):
        c = _client()
        c._request = AsyncMock(return_value=[
            {"symbol": "BTCUSDT", "positionAmt": "0.002", "entryPrice": "63000.0",
             "markPrice": "63100.0", "unRealizedProfit": "0.20",
             "leverage": "5", "liquidationPrice": "50000.0"},
            {"symbol": "ETHUSDT", "positionAmt": "0", "entryPrice": "0"},
        ])
        pos = await c.get_positions()
        self.assertEqual(len(pos), 1)
        p = pos[0]
        self.assertEqual(p["symbol"], "BTC-USD")
        self.assertEqual(p["side"], "long")
        self.assertEqual(p["size"], 0.002)
        self.assertEqual(p["venue"], "aster")
        self.assertEqual(p["liqPrice"], 50000.0)

    async def test_balance_is_wallet_plus_upnl(self):
        c = _client()
        c._request = AsyncMock(return_value={
            "totalWalletBalance": "480.5", "totalUnrealizedProfit": "-3.2"})
        self.assertAlmostEqual(await c.get_account_balance(), 477.3)

    async def test_503_raises_unknown_not_failure(self):
        # Docs: 503 = execution status UNKNOWN — must reconcile, never retry blind.
        c = _client()
        resp = SimpleNamespace(status_code=503,
                               json=lambda: {"code": 503, "msg": "timeout"})
        c._http.request = AsyncMock(return_value=resp)
        with self.assertRaises(AsterAPIError) as ctx:
            await c._request("POST", "/fapi/v3/order", {})
        self.assertIn("UNKNOWN", str(ctx.exception))

    async def test_hedge_stop_uses_quantity_not_close_position(self):
        c = _client()
        c.hedge_mode = True
        c._specs["BTC-USD"] = {"tick": 0.1, "step": 0.001,
                               "min_qty": 0.001, "min_notional": 1.0}
        c._request = AsyncMock(return_value={"orderId": 42})
        cand = SimpleNamespace(side="long", stop_price=61000.06)
        oid = await c._set_position_stop("BTC-USD", cand, 0.0025)
        self.assertEqual(oid, "42")
        params = c._request.call_args[0][2]
        self.assertEqual(params["type"], "STOP_MARKET")
        self.assertEqual(params["workingType"], "MARK_PRICE")
        self.assertEqual(params["positionSide"], "LONG")
        self.assertEqual(params["quantity"], "0.002")
        self.assertEqual(params["stopPrice"], "61000.1")  # 0.1 tick round
        self.assertNotIn("closePosition", params)
        self.assertNotIn("reduceOnly", params)

    async def test_oneway_stop_uses_close_position(self):
        c = _client()
        c.hedge_mode = False
        c._specs["BTC-USD"] = {"tick": 0.1, "step": 0.001,
                               "min_qty": 0.001, "min_notional": 1.0}
        c._request = AsyncMock(return_value={"orderId": 7})
        cand = SimpleNamespace(side="short", stop_price=65000.0)
        await c._set_position_stop("BTC-USD", cand, 0.001)
        params = c._request.call_args[0][2]
        self.assertEqual(params["closePosition"], "true")
        self.assertEqual(params["reduceOnly"], "true")
        self.assertEqual(params["positionSide"], "BOTH")
        self.assertNotIn("quantity", params)

    async def test_close_position_market_venue_contract(self):
        # 2026-08-17 storm regression: venue callers pass size= and read
        # .success — the old bool/qty-only shape gave qty 0.0 ("Quantity
        # less than zero" ×21k) and AttributeError'd past the breaker.
        c = _client()
        c._specs["ADA-USD"] = {"tick": 0.0001, "step": 0.1,
                               "min_qty": 0.1, "min_notional": 1.0}
        c._request = AsyncMock(return_value={"orderId": 9})
        r = await c.close_position_market(symbol="ADA-USD", side="short",
                                          size=587.2)
        self.assertTrue(r.success)                 # OrderResult, not bool
        params = c._request.call_args[0][2]
        self.assertEqual(params["side"], "BUY")    # closing a short
        self.assertEqual(params["quantity"], "587.2")
        self.assertEqual(params["reduceOnly"], "true")
        # qty= still honored for the explosive time-stop's direct call
        c._request = AsyncMock(return_value={"orderId": 10})
        r2 = await c.close_position_market(symbol="ADA-USD", side="long",
                                           qty=12.5)
        self.assertTrue(r2.success)
        self.assertEqual(c._request.call_args[0][2]["side"], "SELL")
        self.assertEqual(c._request.call_args[0][2]["quantity"], "12.5")


class TestFeed(unittest.TestCase):
    def test_force_order_maps_direction_and_symbol(self):
        f = AsterFeed(symbols=["BTC-USD"])
        got = []

        async def cb(symbol, direction, qty, price, ts):
            got.append((symbol, direction, qty, price, ts))

        f.add_liquidation_listener(cb)
        with patch("asyncio.create_task", lambda coro: asyncio.run(coro)):
            f._handle_force_order({"e": "forceOrder", "E": 1700000000000, "o": {
                "s": "BTCUSDT", "S": "SELL", "q": "0.014", "z": "0.014",
                "p": "9910", "ap": "9910", "T": 1700000000000}})
            f._handle_force_order({"e": "forceOrder", "E": 1700000001000, "o": {
                "s": "ETHUSDT", "S": "BUY", "q": "1.5", "z": "1.5",
                "p": "3200", "ap": "3201", "T": 1700000001000}})
        # SELL = long liquidated = bearish; BUY = short liquidated = bullish
        self.assertEqual(got[0][:3], ("BTC-USD", "bearish", 0.014))
        self.assertEqual(got[0][3], 9910.0)
        self.assertEqual(got[1][:2], ("ETH-USD", "bullish"))
        self.assertEqual(got[1][3], 3201.0)              # avg price preferred

    def test_mark_price_store(self):
        f = AsterFeed(symbols=["BTC-USD"])
        f._handle_mark_price({"e": "markPriceUpdate", "s": "BTCUSDT",
                              "p": "11794.15", "i": "11784.62",
                              "r": "0.00038167", "E": 1562305380000})
        self.assertEqual(f.get_mark_price("BTC-USD"), 11794.15)
        self.assertAlmostEqual(f.mark_prices["BTC-USD"]["funding_rate"], 0.00038167)
        self.assertEqual(f.get_mark_price("ETH-USD"), 0.0)   # untracked → 0

    def test_stream_url_includes_all_market_liq(self):
        f = AsterFeed(symbols=["BTC-USD", "ETH-USD"])
        url = f._stream_url()
        self.assertIn("!forceOrder@arr", url)
        self.assertIn("btcusdt@markPrice@1s", url)
        self.assertIn("ethusdt@markPrice@1s", url)


if __name__ == "__main__":
    unittest.main()
