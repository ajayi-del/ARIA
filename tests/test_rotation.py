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
    aftermath_rotation_verdict, residual_overshoots,
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


# ── cascade aftermath rotation filter ────────────────────────────────────────

def _rmatrix(conf=0.8, lead="large_cap", lag="alt_l1"):
    return SimpleNamespace(
        confidence=conf, leading_category=lead, lagging_category=lag,
    )


def test_verdict_long_into_lagging_blocked():
    v, r = aftermath_rotation_verdict(_rmatrix(), "SOL-USD", "long")
    assert (v, r) == ("blocked", "lagging_knife")


def test_verdict_long_into_leading_allowed():
    v, r = aftermath_rotation_verdict(_rmatrix(), "BTC-USD", "long")
    assert (v, r) == ("allowed", "leading_dip")


def test_verdict_long_neutral_category_allowed():
    # XAUT is commodity_precious — neither leading nor lagging
    v, r = aftermath_rotation_verdict(_rmatrix(), "XAUT-USD", "long")
    assert (v, r) == ("allowed", "neutral_category")


def test_verdict_short_into_leading_blocked():
    v, r = aftermath_rotation_verdict(_rmatrix(), "BTC-USD", "short")
    assert (v, r) == ("blocked", "leading_strength")


def test_verdict_short_into_lagging_allowed():
    v, r = aftermath_rotation_verdict(_rmatrix(), "SOL-USD", "short")
    assert (v, r) == ("allowed", "lagging_fade")


def test_verdict_short_neutral_category_allowed():
    v, r = aftermath_rotation_verdict(_rmatrix(), "XAUT-USD", "short")
    assert (v, r) == ("allowed", "neutral_category")


def test_verdict_abstains():
    # no matrix
    assert aftermath_rotation_verdict(None, "SOL-USD", "long") == ("abstain", "no_matrix")
    # low confidence
    assert aftermath_rotation_verdict(_rmatrix(conf=CONF_MIN - 0.01), "SOL-USD", "long") \
        == ("abstain", "low_confidence")
    # no rotation (both ends unknown)
    assert aftermath_rotation_verdict(_rmatrix(lead="none", lag="none"), "SOL-USD", "long") \
        == ("abstain", "no_rotation")
    # uncategorized symbol
    assert aftermath_rotation_verdict(_rmatrix(), "FAKE-USD", "long") \
        == ("abstain", "uncategorized")
    # unknown direction
    assert aftermath_rotation_verdict(_rmatrix(), "SOL-USD", "flat") \
        == ("abstain", "unknown_direction")


def test_verdict_kill_switch(monkeypatch):
    monkeypatch.setenv("CASCADE_ROTATION_FILTER_ENABLED", "false")
    assert aftermath_rotation_verdict(_rmatrix(), "SOL-USD", "long") \
        == ("abstain", "filter_disabled")


def test_verdict_lagging_alone_blocks():
    # Leading unknown but lagging confirmed — the knife block still applies.
    v, r = aftermath_rotation_verdict(_rmatrix(lead="none"), "SOL-USD", "long")
    assert (v, r) == ("blocked", "lagging_knife")


# ── residual_overshoots ──────────────────────────────────────────────────────

def test_residuals_category_mean():
    pre = {"SOL-USD": 100.0, "AVAX-USD": 100.0, "NEAR-USD": 100.0, "LINK-USD": 100.0}
    cur = {"SOL-USD": 95.0, "AVAX-USD": 98.0, "NEAR-USD": 98.0, "LINK-USD": 97.0}
    res = residual_overshoots(pre, cur)
    # alt_l1 mean = (−0.05 −0.02 −0.02)/3 = −0.03 → SOL overshoots by −0.02
    assert res["SOL-USD"] == pytest.approx(-0.02, abs=1e-9)
    assert res["AVAX-USD"] == pytest.approx(0.01, abs=1e-9)
    assert res["NEAR-USD"] == pytest.approx(0.01, abs=1e-9)
    # LINK is a defi_infra singleton → all-symbol mean fallback (−0.03)
    assert res["LINK-USD"] == pytest.approx(0.0, abs=1e-9)


def test_residuals_uncategorized_falls_back_to_all_mean():
    pre = {"FAKE-USD": 100.0, "BTC-USD": 100.0, "ETH-USD": 100.0}
    cur = {"FAKE-USD": 90.0, "BTC-USD": 99.0, "ETH-USD": 99.0}
    res = residual_overshoots(pre, cur)
    # large_cap mean −0.01 (2 members); all mean −0.04; FAKE residual −0.06
    assert res["BTC-USD"] == pytest.approx(0.0, abs=1e-9)
    assert res["ETH-USD"] == pytest.approx(0.0, abs=1e-9)
    assert res["FAKE-USD"] == pytest.approx(-0.06, abs=1e-9)


def test_residuals_empty_and_bad_prices():
    assert residual_overshoots({}, {}) == {}
    assert residual_overshoots(None, None) == {}
    # non-positive prices skipped
    res = residual_overshoots({"BTC-USD": 0.0, "ETH-USD": 100.0},
                              {"BTC-USD": 90.0, "ETH-USD": 100.0})
    assert "BTC-USD" not in res
    assert res["ETH-USD"] == pytest.approx(0.0, abs=1e-9)


def test_residual_ranking_sign_math():
    # Mirrors the inline sort in _execute_cascade_aftermath:
    # longs take the most-negative residual first; shorts the most-positive.
    residuals = {"SOL-USD": -0.02, "AVAX-USD": 0.01, "NEAR-USD": 0.005}
    confirmed = [("AVAX-USD", 0.5), ("SOL-USD", 0.5), ("NEAR-USD", 0.5)]

    long_sorted = sorted(confirmed, key=lambda cs: 1.0 * residuals.get(cs[0], 0.0))
    assert [s for s, _ in long_sorted] == ["SOL-USD", "NEAR-USD", "AVAX-USD"]

    short_sorted = sorted(confirmed, key=lambda cs: -1.0 * residuals.get(cs[0], 0.0))
    assert [s for s, _ in short_sorted] == ["AVAX-USD", "NEAR-USD", "SOL-USD"]
