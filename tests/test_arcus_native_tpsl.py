"""Pins for the 2026-11-03 P0 native Arcus positionTpsl exit rewrite.

Covers: batchPlaceOrders wire contract (per-element signatures, shared ct,
quantity "0" engine-resize, op=4 signed with q:0, stopPrice unsigned),
rounding doctrine (SL away from entry, TP same direction), kill-switch
legacy parity, ALREADY_EXISTS fail-safe surfacing, and the scalp registry's
Chancellor exposure feed (mapping-agent item 1).
"""
import asyncio
import json
import unittest
from types import SimpleNamespace

from nacl.signing import SigningKey

from execution.arcus_client import (ArcusClient, _GTT_HORIZON_US, _OP_TPSL,
                                    _TPSL_SL_BOUND_PCT, _round_step)
from intelligence.arcus_scalper import (LEVERAGE_MAKER_ISOLATED,
                                        ScalpRegistry)

_SEED = bytes(range(32)).hex()
_PUB = SigningKey(bytes.fromhex(_SEED)).verify_key.encode().hex()
_ADDR = "0xAbCdEf0123456789aBCdef0123456789AbCdEf01"


def _client(**cfg):
    defaults = dict(arcus_enabled=True, arcus_api_key=_PUB,
                    arcus_signing_key=_SEED, arcus_address=_ADDR,
                    arcus_account_index=0, arcus_margin_pct=0.10,
                    arcus_max_leverage=10, arcus_max_positions=5,
                    arcus_sleeve_halt_dd_pct=0.30,
                    arcus_native_tpsl_enabled=True)
    defaults.update(cfg)
    c = ArcusClient(SimpleNamespace(**defaults))
    c._specs["BTC-USD"] = {"market_id": 1, "tick": 0.1, "step": 1e-8,
                           "min_notional": 5.0, "min_size": 0.0001,
                           "max_size": 10000.0}
    return c


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _capture(c, responses=None):
    """Intercept _request; return the captured kwargs list."""
    calls = []

    async def fake_request(method, path, params=None, body=None,
                           headers=None):
        calls.append(dict(method=method, path=path, params=params,
                          body=body, headers=headers))
        return {"responses": responses if responses is not None else [
            {"orderId": "111", "status": "ACK"},
            {"orderId": "222", "status": "ACK"},
        ]}

    c._request = fake_request
    return calls


class TestTpslWireContract(unittest.TestCase):
    def test_batch_body_grouping_and_legs(self):
        c = _client()
        calls = _capture(c)
        res = _run(c.place_position_tpsl("BTC-USD", "long", 79000.0,
                                         tp_price=81000.0,
                                         link_id="scalpt123"))
        self.assertEqual(res.status, "open")
        self.assertEqual(res.order_id, "111;222")
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["path"], "/v1/batchPlaceOrders")
        self.assertEqual(call["params"], {"address": _ADDR})
        body = call["body"]
        self.assertEqual(body["grouping"], "positionTpsl")
        orders = body["orders"]
        self.assertEqual(len(orders), 2)
        sl, tp = orders
        # SL leg: MARKET/IOC, reduceOnly, STOP_LOSS, quantity "0"
        self.assertEqual(sl["orderType"], "MARKET")
        self.assertEqual(sl["timeInForce"], "IOC")
        self.assertEqual(sl["tpslType"], "STOP_LOSS")
        self.assertEqual(sl["quantity"], "0")
        self.assertTrue(sl["reduceOnly"])
        self.assertEqual(sl["orderSide"], "SELL")  # closing a long
        self.assertEqual(sl["clientId"], "scalpt123s")
        # TP leg: LIMIT/GTT, price == stopPrice
        self.assertEqual(tp["orderType"], "LIMIT")
        self.assertEqual(tp["timeInForce"], "GTT")
        self.assertEqual(tp["tpslType"], "TAKE_PROFIT")
        self.assertEqual(tp["quantity"], "0")
        self.assertEqual(tp["price"], tp["stopPrice"])
        self.assertEqual(tp["clientId"], "scalpt123t")
        # GTT horizon ≥ 1 month on every leg
        for el in orders:
            self.assertGreaterEqual(int(el["goodTilTime"]),
                                    30 * 86_400 * 1_000_000)
        # stopPrice present on the wire
        self.assertEqual(float(sl["stopPrice"]), 79000.0)

    def test_shared_ct_and_envelope_signature(self):
        c = _client()
        calls = _capture(c)
        _run(c.place_position_tpsl("BTC-USD", "short", 81000.0,
                                   tp_price=79000.0))
        call = calls[0]
        orders = call["body"]["orders"]
        # every element's timestamp == shared ct == X-Timestamp header
        ts = {el["timestamp"] for el in orders}
        self.assertEqual(len(ts), 1)
        self.assertEqual(str(next(iter(ts))),
                         call["headers"]["X-Timestamp"])
        # per-element signature field present and distinct hex
        sigs = [el["signature"] for el in orders]
        for s in sigs:
            self.assertEqual(len(s), 128)
        self.assertNotEqual(sigs[0], sigs[1])
        # envelope X-Signature = sigs[0] (value unverified by venue)
        self.assertEqual(call["headers"]["X-Signature"], sigs[0])

    def test_signed_payload_op4_q0_no_stopprice(self):
        """op=4 signs the op=1 key set with q=0; stopPrice is NOT signed."""
        c = _client()
        captured = {}
        orig_sign = c._sign_bytes

        def spy_sign(b):
            captured["msg"] = json.loads(b.decode())
            return orig_sign(b)

        c._sign_bytes = spy_sign
        calls = _capture(c, responses=[{"orderId": "1", "status": "ACK"}])
        _run(c.place_position_tpsl("BTC-USD", "long", 79000.0,
                                   tp_price=None))
        msg = captured["msg"]
        self.assertEqual(msg["op"], _OP_TPSL)
        self.assertEqual(msg["q"], 0)          # positionTpsl signs qty "0"
        self.assertEqual(msg["r"], 1)          # reduceOnly
        self.assertNotIn("sp", msg)            # stopPrice never signed
        self.assertEqual(msg["m"], 1)
        self.assertEqual(msg["ad"], _ADDR.lower())
        self.assertEqual(msg["v"], 1)
        # signed timestamp matches the element ct
        self.assertEqual(msg["ct"],
                         calls[0]["body"]["orders"][0]["timestamp"])

    def test_sl_only_when_no_tp(self):
        c = _client()
        calls = _capture(c, responses=[{"orderId": "9", "status": "ACK"}])
        res = _run(c.place_position_tpsl("BTC-USD", "long", 79000.0))
        self.assertEqual(res.status, "open")
        orders = calls[0]["body"]["orders"]
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["tpslType"], "STOP_LOSS")


class TestTpslRoundingDoctrine(unittest.TestCase):
    def test_sl_rounds_away_from_entry(self):
        # long: floor (wider stop, never tighter)
        self.assertEqual(_round_step(100.05, 0.1, floor=True, ceil=False),
                         100.0)
        # short: ceil
        self.assertEqual(_round_step(100.05, 0.1, floor=False, ceil=True),
                         pytest_approx(100.1))

    def test_sl_bound_five_pct_away(self):
        c = _client()
        calls = _capture(c)
        _run(c.place_position_tpsl("BTC-USD", "long", 79000.0))
        sl = calls[0]["body"]["orders"][0]
        # long SL bound = stop × (1 − 5%)
        self.assertAlmostEqual(float(sl["price"]),
                               79000.0 * (1 - _TPSL_SL_BOUND_PCT), places=4)

    def test_short_sl_bound_above_trigger(self):
        c = _client()
        calls = _capture(c)
        _run(c.place_position_tpsl("BTC-USD", "short", 81000.0))
        sl = calls[0]["body"]["orders"][0]
        self.assertAlmostEqual(float(sl["price"]),
                               81000.0 * (1 + _TPSL_SL_BOUND_PCT), places=4)
        self.assertEqual(sl["orderSide"], "BUY")  # closing a short


class TestTpslGuards(unittest.TestCase):
    def test_kill_switch_off_rejects_legacy(self):
        c = _client(arcus_native_tpsl_enabled=False)
        res = _run(c.place_position_tpsl("BTC-USD", "long", 79000.0))
        self.assertEqual(res.status, "rejected")
        self.assertEqual(res.error, "tpsl_disabled")

    def test_unarmed_client_rejects(self):
        c = _client()
        c._keys_ready = False
        res = _run(c.place_position_tpsl("BTC-USD", "long", 79000.0))
        self.assertEqual(res.status, "rejected")
        self.assertIn("not_armed", res.error)

    def test_unlisted_symbol_rejects(self):
        c = _client()
        res = _run(c.place_position_tpsl("NOPE-USD", "long", 1.0))
        self.assertEqual(res.status, "rejected")
        self.assertEqual(res.error, "arcus_unlisted_symbol")

    def test_bad_stop_rejects(self):
        c = _client()
        res = _run(c.place_position_tpsl("BTC-USD", "long", 0.0))
        self.assertEqual(res.status, "rejected")
        self.assertEqual(res.error, "bad_stop")

    def test_already_exists_surfaces_for_fail_safe(self):
        """POSITION_TPSL_ALREADY_EXISTS must reach the caller as a rejected
        OrderResult whose error string contains ALREADY_EXISTS — the scalp
        loop pattern-matches it to KEEP the prior legs (never naked)."""
        c = _client()
        _capture(c, responses=[
            {"orderId": "", "status": "REJECTED",
             "rejectionReason": "POSITION_TPSL_ALREADY_EXISTS"},
        ])
        res = _run(c.place_position_tpsl("BTC-USD", "long", 79000.0,
                                         tp_price=81000.0))
        self.assertEqual(res.status, "rejected")
        self.assertIn("ALREADY_EXISTS", res.error.upper())


class TestRegistryExposureFeed(unittest.TestCase):
    def test_open_margins_math(self):
        reg = ScalpRegistry(max_concurrent=3)
        reg.register_open("BTC-USD", {
            "direction": "long", "cls": "scalp", "entry": 80000.0,
            "stop": 79000.0, "target": 82000.0, "qty": 0.001,
            "notional": 80.0, "leverage": 10, "grade": "A"}, 1000.0)
        reg.register_open("ETH-USD", {
            "direction": "short", "cls": "scalp", "entry": 3000.0,
            "stop": 3012.0, "target": 2976.0, "qty": 0.02,
            "notional": 60.0, "leverage": 15, "grade": "A"}, 1000.0)
        margins = dict(reg.open_margins())
        self.assertAlmostEqual(margins["BTC-USD"], 8.0)
        self.assertAlmostEqual(margins["ETH-USD"], 4.0)

    def test_open_margins_defaults_and_garbage(self):
        reg = ScalpRegistry(max_concurrent=3)
        # leverage absent → LEVERAGE_MAKER_ISOLATED default
        reg.register_open("BTC-USD", {
            "direction": "long", "cls": "scalp", "entry": 80000.0,
            "stop": 79000.0, "target": None, "qty": 0.001,
            "notional": 150.0, "grade": "A"}, 1000.0)
        # zero notional → excluded
        reg.register_open("ETH-USD", {
            "direction": "long", "cls": "scalp", "entry": 3000.0,
            "stop": 2900.0, "target": None, "qty": 0.0,
            "notional": 0.0, "leverage": 15, "grade": "A"}, 1000.0)
        # garbage leverage → skipped, never raises
        reg.register_open("SOL-USD", {
            "direction": "long", "cls": "scalp", "entry": 100.0,
            "stop": 99.0, "target": None, "qty": 1.0,
            "notional": 100.0, "leverage": "junk", "grade": "A"}, 1000.0)
        margins = reg.open_margins()
        self.assertEqual(len(margins), 1)
        self.assertAlmostEqual(
            margins[0][1], 150.0 / LEVERAGE_MAKER_ISOLATED)

    def test_position_state_carries_synthesized_geometry(self):
        """The adoption-repair record fields (stop/target) round-trip
        through position_state — what _arm_arcus_tpsl reads."""
        reg = ScalpRegistry(max_concurrent=3)
        reg.register_open("SPY-USD", {
            "direction": "long", "cls": "adopted", "entry": 672.0,
            "stop": 672.0 * (1 - 0.004),
            "target": 672.0 + 2.0 * (672.0 * 0.004),
            "qty": 0.1, "notional": 67.2, "grade": None}, 1000.0)
        st = reg.position_state("SPY-USD")
        self.assertIsNotNone(st["stop"])
        self.assertIsNotNone(st["target"])
        self.assertLess(st["stop"], st["entry"])
        self.assertGreater(st["target"], st["entry"])


def pytest_approx(x):
    return round(x, 6)


if __name__ == "__main__":
    unittest.main()
