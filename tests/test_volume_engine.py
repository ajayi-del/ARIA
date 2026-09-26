"""tests/test_volume_engine.py — pins for the SoPoints volume/fee ledger.

Doctrine pinned: persistence round-trip, one-bad-line recovery, atomic
snapshot (tmp+replace; no torn file on simulated crash), fee-tier boundary
(strictly-greater), Tuesday 12:00 UTC marker math across a week boundary,
pace gauge, by-pool attribution, net-cost abstain legs, negative-pnl cost.

Zero network, zero main.py imports. tmp_path fixture for all files.
"""

import json
import os
from datetime import datetime, timezone

import pytest

from intelligence.volume_engine import (
    FEE_TIERS,
    NET_COST_ABSTAIN_VOLUME_USD,
    SOSO_DISCOUNT,
    VolumeLedger,
    discounted_rate,
    fee_tier_for_14d_volume,
    next_tuesday_noon_utc,
    tuesday_snapshot_marker,
)

WEEK_S = 7 * 86400


def _ts(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc).timestamp()


# 2026-09-22 is a Tuesday; 2026-09-26 is a Saturday (verified against UTC
# calendar math). All pins anchor on these.
TUE_NOON = _ts(2026, 9, 22, 12, 0, 0)
SAT = _ts(2026, 9, 26, 8, 0, 0)


def _ledger(tmp_path, target=65000.0):
    return VolumeLedger(
        ledger_path=str(tmp_path / "vol.jsonl"),
        snapshot_path=str(tmp_path / "gauge.json"),
        weekly_target_usd=target)


def _fill(led, ts, pool="standard", notional=1000.0, fee=0.4, pnl=0.0,
          maker=False, symbol="BTC-USD", side="long"):
    return led.record_fill(ts=ts, symbol=symbol, side=side,
                           notional_usd=notional, fee_usd=fee, pool=pool,
                           realized_pnl_usd=pnl, maker=maker)


# ── Empty ledger ─────────────────────────────────────────────────────────────

class TestEmptyLedger:
    def test_missing_file_is_fresh_start(self, tmp_path):
        led = _ledger(tmp_path)
        g = led.gauge(SAT)
        assert g["cumulative_volume_usd"] == 0.0
        assert g["cumulative_fees_usd"] == 0.0
        assert g["volume_by_pool"] == {}
        assert g["week_volume_usd"] == 0.0
        assert g["pace_vs_target"] == 0.0
        assert g["fee_tier_now"] == "T0"
        assert g["fee_tier_projected"] == "T0"
        assert g["net_cost_per_100k"] is None
        assert g["snapshot_countdown_s"] > 0
        assert g["tuesday_snapshot_window"] is False

    def test_missing_file_no_crash_no_ledger_created_on_load(self, tmp_path):
        _ledger(tmp_path)
        assert not (tmp_path / "vol.jsonl").exists()


# ── Persistence round-trip ────────────────────────────────────────────────────

class TestPersistence:
    def test_round_trip(self, tmp_path):
        led = _ledger(tmp_path)
        _fill(led, SAT, pool="campaign", notional=5000.0, fee=2.0, pnl=1.0)
        _fill(led, SAT + 60, pool="standard", notional=3000.0, fee=1.2)
        led2 = _ledger(tmp_path)  # reboot
        g = led2.gauge(SAT + 120)
        assert g["cumulative_volume_usd"] == 8000.0
        assert g["cumulative_fees_usd"] == pytest.approx(3.2)
        assert g["volume_by_pool"] == {"campaign": 5000.0, "standard": 3000.0}
        assert led2.gauge(SAT + 120)["net_cost_per_100k"] == pytest.approx(
            (2.0 - 1.0) / 5000.0 * 100_000.0)

    def test_append_only_rows_never_rewritten(self, tmp_path):
        led = _ledger(tmp_path)
        _fill(led, SAT, notional=100.0)
        _fill(led, SAT + 1, notional=200.0)
        with open(tmp_path / "vol.jsonl") as f:
            lines = f.readlines()
        assert len(lines) == 2
        r0 = json.loads(lines[0])
        assert r0["notional_usd"] == 100.0
        assert r0["schema"] == 1

    def test_record_fill_returns_true_and_writes_row(self, tmp_path):
        led = _ledger(tmp_path)
        assert _fill(led, SAT, maker=True, side="short") is True
        with open(tmp_path / "vol.jsonl") as f:
            row = json.loads(f.readline())
        assert row["maker"] is True
        assert row["side"] == "short"
        assert row["ts_iso"].endswith("Z")


# ── One-bad-line doctrine ─────────────────────────────────────────────────────

class TestOneBadLine:
    def test_corrupt_line_mid_file_skipped_later_rows_load(self, tmp_path):
        p = tmp_path / "vol.jsonl"
        good1 = json.dumps({"ts": SAT, "notional_usd": 100.0, "fee_usd": 0.1,
                            "pool": "standard"})
        good2 = json.dumps({"ts": SAT + 1, "notional_usd": 200.0,
                            "fee_usd": 0.2, "pool": "campaign",
                            "realized_pnl_usd": 5.0})
        p.write_text(good1 + "\n{corrupt\n" + good2 + "\n")
        led = _ledger(tmp_path)
        g = led.gauge(SAT + 2)
        assert g["cumulative_volume_usd"] == 300.0
        assert led.skipped_lines == 1

    def test_garbage_row_shape_skipped(self, tmp_path):
        p = tmp_path / "vol.jsonl"
        p.write_text(json.dumps({"ts": SAT, "notional_usd": "NaN-not-a-float-in-json"}) + "\n"
                     + json.dumps([1, 2, 3]) + "\n"
                     + json.dumps({"ts": SAT, "notional_usd": 50.0,
                                   "fee_usd": 0.05, "pool": "standard"}) + "\n")
        led = _ledger(tmp_path)
        # row 1: notional not floatable -> skipped in _apply; row 2: not a dict
        assert led.gauge(SAT + 1)["cumulative_volume_usd"] == 50.0
        assert led.skipped_lines == 2

    def test_blank_lines_tolerated(self, tmp_path):
        p = tmp_path / "vol.jsonl"
        p.write_text("\n\n" + json.dumps({"ts": SAT, "notional_usd": 10.0,
                                          "fee_usd": 0.0, "pool": "x"}) + "\n\n")
        led = _ledger(tmp_path)
        assert led.gauge(SAT)["cumulative_volume_usd"] == 10.0
        assert led.skipped_lines == 0


# ── Atomic snapshot ───────────────────────────────────────────────────────────

class TestAtomicSnapshot:
    def test_snapshot_written_on_fill_and_valid_json(self, tmp_path):
        led = _ledger(tmp_path)
        _fill(led, SAT, notional=250.0)
        with open(tmp_path / "gauge.json") as f:
            g = json.load(f)
        assert g["cumulative_volume_usd"] == 250.0
        assert not (tmp_path / "gauge.json.tmp").exists()

    def test_simulated_crash_leaves_old_snapshot_intact(self, tmp_path, monkeypatch):
        led = _ledger(tmp_path)
        _fill(led, SAT, notional=111.0)
        with open(tmp_path / "gauge.json") as f:
            before = f.read()
        # Crash the replace mid-second-write: tmp exists, real name untouched.
        real_replace = os.replace

        def boom(src, dst):
            raise OSError("simulated crash")

        monkeypatch.setattr(os, "replace", boom)
        ok = led.write_snapshot(SAT + 1)
        assert ok is False
        with open(tmp_path / "gauge.json") as f:
            assert f.read() == before
        # cleanup removed the tmp file on failure
        assert not (tmp_path / "gauge.json.tmp").exists()
        monkeypatch.setattr(os, "replace", real_replace)

    def test_write_snapshot_explicit(self, tmp_path):
        led = _ledger(tmp_path)
        assert led.write_snapshot(SAT) is True
        with open(tmp_path / "gauge.json") as f:
            g = json.load(f)
        assert g["cumulative_volume_usd"] == 0.0


# ── Fee-tier table ────────────────────────────────────────────────────────────

class TestFeeTiers:
    def test_boundary_exactly_5m_is_T0(self):
        assert fee_tier_for_14d_volume(5_000_000.0)[0] == "T0"

    def test_just_above_5m_is_T1(self):
        assert fee_tier_for_14d_volume(5_000_001.0)[0] == "T1"

    def test_boundary_25m_is_T1(self):
        assert fee_tier_for_14d_volume(25_000_000.0)[0] == "T1"

    def test_just_above_25m_is_T2(self):
        assert fee_tier_for_14d_volume(25_000_001.0)[0] == "T2"

    def test_boundary_100m_is_T2(self):
        assert fee_tier_for_14d_volume(100_000_000.0)[0] == "T2"

    def test_above_100m_is_T3(self):
        assert fee_tier_for_14d_volume(100_000_001.0)[0] == "T3"

    def test_zero_and_garbage_are_T0(self):
        assert fee_tier_for_14d_volume(0.0)[0] == "T0"
        assert fee_tier_for_14d_volume(-5.0)[0] == "T0"
        assert fee_tier_for_14d_volume(float("nan"))[0] == "T0"
        assert fee_tier_for_14d_volume(None)[0] == "T0"

    def test_rates_match_doctrine(self):
        assert fee_tier_for_14d_volume(0.0) == ("T0", 0.00012, 0.00040)
        assert fee_tier_for_14d_volume(6_000_000.0) == ("T1", 0.00010, 0.00036)
        assert fee_tier_for_14d_volume(30_000_000.0) == ("T2", 0.00006, 0.00032)
        assert fee_tier_for_14d_volume(200_000_000.0) == ("T3", 0.00002, 0.00028)

    def test_soso_discount_multiplicative(self):
        assert SOSO_DISCOUNT == 0.05
        assert discounted_rate(0.00040) == pytest.approx(0.00038)
        assert discounted_rate(0.0) == 0.0

    def test_t4_forward_declared_unreachable(self):
        # T4 threshold is not in the locked doctrine — fail-closed at +inf.
        t4 = [t for t in FEE_TIERS if t[0] == "T4"][0]
        assert t4[1] == float("inf")
        assert t4[2] == 0.0  # maker 0 is the only fixed T4 fact


# ── Tuesday marker math ───────────────────────────────────────────────────────

class TestTuesdayMarker:
    def test_before_noon_false(self):
        assert tuesday_snapshot_marker(_ts(2026, 9, 22, 9, 59, 59)) is False

    def test_noon_exact_true(self):
        assert tuesday_snapshot_marker(TUE_NOON) is True

    def test_inside_window_true(self):
        assert tuesday_snapshot_marker(_ts(2026, 9, 22, 12, 9, 59)) is True

    def test_after_window_false(self):
        assert tuesday_snapshot_marker(_ts(2026, 9, 22, 12, 10, 1)) is False

    def test_next_tuesday_noon_from_saturday(self):
        # Saturday 2026-09-26 08:00 -> next is Tuesday 2026-09-29 12:00
        assert next_tuesday_noon_utc(SAT) == _ts(2026, 9, 29, 12, 0, 0)

    def test_next_tuesday_noon_exact_noon_rolls_to_next_week(self):
        assert next_tuesday_noon_utc(TUE_NOON) == TUE_NOON + WEEK_S

    def test_next_tuesday_noon_monday(self):
        assert next_tuesday_noon_utc(_ts(2026, 9, 21, 23, 0, 0)) == TUE_NOON

    def test_marker_across_week_boundary(self):
        # Wednesday after the window -> false; next Tuesday noon -> true.
        assert tuesday_snapshot_marker(_ts(2026, 9, 23, 12, 0, 0)) is False
        assert tuesday_snapshot_marker(_ts(2026, 9, 29, 12, 0, 0)) is True

    def test_countdown_seconds(self, tmp_path):
        led = _ledger(tmp_path)
        g = led.gauge(_ts(2026, 9, 22, 11, 0, 0))
        assert g["snapshot_countdown_s"] == pytest.approx(3600.0)
        # Inside the window the countdown targets NEXT week's snapshot.
        g2 = led.gauge(TUE_NOON + 60)
        assert g2["snapshot_countdown_s"] == pytest.approx(WEEK_S - 60.0)
        assert g2["tuesday_snapshot_window"] is True


# ── Gauges ────────────────────────────────────────────────────────────────────

class TestGauges:
    def test_rolling_week_excludes_old_fills(self, tmp_path):
        led = _ledger(tmp_path)
        _fill(led, SAT - WEEK_S - 60, notional=9000.0)  # 8d old: outside
        _fill(led, SAT - 3600, notional=1000.0)
        g = led.gauge(SAT)
        assert g["cumulative_volume_usd"] == 10000.0
        assert g["week_volume_usd"] == 1000.0

    def test_pace_vs_target(self, tmp_path):
        led = _ledger(tmp_path, target=65000.0)
        _fill(led, SAT - 100, notional=32500.0)
        g = led.gauge(SAT)
        assert g["pace_vs_target"] == pytest.approx(0.5)

    def test_pace_zero_target_safe(self, tmp_path):
        led = _ledger(tmp_path, target=0.0)
        assert led.gauge(SAT)["pace_vs_target"] is None

    def test_by_pool_attribution(self, tmp_path):
        led = _ledger(tmp_path)
        _fill(led, SAT, pool="campaign", notional=2000.0)
        _fill(led, SAT, pool="campaign", notional=500.0)
        _fill(led, SAT, pool="standard", notional=700.0)
        _fill(led, SAT, pool="whale_probe", notional=100.0)
        g = led.gauge(SAT)
        assert g["volume_by_pool"] == {"campaign": 2500.0,
                                       "standard": 700.0,
                                       "whale_probe": 100.0}

    def test_fee_tier_now_from_14d_window(self, tmp_path):
        led = _ledger(tmp_path)
        # 13d old: inside the 14d window.
        _fill(led, SAT - 13 * 86400, notional=5_000_001.0)
        assert led.gauge(SAT)["fee_tier_now"] == "T1"
        # 15d old: outside — back to T0.
        led2 = _ledger(tmp_path / "b")
        _fill(led2, SAT - 15 * 86400, notional=5_000_001.0)
        assert led2.gauge(SAT)["fee_tier_now"] == "T0"

    def test_fee_tier_projected_from_pace(self, tmp_path):
        led = _ledger(tmp_path)
        _fill(led, SAT - 60, notional=3_000_000.0)  # week pace 3M -> 6M projected
        g = led.gauge(SAT)
        assert g["fee_tier_now"] == "T0"
        assert g["fee_tier_projected"] == "T1"


# ── Net cost per $100k ───────────────────────────────────────────────────────

class TestNetCost:
    def test_abstains_below_1k_campaign_volume(self, tmp_path):
        led = _ledger(tmp_path)
        _fill(led, SAT, pool="campaign", notional=999.99, fee=10.0, pnl=-5.0)
        assert led.gauge(SAT)["net_cost_per_100k"] is None
        assert NET_COST_ABSTAIN_VOLUME_USD == 1000.0

    def test_standard_pool_does_not_lift_campaign_leg(self, tmp_path):
        led = _ledger(tmp_path)
        _fill(led, SAT, pool="standard", notional=50_000.0, fee=20.0)
        _fill(led, SAT, pool="campaign", notional=10.0, fee=0.01)
        assert led.gauge(SAT)["net_cost_per_100k"] is None

    def test_net_cost_positive_pnl_reduces_cost(self, tmp_path):
        led = _ledger(tmp_path)
        _fill(led, SAT, pool="campaign", notional=100_000.0, fee=40.0, pnl=33.0)
        assert led.gauge(SAT)["net_cost_per_100k"] == pytest.approx(7.0)

    def test_negative_pnl_makes_cost_worse(self, tmp_path):
        led = _ledger(tmp_path)
        _fill(led, SAT, pool="campaign", notional=100_000.0, fee=40.0, pnl=-60.0)
        # fees 40 + loss 60 = $100 net cost per $100k
        assert led.gauge(SAT)["net_cost_per_100k"] == pytest.approx(100.0)

    def test_doctrine_target_band(self, tmp_path):
        # $7/$100k is the Governor's ceiling: fees 40, pnl 33.5 -> 6.5 passes.
        led = _ledger(tmp_path)
        _fill(led, SAT, pool="campaign", notional=100_000.0, fee=40.0, pnl=33.5)
        assert led.gauge(SAT)["net_cost_per_100k"] < 7.0


# ── Tier-change detector ──────────────────────────────────────────────────────

class TestTierChange:
    def test_tier_change_consumed_once(self, tmp_path):
        led = _ledger(tmp_path)
        assert led.consume_tier_change() is None
        _fill(led, SAT - 60, notional=5_000_001.0)
        assert led.consume_tier_change() == "T1"
        assert led.consume_tier_change() is None

    def test_no_change_same_tier(self, tmp_path):
        led = _ledger(tmp_path)
        _fill(led, SAT - 60, notional=100.0)
        assert led.consume_tier_change() is None

    def test_tier_downgrades_detected(self, tmp_path):
        led = _ledger(tmp_path)
        _fill(led, SAT - 13 * 86400, notional=6_000_000.0)
        assert led.consume_tier_change() == "T1"
        # After the 14d window rolls past, a new fill re-derives T0.
        _fill(led, SAT + 2 * 86400, notional=10.0)
        assert led.consume_tier_change() == "T0"
