"""Balance-failure guard (2026-09-18 phantom-drawdown incident) — no network.

Incident: SoDEX perps balance endpoints went dark 08:06-10:36Z (1,337
balance_fetch_failed JSONDecodeError events). SoDEXClient.get_account_balance
swallowed every failure into a returned 0.0; execution/venue.py only treats
EXCEPTIONS as leg failure, so the dark SoDEX leg summed as a real zero, the
combined read collapsed to the Aster-only sleeve (~$254 vs honest ~$653),
and the close-time SessionDrawdownTracker computed DD=61.15% >= DD_HALT_PCT
10.0 — all entries halted 08:10→13:40Z.

Fix contract pinned here:
- Total fetch failure (no parseable API answer from either endpoint) RAISES
  SoDEXAPIError under balance_failure_strict_enabled → venue marks the leg
  FAILED → guarded_combined_balance substitutes last-good.
- A parseable API answer (code == 0 with av == 0, or empty balances) is an
  honest zero — genuinely unfunded accounts must NEVER raise.
- Belt-and-braces: a 0.0 leg from a venue with last-good > 0 is treated as
  failed even when it never entered balance_failed_venues() (the swallow
  path); a never-funded venue keeps its honest zero.
- Knob off (balance_failure_strict_enabled=False) = pre-fix bit-for-bit:
  0.0 return, exception-only substitution.
"""

import asyncio
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution import venue  # noqa: E402
from execution.sodex_client import SoDEXClient, SoDEXAPIError  # noqa: E402

_WALLET = "0xdb87899000000000000000000000000000000001"


def _client(**cfg):
    defaults = dict(sodex_account_id=_WALLET, account_id="",
                    sodex_mainnet=True,
                    sodex_rest_perps="https://mainnet-gw.sodex.dev/api/v1/perps")
    defaults.update(cfg)
    signer = Mock()
    signer.get_address.return_value = "0x36C54F0000000000000000000000000000000001"
    c = SoDEXClient(SimpleNamespace(**defaults), signer, Mock())
    c.client = SimpleNamespace(get=AsyncMock())
    return c


def _resp(payload):
    return SimpleNamespace(status_code=200, text="", json=lambda: payload)


def _run(coro):
    # The retry path sleeps 2s per endpoint — patch it out.
    with patch("asyncio.sleep", new=AsyncMock()):
        return asyncio.run(coro)


class TestSoDEXBalanceFailureContract(unittest.TestCase):
    def test_total_failure_raises_in_strict_mode(self):
        # Connection-level darkness (Cloudflare HTML / timeouts).
        c = _client()  # knob absent → getattr default True
        c.client.get = AsyncMock(side_effect=ConnectionError("Cloudflare HTML"))
        with self.assertRaises(SoDEXAPIError):
            _run(c.get_account_balance(_WALLET))

    def test_json_decode_failure_raises(self):
        # The exact 2026-09-18 failure mode: HTTP 200 with a non-JSON body.
        c = _client()
        bad = SimpleNamespace(
            status_code=200, text="<html>error</html>",
            json=Mock(side_effect=json.JSONDecodeError("Expecting value", "<html>", 0)))
        c.client.get = AsyncMock(return_value=bad)
        with self.assertRaises(SoDEXAPIError):
            _run(c.get_account_balance(_WALLET))

    def test_genuine_zero_does_not_raise(self):
        # API alive, account reports av = 0 — honest zero, never raise.
        c = _client()
        c.client.get = AsyncMock(return_value=_resp({"code": 0, "data": {"av": "0"}}))
        assert _run(c.get_account_balance(_WALLET)) == 0.0

    def test_unfunded_account_does_not_raise(self):
        # /state carries no av; /balances is empty — a never-funded account.
        c = _client()
        c.client.get = AsyncMock(side_effect=[
            _resp({"code": 0, "data": {}}),
            _resp({"code": 0, "data": {"balances": []}}),
        ])
        assert _run(c.get_account_balance(_WALLET)) == 0.0

    def test_api_error_code_does_not_raise(self):
        # Parseable answer with a non-zero code: the API is alive — legacy
        # 0.0 return preserved (conservative; not the incident class).
        c = _client()
        c.client.get = AsyncMock(return_value=_resp({"code": 10001, "msg": "rate limited"}))
        assert _run(c.get_account_balance(_WALLET)) == 0.0

    def test_positive_balance_unchanged(self):
        c = _client()
        c.client.get = AsyncMock(return_value=_resp({"code": 0, "data": {"av": "653.21"}}))
        assert _run(c.get_account_balance(_WALLET)) == 653.21

    def test_balances_fallback_still_works(self):
        # /state dark, /balances alive with a positive aw — fallback returns it.
        c = _client()
        c.client.get = AsyncMock(side_effect=[
            ConnectionError("state dark"),
            ConnectionError("state dark"),
            _resp({"code": 0, "data": {"balances": [{"aw": "412.5"}]}}),
        ])
        assert _run(c.get_account_balance(_WALLET)) == 412.5

    def test_knob_off_legacy_zero_return(self):
        # balance_failure_strict_enabled=False → pre-fix bit-for-bit: 0.0, no raise.
        c = _client(balance_failure_strict_enabled=False)
        c.client.get = AsyncMock(side_effect=ConnectionError("dark"))
        assert _run(c.get_account_balance(_WALLET)) == 0.0


class TestGuardedCombinedBalance(unittest.TestCase):
    def test_failed_leg_substitutes_last_good(self):
        # The 2026-08-18 guard: exception-flagged leg uses the cache.
        assert venue.guarded_combined_balance(
            {"sodex": 0.0, "aster": 254.0}, frozenset({"sodex"}),
            {"sodex": 399.0, "aster": 254.0}) == 653.0

    def test_zero_leg_with_last_good_substitutes_strict(self):
        # Belt-and-braces: leg NOT in the failed set (the 2026-09-18 swallow
        # path) still substitutes when the venue was previously funded.
        assert venue.guarded_combined_balance(
            {"sodex": 0.0, "aster": 254.0}, frozenset(),
            {"sodex": 399.0, "aster": 254.0}, strict=True) == 653.0

    def test_never_funded_venue_keeps_honest_zero(self):
        # last-good is 0.0 → the venue was never funded → 0.0 flows through.
        assert venue.guarded_combined_balance(
            {"sodex": 0.0, "aster": 254.0}, frozenset(),
            {"sodex": 0.0, "aster": 254.0}, strict=True) == 254.0

    def test_healthy_legs_untouched(self):
        assert venue.guarded_combined_balance(
            {"sodex": 399.0, "aster": 254.0}, frozenset(),
            {"sodex": 399.0, "aster": 254.0}, strict=True) == 653.0

    def test_knob_off_legacy_semantics(self):
        # strict=False: a 0.0 leg NOT in the failed set sums as a real zero
        # (pre-2026-09-18 behavior, bit-for-bit)...
        assert venue.guarded_combined_balance(
            {"sodex": 0.0, "aster": 254.0}, frozenset(),
            {"sodex": 399.0, "aster": 254.0}, strict=False) == 254.0
        # ...but exception-flagged legs still substitute (2026-08-18 guard kept).
        assert venue.guarded_combined_balance(
            {"sodex": 0.0, "aster": 254.0}, frozenset({"sodex"}),
            {"sodex": 399.0, "aster": 254.0}, strict=False) == 653.0


class _ExecOK:
    def __init__(self, balance=100.0):
        self._balance = balance

    async def get_positions(self, address=""):
        return []

    async def get_account_balance(self, address=""):
        return self._balance


class TestVenueIntegration(unittest.TestCase):
    """End-to-end: a dark SoDEX client marks the venue leg FAILED and the
    guarded sum never collapses to the Aster-only sleeve."""

    def setUp(self):
        venue._executors.clear()
        venue._venue_by_symbol.clear()
        venue._positions_failures.clear()
        venue._balance_failures.clear()

    def tearDown(self):
        venue._executors.clear()
        venue._venue_by_symbol.clear()
        venue._positions_failures.clear()
        venue._balance_failures.clear()

    def test_dark_sodex_leg_failed_and_last_good_substituted(self):
        sodex = _client()
        sodex.client.get = AsyncMock(side_effect=ConnectionError("dark"))
        venue.register_executor("sodex", sodex)
        venue.register_executor("aster", _ExecOK(balance=254.0))
        venue.assign_symbols(["UNI-USD"], "aster")

        out = _run(venue.venue_balances("addr"))
        assert out["sodex"] == 0.0 and out["aster"] == 254.0
        assert venue.balance_failed_venues() == frozenset({"sodex"})

        last_good = {"sodex": 399.0, "aster": 254.0}
        total = venue.guarded_combined_balance(
            out, venue.balance_failed_venues(), last_good)
        assert total == 653.0   # no phantom collapse to the 254 Aster sleeve

    def test_knob_off_dark_sodex_leg_reads_zero(self):
        # Legacy: the swallowed 0.0 was never flagged, never substituted.
        sodex = _client(balance_failure_strict_enabled=False)
        sodex.client.get = AsyncMock(side_effect=ConnectionError("dark"))
        venue.register_executor("sodex", sodex)
        venue.register_executor("aster", _ExecOK(balance=254.0))
        venue.assign_symbols(["UNI-USD"], "aster")

        out = _run(venue.venue_balances("addr"))
        assert venue.balance_failed_venues() == frozenset()
        total = venue.guarded_combined_balance(
            out, venue.balance_failed_venues(),
            {"sodex": 399.0, "aster": 254.0}, strict=False)
        assert total == 254.0   # the pre-fix phantom, reproduced under knob-off


if __name__ == "__main__":
    unittest.main()
