"""tests/test_market_families.py — pins for the family propagation engine."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from intelligence.market_families import (
    FAMILIES, FAMILIES_V2, FamilyEngine, PropagationSignal, PropagationTarget,
    family_map, family_v2, levered_proxy_beta, member_profile,
    propagation_table, required_move, signal_type, whale_tier,
)


def _cfg(**kw):
    base = dict(
        family_engine_enabled=True,
        family_min_leader_move_pct=0.4,
        family_member_moved_frac=0.5,
        family_map=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _signal(direction_pct=0.8, ts=1_000_000.0, family="crypto_majors"):
    eng = FamilyEngine(clock=lambda: ts)
    return eng.on_leader_move(_cfg(), family=family,
                              leader_move_pct=direction_pct,
                              window_minutes=10, now_ts=ts)


# ── family map ────────────────────────────────────────────────────────────────

class TestFamilyMap:
    def test_default_families_exposed(self):
        assert "crypto_majors" in FAMILIES
        assert "metals" in FAMILIES
        assert FAMILIES["crypto_majors"]["leader"] == "BTC-USD"
        assert FAMILIES["metals"]["lag_minutes"]["SILVER-USD"] == 2
        assert FAMILIES["metals"]["lag_minutes"]["COPPER-USD"] == 15

    def test_cfg_override_wins(self):
        custom = {"x": {"leader": "A-USD", "members": ["B-USD"],
                        "lag_minutes": {"B-USD": 1}}}
        assert family_map(_cfg(family_map=custom)) is custom

    def test_cfg_empty_override_falls_back(self):
        assert family_map(_cfg(family_map={})) is FAMILIES

    def test_energy_has_no_members(self):
        assert FAMILIES["energy"]["members"] == []


# ── required_move ─────────────────────────────────────────────────────────────

class TestRequiredMove:
    def test_basic(self):
        assert required_move("BTC-USD", 10.0, 5) == pytest.approx(2.0)

    def test_higher_leverage_needs_less_move(self):
        assert required_move("BTC-USD", 10.0, 10) < required_move("BTC-USD", 10.0, 5)

    def test_zero_leverage_none(self):
        assert required_move("BTC-USD", 10.0, 0) is None

    def test_garbage_none(self):
        assert required_move("BTC-USD", "x", 5) is None
        assert required_move("BTC-USD", 10.0, None) is None


# ── price ring / move_pct ─────────────────────────────────────────────────────

class TestMovePct:
    def test_move_over_window(self):
        eng = FamilyEngine(clock=lambda: 1000.0)
        eng.update_price("BTC-USD", 100.0, 400.0)
        eng.update_price("BTC-USD", 101.0, 1000.0)
        mv = eng.move_pct("BTC-USD", 10, now_ts=1000.0)
        assert mv == pytest.approx(1.0)

    def test_no_data_abstains(self):
        eng = FamilyEngine(clock=lambda: 1000.0)
        assert eng.move_pct("BTC-USD", 10, now_ts=1000.0) is None

    def test_garbage_update_ignored(self):
        eng = FamilyEngine(clock=lambda: 1000.0)
        eng.update_price("BTC-USD", -5, 1000.0)
        eng.update_price("BTC-USD", float("nan"), 1000.0)
        eng.update_price("", 100.0, 1000.0)
        assert eng.move_pct("BTC-USD", 10, now_ts=1000.0) is None

    def test_ring_bounded(self):
        eng = FamilyEngine(clock=lambda: 10_000.0, ring_minutes=5)
        for i in range(400):
            eng.update_price("BTC-USD", 100.0 + i * 0.01, 10_000.0 + i)
        dq = eng._prices["BTC-USD"]
        assert len(dq) <= 301  # 5 minutes of 1s ticks + current

    def test_oldest_observation_inside_window_anchors(self):
        eng = FamilyEngine(clock=lambda: 1000.0)
        eng.update_price("BTC-USD", 100.0, 900.0)   # inside a 10-min window
        eng.update_price("BTC-USD", 102.0, 1000.0)
        assert eng.move_pct("BTC-USD", 10, now_ts=1000.0) == pytest.approx(2.0)


# ── on_leader_move ────────────────────────────────────────────────────────────

class TestOnLeaderMove:
    def test_threshold_arms(self):
        sig = _signal(0.4)
        assert sig is not None
        assert sig.direction == "up"
        assert sig.conviction == pytest.approx(1.0)

    def test_below_threshold_abstains(self):
        assert _signal(0.39) is None

    def test_exactly_threshold_arms(self):
        assert _signal(0.4) is not None

    def test_negative_move_direction_down(self):
        sig = _signal(-0.8)
        assert sig.direction == "down"

    def test_conviction_capped_at_2(self):
        sig = _signal(3.0)
        assert sig.conviction == pytest.approx(2.0)

    def test_conviction_scales(self):
        sig = _signal(0.8)
        assert sig.conviction == pytest.approx(2.0)
        sig2 = _signal(0.6)
        assert sig2.conviction == pytest.approx(1.5)

    def test_kill_switch(self):
        eng = FamilyEngine(clock=lambda: 1e6)
        assert eng.on_leader_move(_cfg(family_engine_enabled=False),
                                  family="crypto_majors", leader_move_pct=1.0,
                                  window_minutes=10, now_ts=1e6) is None

    def test_unknown_family_abstains(self):
        eng = FamilyEngine(clock=lambda: 1e6)
        assert eng.on_leader_move(_cfg(), family="nope",
                                  leader_move_pct=1.0, window_minutes=10,
                                  now_ts=1e6) is None

    def test_etas_from_lag_matrix(self):
        sig = _signal(0.8, ts=1e6)
        etas = dict(sig.member_etas)
        assert etas["ETH-USD"] == pytest.approx(1e6 + 3 * 60)
        assert etas["DOGE-USD"] == pytest.approx(1e6 + 10 * 60)

    def test_energy_leader_move_no_members(self):
        eng = FamilyEngine(clock=lambda: 1e6)
        sig = eng.on_leader_move(_cfg(), family="energy",
                                 leader_move_pct=0.9, window_minutes=10,
                                 now_ts=1e6)
        assert sig is not None and sig.member_etas == ()

    def test_min_move_knob(self):
        eng = FamilyEngine(clock=lambda: 1e6)
        assert eng.on_leader_move(_cfg(family_min_leader_move_pct=1.0),
                                  family="crypto_majors", leader_move_pct=0.8,
                                  window_minutes=10, now_ts=1e6) is None


# ── propagation_targets ───────────────────────────────────────────────────────

class TestPropagationTargets:
    def test_unmoved_members_are_targets(self):
        sig = _signal(0.8)
        eng = FamilyEngine(clock=lambda: 1e6)
        states = {"ETH-USD": 0.1, "SOL-USD": 0.05, "XRP-USD": 0.0, "DOGE-USD": -0.02}
        tgts = eng.propagation_targets(_cfg(), signal=sig, member_states=states)
        assert {t.symbol for t in tgts} == {"ETH-USD", "SOL-USD", "XRP-USD", "DOGE-USD"}

    def test_moved_member_skipped(self):
        sig = _signal(0.8)  # 50% of 0.8 = 0.4
        eng = FamilyEngine(clock=lambda: 1e6)
        states = {"ETH-USD": 0.5, "SOL-USD": 0.1, "XRP-USD": 0.0, "DOGE-USD": 0.0}
        tgts = eng.propagation_targets(_cfg(), signal=sig, member_states=states)
        assert "ETH-USD" not in {t.symbol for t in tgts}
        assert "SOL-USD" in {t.symbol for t in tgts}

    def test_exactly_half_counts_as_moved(self):
        sig = _signal(0.8)
        eng = FamilyEngine(clock=lambda: 1e6)
        states = {"ETH-USD": 0.4}
        tgts = eng.propagation_targets(_cfg(), signal=sig, member_states=states)
        assert "ETH-USD" not in {t.symbol for t in tgts}

    def test_wrong_direction_member_still_target(self):
        sig = _signal(0.8)  # leader up; member went down → not propagated
        eng = FamilyEngine(clock=lambda: 1e6)
        states = {"ETH-USD": -0.6}
        tgts = eng.propagation_targets(_cfg(), signal=sig, member_states=states)
        assert {t.symbol for t in tgts} == {"ETH-USD"}
        assert tgts[0].direction == "up"

    def test_down_leader_mirror(self):
        sig = _signal(-0.8)
        eng = FamilyEngine(clock=lambda: 1e6)
        states = {"ETH-USD": -0.5, "SOL-USD": -0.1}
        tgts = eng.propagation_targets(_cfg(), signal=sig, member_states=states)
        assert {t.symbol for t in tgts} == {"SOL-USD"}
        assert tgts[0].direction == "down"

    def test_missing_member_state_abstains_member(self):
        sig = _signal(0.8)
        eng = FamilyEngine(clock=lambda: 1e6)
        tgts = eng.propagation_targets(_cfg(), signal=sig, member_states={})
        assert tgts == []

    def test_garbage_member_state_abstains_member(self):
        sig = _signal(0.8)
        eng = FamilyEngine(clock=lambda: 1e6)
        states = {"ETH-USD": "junk", "SOL-USD": 0.0}
        tgts = eng.propagation_targets(_cfg(), signal=sig, member_states=states)
        assert {t.symbol for t in tgts} == {"SOL-USD"}

    def test_kill_switch_returns_empty(self):
        sig = _signal(0.8)
        eng = FamilyEngine(clock=lambda: 1e6)
        assert eng.propagation_targets(_cfg(family_engine_enabled=False),
                                       signal=sig, member_states={}) == []

    def test_target_carries_eta_and_remaining(self):
        sig = _signal(0.8, ts=1e6)
        eng = FamilyEngine(clock=lambda: 1e6)
        states = {"ETH-USD": 0.2}
        tgts = eng.propagation_targets(_cfg(), signal=sig, member_states=states)
        t = tgts[0]
        assert t.eta_ts == pytest.approx(1e6 + 180)
        assert t.lag_minutes == 3
        assert t.remaining_pct == pytest.approx(0.6)
        assert t.family == "crypto_majors"


# ── V2 family schema ──────────────────────────────────────────────────────────

class TestFamiliesV2:
    EXPECTED = {
        "CRYPTO_INSTITUTIONAL": (["BTC", "ETH"], "whale_plus_etf"),
        "CRYPTO_LEVERED_PROXY": (["MSTR", "COIN", "HOOD"], "btc_beta_navpremium"),
        "SEMICONDUCTOR": (["NVDA", "AMD"], "sox_plus_earnings_plus_etfflow"),
        "ALT_L1": (["SOL", "SUI", "AVAX", "TIA"], "pure_whale"),
        "DEFI": (["UNI", "AAVE", "SNX", "PENDLE"], "eth_propagation_plus_whale"),
        "L2_BRIDGE": (["ARB", "OP", "ZRO"], "eth_propagation_plus_whale"),
        "AI_CRYPTO": (["FET", "RENDER", "TAO"], "dual_anchor_plus_whale"),
    }

    def test_all_seven_families_present(self):
        assert set(self.EXPECTED) == set(FAMILIES_V2)

    def test_membership_and_signal_type(self):
        for fam, (members, stype) in self.EXPECTED.items():
            assert FAMILIES_V2[fam]["members"] == members
            assert FAMILIES_V2[fam]["signal_type"] == stype
            for m in members:
                assert family_v2(m) == fam
                assert signal_type(m) == stype

    def test_normalization(self):
        assert family_v2("BTC-USD") == "CRYPTO_INSTITUTIONAL"
        assert family_v2("btc") == "CRYPTO_INSTITUTIONAL"
        assert family_v2("eth-usd") == "CRYPTO_INSTITUTIONAL"
        assert signal_type("sol-usd") == "pure_whale"

    def test_unknown_symbol_none_everywhere(self):
        assert family_v2("DOGE") is None
        assert family_v2("") is None
        assert family_v2(None) is None
        assert member_profile("DOGE") is None
        assert signal_type("DOGE") is None
        assert whale_tier("DOGE", 5.0) is None
        assert propagation_table("NOPE", "ETH") is None
        assert propagation_table("DEFI", "BTC") is None
        assert levered_proxy_beta("DOGE", nav_premium=2.0) is None

    def test_crypto_institutional_weights(self):
        spec = FAMILIES_V2["CRYPTO_INSTITUTIONAL"]
        assert spec["whale_weight"] == pytest.approx(0.55)
        assert spec["etf_weight"] == pytest.approx(0.45)
        assert spec["etf_source"] == "IBIT_FBTC_ARKB_daily_flow"

    def test_alt_l1_etf_modifier_zero(self):
        assert FAMILIES_V2["ALT_L1"]["etf_modifier"] == 0


class TestMemberProfiles:
    def test_mstr_beta_boundaries(self):
        assert levered_proxy_beta("MSTR", nav_premium=1.6) == pytest.approx(2.8)
        assert levered_proxy_beta("MSTR", nav_premium=1.5) == pytest.approx(1.8)  # exactly -> 1.8 rung
        assert levered_proxy_beta("MSTR", nav_premium=1.2) == pytest.approx(1.8)
        assert levered_proxy_beta("MSTR", nav_premium=0.7) == pytest.approx(1.3)  # exactly -> 1.3 rung
        assert levered_proxy_beta("MSTR", nav_premium=0.69) == pytest.approx(1.1)
        assert levered_proxy_beta("MSTR") is None

    def test_coin_hood_regime_tables(self):
        assert levered_proxy_beta("COIN", btc_regime="btc_bull") == pytest.approx(1.6)
        assert levered_proxy_beta("COIN", btc_regime="btc_neutral") == pytest.approx(1.1)
        assert levered_proxy_beta("COIN", btc_regime="btc_bear") == pytest.approx(1.9)
        assert levered_proxy_beta("HOOD", btc_regime="btc_bull") == pytest.approx(1.4)
        assert levered_proxy_beta("HOOD", btc_regime="btc_neutral") == pytest.approx(0.9)
        assert levered_proxy_beta("HOOD", btc_regime="btc_bear") == pytest.approx(1.6)
        assert levered_proxy_beta("COIN", btc_regime="sideways") is None

    def test_mstr_range_fade_metadata(self):
        prof = member_profile("MSTR")
        rf = prof["range_fade"]
        assert rf["week_high"] == pytest.approx(171.0)
        assert rf["week_low"] == pytest.approx(123.0)
        assert rf["fade_top_at"] == pytest.approx(0.85)
        assert rf["fade_bot_at"] == pytest.approx(0.15)
        assert rf["cage_ratio"] == pytest.approx(4.0)

    def test_amd_nvda_asymmetry(self):
        amd, nvda = member_profile("AMD"), member_profile("NVDA")
        assert amd["sox_beta"] == pytest.approx(1.15)
        assert nvda["sox_beta"] == pytest.approx(0.60)
        assert amd["compression_bonus"] == pytest.approx(1.25)
        assert nvda["compression_bonus"] == pytest.approx(1.00)
        assert amd["soxs_contrarian"] == pytest.approx(1.15)
        assert "soxs_contrarian" not in nvda
        assert nvda["ai_capex_news"] == pytest.approx(2.50)
        assert amd["analyst_upgrade"] == pytest.approx(2.80)

    def test_normalization_profile_lookup(self):
        assert member_profile("amd-usd") == member_profile("AMD")
        assert member_profile("nvda-USD")["sox_beta"] == pytest.approx(0.60)

    def test_semis_proxy_members(self):
        assert FAMILIES_V2["SEMICONDUCTOR"]["anchor"] == "SOX"
        for sym in ("SAMSUNG", "SKHY", "DRAM"):
            assert family_v2(sym) == "SEMICONDUCTOR"
            assert member_profile(sym) == {"proxy_for": "SOX"}

    def test_plain_member_without_profile_none(self):
        assert member_profile("BTC") is None
        assert member_profile("SOL") is None


class TestPropagationTables:
    def test_defi_from_eth(self):
        t = propagation_table("DEFI", "ETH")
        assert t["UNI"]["multiplier"] == pytest.approx(1.40)
        assert t["UNI"]["lag_h"] == 3
        assert t["AAVE"]["multiplier"] == pytest.approx(1.30)
        assert t["AAVE"]["lag_h"] == 4
        assert t["SNX"]["multiplier"] == pytest.approx(1.60)
        assert t["SNX"]["lag_h"] == 5
        assert t["PENDLE"]["multiplier"] == pytest.approx(1.20)
        assert t["PENDLE"]["lag_h"] == 6

    def test_l2_from_eth(self):
        t = propagation_table("L2_BRIDGE", "ETH")
        assert t["ARB"]["multiplier"] == pytest.approx(1.25)
        assert t["ARB"]["lag_h"] == 2
        assert t["OP"]["multiplier"] == pytest.approx(1.20)
        assert t["ZRO"]["multiplier"] == pytest.approx(1.35)
        assert t["ZRO"]["lag_h"] == 3

    def test_ai_crypto_dual_anchor(self):
        nv = propagation_table("AI_CRYPTO", "NVDA")
        assert nv["FET"]["multiplier"] == pytest.approx(0.60)
        assert nv["FET"]["lag_h"] == 6
        assert nv["RENDER"]["lag_h"] == 7
        assert nv["TAO"]["multiplier"] == pytest.approx(0.45)
        eth = propagation_table("AI_CRYPTO", "ETH")
        assert eth["FET"] == {"multiplier": pytest.approx(0.40), "lag_h": 3}
        assert eth["RENDER"] == {"multiplier": pytest.approx(0.35), "lag_h": 4}
        assert "TAO" not in eth
        assert FAMILIES_V2["AI_CRYPTO"]["combined_anchor_boost"] == pytest.approx(1.35)
        assert FAMILIES_V2["AI_CRYPTO"]["anchors"] == ["NVDA", "ETH"]

    def test_alt_l1_from_sol_and_btc_etf(self):
        t = propagation_table("ALT_L1", "SOL")
        assert t["SUI"] == {"multiplier": pytest.approx(0.75), "lag_h": 6}
        assert t["AVAX"] == {"multiplier": pytest.approx(0.65), "lag_h": 8}
        assert t["TIA"] == {"multiplier": pytest.approx(0.55), "lag_h": 10}
        etf = propagation_table("ALT_L1", "BTC_ETF")
        assert etf["lag_hours"] == 12
        assert etf["multiplier"] == pytest.approx(1.20)

    def test_case_insensitive_family_and_anchor(self):
        assert propagation_table("defi", "eth")["UNI"]["lag_h"] == 3

    def test_defi_whale_bonus_ladder_data(self):
        assert FAMILIES_V2["DEFI"]["eth_whale_bonus"] == [
            (2.0, 1.20), (1.5, 1.10), (1.0, 1.00),
        ]


class TestWhaleTier:
    def test_boundaries(self):
        assert whale_tier("SOL", 4.0) == "TIER_1"
        assert whale_tier("SOL", 5.5) == "TIER_1"
        assert whale_tier("SOL", 3.99) == "TIER_2"
        assert whale_tier("SUI", 3.0) == "TIER_2"
        assert whale_tier("AVAX", 2.0) == "TIER_3"
        assert whale_tier("TIA", 1.5) == "TIER_4"
        assert whale_tier("SOL", 1.49) is None

    def test_only_alt_l1(self):
        assert whale_tier("BTC", 5.0) is None
        assert whale_tier("UNI", 5.0) is None

    def test_venue_suffixed_symbol(self):
        assert whale_tier("sol-usd", 4.0) == "TIER_1"

    def test_garbage_ratio(self):
        assert whale_tier("SOL", "junk") is None
        assert whale_tier("SOL", None) is None


class TestSetupProfilesAdvisory:
    def test_semiconductor_setups(self):
        spec = FAMILIES_V2["SEMICONDUCTOR"]
        assert spec["campaign_setup"]["entry_method"] == "POST_CATALYST_DIP"
        assert spec["campaign_setup"]["spike"] == pytest.approx(0.008)
        assert spec["campaign_setup"]["min_cage_ratio"] == pytest.approx(3.0)
        assert spec["campaign_setup"]["leverage"] == 3
        assert spec["campaign_setup"]["hold_hours"] == 168
        assert spec["scalp_setup"]["entry_method"] == "SOX_LAG"
        assert spec["scalp_setup"]["session_gate"] == "13:30-20:00 UTC"
        assert spec["scalp_setup"]["hold_minutes"] == 60

    def test_leverage_fields_are_data_only(self):
        # Advisory metadata — present as data, never consumed by the engine.
        assert FAMILIES_V2["CRYPTO_INSTITUTIONAL"]["scalp_setup"]["leverage"] == 30
        assert FAMILIES_V2["CRYPTO_INSTITUTIONAL"]["campaign_setup"]["hold_hours"] == 24
        assert FAMILIES_V2["CRYPTO_LEVERED_PROXY"]["campaign_setup"]["entry_method"] == "RANGE_FADE"
        assert FAMILIES_V2["ALT_L1"]["scalp_setup"]["leverage"] == 20
        assert FAMILIES_V2["ALT_L1"]["campaign_setup"]["min_cage_ratio"] == 4.0
        assert FAMILIES_V2["DEFI"]["campaign_setup"]["hold_hours"] == 36
        assert FAMILIES_V2["AI_CRYPTO"]["campaign_setup"]["min_cage_ratio"] == 4.0
