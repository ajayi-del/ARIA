"""Tests for intelligence/sizing_decorrelation.py (Q4 one-vol-responder)."""
import json

import pytest

from intelligence import sizing_decorrelation as sd


HYPE = {
    "tape_mult": 0.9,
    "risk_parity": 0.833,
    "conviction": 0.61,
    "will_gate": 0.95,
    "atr_floor_resize": 0.224,
    "coherence_decay": 0.50,
}


class TestHypeIncident:
    """Mandatory pin: today's HYPE waterfall, decorrelated."""

    def test_atr_floor_never_neutralized(self):
        adjusted, _ = sd.decorrelate(HYPE)
        assert adjusted["atr_floor_resize"] == pytest.approx(0.224)

    def test_duplicate_vol_responders_neutralized(self):
        adjusted, audit = sd.decorrelate(HYPE)
        assert adjusted["risk_parity"] == 1.0
        assert adjusted["coherence_decay"] == 1.0
        names = [n["name"] for n in audit["neutralized"]]
        assert "risk_parity" in names
        assert "coherence_decay" in names

    def test_adjusted_composite_more_than_doubles(self):
        adjusted, audit = sd.decorrelate(HYPE)
        raw = sd.composite(HYPE)
        assert raw == pytest.approx(0.9 * 0.833 * 0.61 * 0.95 * 0.224 * 0.50)
        assert sd.composite(adjusted) > 2.0 * raw
        assert audit["adjusted_composite"] > 2.0 * audit["raw_composite"]


class TestFactorGroups:
    def test_same_factor_second_responder_neutralized(self):
        # risk_parity and atr_floor_resize both key on vol; atr_floor first.
        m = {"atr_floor_resize": 0.5, "risk_parity": 0.8}
        adjusted, audit = sd.decorrelate(m)
        assert adjusted["atr_floor_resize"] == pytest.approx(0.5)
        assert adjusted["risk_parity"] == 1.0
        assert audit["neutralized"][0]["shared_factors"] == ["vol"]

    def test_disjoint_factors_both_keep(self):
        # conviction (signal_quality) and atr_floor (vol) share nothing.
        m = {"atr_floor_resize": 0.5, "conviction": 0.6}
        adjusted, audit = sd.decorrelate(m)
        assert adjusted["atr_floor_resize"] == pytest.approx(0.5)
        assert adjusted["conviction"] == pytest.approx(0.6)
        assert audit["neutralized"] == []

    def test_partial_overlap_neutralizes_whole_multiplier(self):
        # tape_mult {vol, trend} after atr_floor {vol}: vol already claimed.
        m = {"atr_floor_resize": 0.5, "tape_mult": 0.9}
        adjusted, _ = sd.decorrelate(m)
        assert adjusted["tape_mult"] == 1.0

    def test_signal_quality_group(self):
        m = {"conviction": 0.6, "will_gate": 0.9}
        adjusted, audit = sd.decorrelate(m)
        assert adjusted["conviction"] == pytest.approx(0.6)
        assert adjusted["will_gate"] == 1.0


class TestPriority:
    def test_priority_order_determines_keeper(self):
        m = {"atr_floor_resize": 0.224, "risk_parity": 0.833}
        fwd, _ = sd.decorrelate(m, priority=["atr_floor_resize", "risk_parity"])
        assert fwd["atr_floor_resize"] == pytest.approx(0.224)
        assert fwd["risk_parity"] == 1.0
        rev, _ = sd.decorrelate(m, priority=["risk_parity", "atr_floor_resize"])
        assert rev["risk_parity"] == pytest.approx(0.833)
        assert rev["atr_floor_resize"] == 1.0

    def test_names_not_in_priority_walked_after(self):
        m = {"unknown_knob": 0.5, "risk_parity": 0.8, "atr_floor_resize": 0.4}
        adjusted, _ = sd.decorrelate(m, priority=["atr_floor_resize"])
        # priority winner claims vol; risk_parity neutralized; unknown untouched
        assert adjusted["atr_floor_resize"] == pytest.approx(0.4)
        assert adjusted["risk_parity"] == 1.0
        assert adjusted["unknown_knob"] == pytest.approx(0.5)


class TestBoostNonClaiming:
    def test_boost_does_not_claim_factor(self):
        # A boost >= 1.0 sharing a factor must not silence the discount.
        m = {"whale_tac": 1.25, "emerging_trend": 1.25, "etf_tide": 0.9}
        adjusted, audit = sd.decorrelate(m)
        assert adjusted["whale_tac"] == pytest.approx(1.25)
        assert adjusted["emerging_trend"] == pytest.approx(1.25)
        # trend factor still unclaimed by boosts -> etf_tide discount keeps
        assert adjusted["etf_tide"] == pytest.approx(0.9)
        assert audit["neutralized"] == []

    def test_exactly_one_neither_claims_nor_neutralizes(self):
        m = {"risk_parity": 1.0, "atr_floor_resize": 0.5}
        adjusted, _ = sd.decorrelate(m)
        assert adjusted["risk_parity"] == 1.0
        assert adjusted["atr_floor_resize"] == pytest.approx(0.5)


class TestIdentity:
    def test_all_ones_is_identity(self):
        m = {k: 1.0 for k in HYPE}
        adjusted, audit = sd.decorrelate(m)
        assert sd.composite(adjusted) == pytest.approx(1.0)
        assert audit["neutralized"] == []
        final, audit2 = sd.apply(600.0, m)
        assert final == pytest.approx(600.0)
        assert audit2["final_size"] == pytest.approx(600.0)


class TestAuditContent:
    def test_audit_fields(self):
        _, audit = sd.decorrelate(HYPE)
        assert set(audit) >= {"priority", "keepers", "claimed",
                              "neutralized", "raw_composite",
                              "adjusted_composite"}
        n = audit["neutralized"][0]
        assert set(n) >= {"name", "value", "shared_factors", "claimed_by",
                          "reason"}
        assert n["reason"] == "factor_already_claimed"
        assert audit["claimed"].get("vol") == "atr_floor_resize"

    def test_apply_audit_carries_sizes(self):
        final, audit = sd.apply(600.0, HYPE)
        assert audit["base_size"] == pytest.approx(600.0)
        assert audit["final_size"] == pytest.approx(final)
        assert final == pytest.approx(600.0 * sd.composite(
            sd.decorrelate(HYPE)[0]))


class TestJsonlLogging:
    def test_log_audit_writes_record(self, tmp_path, monkeypatch):
        path = tmp_path / "sizing_decorrelation.jsonl"
        monkeypatch.setattr(sd, "_LOG_PATH", path)
        adjusted, audit = sd.decorrelate(HYPE)
        sd.log_audit("HYPE-USD", HYPE, adjusted, audit)
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["symbol"] == "HYPE-USD"
        assert rec["raw_composite"] == pytest.approx(sd.composite(HYPE))
        assert rec["adjusted_composite"] == pytest.approx(
            sd.composite(adjusted))
        assert "risk_parity" in rec["neutralized"]
        assert rec["adjusted"]["atr_floor_resize"] == pytest.approx(0.224)
        assert "ts" in rec


class TestFailSilence:
    def test_log_audit_never_raises(self, tmp_path, monkeypatch):
        # Point at a directory -> open() fails -> must not raise.
        monkeypatch.setattr(sd, "_LOG_PATH", tmp_path)
        sd.log_audit("X", HYPE, HYPE, {})

    def test_decorrelate_garbage_values(self):
        m = {"risk_parity": "bad", "atr_floor_resize": None, "x": float("nan")}
        adjusted, audit = sd.decorrelate(m)  # must not raise
        assert isinstance(adjusted, dict)

    def test_decorrelation_enabled_bool_and_silent(self, monkeypatch):
        # kill_switch import/read failure path -> False, never raises.
        import builtins
        real_import = builtins.__import__

        def boom(name, *a, **k):
            if name == "intelligence.kill_switch" or (
                    name == "intelligence" and "kill_switch" in
                    (k.get("fromlist") or [])):
                raise ImportError("boom")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", boom)
        assert sd.decorrelation_enabled() is False

    def test_decorrelation_enabled_reads_kill_switch(self, monkeypatch):
        from intelligence import kill_switch
        monkeypatch.setattr(kill_switch, "enabled", lambda name: True)
        assert sd.decorrelation_enabled() is True
        monkeypatch.setattr(kill_switch, "enabled", lambda name: False)
        assert sd.decorrelation_enabled() is False
