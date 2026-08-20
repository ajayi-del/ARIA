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
        self.assertEqual(params["positionSide"], "BOTH")
        self.assertNotIn("quantity", params)
        # Exchange rejects reduceOnly alongside closePosition (live 2026-08-17)
        self.assertNotIn("reduceOnly", params)

    async def test_oneway_replace_stop_no_reduce_only(self):
        # Same exchange rule in replace_stop_order — the startup sync hit it
        # as aster_stop_replace_failed → startup_stop_exception (2026-08-17).
        c = _client()
        c.hedge_mode = False
        c._specs["UNI-USD"] = {"tick": 0.001, "step": 0.01,
                               "min_qty": 0.01, "min_notional": 1.0}
        c.get_open_orders = AsyncMock(return_value=[])
        c._request = AsyncMock(return_value={"orderId": 11})
        r = await c.replace_stop_order(symbol="UNI-USD", new_stop=3.3165,
                                       side="short")
        self.assertTrue(r.success)
        self.assertEqual(r.order_id, "11")
        params = c._request.call_args[0][2]
        self.assertEqual(params["side"], "BUY")     # stop for a short
        self.assertEqual(params["type"], "STOP_MARKET")
        self.assertEqual(params["closePosition"], "true")
        self.assertNotIn("reduceOnly", params)

    async def test_replace_stop_accepts_venue_kwargs(self):
        # Venue-boundary callers pass new_stop_price= (startup sync, trailing
        # loop) — swallowing it into **_ gave stop 0.0 "less than zero".
        c = _client()
        c.hedge_mode = False
        c._specs["UNI-USD"] = {"tick": 0.001, "step": 0.01,
                               "min_qty": 0.01, "min_notional": 1.0}
        c.get_open_orders = AsyncMock(return_value=[])
        c._request = AsyncMock(return_value={"orderId": 12})
        r = await c.replace_stop_order(symbol="UNI-USD", side="short",
                                       new_stop_price=3.3165, size=62.0,
                                       entry_price=3.2675, mark_price=3.28)
        self.assertTrue(r.success)
        self.assertEqual(c._request.call_args[0][2]["stopPrice"], "3.316")

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

    async def test_place_protective_orders_contract(self):
        c = _client()
        c._specs["BTC-USD"] = {"tick": 0.1, "step": 0.001,
                               "min_qty": 0.001, "min_notional": 1.0}
        c.get_positions = AsyncMock(return_value=[
            {"symbol": "BTC-USD", "size": 0.05}
        ])
        c._request = AsyncMock(return_value={"orderId": "stop123"})
        from execution.schemas import BracketOrder, TradeCandidate
        cand = TradeCandidate(
            symbol="BTC-USD", side="long", entry_price=65000,
            stop_price=64000, tp1_price=66000, tp2_price=67000,
            tp3_price=68000, size=0.05, leverage=5, initial_margin=10,
            rr_ratio=2.0, coherence_score=5.0, size_multiplier=1.0,
            signal_reason="test", invalidation="test", timestamp_ms=0,
        )
        brkt = BracketOrder(candidate=cand, account_id="acc1", symbol_id=1)
        res = await c.place_protective_orders(brkt)
        self.assertTrue(res.success)
        self.assertEqual(res.stop_order_id, "stop123")
        # TP orders: _place_tp_orders reads tp1_price / tp2_price
        self.assertIsNotNone(res.tp1_order_id)
        self.assertIsNotNone(res.tp2_order_id)

    async def test_place_bracket_returns_tp_ids_not_list_attr(self):
        c = _client()
        c._specs["BTC-USD"] = {"tick": 0.1, "step": 0.001,
                               "min_qty": 0.001, "min_notional": 1.0}
        c.get_positions = AsyncMock(return_value=[])
        c._venue_equity = AsyncMock(return_value=1000.0)
        c._request = AsyncMock(return_value={"orderId": "entry1"})
        c._confirm_position_open = AsyncMock(return_value=True)
        from execution.schemas import BracketOrder, TradeCandidate
        cand = TradeCandidate(
            symbol="BTC-USD", side="long", entry_price=65000,
            stop_price=64000, tp1_price=66000, tp2_price=67000,
            tp3_price=68000, size=0.05, leverage=5, initial_margin=10,
            rr_ratio=2.0, coherence_score=5.0, size_multiplier=1.0,
            signal_reason="test", invalidation="test", timestamp_ms=0,
        )
        brkt = BracketOrder(candidate=cand, account_id="acc1", symbol_id=1)
        res = await c.place_bracket(brkt)
        self.assertTrue(res.success)
        self.assertEqual(res.entry_order_id, "entry1")
        self.assertIsNotNone(res.stop_order_id)
        self.assertIsNotNone(res.tp1_order_id)
        self.assertIsNotNone(res.tp2_order_id)
        self.assertFalse(hasattr(res, "tp_order_ids") and res.tp_order_ids is not None)


class TestClosePositionMarket(unittest.IsolatedAsyncioTestCase):
    """2026-08-20 dust-at-source fix (v2): one-way full closes submit the
    EXCHANGE-reported qty (step-aligned by construction → zero residue).
    closePosition=true was the cleaner v1 but Aster V3 rejects it for MARKET
    orders — live-verified: "Target strategy invalid for orderType MARKET,
    closePosition true" ×41. Partial closes and hedge mode keep the exact
    caller qty. Poll failure never blocks a close."""

    def _ready(self, hedge=False, positions=()):
        c = _client()
        c.hedge_mode = hedge
        c._specs["VIRTUAL-USD"] = {"tick": 0.0001, "step": 0.1,
                                   "min_qty": 0.1, "min_notional": 1.0}
        c.get_positions = AsyncMock(return_value=list(positions))
        c._request = AsyncMock(return_value={"orderId": 99})
        return c

    async def test_full_close_uses_exchange_qty(self):
        c = self._ready(positions=[{"symbol": "VIRTUAL-USD", "size": 165.0}])
        r = await c.close_position_market(symbol="VIRTUAL-USD", side="long",
                                          size=164.9)  # tracked < actual
        self.assertTrue(r.success)
        params = c._request.call_args[0][2]
        self.assertNotIn("closePosition", params)
        self.assertEqual(params["type"], "MARKET")
        self.assertEqual(params["side"], "SELL")
        self.assertEqual(params["quantity"], "165")
        self.assertEqual(params["reduceOnly"], "true")

    async def test_dust_close_uses_exchange_qty(self):
        # The VIRTUAL loop: caller's 0.1 vs exchange's 0.1 — within one step,
        # exchange qty wins (identical here; the drift case is covered above).
        c = self._ready(positions=[{"symbol": "VIRTUAL-USD", "size": 0.1}])
        r = await c.close_position_market(symbol="VIRTUAL-USD", side="long",
                                          size=0.1)
        self.assertTrue(r.success)
        params = c._request.call_args[0][2]
        self.assertNotIn("closePosition", params)
        self.assertEqual(params["quantity"], "0.1")

    async def test_partial_close_keeps_qty_path(self):
        # Treasury trim: 82.5 of 165 — caller's exact qty, poll not consulted
        # for a raise (82.5 < 165 - step).
        c = self._ready(positions=[{"symbol": "VIRTUAL-USD", "size": 165.0}])
        r = await c.close_position_market(symbol="VIRTUAL-USD", side="long",
                                          size=82.5)
        self.assertTrue(r.success)
        params = c._request.call_args[0][2]
        self.assertNotIn("closePosition", params)
        self.assertEqual(params["reduceOnly"], "true")
        self.assertEqual(params["quantity"], "82.5")

    async def test_hedge_mode_always_qty_path(self):
        c = self._ready(hedge=True,
                        positions=[{"symbol": "VIRTUAL-USD", "size": 165.0}])
        await c.close_position_market(symbol="VIRTUAL-USD", side="long",
                                      size=164.9)
        params = c._request.call_args[0][2]
        self.assertNotIn("closePosition", params)
        self.assertEqual(params["quantity"], "164.9")

    async def test_position_poll_failure_falls_back_to_qty(self):
        # A close must never be blocked by a read failure.
        c = self._ready()
        c.get_positions = AsyncMock(side_effect=RuntimeError("rpc down"))
        r = await c.close_position_market(symbol="VIRTUAL-USD", side="long",
                                          size=165.0)
        self.assertTrue(r.success)
        params = c._request.call_args[0][2]
        self.assertNotIn("closePosition", params)
        self.assertEqual(params["quantity"], "165")


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


class TestAsterKlines(unittest.TestCase):
    """2026-08-18: aster-routed tradfi (XAUT/CL) gets execution-venue candles
    via kline_1m — Yahoo futures 1m lagged ~10min overnight and the 90s
    staleness guard vetoed every commodity signal (23.7k signal_stale_data)."""

    def _feed(self):
        from data.candle_buffer import CandleBuffer
        bufs = {"XAUT-USD": {"1m": CandleBuffer("XAUT-USD", "1m")}}
        f = AsterFeed(symbols=["XAUT-USD"], kline_symbols=["XAUT-USD"],
                      candle_buffers=bufs)
        return f, bufs["XAUT-USD"]["1m"]

    _KLINE = {"e": "kline", "E": 1700000060000, "s": "XAUUSDT",
              "k": {"t": 1700000000000, "T": 1700000059999, "s": "XAUUSDT",
                    "i": "1m", "o": "2400.1", "h": "2401.5", "l": "2399.9",
                    "c": "2401.2", "v": "12.5", "x": True}}

    def test_stream_url_includes_kline(self):
        f, _ = self._feed()
        self.assertIn("xauusdt@kline_1m", f._stream_url())

    def test_closed_bar_writes_candle_and_publishes(self):
        import data.aster_feed as af
        f, buf = self._feed()
        with patch.object(af.event_bus, "publish") as pub:
            f._handle_kline(dict(self._KLINE))
        self.assertEqual(buf.count(), 1)
        c = buf.latest(1)[0]
        self.assertEqual(c.open_time, 1700000000000)
        self.assertAlmostEqual(c.close, 2401.2)
        self.assertEqual(pub.call_count, 1)
        self.assertEqual(pub.call_args[0][0].symbol, "XAUT-USD")

    def test_in_progress_bar_written_not_published(self):
        # Bybit contract: the forming bar keeps the buffer tail fresh so the
        # interpreter's 90s staleness guard passes — but never publishes.
        import data.aster_feed as af
        f, buf = self._feed()
        msg = dict(self._KLINE)
        msg["k"] = dict(msg["k"], x=False)
        with patch.object(af.event_bus, "publish") as pub:
            f._handle_kline(msg)
        self.assertEqual(buf.count(), 1)
        self.assertEqual(pub.call_count, 0)

    def test_unknown_symbol_no_crash(self):
        import data.aster_feed as af
        f, buf = self._feed()
        msg = dict(self._KLINE)
        msg["k"] = dict(msg["k"], s="DOGEUSDT")
        with patch.object(af.event_bus, "publish"):
            f._handle_kline(msg)          # no buffer for DOGE — silent skip
        self.assertEqual(buf.count(), 0)


class TestTradfiYield(unittest.IsolatedAsyncioTestCase):
    """Yielded symbols (aster kline owns candles) still get Yahoo underlying
    prices for divergence, but tradfi never writes their candles."""

    @staticmethod
    def _payload(ts_list):
        return {"chart": {"result": [{
            "meta": {"regularMarketPrice": 2400.5},
            "timestamp": ts_list,
            "indicators": {"quote": [{
                "open": [2400.1] * len(ts_list),
                "high": [2401.0] * len(ts_list),
                "low": [2399.5] * len(ts_list),
                "close": [2400.5] * len(ts_list),
                "volume": [10] * len(ts_list)}]}}]}}

    class _Client:
        payload = None
        async def get(self, *a, **k):
            class _Resp:
                status_code = 200
                def json(self_): return self.payload
            return _Resp()

    async def test_yield_skips_candle_write_keeps_underlying(self):
        import data.tradfi_feed as tf
        from data.candle_buffer import CandleBuffer
        bufs = {"XAUT-USD": {"1m": CandleBuffer("XAUT-USD", "1m")}}
        feed = tf.TradfiFeed(candle_buffers=bufs, mark_price_stores={})
        self._Client.payload = self._payload([1700000000])

        tf.set_candle_yield(["XAUT-USD"])
        try:
            await feed._poll_one(self._Client(), "XAUT-USD", "GC=F")
            self.assertEqual(bufs["XAUT-USD"]["1m"].count(), 0)      # yielded
            self.assertEqual(feed._underlying_px["XAUT-USD"], 2400.5)  # still priced
            self.assertTrue(feed.healthy("XAUT-USD"))
            tf.set_candle_yield([])
            await feed._poll_one(self._Client(), "XAUT-USD", "GC=F")
            self.assertEqual(bufs["XAUT-USD"]["1m"].count(), 1)      # owns again
        finally:
            tf.set_candle_yield([])

    async def test_forming_bar_written_not_published(self):
        # 2026-08-18 equity-mute fix: closed-bars-only left the tail 60-120s
        # stale → the 90s interpreter guard vetoed ~every US-session signal.
        # The forming bar must land in the buffer (fresh tail, Bybit contract)
        # but never publish CANDLE_CLOSED.
        import data.tradfi_feed as tf
        from data.candle_buffer import CandleBuffer
        now = int(time.time())
        minute = now - (now % 60)
        bufs = {"AAPL-USD": {"1m": CandleBuffer("AAPL-USD", "1m")}}
        feed = tf.TradfiFeed(candle_buffers=bufs, mark_price_stores={})
        self._Client.payload = self._payload([minute - 120, minute - 60, minute])
        tf.set_candle_yield([])
        with patch.object(tf.event_bus, "publish") as pub:
            await feed._poll_one(self._Client(), "AAPL-USD", "AAPL")
        buf = bufs["AAPL-USD"]["1m"]
        self.assertEqual(buf.count(), 3)                      # forming included
        self.assertEqual(buf.latest(1)[0].open_time, minute * 1000)
        # one publish, for the newest CLOSED bar (minute-60), not the forming one
        self.assertEqual(pub.call_count, 1)
        self.assertEqual(pub.call_args[0][0].timestamp_ms, (minute - 60) * 1000)


if __name__ == "__main__":
    unittest.main()
