"""Equity session regime + colony pins (2026-09-22, Governor equity-perp
framework + ant-colony directive). Live-day-one doctrine: kill-switch off =
pre-module sizing bit-for-bit; pheromone is measured EV, never narrative."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence import equity_session as es  # noqa: E402
from intelligence.equity_colony import EquityColony, SUBFAMILIES  # noqa: E402


def _ts_et(hour: float, minute: int = 0) -> float:
    """UTC epoch for a fixed ET wall time on a summer (EDT) date."""
    import datetime as dt
    et = dt.datetime(2026, 9, 22, int(hour), minute,
                     tzinfo=es._ET)
    return et.timestamp()


class TestSessionRegime:
    def test_core_hours(self):
        assert es.regime(_ts_et(10, 0)) == es.CORE_HOURS
        assert es.regime(_ts_et(15, 59)) == es.CORE_HOURS

    def test_pre_market(self):
        assert es.regime(_ts_et(4, 0)) == es.PRE_MARKET
        assert es.regime(_ts_et(9, 29)) == es.PRE_MARKET

    def test_ah_open_flat_book(self):
        # 16:00-20:00 ET with a flat book: the perp IS the price discovery
        assert es.regime(_ts_et(16, 0)) == es.AH_OPEN
        assert es.regime(_ts_et(19, 59)) == es.AH_OPEN
        assert es.size_mult(_ts_et(17, 0)) == 1.0

    def test_ah_hold_with_book(self):
        # same window carrying a book: thin-hours degradation on new entries
        assert es.regime(_ts_et(16, 0), book_open=True) == es.AH_HOLD
        assert es.size_mult(_ts_et(17, 0), book_open=True) == \
            pytest.approx(0.60)

    def test_ah_event_live_catalyst(self):
        assert es.regime(_ts_et(17, 0), event=True) == es.AH_EVENT
        assert es.size_mult(_ts_et(17, 0), book_open=False,
                            event=True) == pytest.approx(0.40)

    def test_overnight(self):
        assert es.regime(_ts_et(20, 0)) == es.OVERNIGHT
        assert es.regime(_ts_et(3, 59)) == es.OVERNIGHT
        assert es.size_mult(_ts_et(23, 0)) == pytest.approx(0.50)

    def test_core_mult_binds(self):
        m = es.size_mult(_ts_et(12, 0))
        assert m == pytest.approx(0.75)

    def test_pre_market_mult(self):
        assert es.size_mult(_ts_et(8, 0)) == pytest.approx(0.90)

    def test_knob_override(self):
        m = es.size_mult(_ts_et(12, 0), {"CORE_HOURS": 0.5})
        assert m == pytest.approx(0.5)

    def test_dst_winter_boundaries(self):
        # EST (UTC-5): zoneinfo must resolve the wall clock, not a fixed
        # offset — 09:30 ET in January is CORE, 16:00 ET is AH_OPEN.
        import datetime as dt
        jan_core = dt.datetime(2026, 1, 15, 9, 30, tzinfo=es._ET).timestamp()
        jan_ah = dt.datetime(2026, 1, 15, 16, 0, tzinfo=es._ET).timestamp()
        assert es.regime(jan_core) == es.CORE_HOURS
        assert es.regime(jan_ah) == es.AH_OPEN


class TestColony:
    def _colony(self):
        return EquityColony(leader_move_pct=1.0, boost_max=0.25,
                            carry_threshold=0.0001, carry_boost=0.10,
                            clock=lambda: 1000.0)

    def test_leader_up_boosts_follower_long(self):
        c = self._colony()
        mult, trail = c.boost("AMD-USD", "long",
                              {"USTECH100-USD": 2.5}, None)
        assert mult == pytest.approx(1.25)       # weight 1.0 -> full boost
        assert trail[0] == "leadlag" and trail[1] == "USTECH100-USD"

    def test_leader_up_does_not_boost_short(self):
        c = self._colony()
        mult, trail = c.boost("AMD-USD", "short",
                              {"USTECH100-USD": 2.5}, None)
        assert mult == 1.0 and trail is None

    def test_leader_down_boosts_follower_short(self):
        c = self._colony()
        mult, trail = c.boost("TSM-USD", "short",
                              {"GOOGL-USD": -1.8}, None)
        assert mult == pytest.approx(1.25)

    def test_below_threshold_abstains(self):
        c = self._colony()
        mult, trail = c.boost("AMD-USD", "long",
                              {"USTECH100-USD": 0.4}, None)
        assert mult == 1.0 and trail is None

    def test_non_colony_symbol_untouched(self):
        c = self._colony()
        mult, trail = c.boost("BTC-USD", "long",
                              {"USTECH100-USD": 5.0}, None)
        assert mult == 1.0 and trail is None

    def test_circuit_direction(self):
        # INDEX -> MEGA -> SEMIS -> CRYPTO_ADJ; never backwards.
        c = self._colony()
        assert "AMD-USD" in c.downstream("USTECH100-USD")
        assert "COIN-USD" in c.downstream("AMD-USD")
        assert c.downstream("COIN-USD") == []
        assert "USTECH100-USD" not in c.downstream("AMD-USD")

    def test_carry_negative_funding_boosts_long(self):
        c = self._colony()
        mult, trail = c.boost("SKHX-USD", "long", {}, -0.000137)
        assert mult == pytest.approx(1.10)
        assert trail[0] == "carry"

    def test_carry_positive_funding_boosts_short(self):
        c = self._colony()
        mult, _ = c.boost("SKHX-USD", "short", {}, 0.0005)
        assert mult == pytest.approx(1.10)

    def test_carry_wrong_side_abstains(self):
        c = self._colony()
        mult, trail = c.boost("SKHX-USD", "short", {}, -0.000137)
        assert mult == 1.0 and trail is None

    def test_reinforce_win_strengthens(self):
        c = self._colony()
        _, trail = c.boost("AMD-USD", "long", {"USTECH100-USD": 2.5}, None)
        c.reinforce("AMD-USD", trail, True)
        w = c.weights["USTECH100-USD->AMD-USD"]
        assert w == pytest.approx(1.05)
        mult2, _ = c.boost("AMD-USD", "long", {"USTECH100-USD": 2.5}, None)
        assert mult2 == pytest.approx(1.25)      # capped at boost_max

    def test_reinforce_loss_decays(self):
        c = self._colony()
        _, trail = c.boost("AMD-USD", "long", {"USTECH100-USD": 2.5}, None)
        for _ in range(10):
            c.reinforce("AMD-USD", trail, False)
        w = c.weights["USTECH100-USD->AMD-USD"]
        assert 0.5 <= w < 1.0
        mult2, _ = c.boost("AMD-USD", "long", {"USTECH100-USD": 2.5}, None)
        assert mult2 == pytest.approx(1.0 + 0.25 * w)

    def test_weight_bounds(self):
        c = self._colony()
        _, trail = c.boost("AMD-USD", "long", {"USTECH100-USD": 2.5}, None)
        for _ in range(200):
            c.reinforce("AMD-USD", trail, True)
        assert c.weights["USTECH100-USD->AMD-USD"] == 2.0
        for _ in range(500):
            c.reinforce("AMD-USD", trail, False)
        assert c.weights["USTECH100-USD->AMD-USD"] == 0.5

    def test_evaporate_returns_to_neutral(self):
        c = self._colony()
        c.weights["A->B"] = 1.05
        c.evaporate()
        assert 1.0 < c.weights["A->B"] < 1.05   # monotone decay toward 1
        c.weights["A->B"] = 1.00005             # inside the neutral band
        c.evaporate()
        assert "A->B" not in c.weights          # pruned at neutral

    def test_persistence_roundtrip(self, tmp_path):
        c = self._colony()
        c.weights["USTECH100-USD->AMD-USD"] = 1.2
        p = str(tmp_path / "colony.json")
        c.save(p)
        c2 = self._colony()
        c2.load(p)
        assert c2.weights["USTECH100-USD->AMD-USD"] == pytest.approx(1.2)

    def test_load_corrupt_file_abstains(self, tmp_path):
        p = str(tmp_path / "colony.json")
        with open(p, "w") as fh:
            fh.write("{not json")
        c = self._colony()
        c.load(p)                                 # never raises
        assert c.weights == {}

    def test_malformed_inputs_fail_open(self):
        c = self._colony()
        assert c.boost("AMD-USD", "long", None, "junk") == (1.0, None)


class TestGapModule:
    def test_anchor_rolls_at_16_et(self):
        from intelligence import equity_gap as eg
        a_before = eg.latest_anchor_ts(_ts_et(12, 0))   # yesterday 16:00 ET
        a_after = eg.latest_anchor_ts(_ts_et(17, 0))    # today 16:00 ET
        assert a_after - a_before == pytest.approx(86400.0)

    def test_anchor_close_picks_last_bar_at_or_before(self):
        from intelligence import equity_gap as eg
        anchor = eg.latest_anchor_ts(_ts_et(12, 0))
        closes = [(anchor - 3600, 100.0), (anchor - 60, 101.0),
                  (anchor + 60, 999.0)]                 # after-anchor ignored
        assert eg.anchor_close(closes, anchor) == 101.0

    def test_anchor_close_stale_abstains(self):
        from intelligence import equity_gap as eg
        anchor = eg.latest_anchor_ts(_ts_et(12, 0))
        assert eg.anchor_close([(anchor - 3 * 3600, 100.0)], anchor) is None
        assert eg.anchor_close([], anchor) is None

    def test_gap_pct_math_and_degenerate(self):
        from intelligence import equity_gap as eg
        assert eg.gap_pct(105.0, 100.0) == pytest.approx(5.0)
        assert eg.gap_pct(97.0, 100.0) == pytest.approx(-3.0)
        assert eg.gap_pct(0, 100.0) is None
        assert eg.gap_pct(105.0, 0) is None


class TestGapFade:
    def _colony(self, hour=12, minute=0):
        return EquityColony(clock=lambda: _ts_et(hour, minute))

    def test_gap_up_boosts_short_bucket_small(self):
        c = self._colony()
        mult, trail = c.boost("AMD-USD", "short", {}, None,
                              gaps={"AMD-USD": 2.5})
        assert mult == pytest.approx(1.0 + 0.20 * 0.4)   # 1-3% bucket
        assert trail[0] == "gapfade" and trail[1] == "GAP"

    def test_gap_buckets_scale(self):
        c = self._colony()
        m1, _ = c.boost("AMD-USD", "short", {}, None, gaps={"AMD-USD": 2.5})
        m2, _ = c.boost("AMD-USD", "short", {}, None, gaps={"AMD-USD": 5.0})
        m3, _ = c.boost("AMD-USD", "short", {}, None, gaps={"AMD-USD": 8.0})
        assert m1 == pytest.approx(1.08)
        assert m2 == pytest.approx(1.15)
        assert m3 == pytest.approx(1.20)

    def test_gap_down_boosts_long(self):
        c = self._colony()
        mult, trail = c.boost("NVDA-USD", "long", {}, None,
                              gaps={"NVDA-USD": -4.0})
        assert mult == pytest.approx(1.15) and trail[0] == "gapfade"

    def test_with_gap_direction_abstains(self):
        c = self._colony()
        mult, trail = c.boost("AMD-USD", "long", {}, None,
                              gaps={"AMD-USD": 2.5})
        assert mult == 1.0 and trail is None

    def test_below_min_gap_abstains(self):
        c = self._colony()
        mult, trail = c.boost("AMD-USD", "short", {}, None,
                              gaps={"AMD-USD": 0.7})
        assert mult == 1.0 and trail is None

    def test_settle_window_abstains(self):
        c = self._colony(hour=9, minute=45)              # 30-min large settle
        mult, trail = c.boost("AMD-USD", "short", {}, None,
                              gaps={"AMD-USD": 8.0})
        assert mult == 1.0 and trail is None

    def test_pre_market_abstains(self):
        c = self._colony(hour=8, minute=0)               # gap still forming
        mult, trail = c.boost("AMD-USD", "short", {}, None,
                              gaps={"AMD-USD": 5.0})
        assert mult == 1.0 and trail is None

    def test_after_hours_window_open(self):
        c = self._colony(hour=20, minute=0)
        mult, _ = c.boost("AMD-USD", "short", {}, None,
                          gaps={"AMD-USD": 5.0})
        assert mult == pytest.approx(1.15)

    def test_gap_trail_reinforces(self):
        c = self._colony()
        _, trail = c.boost("AMD-USD", "short", {}, None,
                           gaps={"AMD-USD": 5.0})
        c.reinforce("AMD-USD", trail, True)
        assert c.weights["GAP->AMD-USD"] == pytest.approx(1.05)

    def test_malformed_gaps_fail_open(self):
        c = self._colony()
        assert c.boost("AMD-USD", "short", {}, None,
                       gaps={"AMD-USD": "junk"}) == (1.0, None)


class TestTrailDecay:
    def _colony(self, t0=100000.0):
        state = {"now": t0}
        c = EquityColony(clock=lambda: state["now"],
                         trail_halflife_s=1800.0, trail_min_scale=0.10)
        return c, state

    def test_half_life_halves_the_boost(self):
        c, st = self._colony()
        m1, _ = c.boost("AMD-USD", "long", {"USTECH100-USD": 2.5}, None)
        assert m1 == pytest.approx(1.25)
        st["now"] += 1800.0                      # one half-life
        m2, _ = c.boost("AMD-USD", "long", {"USTECH100-USD": 2.5}, None)
        assert m2 == pytest.approx(1.125)

    def test_trail_dead_below_floor(self):
        c, st = self._colony()
        m1, _ = c.boost("AMD-USD", "long", {"USTECH100-USD": 2.5}, None)
        assert m1 == pytest.approx(1.25)
        st["now"] += 7200.0                      # 4 half-lives: 0.0625 < 0.10
        m2, trail = c.boost("AMD-USD", "long", {"USTECH100-USD": 2.5}, None)
        assert m2 == 1.0 and trail is None

    def test_below_threshold_disarms_fresh_arm_on_refire(self):
        c, st = self._colony()
        c.boost("AMD-USD", "long", {"USTECH100-USD": 2.5}, None)
        st["now"] += 1700.0
        # leader drops below threshold -> disarm
        c.boost("AMD-USD", "long", {"USTECH100-USD": 0.5}, None)
        st["now"] += 1700.0                      # 3400s total, nearly 2 HL
        # re-cross re-arms FRESH — no stale age carried over
        m, _ = c.boost("AMD-USD", "long", {"USTECH100-USD": 2.5}, None)
        assert m == pytest.approx(1.25)

    def test_carry_trail_never_decays(self):
        c, st = self._colony()
        m1, _ = c.boost("SKHX-USD", "long", {}, -0.0005)
        assert m1 == pytest.approx(1.10)
        st["now"] += 7200.0
        m2, trail = c.boost("SKHX-USD", "long", {}, -0.0005)
        assert m2 == pytest.approx(1.10) and trail[0] == "carry"

    def test_pair_trail_decays(self):
        c, st = self._colony()
        mv = {"COIN-USD": 3.0, "HOOD-USD": 0.5}
        m1, _ = c.boost("HOOD-USD", "long", mv, None)
        assert m1 == pytest.approx(1.12)
        st["now"] += 7200.0
        m2, trail = c.boost("HOOD-USD", "long", mv, None)
        assert m2 == 1.0 and trail is None

    def test_armed_at_persistence_roundtrip(self, tmp_path):
        c, _ = self._colony()
        c.boost("AMD-USD", "long", {"USTECH100-USD": 2.5}, None)
        assert "L:USTECH100-USD" in c._armed_at
        p = str(tmp_path / "colony.json")
        c.save(p)
        c2, _ = self._colony()
        c2.load(p)
        assert c2._armed_at["L:USTECH100-USD"] == \
            pytest.approx(c._armed_at["L:USTECH100-USD"])

    def test_future_stamp_dropped_on_load(self, tmp_path):
        c, _ = self._colony()
        c._armed_at["L:USTECH100-USD"] = 100000.0 + 99999.0  # future
        p = str(tmp_path / "colony.json")
        c.save(p)
        c2, _ = self._colony()
        c2.load(p)
        assert "L:USTECH100-USD" not in c2._armed_at

    def test_evaporate_prunes_dead_trails(self):
        c, st = self._colony()
        c.boost("AMD-USD", "long", {"USTECH100-USD": 2.5}, None)
        assert "L:USTECH100-USD" in c._armed_at
        st["now"] += 1800.0 * 4.0                # beyond the decay horizon
        c.evaporate()
        assert "L:USTECH100-USD" not in c._armed_at


class TestGapSuppression:
    def _colony(self):
        return EquityColony(clock=lambda: _ts_et(12, 0))

    def test_gapped_leader_never_arms_leadlag(self):
        # GOOGL moved +2.5% on the day but gapped +2% overnight — event
        # noise, not rotation. No leadlag arming, no AMD boost.
        c = self._colony()
        mult, trail = c.boost("AMD-USD", "long", {"GOOGL-USD": 2.5}, None,
                              gaps={"GOOGL-USD": 2.0})
        assert mult == 1.0 and trail is None
        assert "L:GOOGL-USD" not in c._armed_at

    def test_gap_suppression_pops_prior_arm(self):
        c = self._colony()
        c.boost("AMD-USD", "long", {"GOOGL-USD": 2.5}, None)
        assert "L:GOOGL-USD" in c._armed_at
        c.boost("AMD-USD", "long", {"GOOGL-USD": 2.5}, None,
                gaps={"GOOGL-USD": -3.0})
        assert "L:GOOGL-USD" not in c._armed_at

    def test_ungapped_leader_still_arms(self):
        c = self._colony()
        mult, trail = c.boost("AMD-USD", "long", {"USTECH100-USD": 2.5},
                              None, gaps={"USTECH100-USD": 0.3})
        assert mult == pytest.approx(1.25) and trail[0] == "leadlag"


class TestTieredSettle:
    def _colony(self, hour, minute):
        return EquityColony(clock=lambda: _ts_et(hour, minute))

    def test_small_gap_fires_after_5min(self):
        assert self._colony(9, 34).boost(
            "AMD-USD", "short", {}, None,
            gaps={"AMD-USD": 2.0}) == (1.0, None)
        m, _ = self._colony(9, 36).boost("AMD-USD", "short", {}, None,
                                         gaps={"AMD-USD": 2.0})
        assert m == pytest.approx(1.08)

    def test_mid_gap_fires_after_15min(self):
        assert self._colony(9, 44).boost(
            "AMD-USD", "short", {}, None,
            gaps={"AMD-USD": 5.0}) == (1.0, None)
        m, _ = self._colony(9, 46).boost("AMD-USD", "short", {}, None,
                                         gaps={"AMD-USD": 5.0})
        assert m == pytest.approx(1.15)

    def test_large_gap_fires_after_30min(self):
        assert self._colony(9, 59).boost(
            "AMD-USD", "short", {}, None,
            gaps={"AMD-USD": 8.0}) == (1.0, None)
        m, _ = self._colony(10, 0).boost("AMD-USD", "short", {}, None,
                                         gaps={"AMD-USD": 8.0})
        assert m == pytest.approx(1.20)


class TestFreshestClose:
    def test_latest_fresh_close(self):
        from intelligence import equity_gap as eg
        now = 100000.0
        assert eg.freshest_close([(now - 900, 100.0),
                                  (now - 60, 101.0)], now) == 101.0

    def test_frozen_feed_abstains(self):
        from intelligence import equity_gap as eg
        now = 100000.0
        assert eg.freshest_close([(now - 30 * 60, 100.0)], now) is None

    def test_degenerate_input_abstains(self):
        from intelligence import equity_gap as eg
        assert eg.freshest_close([], 100000.0) is None
        assert eg.freshest_close(None, 100000.0) is None
        assert eg.freshest_close([(100000.0, -5.0)], 100000.0) is None
        assert eg.freshest_close([(100000.0, 1.0)], "junk") is None

    def test_malformed_rows_skipped(self):
        from intelligence import equity_gap as eg
        now = 100000.0
        rows = [("bad", 1.0), (now - 60, "bad"), (now - 120, 99.5)]
        assert eg.freshest_close(rows, now) == 99.5


class TestPairConvergence:
    def _colony(self):
        return EquityColony(clock=lambda: _ts_et(12, 0))

    def test_laggard_long_boosted(self):
        c = self._colony()
        mult, trail = c.boost("HOOD-USD", "long",
                              {"COIN-USD": 3.0, "HOOD-USD": 0.5}, None)
        assert mult == pytest.approx(1.12)
        assert trail[0] == "pairconv" and trail[1] == "COIN-USD"

    def test_laggard_short_boosted(self):
        c = self._colony()
        mult, _ = c.boost("COIN-USD", "short",
                          {"COIN-USD": -0.5, "HOOD-USD": -3.2}, None)
        assert mult == pytest.approx(1.12)

    def test_mover_direction_abstains(self):
        # chasing the mover is not convergence
        c = self._colony()
        mult, trail = c.boost("COIN-USD", "long",
                              {"COIN-USD": 3.0, "HOOD-USD": 0.5}, None)
        assert mult == 1.0 and trail is None

    def test_spread_below_threshold_abstains(self):
        c = self._colony()
        mult, trail = c.boost("HOOD-USD", "long",
                              {"COIN-USD": 1.5, "HOOD-USD": 0.5}, None)
        assert mult == 1.0 and trail is None

    def test_non_bridge_symbol_untouched(self):
        c = self._colony()
        mult, trail = c.boost("AMD-USD", "long",
                              {"COIN-USD": 4.0, "AMD-USD": 0.5}, None)
        assert mult == 1.0 and trail is None

    def test_pair_trail_reinforces(self):
        c = self._colony()
        _, trail = c.boost("HOOD-USD", "long",
                           {"COIN-USD": 3.0, "HOOD-USD": 0.5}, None)
        c.reinforce("HOOD-USD", trail, False)
        assert c.weights["COIN-USD->HOOD-USD"] == pytest.approx(0.95)


class TestIntegrationSevenStep:
    """The review's pre-live scenario, one colony walked through it:
    leader arms a follower -> decay halves -> decay kills -> a gapped
    leader is suppressed -> session tiers read position state."""

    def test_full_scenario(self):
        t0 = _ts_et(10, 0)
        st = {"now": t0}
        c = EquityColony(clock=lambda: st["now"])
        # 1. GOOGL +1.2% arms the AMD trail at full boost
        m1, trail = c.boost("AMD-USD", "long", {"GOOGL-USD": 1.2}, None)
        assert m1 == pytest.approx(1.25) and trail[0] == "leadlag"
        # 2. +31 min (~1 half-life): the boost has roughly halved
        st["now"] = t0 + 1860.0
        m2, _ = c.boost("AMD-USD", "long", {"GOOGL-USD": 1.2}, None)
        assert m2 == pytest.approx(1.125, abs=0.01)
        # 3. +120 min total (4 half-lives, scale 0.0625 < 0.10): dead
        st["now"] = t0 + 7200.0
        m3, trail3 = c.boost("AMD-USD", "long", {"GOOGL-USD": 1.2}, None)
        assert m3 == 1.0 and trail3 is None
        # 4. AMD gaps +8% overnight: gap-suppression disarms AMD as a
        #    leader (event noise, not rotation) so COIN gets no boost
        st["now"] = _ts_et(11, 0)
        m4, trail4 = c.boost("COIN-USD", "long", {"AMD-USD": 3.0}, None,
                             gaps={"AMD-USD": 8.0})
        assert m4 == 1.0 and trail4 is None
        assert "L:AMD-USD" not in c._armed_at
        # 5-7. session tiers read position state (module-level, same leg)
        ah = _ts_et(16, 1)
        assert es.size_mult(ah, book_open=False) == pytest.approx(1.0)
        assert es.size_mult(ah, book_open=True) == pytest.approx(0.60)
        assert es.size_mult(_ts_et(20, 1)) == pytest.approx(0.50)


class TestColonySubfamilies:
    def test_new_additions_in_semis(self):
        assert "AMD-USD" in SUBFAMILIES["AI_SEMIS"]
        assert "DRAM-USD" in SUBFAMILIES["AI_SEMIS"]

    def test_config_knobs(self):
        from core.config import Settings
        s = Settings()
        assert s.equity_session_sizing_enabled is True
        assert s.equity_session_core_mult == 0.75
        assert s.equity_session_premarket_mult == 0.90
        assert s.equity_session_ah_open_mult == 1.0
        assert s.equity_session_ah_hold_mult == 0.60
        assert s.equity_session_ah_event_mult == 0.40
        assert s.equity_session_overnight_mult == 0.50
        assert s.equity_colony_enabled is True
        assert s.colony_boost_max == 0.25
        assert s.colony_gap_min_pct == 1.0
        assert s.colony_gap_boost_max == 0.20
        assert s.colony_gap_settle_min == 30
        assert s.colony_gap_settle_small_min == 5
        assert s.colony_gap_settle_mid_min == 15
        assert s.colony_trail_halflife_s == 1800.0
        assert s.colony_trail_min_scale == 0.10
        assert s.colony_pair_spread_pct == 2.0
        assert s.colony_pair_boost == 0.12


class TestColonyWiring:
    def _src(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "main.py")) as fh:
            return fh.read()

    def test_sizing_legs_and_decorrelation(self):
        src = self._src()
        for leg in ("eq_session_mult", "colony_mult"):
            assert leg in src
        for name in ('"eq_session"', '"equity_colony"'):
            assert name in src

    def test_reinforcement_and_feed(self):
        src = self._src()
        assert "_equity_colony.reinforce" in src
        assert "_equity_colony.evaporate" in src
        assert "_colony_leader_moves.update" in src

    def test_gap_and_pair_feed(self):
        # Phase B: gaps computed from the rolling 16:00 ET anchor cache and
        # passed into boost(); CRYPTO_ADJ day moves now feed the pair leg.
        src = self._src()
        assert "gaps=_colony_gaps" in src
        assert "_colony_gap_anchors" in src
        assert "_equity_gap.latest_anchor_ts" in src
        assert "_colony_gaps.update" in src
        assert 'if _fam == "CRYPTO_ADJ":\n' not in src

    def test_session_v2_and_decay_wiring(self):
        # Session v2: position state + catalyst feed the regime; colony
        # decay knobs + freshest_close staleness guard wired.
        src = self._src()
        assert "book_open=" in src
        assert "event=" in src
        assert "_sess_book_open" in src
        assert "equity_session_ah_hold_mult" in src
        assert "_equity_gap.freshest_close" in src
        assert "colony_trail_halflife_s" in src
        assert "colony_trail_min_scale" in src

    def test_no_unbound_direction_var_in_legs(self):
        # 2026-09-22 dead-wiring catch: _sig_direction is assigned ~1600
        # lines below the sizing chain inside on_signal_ready — legs must
        # read candidate.side. Pin the boundary: no _sig_direction read
        # between the narr leg and the sizing_chain log.
        src = self._src()
        i0 = src.index("_narr_mult = 1.0")
        i1 = src.index('"sizing_chain"', i0)
        assert "_sig_direction" not in src[i0:i1]


class TestOffHoursFlow:
    """2026-09-23 doctrine migration: the Kant RTH hard block is retired in
    favor of the session-regime tiers (the venue, perp-kline plane and
    framework are all 24/7). False knob = legacy block bit-for-bit."""

    def _src(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "main.py")) as fh:
            return fh.read()

    def test_knob_defaults_on(self):
        from core.config import Settings
        assert Settings().equity_off_hours_flow_enabled is True

    def test_relief_and_legacy_paths_wired(self):
        src = self._src()
        assert "equity_off_hours_relieved" in src        # new 24/7 flow
        assert "equity_off_hours_blocked" in src         # legacy path kept
        assert "equity_off_hours_elite_override" in src  # legacy path kept
        assert "equity_off_hours_flow_enabled" in src

    def test_relief_carries_regime_telemetry(self):
        src = self._src()
        i = src.index("equity_off_hours_relieved")
        assert "regime=_eq_session_regime(_now_ts)" in src[i - 400:i + 400]

    def test_daily_cap_120(self):
        from execution.kant_gate import BALANCE_TIERS
        assert BALANCE_TIERS[0] == (1000.0, 120, 10)  # Governor 2026-09-23: 120/day + 10 concurrent at $1000
        assert BALANCE_TIERS[1] == (200.0, 70, 5)     # live ~$333 book stays 70/day

    def test_concurrent_cap_10(self):
        # The real concurrent cap lives in config (kant tier max_pos is vestigial
        # — kant_gate._balance_tier is unpacked as `_, max_global, _`).
        from core.config import Settings
        s = Settings.model_fields
        assert s["max_concurrent_positions"].default == 10
        assert s["alt_season_max_positions"].default == 10
