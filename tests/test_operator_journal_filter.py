"""Operator-class journal filter pins (Governor directive 2026-09-26:
"reset every draw down from my operator trades today... every part bad
trades touch should be cleaned").

The 2026-09-26 manual session left 41 adopted-position synthetic orphan
closes (orphan_close:true, net -$55.25) in the day-file. Journals are
permanent (rule #14), so the cleanup is a READ-PATH filter:
is_operator_record + JOURNAL_OPERATOR_FILTER_ENABLED, bound at
get_closed() and at restore_from_journal (the boot beliefs rebuild —
agent_winrates / personality stats / global streak). Tier-1 migrated
engine entries carry close_migrated_from and are NOT operator class.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.performance import PerformanceTracker  # noqa: E402
from memory.trade_journal import (  # noqa: E402
    TradeJournal,
    is_operator_record,
    operator_filter_enabled,
)


def _orphan(pnl=-1.31, sym="BTC-USD", ms=1790000000000):
    # Shape written by record_cross_day_close tier 2 (trade_journal.py:472).
    return {"entry_id": f"orphan-{sym}-{ms}", "symbol": sym,
            "outcome": "loss" if pnl < 0 else "win",
            "pnl_usd": pnl, "pnl_net_usd": pnl, "closed_at_ms": ms,
            "timestamp_ms": ms - 60_000, "approved": True,
            "personality": "SCOUT", "orphan_close": True}


def _engine(pnl=0.42, sym="ETH-USD", ms=1790000060000, **extra):
    rec = {"entry_id": f"e-{sym}-{ms}", "symbol": sym,
           "outcome": "win" if pnl >= 0 else "loss",
           "pnl_usd": pnl, "pnl_net_usd": pnl, "closed_at_ms": ms,
           "timestamp_ms": ms - 60_000, "approved": True,
           "personality": "FLOW"}
    rec.update(extra)
    return rec


class TestOperatorPredicate:
    def test_orphan_close_is_operator(self):
        assert is_operator_record(_orphan()) is True

    def test_plain_engine_close_is_not(self):
        assert is_operator_record(_engine()) is False

    def test_migrated_tier1_is_not_operator(self):
        # Tier-1 migrated copies are real engine trades carried across a
        # restart — the LINK 2026-09-24 close class. Excluded from the cohort.
        assert is_operator_record(
            _engine(close_migrated_from="2026-09-24")) is False

    def test_orphan_close_false_is_not(self):
        assert is_operator_record(_engine(orphan_close=False)) is False

    def test_kill_switch_default_on(self, monkeypatch):
        monkeypatch.delenv("JOURNAL_OPERATOR_FILTER_ENABLED", raising=False)
        assert operator_filter_enabled() is True

    def test_kill_switch_off(self, monkeypatch):
        monkeypatch.setenv("JOURNAL_OPERATOR_FILTER_ENABLED", "false")
        assert operator_filter_enabled() is False


class TestGetClosedFilter:
    def test_operator_filtered_by_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JOURNAL_OPERATOR_FILTER_ENABLED", raising=False)
        j = TradeJournal(str(tmp_path))
        j.entries = [_orphan(), _engine()]
        closed = j.get_closed()
        assert len(closed) == 1
        assert closed[0]["entry_id"].startswith("e-")

    def test_kill_switch_restores_raw_read(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JOURNAL_OPERATOR_FILTER_ENABLED", "false")
        j = TradeJournal(str(tmp_path))
        j.entries = [_orphan(), _engine()]
        assert len(j.get_closed()) == 2

    def test_migrated_close_survives_filter(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JOURNAL_OPERATOR_FILTER_ENABLED", raising=False)
        j = TradeJournal(str(tmp_path))
        j.entries = [_orphan(), _engine(close_migrated_from="2026-09-24")]
        closed = j.get_closed()
        assert len(closed) == 1
        assert closed[0].get("close_migrated_from") == "2026-09-24"


class TestRestoreFromJournal:
    def _write(self, log_dir, name, records):
        (log_dir / name).write_text(json.dumps(records))

    def test_operator_closes_excluded_from_beliefs(self, tmp_path):
        # The reset case: a day-file holding the Governor's manual-session
        # orphan closes plus real engine closes. The rebuild must attribute
        # ONLY the engine records — personality stats and pnl clean.
        orphan = _orphan(pnl=-2.5)
        real = _engine(pnl=1.0)
        self._write(tmp_path, "trade_journal_2026-09-26.json",
                    [orphan, real])

        pt = PerformanceTracker()
        pt.restore_from_journal(str(tmp_path))

        assert pt.get_personality_stats("SCOUT") is None  # orphan-only bucket
        stats = pt.get_personality_stats("FLOW")
        assert stats is not None
        assert stats.total_trades == 1
        assert stats.total_pnl_usd == pytest.approx(1.0)

    def test_operator_exclusion_wins_over_dedup(self, tmp_path):
        # Same orphan record re-contained in a later rolling day-file must be
        # skipped as operator, never deduped-and-kept.
        orphan = _orphan(pnl=-2.5)
        self._write(tmp_path, "trade_journal_2026-09-26.json", [orphan])
        self._write(tmp_path, "trade_journal_2026-09-27.json", [orphan])

        pt = PerformanceTracker()
        pt.restore_from_journal(str(tmp_path))
        assert pt.get_personality_stats("SCOUT") is None
