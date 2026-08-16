"""Unit tests for the explosive breakout path (2026-08-16) — no network.

Pins the audit-critical invariants: callbackRate formatting/clamping,
reduceOnly on the trailing stop, quantity rounding, and the config caps.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from execution.aster_client import AsterClient


def _client(**cfg):
    defaults = dict(aster_api_key="0x21cF8Ae13Bb72632562c6Fff438652Ba1a151bb0",
                    aster_api_secret=("0x4fd0a42218f3eae43a6ce26d22544e9861"
                                      "39a01e5b34a62db53757ffca81bae1"),
                    aster_sleeve_halt_dd_pct=0.30, aster_margin_pct=0.10,
                    aster_max_leverage=10, aster_max_positions=5)
    defaults.update(cfg)
    c = AsterClient(SimpleNamespace(**defaults))
    c._specs["AKE-USD"] = {"tick": 0.000001, "step": 1.0,
                           "min_qty": 1.0, "min_notional": 1.0}
    return c


class TestTrailingStop(unittest.IsolatedAsyncioTestCase):
    async def test_param_shape_oneway(self):
        c = _client()
        c._request = AsyncMock(return_value={"orderId": 99})
        oid = await c.place_trailing_stop("AKE-USD", "long", 16666.0,
                                          10.0, activation_price=0.0069)
        self.assertEqual(oid, "99")
        p = c._request.call_args[0][2]
        self.assertEqual(p["type"], "TRAILING_STOP_MARKET")
        self.assertEqual(p["side"], "SELL")          # long position → SELL trail
        self.assertEqual(p["workingType"], "MARK_PRICE")
        self.assertEqual(p["callbackRate"], "5")     # clamped to venue max (10 rejected live)
        self.assertEqual(p["reduceOnly"], "true")    # can never re-open
        self.assertEqual(p["quantity"], "16666")
        self.assertEqual(p["activationPrice"], "0.0069")
        self.assertEqual(p["symbol"], "AKEUSDT")

    async def test_callback_rate_clamped_to_safe_band(self):
        c = _client()
        c._request = AsyncMock(return_value={"orderId": 1})
        await c.place_trailing_stop("AKE-USD", "long", 10.0, 0.05)
        self.assertEqual(c._request.call_args[0][2]["callbackRate"], "0.1")
        await c.place_trailing_stop("AKE-USD", "long", 10.0, 50.0)
        self.assertEqual(c._request.call_args[0][2]["callbackRate"], "5")

    async def test_no_activation_price_omits_field(self):
        c = _client()
        c._request = AsyncMock(return_value={"orderId": 1})
        await c.place_trailing_stop("AKE-USD", "long", 10.0, 10.0)
        self.assertNotIn("activationPrice", c._request.call_args[0][2])

    async def test_zero_qty_after_rounding_returns_none(self):
        c = _client()
        c._request = AsyncMock(return_value={"orderId": 1})
        oid = await c.place_trailing_stop("AKE-USD", "long", 0.4, 10.0)
        self.assertIsNone(oid)                       # step=1.0 floors 0.4 → 0
        c._request.assert_not_called()               # never sent to exchange

    async def test_api_error_returns_none_not_raise(self):
        from execution.aster_client import AsterAPIError
        c = _client()
        c._request = AsyncMock(side_effect=AsterAPIError("boom"))
        oid = await c.place_trailing_stop("AKE-USD", "long", 10.0, 10.0)
        self.assertIsNone(oid)


class TestExplosiveConfig(unittest.TestCase):
    def test_operator_caps(self):
        from core.config import Settings
        s = Settings()
        self.assertEqual(s.explosive_max_concurrent, 3)   # max 3 at a time
        self.assertEqual(s.explosive_daily_cap, 10)       # up to 10/day
        self.assertEqual(s.explosive_min_score, 3.0)
        self.assertEqual(s.explosive_trail_callback_pct, 5.0)  # Aster venue max
        self.assertEqual(s.explosive_trail_activation_pct, 15.0)
        self.assertEqual(s.explosive_max_stop_pct, 5.0)   # wick cap
        self.assertEqual(s.explosive_time_stop_hours, 4.0)
        self.assertTrue(s.explosive_enabled)

    def test_shadow_assets_code_only(self):
        # .env can never inject a shadow symbol (issue #17 class).
        from core.config import Settings
        s = Settings(aster_shadow_assets=["DOGE-USD"])
        self.assertEqual(s.aster_shadow_assets,
                         ["BTC-USD", "ETH-USD", "SOL-USD"])

    def test_shadow_assets_not_in_aster_assets(self):
        # The isolation invariant: shadow symbols must NEVER appear in the
        # live-routing list, or boot would flip them to Aster execution.
        from core.config import Settings
        s = Settings()
        for sym in s.aster_shadow_assets:
            self.assertNotIn(sym, s.aster_assets)
            self.assertIn(sym, s.assets)   # universe membership required


class TestScannerMetrics(unittest.TestCase):
    def test_update_readiness_populates_metrics(self):
        from intelligence.explosive_scanner import ExplosiveScanner

        class _C:
            def __init__(self, close, volume):
                self.close, self.volume = close, volume

        class _Buf:
            def __init__(self, cs):
                self._cs = cs
            def latest(self, n):
                return self._cs[-n:]

        # 120 candles with strictly decaying amplitude → the current BB width
        # is the tightest of the buffer's own history (low percentile).
        cs = [_C(100.0 + (i % 3) * 0.05 * (1.0 - i / 240.0), 10.0)
              for i in range(120)]
        sc = ExplosiveScanner()
        sc.update_readiness(["FAKE-USD"], {"FAKE-USD": {"1m": _Buf(cs)}},
                            {"FAKE-USD": {"funding_rate": 0.0}}, None)
        m = sc.metrics["FAKE-USD"]
        self.assertIn("bb_pctl", m)
        self.assertLessEqual(m["bb_pctl"], 20.0)     # compressed
        self.assertIn("compression", m["precursors"])
        self.assertAlmostEqual(m["score"], len(m["precursors"]) / 4.0, places=3)
        self.assertGreater(m["vol_ratio"], 0)


if __name__ == "__main__":
    unittest.main()
