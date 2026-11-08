"""ProprClient venue-semantics pins.

Under test: set_leverage margin-mode resolution. The venue pins xyz:
assets isolated-only (400 13305 margin_config_invalid_margin_mode on
"cross", probed live 2026-11-08 on turbo2). The client must preserve
the existing margin config's mode and fall back by asset prefix —
never blind-send "cross" to a HIP-3 class asset.
"""

from __future__ import annotations

from prop_harness.io.propr_client import ProprClient


def _client(monkeypatch, existing_mode: str | None):
    c = ProprClient(api_key="k", base_url="https://example.invalid")
    c.account_id = "urn:prp-account:test"
    cfg = {"configId": "cfg-1"}
    if existing_mode is not None:
        cfg["marginMode"] = existing_mode
    monkeypatch.setattr(c, "get_margin_config", lambda asset: cfg)
    sent = {}

    def fake_request(method, path, params=None, payload=None):
        sent.update(payload or {})
        return {"ok": True}

    monkeypatch.setattr(c, "_request", fake_request)
    return c, sent


class TestSetLeverageMarginMode:
    def test_preserves_existing_isolated(self, monkeypatch):
        c, sent = _client(monkeypatch, "isolated")
        c.set_leverage("xyz:SP500", 10)
        assert sent["marginMode"] == "isolated"
        assert sent["leverage"] == 10
        assert sent["asset"] == "xyz:SP500"

    def test_preserves_existing_cross(self, monkeypatch):
        c, sent = _client(monkeypatch, "cross")
        c.set_leverage("BTC", 10)
        assert sent["marginMode"] == "cross"

    def test_prefix_fallback_when_no_config_mode(self, monkeypatch):
        c, sent = _client(monkeypatch, None)
        c.set_leverage("xyz:GOLD", 8)
        assert sent["marginMode"] == "isolated"
        c, sent = _client(monkeypatch, None)
        c.set_leverage("ETH", 10)
        assert sent["marginMode"] == "cross"

    def test_explicit_caller_mode_wins(self, monkeypatch):
        c, sent = _client(monkeypatch, "cross")
        c.set_leverage("BTC", 5, margin_mode="isolated")
        assert sent["marginMode"] == "isolated"
