import json
import os

import pytest

from funding.history import FundingHistory

HOUR_MS = 3_600_000
BASE = 1_757_498_400_000  # hour-aligned UTC timestamp


@pytest.fixture
def hist(tmp_path):
    return FundingHistory(storage_path=str(tmp_path / "funding_history.json"))


def test_same_hour_replaces_latest_wins_timestamp_advances(hist):
    hist.add("BTC-USD", 0.0001, "live", now_ms=BASE + 5 * 60_000)
    hist.add("BTC-USD", 0.0002, "derived", now_ms=BASE + 35 * 60_000)

    records = hist._history["BTC-USD"]
    assert len(records) == 1
    assert records[0].rate == 0.0002
    assert records[0].source == "derived"
    assert records[0].timestamp_ms == BASE + 35 * 60_000


def test_different_hours_append(hist):
    hist.add("BTC-USD", 0.0001, "live", now_ms=BASE)
    hist.add("BTC-USD", 0.0002, "live", now_ms=BASE + HOUR_MS)

    records = hist._history["BTC-USD"]
    assert len(records) == 2
    assert [r.rate for r in records] == [0.0001, 0.0002]


def test_hour_boundary_replace_only_within_same_hour(hist):
    hist.add("BTC-USD", 0.0001, "live", now_ms=BASE + HOUR_MS - 1)
    hist.add("BTC-USD", 0.0002, "live", now_ms=BASE + HOUR_MS)
    assert len(hist._history["BTC-USD"]) == 2

    hist.add("BTC-USD", 0.0003, "live", now_ms=BASE + HOUR_MS + 1)
    records = hist._history["BTC-USD"]
    assert len(records) == 2
    assert records[-1].rate == 0.0003
    assert records[-1].timestamp_ms == BASE + HOUR_MS + 1


def test_kill_switch_off_restores_append_always(hist, monkeypatch):
    monkeypatch.setenv("FUNDING_HOURLY_BUCKET_ENABLED", "false")
    hist.add("BTC-USD", 0.0001, "live", now_ms=BASE + 5 * 60_000)
    hist.add("BTC-USD", 0.0002, "live", now_ms=BASE + 35 * 60_000)

    records = hist._history["BTC-USD"]
    assert len(records) == 2
    assert [r.rate for r in records] == [0.0001, 0.0002]
    assert records[0].timestamp_ms == BASE + 5 * 60_000


def test_prune_168_across_hours(hist):
    for i in range(200):
        hist.add("BTC-USD", float(i), "live", now_ms=BASE + i * HOUR_MS)

    records = hist._history["BTC-USD"]
    assert len(records) == 168
    assert records[0].rate == 32.0
    assert records[-1].rate == 199.0


def test_prune_168_legacy_mode(hist, monkeypatch):
    monkeypatch.setenv("FUNDING_HOURLY_BUCKET_ENABLED", "false")
    for i in range(200):
        hist.add("BTC-USD", float(i), "live", now_ms=BASE + i * 60_000)

    records = hist._history["BTC-USD"]
    assert len(records) == 168
    assert records[-1].rate == 199.0


def test_save_load_roundtrip(tmp_path):
    path = str(tmp_path / "funding_history.json")
    h1 = FundingHistory(storage_path=path)
    h1.add("BTC-USD", 0.0001, "live", now_ms=BASE)
    h1.add("BTC-USD", 0.0002, "derived", now_ms=BASE + 30 * 60_000)
    h1.add("BTC-USD", 0.0003, "live", now_ms=BASE + HOUR_MS)
    h1.add("ETH-USD", -0.0004, "live", now_ms=BASE)

    h2 = FundingHistory(storage_path=path)
    h2.load()

    assert set(h2._history.keys()) == {"BTC-USD", "ETH-USD"}
    btc = h2._history["BTC-USD"]
    assert len(btc) == 2
    assert [(r.rate, r.timestamp_ms, r.source) for r in btc] == [
        (0.0002, BASE + 30 * 60_000, "derived"),
        (0.0003, BASE + HOUR_MS, "live"),
    ]
    assert h2._history["ETH-USD"][0].rate == -0.0004

    with open(path) as f:
        raw = json.load(f)
    assert len(raw["BTC-USD"]) == 2


def test_get_rates_and_avg_semantics(hist):
    hist.add("BTC-USD", 0.1, "live", now_ms=BASE)
    hist.add("BTC-USD", 0.2, "live", now_ms=BASE + HOUR_MS)
    hist.add("BTC-USD", 0.6, "live", now_ms=BASE + 2 * HOUR_MS)

    assert hist.get_rates("BTC-USD", 2) == [0.2, 0.6]
    assert hist.get_rates("BTC-USD") == [0.1, 0.2, 0.6]
    assert hist.avg("BTC-USD", 24) == pytest.approx(0.3)
    assert hist.avg("MISSING-USD") == 0.0

    # intra-hour replace updates the values consumers see
    hist.add("BTC-USD", 0.9, "live", now_ms=BASE + 2 * HOUR_MS + 10 * 60_000)
    assert hist.get_rates("BTC-USD", 1) == [0.9]
    assert hist.avg("BTC-USD", 24) == pytest.approx(0.4)
