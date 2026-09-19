"""Pins for the phantom-balance sentinel (proposal phantom-balance-sentinel-0918,
ceo_endorsed 2026-09-19T05:05Z, deferral released; IMPLEMENTED by the local node
in 7ad3cee as `api_responded` + balance_failure_strict_enabled raising
SoDEXAPIError — these pins lock THAT shipped contract).

The 0.0 sentinel from SoDEXClient.get_account_balance must raise when NO
endpoint returned a parseable payload (provable fetch failure — gateway down),
so venue.py marks the leg failed and the 39136a5 last-good substitution
engages. A real zero (endpoint answers code==0 with av≈0 — a wipe or unfunded
account) must still flow through as 0.0.
"""
import asyncio

import pytest

import execution.sodex_client as sc
from execution.sodex_client import SoDEXClient, SoDEXAPIError


class _Cfg:
    sodex_account_id = "0xabc123"
    account_id = "0xabc123"
    sodex_mainnet = True
    balance_failure_strict_enabled = True


class _Resp:
    def __init__(self, payload=None, raise_on_json=False):
        self._payload = payload
        self._raise = raise_on_json

    def json(self):
        if self._raise:
            raise ValueError("not JSON (Cloudflare HTML error page)")
        return self._payload


class _FakeHTTP:
    """Scripted per-endpoint responses keyed by URL tail."""

    def __init__(self, script):
        self.script = script

    async def get(self, url, timeout=20.0):
        key = "state" if url.rstrip("/").endswith("state") else "balances"
        r = self.script[key]
        if isinstance(r, Exception):
            raise r
        return r


def _client(script, cfg=_Cfg()):
    c = SoDEXClient.__new__(SoDEXClient)
    c.config = cfg
    c.client = _FakeHTTP(script)
    return c


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    async def _instant(_s):
        return None
    monkeypatch.setattr(asyncio, "sleep", _instant)


def test_both_endpoints_fail_raises():
    c = _client({"state": ConnectionError("gateway 503"),
                 "balances": ConnectionError("gateway 503")})
    with pytest.raises(SoDEXAPIError):
        asyncio.get_event_loop().run_until_complete(
            c.get_account_balance("0xabc123"))


def test_html_error_page_counts_as_failure():
    """The 09-18 shape: HTTP 200 + Cloudflare HTML body → json() raises."""
    c = _client({"state": _Resp(raise_on_json=True),
                 "balances": _Resp(raise_on_json=True)})
    with pytest.raises(SoDEXAPIError):
        asyncio.get_event_loop().run_until_complete(
            c.get_account_balance("0xabc123"))


def test_strict_knob_off_legacy_zero():
    """balance_failure_strict_enabled=False = legacy: sentinel 0.0, no raise."""
    cfg = _Cfg()
    cfg.balance_failure_strict_enabled = False
    c = _client({"state": ConnectionError("down"),
                 "balances": ConnectionError("down")}, cfg=cfg)
    out = asyncio.get_event_loop().run_until_complete(
        c.get_account_balance("0xabc123"))
    assert out == 0.0


def test_real_zero_flows_through():
    """Real wipe / unfunded: endpoints ANSWER code==0 with zeros → 0.0, no raise."""
    c = _client({
        "state": _Resp({"code": 0, "data": {"av": "0.0"}}),
        "balances": _Resp({"code": 0, "data": {"balances": [{"av": "0.0"}]}}),
    })
    out = asyncio.get_event_loop().run_until_complete(
        c.get_account_balance("0xabc123"))
    assert out == 0.0


def test_state_fails_balances_leg_succeeds():
    c = _client({
        "state": ConnectionError("down"),
        "balances": _Resp({"code": 0, "data": {"balances": [{"aw": "123.4"}]}}),
    })
    out = asyncio.get_event_loop().run_until_complete(
        c.get_account_balance("0xabc123"))
    assert out == pytest.approx(123.4)


def test_state_av_positive_legacy_path():
    c = _client({
        "state": _Resp({"code": 0, "data": {"av": "55.0"}}),
        "balances": _Resp({"code": 0, "data": {"balances": []}}),
    })
    out = asyncio.get_event_loop().run_until_complete(
        c.get_account_balance("0xabc123"))
    assert out == pytest.approx(55.0)
