"""Tests for intelligence/squeeze_radar.py — absolute-positioning squeeze radar."""
import math

import pytest

from intelligence.squeeze_radar import (
    ENV_KILL_SWITCH,
    SqueezeRadar,
    SqueezeRadarConfig,
    SqueezeSnapshot,
    squeeze_radar_enabled,
)


def snap(symbol="IRYS-USD", mark=1.0, funding=None, oi_usd=None, oi_growth_3h=None,
         supply_usd=None, long_short_ratio=None, volume_spike=None,
         spread_bps=None, depth_usd_10bps=None, listing_age_days=None,
         macro_window=False):
    return SqueezeSnapshot(
        symbol=symbol, mark_price=mark, funding_rate=funding, oi_usd=oi_usd,
        oi_growth_3h=oi_growth_3h, supply_usd=supply_usd,
        long_short_ratio=long_short_ratio, volume_spike=volume_spike,
        spread_bps=spread_bps, depth_usd_10bps=depth_usd_10bps,
        listing_age_days=listing_age_days, macro_window=macro_window,
        now_s=0.0,
    )


@pytest.fixture
def radar():
    return SqueezeRadar(SqueezeRadarConfig())


# ── kill switch ───────────────────────────────────────────────────────────────

class TestKillSwitch:
    def test_default_enabled(self, monkeypatch):
        monkeypatch.delenv(ENV_KILL_SWITCH, raising=False)
        assert squeeze_radar_enabled() is True

    def test_disabled_returns_empty(self, monkeypatch, radar):
        monkeypatch.setenv(ENV_KILL_SWITCH, "false")
        out = radar.scan([snap(funding=-9e-5)])
        assert out == []

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", "FALSE"])
    def test_false_spellings(self, monkeypatch, val):
        monkeypatch.setenv(ENV_KILL_SWITCH, val)
        assert squeeze_radar_enabled() is False

    def test_read_at_call_time(self, monkeypatch, radar):
        monkeypatch.setenv(ENV_KILL_SWITCH, "false")
        assert radar.scan([snap(funding=-9e-5)]) == []
        monkeypatch.setenv(ENV_KILL_SWITCH, "true")
        assert len(radar.scan([snap(funding=-9e-5)])) == 1


# ── tier boundaries ───────────────────────────────────────────────────────────

class TestTiers:
    def test_tier1_absolute_funding_fires(self, radar):
        out = radar.scan([snap(funding=-5e-5)])
        assert len(out) == 1
        assert out[0].tier == 1
        assert out[0].legs["funding_negative"] is True

    def test_tier1_boundary_strict(self, radar):
        # funding == funding_negative exactly -> no verdict (strict <)
        assert radar.scan([snap(funding=-3.0e-5)]) == []

    def test_funding_above_threshold_no_verdict(self, radar):
        assert radar.scan([snap(funding=-2e-5)]) == []
        assert radar.scan([snap(funding=0.0)]) == []
        assert radar.scan([snap(funding=1e-4)]) == []

    def test_absolute_funding_fix_negative_regime_still_fires(self, radar):
        """The old scanner's blind spot: deeply negative funding while the 7d
        average is ALSO negative read as 'normal' relative-to-window. The radar
        compares against absolute thresholds only — funding -9e-5 fires even
        though a hypothetical negative trailing average would have hidden it."""
        out = radar.scan([snap(funding=-9e-5)])
        assert len(out) == 1
        assert out[0].tier >= 1
        assert out[0].funding == pytest.approx(-9e-5)

    def test_tier2_fires(self, radar):
        # funding < extreme AND oi_to_float 0.20 > 0.15
        out = radar.scan([snap(funding=-9e-5, oi_usd=2.0e6, supply_usd=10.0e6)])
        assert out[0].tier == 2
        assert out[0].oi_to_float == pytest.approx(0.20)

    def test_tier2_unreachable_when_supply_abstains(self, radar):
        out = radar.scan([snap(funding=-9e-5, oi_usd=2.0e6, supply_usd=None)])
        assert out[0].tier == 1
        assert out[0].oi_to_float is None
        assert "oi_float" in out[0].abstained

    def test_tier2_unreachable_when_oi_missing(self, radar):
        out = radar.scan([snap(funding=-9e-5, oi_usd=None, supply_usd=10.0e6)])
        assert out[0].tier == 1

    def test_tier2_unreachable_zero_supply(self, radar):
        out = radar.scan([snap(funding=-9e-5, oi_usd=2.0e6, supply_usd=0.0)])
        assert out[0].tier == 1
        assert "oi_float" in out[0].abstained

    def test_tier3_fires_irys_anatomy(self, radar):
        # IRYS anatomy: oi/float 0.51, funding extreme, whales net short 2x
        out = radar.scan([snap(funding=-9e-5, oi_usd=5.1e6, supply_usd=10.0e6,
                               long_short_ratio=0.5)])
        assert out[0].tier == 3
        assert out[0].short_ratio == pytest.approx(2.0)
        assert out[0].legs["oi_float_extreme"] is True
        assert out[0].legs["short_ratio_high"] is True

    def test_tier3_unreachable_when_ls_abstains(self, radar):
        out = radar.scan([snap(funding=-9e-5, oi_usd=5.1e6, supply_usd=10.0e6,
                               long_short_ratio=None)])
        assert out[0].tier == 2
        assert "short_ratio" in out[0].abstained

    def test_tier3_unreachable_when_short_ratio_low(self, radar):
        # ls 0.8 -> short_ratio 1.25 < 1.5
        out = radar.scan([snap(funding=-9e-5, oi_usd=5.1e6, supply_usd=10.0e6,
                               long_short_ratio=0.8)])
        assert out[0].tier == 2
        assert out[0].short_ratio == pytest.approx(1.25)


# ── macro-window boost ────────────────────────────────────────────────────────

class TestMacroBoost:
    def test_macro_requires_major(self, radar):
        # in-window NON-major with no funding evidence -> still nothing
        assert radar.scan([snap(symbol="PEPE-USD", macro_window=True)]) == []

    def test_macro_requires_window(self, radar):
        assert radar.scan([snap(symbol="BTC-USD", macro_window=False)]) == []

    def test_macro_requires_funding_evidence(self, radar):
        # fail-closed: no funding read -> boost cannot fire even in-window
        assert radar.scan([snap(symbol="BTC-USD", macro_window=True,
                                funding=None)]) == []

    def test_macro_in_window_major_tiers(self, radar):
        out = radar.scan([snap(symbol="BTC-USD", macro_window=True,
                               funding=-5e-5)])
        assert out[0].tier >= 1


# ── thin book leg ─────────────────────────────────────────────────────────────

class TestThinBook:
    def test_thin_by_spread(self, radar):
        out = radar.scan([snap(funding=-5e-5, spread_bps=40.0)])
        assert out[0].thin_book is True

    def test_thin_by_depth(self, radar):
        out = radar.scan([snap(funding=-5e-5, depth_usd_10bps=9000.0)])
        assert out[0].thin_book is True

    def test_thin_by_listing_age(self, radar):
        out = radar.scan([snap(funding=-5e-5, listing_age_days=3.0)])
        assert out[0].thin_book is True

    def test_not_thin(self, radar):
        out = radar.scan([snap(funding=-5e-5, spread_bps=5.0,
                               depth_usd_10bps=50000.0, listing_age_days=100.0)])
        assert out[0].thin_book is False

    def test_thin_abstains_when_all_missing(self, radar):
        out = radar.scan([snap(funding=-5e-5)])
        assert out[0].thin_book is None
        assert "thin_book" in out[0].abstained


# ── grade ─────────────────────────────────────────────────────────────────────

class TestGrade:
    def test_grade_exact_tier1(self, radar):
        # 65 + 10*1 + min(15, 0.5*15=7.5) = 82.5
        out = radar.scan([snap(funding=-5e-5)])
        assert out[0].grade == pytest.approx(82.5)

    def test_grade_monotonic_in_funding(self, radar):
        g1 = radar.scan([snap(funding=-4e-5)])[0].grade
        g2 = radar.scan([snap(funding=-6e-5)])[0].grade
        assert g2 > g1

    def test_grade_monotonic_across_tiers(self, radar):
        t1 = radar.scan([snap(funding=-9e-5)])[0].grade
        t2 = radar.scan([snap(funding=-9e-5, oi_usd=2e6, supply_usd=1e7)])[0].grade
        t3 = radar.scan([snap(funding=-9e-5, oi_usd=5.1e6, supply_usd=1e7,
                              long_short_ratio=0.5)])[0].grade
        assert t1 <= t2 <= t3

    def test_grade_capped_at_100(self, radar):
        out = radar.scan([snap(funding=-9e-5, oi_usd=5.1e6, supply_usd=1e7,
                               long_short_ratio=0.5, volume_spike=9.0,
                               oi_growth_3h=0.5, spread_bps=50.0)])
        assert out[0].grade == 100.0

    def test_grade_bonus_legs(self, radar):
        base = radar.scan([snap(funding=-3.5e-5)])[0].grade
        bumped = radar.scan([snap(funding=-3.5e-5, volume_spike=6.0,
                                  oi_growth_3h=0.3, spread_bps=30.0)])[0].grade
        assert bumped - base == pytest.approx(8.0 + 7.0 + 4.0)


# ── geometry / fail-closed ────────────────────────────────────────────────────

class TestGeometryAndDegenerate:
    def test_trigger_price_estimate(self, radar):
        out = radar.scan([snap(mark=2.5, funding=-5e-5)])
        assert out[0].trigger_price == pytest.approx(2.5 * 1.08)
        assert out[0].legs["trigger_estimate"] is True

    @pytest.mark.parametrize("bad_mark", [0.0, -1.0, None, float("nan"),
                                          float("inf"), "x"])
    def test_degenerate_mark_skips(self, radar, bad_mark):
        assert radar.scan([snap(mark=bad_mark, funding=-9e-5)]) == []

    def test_degenerate_inputs_never_raise(self, radar):
        s = snap(mark=1.0, funding=float("nan"), oi_usd=-5.0,
                 supply_usd=float("inf"), long_short_ratio=0.0,
                 volume_spike="bad", spread_bps=None, depth_usd_10bps=None,
                 listing_age_days=None)
        out = radar.scan([s])
        assert out == []          # funding NaN abstains -> no tier, no raise

    def test_scan_sorted_by_grade_desc(self, radar):
        snaps = [
            snap(symbol="AAA-USD", funding=-4e-5),                                  # ~81
            snap(symbol="BBB-USD", funding=-9e-5, oi_usd=2e6, supply_usd=1e7),      # tier2
            snap(symbol="CCC-USD", funding=-5e-5),                                  # 82.5
        ]
        out = radar.scan(snaps)
        assert [v.symbol for v in out] == ["BBB-USD", "CCC-USD", "AAA-USD"]

    def test_scan_filters_tier0(self, radar):
        out = radar.scan([snap(symbol="A-USD", funding=1e-4),
                          snap(symbol="B-USD", funding=-5e-5)])
        assert len(out) == 1 and out[0].symbol == "B-USD"

    def test_custom_config_thresholds(self):
        cfg = SqueezeRadarConfig(funding_negative=-1e-4)
        r = SqueezeRadar(cfg)
        # -5e-5 is below the DEFAULT threshold but above the custom one
        assert r.scan([snap(funding=-5e-5)]) == []
        assert len(r.scan([snap(funding=-2e-4)])) == 1
