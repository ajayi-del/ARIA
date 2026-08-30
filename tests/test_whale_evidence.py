"""Whale evidence layer pins (2026-08-30): four-confidence geometric mean,
effective breadth (leviathan cap × venue-cluster deflation), EV-from-samples
k=20 shrinkage. The abstain semantics (None before data) are the contract."""
import math

import pytest

from intelligence.whale_evidence import (SignalEvidence, combined_confidence,
                                         effective_breadth, ev_from_samples)


class TestCombinedConfidence:
    def test_geometric_mean(self):
        c = combined_confidence(0.8, 0.5, 1.0, 0.25)
        assert c == pytest.approx((0.8 * 0.5 * 1.0 * 0.25) ** 0.25)

    def test_missing_legs_skipped(self):
        assert combined_confidence(0.64, None, None, None) == pytest.approx(0.64)
        assert combined_confidence(0.5, 0.5, None, None) == pytest.approx(0.5)

    def test_any_zero_leg_zeroes_all(self):
        assert combined_confidence(0.9, 0.0, 0.9, 0.9) == 0.0

    def test_all_none_is_abstain(self):
        assert combined_confidence(None, None, None, None) is None


class TestEffectiveBreadth:
    def test_empty(self):
        assert effective_breadth([]) == 0.0

    def test_single_whale_is_one(self):
        assert effective_breadth([{"venue": "aster"}]) == pytest.approx(1.0)
        assert effective_breadth(
            [{"venue": "aster", "capital_usd": 50e6}]) == pytest.approx(1.0)

    def test_two_equal_same_venue_deflated(self):
        b = effective_breadth([{"venue": "aster"}, {"venue": "aster"}])
        assert b == pytest.approx(math.sqrt(2))

    def test_two_equal_two_venues_full_breadth(self):
        b = effective_breadth([{"venue": "aster"}, {"venue": "hyperliquid"}])
        assert b == pytest.approx(2.0)

    def test_four_equal_one_venue_is_two(self):
        b = effective_breadth([{"venue": "aster"}] * 4)
        assert b == pytest.approx(2.0)

    def test_leviathan_deflates_cluster(self):
        flows = [{"venue": "aster", "capital_usd": 100.0},
                 {"venue": "aster", "capital_usd": 1.0},
                 {"venue": "aster", "capital_usd": 1.0}]
        b = effective_breadth(flows)
        assert b < 1.2                     # ≈ one risk factor, not three
        assert b < math.sqrt(3)            # strictly below equal-weight case

    def test_leviathan_cap_binds(self):
        # Without the cap the dominant wallet would deflate further; the
        # 40% cap floors the small wallets' contribution.
        flows = [{"venue": "aster", "capital_usd": 1e9},
                 {"venue": "aster", "capital_usd": 1.0},
                 {"venue": "aster", "capital_usd": 1.0}]
        capped = effective_breadth(flows, leviathan_cap_frac=0.40)
        uncapped = effective_breadth(flows, leviathan_cap_frac=1.0)
        assert capped > uncapped
        assert uncapped == pytest.approx(1.0, abs=0.01)

    def test_nan_and_garbage_weights_become_one(self):
        flows = [{"venue": "a", "capital_usd": float("nan")},
                 {"venue": "b", "capital_usd": "not-a-number"},
                 {"venue": "c", "capital_usd": -5}]
        assert effective_breadth(flows) == pytest.approx(3.0)


class TestEvFromSamples:
    def test_none_before_data(self):
        assert ev_from_samples([]) is None
        assert ev_from_samples(None) is None

    def test_shrinkage_toward_prior(self):
        est = ev_from_samples([1.0])           # n=1, k=20
        assert est["n"] == 1
        assert est["ev_R"] == pytest.approx(1.0 / 21.0, abs=1e-4)
        assert est["mean_R"] == 1.0

    def test_large_n_converges_to_sample(self):
        rs = [0.5] * 400
        est = ev_from_samples(rs)
        assert est["ev_R"] == pytest.approx(0.5, abs=0.03)

    def test_p_win_shrunk_toward_half(self):
        est = ev_from_samples([1.0, 1.0, -1.0])
        assert est["p_win"] == pytest.approx((2 + 10) / 23.0, abs=1e-4)

    def test_lower_ci_none_on_thin_sample(self):
        assert ev_from_samples([0.7])["lower_ci_95"] is None

    def test_lower_ci_present(self):
        est = ev_from_samples([0.3] * 100)
        assert est["lower_ci_95"] is not None
        assert est["lower_ci_95"] > 0           # consistent winners → CI > 0

    def test_losing_sample_negative_ci(self):
        est = ev_from_samples([-0.3] * 100)
        assert est["lower_ci_95"] < 0


class TestSignalEvidence:
    def test_to_dict_roundtrip(self):
        ev = SignalEvidence(event_type="tide_consensus", symbol="BTC-USD",
                            direction="long", effective_breadth=1.7,
                            features={"rung": "weak"})
        d = ev.to_dict()
        assert d["event_type"] == "tide_consensus"
        assert d["p_win"] is None               # honest abstain pre-data
        assert d["features"]["rung"] == "weak"
