"""
tests/test_variational_ledger.py — pins for execution/variational_ledger.py.

The Governor trades manually on the Variational venue; ARIA mirrors those
positions (venue-asymmetry axiom: "Variational = manual ledger"). The bot
NEVER manages them — these pins guard the bookkeeping contract: append-only
JSONL journal + atomic snapshot, one-bad-line reads, call-time kill switch,
validation that never raises.

Every test pins log_dir to tmp_path — the real logs/ tree is never touched.
"""

import json
import os

import pytest

from execution.variational_ledger import (
    VariationalLedger,
    variational_ledger_enabled,
    open_position,
    update_position,
    close_position,
    get_positions,
    snapshot_now,
    JOURNAL_NAME,
    SNAPSHOT_NAME,
)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A ledger rooted in tmp_path with the kill switch explicitly ON."""
    monkeypatch.delenv("VARIATIONAL_LEDGER_ENABLED", raising=False)
    return VariationalLedger(log_dir=str(tmp_path))


def _journal_lines(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ── round-trip ──────────────────────────────────────────────────────────────

def test_open_get_roundtrip_full_schema(ledger):
    assert ledger.open_position("BTC-USD", "long", 0.5, 67000.0,
                                leverage=10.0, note="governor scalp",
                                ts=1000.0) is True
    positions = ledger.get_positions()
    assert set(positions) == {"BTC-USD"}
    pos = positions["BTC-USD"]
    assert pos == {
        "symbol": "BTC-USD",
        "side": "long",
        "qty": 0.5,
        "entry_price": 67000.0,
        "leverage": 10.0,
        "venue": "variational",
        "source": "manual",
        "opened_ts": 1000.0,
        "updated_ts": 1000.0,
        "note": "governor scalp",
    }


def test_open_without_optionals_stores_none(ledger):
    assert ledger.open_position("ETH-USD", "short", 2.0, 3500.0) is True
    pos = ledger.get_positions()["ETH-USD"]
    assert pos["leverage"] is None
    assert pos["note"] is None


def test_update_position_mutates_allowed_fields(ledger):
    ledger.open_position("BTC-USD", "long", 0.5, 67000.0, ts=1000.0)
    assert ledger.update_position("BTC-USD", qty=0.75, leverage=20.0,
                                  note="added", ts=1100.0) is True
    pos = ledger.get_positions()["BTC-USD"]
    assert pos["qty"] == 0.75
    assert pos["leverage"] == 20.0
    assert pos["note"] == "added"
    assert pos["opened_ts"] == 1000.0   # opened_ts immutable
    assert pos["updated_ts"] == 1100.0


def test_close_position_removes(ledger):
    ledger.open_position("BTC-USD", "long", 0.5, 67000.0)
    assert ledger.close_position("BTC-USD") is True
    assert ledger.get_positions() == {}


def test_open_duplicate_symbol_refused(ledger):
    assert ledger.open_position("BTC-USD", "long", 0.5, 67000.0) is True
    assert ledger.open_position("BTC-USD", "short", 1.0, 68000.0) is False
    pos = ledger.get_positions()["BTC-USD"]
    assert pos["side"] == "long" and pos["qty"] == 0.5


def test_close_missing_symbol_false(ledger):
    assert ledger.close_position("NOPE-USD") is False


def test_update_missing_symbol_false(ledger):
    assert ledger.update_position("NOPE-USD", qty=1.0) is False


# ── snapshot + journal replay ───────────────────────────────────────────────

def test_snapshot_written_and_reread(ledger, tmp_path):
    ledger.open_position("SOL-USD", "long", 10.0, 150.0, ts=1000.0)
    snap = tmp_path / SNAPSHOT_NAME
    assert snap.exists()
    payload = json.loads(snap.read_text())
    assert payload["positions"]["SOL-USD"]["qty"] == 10.0
    # A second ledger instance (no shared memory) reads the snapshot.
    other = VariationalLedger(log_dir=str(tmp_path))
    assert other.get_positions()["SOL-USD"]["entry_price"] == 150.0


def test_journal_replay_fallback_when_snapshot_deleted(ledger, tmp_path):
    ledger.open_position("BTC-USD", "long", 0.5, 67000.0, ts=1000.0)
    ledger.update_position("BTC-USD", qty=0.9, ts=1100.0)
    ledger.open_position("ETH-USD", "short", 2.0, 3500.0, ts=1200.0)
    ledger.close_position("ETH-USD", ts=1300.0)
    os.remove(tmp_path / SNAPSHOT_NAME)
    positions = ledger.get_positions()
    assert set(positions) == {"BTC-USD"}
    assert positions["BTC-USD"]["qty"] == 0.9
    assert positions["BTC-USD"]["updated_ts"] == 1100.0


def test_corrupt_snapshot_falls_back_to_journal(ledger, tmp_path):
    ledger.open_position("BTC-USD", "long", 0.5, 67000.0, ts=1000.0)
    (tmp_path / SNAPSHOT_NAME).write_text("{not json")
    positions = ledger.get_positions()
    assert positions["BTC-USD"]["qty"] == 0.5


def test_one_bad_line_journal_tolerance(ledger, tmp_path):
    ledger.open_position("BTC-USD", "long", 0.5, 67000.0, ts=1000.0)
    ledger.open_position("ETH-USD", "short", 2.0, 3500.0, ts=1100.0)
    journal = tmp_path / JOURNAL_NAME
    with open(journal, "a", encoding="utf-8") as f:
        f.write("THIS IS NOT JSON\n")
        f.write(json.dumps({"op": "close", "symbol": "ETH-USD",
                            "ts": 1200.0}) + "\n")
    os.remove(tmp_path / SNAPSHOT_NAME)  # force replay
    positions = ledger.get_positions()
    assert set(positions) == {"BTC-USD"}  # bad line died alone; close applied


def test_note_op_replays(ledger, tmp_path):
    """op 'note' (commentary-only event) merges on replay like an update."""
    journal = tmp_path / JOURNAL_NAME
    with open(journal, "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 1000.0, "op": "open", "symbol": "BTC-USD",
                            "side": "long", "qty": 0.5,
                            "entry_price": 67000.0}) + "\n")
        f.write(json.dumps({"ts": 1100.0, "op": "note", "symbol": "BTC-USD",
                            "note": "watching"}) + "\n")
    positions = ledger.get_positions()
    assert positions["BTC-USD"]["note"] == "watching"
    assert positions["BTC-USD"]["updated_ts"] == 1100.0


# ── kill switch ─────────────────────────────────────────────────────────────

def test_kill_switch_off_writes_noop(ledger, tmp_path, monkeypatch):
    monkeypatch.setenv("VARIATIONAL_LEDGER_ENABLED", "false")
    assert variational_ledger_enabled() is False
    assert ledger.open_position("BTC-USD", "long", 0.5, 67000.0) is False
    assert ledger.update_position("BTC-USD", qty=1.0) is False
    assert ledger.close_position("BTC-USD") is False
    assert ledger.snapshot_now() is False
    assert not (tmp_path / JOURNAL_NAME).exists()
    assert not (tmp_path / SNAPSHOT_NAME).exists()


def test_kill_switch_off_reads_empty(ledger, monkeypatch):
    ledger.open_position("BTC-USD", "long", 0.5, 67000.0)
    assert ledger.get_positions() != {}
    monkeypatch.setenv("VARIATIONAL_LEDGER_ENABLED", "false")
    assert ledger.get_positions() == {}


# ── validation (False, never raise) ─────────────────────────────────────────

@pytest.mark.parametrize("bad_qty", [0, -1, 0.0, -0.5, float("nan"),
                                     float("inf"), "abc", None])
def test_invalid_qty_rejected(ledger, bad_qty):
    assert ledger.open_position("BTC-USD", "long", bad_qty, 67000.0) is False
    assert ledger.get_positions() == {}


@pytest.mark.parametrize("bad_entry", [0, -1, -67000.0, float("nan"),
                                       float("inf"), None])
def test_invalid_entry_rejected(ledger, bad_entry):
    assert ledger.open_position("BTC-USD", "long", 0.5, bad_entry) is False
    assert ledger.get_positions() == {}


@pytest.mark.parametrize("bad_side", ["flat", "BUY", "", None, 1])
def test_invalid_side_rejected(ledger, bad_side):
    assert ledger.open_position("BTC-USD", bad_side, 0.5, 67000.0) is False
    assert ledger.get_positions() == {}


@pytest.mark.parametrize("bad_lev", [0, -5, float("nan"), float("inf")])
def test_invalid_leverage_rejected(ledger, bad_lev):
    assert ledger.open_position("BTC-USD", "long", 0.5, 67000.0,
                                leverage=bad_lev) is False
    ledger.open_position("BTC-USD", "long", 0.5, 67000.0)
    assert ledger.update_position("BTC-USD", leverage=bad_lev) is False


def test_update_unknown_field_rejected(ledger):
    ledger.open_position("BTC-USD", "long", 0.5, 67000.0)
    assert ledger.update_position("BTC-USD", stop_price=1.0) is False
    assert ledger.update_position("BTC-USD") is False  # no fields at all
    assert ledger.get_positions()["BTC-USD"]["qty"] == 0.5


def test_invalid_update_value_rejected_position_untouched(ledger):
    ledger.open_position("BTC-USD", "long", 0.5, 67000.0)
    assert ledger.update_position("BTC-USD", qty=-1) is False
    assert ledger.update_position("BTC-USD", entry_price=0) is False
    assert ledger.update_position("BTC-USD", note=123) is False
    pos = ledger.get_positions()["BTC-USD"]
    assert pos["qty"] == 0.5 and pos["entry_price"] == 67000.0


# ── module-level default instance ───────────────────────────────────────────

def test_module_level_functions_delegate(tmp_path, monkeypatch):
    """Default-instance functions honor the env log-dir override at call
    time (testability seam) and the kill switch."""
    monkeypatch.delenv("VARIATIONAL_LEDGER_ENABLED", raising=False)
    monkeypatch.setenv("VARIATIONAL_LEDGER_LOG_DIR", str(tmp_path))
    assert open_position("BTC-USD", "long", 0.5, 67000.0, ts=1000.0) is True
    assert (tmp_path / JOURNAL_NAME).exists()
    assert get_positions()["BTC-USD"]["qty"] == 0.5
    assert snapshot_now() is True
    assert update_position("BTC-USD", note="x") is True
    assert close_position("BTC-USD") is True
    assert get_positions() == {}
    monkeypatch.setenv("VARIATIONAL_LEDGER_ENABLED", "false")
    assert open_position("ETH-USD", "long", 1.0, 3500.0) is False
    assert get_positions() == {}
