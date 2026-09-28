"""tests/test_pipeline_allocator.py — pins for the three-pipeline capital
allocator.

Every doctrine pinned: caps sum to 0.70 with the 0.30 reserve never
allocated, A available = 0.35 x equity - deployed, B's $25-35 margin band
($24 rejected / $25 / $35 accepted / $36 rejected), B's 50/day ceiling, C's
3/week ceiling, UTC day / ISO-week counter rolls, Kelly clamps into the
bands, bucket capacity binding last, persistence round-trip, and the kill
switch.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from intelligence.pipeline_allocator import (
    B_DAILY_TRADE_CEILING,
    B_MARGIN_MAX_USD,
    B_MARGIN_MIN_USD,
    C_WEEKLY_TRADE_CEILING,
    KELLY_BANDS,
    PIPELINE_CAPS,
    RESERVE_FRACTION,
    PipelineAllocator,
    clamp_kelly,
    pipeline_allocator_enabled,
)

TS_SAT = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc).timestamp()
TS_NEXT_DAY = TS_SAT + 86400.0
TS_NEXT_WEEK = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc).timestamp()


def _alloc(tmp_path, time_fn=None):
    return PipelineAllocator(time_fn=time_fn or (lambda: TS_SAT),
                             persist_path=str(tmp_path / "pa.json"))


# ── Doctrine constants ────────────────────────────────────────────────────────

class TestConstants:
    def test_caps_sum_070_reserve_030(self):
        assert sum(PIPELINE_CAPS.values()) == pytest.approx(0.70)
        assert RESERVE_FRACTION == pytest.approx(0.30)
        assert PIPELINE_CAPS == {"A": 0.35, "B": 0.20, "C": 0.15}

    def test_kelly_bands(self):
        assert KELLY_BANDS["A"] == (0.12, 0.24)
        assert KELLY_BANDS["B"] == (0.06, 0.10)
        assert KELLY_BANDS["C"] == (0.0, 0.05)

    def test_b_band_and_ceilings(self):
        assert (B_MARGIN_MIN_USD, B_MARGIN_MAX_USD) == (25.0, 35.0)
        assert B_DAILY_TRADE_CEILING == 50
        assert C_WEEKLY_TRADE_CEILING == 3


# ── allocate ──────────────────────────────────────────────────────────────────

class TestAllocate:
    def test_a_available_is_cap_minus_deployed(self, tmp_path):
        a = _alloc(tmp_path)
        assert a.allocate("A", 1000.0) == pytest.approx(350.0)
        snap = {"deployed_margin": {"A": 100.0}}
        assert a.allocate("A", 1000.0, snap) == pytest.approx(250.0)

    def test_all_pipeline_caps(self, tmp_path):
        a = _alloc(tmp_path)
        assert a.allocate("B", 1000.0) == pytest.approx(200.0)
        assert a.allocate("C", 1000.0) == pytest.approx(150.0)

    def test_reserve_never_allocated(self, tmp_path):
        a = _alloc(tmp_path)
        assert a.allocate("RESERVE", 1000.0) == 0.0
        assert a.allocate("reserve", 1000.0) == 0.0
        assert a.allocate("D", 1000.0) == 0.0
        assert a.allocate(None, 1000.0) == 0.0

    def test_internal_ledger_deployed(self, tmp_path):
        a = _alloc(tmp_path)
        a.record_trade("A", 120.0, ts=TS_SAT)
        assert a.allocate("A", 1000.0) == pytest.approx(230.0)
        a.release("A", 120.0)
        assert a.allocate("A", 1000.0) == pytest.approx(350.0)

    def test_snapshot_overrides_ledger(self, tmp_path):
        a = _alloc(tmp_path)
        a.record_trade("A", 120.0, ts=TS_SAT)
        assert a.allocate("A", 1000.0, {"A": 50.0}) == pytest.approx(300.0)

    def test_bad_equity_zero(self, tmp_path):
        a = _alloc(tmp_path)
        assert a.allocate("A", None) == 0.0
        assert a.allocate("A", -10.0) == 0.0

    def test_budget_dataclass(self, tmp_path):
        a = _alloc(tmp_path)
        a.record_trade("B", 25.0, ts=TS_SAT)
        b = a.budget("B", 1000.0)
        assert b.pipeline == "B"
        assert b.cap_fraction == 0.20
        assert b.available == pytest.approx(175.0)
        assert b.trades_today == 1
        assert b.trades_this_week == 1


# ── can_trade admission ───────────────────────────────────────────────────────

class TestCanTrade:
    def test_b_margin_band(self, tmp_path):
        a = _alloc(tmp_path)
        assert a.can_trade("B", 24.0, TS_SAT) == (False, "b_margin_below_band")
        assert a.can_trade("B", 25.0, TS_SAT) == (True, "ok")
        assert a.can_trade("B", 35.0, TS_SAT) == (True, "ok")
        assert a.can_trade("B", 36.0, TS_SAT) == (False, "b_margin_above_band")

    def test_a_and_c_have_no_margin_band(self, tmp_path):
        a = _alloc(tmp_path)
        assert a.can_trade("A", 5.0, TS_SAT) == (True, "ok")
        assert a.can_trade("C", 200.0, TS_SAT) == (True, "ok")

    def test_unknown_pipeline(self, tmp_path):
        a = _alloc(tmp_path)
        assert a.can_trade("RESERVE", 25.0, TS_SAT) == (False, "unknown_pipeline")

    def test_bad_margin(self, tmp_path):
        a = _alloc(tmp_path)
        assert a.can_trade("A", 0.0, TS_SAT) == (False, "bad_margin")
        assert a.can_trade("A", None, TS_SAT) == (False, "bad_margin")

    def test_b_daily_ceiling_51st_rejected(self, tmp_path):
        a = _alloc(tmp_path)
        for _ in range(50):
            a.record_trade("B", 25.0, ts=TS_SAT)
        assert a.can_trade("B", 25.0, TS_SAT) == (False, "b_daily_ceiling")

    def test_c_weekly_ceiling_4th_rejected(self, tmp_path):
        a = _alloc(tmp_path)
        for _ in range(3):
            a.record_trade("C", 10.0, ts=TS_SAT)
        assert a.can_trade("C", 10.0, TS_SAT) == (False, "c_weekly_ceiling")

    def test_bucket_capacity_binds_last(self, tmp_path):
        a = _alloc(tmp_path)
        # A margin $400 > 35% x $1000 = $350 available.
        assert a.can_trade("A", 400.0, TS_SAT, account_equity=1000.0) == (
            False, "bucket_capacity")
        assert a.can_trade("A", 350.0, TS_SAT, account_equity=1000.0) == (
            True, "ok")


# ── UTC boundary rolls ────────────────────────────────────────────────────────

class TestRolls:
    def test_day_roll_frees_b_ceiling(self, tmp_path):
        a = _alloc(tmp_path)
        for _ in range(50):
            a.record_trade("B", 25.0, ts=TS_SAT)
        assert a.can_trade("B", 25.0, TS_SAT) == (False, "b_daily_ceiling")
        assert a.can_trade("B", 25.0, TS_NEXT_DAY) == (True, "ok")
        b = a.budget("B", 1000.0)
        # The can_trade at TS_NEXT_DAY already rolled the day counter.
        assert b.trades_today == 0

    def test_week_roll_frees_c_ceiling(self, tmp_path):
        a = _alloc(tmp_path)
        for _ in range(3):
            a.record_trade("C", 10.0, ts=TS_SAT)
        assert a.can_trade("C", 10.0, TS_SAT) == (False, "c_weekly_ceiling")
        # Same ISO week next day: still blocked.
        assert a.can_trade("C", 10.0, TS_NEXT_DAY) == (False, "c_weekly_ceiling")
        # Next ISO week: free.
        assert a.can_trade("C", 10.0, TS_NEXT_WEEK) == (True, "ok")

    def test_counters_roll_on_read_via_budget(self, tmp_path):
        clock = {"t": TS_SAT}
        a = _alloc(tmp_path, time_fn=lambda: clock["t"])
        a.record_trade("B", 25.0)          # uses injected clock
        clock["t"] = TS_NEXT_DAY
        assert a.budget("B", 1000.0).trades_today == 0


# ── Kelly clamp ───────────────────────────────────────────────────────────────

class TestClampKelly:
    def test_clamp_into_bands(self):
        assert clamp_kelly("A", 0.30) == 0.24
        assert clamp_kelly("A", 0.05) == 0.12
        assert clamp_kelly("A", 0.18) == 0.18
        assert clamp_kelly("B", 0.20) == 0.10
        assert clamp_kelly("B", 0.01) == 0.06
        assert clamp_kelly("C", 0.50) == 0.05
        assert clamp_kelly("C", -0.30) == 0.0

    def test_unknown_pipeline_zero(self):
        assert clamp_kelly("RESERVE", 0.20) == 0.0
        assert clamp_kelly(None, 0.20) == 0.0
        assert clamp_kelly("A", None) == 0.0


# ── Persistence ───────────────────────────────────────────────────────────────

class TestPersistence:
    def test_round_trip(self, tmp_path):
        path = str(tmp_path / "pa.json")
        a1 = PipelineAllocator(time_fn=lambda: TS_SAT, persist_path=path)
        a1.record_trade("A", 100.0, ts=TS_SAT)
        a1.record_trade("A", 50.0, ts=TS_SAT)
        a1.record_trade("C", 10.0, ts=TS_SAT)

        a2 = PipelineAllocator(time_fn=lambda: TS_SAT, persist_path=path)
        assert a2.allocate("A", 1000.0) == pytest.approx(200.0)
        b = a2.budget("A", 1000.0)
        assert b.trades_today == 2
        assert b.trades_this_week == 2
        assert b.deployed == pytest.approx(150.0)
        assert a2.budget("C", 1000.0).trades_this_week == 1

    def test_jsonl_audit_written(self, tmp_path):
        path = str(tmp_path / "pa.json")
        a = PipelineAllocator(time_fn=lambda: TS_SAT, persist_path=path)
        a.record_trade("B", 30.0, ts=TS_SAT)
        lines = (tmp_path / "pa.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        import json as _json
        row = _json.loads(lines[0])
        assert row["pipeline"] == "B"
        assert row["margin_usd"] == 30.0

    def test_corrupt_state_loads_flat(self, tmp_path):
        p = tmp_path / "pa.json"
        p.write_text("{not json")
        a = PipelineAllocator(persist_path=str(p))
        assert a.allocate("A", 1000.0) == pytest.approx(350.0)


# ── Kill switch ───────────────────────────────────────────────────────────────

class TestKillSwitch:
    def test_disabled_refuses(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PIPELINE_ALLOCATOR_ENABLED", "false")
        assert pipeline_allocator_enabled() is False
        a = _alloc(tmp_path)
        assert a.allocate("A", 1000.0) == 0.0
        assert a.can_trade("B", 25.0, TS_SAT) == (False, "kill_switch")

    def test_default_enabled(self, monkeypatch):
        monkeypatch.delenv("PIPELINE_ALLOCATOR_ENABLED", raising=False)
        assert pipeline_allocator_enabled() is True

    def test_bookkeeping_honest_under_switch(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PIPELINE_ALLOCATOR_ENABLED", "false")
        a = _alloc(tmp_path)
        a.record_trade("A", 100.0, ts=TS_SAT)   # still books
        assert clamp_kelly("A", 0.30) == 0.24      # pure math still runs
        assert a.budget("A", 1000.0).trades_today == 1
