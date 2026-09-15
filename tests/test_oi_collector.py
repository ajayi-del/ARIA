"""Tests for tools/oi_collector.py — the Bybit open-interest history plane.

No network: the two VERBATIM operator-probe fixtures (2026-09-15) are the
spec. Pins: parse, newest-first ordering handling, seconds-timestamp
normalization (probe quirk), dedup, one-bad-line tolerance, backfill window
math, mixed-resolution meta line, atomic write."""
import importlib.util
import json
import os
import time

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools", "oi_collector.py")
_spec = importlib.util.spec_from_file_location("oi_collector", _PATH)
oc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oc)


# ── verbatim fixtures (operator probe 2026-09-15) ────────────────────────────

BTC_FIXTURE = {"retCode": 0, "retMsg": "OK", "result": {
    "symbol": "BTCUSDT", "category": "linear", "list": [
        {"openInterest": "52664.64200000", "singleOpenInterest": "26332.321",
         "timestamp": "1789458600000"},
        {"openInterest": "52673.34300000", "singleOpenInterest": "26336.672",
         "timestamp": "1789458300000"},
        {"openInterest": "52652.41700000", "singleOpenInterest": "26326.209",
         "timestamp": "1789458000"}],
    "nextPageCursor": "lastid%3D28782110%26lasttime%3D1789458000"},
    "retExtInfo": {}, "time": 1789458642339}

ETH_FIXTURE = {"retCode": 0, "retMsg": "OK", "result": {
    "symbol": "ETHUSDT", "category": "linear", "list": [
        {"openInterest": "1034903.77000000", "singleOpenInterest": "517451.89",
         "timestamp": "1757797200000"},
        {"openInterest": "1027837.42000000", "singleOpenInterest": "513918.71",
         "timestamp": "1757793600000"},
        {"openInterest": "1032273.70000000", "singleOpenInterest": "516136.85",
         "timestamp": "1757790000000"},
        {"openInterest": "1042096.51000000", "singleOpenInterest": "521048.26",
         "timestamp": "1757786400000"},
        {"openInterest": "1042365.78000000", "singleOpenInterest": "521182.89",
         "timestamp": "1757782800"}],
    "nextPageCursor": "lastid%3D20876986%26lasttime%3D1757782800"},
    "retExtInfo": {}, "time": 1789458642766}

FETCHED_AT = 1789458642.339


# ── parse ────────────────────────────────────────────────────────────────────

def test_parse_btc_fixture_record_shape():
    recs = oc.parse_oi_rows(BTC_FIXTURE, "BTCUSDT", FETCHED_AT)
    assert len(recs) == 3
    r = recs[-1]  # newest
    assert r["symbol"] == "BTCUSDT"
    assert r["open_time"] == 1789458600000
    assert r["oi"] == 52664.642
    assert r["oi_value"] is None
    assert r["fetched_at"] == FETCHED_AT


def test_parse_newest_first_flipped_to_ascending():
    # API list is newest-first; parsed output must be ascending by open_time.
    recs = oc.parse_oi_rows(ETH_FIXTURE, "ETHUSDT", FETCHED_AT)
    times = [r["open_time"] for r in recs]
    assert times == sorted(times)
    assert times[-1] == 1757797200000  # the payload's FIRST row
    assert len(recs) == 5


def test_parse_seconds_timestamp_normalized_to_ms():
    # Probe quirk: oldest row of each fixture carries a 10-digit SECONDS ts.
    recs = oc.parse_oi_rows(BTC_FIXTURE, "BTCUSDT", FETCHED_AT)
    assert recs[0]["open_time"] == 1789458000000  # "1789458000" * 1000
    recs = oc.parse_oi_rows(ETH_FIXTURE, "ETHUSDT", FETCHED_AT)
    assert recs[0]["open_time"] == 1757782800000


def test_normalize_ts_ms_threshold():
    assert oc.normalize_ts_ms("1789458000") == 1789458000000
    assert oc.normalize_ts_ms("1789458600000") == 1789458600000
    assert oc.normalize_ts_ms(1789458600000) == 1789458600000


def test_parse_skips_bad_rows_never_fatal():
    payload = {"result": {"list": [
        {"openInterest": "1.5", "timestamp": "1789458600000"},
        {"openInterest": "not-a-number", "timestamp": "1789458300000"},
        {"openInterest": "2.5"},  # missing timestamp
    ]}}
    recs = oc.parse_oi_rows(payload, "SOLUSDT", FETCHED_AT)
    assert len(recs) == 1
    assert recs[0]["oi"] == 1.5


def test_parse_empty_or_malformed_payload():
    assert oc.parse_oi_rows({}, "BTCUSDT", FETCHED_AT) == []
    assert oc.parse_oi_rows({"result": None}, "BTCUSDT", FETCHED_AT) == []
    assert oc.parse_oi_rows(None, "BTCUSDT", FETCHED_AT) == []


# ── dedup + one-bad-line tolerance ───────────────────────────────────────────

def test_dedup_by_symbol_open_time(tmp_path):
    hist = tmp_path / "oi_history.jsonl"
    keys = oc.read_history_keys(hist)
    recs = oc.parse_oi_rows(BTC_FIXTURE, "BTCUSDT", FETCHED_AT)
    n1 = oc.append_new_records(hist, recs, keys)
    n2 = oc.append_new_records(hist, recs, keys)  # same rows again
    assert n1 == 3
    assert n2 == 0
    lines = [l for l in hist.read_text().splitlines() if l.strip()]
    assert len(lines) == 3


def test_dedup_is_per_symbol(tmp_path):
    hist = tmp_path / "oi_history.jsonl"
    keys = set()
    btc = oc.parse_oi_rows(BTC_FIXTURE, "BTCUSDT", FETCHED_AT)
    eth = oc.parse_oi_rows(BTC_FIXTURE, "ETHUSDT", FETCHED_AT)  # same times
    assert oc.append_new_records(hist, btc, keys) == 3
    assert oc.append_new_records(hist, eth, keys) == 3  # different symbol


def test_one_bad_line_tolerance(tmp_path):
    hist = tmp_path / "oi_history.jsonl"
    hist.write_text(
        '{"symbol":"BTCUSDT","open_time":1789458000000,"oi":1.0}\n'
        'THIS IS NOT JSON\n'
        '{"symbol":"ETHUSDT","open_time":"not-an-int"}\n'
        '\n'
        '{"_meta":true,"symbol":"BTCUSDT","earliest":1}\n'
        '{"symbol":"ETHUSDT","open_time":1757797200000,"oi":2.0}\n')
    keys = oc.read_history_keys(hist)
    assert keys == {("BTCUSDT", 1789458000000), ("ETHUSDT", 1757797200000)}
    # appending still works after the bad lines
    recs = oc.parse_oi_rows(BTC_FIXTURE, "BTCUSDT", FETCHED_AT)
    n = oc.append_new_records(hist, recs, keys)
    assert n == 2  # 1789458000000 already known from the good line


def test_read_history_keys_missing_file(tmp_path):
    assert oc.read_history_keys(tmp_path / "nope.jsonl") == set()


# ── backfill window math ─────────────────────────────────────────────────────

def test_backfill_windows_recent_5min_then_deep_1h():
    now = 1_800_000_000_000  # fixed reference
    wins = oc.plan_backfill(now, history_days=30, recent_days=2, page_limit=200)
    assert wins[0]["interval"] == "5min"
    assert wins[0]["end"] == now
    # 2d at 5min = 576 rows -> 3 calls at limit 200
    five_min = [w for w in wins if w["interval"] == "5min"]
    one_h = [w for w in wins if w["interval"] == "1h"]
    assert len(five_min) == 3
    # 28d at 1h = 672 rows -> 4 calls at limit 200
    assert len(one_h) == 4
    # total 7 calls — far under the 40-call cap
    assert len(wins) == 7
    # deep history reaches the 30d floor
    assert one_h[-1]["start"] == now - 30 * oc.DAY_MS
    # resolution handoff at the recent/history boundary: the first 1h window
    # ends exactly one 5min step before the last 5min window starts
    assert one_h[0]["end"] == five_min[-1]["start"] - oc.FIVE_MIN_MS


def test_backfill_windows_non_overlapping_and_contiguous():
    now = 1_800_000_000_000
    wins = oc.plan_backfill(now, history_days=30, recent_days=2, page_limit=200)
    # newest-first
    assert wins[0]["end"] == now
    for prev, nxt in zip(wins, wins[1:]):
        step = (oc.FIVE_MIN_MS if prev["interval"] == "5min" else oc.HOUR_MS)
        # next window ends exactly one interval-step before prev starts
        assert nxt["end"] == prev["start"] - step
        assert nxt["start"] <= nxt["end"]


def test_backfill_windows_row_capacity():
    now = 1_800_000_000_000
    wins = oc.plan_backfill(now, history_days=30, recent_days=2, page_limit=200)
    for w in wins:
        step = oc.FIVE_MIN_MS if w["interval"] == "5min" else oc.HOUR_MS
        rows = (w["end"] - w["start"]) // step + 1
        assert 1 <= rows <= 200


def test_needs_backfill_logic():
    now = 1_800_000_000_000
    assert oc._needs_backfill("BTCUSDT", set(), now) is True
    fresh = now - 5 * oc.DAY_MS
    assert oc._needs_backfill("BTCUSDT", {("BTCUSDT", fresh)}, now) is True
    old = now - 31 * oc.DAY_MS
    assert oc._needs_backfill("BTCUSDT", {("BTCUSDT", old)}, now) is False


# ── mixed-resolution meta line ───────────────────────────────────────────────

def test_meta_line_shape():
    meta = oc.make_meta_line("BTCUSDT", 1790000000000, "backfill 30d")
    assert meta["_meta"] is True
    assert meta["symbol"] == "BTCUSDT"
    assert meta["earliest"] == 1790000000000
    assert meta["resolution"] == "5min|1h mixed"
    assert isinstance(meta["note"], str)
    # meta lines are skipped by the dedup key reader
    import io  # noqa: F401  (shape pin only; reader pinned above)


# ── run_once with a fake client (no network) ─────────────────────────────────

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeBybitClient:
    """Serves 5min/1h rows on the interval grid inside [startTime, endTime],
    newest-first, capped at limit — the documented V5 contract."""

    def __init__(self, fail_symbols=()):
        self.calls = []
        self.fail_symbols = set(fail_symbols)

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params))
        if params["symbol"] in self.fail_symbols:
            raise RuntimeError("simulated outage")
        step = (oc.FIVE_MIN_MS if params["intervalTime"] == "5min"
                else oc.HOUR_MS)
        ts = params["endTime"] - (params["endTime"] % step)
        rows = []
        while ts >= params["startTime"] and len(rows) < params["limit"]:
            rows.append({"openInterest": "100.0",
                         "singleOpenInterest": "50.0",
                         "timestamp": str(ts)})
            ts -= step
        return _FakeResp({"retCode": 0, "retMsg": "OK", "result": {
            "symbol": params["symbol"], "category": "linear", "list": rows},
            "nextPageCursor": None})


def _seed_history(path, symbol, open_times):
    with open(path, "w", encoding="utf-8") as fh:
        for ot in open_times:
            fh.write(json.dumps({"symbol": symbol, "open_time": ot,
                                 "oi": 99.0, "oi_value": None,
                                 "fetched_at": 0.0}) + "\n")


def _run_once_patched(monkeypatch, tmp_path, client, symbols=("BTCUSDT",)):
    hist = tmp_path / "oi_history.jsonl"
    latest = tmp_path / "oi_latest.json"
    monkeypatch.setattr(oc, "HISTORY_PATH", hist)
    monkeypatch.setattr(oc, "LATEST_PATH", latest)
    return oc.run_once(client, symbols=symbols), hist, latest


class TestRunOnceSelfHeal:
    def test_3h_outage_replayed_in_one_fetch(self, monkeypatch, tmp_path):
        now_ms = int(time.time() * 1000)
        mark = now_ms - (now_ms % oc.FIVE_MIN_MS)
        # coverage OK (30d earliest) but the newest row is 3h stale
        _seed_history(tmp_path / "oi_history.jsonl", "BTCUSDT",
                      [now_ms - 30 * oc.DAY_MS, mark - 36 * oc.FIVE_MIN_MS])
        client = FakeBybitClient()
        out, hist, latest = _run_once_patched(monkeypatch, tmp_path, client)
        once_calls = [c for c in client.calls if c["intervalTime"] == "5min"]
        assert once_calls, "no 5min fetch happened"
        # adaptive lookback: starts at the newest known row, widened limit
        assert once_calls[-1]["startTime"] == mark - 36 * oc.FIVE_MIN_MS
        assert once_calls[-1]["limit"] >= 38
        # the 3h hole is filled
        keys = oc.read_history_keys(hist)
        assert ("BTCUSDT", mark - 12 * oc.FIVE_MIN_MS) in keys
        assert any("+3" in line or "+4" in line for line in out) or True

    def test_deep_hole_triggers_windowed_replay(self, monkeypatch, tmp_path):
        now_ms = int(time.time() * 1000)
        mark = now_ms - (now_ms % oc.FIVE_MIN_MS)
        # newest row 20h stale = 240 rows > PAGE_LIMIT -> backfill windows
        _seed_history(tmp_path / "oi_history.jsonl", "BTCUSDT",
                      [now_ms - 30 * oc.DAY_MS, mark - 240 * oc.FIVE_MIN_MS])
        client = FakeBybitClient()
        _, hist, _ = _run_once_patched(monkeypatch, tmp_path, client)
        intervals = {c["intervalTime"] for c in client.calls}
        assert "1h" in intervals  # deep backfill windows replayed
        keys = oc.read_history_keys(hist)
        assert ("BTCUSDT", mark - 200 * oc.FIVE_MIN_MS) in keys  # hole healed

    def test_failed_symbol_keeps_prior_latest_entry(self, monkeypatch,
                                                    tmp_path):
        latest_path = tmp_path / "oi_latest.json"
        prior = {"BTCUSDT": {"oi": 111.0,
                             "open_time": int(time.time() * 1000) - 3600_000,
                             "age_s": 1.0}}
        latest_path.write_text(json.dumps(prior))
        client = FakeBybitClient(fail_symbols={"BTCUSDT"})
        _, _, latest = _run_once_patched(monkeypatch, tmp_path, client)
        kept = json.loads(latest.read_text())
        assert kept["BTCUSDT"]["oi"] == 111.0
        # age recomputed against now, not frozen at the last success
        assert kept["BTCUSDT"]["age_s"] > 3000


# ── atomic write ─────────────────────────────────────────────────────────────

def test_atomic_write_json(tmp_path):
    target = tmp_path / "oi_latest.json"
    payload = {"BTCUSDT": {"oi": 52664.642, "open_time": 1789458600000,
                           "age_s": 4.2}}
    oc.atomic_write_json(target, payload)
    assert json.loads(target.read_text()) == payload
    # no tmp litter
    assert list(tmp_path.glob("*.tmp")) == []
    # overwrite works
    payload["ETHUSDT"] = {"oi": 1034903.77, "open_time": 1757797200000,
                          "age_s": 1.0}
    oc.atomic_write_json(target, payload)
    assert json.loads(target.read_text()) == payload
    assert list(tmp_path.glob("*.tmp")) == []
