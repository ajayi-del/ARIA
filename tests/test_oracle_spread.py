"""
tests/test_oracle_spread.py — pins for intelligence/oracle_spread.py
(Governor venue-asymmetry axiom 2026-10-24: bybit leads, arcus/sodex lag;
20bps alert / 50bps strong; exact direction-hint mapping table).
"""
from __future__ import annotations

import math

import pytest

from intelligence.oracle_spread import (
    OracleSpread,
    OracleSpreadConfig,
    SpreadVerdict,
    oracle_spread_enabled,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("ORACLE_SPREAD_ENABLED", raising=False)
    yield


def _spread(enabled=True) -> OracleSpread:
    return OracleSpread()


def _feed3(sp: OracleSpread, symbol: str = "BTC-USD", t: float = 1000.0,
           bybit=100.0, arcus=100.0, sodex=100.0):
    if bybit is not None:
        sp.update(symbol, "bybit", bybit, t)
    if arcus is not None:
        sp.update(symbol, "arcus", arcus, t)
    if sodex is not None:
        sp.update(symbol, "sodex", sodex, t)


# ---------------------------------------------------------------- kill switch

class TestKillSwitch:
    def test_default_enabled(self):
        assert oracle_spread_enabled() is True

    def test_env_false_disables(self, monkeypatch):
        monkeypatch.setenv("ORACLE_SPREAD_ENABLED", "false")
        assert oracle_spread_enabled() is False

    def test_disabled_update_keeps_no_state(self, monkeypatch):
        monkeypatch.setenv("ORACLE_SPREAD_ENABLED", "false")
        sp = _spread()
        sp.update("BTC-USD", "bybit", 100.0, 1000.0)
        sp.update("BTC-USD", "sodex", 100.5, 1000.0)
        assert sp.state_snapshot()["marks"] == {}

    def test_disabled_observe_returns_empty(self, monkeypatch):
        sp = _spread()
        _feed3(sp, sodex=100.6)  # 60bps — would be strong
        monkeypatch.setenv("ORACLE_SPREAD_ENABLED", "false")
        assert sp.observe(["BTC-USD"], 1000.0) == []

    def test_call_time_read(self, monkeypatch):
        """Toggling the env between calls flips behavior on the same instance."""
        sp = _spread()
        _feed3(sp, sodex=100.3)
        assert len(sp.observe(["BTC-USD"], 1000.0)) == 1
        monkeypatch.setenv("ORACLE_SPREAD_ENABLED", "false")
        assert sp.observe(["BTC-USD"], 1000.0) == []
        monkeypatch.delenv("ORACLE_SPREAD_ENABLED")
        assert len(sp.observe(["BTC-USD"], 1000.0)) == 1


# ------------------------------------------------------------ threshold bands

class TestThresholds:
    def test_15bps_no_verdict(self):
        sp = _spread()
        _feed3(sp, sodex=100.15)
        assert sp.observe(["BTC-USD"], 1000.0) == []

    def test_25bps_alert(self):
        sp = _spread()
        _feed3(sp, sodex=100.25)
        vs = sp.observe(["BTC-USD"], 1000.0)
        assert len(vs) == 1
        assert vs[0].strength == "alert"

    def test_55bps_strong(self):
        sp = _spread()
        _feed3(sp, sodex=100.55)
        vs = sp.observe(["BTC-USD"], 1000.0)
        assert len(vs) == 1
        assert vs[0].strength == "strong"

    def test_exact_20bps_is_alert(self):
        sp = _spread()
        _feed3(sp, sodex=100.20)
        vs = sp.observe(["BTC-USD"], 1000.0)
        assert len(vs) == 1
        assert vs[0].strength == "alert"

    def test_exact_50bps_is_strong(self):
        sp = _spread()
        _feed3(sp, sodex=100.50)
        vs = sp.observe(["BTC-USD"], 1000.0)
        assert len(vs) == 1
        assert vs[0].strength == "strong"

    def test_negative_spread_alerts_too(self):
        sp = _spread()
        _feed3(sp, sodex=99.45)  # -55bps
        vs = sp.observe(["BTC-USD"], 1000.0)
        assert len(vs) == 1
        assert vs[0].strength == "strong"
        assert vs[0].spread_bps < 0


# --------------------------------------------------------- leader / staleness

class TestLeaderSelection:
    def test_bybit_leads_even_when_arcus_fresher(self):
        sp = _spread()
        sp.update("BTC-USD", "bybit", 100.0, 999.5)   # older
        sp.update("BTC-USD", "arcus", 100.4, 1000.0)  # fresher
        vs = sp.observe(["BTC-USD"], 1000.0)
        assert len(vs) == 1
        assert vs[0].leader_venue == "bybit"
        assert vs[0].lagger_venue == "arcus"

    def test_bybit_stale_arcus_leads(self):
        sp = _spread()
        sp.update("BTC-USD", "bybit", 100.0, 985.0)   # 15s old > 10s stale
        sp.update("BTC-USD", "arcus", 100.0, 1000.0)
        sp.update("BTC-USD", "sodex", 100.3, 1000.0)
        vs = sp.observe(["BTC-USD"], 1000.0)
        assert len(vs) == 1
        assert vs[0].leader_venue == "arcus"
        assert vs[0].lagger_venue == "sodex"

    def test_staleness_exclusion_drops_old_marks(self):
        sp = _spread()
        sp.update("BTC-USD", "bybit", 100.0, 1000.0)
        sp.update("BTC-USD", "sodex", 100.6, 989.9)  # stale at t=1000
        assert sp.observe(["BTC-USD"], 1000.0) == []

    def test_mark_exactly_at_staleness_boundary_is_fresh(self):
        sp = _spread()
        sp.update("BTC-USD", "bybit", 100.0, 1000.0)
        sp.update("BTC-USD", "sodex", 100.3, 990.0)  # age == staleness_s
        vs = sp.observe(["BTC-USD"], 1000.0)
        assert len(vs) == 1

    def test_single_fresh_venue_no_verdict(self):
        sp = _spread()
        sp.update("BTC-USD", "bybit", 100.0, 1000.0)
        assert sp.observe(["BTC-USD"], 1000.0) == []

    def test_two_laggers_both_emit(self):
        sp = _spread()
        _feed3(sp, arcus=100.30, sodex=99.45)  # +30 alert / -55 strong
        vs = sp.observe(["BTC-USD"], 1000.0)
        assert len(vs) == 2
        by_lagger = {v.lagger_venue: v for v in vs}
        assert by_lagger["arcus"].strength == "alert"
        assert by_lagger["sodex"].strength == "strong"
        assert all(v.leader_venue == "bybit" for v in vs)


# ------------------------------------------------------- direction-hint table

class TestDirectionHints:
    def test_arcus_premium_long_arcus(self):
        sp = _spread()
        _feed3(sp, arcus=100.30, sodex=None)
        vs = sp.observe(["BTC-USD"], 1000.0)
        assert vs[0].direction_hint == "long_arcus"

    def test_arcus_discount_no_hint(self):
        sp = _spread()
        _feed3(sp, arcus=99.70, sodex=None)
        vs = sp.observe(["BTC-USD"], 1000.0)
        assert vs[0].direction_hint == "none"

    def test_sodex_discount_long_sodex(self):
        sp = _spread()
        _feed3(sp, sodex=99.70, arcus=None)
        vs = sp.observe(["BTC-USD"], 1000.0)
        assert vs[0].direction_hint == "long_sodex"

    def test_sodex_premium_fade_sodex(self):
        sp = _spread()
        _feed3(sp, sodex=100.30, arcus=None)
        vs = sp.observe(["BTC-USD"], 1000.0)
        assert vs[0].direction_hint == "fade_sodex"

    def test_bybit_lagger_no_hint(self):
        """Only reachable under a non-default priority (bybit demoted)."""
        cfg = OracleSpreadConfig(venue_priority=("arcus", "bybit", "sodex"))
        sp = OracleSpread(cfg)
        sp.update("BTC-USD", "arcus", 100.0, 1000.0)
        sp.update("BTC-USD", "bybit", 100.30, 1000.0)
        vs = sp.observe(["BTC-USD"], 1000.0)
        assert len(vs) == 1
        assert vs[0].leader_venue == "arcus"
        assert vs[0].lagger_venue == "bybit"
        assert vs[0].direction_hint == "none"


# ------------------------------------------------------------- verdict fields

class TestVerdictFields:
    def test_signed_spread_lagger_minus_leader(self):
        sp = _spread()
        _feed3(sp, sodex=100.25, arcus=None)
        v = sp.observe(["BTC-USD"], 1000.0)[0]
        assert v.spread_bps == pytest.approx(25.0)
        assert v.leader_price == 100.0
        assert v.lagger_price == 100.25
        assert v.ts_s == 1000.0
        assert v.symbol == "BTC-USD"
        assert v.leader_flipped is False

    def test_verdict_is_frozen_dataclass(self):
        sp = _spread()
        _feed3(sp, sodex=100.25, arcus=None)
        v = sp.observe(["BTC-USD"], 1000.0)[0]
        assert isinstance(v, SpreadVerdict)
        with pytest.raises(Exception):
            v.spread_bps = 0.0


# ------------------------------------------------------------ leader flips

class TestLeaderFlip:
    def test_no_flip_before_min_observations(self):
        cfg = OracleSpreadConfig(ema_alpha=1.0, min_observations=3)
        sp = OracleSpread(cfg)
        sp.update("BTC-USD", "bybit", 100.0, 1000.0)
        sp.update("BTC-USD", "sodex", 100.30, 1000.0)   # +30, n=1
        assert sp.observe(["BTC-USD"], 1000.0)[0].leader_flipped is False
        sp.update("BTC-USD", "sodex", 99.70, 1001.0)    # -30, n=2, sign changed
        assert sp.observe(["BTC-USD"], 1001.0)[0].leader_flipped is False

    def test_flip_fires_on_ema_sign_change_after_min_obs(self):
        cfg = OracleSpreadConfig(ema_alpha=1.0, min_observations=3)
        sp = OracleSpread(cfg)
        sp.update("BTC-USD", "bybit", 100.0, 1000.0)
        sp.update("BTC-USD", "sodex", 99.70, 1000.0)
        sp.observe(["BTC-USD"], 1000.0)                  # n=1
        sp.observe(["BTC-USD"], 1000.0)                  # n=2
        sp.observe(["BTC-USD"], 1000.0)                  # n=3: regime = neg
        sp.update("BTC-USD", "sodex", 100.30, 1001.0)
        v = sp.observe(["BTC-USD"], 1001.0)[0]           # n=4: sign flip
        assert v.leader_flipped is True

    def test_flip_with_default_ema_alpha(self):
        """alpha=0.2: 3x +30bps establishes the regime; -30bps crosses the
        EMA sign on the 7th observation."""
        sp = _spread()
        sp.update("BTC-USD", "bybit", 100.0, 1000.0)
        sp.update("BTC-USD", "sodex", 100.30, 1000.0)
        for _ in range(3):
            sp.observe(["BTC-USD"], 1000.0)              # regime = +1
        sp.update("BTC-USD", "sodex", 99.70, 1001.0)
        flips = [sp.observe(["BTC-USD"], 1001.0)[0].leader_flipped
                 for _ in range(4)]
        assert flips == [False, False, False, True]

    def test_ema_tracks_subthreshold_spreads(self):
        """Spreads below alert_bps never emit verdicts but still feed the EMA."""
        sp = _spread()
        sp.update("BTC-USD", "bybit", 100.0, 1000.0)
        sp.update("BTC-USD", "sodex", 100.10, 1000.0)    # +10bps < alert
        assert sp.observe(["BTC-USD"], 1000.0) == []
        st = sp.state_snapshot()["pair_ema"]["BTC-USD|bybit|sodex"]
        assert st["ema"] == pytest.approx(10.0)
        assert st["n"] == 1

    def test_pair_ema_independent_per_pair(self):
        sp = _spread()
        _feed3(sp, arcus=100.30, sodex=99.70)
        sp.observe(["BTC-USD"], 1000.0)
        snap = sp.state_snapshot()["pair_ema"]
        assert snap["BTC-USD|bybit|arcus"]["n"] == 1
        assert snap["BTC-USD|bybit|arcus"]["ema"] == pytest.approx(30.0)
        assert snap["BTC-USD|bybit|sodex"]["ema"] == pytest.approx(-30.0)


# ------------------------------------------------------------- fail-closed

class TestFailClosed:
    @pytest.mark.parametrize("bad", [0.0, -5.0, float("nan"), float("inf"),
                                     None, "abc", ""])
    def test_bad_prices_ignored(self, bad):
        sp = _spread()
        sp.update("BTC-USD", "bybit", bad, 1000.0)
        assert sp.state_snapshot()["marks"] == {}

    def test_bad_now_s_ignored(self):
        sp = _spread()
        sp.update("BTC-USD", "bybit", 100.0, "not-a-time")
        assert sp.state_snapshot()["marks"] == {}

    def test_observe_garbage_never_raises(self):
        sp = _spread()
        assert sp.observe(None, 1000.0) == []
        assert sp.observe([], 1000.0) == []
        assert sp.observe(["BTC-USD"], None) == []
        assert sp.observe([None, 123, "BTC-USD"], 1000.0) == []

    def test_update_garbage_never_raises(self):
        sp = _spread()
        sp.update(None, None, None, None)
        sp.update("", "", 100.0, 1000.0)
        assert sp.state_snapshot()["marks"] == {}

    def test_observe_recovers_after_defective_symbol(self):
        sp = _spread()
        _feed3(sp, symbol="ETH-USD", sodex=100.5)
        sp._marks[("BROKEN", "bybit")] = (float("nan"), 1000.0)  # injected rot
        vs = sp.observe(["BROKEN", "ETH-USD"], 1000.0)
        assert len(vs) == 1
        assert vs[0].symbol == "ETH-USD"


# ------------------------------------------------------------------ snapshot

class TestStateSnapshot:
    def test_snapshot_shape(self):
        sp = _spread()
        _feed3(sp, sodex=100.3)
        sp.observe(["BTC-USD"], 1000.0)
        snap = sp.state_snapshot()
        assert snap["enabled"] is True
        assert snap["config"]["alert_bps"] == 20.0
        assert snap["config"]["venue_priority"] == ["bybit", "arcus", "sodex"]
        assert "BTC-USD|bybit" in snap["marks"]
        assert "BTC-USD|bybit|sodex" in snap["pair_ema"]

    def test_update_overwrites_latest(self):
        sp = _spread()
        sp.update("BTC-USD", "bybit", 100.0, 1000.0)
        sp.update("BTC-USD", "bybit", 101.0, 1001.0)
        rec = sp.state_snapshot()["marks"]["BTC-USD|bybit"]
        assert rec["price"] == 101.0
        assert rec["ts_s"] == 1001.0
