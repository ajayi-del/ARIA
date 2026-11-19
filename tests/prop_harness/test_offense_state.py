"""Pins: state-gated offense brain (ATS-197/198, 2026-11-19).

Pure-module only — no executor/main actuation required for these pins.
Kill-OFF / dark-state paths fail OPEN. R4/R5 never bind without loosen GO.
Scrape / R0–R1 never scale up. Sticky apex stands neighbour down under twin.
"""

from __future__ import annotations

from prop_harness.brains import offense_state as ofs


def _state(**kw):
    base = {
        "box": {"equity": 5000.0, "headroom": 120.0, "soft_box": 150.0,
                "h": 0.80, "band": "ACTIVE", "breached": False},
        "budget": {"day_budget": 120.0, "committed": 0.0, "realized": 0.0,
                   "risk_remaining": 100.0},
        "book": {"open": [], "n_open": 0},
        "rung": {"rung_id": "R3", "size_mult": 1.0, "would_rung": None,
                 "scrape_day": False, "h": 0.80},
        "cooloff": {"post_loss": {}, "post_win": {}},
        "company": {"twin": [], "opposite": []},
        "phase": "P2",
    }
    for k, v in kw.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = {**base[k], **v}
        else:
            base[k] = v
    return base


class TestCapitalScale:
    def test_kill_off_fail_open(self):
        d = ofs.capital_scale_for_state(_state(), enabled=False)
        assert d["size_mult"] == 1.0 and d["reason"] == "kill_off"

    def test_dark_state_fail_open(self):
        d = ofs.capital_scale_for_state({"error": "boom"})
        assert d["size_mult"] == 1.0 and "fail_open" in d["reason"]
        d = ofs.capital_scale_for_state(None)
        assert d["size_mult"] == 1.0

    def test_scrape_never_scales_up(self):
        d = ofs.capital_scale_for_state(_state(
            rung={"rung_id": "R1", "size_mult": 0.70, "scrape_day": True,
                  "would_rung": "R5", "h": 0.95},
            phase="P0"))
        assert d["size_mult"] <= 0.70
        assert d["scrape_day"] is True
        # would_rung must not bind
        assert d["size_mult"] <= 1.0

    def test_r0_deny(self):
        d = ofs.capital_scale_for_state(_state(
            rung={"rung_id": "R0", "size_mult": 0.0, "scrape_day": False,
                  "h": 0.1}))
        assert d["size_mult"] == 0.0
        assert d["rem_cap_frac"] == 0.0

    def test_r4_r5_stamp_only_without_go(self):
        st = _state(rung={"rung_id": "R3", "size_mult": 1.0,
                          "would_rung": "R5", "scrape_day": False, "h": 0.96})
        d = ofs.capital_scale_for_state(st, loosen_enabled=False)
        assert d["size_mult"] == 1.0
        assert d["would_rung"] == "R5"
        d2 = ofs.capital_scale_for_state(st, loosen_enabled=True)
        assert d2["size_mult"] == 1.30
        assert d2["reason"] == "loosen_go"

    def test_twin_hot_caps_mult(self):
        d = ofs.capital_scale_for_state(_state(
            company={"twin": [{"asset": "BTC", "side": "buy"}],
                     "opposite": []},
            rung={"rung_id": "R3", "size_mult": 1.0, "would_rung": "R5",
                  "scrape_day": False, "h": 0.96}),
            loosen_enabled=True)
        assert d["size_mult"] <= 1.0
        assert d["twin_hot"] is True

    def test_rem_clip_respects_frac(self):
        st = _state(rung={"rung_id": "R1", "size_mult": 0.70,
                          "scrape_day": True, "h": 0.4},
                    budget={"day_budget": 50.0, "committed": 0.0,
                            "realized": -30.0, "risk_remaining": 20.0},
                    phase="P0")
        risk, d = ofs.apply_rem_clip(14.0, st)
        assert risk <= 14.0 * 0.70
        assert risk <= 20.0 * d["rem_cap_frac"]


class TestOffenseActGate:
    def test_size_up_refused_on_scrape(self):
        ok, d = ofs.offense_act_allowed(
            _state(rung={"rung_id": "R1", "size_mult": 0.7,
                         "scrape_day": True, "h": 0.4}, phase="P0"),
            "size_up")
        assert ok is False
        assert "scrape" in d["reason"]

    def test_partial_allowed_on_scrape(self):
        ok, _ = ofs.offense_act_allowed(
            _state(rung={"rung_id": "R1", "size_mult": 0.7,
                         "scrape_day": True, "h": 0.4}),
            "partial", is_apex_seat=True)
        assert ok is True

    def test_harvest_refused_on_scrape(self):
        ok, d = ofs.offense_act_allowed(
            _state(rung={"rung_id": "R1", "size_mult": 0.7,
                         "scrape_day": True, "h": 0.4}),
            "harvest", is_apex_seat=True)
        assert ok is False
        assert "scrape" in d["reason"]

    def test_sticky_apex_stands_neighbour(self):
        st = _state(company={"twin": [{"asset": "BTC"}], "opposite": []})
        ok, d = ofs.offense_act_allowed(
            st, "partial", asset="BTC", is_apex_seat=False,
            sticky_apex_enabled=True)
        assert ok is False
        assert d["reason"] == "sticky_apex_standdown"
        ok2, _ = ofs.offense_act_allowed(
            st, "partial", asset="BTC", is_apex_seat=True)
        assert ok2 is True

    def test_dark_fail_open(self):
        ok, d = ofs.offense_act_allowed(None, "harvest")
        assert ok is True and "fail_open" in d["reason"]


class TestStickyApex:
    def test_no_twin_is_apex(self):
        v = ofs.sticky_apex_verdict(_state(), asset="BTC",
                                    seat_is_primary=False)
        assert v["is_apex"] is True
        assert v["reason"] == "no_twin"

    def test_peer_bank_proof_wins(self):
        st = _state(company={"twin": [{"asset": "BTC"}], "opposite": []})
        v = ofs.sticky_apex_verdict(
            st, asset="BTC", seat_is_primary=True,
            own_mfe_r=0.6, peer_mfe_r=2.5)
        assert v["is_apex"] is False
        assert v["reason"] == "peer_bank_proof"

    def test_primary_owns_when_tied(self):
        st = _state(company={"twin": [{"asset": "LINK"}], "opposite": []})
        v = ofs.sticky_apex_verdict(
            st, asset="LINK", seat_is_primary=True,
            own_mfe_r=1.2, peer_mfe_r=1.1)
        assert v["is_apex"] is True


class TestTrailAfterMfe:
    def test_below_arm_blocks(self):
        ok, d = ofs.trail_after_mfe_ok(
            {"mfe_peak_r": 0.2}, _state(), arm_r=0.5)
        assert ok is False
        assert d["reason"] == "mfe_below_arm"

    def test_mid_proof_allows(self):
        ok, _ = ofs.trail_after_mfe_ok(
            {"mfe_peak_r": 0.8}, _state(), arm_r=0.5)
        assert ok is True

    def test_kill_off_fail_open(self):
        ok, d = ofs.trail_after_mfe_ok(
            {"mfe_peak_r": 0.1}, _state(), enabled=False)
        assert ok is True and d["reason"] == "kill_off"


class TestRPartial:
    def test_nearest_unpaid_rung(self):
        plan = ofs.r_partial_verdict(
            {"asset": "BTC", "quantity": 10.0, "mfe_peak_r": 1.2,
             "status": "open"},
            _state(), is_apex_seat=True)
        assert plan is not None
        assert plan["trigger_r"] == 0.5
        assert abs(plan["reduce_qty"] - 2.5) < 1e-9

    def test_skips_paid_rungs(self):
        plan = ofs.r_partial_verdict(
            {"asset": "BTC", "quantity": 8.0, "mfe_peak_r": 1.5,
             "status": "open", "offense_partial_rungs": [0.5]},
            _state(), is_apex_seat=True)
        assert plan is not None
        assert plan["trigger_r"] == 1.0

    def test_neighbour_twin_standdown(self):
        st = _state(company={"twin": [{"asset": "BTC"}], "opposite": []})
        plan = ofs.r_partial_verdict(
            {"asset": "BTC", "quantity": 10.0, "mfe_peak_r": 2.0,
             "status": "open"},
            st, is_apex_seat=False, sticky_apex_enabled=True)
        assert plan is None

    def test_kill_off_noop(self):
        assert ofs.r_partial_verdict(
            {"asset": "BTC", "quantity": 10.0, "mfe_peak_r": 2.0},
            _state(), enabled=False) is None

    def test_parse_rungs(self):
        assert ofs.parse_partial_rungs("0.5:0.3,1.0:0.2") == (
            (0.5, 0.3), (1.0, 0.2))
        assert ofs.parse_partial_rungs("") == ofs.DEFAULT_PARTIAL_RUNGS
