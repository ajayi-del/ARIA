"""Wiring pins for the BitGuard ETF plane (2026-10-17, Governor directive —
SoSoValue Cloudflare-dark since 10-13; TheBitGuard = primary, freshest-wins
fallback chain in _etf_flow). Kill switch bitguard_etf_enabled=False =
pre-module system bit-for-bit."""
import asyncio
import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MAIN = (_ROOT / "main.py").read_text()
_CONFIG = (_ROOT / "core" / "config.py").read_text()

import sys
sys.path.insert(0, str(_ROOT))
from data.bitguard_etf_feed import BitguardEtfFeed, _num  # noqa: E402

_SNAPSHOT = {"data": {
    "generatedAt": "2026-10-17T06:00:00Z",
    "asOfDate": "2026-10-16",
    "btc": {"netFlowYesterdayUsd": -82400000, "netFlow7dUsd": -451400000,
            "netFlow30dUsd": 1440000000},
    "eth": {"netFlowYesterdayUsd": 30500000, "netFlow7dUsd": 120000000,
            "netFlow30dUsd": 900000000}}}


def _feed(tmp_path, now=1_600_000_000.0):
    return BitguardEtfFeed(log_dir=str(tmp_path), time_fn=lambda: now)


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _FakeHttp:
    def __init__(self, payload):
        self._p = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        return _Resp(self._p)


def _patch_http(monkeypatch, payload):
    import data.bitguard_etf_feed as mod
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: _FakeHttp(payload))


def test_config_knob_default_on():
    assert "bitguard_etf_enabled: bool = True" in _CONFIG


def test_fetch_ingests_both_legs(tmp_path, monkeypatch):
    _patch_http(monkeypatch, _SNAPSHOT)
    f = _feed(tmp_path)
    assert asyncio.run(f.fetch_due()) is True
    vb = f.verdict("BTC")
    assert vb["rows"] == 1
    assert vb["last_date"] == "2026-10-16"
    assert vb["last_inflow_usd"] == -82400000.0
    assert vb["sum_3d_usd"] == -82400000.0      # honest: 1 day of history
    assert vb["streak_days"] == -1
    assert vb["accel_3d_usd"] is None           # abstains until 4 rows
    assert vb["source"] == "bitguard"
    assert vb["sum_7d_usd"] == -451400000.0     # aggregate telemetry only
    assert vb["sum_30d_usd"] == 1440000000.0
    assert f.verdict("ETH")["last_inflow_usd"] == 30500000.0
    assert f.verdict("SOL")["rows"] == 0        # no plane → honest abstain


def test_history_accumulates_newest_first(tmp_path, monkeypatch):
    f = _feed(tmp_path)
    _patch_http(monkeypatch, _SNAPSHOT)
    assert asyncio.run(f.fetch_due()) is True
    f._last_fetch_ts = 0.0
    day2 = {"data": {"asOfDate": "2026-10-17",
                     "btc": {"netFlowYesterdayUsd": 10000000},
                     "eth": {"netFlowYesterdayUsd": -5000000}}}
    _patch_http(monkeypatch, day2)
    assert asyncio.run(f.fetch_due()) is True
    vb = f.verdict("BTC")
    assert vb["rows"] == 2
    assert vb["last_date"] == "2026-10-17"
    assert vb["sum_3d_usd"] == -72400000.0      # real 2-day sum
    assert vb["streak_days"] == 1               # sign flipped


def test_same_date_repoll_refreshes_in_place(tmp_path, monkeypatch):
    f = _feed(tmp_path)
    _patch_http(monkeypatch, _SNAPSHOT)
    assert asyncio.run(f.fetch_due()) is True
    f._last_fetch_ts = 0.0
    revised = {"data": {"asOfDate": "2026-10-16",
                        "btc": {"netFlowYesterdayUsd": -90000000},
                        "eth": {"netFlowYesterdayUsd": 30500000}}}
    _patch_http(monkeypatch, revised)
    assert asyncio.run(f.fetch_due()) is True
    vb = f.verdict("BTC")
    assert vb["rows"] == 1
    assert vb["last_inflow_usd"] == -90000000.0


def test_self_throttle(tmp_path, monkeypatch):
    f = _feed(tmp_path)
    _patch_http(monkeypatch, _SNAPSHOT)
    assert asyncio.run(f.fetch_due()) is True
    assert asyncio.run(f.fetch_due()) is False  # inside the 3h interval


def test_fetch_failure_abstains_never_crashes(tmp_path, monkeypatch):
    f = _feed(tmp_path)

    class _Boom:
        async def __aenter__(self):
            raise RuntimeError("net down")

        async def __aexit__(self, *a):
            return False

    import data.bitguard_etf_feed as mod
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: _Boom())
    assert asyncio.run(f.fetch_due()) is False
    assert f.verdict("BTC")["rows"] == 0
    assert f.flow_age_hours("BTC") == 999.0


def test_bad_shape_abstains(tmp_path, monkeypatch):
    f = _feed(tmp_path)
    _patch_http(monkeypatch, {"unexpected": True})
    assert asyncio.run(f.fetch_due()) is False
    assert f.verdict("BTC")["rows"] == 0


def test_snake_case_keys_accepted():
    leg = {"net_flow_yesterday_usd": -1.5, "net_flow_7d_usd": 7.0}
    assert _num(leg, "netFlowYesterdayUsd", "net_flow_yesterday_usd") == -1.5
    assert _num(leg, "netFlow7dUsd", "net_flow_7d_usd") == 7.0


def test_cache_roundtrip(tmp_path, monkeypatch):
    f = _feed(tmp_path)
    _patch_http(monkeypatch, _SNAPSHOT)
    assert asyncio.run(f.fetch_due()) is True
    f2 = _feed(tmp_path)
    assert f2.verdict("BTC")["rows"] == 1
    assert f2.verdict("BTC")["last_inflow_usd"] == -82400000.0
    assert (tmp_path / "bitguard_etf_flows.json").exists()
    lines = (tmp_path / "bitguard_etf_flows.jsonl").read_text().strip()
    assert json.loads(lines)["flows"]["BTC"]["rows"] == 1


def test_flow_age_hours_uses_date(tmp_path, monkeypatch):
    f = _feed(tmp_path)
    _patch_http(monkeypatch, _SNAPSHOT)
    assert asyncio.run(f.fetch_due()) is True
    age = f.flow_age_hours("BTC")
    assert 0.0 <= age < 999.0


# ---- main.py splice pins (textual, house style) ----

def test_feed_instantiated_gated_on_knob():
    assert re.search(r"_bitguard_feed = \(BitguardEtfFeed\(", _MAIN)
    assert re.search(
        r'if getattr\(config, "bitguard_etf_enabled", True\) else None\)',
        _MAIN)


def test_etf_flow_freshest_wins_chain():
    m = re.search(
        r"def _etf_flow\(symbol: str\):.*?return _best_v, _best_age",
        _MAIN, re.S)
    assert m, "_etf_flow must be the freshest-wins chain"
    body = m.group(0)
    assert "for _feed in (_bitguard_feed, _soso_feed):" in body
    assert "if _best_v is None or _a < _best_age:" in body


def test_legacy_behavior_when_bitguard_absent():
    # With _bitguard_feed None the chain degenerates to the legacy read:
    # SoSoValue rows-or-None plus its own age. Pinned by the loop skipping
    # None feeds and requiring rows before candidacy.
    m = re.search(
        r"def _etf_flow\(symbol: str\):.*?return _best_v, _best_age",
        _MAIN, re.S)
    body = m.group(0)
    assert "if _feed is None:\n                continue" in body
    assert 'if not _v.get("rows"):\n                continue' in body


def test_poll_loop_reads_bitguard():
    assert "if await _bitguard_feed.fetch_due():" in _MAIN
    assert 'logger.info("bitguard_flow_poll"' in _MAIN
