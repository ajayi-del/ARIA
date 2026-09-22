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

    def test_after_hours(self):
        assert es.regime(_ts_et(16, 0)) == es.AFTER_HOURS
        assert es.regime(_ts_et(3, 59)) == es.AFTER_HOURS

    def test_core_mult_binds(self):
        m = es.size_mult(_ts_et(12, 0))
        assert m == pytest.approx(0.75)

    def test_edge_windows_full_size(self):
        assert es.size_mult(_ts_et(8, 0)) == 1.0
        assert es.size_mult(_ts_et(20, 0)) == 1.0

    def test_knob_override(self):
        m = es.size_mult(_ts_et(12, 0), {"CORE_HOURS": 0.5})
        assert m == pytest.approx(0.5)


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
        c = self._colony(hour=9, minute=45)              # 30-min settle
        mult, trail = c.boost("AMD-USD", "short", {}, None,
                              gaps={"AMD-USD": 5.0})
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


class TestColonySubfamilies:
    def test_new_additions_in_semis(self):
        assert "AMD-USD" in SUBFAMILIES["AI_SEMIS"]
        assert "DRAM-USD" in SUBFAMILIES["AI_SEMIS"]

    def test_config_knobs(self):
        from core.config import Settings
        s = Settings()
        assert s.equity_session_sizing_enabled is True
        assert s.equity_session_core_mult == 0.75
        assert s.equity_colony_enabled is True
        assert s.colony_boost_max == 0.25
        assert s.colony_gap_min_pct == 1.0
        assert s.colony_gap_boost_max == 0.20
        assert s.colony_gap_settle_min == 30
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

    def test_no_unbound_direction_var_in_legs(self):
        # 2026-09-22 dead-wiring catch: _sig_direction is assigned ~1600
        # lines below the sizing chain inside on_signal_ready — legs must
        # read candidate.side. Pin the boundary: no _sig_direction read
        # between the narr leg and the sizing_chain log.
        src = self._src()
        i0 = src.index("_narr_mult = 1.0")
        i1 = src.index('"sizing_chain"', i0)
        assert "_sig_direction" not in src[i0:i1]
