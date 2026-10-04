"""tests/test_reward_actuals.py — reward-actuals tracker pins.

Covers: Tue 00:00 UTC epoch bucketing across the boundary, treasure-box
floor math (incl. mid-epoch crossings), upsert idempotency, net_usd =
rewards_cash − fees (HARVEST ledger — never alpha pnl), and unknown-venue
manual rows passing through as skeletons.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import reward_actuals as ra  # noqa: E402


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


# 2026-09-29 is a Tuesday. Epoch E1 = Tue 2026-09-29 00:00Z → Mon 2026-10-05 23:59:59Z.
E1_START = _ts("2026-09-29T00:00:00Z")
E1_END = E1_START + 7 * 86400.0
# Previous epoch E0 ends exactly at E1_START.
E0_START = E1_START - 7 * 86400.0


def _ledger_row(ts: float, notional: float, fee: float, pnl: float = 0.0) -> dict:
    return {"ts": ts, "notional_usd": notional, "fee_usd": fee,
            "realized_pnl_usd": pnl}


class TestEpochBounds:
    def test_tuesday_midnight_starts_new_epoch(self):
        start, end = ra.epoch_bounds(E1_START)
        assert start == E1_START
        assert end == E1_END

    def test_monday_late_belongs_to_prior_tuesday(self):
        start, end = ra.epoch_bounds(E1_END - 1.0)  # Mon 23:59:59Z
        assert start == E1_START
        assert end == E1_END

    def test_one_second_before_tuesday_is_old_epoch(self):
        start, _ = ra.epoch_bounds(E1_START - 1.0)  # Mon 23:59:59Z prev week
        assert start == E0_START

    def test_midweek_stays_put(self):
        start, end = ra.epoch_bounds(_ts("2026-10-02T13:37:00Z"))
        assert start == E1_START
        assert end == E1_END


class TestEpochBucketing:
    def test_rows_split_across_the_tuesday_boundary(self):
        rows = [
            _ledger_row(E1_START - 1.0, 1000.0, 0.4),   # Mon 23:59:59 → E0
            _ledger_row(E1_START, 2000.0, 0.8),          # Tue 00:00:00 → E1
            _ledger_row(E1_END - 0.5, 4000.0, 1.6),      # Mon 23:59:59.5 → E1
        ]
        rec = ra.compute_epoch_record(
            "sodex", E1_START, E1_END, E1_END, rows, [], E1_END)
        assert rec["volume_usd"] == pytest.approx(6000.0)
        assert rec["fees_usd"] == pytest.approx(2.4)
        # Cumulative counters still see the E0 row (boxes are lifetime).
        assert rec["boxes"]["earned_cumulative"] == 0  # 7000 < 10_000

    def test_fee_per_100k(self):
        rows = [_ledger_row(E1_START + 60, 50_000.0, 10.0)]
        rec = ra.compute_epoch_record(
            "sodex", E1_START, E1_END, E1_END, rows, [], E1_END)
        assert rec["fee_per_100k"] == pytest.approx(20.0)

    def test_zero_volume_fee_per_100k_null(self):
        rec = ra.compute_epoch_record(
            "sodex", E1_START, E1_END, E1_END, [], [], E1_END)
        assert rec["fee_per_100k"] is None
        assert rec["net_usd"] == pytest.approx(0.0)


class TestBoxesFloorMath:
    def test_floor_math_cumulative_and_delta(self):
        # 29,999 lifetime before epoch → 2 boxes; epoch pushes to 31,000 → 3.
        rows = [
            _ledger_row(E0_START + 10, 29_999.0, 0.0),       # pre-epoch
            _ledger_row(E1_START + 10, 1_001.0, 0.0),        # crosses 30k
        ]
        rec = ra.compute_epoch_record(
            "sodex", E1_START, E1_END, E1_END, rows, [], E1_END)
        assert rec["boxes"]["earned_cumulative"] == 3
        assert rec["boxes"]["delta"] == 1
        assert rec["boxes"]["volume_per_box_usd"] == 10_000

    def test_no_crossing_delta_zero(self):
        rows = [
            _ledger_row(E0_START + 10, 15_000.0, 0.0),
            _ledger_row(E1_START + 10, 4_999.0, 0.0),   # 19,999 → still 1 box
        ]
        rec = ra.compute_epoch_record(
            "sodex", E1_START, E1_END, E1_END, rows, [], E1_END)
        assert rec["boxes"]["earned_cumulative"] == 1
        assert rec["boxes"]["delta"] == 0

    def test_in_progress_epoch_no_lookahead(self):
        # A row AFTER as_of (later in the live week) must not count yet.
        as_of = E1_START + 100.0
        rows = [
            _ledger_row(E1_START + 50, 9_000.0, 1.0),
            _ledger_row(E1_START + 200, 9_000.0, 1.0),   # after as_of
        ]
        rec = ra.compute_epoch_record(
            "sodex", E1_START, E1_END, as_of, rows, [], as_of)
        assert rec["volume_usd"] == pytest.approx(9_000.0)
        assert rec["boxes"]["earned_cumulative"] == 0
        # Display bound stays the full week's Monday close.
        assert rec["epoch_end"] == "2026-10-05T23:59:59Z"


class TestManualMergeAndNet:
    def test_cash_kinds_merge_and_net_math(self):
        rows = [_ledger_row(E1_START + 10, 20_000.0, 8.0)]
        manual = [
            {"ts": E1_START + 20, "venue": "sodex", "kind": "box_opened",
             "amount_usd": 3.5, "notes": "box #1"},
            {"ts": E1_START + 30, "venue": "sodex", "kind": "usdc_payout",
             "amount_usd": 10.0},
        ]
        rec = ra.compute_epoch_record(
            "sodex", E1_START, E1_END, E1_END, rows, manual, E1_END)
        assert rec["rewards_cash_usd"] == pytest.approx(13.5)
        assert rec["net_usd"] == pytest.approx(13.5 - 8.0)
        assert rec["boxes"]["opened_manual_count"] == 1
        assert rec["boxes"]["earned_cumulative"] == 2
        # Alpha pnl never enters net_usd even when the ledger carries it.
        assert "realized_pnl_usd_info" in rec

    def test_net_excludes_trading_pnl(self):
        rows = [_ledger_row(E1_START + 10, 10_000.0, 4.0, pnl=+500.0)]
        rec = ra.compute_epoch_record(
            "sodex", E1_START, E1_END, E1_END, rows, [], E1_END)
        assert rec["net_usd"] == pytest.approx(-4.0)  # rewards 0 − fees 4
        assert rec["realized_pnl_usd_info"] == pytest.approx(500.0)

    def test_points_snapshot_nullable(self):
        manual = [
            {"ts": E1_START + 20, "venue": "sodex", "kind": "points_snapshot",
             "program": "sopoints", "points": 1234.0},
        ]
        rec = ra.compute_epoch_record(
            "sodex", E1_START, E1_END, E1_END, [], manual, E1_END)
        assert rec["rewards_points"] == {"program": "sopoints", "points": 1234.0}
        assert rec["rewards_cash_usd"] == 0.0  # points are not cash

    def test_manual_outside_epoch_ignored(self):
        manual = [
            {"ts": E0_START + 10, "venue": "sodex", "kind": "usdc_payout",
             "amount_usd": 99.0},
        ]
        rec = ra.compute_epoch_record(
            "sodex", E1_START, E1_END, E1_END, [], manual, E1_END)
        assert rec["rewards_cash_usd"] == 0.0


class TestUnknownVenuePassthrough:
    def test_unknown_venue_skeleton_row(self):
        manual = [
            {"ts": E1_START + 10, "venue": "hyperliquid",
             "kind": "usdc_payout", "amount_usd": 7.25},
        ]
        recs = ra.build_records(E1_START, E1_END, E1_END, [], True, manual,
                                E1_END)
        venues = {r["venue"] for r in recs}
        assert venues == {"sodex", "hyperliquid"}
        hl = next(r for r in recs if r["venue"] == "hyperliquid")
        assert hl["volume_usd"] == 0.0
        assert hl["fees_usd"] == 0.0
        assert hl["rewards_cash_usd"] == pytest.approx(7.25)
        assert hl["net_usd"] == pytest.approx(7.25)
        assert hl["boxes"] is None  # boxes are sodex-only
        assert hl["rewards_points"]["points"] is None

    def test_manual_entry_outside_epoch_does_not_spawn_venue(self):
        manual = [
            {"ts": E0_START + 10, "venue": "vooi", "kind": "usdc_payout",
             "amount_usd": 1.0},
        ]
        recs = ra.build_records(E1_START, E1_END, E1_END, [], True, manual,
                                E1_END)
        assert {r["venue"] for r in recs} == {"sodex"}


class TestIdempotency:
    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_upsert_replaces_same_epoch_venue_never_duplicates(self, tmp_path):
        out = tmp_path / "reward_actuals.jsonl"
        rec_v1 = [{"epoch_start": "2026-09-29T00:00:00Z", "venue": "sodex",
                   "net_usd": -1.0}]
        rec_v2 = [{"epoch_start": "2026-09-29T00:00:00Z", "venue": "sodex",
                   "net_usd": -2.0},
                  {"epoch_start": "2026-09-29T00:00:00Z", "venue": "ondo",
                   "net_usd": 5.0}]
        ra.upsert_records(out, rec_v1)
        written, replaced = ra.upsert_records(out, rec_v2)
        assert (written, replaced) == (2, 1)
        lines = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
        assert len(lines) == 2
        sodex = [l for l in lines if l["venue"] == "sodex"]
        assert len(sodex) == 1
        assert sodex[0]["net_usd"] == -2.0  # latest wins

    def test_other_epochs_untouched(self, tmp_path):
        out = tmp_path / "reward_actuals.jsonl"
        old = [{"epoch_start": "2026-09-22T00:00:00Z", "venue": "sodex",
                "net_usd": -9.0}]
        new = [{"epoch_start": "2026-09-29T00:00:00Z", "venue": "sodex",
                "net_usd": -1.0}]
        ra.upsert_records(out, old)
        ra.upsert_records(out, new)
        lines = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
        assert len(lines) == 2
        assert any(l["epoch_start"] == "2026-09-22T00:00:00Z" for l in lines)

    def test_full_run_twice_single_line_per_venue(self, tmp_path):
        ledger = tmp_path / "volume_ledger.jsonl"
        manual = tmp_path / "reward_manual_entries.jsonl"
        out = tmp_path / "reward_actuals.jsonl"
        self._write_jsonl(ledger, [{
            "ts": E1_START + 10, "notional_usd": 12_000.0, "fee_usd": 3.0,
            "realized_pnl_usd": 0.0}])
        self._write_jsonl(manual, [{
            "ts": E1_START + 20, "venue": "ondo", "kind": "usdc_payout",
            "amount_usd": 2.0}])
        argv = ["--epoch", "2026-09-29", "--ledger", str(ledger),
                "--manual", str(manual), "--out", str(out)]
        assert ra.main(argv) == 0
        assert ra.main(argv) == 0  # second run upserts, never duplicates
        lines = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
        assert len(lines) == 2
        assert sorted(l["venue"] for l in lines) == ["ondo", "sodex"]


class TestFailClosedReads:
    def test_missing_ledger_flags_note_not_error(self, tmp_path):
        rows, skipped, present = ra.read_ledger_rows(tmp_path / "nope.jsonl")
        assert rows == [] and present is False
        rec = ra.compute_epoch_record(
            "sodex", E1_START, E1_END, E1_END, rows, [], E1_END,
            ledger_present=present)
        assert rec["notes"] == "ledger_missing"
        assert rec["volume_usd"] == 0.0

    def test_one_bad_line_doctrine(self, tmp_path):
        p = tmp_path / "volume_ledger.jsonl"
        p.write_text(
            '{"ts": %d, "notional_usd": 1000.0, "fee_usd": 0.4}\n'
            "NOT JSON\n"
            '{"ts": "garbage", "notional_usd": 5}\n'
            '{"ts": %d, "notional_usd": 2000.0, "fee_usd": 0.8}\n'
            % (E1_START + 1, E1_START + 2))
        rows, skipped, present = ra.read_ledger_rows(p)
        assert present is True
        assert len(rows) == 2
        assert skipped == 2

    def test_manual_ts_accepts_epoch_and_iso(self):
        assert ra._parse_ts(1790640000.0) == 1790640000.0
        assert ra._parse_ts("2026-09-29T00:00:00Z") == E1_START
        assert ra._parse_ts("2026-09-29T00:00:00+00:00") == E1_START
        assert ra._parse_ts("not-a-time") is None
        assert ra._parse_ts(None) is None
