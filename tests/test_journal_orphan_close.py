"""Orphan-close journal repair: cross-day migration, synthetic fallback, dedup."""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.trade_journal import TradeJournal  # noqa: E402

NOW_MS = 1_800_000_000_000
TODAY = datetime.fromtimestamp(NOW_MS / 1000, timezone.utc).date()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()
TODAY_S = TODAY.isoformat()


def _journal(tmp_path, monkeypatch):
    # Pin the exchange clock so file naming/date math is deterministic.
    from core.clock import exchange_clock
    monkeypatch.setattr(exchange_clock, "now_ms", lambda: NOW_MS)
    monkeypatch.setattr(exchange_clock, "now_date_str", lambda: TODAY_S)
    return TradeJournal(log_dir=str(tmp_path))


def _write_day_file(tmp_path, date_s, entries):
    (tmp_path / f"trade_journal_{date_s}.json").write_text(json.dumps(entries))


def _open_entry(symbol="BTC-USD", ts=None, entry_id="e-1", personality="FLOW"):
    return {
        "entry_id": entry_id,
        "timestamp_ms": ts if ts is not None else NOW_MS - 86_400_000,
        "symbol": symbol,
        "direction": "long",
        "approved": True,
        "personality": personality,
        "strategy_tag": "cascade_aftermath",
        "entry_price": 100.0,
        "position_size": 1.0,
        "initial_margin": 10.0,
        "leverage": 5,
        "outcome": None,
        "pnl_usd": None,
        "pnl_net_usd": None,
        "closed_at_ms": None,
    }


# ── cross-day migration (the dominant production case) ──────────────────────

def test_migrated_close_carries_source_context(tmp_path, monkeypatch):
    j = _journal(tmp_path, monkeypatch)
    _write_day_file(tmp_path, YESTERDAY, [_open_entry()])
    eid = j.record_cross_day_close(
        symbol="BTC-USD", direction="long", outcome="loss",
        pnl_usd=-0.63, pnl_net_usd=-0.6268, closed_at_ms=NOW_MS,
        exit_reason="software_stop",
    )
    assert eid == "e-1"
    rec = j.entries[-1]
    assert rec["outcome"] == "loss"
    assert rec["close_migrated_from"] == YESTERDAY
    assert rec["personality"] == "FLOW"          # real personality survives
    assert rec["initial_margin"] == 10.0
    assert abs(rec["pnl_r"] - (-0.06268)) < 1e-9
    assert rec["hold_time_ms"] == 86_400_000
    assert "orphan_close" not in rec


def test_migrated_close_is_visible_to_get_closed(tmp_path, monkeypatch):
    j = _journal(tmp_path, monkeypatch)
    _write_day_file(tmp_path, YESTERDAY, [_open_entry()])
    j.record_cross_day_close(
        symbol="BTC-USD", direction="long", outcome="win",
        pnl_usd=0.5, closed_at_ms=NOW_MS,
    )
    closed = j.get_closed()
    assert len(closed) == 1 and closed[0]["symbol"] == "BTC-USD"


def test_source_file_never_mutated(tmp_path, monkeypatch):
    j = _journal(tmp_path, monkeypatch)
    _write_day_file(tmp_path, YESTERDAY, [_open_entry()])
    j.record_cross_day_close(
        symbol="BTC-USD", direction="long", outcome="loss",
        pnl_usd=-0.5, closed_at_ms=NOW_MS,
    )
    disk = json.loads((tmp_path / f"trade_journal_{YESTERDAY}.json").read_text())
    assert disk[0]["outcome"] is None            # rule #14: journals permanent


def test_newest_open_entry_wins(tmp_path, monkeypatch):
    j = _journal(tmp_path, monkeypatch)
    older = _open_entry(entry_id="e-old", ts=NOW_MS - 200_000)
    newer = _open_entry(entry_id="e-new", ts=NOW_MS - 100_000)
    _write_day_file(tmp_path, YESTERDAY, [older, newer])
    eid = j.record_cross_day_close(
        symbol="BTC-USD", direction="long", outcome="win",
        pnl_usd=0.1, closed_at_ms=NOW_MS,
    )
    assert eid == "e-new"


def test_rejected_and_closed_entries_skipped(tmp_path, monkeypatch):
    j = _journal(tmp_path, monkeypatch)
    rejected = {**_open_entry(entry_id="e-rej"), "approved": False}
    closed = {**_open_entry(entry_id="e-cls"), "outcome": "win",
              "pnl_usd": 1.0, "closed_at_ms": NOW_MS - 300_000}
    _write_day_file(tmp_path, YESTERDAY, [rejected, closed])
    src, d = j.find_open_entry_in_files("BTC-USD")
    assert src is None and d is None


def test_corrupt_day_file_tolerated(tmp_path, monkeypatch):
    j = _journal(tmp_path, monkeypatch)
    (tmp_path / f"trade_journal_{YESTERDAY}.json").write_text("{not json")
    src, d = j.find_open_entry_in_files("BTC-USD")
    assert (src, d) == (None, None)


# ── same-day scan (2026-08-30 watchdog proposal: today was excluded) ─────────

def test_today_file_included_in_scan(tmp_path, monkeypatch):
    # A position entered by a PREVIOUS process the same UTC day lives in
    # today's file; after a restart it must still match its real entry.
    j = _journal(tmp_path, monkeypatch)
    _write_day_file(tmp_path, TODAY_S,
                    [_open_entry(ts=NOW_MS - 3_600_000, entry_id="e-today")])
    src, d = j.find_open_entry_in_files("BTC-USD")
    assert src is not None and src["entry_id"] == "e-today"
    assert d == TODAY_S


def test_today_closed_entry_still_not_matched(tmp_path, monkeypatch):
    j = _journal(tmp_path, monkeypatch)
    closed = {**_open_entry(entry_id="e-done"), "outcome": "win",
              "pnl_usd": 1.0, "closed_at_ms": NOW_MS - 300_000}
    _write_day_file(tmp_path, TODAY_S, [closed])
    src, d = j.find_open_entry_in_files("BTC-USD")
    assert (src, d) == (None, None)


def test_today_wins_over_yesterday(tmp_path, monkeypatch):
    # Scan order is newest-first: today's open entry outranks yesterday's.
    j = _journal(tmp_path, monkeypatch)
    _write_day_file(tmp_path, YESTERDAY, [_open_entry(entry_id="e-old")])
    _write_day_file(tmp_path, TODAY_S,
                    [_open_entry(ts=NOW_MS - 3_600_000, entry_id="e-new")])
    src, d = j.find_open_entry_in_files("BTC-USD")
    assert src["entry_id"] == "e-new" and d == TODAY_S


# ── synthetic orphan (no entry anywhere) ─────────────────────────────────────

def test_synthetic_orphan_record(tmp_path, monkeypatch):
    j = _journal(tmp_path, monkeypatch)
    eid = j.record_cross_day_close(
        symbol="SOL-USD", direction="short", outcome="loss",
        pnl_usd=-0.63, pnl_net_usd=-0.6268, closed_at_ms=NOW_MS,
        exit_reason="stop_dust_purged", entry_price=75.0, position_size=0.5,
        initial_margin=7.5, leverage=5, opened_at_ms=NOW_MS - 3_600_000,
        strategy_tag="momentum_cont",
    )
    assert eid == f"orphan-SOL-USD-{NOW_MS}"
    rec = j.entries[-1]
    assert rec["orphan_close"] is True
    assert rec["approved"] is True
    assert rec["direction"] == "short"
    assert abs(rec["pnl_r"] - (-0.6268 / 7.5)) < 1e-9
    assert rec["hold_time_ms"] == 3_600_000


# ── dedup ────────────────────────────────────────────────────────────────────

def _closed_rec(symbol, closed_ms, pnl):
    return {"entry_id": f"c-{symbol}-{closed_ms}", "symbol": symbol,
            "outcome": "loss" if pnl < 0 else "win",
            "pnl_usd": pnl, "pnl_net_usd": pnl, "closed_at_ms": closed_ms,
            "timestamp_ms": closed_ms - 60_000, "approved": True}


def test_dedup_same_close_not_rebooked(tmp_path, monkeypatch):
    j = _journal(tmp_path, monkeypatch)
    j.entries.append(_closed_rec("ETH-USD", NOW_MS - 5_000, -0.7952))
    eid = j.record_cross_day_close(
        symbol="ETH-USD", direction="long", outcome="loss",
        pnl_usd=-0.79, pnl_net_usd=-0.7952, closed_at_ms=NOW_MS,
    )
    assert eid is None
    assert len(j.entries) == 1


def test_dedup_window_boundary(tmp_path, monkeypatch):
    j = _journal(tmp_path, monkeypatch)
    # 3 minutes old — outside the 120s window → a NEW trade, must record
    j.entries.append(_closed_rec("ETH-USD", NOW_MS - 180_000, -0.7952))
    eid = j.record_cross_day_close(
        symbol="ETH-USD", direction="long", outcome="loss",
        pnl_usd=-0.79, pnl_net_usd=-0.7952, closed_at_ms=NOW_MS,
    )
    assert eid is not None
    assert len(j.entries) == 2


def test_dedup_pnl_mismatch_records(tmp_path, monkeypatch):
    j = _journal(tmp_path, monkeypatch)
    # Same window but wildly different pnl → not the same trade
    j.entries.append(_closed_rec("ETH-USD", NOW_MS - 5_000, -5.0))
    eid = j.record_cross_day_close(
        symbol="ETH-USD", direction="long", outcome="loss",
        pnl_usd=-0.79, pnl_net_usd=-0.7952, closed_at_ms=NOW_MS,
    )
    assert eid is not None


def test_dedup_different_symbol_records(tmp_path, monkeypatch):
    j = _journal(tmp_path, monkeypatch)
    j.entries.append(_closed_rec("BTC-USD", NOW_MS - 5_000, -0.7952))
    eid = j.record_cross_day_close(
        symbol="ETH-USD", direction="long", outcome="loss",
        pnl_usd=-0.79, pnl_net_usd=-0.7952, closed_at_ms=NOW_MS,
    )
    assert eid is not None
