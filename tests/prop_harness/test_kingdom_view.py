"""KingdomView Phase 1 pins — harness-wiring Law 2 shared kingdom memory."""

from __future__ import annotations

from pathlib import Path

import pytest

from prop_harness.brains import kingdom_view as kv
from prop_harness.brains import eval_profile as ep


NOW = 1_700_000_000.0
DAY = "2026-11-08"


def _snap(aid="turbo1", sleeve="crypto", n=3, maxp=3, age=5.0,
          asset="BTC", side="buy", r_moved=1.2, be_armed=True):
    positions = [{"asset": f"{asset}{i}" if n > 1 else asset,
                  "side": side, "entry_ref": 100.0, "stop_price": 99.0,
                  "quantity": 1.0, "risk_usd": 5.0,
                  "opened_ts": NOW - age - i,
                  "be_armed": be_armed, "r_moved": r_moved}
                 for i in range(n)]
    if n == 1:
        positions[0]["asset"] = asset
    return {
        "ts": NOW - age,
        "account_id": aid,
        "sleeve": sleeve,
        "max_positions": maxp,
        "positions": positions,
    }


class TestBuildKingdomView:
    def test_schema_and_complete(self):
        armies = {
            "turbo1": _snap("turbo1", n=1),
            "turbo2": _snap("turbo2", sleeve="tradfi", n=1, asset="xyz:NVDA"),
        }
        view = kv.build_kingdom_view(armies, now=NOW, expected_ids=list(armies),
                                     day=DAY)
        assert view["schema"] == 1
        assert view["ts"] == NOW
        assert view["incomplete"] is False
        assert set(view["armies"]) == {"turbo1", "turbo2"}
        assert "BTC:buy:2026-11-08" in view["thesis_slots"]
        assert view["slot_economy"]["by_army"]["turbo1"]["free"] == 2
        # positions + R exposed for overflow freshness / sniper evidence
        leg = view["armies"]["turbo1"]["positions"][0]
        assert leg["be_armed"] is True
        assert leg["r_moved"] == pytest.approx(1.2)
        assert any(e["asset"] == "BTC" and e["r_moved"] == pytest.approx(1.2)
                   for e in view["evidence_legs"])

    def test_missing_expected_marks_incomplete(self):
        armies = {"turbo1": _snap("turbo1"), "turbo2": None}
        view = kv.build_kingdom_view(
            armies, now=NOW, expected_ids=["turbo1", "turbo2"], day=DAY)
        assert view["incomplete"] is True
        assert "turbo1" in view["armies"]
        assert "turbo2" not in view["armies"]

    def test_empty_armies_incomplete(self):
        view = kv.build_kingdom_view({}, now=NOW, expected_ids=[])
        assert view["incomplete"] is True
        assert view["armies"] == {}
        assert view["thesis_slots"] == {}

    def test_loss_veto_passthrough(self):
        veto = {"ts": NOW, "stats": {"majors:buy": {"n": 5, "wr": 0.2}}}
        view = kv.build_kingdom_view(
            {"demo": _snap("demo", sleeve="all", n=0)},
            now=NOW, loss_veto=veto, expected_ids=["demo"], day=DAY)
        assert view["loss_veto"] is veto

    def test_thesis_first_holder_wins(self):
        a = _snap("turbo1", n=1, asset="ETH", side="sell")
        b = _snap("demo", sleeve="all", n=1, asset="ETH", side="sell")
        view = kv.build_kingdom_view(
            {"turbo1": a, "demo": b}, now=NOW,
            expected_ids=["turbo1", "demo"], day=DAY)
        slot = view["thesis_slots"]["ETH:sell:2026-11-08"]
        assert slot["army_id"] == "turbo1"
        assert slot["status"] == "held"

    def test_heartbeat_fold(self):
        snap = _snap("turbo1", n=0)
        hb = {"ts": NOW, "budget": {"remaining": 30.0}, "dd": {"band": "OK"},
              "enabled": True}
        row = kv.fold_army_row(snap, hb)
        assert row["budget"]["remaining"] == 30.0
        assert row["dd"]["band"] == "OK"
        assert row["enabled"] is True
        assert row["heartbeat_ts"] == NOW


class TestPublishFailOpen:
    def test_write_fault_returns_false(self):
        def boom(path, obj):
            raise OSError("disk full")

        assert kv.publish_kingdom_view(
            Path("/tmp/x.json"), {"schema": 1}, write_json=boom) is False

    def test_write_ok(self, tmp_path):
        wrote = {}

        def capture(path, obj):
            wrote["path"] = path
            wrote["obj"] = obj

        ok = kv.publish_kingdom_view(
            tmp_path / "prop_kingdom_view.json",
            {"schema": 1, "ts": NOW}, write_json=capture)
        assert ok is True
        assert wrote["obj"]["schema"] == 1


class TestCollectArmies:
    def test_loads_positions_and_heartbeat(self, tmp_path):
        root = tmp_path / "turbo1"
        root.mkdir()
        snap = _snap("turbo1", n=1)
        (root / "prop_positions.json").write_text(
            __import__("json").dumps(snap))
        (root / "heartbeat.json").write_text(
            __import__("json").dumps(
                {"ts": NOW, "account_id": "turbo1",
                 "budget": {"day": DAY}, "dd": {"band": "OK"}}))

        def rj(path, default=None):
            try:
                return __import__("json").loads(Path(path).read_text())
            except OSError:
                return default

        armies, expected = kv.collect_armies([root], read_json=rj)
        assert expected == ["turbo1"]
        assert armies["turbo1"]["budget"]["day"] == DAY
        assert len(armies["turbo1"]["positions"]) == 1

    def test_missing_peer_yields_none_row(self, tmp_path):
        missing = tmp_path / "ghost"

        def rj(path, default=None):
            return default

        armies, expected = kv.collect_armies([missing], read_json=rj)
        assert expected == ["ghost"]
        assert armies["ghost"] is None


class TestResolveOverflowSnaps:
    def test_disabled_uses_legacy(self):
        legacy = [_snap("turbo1")]
        out = kv.resolve_overflow_snaps(
            view_enabled=False, view={"schema": 1, "ts": NOW, "armies": {}},
            legacy_snaps=legacy, now=NOW, stale_s=90.0)
        assert out is legacy

    def test_missing_view_falls_back_legacy(self):
        legacy = [_snap("turbo1")]
        out = kv.resolve_overflow_snaps(
            view_enabled=True, view=None, legacy_snaps=legacy,
            now=NOW, stale_s=90.0)
        assert out is legacy

    def test_stale_view_standby_empty(self):
        view = {
            "schema": 1, "ts": NOW - 500,
            "armies": {"turbo1": _snap("turbo1", age=500)},
        }
        out = kv.resolve_overflow_snaps(
            view_enabled=True, view=view, legacy_snaps=[_snap("turbo1")],
            now=NOW, stale_s=90.0)
        assert out == []
        assert ep.overflow_verdict(out, False, NOW) is False

    def test_fresh_view_excludes_self(self):
        view = {
            "schema": 1, "ts": NOW,
            "incomplete": False,
            "armies": {
                "demo": _snap("demo", sleeve="all", n=0),
                "turbo1": _snap("turbo1", n=3),
            },
        }
        out = kv.resolve_overflow_snaps(
            view_enabled=True, view=view, legacy_snaps=[],
            now=NOW, stale_s=90.0, own_account_id="demo")
        assert len(out) == 1
        assert out[0]["account_id"] == "turbo1"
        assert ep.overflow_verdict(out, False, NOW) is True

    def test_incomplete_standby_even_if_peers_full(self):
        # incomplete:true → standby even when a covering army looks full
        # (missing expected peer must not double-fire via partial view)
        view = {
            "schema": 1, "ts": NOW, "incomplete": True,
            "armies": {"turbo1": _snap("turbo1", n=3)},
        }
        out = kv.resolve_overflow_snaps(
            view_enabled=True, view=view, legacy_snaps=[_snap("turbo1")],
            now=NOW, stale_s=90.0)
        assert out == []
        assert ep.overflow_verdict(out, False, NOW) is False

    def test_bad_schema_standby(self):
        out = kv.resolve_overflow_snaps(
            view_enabled=True, view={"schema": 99, "ts": NOW, "armies": {}},
            legacy_snaps=[_snap("turbo1")], now=NOW, stale_s=90.0)
        assert out == []


class TestPeerPathsFor:
    def test_self_plus_overflow_peers(self, tmp_path):
        self_root = tmp_path / "demo"
        self_root.mkdir()
        peer = tmp_path / "turbo1" / "prop_positions.json"
        peer.parent.mkdir()
        peer.write_text("{}")
        paths = kv.peer_paths_for(
            state_root=self_root,
            kingdom_view_peers="",
            overflow_peers=str(peer))
        assert paths[0] == self_root
        assert paths[1] == peer

    def test_kingdom_peers_override_overflow_list(self, tmp_path):
        self_root = tmp_path / "demo"
        self_root.mkdir()
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        paths = kv.peer_paths_for(
            state_root=self_root,
            kingdom_view_peers=f"{a},{b}",
            overflow_peers="/should/not/use")
        assert [p.name for p in paths] == ["demo", "a", "b"]


class TestThesisId:
    def test_shape(self):
        assert kv.thesis_id("BTC", "buy", day="2026-11-08") == "BTC:buy:2026-11-08"


class TestNormalizePositionR:
    def test_fills_missing_r_fields(self):
        raw = {"asset": "ETH", "side": "sell", "entry_ref": "200",
               "stop_price": "210", "quantity": "1", "risk_usd": 5,
               "opened_ts": NOW}
        norm = kv.normalize_position(raw)
        assert norm["be_armed"] is False
        assert norm["r_moved"] is None
        assert norm["entry_ref"] == 200.0

    def test_bad_leg_dropped(self):
        assert kv.normalize_position({"asset": "X"}) is None
        assert kv.normalize_position(None) is None


class TestSniperEvidenceFailOpen:
    def _view(self, age=5.0, army_age=5.0, incomplete=False):
        snap = _snap("turbo1", n=1, age=army_age, r_moved=1.5)
        return {
            "schema": 1,
            "ts": NOW - age,
            "incomplete": incomplete,
            "armies": {"turbo1": snap},
            "evidence_legs": [],
        }

    def test_fresh_returns_snaps_with_r(self):
        snaps = kv.sniper_evidence_snaps(self._view(), now=NOW)
        assert len(snaps) == 1
        assert snaps[0]["positions"][0]["r_moved"] == pytest.approx(1.5)
        assert snaps[0]["positions"][0]["be_armed"] is True

    def test_stale_view_empty(self):
        # fail-open: empty evidence, never raises — sniper disarms
        assert kv.sniper_evidence_snaps(
            self._view(age=500.0), now=NOW, stale_s=120.0) == []

    def test_missing_view_empty(self):
        assert kv.sniper_evidence_snaps(None, now=NOW) == []

    def test_bad_schema_empty(self):
        assert kv.sniper_evidence_snaps(
            {"schema": 99, "ts": NOW, "armies": {}}, now=NOW) == []

    def test_incomplete_still_yields_present_armies(self):
        # sniper fail-open: partial evidence OK (overflow would standby)
        snaps = kv.sniper_evidence_snaps(
            self._view(incomplete=True), now=NOW)
        assert len(snaps) == 1

    def test_stale_army_filtered_keep_fresh_peer(self):
        view = {
            "schema": 1, "ts": NOW, "incomplete": False,
            "armies": {
                "stale": _snap("stale", n=1, age=500.0, asset="SOL"),
                "fresh": _snap("fresh", n=1, age=5.0, asset="BTC",
                               r_moved=2.0),
            },
        }
        snaps = kv.sniper_evidence_snaps(view, now=NOW, stale_s=120.0)
        assert len(snaps) == 1
        assert snaps[0]["account_id"] == "fresh"

    def test_legs_helper_fail_open(self):
        assert kv.sniper_evidence_legs(None, now=NOW) == []
        legs = kv.sniper_evidence_legs(self._view(), now=NOW)
        assert legs[0]["r_moved"] == pytest.approx(1.5)
        assert legs[0]["army_id"] == "turbo1"

    def test_read_path_io_fault_empty(self, tmp_path):
        def boom(path, default=None):
            raise OSError("nope")

        assert kv.read_sniper_evidence(
            tmp_path / "missing.json", now=NOW, read_json=boom) == []

    def test_overflow_stale_still_fail_closed(self):
        # asymmetry pin: overflow standby ≠ sniper empty-ok semantics share
        # the same stale clock but different consumer law
        view = self._view(age=500.0)
        legacy = [_snap("turbo1")]
        assert kv.resolve_overflow_snaps(
            view_enabled=True, view=view, legacy_snaps=legacy,
            now=NOW, stale_s=90.0) == []
        assert kv.sniper_evidence_snaps(
            view, now=NOW, stale_s=120.0) == []
