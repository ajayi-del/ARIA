"""Tests for tools/account_ratio_collector.py — the Bybit global long/short
account-ratio plane.

No network: the VERBATIM operator-probe fixture (2026-09-15) is the spec.
Pins: parse, newest-first flip to ascending, ls_ratio math, sell_ratio 0 /
missing rows omitted and counted, string/float/ts<1e12 normalization, dedup
by (symbol, open_time) rerun-idempotence, one-bad-line tolerance, backfill
page-merge dedup of overlap."""
import importlib.util
import json
import os

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools", "account_ratio_collector.py")
_spec = importlib.util.spec_from_file_location("account_ratio_collector", _PATH)
arc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arc)


# ── verbatim fixture (operator probe 2026-09-15) ─────────────────────────────

ETH_FIXTURE = {"retCode": 0, "retMsg": "OK", "result": {"list": [
    {"symbol": "ETHUSDT", "buyRatio": "0.6784", "sellRatio": "0.3216",
     "timestamp": "1789460400000"},
    {"symbol": "ETHUSDT", "buyRatio": "0.6783", "sellRatio": "0.3217",
     "timestamp": "1789460100000"},
    {"symbol": "ETHUSDT", "buyRatio": "0.6772", "sellRatio": "0.3228",
     "timestamp": "1789459800000"}],
    "nextPageCursor": "lastid%3D0%26lasttime%3D1789459800"},
    "retExtInfo": {}, "time": 1789460661467}

FETCHED_AT = 1789460661.467


# ── parse ────────────────────────────────────────────────────────────────────

def test_fixture_parses_to_3_rows():
    recs, skipped = arc.parse_ratio_rows(ETH_FIXTURE, "ETHUSDT", FETCHED_AT)
    assert len(recs) == 3
    assert skipped == 0


def test_fixture_flipped_to_ascending():
    # API list is newest-first; parsed output must be ascending by open_time.
    recs, _ = arc.parse_ratio_rows(ETH_FIXTURE, "ETHUSDT", FETCHED_AT)
    times = [r["open_time"] for r in recs]
    assert times == sorted(times)
    assert times[0] == 1789459800000   # the payload's LAST row
    assert times[-1] == 1789460400000  # the payload's FIRST row


def test_ls_ratio_math():
    recs, _ = arc.parse_ratio_rows(ETH_FIXTURE, "ETHUSDT", FETCHED_AT)
    newest = recs[-1]
    assert newest["buy_ratio"] == pytest.approx(0.6784)
    assert newest["sell_ratio"] == pytest.approx(0.3216)
    assert newest["ls_ratio"] == pytest.approx(0.6784 / 0.3216)


def test_record_shape():
    recs, _ = arc.parse_ratio_rows(ETH_FIXTURE, "ETHUSDT", FETCHED_AT)
    r = recs[-1]
    assert r["symbol"] == "ETHUSDT"
    assert r["open_time"] == 1789460400000
    assert isinstance(r["buy_ratio"], float)
    assert isinstance(r["sell_ratio"], float)
    assert isinstance(r["ls_ratio"], float)
    assert r["fetched_at"] == FETCHED_AT


def test_sell_ratio_zero_row_omitted_and_counted():
    payload = {"result": {"list": [
        {"symbol": "BTCUSDT", "buyRatio": "0.5", "sellRatio": "0",
         "timestamp": "1789460400000"},
        {"symbol": "BTCUSDT", "buyRatio": "0.6", "sellRatio": "0.4",
         "timestamp": "1789460100000"},
    ]}}
    recs, skipped = arc.parse_ratio_rows(payload, "BTCUSDT", FETCHED_AT)
    assert len(recs) == 1
    assert recs[0]["ls_ratio"] == pytest.approx(1.5)
    assert skipped == 1


def test_sell_ratio_missing_row_omitted_and_counted():
    payload = {"result": {"list": [
        {"symbol": "BTCUSDT", "buyRatio": "0.5",
         "timestamp": "1789460400000"},  # sellRatio missing
        {"symbol": "BTCUSDT", "buyRatio": "0.6", "sellRatio": "0.4",
         "timestamp": "1789460100000"},
    ]}}
    recs, skipped = arc.parse_ratio_rows(payload, "BTCUSDT", FETCHED_AT)
    assert len(recs) == 1
    assert skipped == 1


def test_string_fields_normalized_to_float():
    payload = {"result": {"list": [
        {"symbol": "SOLUSDT", "buyRatio": "0.55", "sellRatio": "0.45",
         "timestamp": "1789460400000"},
    ]}}
    recs, _ = arc.parse_ratio_rows(payload, "SOLUSDT", FETCHED_AT)
    assert recs[0]["buy_ratio"] == 0.55
    assert recs[0]["sell_ratio"] == 0.45
    assert type(recs[0]["buy_ratio"]) is float
    assert type(recs[0]["open_time"]) is int


def test_normalize_ts_ms_threshold():
    assert arc.normalize_ts_ms("1789459800") == 1789459800000   # seconds -> ms
    assert arc.normalize_ts_ms("1789460400000") == 1789460400000
    assert arc.normalize_ts_ms(1789460400000) == 1789460400000


def test_parse_bad_rows_skipped_never_fatal():
    payload = {"result": {"list": [
        {"buyRatio": "0.5", "sellRatio": "0.5", "timestamp": "1789460400000"},
        {"buyRatio": "junk", "sellRatio": "0.5", "timestamp": "1789460100000"},
        {"buyRatio": "0.6", "sellRatio": "0.4"},  # missing timestamp
    ]}}
    recs, skipped = arc.parse_ratio_rows(payload, "BTCUSDT", FETCHED_AT)
    assert len(recs) == 1
    assert skipped == 2


def test_parse_empty_or_malformed_payload():
    assert arc.parse_ratio_rows({}, "BTCUSDT", FETCHED_AT) == ([], 0)
    assert arc.parse_ratio_rows({"result": None}, "BTCUSDT", FETCHED_AT) == ([], 0)
    assert arc.parse_ratio_rows(None, "BTCUSDT", FETCHED_AT) == ([], 0)


# ── dedup + one-bad-line tolerance ───────────────────────────────────────────

def test_dedup_by_symbol_open_time_rerun_idempotent(tmp_path):
    hist = tmp_path / "account_ratio.jsonl"
    keys = arc.read_history_keys(hist)
    recs, _ = arc.parse_ratio_rows(ETH_FIXTURE, "ETHUSDT", FETCHED_AT)
    n1 = arc.append_new_records(hist, recs, keys)
    n2 = arc.append_new_records(hist, recs, keys)  # same rows again
    assert n1 == 3
    assert n2 == 0
    lines = [l for l in hist.read_text().splitlines() if l.strip()]
    assert len(lines) == 3


def test_dedup_is_per_symbol(tmp_path):
    hist = tmp_path / "account_ratio.jsonl"
    keys = set()
    eth, _ = arc.parse_ratio_rows(ETH_FIXTURE, "ETHUSDT", FETCHED_AT)
    btc, _ = arc.parse_ratio_rows(ETH_FIXTURE, "BTCUSDT", FETCHED_AT)
    assert arc.append_new_records(hist, eth, keys) == 3
    assert arc.append_new_records(hist, btc, keys) == 3  # different symbol


def test_one_bad_line_read_skips_garbage_keeps_good(tmp_path):
    hist = tmp_path / "account_ratio.jsonl"
    hist.write_text(
        '{"symbol":"BTCUSDT","open_time":1789459800000,"buy_ratio":0.5}\n'
        'THIS IS NOT JSON\n'
        '{"symbol":"ETHUSDT","open_time":"not-an-int"}\n'
        '\n'
        '{"_meta":true,"symbol":"BTCUSDT","earliest":1}\n'
        '{"symbol":"ETHUSDT","open_time":1789460400000,"buy_ratio":0.6}\n')
    keys = arc.read_history_keys(hist)
    assert keys == {("BTCUSDT", 1789459800000), ("ETHUSDT", 1789460400000)}
    # appending still works after the bad lines
    recs, _ = arc.parse_ratio_rows(ETH_FIXTURE, "ETHUSDT", FETCHED_AT)
    n = arc.append_new_records(hist, recs, keys)
    assert n == 2  # 1789460400000 already known from the good line


def test_read_history_keys_missing_file(tmp_path):
    assert arc.read_history_keys(tmp_path / "nope.jsonl") == set()


# ── backfill page-merge dedup ────────────────────────────────────────────────

def test_backfill_page_merge_dedups_overlap(tmp_path):
    """Two consecutive 1h pages sharing one boundary row merge to the union
    of distinct (symbol, open_time) keys — overlap is absorbed by dedup."""
    hist = tmp_path / "account_ratio.jsonl"
    keys = set()
    page_newer = {"result": {"list": [
        {"symbol": "ETHUSDT", "buyRatio": "0.60", "sellRatio": "0.40",
         "timestamp": "1789464000000"},
        {"symbol": "ETHUSDT", "buyRatio": "0.59", "sellRatio": "0.41",
         "timestamp": "1789460400000"},
    ]}}
    page_older = {"result": {"list": [
        {"symbol": "ETHUSDT", "buyRatio": "0.59", "sellRatio": "0.41",
         "timestamp": "1789460400000"},  # overlapping boundary row
        {"symbol": "ETHUSDT", "buyRatio": "0.58", "sellRatio": "0.42",
         "timestamp": "1789456800000"},
    ]}}
    recs1, _ = arc.parse_ratio_rows(page_newer, "ETHUSDT", FETCHED_AT)
    recs2, _ = arc.parse_ratio_rows(page_older, "ETHUSDT", FETCHED_AT)
    n1 = arc.append_new_records(hist, recs1, keys)
    n2 = arc.append_new_records(hist, recs2, keys)
    assert n1 == 2
    assert n2 == 1  # only the truly-new older row appends
    keys_final = arc.read_history_keys(hist)
    assert keys_final == {
        ("ETHUSDT", 1789464000000),
        ("ETHUSDT", 1789460400000),
        ("ETHUSDT", 1789456800000),
    }


# ── meta line ────────────────────────────────────────────────────────────────

def test_meta_line_shape_and_reader_skips_it(tmp_path):
    meta = arc.make_meta_line("BTCUSDT", 1789456800000, "backfill 7d at 1h")
    assert meta["_meta"] is True
    assert meta["symbol"] == "BTCUSDT"
    assert meta["earliest"] == 1789456800000
    assert meta["resolution"] == "1h"
    assert isinstance(meta["note"], str)
    hist = tmp_path / "account_ratio.jsonl"
    hist.write_text(json.dumps(meta) + "\n")
    assert arc.read_history_keys(hist) == set()  # meta never becomes a key


# ── run_once / run_backfill with a fake client (no network) ──────────────────

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeBybitClient:
    """Serves fixture-shaped rows newest-first inside (endTime, limit),
    stepping back one interval per row — the documented V5 contract."""

    def __init__(self, fail_symbols=()):
        self.calls = []
        self.fail_symbols = set(fail_symbols)

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params))
        if params["symbol"] in self.fail_symbols:
            raise RuntimeError("simulated outage")
        step = (arc.FIVE_MIN_MS if params["period"] == "5min"
                else arc.HOUR_MS)
        end = params.get("endTime", 1789460400000)
        ts = end - (end % step)
        rows = []
        for _ in range(params["limit"]):
            rows.append({"symbol": params["symbol"], "buyRatio": "0.55",
                         "sellRatio": "0.45", "timestamp": str(ts)})
            ts -= step
        return _FakeResp({"retCode": 0, "retMsg": "OK", "result": {
            "list": rows}, "nextPageCursor": "opaque"})


def test_run_once_appends_and_summarizes(monkeypatch, tmp_path):
    hist = tmp_path / "account_ratio.jsonl"
    monkeypatch.setattr(arc, "HISTORY_PATH", hist)
    monkeypatch.setattr(arc, "SLEEP_S", 0)  # no politeness sleep in tests
    client = FakeBybitClient()
    out = arc.run_once(client, symbols=("BTCUSDT", "ETHUSDT"))
    keys = arc.read_history_keys(hist)
    assert len(keys) == 4  # 2 symbols x limit=2
    assert all(c["period"] == "5min" and c["limit"] == 2
               for c in client.calls)
    assert any(line.startswith("summary once: +4 rows skipped=0")
               for line in out)


def test_run_once_failed_symbol_self_errors(monkeypatch, tmp_path):
    hist = tmp_path / "account_ratio.jsonl"
    monkeypatch.setattr(arc, "HISTORY_PATH", hist)
    monkeypatch.setattr(arc, "SLEEP_S", 0)
    client = FakeBybitClient(fail_symbols={"ETHUSDT"})
    out = arc.run_once(client, symbols=("BTCUSDT", "ETHUSDT"))
    # BTC succeeded, ETH self-errored, the run kept going
    keys = arc.read_history_keys(hist)
    assert len(keys) == 2
    assert all(sym == "BTCUSDT" for sym, _ in keys)
    assert any("ERROR" in line and "ETHUSDT" in line for line in out)


def test_run_backfill_pages_back_and_writes_meta(monkeypatch, tmp_path):
    hist = tmp_path / "account_ratio.jsonl"
    monkeypatch.setattr(arc, "HISTORY_PATH", hist)
    monkeypatch.setattr(arc, "SLEEP_S", 0)
    client = FakeBybitClient()
    out = arc.run_backfill(0.01, client, symbols=("BTCUSDT",))  # ~14.4h
    assert client.calls, "no backfill fetch happened"
    assert all(c["period"] == "1h" for c in client.calls)
    # endTime stepping: each page's endTime is strictly older than the last
    ends = [c["endTime"] for c in client.calls]
    assert ends == sorted(ends, reverse=True)
    # meta line landed and is not a data key
    text = hist.read_text()
    assert '"_meta":true' in text
    keys = arc.read_history_keys(hist)
    assert keys  # data rows present
    assert all(sym == "BTCUSDT" for sym, _ in keys)
    assert any(line.startswith("backfill BTCUSDT: calls=") for line in out)
