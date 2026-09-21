"""Pins for the 2026-09-20 aster place_bracket clamp-telemetry fix.

Defect (verified 2026-09-20): place_bracket clamped the candidate size to
the equity ceiling SILENTLY and BracketResult carried no size field, so the
caller built the Position from the PRE-clamp candidate size — the book
believed it held more than the exchange did. The fix:
  1. BracketResult gains additive Optional size / notional_usd (None = legacy).
  2. place_bracket populates them with the ACTUAL post-clamp post-rounding
     size on success paths.
  3. A bound cap emits ONE structured `aster_bracket_size_clamped` event and
     never raises.
  4. Equity-fetch failure behavior is byte-identical to the pre-fix code.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from structlog.testing import capture_logs

from execution.aster_client import AsterClient
from execution.schemas import BracketOrder, BracketResult, TradeCandidate

_DOCS_SIGNER = "0x21cF8Ae13Bb72632562c6Fff438652Ba1a151bb0"
_DOCS_PRIVKEY = ("0x4fd0a42218f3eae43a6ce26d22544e986139a01e5b34a62db5"
                 "3757ffca81bae1")


def _client(**cfg):
    defaults = dict(aster_api_key=_DOCS_SIGNER, aster_api_secret=_DOCS_PRIVKEY,
                    aster_sleeve_halt_dd_pct=0.30, aster_margin_pct=0.10,
                    aster_max_leverage=10, aster_max_positions=5)
    defaults.update(cfg)
    return AsterClient(SimpleNamespace(**defaults))


def _bracket(size):
    cand = TradeCandidate(
        symbol="BTC-USD", side="long", entry_price=65000,
        stop_price=64000, tp1_price=66000, tp2_price=67000,
        tp3_price=68000, size=size, leverage=5, initial_margin=10,
        rr_ratio=2.0, coherence_score=5.0, size_multiplier=1.0,
        signal_reason="test", invalidation="test", timestamp_ms=0,
    )
    return BracketOrder(candidate=cand, account_id="acc1", symbol_id=1)


def _ready_client(equity=1000.0):
    c = _client()
    c._specs["BTC-USD"] = {"tick": 0.1, "step": 0.001,
                           "min_qty": 0.001, "min_notional": 1.0}
    c.get_positions = AsyncMock(return_value=[])
    c._venue_equity = AsyncMock(return_value=equity)
    c._request = AsyncMock(return_value={"orderId": "entry1"})
    c._confirm_position_open = AsyncMock(return_value=True)
    return c


class TestClampTelemetry(unittest.IsolatedAsyncioTestCase):
    # cap = equity x margin_pct x lev / entry = 1000 x 0.10 x 5 / 65000
    #     = 0.0076923... -> floored to step 0.001 -> 0.007

    async def test_cap_binds_emits_event_once_and_result_carries_final_size(self):
        c = _ready_client()
        with capture_logs() as logs:
            res = await c.place_bracket(_bracket(0.05))
        self.assertTrue(res.success)
        events = [e for e in logs if e.get("event") == "aster_bracket_size_clamped"]
        self.assertEqual(len(events), 1)               # exactly once
        ev = events[0]
        self.assertEqual(ev["symbol"], "BTC-USD")
        self.assertAlmostEqual(ev["cand_size"], 0.05)
        self.assertAlmostEqual(ev["cap_size"], 1000.0 * 0.10 * 5 / 65000)
        self.assertAlmostEqual(ev["final_size"], 0.007)
        self.assertAlmostEqual(ev["equity_used"], 1000.0)
        self.assertAlmostEqual(ev["margin_pct"], 0.10)
        self.assertEqual(ev["leverage"], 5)
        # result carries the ACTUAL post-clamp post-rounding size
        self.assertAlmostEqual(res.size, 0.007)
        self.assertAlmostEqual(res.notional_usd, 0.007 * 65000)
        # the exchange saw the same size the result reports
        qty = float(c._request.call_args_list[0][0][2]["quantity"])
        self.assertAlmostEqual(qty, res.size, places=9)

    async def test_cap_not_binding_emits_no_event_and_size_is_candidate(self):
        c = _ready_client()
        with capture_logs() as logs:
            res = await c.place_bracket(_bracket(0.005))
        self.assertTrue(res.success)
        events = [e for e in logs if e.get("event") == "aster_bracket_size_clamped"]
        self.assertEqual(events, [])
        self.assertAlmostEqual(res.size, 0.005)        # post-rounding candidate
        self.assertAlmostEqual(res.notional_usd, 0.005 * 65000)

    async def test_equity_failure_path_unchanged_no_event_no_raise(self):
        # _venue_equity returns 0 (fetch failed) — the legacy fail-closed
        # reject must behave byte-identically: same error, no clamp event,
        # no new exceptions, no order flow.
        c = _ready_client()
        c._venue_equity = AsyncMock(return_value=0.0)
        with capture_logs() as logs:
            res = await c.place_bracket(_bracket(0.05))
        self.assertFalse(res.success)
        self.assertEqual(res.error, "aster_equity_unavailable")
        self.assertIsNone(res.size)
        self.assertIsNone(res.notional_usd)
        events = [e for e in logs if e.get("event") == "aster_bracket_size_clamped"]
        self.assertEqual(events, [])
        c._request.assert_not_called()


class TestBracketResultAdditiveContract(unittest.TestCase):
    def test_legacy_construction_size_defaults_none(self):
        # Pre-build call sites construct BracketResult without the new
        # fields — the additive contract keeps them bit-for-bit valid.
        res = BracketResult(success=True, entry_order_id="e1")
        self.assertIsNone(res.size)
        self.assertIsNone(res.notional_usd)
        res2 = BracketResult(success=False, error="x")
        self.assertIsNone(res2.size)
        self.assertIsNone(res2.notional_usd)


if __name__ == "__main__":
    unittest.main()
