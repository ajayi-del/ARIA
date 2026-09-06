"""Pins for the phantom-open journal repair (2026-09-06).

journal.log_decision writes the row at INTENT (main.py, before the bracket
task). No-fill exits (l4 defers, allocator veto, bracket_failed,
bracket_exception) used to leave outcome=None forever — an approved intent
reading as an OPEN entry (AKE 941cfc48 / 1df4be1c). Forward fix stamps
outcome="rejected" at the kill site; the two residual rows are filtered at
the read path by is_phantom_open_entry (journals permanent, rule #14).
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.trade_journal import (  # noqa: E402
    TradeJournal, is_phantom_open_entry, phantom_filter_enabled,
)

NOW_MS = 1_800_000_000_000
TODAY = datetime.fromtimestamp(NOW_MS / 1000, timezone.utc).date()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()
TODAY_S = TODAY.isoformat()

PHANTOM_ID_1 = "941cfc48-ccfe-4c60-8071-30986df026fe"
PHANTOM_ID_2 = "1df4be1c-cc8a-4b1e-9bce-849de06dd798"


def _journal(tmp_path, monkeypatch):
    from core.clock import exchange_clock
    monkeypatch.setattr(exchange_clock, "now_ms", lambda: NOW_MS)
    monkeypatch.setattr(exchange_clock, "now_date_str", lambda: TODAY_S)
    return TradeJournal(log_dir=str(tmp_path))


def _open_entry(symbol="AKE-USD", entry_id="e-real", ts=None):
    return {
        "entry_id": entry_id,
        "timestamp_ms": ts if ts is not None else NOW_MS - 3_600_000,
        "symbol": symbol,
        "direction": "long",
        "approved": True,
        "entry_price": 0.18,
        "position_size": 500.0,
        "initial_margin": 18.0,
        "leverage": 5,
        "outcome": None,
        "pnl_usd": None,
        "pnl_net_usd": None,
        "closed_at_ms": None,
    }


def _write_day_file(tmp_path, date_s, entries):
    (tmp_path / f"trade_journal_{date_s}.json").write_text(json.dumps(entries))


# ── the predicate ────────────────────────────────────────────────────────────

def test_phantom_ids_are_the_two_ake_rows():
    assert is_phantom_open_entry({"entry_id": PHANTOM_ID_1})
    assert is_phantom_open_entry({"entry_id": PHANTOM_ID_2})


def test_predicate_rejects_everything_else():
    assert not is_phantom_open_entry({"entry_id": "e-real"})
    assert not is_phantom_open_entry({"entry_id": None})
    assert not is_phantom_open_entry({})


# ── find_open_entry_in_files ─────────────────────────────────────────────────

def test_scan_skips_phantom_and_finds_real_open(tmp_path, monkeypatch):
    j = _journal(tmp_path, monkeypatch)
    # Phantom newest in the file — a naive newest-first scan would hit it.
    _write_day_file(tmp_path, YESTERDAY,
                    [_open_entry(entry_id="e-real"),
                     _open_entry(entry_id=PHANTOM_ID_1)])
    rec, day = j.find_open_entry_in_files("AKE-USD")
    assert rec is not None and rec["entry_id"] == "e-real"
    assert day == YESTERDAY


def test_scan_returns_none_when_only_phantom_matches(tmp_path, monkeypatch):
    j = _journal(tmp_path, monkeypatch)
    _write_day_file(tmp_path, YESTERDAY, [_open_entry(entry_id=PHANTOM_ID_2)])
    rec, day = j.find_open_entry_in_files("AKE-USD")
    assert rec is None and day is None


def test_scan_legacy_when_filter_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("JOURNAL_PHANTOM_FILTER_ENABLED", "false")
    assert not phantom_filter_enabled()
    j = _journal(tmp_path, monkeypatch)
    _write_day_file(tmp_path, YESTERDAY, [_open_entry(entry_id=PHANTOM_ID_1)])
    rec, _ = j.find_open_entry_in_files("AKE-USD")
    assert rec is not None and rec["entry_id"] == PHANTOM_ID_1


# ── get_open ─────────────────────────────────────────────────────────────────

def test_get_open_filters_phantom_rows(tmp_path, monkeypatch):
    j = _journal(tmp_path, monkeypatch)
    j.entries = [_open_entry(entry_id="e-real"),
                 _open_entry(entry_id=PHANTOM_ID_1)]
    ids = [e["entry_id"] for e in j.get_open()]
    assert ids == ["e-real"]


def test_get_open_legacy_when_filter_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("JOURNAL_PHANTOM_FILTER_ENABLED", "false")
    j = _journal(tmp_path, monkeypatch)
    j.entries = [_open_entry(entry_id=PHANTOM_ID_1)]
    assert len(j.get_open()) == 1


# ── the "rejected" outcome (forward fix semantics) ───────────────────────────

def test_rejected_outcome_excluded_from_open_and_closed(tmp_path, monkeypatch):
    j = _journal(tmp_path, monkeypatch)
    entry = _open_entry(entry_id="e-rej")
    j.entries = [entry]
    j.update_outcome(entry_id="e-rej", outcome="rejected",
                     pnl_usd=0.0, closed_at_ms=NOW_MS,
                     exit_reason="l4_spread_gate_deferred")
    rec = j.entries[0]
    assert rec["outcome"] == "rejected"
    assert rec["closed_at_ms"] == NOW_MS
    assert rec["exit_reason"] == "l4_spread_gate_deferred"
    assert rec["pnl_r"] == 0.0          # 0 pnl over real margin, not None
    assert rec["hold_time_ms"] == NOW_MS - entry["timestamp_ms"]
    assert j.get_open() == []
    assert j.get_closed() == []         # rejected is not a traded close
