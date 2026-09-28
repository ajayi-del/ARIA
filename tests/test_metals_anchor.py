"""tests/test_metals_anchor.py — pins for the E5 metals macro-anchor brain.

Every doctrine pinned: the 15% XAUT cap (equity $1000 -> max $150 exposure,
tranches $50 x 3), the 2x leverage cap, no-regime-no-signal, the ratio pair
hysteresis (86 arms, 84.9 does not, 74 exits, the 75-85 band holds), the
copper modifier ladder (+0.15 / -0.20 / 0.0), the 3-day minimum between
anchor direction changes, and the kill switch.
"""
from __future__ import annotations

import pytest

from intelligence.metals_anchor import (
    ANCHOR_MIN_CHANGE_INTERVAL_S,
    PAIR_ENTER_RATIO,
    PAIR_EXIT_RATIO,
    XAUT_CAP_FRACTION,
    XAUT_MAX_LEVERAGE,
    XAUT_TRANCHE_FRACTION,
    XAUT_TRANCHES,
    MetalsVerdict,
    anchor_change_ok,
    copper_regime_modifier,
    metals_anchor_enabled,
    metals_verdict,
    silver_gold_pair,
    tranche_verdict,
    xaut_anchor,
)


# ── Doctrine constants ────────────────────────────────────────────────────────

class TestConstants:
    def test_tranche_math(self):
        assert XAUT_CAP_FRACTION == 0.15
        assert XAUT_TRANCHES == 3
        assert XAUT_TRANCHE_FRACTION == pytest.approx(0.05)
        assert XAUT_MAX_LEVERAGE == 4.0  # Governor amendment 2026-09-26: 2x -> 4x

    def test_pair_band(self):
        assert PAIR_ENTER_RATIO == 85.0
        assert PAIR_EXIT_RATIO == 75.0

    def test_min_change_interval_is_3_days(self):
        assert ANCHOR_MIN_CHANGE_INTERVAL_S == 3.0 * 86400.0


# ── XAUT anchor cap ───────────────────────────────────────────────────────────

class TestXautAnchor:
    def test_15pct_cap_on_1000_equity(self):
        sig = xaut_anchor("long", 1000.0, 0.0)
        assert sig is not None
        assert sig.margin_cap_usd == pytest.approx(150.0)
        assert sig.tranche_size_usd == pytest.approx(50.0)

    def test_leverage_cap_4(self):
        # Governor amendment 2026-09-26: XAUT anchor leverage 2x -> 4x
        sig = xaut_anchor("short", 5000.0, 0.0)
        assert sig.leverage_cap == 4.0

    def test_direction_normalized(self):
        assert xaut_anchor("bullish", 1000.0).direction == "long"
        assert xaut_anchor("BEAR", 1000.0).direction == "short"

    def test_exposure_eats_cap(self):
        sig = xaut_anchor("long", 1000.0, 100.0)
        assert sig.margin_cap_usd == pytest.approx(50.0)

    def test_cap_fully_deployed_stands_down(self):
        assert xaut_anchor("long", 1000.0, 150.0) is None
        assert xaut_anchor("long", 1000.0, 200.0) is None

    def test_no_regime_no_signal(self):
        assert xaut_anchor(None, 1000.0) is None
        assert xaut_anchor("range", 1000.0) is None
        assert xaut_anchor("", 1000.0) is None

    def test_bad_equity_abstains(self):
        assert xaut_anchor("long", None) is None
        assert xaut_anchor("long", 0.0) is None
        assert xaut_anchor("long", -50.0) is None
        assert xaut_anchor("long", "garbage") is None


# ── Tranches ──────────────────────────────────────────────────────────────────

class TestTranches:
    def test_sequence(self):
        assert tranche_verdict(0) == 1
        assert tranche_verdict(1) == 2
        assert tranche_verdict(2) == 3

    def test_fully_deployed(self):
        assert tranche_verdict(3) is None
        assert tranche_verdict(7) is None

    def test_invalid_input_abstains(self):
        assert tranche_verdict(None) is None
        assert tranche_verdict(-1) is None
        assert tranche_verdict("x") is None


# ── Silver/gold pair hysteresis ───────────────────────────────────────────────

class TestSilverGoldPair:
    def test_ratio_86_arms(self):
        sig = silver_gold_pair(silver_price=20.0, gold_price=1720.0,
                               currently_open=False, notional_usd=100.0)
        assert sig is not None
        assert sig.action == "enter"
        assert sig.ratio == pytest.approx(86.0)
        assert sig.silver_side == "long"
        assert sig.gold_side == "short"
        assert sig.silver_notional_usd == sig.gold_notional_usd == 100.0

    def test_ratio_849_does_not_arm(self):
        assert silver_gold_pair(silver_price=20.0, gold_price=1698.0,
                                currently_open=False) is None

    def test_ratio_74_exits_when_open(self):
        sig = silver_gold_pair(silver_price=20.0, gold_price=1480.0,
                               currently_open=True)
        assert sig is not None
        assert sig.action == "exit"
        assert sig.ratio == pytest.approx(74.0)

    def test_hysteresis_band_holds_when_open(self):
        # 80 is inside the 75-85 dead zone: no exit, no re-entry.
        assert silver_gold_pair(silver_price=20.0, gold_price=1600.0,
                                currently_open=True) is None

    def test_boundary_values(self):
        # Exactly 85 does NOT arm (strictly greater); exactly 75 does NOT exit.
        assert silver_gold_pair(20.0, 1700.0, currently_open=False) is None
        assert silver_gold_pair(20.0, 1500.0, currently_open=True) is None

    def test_bad_prices_abstain(self):
        assert silver_gold_pair(None, 1700.0) is None
        assert silver_gold_pair(20.0, None) is None
        assert silver_gold_pair(0.0, 1700.0) is None
        assert silver_gold_pair(20.0, -1.0) is None


# ── Copper modifier ───────────────────────────────────────────────────────────

class TestCopperModifier:
    def test_uptrend(self):
        assert copper_regime_modifier("up") == 0.15
        assert copper_regime_modifier("uptrend") == 0.15

    def test_downtrend(self):
        assert copper_regime_modifier("down") == -0.20
        assert copper_regime_modifier("downtrend") == -0.20

    def test_range(self):
        assert copper_regime_modifier("range") == 0.0

    def test_unknown_abstains_to_zero(self):
        assert copper_regime_modifier(None) == 0.0
        assert copper_regime_modifier("sideways-ish?") == 0.0


# ── Frequency discipline ─────────────────────────────────────────────────────

class TestAnchorChangeOk:
    def test_first_change_allowed(self):
        assert anchor_change_ok(None, 1_800_000_000.0) is True

    def test_three_days_boundary(self):
        now = 1_800_000_000.0
        assert anchor_change_ok(now - 3 * 86400.0, now) is True
        assert anchor_change_ok(now - 3 * 86400.0 + 1, now) is False

    def test_two_days_blocked(self):
        now = 1_800_000_000.0
        assert anchor_change_ok(now - 2 * 86400.0, now) is False

    def test_unknown_clock_defensive(self):
        assert anchor_change_ok(1_800_000_000.0, None) is False
        assert anchor_change_ok(1_800_000_000.0, "garbage") is False


# ── Composite verdict + kill switch ───────────────────────────────────────────

class TestVerdict:
    def test_composite_all_legs(self):
        v = metals_verdict(regime_direction="long", account_equity=1000.0,
                           silver_price=20.0, gold_price=1720.0,
                           pair_notional_usd=100.0, copper_trend="up")
        assert v.xaut_signal is not None
        assert v.silver_gold_pair is not None
        assert v.copper_confidence == 0.15
        assert "xaut_anchor_long" in v.notes
        assert "silver_gold_pair_enter" in v.notes
        assert "copper_risk_on" in v.notes

    def test_composite_abstains_independently(self):
        v = metals_verdict()
        assert v.xaut_signal is None
        assert v.silver_gold_pair is None
        assert v.copper_confidence == 0.0
        assert v.notes == ()

    def test_kill_switch(self, monkeypatch):
        monkeypatch.setenv("METALS_ANCHOR_ENABLED", "false")
        assert metals_anchor_enabled() is False
        v = metals_verdict(regime_direction="long", account_equity=1000.0,
                           silver_price=20.0, gold_price=1720.0,
                           copper_trend="up")
        assert v == MetalsVerdict(notes=("kill_switch",))

    def test_kill_switch_default_true(self, monkeypatch):
        monkeypatch.delenv("METALS_ANCHOR_ENABLED", raising=False)
        assert metals_anchor_enabled() is True
