"""Pins for intelligence/whale_proxy.py — the Governor's WhaleProxy brain.

Zero-I/O: every pin is pure compute or injected callables — NO network.
"""
import pytest

from intelligence.whale_proxy import (
    ProxyScore,
    WhaleProxyFeed,
    account_ratio_score,
    compute,
    funding_discipline_score,
    oi_growth_score,
    proxy_tier,
    ratio_long_share_provider,
    spike_score,
    tier_to_axiom,
)


# ---------------------------------------------------------------- leg bands

class TestFundingDiscipline:
    def test_band_boundaries(self):
        assert funding_discipline_score(2.99) == 0.35
        assert funding_discipline_score(3.0) == 0.25       # < 3 fails at exactly 3
        assert funding_discipline_score(5.99) == 0.25
        assert funding_discipline_score(6.0) == 0.10
        assert funding_discipline_score(9.99) == 0.10
        assert funding_discipline_score(10.0) == 0.0
        assert funding_discipline_score(25.0) == 0.0

    def test_none_abstains(self):
        assert funding_discipline_score(None) is None
        assert funding_discipline_score("x") is None

    def test_structural_cap_bonus(self):
        # avg == max (pinned class) → +0.10, capped at 0.35
        assert funding_discipline_score(5.0, 5.0) == pytest.approx(0.35)
        assert funding_discipline_score(2.0, 2.0) == pytest.approx(0.35)   # capped
        assert funding_discipline_score(8.0, 8.0) == pytest.approx(0.20)
        # within the 0.01bps tolerance
        assert funding_discipline_score(5.0, 5.005) == pytest.approx(0.35)
        # outside it → no bonus
        assert funding_discipline_score(5.0, 5.02) == pytest.approx(0.25)


class TestOIGrowth:
    def test_band_boundaries(self):
        assert oi_growth_score(20.01) == 0.35
        assert oi_growth_score(20.0) == 0.25               # > 20 fails at exactly 20
        assert oi_growth_score(10.01) == 0.25
        assert oi_growth_score(10.0) == 0.15
        assert oi_growth_score(0.01) == 0.15
        assert oi_growth_score(0.0) == 0.05                # > 0 fails at exactly 0
        assert oi_growth_score(-4.99) == 0.05
        assert oi_growth_score(-5.0) == 0.0                # > -5 fails at exactly -5
        assert oi_growth_score(-30.0) == 0.0

    def test_none_abstains(self):
        assert oi_growth_score(None) is None


class TestSpikeScore:
    def test_band_boundaries(self):
        assert spike_score(5.0, 15.01) == 0.0              # ratio > 3 → retail FOMO
        assert spike_score(5.0, 15.0) == 0.10              # exactly 3.0 is NOT > 3
        assert spike_score(5.0, 7.5) == 0.10               # exactly 1.5 is NOT < 1.5
        assert spike_score(5.0, 7.49) == 0.20
        assert spike_score(5.0, 5.0) == 0.20               # flat = sustained

    def test_zero_avg_denominator_guard(self):
        # max(avg, 0.01bps) — never divide by zero
        assert spike_score(0.0, 0.02) == 0.10              # ratio 2.0

    def test_none_abstains(self):
        assert spike_score(None, 5.0) is None
        assert spike_score(5.0, None) is None


class TestAccountRatio:
    def test_dark_neutral(self):
        assert account_ratio_score(None) == 0.05
        assert account_ratio_score("x") == 0.05

    def test_band_boundaries(self):
        assert account_ratio_score(0.61) == 0.10
        assert account_ratio_score(0.6) == 0.07            # > 0.6 fails at exactly 0.6
        assert account_ratio_score(0.51) == 0.07
        assert account_ratio_score(0.5) == 0.02            # > 0.5 fails at exactly 0.5
        assert account_ratio_score(0.2) == 0.02


# ---------------------------------------------------------------- tiers

class TestTiers:
    def test_proxy_tier_boundaries(self):
        assert proxy_tier(0.80) == 1
        assert proxy_tier(0.7999) == 2
        assert proxy_tier(0.60) == 2
        assert proxy_tier(0.5999) == 3
        assert proxy_tier(0.40) == 3
        assert proxy_tier(0.3999) == 4
        assert proxy_tier(0.25) == 4
        assert proxy_tier(0.2499) == 5
        assert proxy_tier(0.0) == 5
        assert proxy_tier(None) == 5

    def test_tier_to_axiom(self):
        assert tier_to_axiom(1) == 1
        assert tier_to_axiom(2) == 2
        assert tier_to_axiom(3) == 3
        assert tier_to_axiom(4) == 0      # watch = no entry
        assert tier_to_axiom(5) == 0      # reject = no entry
        assert tier_to_axiom("x") == 0
        assert tier_to_axiom(None) == 0


# ---------------------------------------------------------------- compute

class TestCompute:
    def test_all_dark_returns_none(self):
        assert compute() is None
        assert compute(None, None, None, None, None) is None

    def test_ratio_alone_cannot_mint_a_score(self):
        # the account-ratio tilt is not evidence
        assert compute(None, None, None, None, 0.7) is None

    def test_dark_oi_renormalizes_not_zeroes(self):
        # 0.25 (funding) + 0.20 (spike) + 0.05 (ratio dark neutral)
        # over available weight 0.65 → 0.7692, NOT 0.50
        s = compute(funding_avg_bps=5.26, funding_max_bps=5.8,
                    funding_min_bps=4.9, oi_growth_pct=None, account_ratio=None)
        assert s.score == pytest.approx(0.7692, abs=1e-4)
        assert s.tier == 2
        assert "oi_growth_dark_renormalized" in s.notes
        assert s.components["oi_growth"] is None
        assert s.components["account_ratio"] == 0.05

    def test_structural_override_fet_calibration(self):
        # FET: avg 10.31bps sustained AT max cap (max 10.4 / min 10.2),
        # OI dark — institutional bid, forced tier 1.
        s = compute(funding_avg_bps=10.31, funding_max_bps=10.4,
                    funding_min_bps=10.2, oi_growth_pct=None, account_ratio=None)
        assert s.score >= 0.80
        assert s.tier == 1
        assert "structural_institutional_bid" in s.notes
        assert tier_to_axiom(s.tier) == 1

    def test_structural_override_requires_tight_spread(self):
        # avg high but max/min spread 6× → spiky, NOT structural
        s = compute(funding_avg_bps=9.0, funding_max_bps=30.0,
                    funding_min_bps=5.0, oi_growth_pct=10.0, account_ratio=None)
        assert "structural_institutional_bid" not in s.notes

    def test_sui_calibration_tier2(self):
        # SUI: avg 5.26, latest 5.73, max 5.8, min 4.9, OI +4.8%, ratio dark
        s = compute(funding_avg_bps=5.26, funding_max_bps=5.8,
                    funding_min_bps=4.9, oi_growth_pct=4.8, account_ratio=None,
                    funding_latest_bps=5.73)
        assert s.score == pytest.approx(0.65)
        assert s.tier == 2
        assert tier_to_axiom(s.tier) == 2

    def test_link_calibration_tier5(self):
        # LINK: avg 4.7 but latest 10.0 AT the cap (fresh retail FOMO spike),
        # OI −11.4% (distribution), ratio dark → reject.
        s = compute(funding_avg_bps=4.7, funding_max_bps=10.0,
                    funding_min_bps=4.7, oi_growth_pct=-11.4, account_ratio=None,
                    funding_latest_bps=10.0)
        assert s.score <= 0.20
        assert s.tier == 5
        assert "retail_fomo_spike_at_cap" in s.notes
        assert tier_to_axiom(s.tier) == 0

    def test_retail_override_needs_fresh_spike(self):
        # latest below the cap → no override even with low avg
        s = compute(funding_avg_bps=4.7, funding_max_bps=5.0,
                    funding_min_bps=4.5, oi_growth_pct=-11.4, account_ratio=None,
                    funding_latest_bps=5.0)
        assert "retail_fomo_spike_at_cap" not in s.notes

    def test_strong_institutional_profile(self):
        # disciplined funding + expanding OI + sustained + crowded longs
        s = compute(funding_avg_bps=2.0, funding_max_bps=2.0,
                    funding_min_bps=2.0, oi_growth_pct=25.0, account_ratio=0.65)
        assert s.score == pytest.approx(1.0)
        assert s.tier == 1

    def test_bearish_profile(self):
        s = compute(funding_avg_bps=12.0, funding_max_bps=40.0,
                    funding_min_bps=10.0, oi_growth_pct=-20.0, account_ratio=0.3)
        assert s.tier == 5
        assert s.score < 0.25


# ---------------------------------------------------------------- feed

class TestWhaleProxyFeed:
    def _sui_funding_raw(self, sym):
        # RAW DECIMAL Bybit rates — the feed converts ×1e4 (UNITS LAW)
        return {"avg": 0.000526, "max": 0.00058, "min": 0.00049,
                "latest": 0.000573}

    def test_raw_rates_converted_to_bps(self):
        feed = WhaleProxyFeed(funding_provider=self._sui_funding_raw,
                              oi_provider=lambda s: 4.8,
                              ratio_provider=lambda s: None)
        s = feed.score("SUIUSDT")
        assert s is not None
        assert s.score == pytest.approx(0.65)
        assert s.tier == 2

    def test_all_planes_dark_returns_none(self):
        feed = WhaleProxyFeed(funding_provider=lambda s: None,
                              oi_provider=lambda s: None,
                              ratio_provider=lambda s: None)
        assert feed.score("BTCUSDT") is None

    def test_no_providers_returns_none(self):
        assert WhaleProxyFeed().score("BTCUSDT") is None

    def test_provider_exceptions_are_dark(self):
        def boom(s):
            raise RuntimeError("plane down")
        feed = WhaleProxyFeed(funding_provider=boom, oi_provider=boom,
                              ratio_provider=boom)
        assert feed.score("BTCUSDT") is None

    def test_partial_funding_dict_legs_abstain(self):
        feed = WhaleProxyFeed(funding_provider=lambda s: {"avg": 0.0002},
                              oi_provider=lambda s: 25.0,
                              ratio_provider=lambda s: None)
        s = feed.score("BTCUSDT")
        assert s is not None
        assert s.components["spike_vs_sustained"] is None   # max absent

    def test_kill_switch_inert(self, monkeypatch):
        monkeypatch.setenv("WHALE_PROXY_ENABLED", "false")
        feed = WhaleProxyFeed(funding_provider=self._sui_funding_raw,
                              oi_provider=lambda s: 4.8,
                              ratio_provider=lambda s: None)
        assert feed.score("SUIUSDT") is None

    def test_ratio_adapter_over_whale_ratio_feed(self):
        class _Reading:
            long_share = 0.65

        class _FakeRatioFeed:
            def get_reading(self, symbol):
                return _Reading() if symbol == "BTCUSDT" else None

        provider = ratio_long_share_provider(_FakeRatioFeed())
        assert provider("BTCUSDT") == 0.65
        assert provider("ETHUSDT") is None

        feed = WhaleProxyFeed(funding_provider=lambda s: {"avg": 0.0002},
                              oi_provider=lambda s: 25.0,
                              ratio_provider=provider)
        s = feed.score("BTCUSDT")
        assert s.components["account_ratio"] == 0.10        # long_share > 0.6
