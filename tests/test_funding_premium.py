"""tests/test_funding_premium.py — hard pins for intelligence/funding_premium.py.

House idiom: cfg is a plain namespace; every method honors the
funding_premium_enabled master gate (False → verdicts None, carry None).
"""
from types import SimpleNamespace

import pytest

from intelligence.funding_premium import FundingPremiumBrain


@pytest.fixture
def brain():
    return FundingPremiumBrain()


@pytest.fixture
def cfg():
    return SimpleNamespace()


@pytest.fixture
def cfg_off():
    return SimpleNamespace(funding_premium_enabled=False)


# ── hourly_from_8h ─────────────────────────────────────────────────────────

def test_hourly_from_8h_arithmetic(brain):
    assert brain.hourly_from_8h(0.0008) == pytest.approx(0.0001)
    assert brain.hourly_from_8h(-0.04) == pytest.approx(-0.005)
    assert brain.hourly_from_8h(0.0) == 0.0


def test_hourly_from_8h_degenerate_none(brain):
    assert brain.hourly_from_8h(None) is None
    assert brain.hourly_from_8h("junk") is None
    assert brain.hourly_from_8h(float("nan")) is None
    assert brain.hourly_from_8h(float("inf")) is None


# ── carry_accrual_usd ──────────────────────────────────────────────────────

def test_carry_accrual_obob_worked_example(brain, cfg):
    # $280 notional at 0.01%/hour x 4h = $0.112 — pin exact.
    out = brain.carry_accrual_usd(cfg, notional_usd=280.0,
                                  hourly_rate=0.0001, hours=4.0)
    assert out == pytest.approx(0.112)


def test_carry_accrual_negative_rate_is_income(brain, cfg):
    out = brain.carry_accrual_usd(cfg, notional_usd=320.0,
                                  hourly_rate=-0.04, hours=1.0)
    assert out == pytest.approx(-12.8)          # negative = RECEIVED


def test_carry_accrual_degenerate_none(brain, cfg):
    assert brain.carry_accrual_usd(cfg, notional_usd=None,
                                   hourly_rate=0.0001, hours=4) is None
    assert brain.carry_accrual_usd(cfg, notional_usd=100.0,
                                   hourly_rate=None, hours=4) is None
    assert brain.carry_accrual_usd(cfg, notional_usd=100.0,
                                   hourly_rate=0.0001, hours=None) is None
    assert brain.carry_accrual_usd(cfg, notional_usd=0.0,
                                   hourly_rate=0.0001, hours=4) is None
    assert brain.carry_accrual_usd(cfg, notional_usd=-50.0,
                                   hourly_rate=0.0001, hours=4) is None
    assert brain.carry_accrual_usd(cfg, notional_usd=100.0,
                                   hourly_rate=0.0001, hours=-1) is None


# ── premium_bps ────────────────────────────────────────────────────────────

def test_premium_bps_worked_example(brain):
    # premium = (b − o) − (o − a) = b + a − 2o = 2 x (mid − oracle).
    # bid 100.06 / ask 100.02 / oracle 100.0 → 0.06 − (−0.02) = 0.08 → 8 bps.
    out = brain.premium_bps(impact_bid=100.06, impact_ask=100.02,
                            oracle=100.0)
    assert out == pytest.approx(8.0)


def test_premium_bps_negative_side(brain):
    out = brain.premium_bps(impact_bid=99.98, impact_ask=99.94,
                            oracle=100.0)
    # (−0.02) − (0.06) = −0.08 → −8 bps
    assert out == pytest.approx(-8.0)


def test_premium_bps_degenerate_none(brain):
    assert brain.premium_bps(impact_bid=100.06, impact_ask=100.02,
                             oracle=None) is None
    assert brain.premium_bps(impact_bid=None, impact_ask=100.02,
                             oracle=100.0) is None
    assert brain.premium_bps(impact_bid=100.06, impact_ask=100.02,
                             oracle=0.0) is None
    assert brain.premium_bps(impact_bid=-1.0, impact_ask=100.02,
                             oracle=100.0) is None


# ── premium_flow_verdict ───────────────────────────────────────────────────

def test_premium_flow_buy_pressure(brain, cfg):
    out = brain.premium_flow_verdict(cfg, impact_bid=100.08,
                                     impact_ask=100.02, oracle=100.0)
    # premium = 0.08 − (−0.02) = 0.10 → 10 bps ≥ 5 bps knob → buy_pressure
    assert out is not None
    assert out["side"] == "buy_pressure"
    assert out["premium_bps"] == pytest.approx(10.0)


def test_premium_flow_sell_pressure_mirror(brain, cfg):
    out = brain.premium_flow_verdict(cfg, impact_bid=99.98,
                                     impact_ask=99.92, oracle=100.0)
    # premium = (−0.02) − (0.08) = −0.10 → −10 bps → sell_pressure
    assert out is not None
    assert out["side"] == "sell_pressure"
    assert out["premium_bps"] == pytest.approx(-10.0)


def test_premium_flow_boundary_exactly_at_knob_binds(brain, cfg):
    # DEFINED boundary: |premium_bps| >= knob → spike (>= binds).
    # Exact-binary inputs: premium = 0.0625 + 0.0 = 0.0625 → exactly 6.25 bps.
    cfg.funding_premium_spike_bps = 6.25
    out = brain.premium_flow_verdict(cfg, impact_bid=100.0625,
                                     impact_ask=100.0, oracle=100.0)
    assert out["premium_bps"] == pytest.approx(6.25)
    assert out["side"] == "buy_pressure"
    # one tick above the premium → neutral
    cfg.funding_premium_spike_bps = 6.2501
    out = brain.premium_flow_verdict(cfg, impact_bid=100.0625,
                                     impact_ask=100.0, oracle=100.0)
    assert out["side"] == "neutral"


def test_premium_flow_neutral_below_knob(brain, cfg):
    out = brain.premium_flow_verdict(cfg, impact_bid=100.02,
                                     impact_ask=100.00, oracle=100.0)
    # premium = 0.02 − 0.00 = 0.02 → 2 bps < 5 → neutral
    assert out["side"] == "neutral"
    assert out["premium_bps"] == pytest.approx(2.0)


def test_premium_flow_dark_inputs_abstain(brain, cfg):
    assert brain.premium_flow_verdict(cfg, impact_bid=None,
                                      impact_ask=100.02,
                                      oracle=100.0) is None
    assert brain.premium_flow_verdict(cfg, impact_bid=100.06,
                                      impact_ask=100.02,
                                      oracle=None) is None


# ── funding_extreme_verdict ────────────────────────────────────────────────

def test_funding_extreme_longs_crowded(brain, cfg):
    out = brain.funding_extreme_verdict(cfg, symbol="LINK-USD",
                                        hourly_rate=0.0009)
    assert out["verdict"] == "longs_crowded"
    assert out["blocks_new_longs"] is True
    assert out["carry_side"] == "short"
    assert out["symbol"] == "LINK-USD"


def test_funding_extreme_shorts_crowded(brain, cfg):
    out = brain.funding_extreme_verdict(cfg, symbol="LINK-USD",
                                        hourly_rate=-0.0009)
    assert out["verdict"] == "shorts_crowded"
    assert out["blocks_new_shorts"] is True
    assert out["carry_side"] == "long"


def test_funding_extreme_exactly_at_knob_is_normal(brain, cfg):
    # STRICTLY-greater semantics: exactly at 0.0008 → normal.
    out = brain.funding_extreme_verdict(cfg, symbol="LINK-USD",
                                        hourly_rate=0.0008)
    assert out["verdict"] == "normal"
    assert "blocks_new_longs" not in out
    out = brain.funding_extreme_verdict(cfg, symbol="LINK-USD",
                                        hourly_rate=-0.0008)
    assert out["verdict"] == "normal"


def test_funding_extreme_none_rate_abstains(brain, cfg):
    assert brain.funding_extreme_verdict(cfg, symbol="LINK-USD",
                                         hourly_rate=None) is None


# ── funding_flip ───────────────────────────────────────────────────────────

def test_funding_flip_long_signal(brain, cfg):
    assert brain.funding_flip(cfg, rates_window=[0.0001, -0.0002]) == \
        "long_signal"


def test_funding_flip_short_signal(brain, cfg):
    assert brain.funding_flip(cfg, rates_window=[-0.0001, 0.0002]) == \
        "short_signal"


def test_funding_flip_no_flip_same_sign(brain, cfg):
    assert brain.funding_flip(cfg, rates_window=[0.0001, 0.0003]) is None
    assert brain.funding_flip(cfg, rates_window=[-0.0001, -0.0003]) is None


def test_funding_flip_insufficient_samples(brain, cfg):
    assert brain.funding_flip(cfg, rates_window=[-0.0002]) is None
    assert brain.funding_flip(cfg, rates_window=[]) is None
    assert brain.funding_flip(cfg, rates_window=None) is None


def test_funding_flip_zero_crossing_rule(brain, cfg):
    # PINNED RULE: zero is non-directional; the flip compares the latest
    # against the most recent NON-ZERO prior print.
    # [+, 0, −] → prior non-zero is + → long_signal
    assert brain.funding_flip(
        cfg, rates_window=[0.0001, 0.0, -0.0002]) == "long_signal"
    # [−, 0, +] → prior non-zero is − → short_signal
    assert brain.funding_flip(
        cfg, rates_window=[-0.0001, 0.0, 0.0002]) == "short_signal"
    # [0, 0, −] → no non-zero prior → no established side → None
    assert brain.funding_flip(cfg, rates_window=[0.0, 0.0, -0.0002]) is None
    # [+, −, −] → prior non-zero is − (the latest prior) → same sign → None
    assert brain.funding_flip(
        cfg, rates_window=[0.0001, -0.0001, -0.0002]) is None
    # latest == 0 → zero is never a flip
    assert brain.funding_flip(cfg, rates_window=[0.0001, 0.0]) is None


def test_funding_flip_min_samples_knob(brain, cfg):
    cfg.funding_flip_min_samples = 3
    assert brain.funding_flip(cfg, rates_window=[0.0001, -0.0002]) is None
    assert brain.funding_flip(
        cfg, rates_window=[0.0001, 0.0002, -0.0003]) == "long_signal"


def test_funding_flip_dark_entries_dropped(brain, cfg):
    # non-numeric entries never vote; [+, dark, −] still flips.
    assert brain.funding_flip(
        cfg, rates_window=[0.0001, None, -0.0002]) == "long_signal"


# ── Master gate ────────────────────────────────────────────────────────────

def test_master_gate_false_all_none(brain, cfg_off):
    assert brain.carry_accrual_usd(cfg_off, notional_usd=280.0,
                                   hourly_rate=0.0001, hours=4) is None
    assert brain.premium_flow_verdict(cfg_off, impact_bid=100.08,
                                      impact_ask=100.02,
                                      oracle=100.0) is None
    assert brain.funding_extreme_verdict(cfg_off, symbol="LINK-USD",
                                         hourly_rate=0.0009) is None
    assert brain.funding_flip(cfg_off,
                              rates_window=[0.0001, -0.0002]) is None
