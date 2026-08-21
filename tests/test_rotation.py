"""Rotation laggard-catch-up modifier (2026-08-21, operator directive — live).

Book grounding: Murphy (leaders move first, laggards catch up), Chan (trade
the residual, only while the sector factor is confirmed), Clenow (never buy a
broken trend — absolute-momentum floor), Ilmanen (intraday reversal edge is
small — cap the modifier), Aronson (measure it from day one).
"""
from types import SimpleNamespace

import pytest

from intelligence.rotation import (
    laggard_boosts, BOOST_CAP, GAP_MIN, CONF_MIN, ASSET_FLOOR,
)
from intelligence.coherence import CoherenceEngine


def _matrix(conf=0.8, lead="large_cap", cat=None, assets=None):
    return SimpleNamespace(
        confidence=conf,
        leading_category=lead,
        category_scores=cat if cat is not None else {"large_cap": 0.015},
        asset_scores=assets if assets is not None
        else {"BTC-USD": 0.018, "ETH-USD": 0.008},
    )


# ── laggard_boosts ───────────────────────────────────────────────────────────

def test_laggard_in_leading_category_boosted():
    boosts = laggard_boosts(_matrix(), ["BTC-USD", "ETH-USD"])
    # ETH gap = 0.015 − 0.008 = 0.007 → 0.35; BTC gap = −0.003 → none
    assert boosts == {"ETH-USD": pytest.approx(0.35, abs=1e-4)}


def test_leader_gets_no_boost():
    boosts = laggard_boosts(_matrix(), ["BTC-USD"])
    assert boosts == {}


def test_cap_respected():
    m = _matrix(cat={"large_cap": 0.05}, assets={"BTC-USD": 0.05, "ETH-USD": 0.01})
    boosts = laggard_boosts(m, ["ETH-USD"])
    assert boosts["ETH-USD"] == BOOST_CAP


def test_low_confidence_no_boost():
    assert laggard_boosts(_matrix(conf=CONF_MIN - 0.01), ["ETH-USD"]) == {}


def test_no_leading_category_no_boost():
    assert laggard_boosts(_matrix(lead="none"), ["ETH-USD"]) == {}
    assert laggard_boosts(_matrix(lead="confused"), ["ETH-USD"]) == {}


def test_unconfirmed_sector_factor_no_boost():
    # Chan: category score must be positive — never catch a laggard in a
    # falling sector.
    m = _matrix(cat={"large_cap": -0.01}, assets={"BTC-USD": -0.008, "ETH-USD": -0.02})
    assert laggard_boosts(m, ["ETH-USD"]) == {}


def test_gap_floor():
    # gap 0.004 < GAP_MIN 0.005 → no boost
    m = _matrix(cat={"large_cap": 0.012}, assets={"BTC-USD": 0.012, "ETH-USD": 0.008})
    assert laggard_boosts(m, ["ETH-USD"]) == {}


def test_clenow_broken_trend_floor():
    # ETH deeply negative even though the gap is large — falling knife, no boost
    m = _matrix(cat={"large_cap": 0.02}, assets={"BTC-USD": 0.02, "ETH-USD": ASSET_FLOOR - 0.001})
    assert laggard_boosts(m, ["ETH-USD"]) == {}


def test_wrong_category_no_boost():
    # SOL is alt_l1, not large_cap
    m = _matrix(assets={"BTC-USD": 0.018, "ETH-USD": 0.008, "SOL-USD": 0.001})
    boosts = laggard_boosts(m, ["SOL-USD"])
    assert boosts == {}


def test_none_matrix_and_empty_inputs():
    assert laggard_boosts(None, ["ETH-USD"]) == {}
    assert laggard_boosts(_matrix(), []) == {}


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("ROTATION_MODIFIER_ENABLED", "false")
    assert laggard_boosts(_matrix(), ["ETH-USD"]) == {}


# ── coherence Tier 10 reader ─────────────────────────────────────────────────

def _ctx(boosts):
    return SimpleNamespace(rotation_boosts=boosts, signal_weights={})


def test_tier10_adds_component_and_raw():
    eng = CoherenceEngine()
    _, raw, comps = eng.calculate_weighted_score(
        "ETH-USD", {"macro_bias": "bullish"}, market_context=_ctx({"ETH-USD": 0.4})
    )
    assert comps["rotation_laggard"] == pytest.approx(0.4)
    assert raw >= 1


def test_tier10_bearish_bias_zeroes_boost():
    eng = CoherenceEngine()
    _, raw, comps = eng.calculate_weighted_score(
        "ETH-USD", {"macro_bias": "bearish"}, market_context=_ctx({"ETH-USD": 0.4})
    )
    assert comps["rotation_laggard"] == 0.0
    assert raw == 0


def test_tier10_cap_enforced():
    eng = CoherenceEngine()
    _, _, comps = eng.calculate_weighted_score(
        "ETH-USD", {"macro_bias": "bullish"}, market_context=_ctx({"ETH-USD": 0.9})
    )
    assert comps["rotation_laggard"] == 0.5


def test_tier10_absent_context_is_zero():
    eng = CoherenceEngine()
    _, raw, comps = eng.calculate_weighted_score("ETH-USD", {}, market_context=None)
    assert comps["rotation_laggard"] == 0.0
    assert raw == 0
