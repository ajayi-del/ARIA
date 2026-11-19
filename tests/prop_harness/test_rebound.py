"""Pins: rebound brain — underwater-recovery cycles (2026-11-17).

Governor observation: coins dip underwater and come back several times;
each completed dip→recover cycle is a defended-level signal. The
tracker ARMS at -arm_r, COUNTS on recovery to >= recover_r, and re-arms
for the next dip. Live effect is decline-only: a rebound-qualified
bracket currently underwater may not be sharpened past entry into the
dip (never loosens). Kill PROP_REBOUND_TRACKER=0 /
PROP_REBOUND_SHARPEN_GUARD=0 → legacy bit-for-bit.
"""

from __future__ import annotations

import json

import pytest

from prop_harness.brains import rebound as rb


def _bracket(**over) -> dict:
    b = {"semantic_id": "s1", "asset": "BTC", "side": "buy",
         "entry_ref": "100", "stop_price": "92", "current_stop": "92",
         "status": "open"}
    b.update(over)
    return b


class TestTrackRebound:
    def test_dip_arms_recovery_counts(self):
        b = _bracket()
        # stop_dist = 8 → -0.5R = 96, breakeven = 100
        mut, rec = rb.track_rebound(b, 95.0, arm_r=0.5)
        assert mut is True and rec is False
        assert b["rebound_armed"] is True
        mut, rec = rb.track_rebound(b, 100.5, arm_r=0.5)
        assert mut is True and rec is True
        assert b["rebound_count"] == 1
        assert b["rebound_armed"] is False

    def test_noise_band_dip_never_arms(self):
        b = _bracket()
        mut, rec = rb.track_rebound(b, 97.0, arm_r=0.5)  # -0.375R
        assert (mut, rec) == (False, False)
        assert "rebound_armed" not in b

    def test_multiple_cycles_count_each(self):
        b = _bracket()
        rb.track_rebound(b, 95.0)      # arm
        rb.track_rebound(b, 101.0)     # cycle 1
        rb.track_rebound(b, 95.5)      # re-arm
        mut, rec = rb.track_rebound(b, 100.2)  # cycle 2
        assert rec is True and b["rebound_count"] == 2

    def test_stays_armed_through_deep_dip_no_double_count(self):
        b = _bracket()
        rb.track_rebound(b, 95.0)
        mut, rec = rb.track_rebound(b, 93.0)   # deeper, still armed
        assert (mut, rec) == (False, False)
        assert b["rebound_armed"] is True
        assert "rebound_count" not in b

    def test_recovery_requires_arm_first(self):
        b = _bracket()
        mut, rec = rb.track_rebound(b, 105.0)  # green, never armed
        assert (mut, rec) == (False, False)
        assert "rebound_count" not in b

    def test_sell_mirror(self):
        b = _bracket(side="sell", stop_price="108")  # stop_dist 8
        mut, rec = rb.track_rebound(b, 105.0)  # +5 adverse = -0.625R
        assert mut is True and b["rebound_armed"] is True
        mut, rec = rb.track_rebound(b, 99.5)   # back through entry
        assert rec is True and b["rebound_count"] == 1

    def test_bad_geometry_abstains(self):
        b = _bracket(entry_ref=None)
        assert rb.track_rebound(b, 95.0) == (False, False)
        assert "rebound_armed" not in b
        assert rb.track_rebound(_bracket(), None) == (False, False)


class TestReboundQualified:
    def test_threshold(self):
        assert rb.rebound_qualified(_bracket(rebound_count=2)) is True
        assert rb.rebound_qualified(_bracket(rebound_count=1)) is False
        assert rb.rebound_qualified(_bracket()) is False
        assert rb.rebound_qualified(
            _bracket(rebound_count="junk")) is False


class TestSharpenSuppressed:
    def test_qualified_underwater_lock_past_entry_suppresses(self):
        b = _bracket(rebound_count=2)
        assert rb.sharpen_suppressed(b, 99.0, "101") is True

    def test_green_position_never_suppressed(self):
        b = _bracket(rebound_count=3)
        assert rb.sharpen_suppressed(b, 102.0, "101") is False

    def test_unqualified_never_suppressed(self):
        b = _bracket(rebound_count=1)
        assert rb.sharpen_suppressed(b, 99.0, "101") is False

    def test_target_below_entry_never_suppressed(self):
        # a target still under water is not a lock-past-entry tighten
        b = _bracket(rebound_count=2)
        assert rb.sharpen_suppressed(b, 99.0, "97") is False

    def test_sell_mirror(self):
        b = _bracket(side="sell", stop_price="108", rebound_count=2)
        assert rb.sharpen_suppressed(b, 101.0, "99") is True
        assert rb.sharpen_suppressed(b, 98.0, "99") is False


class TestExecutorRebound:
    def _exec(self, tmp_path, **over):
        from tests.prop_harness.test_stop_arbiter import (_ArbClient,
                                                          _exec as mk)
        return mk(tmp_path, _ArbClient(), **over)

    def test_tracker_writes_and_journals_cycle(self, tmp_path):
        ex = self._exec(tmp_path)
        b = _bracket()
        assert ex.track_rebound(b, 95.0) is True     # armed
        assert ex.track_rebound(b, 101.0) is True    # cycle 1 + event
        assert b["rebound_count"] == 1
        from tests.prop_harness.test_stop_arbiter import _events
        evs = [e for e in _events(tmp_path)
               if e["type"] == "rebound_recovered"]
        assert len(evs) == 1
        assert evs[0]["rebound_count"] == 1
        assert evs[0]["asset"] == "BTC"

    def test_kill_off_untouched(self, tmp_path):
        ex = self._exec(tmp_path, rebound_tracker_enabled=False)
        b = _bracket()
        assert ex.track_rebound(b, 95.0) is False
        assert "rebound_armed" not in b

    def test_closed_status_and_none_mark_abstain(self, tmp_path):
        ex = self._exec(tmp_path)
        assert ex.track_rebound(_bracket(status="closed"), 95.0) is False
        assert ex.track_rebound(_bracket(), None) is False

    def test_sharpen_guard_suppresses_qualified_underwater(self, tmp_path):
        ex = self._exec(tmp_path)
        # mfe_sharpen needs a peak_usd >= trigger ($30) — geometry:
        # entry 100, stop 92, qty 1.5 → birth risk $12; peak at 140 =
        # +5R = $60 peak usd. Mark currently underwater at 99.
        b = _bracket(rebound_count=2, mfe_peak_r=5.0, risk_usd=12.0)
        pos = {"quantity": "1.5", "positionId": "p1"}
        assert ex.sharpen_mfe_bracket(b, pos, 99.0) is False
        from tests.prop_harness.test_stop_arbiter import _events
        evs = [e for e in _events(tmp_path)
               if e["type"] == "rebound_sharpen_suppressed"]
        assert len(evs) == 1
        assert evs[0]["rebound_count"] == 2

    def test_sharpen_guard_off_legacy(self, tmp_path):
        ex = self._exec(tmp_path, rebound_sharpen_guard_enabled=False,
                        stop_arbiter_enabled=False)  # direct resize path
        b = _bracket(rebound_count=2, mfe_peak_r=5.0, risk_usd=12.0)
        pos = {"quantity": "1.5", "positionId": "p1"}
        # legacy: the sharpen proceeds to the venue unsuppressed
        assert ex.sharpen_mfe_bracket(b, pos, 99.0) is True


class TestConfigParse:
    def test_defaults_and_env(self, monkeypatch, tmp_path):
        for k in ("PROP_REBOUND_TRACKER", "PROP_REBOUND_SHARPEN_GUARD",
                  "PROP_REBOUND_ARM_R", "PROP_REBOUND_MIN_COUNT"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        from prop_harness.config import PropConfig
        cfg = PropConfig.from_env()
        assert cfg.rebound_tracker_enabled is True
        assert cfg.rebound_sharpen_guard_enabled is True
        assert cfg.rebound_arm_r == pytest.approx(0.5)
        assert cfg.rebound_min_count == 2
        monkeypatch.setenv("PROP_REBOUND_TRACKER", "0")
        monkeypatch.setenv("PROP_REBOUND_MIN_COUNT", "3")
        cfg = PropConfig.from_env()
        assert cfg.rebound_tracker_enabled is False
        assert cfg.rebound_min_count == 3
