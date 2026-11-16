"""Pins for cluster-loser cut brain (shadow default OFF).

Specimen (Vultr 2026-11-16): turbo BTC short + FT XYZ100 short both red
same side — crypto_comove never sees stocks; zero cluster stamps in
journals. This brain must detect the macro-β adverse cluster and propose
loser-only actions without touching winners or actuating.
"""

from __future__ import annotations

import time

from prop_harness.brains import cluster_loser_cut as clc
from prop_harness.config import PropConfig


def _leg(asset, side, *, upnl=-1.0, r_moved=None, be=False, released=None,
         neg_since=None, opened=None, army="a1", sleeve="crypto",
         peak_r=0.0, risk=7.0, i=0):
    now = time.time()
    return {
        "semantic_id": f"sid-{asset}-{i}",
        "status": "open",
        "asset": asset,
        "side": side,
        "family": None,  # normalize via family_of
        "risk_usd": risk,
        "last_upnl": upnl,
        "r_moved": r_moved,
        "be_armed": be,
        "risk_released": released,
        "opened_ts": opened if opened is not None else now - 1200,
        "upnl_negative_since": (
            neg_since if neg_since is not None else now - 900),
        "mfe_peak_r": peak_r,
        "army_id": army,
        "sleeve": sleeve,
        "quantity": "1",
        "entry_ref": "100",
        "stop_price": "101" if side == "sell" else "99",
    }


class TestKillSwitchAndDefaults:
    def test_config_default_off(self):
        cfg = PropConfig(enabled=True, state_root="/tmp/x")
        assert cfg.cluster_loser_cut_enabled is False
        assert cfg.cluster_loser_cut_live is False

    def test_disabled_returns_none(self):
        now = time.time()
        legs = [
            _leg("BTC", "sell", upnl=-5.0, army="turbo"),
            _leg("xyz:XYZ100", "sell", upnl=-2.0, army="ft", sleeve="tradfi"),
        ]
        assert clc.cluster_loser_verdict(False, legs, now=now) is None


class TestMacroBetaCluster:
    def test_btc_xyz100_same_side_adverse_detects(self):
        now = time.time()
        legs = [
            _leg("BTC", "sell", upnl=-5.7, be=True, army="turbo",
                 sleeve="crypto", neg_since=now - 600),
            _leg("xyz:XYZ100", "sell", upnl=-2.6, be=True, army="ft",
                 sleeve="tradfi", neg_since=now - 600),
            _leg("HYPE", "sell", upnl=5.0, army="turbo", sleeve="crypto"),
        ]
        detail = clc.cluster_loser_verdict(
            True, legs, min_n=2, underwater_s=300.0, now=now)
        assert detail is not None
        assert detail["event"] == clc.CLUSTER_LOSER_WOULD_CUT
        assert detail["cluster_key"] == "sell|macro_beta"
        assert detail["n_adverse"] == 2
        assert detail["cross_sleeve"] is True
        assert detail["shadow"] is True
        assert detail["actuation"] == "forbidden"
        assets = {m["asset"] for m in detail["members"]}
        assert assets == {"BTC", "xyz:XYZ100"}
        # Winner spared
        assert detail["n_winners_spared"] == 1
        actions = {a["asset"]: a["action"] for a in detail["actions"]}
        assert actions["BTC"] == "would_tighten"
        assert actions["xyz:XYZ100"] == "would_tighten"

    def test_winner_never_in_actions(self):
        now = time.time()
        legs = [
            _leg("BTC", "sell", upnl=-3.0, army="t1", neg_since=now - 600),
            _leg("ETH", "sell", upnl=-1.0, army="t1", neg_since=now - 600),
            _leg("HYPE", "sell", upnl=4.0, army="t1"),
        ]
        detail = clc.cluster_loser_verdict(True, legs, now=now)
        assert detail is not None
        assert all(a["asset"] != "HYPE" for a in detail["actions"])
        assert all(m["asset"] != "HYPE" for m in detail["members"])

    def test_opposite_sides_do_not_cluster(self):
        now = time.time()
        legs = [
            _leg("BTC", "sell", upnl=-5.0, army="a", neg_since=now - 600),
            _leg("xyz:XYZ100", "buy", upnl=-2.0, army="b", sleeve="tradfi",
                 neg_since=now - 600),
        ]
        assert clc.cluster_loser_verdict(True, legs, now=now) is None

    def test_ttl_gate_cold_when_fresh(self):
        now = time.time()
        legs = [
            _leg("BTC", "sell", upnl=-5.0, army="a", neg_since=now - 30),
            _leg("ETH", "sell", upnl=-2.0, army="a", neg_since=now - 20),
        ]
        assert clc.cluster_loser_verdict(
            True, legs, underwater_s=300.0, now=now) is None

    def test_r_moved_fallback_for_peer_legs(self):
        now = time.time()
        # Kingdom-view peers often lack last_upnl — r_moved < 0 counts.
        btc = _leg("BTC", "sell", upnl=None, r_moved=-0.13, army="turbo",
                   neg_since=now - 600)
        xyz = _leg("xyz:XYZ100", "sell", upnl=None, r_moved=-0.06, army="ft",
                   sleeve="tradfi", neg_since=now - 600)
        detail = clc.cluster_loser_verdict(True, [btc, xyz], now=now)
        assert detail is not None
        assert detail["n_adverse"] == 2


class TestActions:
    def test_be_armed_tightens(self):
        assert clc.proposed_action({"be_armed": True}) == "would_tighten"

    def test_released_harvests(self):
        assert clc.proposed_action(
            {"be_armed": False, "risk_released": 10.0}) == "would_harvest"

    def test_plain_reduce(self):
        assert clc.proposed_action(
            {"be_armed": False, "risk_released": 0}) == "would_reduce"


class TestCollectLegs:
    def test_dedup_own_army_prefers_local(self):
        local = [_leg("BTC", "sell", upnl=-5.0, army="acct1")]
        peers = [{
            "asset": "BTC", "side": "sell", "army_id": "acct1",
            "entry_ref": 1, "stop_price": 2, "r_moved": -0.1,
            "be_armed": False, "opened_ts": time.time() - 1000,
            "status": "open",
        }, {
            "asset": "xyz:XYZ100", "side": "sell", "army_id": "ft",
            "sleeve": "tradfi", "entry_ref": 1, "stop_price": 2,
            "r_moved": -0.2, "be_armed": True,
            "opened_ts": time.time() - 1000, "last_upnl": -2.0,
            "upnl_negative_since": time.time() - 600, "status": "open",
        }]
        legs = clc.collect_legs(local, peers, own_account_id="acct1",
                                own_sleeve="crypto")
        assets = {(l["army_id"], l["asset"]) for l in legs}
        assert ("acct1", "BTC") in assets
        assert ("ft", "xyz:XYZ100") in assets
        # Only one BTC
        assert sum(1 for l in legs if l["asset"] == "BTC") == 1


class TestJournalThrottle:
    def test_cooldown(self):
        now = 1000.0
        assert clc.should_journal(None, now, cooldown_s=60) is True
        assert clc.should_journal(940.0, now, cooldown_s=60) is True  # 60s ago
        assert clc.should_journal(970.0, now, cooldown_s=60) is False  # 30s ago
