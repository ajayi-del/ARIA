"""KingdomView Phase 1 pins — harness-wiring Law 2 shared kingdom memory."""

from __future__ import annotations

from pathlib import Path

import pytest

from prop_harness.brains import kingdom_view as kv
from prop_harness.brains import eval_profile as ep


NOW = 1_700_000_000.0
DAY = "2026-11-08"


def _snap(aid="turbo1", sleeve="crypto", n=3, maxp=3, age=5.0,
          asset="BTC", side="buy"):
    positions = [{"asset": f"{asset}{i}" if n > 1 else asset,
                  "side": side, "entry_ref": 100.0, "stop_price": 99.0,
                  "quantity": 1.0, "risk_usd": 5.0,
                  "opened_ts": NOW - age - i}
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
