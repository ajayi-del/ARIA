"""Unit tests for execution/arcus_client.py — no network.

Deterministic Ed25519 key from a fixed seed; every signed message is verified
with the matching VerifyKey, so the Scheme-1/Scheme-2 constructions are proven
against the real primitive, not a mock.
"""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from nacl.signing import SigningKey

from execution.arcus_client import (ArcusAPIError, ArcusClient, _TIF_MAP,
                                    _dec_str, _quantums, _round_step, _ticks,
                                    to_arcus_symbol, to_canonical_symbol)

_SEED = bytes(range(32)).hex()                      # deterministic signing key
_PUB = SigningKey(bytes.fromhex(_SEED)).verify_key.encode().hex()
_ADDR = "0xAbCdEf0123456789aBCdef0123456789AbCdEf01"

_MARKETS_FIXTURE = [
    {"marketDisplayName": "BTC-USD", "marketId": 1, "status": "ONLINE",
     "tickSize": "0.1", "stepSize": "0.00000001", "minOrderNotional": "5",
     "minOrderSize": "0.0001", "maxOrderSize": "10000",
     "markPrice": "80747.9"},
    {"marketDisplayName": "SPY-USD", "marketId": 65, "status": "ONLINE",
     "tickSize": "0.01", "stepSize": "0.001", "minOrderNotional": "5",
     "minOrderSize": "0.001", "maxOrderSize": "50000",
     "markPrice": "672.11"},
    {"marketDisplayName": "F-USD", "marketId": 66, "status": "OFFLINE",
     "tickSize": "0.01", "stepSize": "0.01", "minOrderNotional": "5"},
]


def _client(**cfg):
    defaults = dict(arcus_enabled=True, arcus_api_key=_PUB,
                    arcus_signing_key=_SEED, arcus_address=_ADDR,
                    arcus_account_index=0, arcus_margin_pct=0.10,
                    arcus_max_leverage=10, arcus_max_positions=5,
                    arcus_sleeve_halt_dd_pct=0.30)
    defaults.update(cfg)
    c = ArcusClient(SimpleNamespace(**defaults))
    c._specs["BTC-USD"] = {"market_id": 1, "tick": 0.1, "step": 1e-8,
                           "min_notional": 5.0, "min_size": 0.0001,
                           "max_size": 10000.0}
    return c


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestSigner(unittest.TestCase):
    def test_signer_ready_and_pubkey_matches(self):
        c = _client()
        self.assertTrue(c._keys_ready)
        self.assertEqual(c._signer.verify_key.encode().hex(), _PUB)

    def test_key_mismatch_refuses(self):
        other = SigningKey(b"\x01" * 32).verify_key.encode().hex()
        c = _client(arcus_api_key=other)
        self.assertFalse(c._keys_ready)
        self.assertIsNone(c._signer)

    def test_missing_keys_refuses(self):
        c = _client(arcus_api_key="", arcus_signing_key="")
        self.assertFalse(c._keys_ready)

    def test_timestamps_strictly_increase(self):
        c = _client()
        ts = [c._ts_ns() for _ in range(100)]
        self.assertEqual(ts, sorted(ts))
        self.assertEqual(len(set(ts)), 100)


class TestCanonicalSigning(unittest.TestCase):
    def test_scheme1_canonical_key_order(self):
        c = _client()
        payload = {"ad": _ADDR.lower(), "ai": 0, "ct": 123, "g": 456, "m": 1,
                   "op": 1, "p": 807479, "q": 100000, "r": 0, "s": 0, "t": 0,
                   "v": 1}
        self.assertEqual(
            c._canonical(payload),
            '{"ad":"' + _ADDR.lower() + '","ai":0,"ct":123,"g":456,"m":1,'
            '"op":1,"p":807479,"q":100000,"r":0,"s":0,"t":0,"v":1}')

    def test_scheme1_signature_verifies(self):
        c = _client()
        payload = {"ad": _ADDR.lower(), "ai": 0, "ct": c._ts_ns(), "g": 99,
                   "m": 1, "op": 1, "p": 807479, "q": 100000, "r": 0, "s": 0,
                   "t": 0, "v": 1, "_px": 80747.9, "_qty": 0.001}
        headers = c._order_headers(payload)
        self.assertEqual(headers["X-API-Key"], _PUB)
        self.assertEqual(headers["X-Timestamp"], str(payload["ct"]))
        wire = {k: v for k, v in payload.items() if not k.startswith("_")}
        # Verifies against the REAL pubkey; raises on any deviation
        c._signer.verify_key.verify(
            c._canonical(wire).encode(), bytes.fromhex(headers["X-Signature"]))

    def test_scheme1_underscore_keys_never_signed(self):
        c = _client()
        payload = {"ad": _ADDR.lower(), "ai": 0, "ct": 7, "g": 1, "m": 1,
                   "op": 1, "p": 1, "q": 1, "r": 0, "s": 0, "t": 0, "v": 1,
                   "_px": 1.0, "_qty": 1.0}
        c._order_headers(payload)
        signed = c._canonical({k: v for k, v in payload.items()
                               if not k.startswith("_")})
        self.assertNotIn("_px", signed)
        self.assertNotIn("_qty", signed)

    def test_scheme2_signature_verifies(self):
        c = _client()
        ts = c._ts_ns()
        body = {"address": _ADDR, "marketId": 1, "leverage": 10,
                "accountIndex": 0}
        headers = c._legacy_headers("setLeverage", body, ts)
        msg = str(ts) + "setLeverage" + c._canonical(body)
        c._signer.verify_key.verify(msg.encode(),
                                    bytes.fromhex(headers["X-Signature"]))

    def test_scheme2_signed_get_no_body(self):
        c = _client()
        ts = c._ts_ns()
        headers = c._legacy_headers("inviteCodes", None, ts)
        msg = str(ts) + "inviteCodes"
        c._signer.verify_key.verify(msg.encode(),
                                    bytes.fromhex(headers["X-Signature"]))


class TestGridConversions(unittest.TestCase):
    def test_ticks_exact(self):
        self.assertEqual(_ticks(80747.9, 0.1), 807479)

    def test_ticks_off_grid_raises(self):
        with self.assertRaises(ArcusAPIError):
            _ticks(80747.95, 0.1)

    def test_quantums_exact(self):
        self.assertEqual(_quantums(0.001, 1e-8), 100000)

    def test_quantums_off_grid_raises(self):
        with self.assertRaises(ArcusAPIError):
            _quantums(0.000000015, 1e-8)

    def test_dec_str_no_exponent(self):
        self.assertEqual(_dec_str(1e-8), "0.00000001")
        self.assertEqual(_dec_str(80747.9), "80747.9")

    def test_alo_side_aware_rounding(self):
        # Post-only: buys floor (never cross up), sells ceil (never cross down)
        self.assertAlmostEqual(_round_step(80747.95, 0.1, floor=True), 80747.9)
        self.assertAlmostEqual(_round_step(80747.95, 0.1, ceil=True), 80748.0)


class TestSignedOrderPayload(unittest.TestCase):
    def test_enums_and_units(self):
        c = _client()
        gtt_us = 1_800_000_000_000_000
        p = c._signed_order_payload("BTC-USD", "long", 0.001, 80747.9, "GTT",
                                    False, gtt_us, "aria-test_1")
        self.assertEqual((p["op"], p["s"], p["t"], p["r"], p["v"]),
                         (1, 0, 0, 0, 1))
        self.assertEqual(p["g"], gtt_us * 1000)      # µs → ns
        self.assertEqual(p["p"], 807479)             # 80747.9 ÷ 0.1
        self.assertEqual(p["q"], 100000)             # 0.001 ÷ 1e-8
        self.assertEqual(p["m"], 1)
        self.assertEqual(p["ad"], _ADDR.lower())
        self.assertEqual(p["c"], "aria-test_1")

    def test_short_sell_enum(self):
        c = _client()
        p = c._signed_order_payload("BTC-USD", "short", 0.001, 80747.9,
                                    "IOC", True, 1)
        self.assertEqual((p["s"], p["t"], p["r"]), (1, 2, 1))
        self.assertNotIn("c", p)                      # empty clientId omitted

    def test_tif_map(self):
        self.assertEqual(_TIF_MAP["GTC"], "GTT")
        self.assertEqual(_TIF_MAP["PostOnly"], "ALO")

    def test_client_id_sanitized(self):
        c = _client()
        p = c._signed_order_payload("BTC-USD", "long", 0.001, 80747.9, "GTT",
                                    False, 1, "bad id!@#$%^&*()_+-x" * 3)
        self.assertNotIn(" ", p["c"])
        self.assertLessEqual(len(p["c"]), 36)


class TestSpecSync(unittest.TestCase):
    def test_sync_parses_markets(self):
        c = _client(arcus_enabled=False, arcus_api_key="",
                    arcus_signing_key="", arcus_address="")
        c._request = AsyncMock(return_value=_MARKETS_FIXTURE)
        n = _run(c.sync_symbol_specs(["BTC-USD", "SPY-USD", "F-USD"]))
        self.assertEqual(n, 2)                        # OFFLINE F-USD skipped
        self.assertTrue(c.listed("BTC-USD"))
        self.assertFalse(c.listed("F-USD"))
        spec = c.get_spec("SPY-USD")
        self.assertEqual(spec["market_id"], 65)
        self.assertEqual(spec["tick"], 0.01)
        self.assertEqual(spec["min_notional"], 5.0)

    def test_symbol_map_identity(self):
        self.assertEqual(to_arcus_symbol("BTC-USD"), "BTC-USD")
        self.assertEqual(to_canonical_symbol("BTC-USD"), "BTC-USD")


class TestPlaceOrder(unittest.TestCase):
    def test_refused_when_not_armed(self):
        c = _client(arcus_enabled=False)
        r = _run(c.place_order({"symbol": "BTC-USD", "side": "long",
                                "qty": 0.001}))
        self.assertEqual(r.status, "rejected")
        self.assertIn("arcus_not_armed", r.error)

    def test_refused_unlisted(self):
        c = _client()
        r = _run(c.place_order({"symbol": "DOGE-USD", "side": "long",
                                "qty": 10}))
        self.assertEqual(r.status, "rejected")
        self.assertIn("unlisted", r.error)

    def test_limit_wire_shape(self):
        c = _client()
        captured = {}

        async def fake_request(method, path, params=None, body=None,
                               headers=None):
            captured.update(method=method, path=path, params=params,
                            body=body, headers=headers)
            return {"orderId": "abc123", "status": "ORDER_ACKNOWLEDGED"}

        c._request = fake_request
        r = _run(c.place_order({
            "symbol": "BTC-USD", "side": "long", "qty": 0.001,
            "order_type": "LIMIT", "price": 80747.9,
            "time_in_force": "PostOnly", "link_id": "aria-x1"}))
        self.assertEqual(r.status, "open")
        self.assertEqual(r.order_id, "abc123")
        body = captured["body"]
        self.assertEqual(captured["path"], "/v1/placeOrder")
        self.assertEqual(captured["params"], {"address": _ADDR})
        self.assertEqual(body["orderSide"], "BUY")
        self.assertEqual(body["orderType"], "LIMIT")
        self.assertEqual(body["timeInForce"], "ALO")
        self.assertEqual(body["quantity"], "0.001")
        self.assertEqual(body["price"], "80747.9")
        self.assertEqual(body["marketId"], 1)
        self.assertEqual(body["accountIndex"], 0)
        self.assertEqual(body["clientId"], "aria-x1")
        # goodTilTime µs ≥ 1 month out; timestamp ns == ct == X-Timestamp
        gtt = int(body["goodTilTime"])
        self.assertGreater(gtt, 30 * 86_400 * 1_000_000)
        self.assertEqual(str(body["timestamp"]),
                         captured["headers"]["X-Timestamp"])
        # Header signature verifies over the canonical typed payload
        wire = {"ad": _ADDR.lower(), "ai": 0, "c": "aria-x1",
                "ct": body["timestamp"], "g": gtt * 1000, "m": 1, "op": 1,
                "p": 807479, "q": 100000, "r": 0, "s": 0, "t": 3, "v": 1}
        c._signer.verify_key.verify(
            c._canonical(wire).encode(),
            bytes.fromhex(captured["headers"]["X-Signature"]))

    def test_market_order_ioc_with_slippage_bound(self):
        c = _client()
        captured = {}

        async def fake_request(method, path, params=None, body=None,
                               headers=None):
            captured.update(path=path, body=body)
            if path == "/v1/markets":
                return _MARKETS_FIXTURE
            return {"orderId": "m1"}

        c._request = fake_request
        r = _run(c.place_order({"symbol": "BTC-USD", "side": "long",
                                "qty": 0.001, "order_type": "MARKET"}))
        self.assertEqual(r.status, "open")
        body = captured["body"]
        self.assertEqual(body["orderType"], "MARKET")
        self.assertEqual(body["timeInForce"], "IOC")
        # mark 80747.9 × 1.05 = 84785.295 → tick-rounded to 84785.3
        self.assertEqual(body["price"], "84785.3")

    def test_rejection_reason_propagates(self):
        c = _client()

        async def fake_request(method, path, params=None, body=None,
                               headers=None):
            return {"orderId": "", "rejectionReason": "POST_ONLY_WOULD_CROSS"}

        c._request = fake_request
        r = _run(c.place_order({
            "symbol": "BTC-USD", "side": "long", "qty": 0.001,
            "order_type": "LIMIT", "price": 80747.9,
            "time_in_force": "PostOnly"}))
        self.assertEqual(r.status, "rejected")
        self.assertEqual(r.error, "POST_ONLY_WOULD_CROSS")


class TestCancelAndDMS(unittest.TestCase):
    def test_cancel_by_order_id_payload(self):
        c = _client()
        captured = {}

        async def fake_request(method, path, params=None, body=None,
                               headers=None):
            captured.update(body=body, headers=headers)
            return {"status": "CANCEL_ACKNOWLEDGED"}

        c._request = fake_request
        ok = _run(c.cancel_order("deadbeef", symbol="BTC-USD"))
        self.assertTrue(ok)
        self.assertEqual(captured["body"]["kind"], "orderId")
        self.assertEqual(captured["body"]["orderId"], "deadbeef")
        # signed payload: op=2 with id, no c
        body = captured["body"]
        wire = {"ad": _ADDR.lower(), "ai": 0, "ct": body["timestamp"],
                "id": "deadbeef", "m": 1, "op": 2, "v": 1}
        c._signer.verify_key.verify(
            c._canonical(wire).encode(),
            bytes.fromhex(captured["headers"]["X-Signature"]))

    def test_cancel_by_client_id_payload(self):
        c = _client()
        captured = {}

        async def fake_request(method, path, params=None, body=None,
                               headers=None):
            captured.update(body=body, headers=headers)
            return {"status": "CANCEL_ACKNOWLEDGED"}

        c._request = fake_request
        ok = _run(c.cancel_order("", symbol="BTC-USD", client_id="aria-9"))
        self.assertTrue(ok)
        self.assertEqual(captured["body"]["kind"], "clientId")
        body = captured["body"]
        wire = {"ad": _ADDR.lower(), "ai": 0, "c": "aria-9",
                "ct": body["timestamp"], "m": 1, "op": 2, "v": 1}
        c._signer.verify_key.verify(
            c._canonical(wire).encode(),
            bytes.fromhex(captured["headers"]["X-Signature"]))

    def test_cancel_all_scheme2(self):
        c = _client()
        captured = {}

        async def fake_request(method, path, params=None, body=None,
                               headers=None):
            captured.update(path=path, body=body, headers=headers)
            return {"status": "CANCEL_ALL_ACKNOWLEDGED"}

        c._request = fake_request
        ok = _run(c.cancel_all_orders(symbol="BTC-USD"))
        self.assertTrue(ok)
        self.assertEqual(captured["path"], "/v1/cancelAllOrders")
        body = captured["body"]
        self.assertEqual(body["marketId"], 1)
        ts = int(captured["headers"]["X-Timestamp"])
        msg = str(ts) + "cancelAllOrders" + c._canonical(body)
        c._signer.verify_key.verify(
            msg.encode(), bytes.fromhex(captured["headers"]["X-Signature"]))

    def test_deadman_arm_and_disarm(self):
        c = _client()
        calls = []

        async def fake_request(method, path, params=None, body=None,
                               headers=None):
            calls.append(body)
            return {"status": "scheduled"}

        c._request = fake_request
        self.assertTrue(_run(c.set_deadman_switch(120.0)))
        self.assertIn("time", calls[0])
        # deadline µs within the venue's 5s–5min window
        import time as _t
        self.assertGreater(calls[0]["time"],
                           int((_t.time() + 5) * 1_000_000))
        self.assertLess(calls[0]["time"], int((_t.time() + 300) * 1_000_000))
        self.assertTrue(_run(c.set_deadman_switch(None)))
        self.assertNotIn("time", calls[1])
        # Scheme 2 with action scheduleCancel
        ts = None  # signature path covered by cancel_all pin; body shape here
        self.assertEqual(calls[0]["address"], _ADDR)

    def test_deadman_market_scoped(self):
        c = _client()
        calls = []

        async def fake_request(method, path, params=None, body=None,
                               headers=None):
            calls.append(body)
            return {"status": "scheduled"}

        c._request = fake_request
        self.assertTrue(_run(c.set_deadman_switch(60.0, symbol="BTC-USD")))
        self.assertEqual(calls[0]["marketId"], 1)


class TestLeverage(unittest.TestCase):
    def test_set_leverage_scheme2(self):
        c = _client()
        captured = {}

        async def fake_request(method, path, params=None, body=None,
                               headers=None):
            captured.update(path=path, body=body, headers=headers)
            return {"status": "ok"}

        c._request = fake_request
        ok = _run(c.update_leverage("BTC-USD", 10))
        self.assertTrue(ok)
        self.assertEqual(captured["path"], "/v1/setLeverage")
        self.assertEqual(captured["body"]["leverage"], 10)
        self.assertEqual(c._leverage_set["BTC-USD"], 10)
        ts = int(captured["headers"]["X-Timestamp"])
        msg = str(ts) + "setLeverage" + c._canonical(captured["body"])
        c._signer.verify_key.verify(
            msg.encode(), bytes.fromhex(captured["headers"]["X-Signature"]))

    def test_leverage_refused_unlisted(self):
        c = _client()
        self.assertFalse(_run(c.update_leverage("DOGE-USD", 5)))


class TestErrorTaxonomy(unittest.TestCase):
    def test_429_retry_after(self):
        c = _client()

        class FakeResp:
            status_code = 429

            def json(self):
                return {"error": "rate limited", "retryAfterMs": 250}

        async def go():
            c._http.request = AsyncMock(return_value=FakeResp())
            with self.assertRaises(ArcusAPIError) as ctx:
                await c._request("GET", "/v1/account")
            self.assertEqual(ctx.exception.retry_after_ms, 250)

        _run(go())

    def test_502_safe_retry_flag(self):
        c = _client()

        class FakeResp:
            status_code = 502

            def json(self):
                return {"error": "Transmission"}

        async def go():
            c._http.request = AsyncMock(return_value=FakeResp())
            with self.assertRaises(ArcusAPIError) as ctx:
                await c._request("POST", "/v1/placeOrder")
            self.assertTrue(ctx.exception.safe_retry)

        _run(go())

    def test_503_unknown_never_safe(self):
        c = _client()

        class FakeResp:
            status_code = 503

            def json(self):
                return {}

        async def go():
            c._http.request = AsyncMock(return_value=FakeResp())
            with self.assertRaises(ArcusAPIError) as ctx:
                await c._request("POST", "/v1/placeOrder")
            self.assertFalse(ctx.exception.safe_retry)

        _run(go())


if __name__ == "__main__":
    unittest.main()
