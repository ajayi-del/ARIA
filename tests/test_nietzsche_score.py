"""P4 Nietzsche conviction composite pins (Governor 2026-09-29: "PHILOSOPHY
CAN ALSO WORK HERE KANT AND NEITSCHE FOR BOTH SIZING AND STRUCTURE" +
"ALSO BUIILD P4").

Brain: intelligence/nietzsche_score.py (zero-I/O). Fixed denominator
Σw=1.0 — missing inputs contribute 0, reproducing the Governor's worked
examples (ETH at market = 0.25+0.09+0+0.06 = 0.40 → half size; the $2,657
pullback floors N3 at 0.85 → 0.57 → 0.75×). Direction mirroring for
shorts. N1+N3+N4 crowding family ×0.75 when all three agree — one
crowding read never triple-counted.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence import nietzsche_score as nz  # noqa: E402


def _main_src() -> str:
    return open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "main.py")).read()


class TestN1Whale:
    def test_anchors(self):
        assert nz.n1_whale(1.0, "long") == 0.0
        assert nz.n1_whale(1.5, "long") == 0.5
        assert nz.n1_whale(2.0, "long") == 0.75
        assert nz.n1_whale(2.5, "long") == 1.0

    def test_interp_and_clamp(self):
        assert nz.n1_whale(1.75, "long") == 0.625
        assert nz.n1_whale(3.0, "long") == 1.0       # clamp above 2.5
        assert nz.n1_whale(0.9, "long") == 0.0       # crowd short, bad long

    def test_short_mirror(self):
        # Crowd long 2.5:1 = zero conviction for OUR short.
        assert nz.n1_whale(2.5, "short") == 0.0
        # Crowd short 0.5 = ratio 2.0 on the mirror → 0.75.
        assert nz.n1_whale(0.5, "short") == 0.75

    def test_degenerate_abstains(self):
        assert nz.n1_whale("bad", "long") is None
        assert nz.n1_whale(0, "long") is None
        assert nz.n1_whale(-1.5, "long") is None
        assert nz.n1_whale(None, "long") is None


class TestN2OI:
    def test_anchors(self):
        assert nz.n2_oi(-3.0) == 0.0
        assert nz.n2_oi(-10.0) == 0.0                # declining
        assert nz.n2_oi(3.0) == 0.4
        assert nz.n2_oi(5.0) == 0.7
        assert nz.n2_oi(10.0) == 1.0
        assert nz.n2_oi(13.5) == 1.0                 # clamp (BTC +13.5%/30d)

    def test_interp(self):
        # 4.3333... → 0.6 — the Governor's ETH worked-example N2 leg.
        assert abs(nz.n2_oi(4.3333333) - 0.6) < 1e-3

    def test_degenerate(self):
        assert nz.n2_oi(None) is None
        assert nz.n2_oi("flat") is None


class TestN3Funding:
    def test_bands_long(self):
        assert nz.n3_funding(0.0090, "long") == 0.0      # crowded (ETH today)
        assert nz.n3_funding(0.0075, "long") == 0.0
        assert nz.n3_funding(0.004, "long") == 0.5
        assert nz.n3_funding(0.001, "long") == 0.85
        assert nz.n3_funding(0.0, "long") == 1.0
        assert nz.n3_funding(-0.002, "long") == 1.0      # shorts pay us

    def test_interp(self):
        # BTC 0.0028% → 0.64 between the 0.001/0.004 anchors.
        assert abs(nz.n3_funding(0.0028, "long") - 0.64) < 1e-3

    def test_limit_floor(self):
        # Pullback-at-structure is exempt from the crowding penalty.
        assert nz.n3_funding(0.0090, "long", entry_type="limit") == 0.85
        assert nz.n3_funding(0.004, "long", entry_type="limit") == 0.85
        # A rate already above the floor is untouched.
        assert nz.n3_funding(0.0, "long", entry_type="limit") == 1.0

    def test_short_mirror(self):
        # Positive funding = longs pay = crowded longs = good short.
        assert nz.n3_funding(0.0090, "short") == 1.0
        assert nz.n3_funding(-0.0090, "short") == 0.0

    def test_degenerate(self):
        assert nz.n3_funding(None, "long") is None


class TestN4FearGreed:
    def test_steps_long(self):
        assert nz.n4_fear_greed(10, "long") == 1.0
        assert nz.n4_fear_greed(24, "long") == 1.0
        assert nz.n4_fear_greed(25, "long") == 0.8
        assert nz.n4_fear_greed(44, "long") == 0.8
        assert nz.n4_fear_greed(45, "long") == 0.6
        assert nz.n4_fear_greed(54, "long") == 0.6
        assert nz.n4_fear_greed(55, "long") == 0.4
        assert nz.n4_fear_greed(73, "long") == 0.4        # today's Greed print
        assert nz.n4_fear_greed(75, "long") == 0.2
        assert nz.n4_fear_greed(89, "long") == 0.2
        assert nz.n4_fear_greed(90, "long") == 0.0

    def test_short_mirror(self):
        # Greed 73 → 27 on the mirror → 0.8 conviction for shorts.
        assert nz.n4_fear_greed(73, "short") == 0.8
        assert nz.n4_fear_greed(10, "short") == 0.0       # extreme fear, bad short

    def test_degenerate(self):
        assert nz.n4_fear_greed(None, "long") is None


class TestN5Macro:
    def test_long_table(self):
        assert nz.n5_macro(1.5, 1.5, "long") == 1.0       # both > +1%
        assert nz.n5_macro(0.5, 0.5, "long") == 0.7       # both > 0
        assert nz.n5_macro(0.5, -0.5, "long") == 0.4      # mixed
        assert nz.n5_macro(-0.5, 0.5, "long") == 0.2      # tech < 0
        assert nz.n5_macro(-1.6, 0.5, "long") == 0.0      # tech < -1.5%

    def test_short_mirror(self):
        assert nz.n5_macro(-1.6, -1.6, "short") == 1.0    # both < -1%
        assert nz.n5_macro(1.6, 1.6, "short") == 0.0      # tech ripping, bad short
        assert nz.n5_macro(-0.5, -0.5, "short") == 0.7

    def test_dark_leg_abstains(self):
        assert nz.n5_macro(None, 1.0, "long") is None
        assert nz.n5_macro(1.0, None, "long") is None


class TestN6ETFFlow:
    def test_long(self):
        assert nz.n6_etf_flow(200_000_000, "long") == 1.0    # accumulation
        assert nz.n6_etf_flow(-200_000_000, "long") == 0.0   # distribution
        assert nz.n6_etf_flow(0, "long") == 0.5              # neutral
        assert nz.n6_etf_flow(149_999_999, "long") == 0.5    # below materiality

    def test_short_mirror(self):
        assert nz.n6_etf_flow(-200_000_000, "short") == 1.0
        assert nz.n6_etf_flow(200_000_000, "short") == 0.0

    def test_degenerate(self):
        assert nz.n6_etf_flow(None, "long") is None


class TestSizeScalarLadder:
    def test_rungs(self):
        assert nz.size_scalar(0.0) == 0.0
        assert nz.size_scalar(0.2999) == 0.0
        assert nz.size_scalar(0.30) == 0.5
        assert nz.size_scalar(0.4999) == 0.5
        assert nz.size_scalar(0.50) == 0.75
        assert nz.size_scalar(0.70) == 1.0
        assert nz.size_scalar(0.8499) == 1.0
        assert nz.size_scalar(0.85) == 1.25
        assert nz.size_scalar(1.0) == 1.25

    def test_degenerate_passes_through(self):
        assert nz.size_scalar(None) == 1.0


class TestCompositeWorkedExamples:
    def test_eth_at_market_040(self):
        # The Governor's worked example: ETH long at market, funding
        # 0.0090% crowded → N3 zeroed. 0.25+0.09+0+0.06 = 0.40 → half size.
        v = nz.conviction_score(
            "long", whale_ratio=2.5, oi_growth_pct=4.3333333,
            funding_rate_pct=0.0090, fear_greed=73)
        assert v["signals"]["n1"] == 1.0
        assert abs(v["signals"]["n2"] - 0.6) < 1e-3
        assert v["signals"]["n3"] == 0.0
        assert v["signals"]["n4"] == 0.4
        assert abs(v["score"] - 0.40) < 1e-3
        assert v["size_scalar"] == 0.5
        assert v["standdown"] is False
        assert v["crowding_capped"] is False     # family disagrees (1.0/0/0.4)

    def test_eth_pullback_limit_restores_n3(self):
        # Same tape, limit at $2,657: N3 floors at 0.85 → 0.57 → 0.75×.
        v = nz.conviction_score(
            "long", whale_ratio=2.5, oi_growth_pct=4.3333333,
            funding_rate_pct=0.0090, fear_greed=73, entry_type="limit")
        assert v["signals"]["n3"] == 0.85
        assert abs(v["score"] - 0.57) < 1e-3
        assert v["size_scalar"] == 0.75

    def test_btc_full_conviction(self):
        # BTC long: 1.5:1 whale, OI +13.5%, funding 0.0028%, F&G 73,
        # macro both >+1%, ETF +$200M → ~0.713 → full size.
        v = nz.conviction_score(
            "long", whale_ratio=1.5, oi_growth_pct=13.5,
            funding_rate_pct=0.0028, fear_greed=73,
            tech_day_pct=1.5, mag7_day_pct=1.5,
            etf_flow_3d_usd=200_000_000)
        assert v["signals"]["n3"] == 0.64
        assert abs(v["score"] - 0.713) < 1e-3
        assert v["size_scalar"] == 1.0
        assert v["crowding_capped"] is False     # n1 0.5 breaks agreement

    def test_empty_evidence_standdown(self):
        # Every plane dark: score 0 → standdown. Fixed denominator never
        # invents conviction.
        v = nz.conviction_score("long")
        assert v["score"] == 0.0
        assert v["standdown"] is True
        assert v["size_scalar"] == 0.0
        assert all(s is None for s in v["signals"].values())

    def test_side_normalized(self):
        v = nz.conviction_score("SHORT", whale_ratio=2.5)
        assert v["side"] == "short"
        assert v["signals"]["n1"] == 0.0


class TestCrowdingFamilyCap:
    def test_agreement_high_caps(self):
        # n1 0.75 / n3 1.0 / n4 1.0 — all ≥0.6: one crowding read
        # triple-counted otherwise → family contribution ×0.75.
        v = nz.conviction_score(
            "long", whale_ratio=2.0, funding_rate_pct=0.0, fear_greed=20)
        raw = 0.25 * 0.75 + 0.20 * 1.0 + 0.15 * 1.0       # 0.5375
        assert v["crowding_capped"] is True
        assert abs(v["score"] - raw * 0.75) < 1e-3        # 0.4031

    def test_cap_off_is_legacy(self):
        v = nz.conviction_score(
            "long", whale_ratio=2.0, funding_rate_pct=0.0, fear_greed=20,
            crowding_cap=False)
        assert v["crowding_capped"] is False
        assert abs(v["score"] - 0.5375) < 1e-3

    def test_agreement_low_also_caps(self):
        # All three ≤0.4 (crowd against us on every read): same cap.
        v = nz.conviction_score(
            "long", whale_ratio=1.1, funding_rate_pct=0.0090, fear_greed=95)
        assert v["signals"]["n1"] == 0.1
        assert v["crowding_capped"] is True
        assert abs(v["score"] - 0.25 * 0.1 * 0.75) < 1e-3
        assert v["standdown"] is True

    def test_disagreement_never_caps(self):
        # n1 high, n3/n4 low — mixed evidence is not a triple-count.
        v = nz.conviction_score(
            "long", whale_ratio=2.5, funding_rate_pct=0.0090, fear_greed=90)
        assert v["crowding_capped"] is False

    def test_partial_family_never_caps(self):
        # A dark leg breaks the family read — no cap without all three.
        v = nz.conviction_score(
            "long", whale_ratio=2.0, funding_rate_pct=0.0)   # n4 missing
        assert v["crowding_capped"] is False


class TestConfigKnobs:
    def test_defaults(self):
        from core.config import Settings
        s = Settings()
        assert s.nietzsche_score_enabled is True
        assert s.nietzsche_score_symbols == "BTC-USD,ETH-USD,SOL-USD,XAUT-USD"
        assert s.nietzsche_standdown_enabled is True
        assert s.nietzsche_standdown_lt == 0.3
        assert s.nietzsche_crowding_cap_enabled is True
        assert s.nietzsche_fng_stale_h == 48.0
        assert s.nietzsche_fleet_standdown_enabled is True


class TestWiringPins:
    def test_sizing_chain_splice(self):
        src = _main_src()
        assert "signal_rejected_nietzsche_conviction" in src
        assert "nietzsche_conviction_sized" in src
        assert "nietzsche_conviction_standdown_shadow" in src
        assert "nietzsche_score=(round(_nz_score, 3)" in src
        assert '("nietzsche", locals().get("_nz_mult"))' in src

    def test_kill_switch_legacy_path(self):
        src = _main_src()
        assert ('if not getattr(config, "nietzsche_score_enabled", True):\n'
                "            return None") in src

    def test_feed_sharing_splice(self):
        # The axiom loop's WhaleRatioFeed/OIHistoryFeed are shared read-only
        # with the Nietzsche closure — Nietzsche never fetches.
        src = _main_src()
        assert "_nz_shared_feeds.update(_ax_feeds)" in src

    def test_fleet_standdown_splice(self):
        src = _main_src()
        assert "anticipator_nietzsche_standdown" in src
        assert "nietzsche_fleet_standdown_enabled" in src
