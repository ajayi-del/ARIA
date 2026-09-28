"""tests/test_spread_oracle.py — pins for the persistent spread oracle."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from intelligence.spread_oracle import SpreadOracle, SpreadCascadeWarning


def _cfg(**kw):
    base = dict(
        spread_oracle_enabled=True,
        spread_oracle_warn_multiple=3.0,
        spread_oracle_baseline_halflife_s=14400,
        spread_oracle_min_obs=30,
        spread_oracle_persist_path="logs/spread_oracle.json",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _warmed_oracle(tmp_path, n=30, spread=2.0, mid=100.0, start_ts=1_000_000.0):
    """Oracle with n observations of a stable spread → warm baseline ≈ spread."""
    o = SpreadOracle(persist_path=str(tmp_path / "oracle.json"),
                     clock=lambda: start_ts)
    for i in range(n):
        o.update("BTC-USD", spread, mid, start_ts + i * 60.0)
    return o, start_ts + n * 60.0


# ── cold start / abstain ──────────────────────────────────────────────────────

class TestColdStart:
    def test_no_observations_multiple_none(self):
        o = SpreadOracle(persist_path=None, clock=lambda: 1e6)
        assert o.spread_multiple("BTC-USD", 5.0) is None

    def test_below_min_obs_abstains(self, tmp_path):
        o, _ = _warmed_oracle(tmp_path, n=29)
        assert o.spread_multiple("BTC-USD", 6.0) is None

    def test_exactly_min_obs_answers(self, tmp_path):
        o, _ = _warmed_oracle(tmp_path, n=30, spread=2.0)
        m = o.spread_multiple("BTC-USD", 6.0)
        assert m == pytest.approx(3.0, rel=0.05)

    def test_cold_start_warning_none(self, tmp_path):
        o, now = _warmed_oracle(tmp_path, n=29)
        assert o.cascade_warning(_cfg(), symbol="BTC-USD",
                                 spread_bps=50.0, now_ts=now) is None

    def test_direction_vote_no_data_none(self):
        o = SpreadOracle(persist_path=None, clock=lambda: 1e6)
        assert o.direction_vote("BTC-USD") is None


# ── baseline / multiple ───────────────────────────────────────────────────────

class TestMultiple:
    def test_flat_spread_multiple_one(self, tmp_path):
        o, _ = _warmed_oracle(tmp_path, n=40, spread=2.0)
        assert o.spread_multiple("BTC-USD", 2.0) == pytest.approx(1.0, rel=0.05)

    def test_zero_baseline_no_division(self):
        o = SpreadOracle(persist_path=None, clock=lambda: 1e6)
        # n=30 zero-spread observations → baseline 0 → None, never inf
        for i in range(30):
            o.update("BTC-USD", 0.0, 100.0, 1e6 + i)
        assert o.spread_multiple("BTC-USD", 1.0) is None

    def test_garbage_update_ignored(self):
        o = SpreadOracle(persist_path=None, clock=lambda: 1e6)
        o.update("BTC-USD", float("nan"), 100.0, 1e6)
        o.update("BTC-USD", 2.0, -1, 1e6)
        o.update("", 2.0, 100.0, 1e6)
        assert "BTC-USD" not in o.snapshot()

    def test_ewm_tracks_regime_shift(self, tmp_path):
        o, now = _warmed_oracle(tmp_path, n=30, spread=2.0)
        # 600 minutes ≈ 2.5 halflives at the 4h default → baseline must
        # chase the new 8bps regime most of the way.
        for i in range(600):
            o.update("BTC-USD", 8.0, 100.0, now + i * 60.0)
        base = o.snapshot()["BTC-USD"]["baseline"]
        assert base > 6.0  # baseline chased the new regime

    def test_multiple_garbage_input_none(self, tmp_path):
        o, _ = _warmed_oracle(tmp_path, n=30)
        assert o.spread_multiple("BTC-USD", "junk") is None


# ── cascade warning ───────────────────────────────────────────────────────────

class TestCascadeWarning:
    def test_below_threshold_none(self, tmp_path):
        o, now = _warmed_oracle(tmp_path, n=30, spread=2.0)
        assert o.cascade_warning(_cfg(), symbol="BTC-USD",
                                 spread_bps=5.9, now_ts=now) is None

    def test_at_exactly_3x_warns(self, tmp_path):
        o, now = _warmed_oracle(tmp_path, n=30, spread=2.0)
        w = o.cascade_warning(_cfg(), symbol="BTC-USD",
                              spread_bps=6.0, now_ts=now)
        assert w is not None and w.multiple == pytest.approx(3.0, rel=0.05)

    def test_above_threshold_warns(self, tmp_path):
        o, now = _warmed_oracle(tmp_path, n=30, spread=2.0)
        w = o.cascade_warning(_cfg(), symbol="BTC-USD",
                              spread_bps=10.0, now_ts=now)
        assert w is not None and w.multiple == pytest.approx(5.0, rel=0.05)

    def test_kill_switch_none(self, tmp_path):
        o, now = _warmed_oracle(tmp_path, n=30, spread=2.0)
        assert o.cascade_warning(_cfg(spread_oracle_enabled=False),
                                 symbol="BTC-USD", spread_bps=20.0,
                                 now_ts=now) is None

    def test_custom_warn_multiple(self, tmp_path):
        o, now = _warmed_oracle(tmp_path, n=30, spread=2.0)
        assert o.cascade_warning(_cfg(spread_oracle_warn_multiple=6.0),
                                 symbol="BTC-USD", spread_bps=10.0,
                                 now_ts=now) is None

    def test_warning_carries_baseline(self, tmp_path):
        o, now = _warmed_oracle(tmp_path, n=30, spread=2.0)
        w = o.cascade_warning(_cfg(), symbol="BTC-USD",
                              spread_bps=8.0, now_ts=now)
        assert w.baseline_bps == pytest.approx(2.0, rel=0.05)
        assert w.symbol == "BTC-USD"


# ── direction vote ────────────────────────────────────────────────────────────

class TestDirectionVote:
    def test_rising_mid_up(self, tmp_path):
        o, now = _warmed_oracle(tmp_path, n=30, spread=2.0)
        o.update("BTC-USD", 6.0, 100.0, now)
        o.update("BTC-USD", 6.0, 101.0, now + 30)
        assert o.direction_vote("BTC-USD", now + 30) == "up"

    def test_falling_mid_down(self, tmp_path):
        o, now = _warmed_oracle(tmp_path, n=30, spread=2.0)
        o.update("BTC-USD", 6.0, 100.0, now)
        o.update("BTC-USD", 6.0, 99.0, now + 30)
        assert o.direction_vote("BTC-USD", now + 30) == "down"

    def test_flat_mids_none(self, tmp_path):
        o, now = _warmed_oracle(tmp_path, n=30, spread=2.0)
        o.update("BTC-USD", 6.0, 100.0, now)
        o.update("BTC-USD", 6.0, 100.0, now + 30)
        assert o.direction_vote("BTC-USD", now + 30) is None

    def test_single_observation_none(self, tmp_path):
        o, now = _warmed_oracle(tmp_path, n=30, spread=2.0)
        fresh = now + 3600  # old mids fallen out of the 60s ring
        o.update("BTC-USD", 6.0, 100.0, fresh)
        assert o.direction_vote("BTC-USD", fresh) is None

    def test_warning_flips_up_drift_to_down_vote(self, tmp_path):
        """positive drift + widening = longs trapped → DOWN vote."""
        o, now = _warmed_oracle(tmp_path, n=30, spread=2.0)
        o.update("BTC-USD", 6.0, 100.0, now)
        o.update("BTC-USD", 6.5, 101.0, now + 30)
        w = o.cascade_warning(_cfg(), symbol="BTC-USD",
                              spread_bps=6.5, now_ts=now + 30)
        assert w.direction_vote == "down"

    def test_warning_flips_down_drift_to_up_vote(self, tmp_path):
        o, now = _warmed_oracle(tmp_path, n=30, spread=2.0)
        o.update("BTC-USD", 6.0, 100.0, now)
        o.update("BTC-USD", 6.5, 99.0, now + 30)
        w = o.cascade_warning(_cfg(), symbol="BTC-USD",
                              spread_bps=6.5, now_ts=now + 30)
        assert w.direction_vote == "up"

    def test_warning_flat_drift_none_vote(self, tmp_path):
        o, now = _warmed_oracle(tmp_path, n=30, spread=2.0)
        o.update("BTC-USD", 6.0, 100.0, now)
        o.update("BTC-USD", 6.5, 100.0, now + 30)
        w = o.cascade_warning(_cfg(), symbol="BTC-USD",
                              spread_bps=6.5, now_ts=now + 30)
        assert w.direction_vote is None


# ── persistence ───────────────────────────────────────────────────────────────

class TestPersistence:
    def test_snapshot_roundtrip(self, tmp_path):
        path = str(tmp_path / "oracle.json")
        o, now = _warmed_oracle(tmp_path, n=30, spread=2.0)
        # force a write past the throttle
        o.update("BTC-USD", 2.0, 100.0, now + 400.0)
        o2 = SpreadOracle(persist_path=path, clock=lambda: now + 500.0)
        m = o2.spread_multiple("BTC-USD", 6.0)
        assert m == pytest.approx(3.0, rel=0.1)

    def test_throttle_one_write_per_5min(self, tmp_path):
        path = str(tmp_path / "oracle.json")
        o = SpreadOracle(persist_path=path, clock=lambda: 1e6)
        o.update("BTC-USD", 2.0, 100.0, 1e6)         # first write
        mtime1 = os.path.getmtime(path)
        o.update("BTC-USD", 50.0, 100.0, 1e6 + 60)   # inside 5 min → no write
        with open(path) as fh:
            doc = json.load(fh)
        assert doc["state"]["BTC-USD"]["baseline"] == pytest.approx(2.0)

    def test_write_after_throttle_window(self, tmp_path):
        path = str(tmp_path / "oracle.json")
        o = SpreadOracle(persist_path=path, clock=lambda: 1e6)
        o.update("BTC-USD", 2.0, 100.0, 1e6)
        o.update("BTC-USD", 10.0, 100.0, 1e6 + 301)  # past 5 min → writes
        with open(path) as fh:
            doc = json.load(fh)
        assert doc["state"]["BTC-USD"]["baseline"] > 2.0

    def test_corrupt_file_fresh_start(self, tmp_path):
        path = str(tmp_path / "oracle.json")
        with open(path, "w") as fh:
            fh.write("{not json")
        o = SpreadOracle(persist_path=path, clock=lambda: 1e6)  # no crash
        assert o.spread_multiple("BTC-USD", 5.0) is None
        o.update("BTC-USD", 2.0, 100.0, 1e6)  # recovers and tracks

    def test_none_persist_path_no_io(self, tmp_path):
        o = SpreadOracle(persist_path=None, clock=lambda: 1e6)
        for i in range(40):
            o.update("BTC-USD", 2.0, 100.0, 1e6 + i * 400)
        assert not (tmp_path / "oracle.json").exists()

    def test_atomic_no_tmp_leftover(self, tmp_path):
        path = str(tmp_path / "oracle.json")
        o = SpreadOracle(persist_path=path, clock=lambda: 1e6)
        o.update("BTC-USD", 2.0, 100.0, 1e6)
        assert os.path.exists(path)
        assert not os.path.exists(path + ".tmp")
