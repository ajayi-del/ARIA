"""Equity-events pins: seed integrity (all 5 earnings symbols), JSONL
override load + merge, one-bad-line tolerance, stale-abstain (>10d past,
no newer entry = dark), template rebalance rows unconfirmed, kill switch."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk_calendar import equity_events as ee  # noqa: E402

D = 86400.0
T0 = 1_800_000_000.0


def _row(**kw):
    r = {"symbol": "TESTX", "event_type": "earnings",
         "ts_utc": T0 + 3 * D, "confirmed": True, "note": "t"}
    r.update(kw)
    return r


# ── Seed integrity ────────────────────────────────────────────────────────────

def test_seed_all_five_symbols_have_earnings():
    syms = {e.symbol for e in ee.EARNINGS_EVENTS
            if e.event_type == ee.EARNINGS_EVENT}
    assert {"NVDA", "AMD", "COIN", "MSTR", "HOOD"} <= syms
    for e in ee.EARNINGS_EVENTS:
        assert e.ts_utc > 0 and e.confirmed and e.source and e.note


def test_rebalance_templates_unconfirmed_and_marked():
    assert len(ee.REBALANCE_EVENTS) >= 2
    for e in ee.REBALANCE_EVENTS:
        assert e.event_type in (ee.INDEX_ADD, ee.INDEX_DELETE)
        assert e.confirmed is False
        assert "TEMPLATE" in e.note
        assert e.index_name


# ── Override rows: load + merge ───────────────────────────────────────────────

def test_override_rows_load_and_query():
    rows = [_row()]
    evs = ee.upcoming_events("TESTX", T0, 10, override_rows=rows)
    assert len(evs) == 1 and evs[0].ts_utc == T0 + 3 * D
    assert ee.days_to_event("TESTX", "earnings", T0, override_rows=rows) == 3.0


def test_override_newer_row_supersedes_stale_seed():
    # Same key (TESTX, earnings): a past row and a fresh future row —
    # the fresh row rescues the key from the dark state.
    rows = [_row(ts_utc=T0 - 20 * D), _row(ts_utc=T0 + 2 * D)]
    evs = ee.upcoming_events("TESTX", T0, 10, override_rows=rows)
    assert len(evs) == 1 and evs[0].ts_utc == T0 + 2 * D


def test_days_to_event_none_when_unknown():
    assert ee.days_to_event("NOSUCH", "earnings", T0) is None
    assert ee.days_to_event("TESTX", "earnings", T0,
                            override_rows=[_row(ts_utc=T0 - D)]) is None


def test_latest_event_returns_effective_row():
    rows = [_row(ts_utc=T0 - 2 * D), _row(ts_utc=T0 + 4 * D, note="newer")]
    ev = ee.latest_event("TESTX", "earnings", T0, override_rows=rows)
    assert ev is not None and ev.ts_utc == T0 + 4 * D and ev.note == "newer"


def test_iso_ts_accepted():
    evs, errs = ee.load_override_rows([_row(ts_utc="2026-11-19T21:00:00Z")])
    assert errs == [] and len(evs) == 1
    from datetime import datetime, timezone
    expect = datetime(2026, 11, 19, 21, 0, tzinfo=timezone.utc).timestamp()
    assert evs[0].ts_utc == expect


# ── One-bad-line tolerance ────────────────────────────────────────────────────

def test_one_bad_line_tolerance_rows():
    rows = [_row(), "not a dict", {"garbage": True},
            _row(event_type="not_a_type"), _row(ts_utc="junk"),
            _row(symbol="TESTY", ts_utc=T0 + 5 * D)]
    evs, errs = ee.load_override_rows(rows)
    assert len(evs) == 2 and len(errs) == 4
    assert {e.symbol for e in evs} == {"TESTX", "TESTY"}


def test_jsonl_file_roundtrip_and_bad_line(tmp_path):
    p = tmp_path / "earnings_calendar.jsonl"
    p.write_text(
        '{"symbol": "TESTX", "event_type": "earnings", '
        '"ts_utc": %d, "confirmed": true}\n'
        '{"malformed json\n'
        '\n'
        '# comment line\n'
        '{"symbol": "TESTY", "event_type": "index_add", '
        '"effective_ts_utc": %d, "index_name": "SP500"}\n'
        % (int(T0 + 3 * D), int(T0 + 8 * D)))
    evs, errs = ee.read_jsonl_overrides(str(p))
    assert len(evs) == 2 and len(errs) == 1
    assert evs[1].event_type == "index_add" and evs[1].index_name == "SP500"


def test_jsonl_missing_file_is_empty():
    assert ee.read_jsonl_overrides("/nonexistent/path.jsonl") == ([], [])


# ── Stale abstain ─────────────────────────────────────────────────────────────

def test_stale_event_goes_dark():
    rows = [_row(ts_utc=T0 - 11 * D)]      # >10d past, no newer entry
    assert ee.upcoming_events("TESTX", T0, 30, override_rows=rows) == []
    assert ee.days_to_event("TESTX", "earnings", T0,
                            override_rows=rows) is None
    assert ee.latest_event("TESTX", "earnings", T0,
                           override_rows=rows) is None


def test_recent_past_event_not_stale():
    rows = [_row(ts_utc=T0 - 5 * D)]       # 5d past — still live
    ev = ee.latest_event("TESTX", "earnings", T0, override_rows=rows)
    assert ev is not None and ev.ts_utc == T0 - 5 * D


# ── Kill switch ───────────────────────────────────────────────────────────────

def test_kill_switch(monkeypatch):
    rows = [_row()]
    monkeypatch.setenv("EQUITY_EVENTS_CALENDAR_ENABLED", "false")
    assert ee.upcoming_events("TESTX", T0, 10, override_rows=rows) == []
    assert ee.days_to_event("TESTX", "earnings", T0,
                            override_rows=rows) is None
    assert ee.latest_event("TESTX", "earnings", T0,
                           override_rows=rows) is None
    monkeypatch.setenv("EQUITY_EVENTS_CALENDAR_ENABLED", "true")
    assert len(ee.upcoming_events("TESTX", T0, 10, override_rows=rows)) == 1


# ── Fail-closed inputs ────────────────────────────────────────────────────────

def test_fail_closed_garbage():
    assert ee.upcoming_events(None, T0, 10) == []
    assert ee.upcoming_events("TESTX", "junk", 10) == []
    assert ee.upcoming_events("TESTX", T0, -1) == []
    assert ee.days_to_event("TESTX", "earnings", None) is None
    assert ee.latest_event("TESTX", "bogus_type", T0) is None
