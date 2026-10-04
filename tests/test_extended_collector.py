"""Tests for tools/extended_collector.py — the Extended venue sensor.

No network: the VERBATIM fixture tests/fixtures/extended_markets_20261004.json
(recorded 2026-10-04 via `tools/extended_collector.py --save-fixture`,
17 representative entries incl. DELISTED CL/TON and a _24_5 session variant)
is the spec. Pins: parse, status-ACTIVE fail-closed filter, missing-field
skip, symbol normalization (aliases + _24_5 strip), dedup, basis math,
HTTP error = no write + exit 1, SoDEX-leg failure degrades never blocks."""
import asyncio
import importlib.util
import json
import os

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools", "extended_collector.py")
_spec = importlib.util.spec_from_file_location("extended_collector", _PATH)
ec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ec)

FIXTURE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fixtures", "extended_markets_20261004.json")
with open(FIXTURE_PATH, "r", encoding="utf-8") as _fh:
    _FIXTURE_FULL = json.load(_fh)

# Representative 17-entry sub-payload carved from the verbatim recording:
# crypto majors, both gold markets, oil/silver/copper, a _24_5 session
# variant, the aliased TradFi names, and two DELISTED markets (CL, TON).
_WANT = ["BTC-USD", "ENA-USD", "XAU-USD", "XAUT-USD", "WTI-USD", "XBR-USD",
         "XAG-USD", "XCU-USD", "AMD_24_5-USD", "TECH100m-USD", "SPX-USD",
         "SPX500m-USD", "EUR-USD", "USDJPY-USD", "JP225-USD", "CL-USD",
         "TON-USD"]
_by_name = {e["name"]: e for e in _FIXTURE_FULL["data"]}
FIXTURE = {"status": _FIXTURE_FULL.get("status"),
           "data": [_by_name[n] for n in _WANT]}

TS = 1791122051.0


def _entry(name, **stats):
    base = {"name": name, "active": True, "status": "ACTIVE",
            "marketStats": {"markPrice": "100.0", "fundingRate": "0.00001",
                            "indexPrice": "100.1", "openInterest": "5000.0",
                            "lastPrice": "100.05", "bidPrice": "99.9",
                            "askPrice": "100.2", "dailyVolumeQuote": "12345.6",
                            "dailyLow": "99.0", "dailyHigh": "101.0",
                            "dailyPriceChangePercentage": "0.5",
                            "nextFundingRate": 1791122400000}}
    base["marketStats"].update({k: str(v) for k, v in stats.items()})
    return base


# ── parse: the verbatim fixture ──────────────────────────────────────────────

def test_fixture_parses_active_markets_skips_delisted():
    records, skipped = ec.parse_markets_payload(FIXTURE, TS)
    symbols = {r["symbol"] for r in records}
    # 17 fixture entries: CL-USD + TON-USD are DELISTED -> skipped.
    assert skipped == 2
    assert len(records) == 15
    assert "BTC-USD" in symbols
    assert "TON-USD" not in symbols
    assert "CL-USD" in symbols  # ...via the WTI->CL alias, not delisted CL


def test_fixture_btc_record_shape():
    records, _ = ec.parse_markets_payload(FIXTURE, TS)
    btc = next(r for r in records if r["symbol"] == "BTC-USD")
    assert btc["venue"] == "extended"
    assert btc["raw_symbol"] == "BTC-USD"
    assert btc["base"] == "BTC"
    assert btc["mark"] > 80000
    assert isinstance(btc["funding_rate"], float)
    assert isinstance(btc["open_interest"], float)
    assert isinstance(btc["next_funding_ms"], int)
    assert btc["sodex_mark"] is None and btc["basis_bps"] is None
    assert btc["ts"] == TS


def test_fixture_delisted_entry_parse_market_returns_none():
    ton = next(e for e in FIXTURE["data"] if e["name"] == "TON-USD")
    assert ton["status"] == "DELISTED" and ton["active"] is True  # unreliable flag
    assert ec.parse_market(ton, TS) is None


# ── parse: skip / fail-closed legs ───────────────────────────────────────────

def test_missing_market_stats_skips_symbol():
    assert ec.parse_market({"name": "FOO-USD", "active": True,
                            "status": "ACTIVE"}, TS) is None


def test_missing_mark_price_skips_symbol():
    e = _entry("FOO-USD")
    del e["marketStats"]["markPrice"]
    assert ec.parse_market(e, TS) is None


def test_zero_mark_skips_symbol():
    assert ec.parse_market(_entry("FOO-USD", markPrice="0"), TS) is None


def test_missing_funding_skips_symbol():
    e = _entry("FOO-USD")
    del e["marketStats"]["fundingRate"]
    assert ec.parse_market(e, TS) is None


def test_inactive_and_reduce_only_skipped():
    e = _entry("FOO-USD"); e["active"] = False
    assert ec.parse_market(e, TS) is None
    e = _entry("FOO-USD"); e["status"] = "REDUCE_ONLY"
    assert ec.parse_market(e, TS) is None
    e = _entry("FOO-USD"); e["status"] = "PRELISTED"
    assert ec.parse_market(e, TS) is None


def test_bad_payload_raises_no_records():
    with pytest.raises(ValueError):
        ec.parse_markets_payload({"status": "ERROR", "data": []}, TS)
    with pytest.raises(ValueError):
        ec.parse_markets_payload({"status": "OK", "data": []}, TS)
    with pytest.raises(ValueError):
        ec.parse_markets_payload(["not", "a", "dict"], TS)
    with pytest.raises(ValueError):
        ec.parse_markets_payload({"status": "OK"}, TS)


def test_all_unparseable_raises():
    payload = {"status": "OK", "data": [{"name": "X"}, {"active": False}]}
    with pytest.raises(ValueError):
        ec.parse_markets_payload(payload, TS)


# ── symbol normalization ─────────────────────────────────────────────────────

def test_symbol_normalization_identity_crypto():
    assert ec.normalize_symbol("BTC-USD") == ("BTC", "BTC-USD")


def test_symbol_normalization_aliases():
    assert ec.normalize_symbol("WTI-USD") == ("CL", "CL-USD")
    assert ec.normalize_symbol("XAG-USD") == ("SILVER", "SILVER-USD")
    assert ec.normalize_symbol("XCU-USD") == ("COPPER", "COPPER-USD")
    assert ec.normalize_symbol("TECH100m-USD") == ("USTECH100", "USTECH100-USD")


def test_symbol_normalization_session_suffix_stripped():
    assert ec.normalize_symbol("AMD_24_5-USD") == ("AMD", "AMD-USD")
    assert ec.normalize_symbol("TSLA_24_5-USD") == ("TSLA", "TSLA-USD")


def test_symbol_normalization_xau_not_aliased_to_xaut():
    # Live probe 2026-10-04: Extended lists BOTH XAU-USD (physical gold) and
    # XAUT-USD (token) — aliasing collided them on one output symbol.
    assert ec.normalize_symbol("XAU-USD") == ("XAU", "XAU-USD")
    assert ec.normalize_symbol("XAUT-USD") == ("XAUT", "XAUT-USD")


# ── dedup ────────────────────────────────────────────────────────────────────

def test_dedup_prefers_identity_named_market():
    amd = _entry("AMD-USD", openInterest="9000.0")
    amd245 = _entry("AMD_24_5-USD", openInterest="100.0")
    records, skipped = ec.parse_markets_payload(
        {"status": "OK", "data": [amd245, amd]}, TS)
    assert len(records) == 1 and skipped == 1
    assert records[0]["raw_symbol"] == "AMD-USD"


def test_dedup_session_variant_kept_when_no_identity():
    (rec,), skipped = ec.parse_markets_payload(
        {"status": "OK", "data": [_entry("AMD_24_5-USD")]}, TS)
    assert skipped == 0
    assert rec["symbol"] == "AMD-USD" and rec["raw_symbol"] == "AMD_24_5-USD"


# ── basis join ───────────────────────────────────────────────────────────────

def test_parse_sodex_marks():
    payload = {"data": [{"symbol": "BTC-USD", "markPrice": "100.0",
                         "fundingRate": "0.0001"},
                        {"symbol": "BROKEN"},
                        {"symbol": "ETH-USD", "markPrice": "bad"}]}
    assert ec.parse_sodex_marks(payload) == {"BTC": 100.0}


def test_basis_math_positive_extended_rich():
    rec = {"base": "BTC", "mark": 100.5, "sodex_mark": None, "basis_bps": None}
    n = ec.join_basis([rec], {"BTC": 100.0})
    assert n == 1
    assert rec["sodex_mark"] == 100.0
    assert rec["basis_bps"] == 50.0


def test_basis_skips_missing_or_zero_sodex():
    rec = {"base": "BTC", "mark": 100.5, "sodex_mark": None, "basis_bps": None}
    assert ec.join_basis([rec], {}) == 0
    assert ec.join_basis([rec], {"BTC": 0.0}) == 0
    assert rec["basis_bps"] is None


# ── append ───────────────────────────────────────────────────────────────────

def test_append_records_one_line_per_symbol(tmp_path):
    out = tmp_path / "extended_snapshots.jsonl"
    recs = [{"ts": TS, "venue": "extended", "symbol": "BTC-USD"},
            {"ts": TS, "venue": "extended", "symbol": "ETH-USD"}]
    assert ec.append_records(out, recs) == 2
    lines = out.read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["symbol"] == "BTC-USD"


# ── run_once: failure semantics (network fully mocked) ──────────────────────

def test_http_error_no_write_exit_nonzero(tmp_path, monkeypatch, capsys):
    out = tmp_path / "extended_snapshots.jsonl"

    async def boom(client, url):
        raise RuntimeError("http 503")

    monkeypatch.setattr(ec, "_fetch_json", boom)
    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(ec.run_once(out_path=out, with_sodex=False))
    assert excinfo.value.code == 1
    assert not out.exists()  # fail-closed: nothing written
    line = capsys.readouterr().out.strip().split("\n")[0]
    summary = json.loads(line)
    assert summary["ok"] is False and summary["written"] == 0


def test_bad_payload_no_write_exit_nonzero(tmp_path, monkeypatch):
    out = tmp_path / "extended_snapshots.jsonl"

    async def bad(client, url):
        return {"status": "OK", "data": []}

    monkeypatch.setattr(ec, "_fetch_json", bad)
    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(ec.run_once(out_path=out, with_sodex=False))
    assert excinfo.value.code == 1
    assert not out.exists()


def test_sodex_failure_degrades_never_blocks(tmp_path, monkeypatch):
    out = tmp_path / "extended_snapshots.jsonl"

    async def fake(client, url):
        if "extended" in url:
            return {"status": "OK", "data": [_entry("BTC-USD")]}
        raise RuntimeError("sodex down")

    monkeypatch.setattr(ec, "_fetch_json", fake)
    summary = asyncio.run(ec.run_once(out_path=out, with_sodex=True))
    assert summary["ok"] is True and summary["written"] == 1
    assert summary["basis_overlap"] == 0
    assert "sodex_error" in summary
    rec = json.loads(out.read_text().strip())
    assert rec["basis_bps"] is None and rec["sodex_mark"] is None


def test_run_once_happy_path_writes_and_joins(tmp_path, monkeypatch):
    out = tmp_path / "extended_snapshots.jsonl"

    async def fake(client, url):
        if "extended" in url:
            return {"status": "OK", "data": [_entry("BTC-USD", markPrice="100.5"),
                                             _entry("AMD_24_5-USD")]}
        return {"data": [{"symbol": "BTC-USD", "markPrice": "100.0"}]}

    monkeypatch.setattr(ec, "_fetch_json", fake)
    summary = asyncio.run(ec.run_once(out_path=out, with_sodex=True))
    assert summary["ok"] is True and summary["written"] == 2
    assert summary["basis_overlap"] == 1
    lines = out.read_text().strip().split("\n")
    btc = json.loads(next(l for l in lines if "BTC" in l))
    assert btc["basis_bps"] == 50.0
