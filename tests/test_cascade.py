"""
Tests for cascade intelligence system:
  - CascadeTracker state machine
  - Coherence Tier 8 cascade aftermath boost
  - Cascade type classification (momentum vs exhaustion)
  - AdaptiveCalibrator fast/medium loops
  - SignalRanker EV scoring and cascade boost
  - CrossVenueSignal computation
"""

import pytest
import time
from unittest.mock import MagicMock, patch


# ── CascadeTracker ─────────────────────────────────────────────────────────────

class TestCascadeTracker:

    def _make_config(self, momentum_vel=3.0, momentum_notional=50_000.0, cascade_min_coh=3.0):
        cfg = MagicMock()
        cfg.momentum_velocity_threshold = momentum_vel
        cfg.momentum_notional_threshold = momentum_notional
        cfg.cascade_min_coherence = cascade_min_coh
        return cfg

    def _make_tracker(self, **kwargs):
        from intelligence.cascade_tracker import CascadeTracker
        return CascadeTracker(self._make_config(**kwargs))

    def test_initial_phase_is_idle(self):
        tracker = self._make_tracker()
        from intelligence.cascade_tracker import CascadePhase
        assert tracker.get_phase() == CascadePhase.IDLE

    def test_blocked_phase_on_exhaustion_cascade(self):
        tracker = self._make_tracker()
        from intelligence.cascade_tracker import CascadePhase
        # Inject timestamps to get near-zero velocity (decelerating)
        tracker._event_timestamps.extend([time.time() - 25, time.time() - 20])
        tracker.on_liquidation_batch(5, 10_000, "bearish")
        assert tracker.get_phase() == CascadePhase.BLOCKED
        assert not tracker.is_primed()
        assert not tracker.is_momentum()
        assert tracker.is_blocked()

    def test_momentum_phase_on_accelerating_cascade(self):
        tracker = self._make_tracker(momentum_vel=0.1, momentum_notional=1_000.0)
        from intelligence.cascade_tracker import CascadePhase
        # Inject many recent timestamps to produce positive velocity
        now = time.time()
        for i in range(5):
            tracker._event_timestamps.append(now - 14.0 + i)
        tracker.on_liquidation_batch(10, 100_000, "bearish", zscore=3.0)
        assert tracker.get_phase() == CascadePhase.MOMENTUM
        assert tracker.is_momentum()
        assert not tracker.is_blocked()

    def test_momentum_direction_is_with_cascade(self):
        tracker = self._make_tracker(momentum_vel=0.0, momentum_notional=0.0)
        now = time.time()
        for i in range(5):
            tracker._event_timestamps.append(now - 5.0 + i)
        tracker.on_liquidation_batch(5, 100_000, "bearish", zscore=3.0)
        direction, notional = tracker.consume_momentum()
        assert direction == "short"   # bearish cascade → trade WITH = short
        assert notional == 100_000

    def test_primed_direction_is_against_cascade(self):
        tracker = self._make_tracker()
        from intelligence.cascade_tracker import CascadePhase, AFTERMATH_MIN_SIGNALS
        # Force BLOCKED state
        tracker._event_timestamps.extend([time.time() - 25, time.time() - 20])
        tracker.on_liquidation_batch(5, 10_000, "bearish")
        assert tracker.get_phase() == CascadePhase.BLOCKED

        # Advance _blocked_at past the minimum dwell (60s) so check_aftermath()
        # doesn't hit the dwell gate and return early before evaluating signals.
        tracker._blocked_at = time.time() - 61.0

        # Mock aftermath to pass all checks (≥ AFTERMATH_MIN_SIGNALS = 2 required)
        with patch.object(tracker, "_evaluate_aftermath",
                          return_value={"price_overshoot": True,
                                        "vpin_recovering": True,
                                        "funding_normalising": True,
                                        "orderbook_rebuilding": True,
                                        "cross_venue_normalising": False}):
            tracker.check_aftermath()

        assert tracker.get_phase() == CascadePhase.PRIMED
        assert tracker.get_primed_direction() == "long"  # bearish cascade → recovery = long

    def test_consume_primed_resets_to_idle(self):
        tracker = self._make_tracker()
        from intelligence.cascade_tracker import CascadePhase
        # Force to PRIMED manually
        tracker._phase = CascadePhase.PRIMED
        tracker._primed_direction = "long"
        tracker._primed_at = time.time()
        tracker._last_snapshot = MagicMock()
        tracker._last_event_ts = time.time()

        direction = tracker.consume_primed()
        assert direction == "long"
        assert tracker.get_phase() == CascadePhase.IDLE

    def test_cooldown_prevents_double_fire(self):
        tracker = self._make_tracker()
        tracker.on_liquidation_batch(5, 10_000, "bearish")
        first_ts = tracker._last_cascade_signal_ms

        # Immediate second call — should be blocked by cooldown
        tracker.on_liquidation_batch(5, 10_000, "bearish")
        assert tracker._last_cascade_signal_ms == first_ts  # unchanged

    def test_auto_timeout_releases_blocked(self):
        tracker = self._make_tracker()
        from intelligence.cascade_tracker import CascadePhase, CASCADE_BLOCKED_TIMEOUT_S
        tracker._event_timestamps.extend([time.time() - 25, time.time() - 20])
        tracker.on_liquidation_batch(5, 10_000, "bearish")
        assert tracker.is_blocked()

        # Simulate 90s of silence
        tracker._last_event_ts = time.time() - CASCADE_BLOCKED_TIMEOUT_S - 1
        tracker.check_aftermath()
        assert tracker.get_phase() == CascadePhase.IDLE

    def test_blocked_gate_is_hard_block(self):
        tracker = self._make_tracker()
        from intelligence.cascade_tracker import CascadePhase
        tracker._phase = CascadePhase.BLOCKED
        assert tracker.is_blocked()
        assert not tracker.is_primed()
        assert not tracker.is_momentum()


# ── Cascade Coherence Tier 8 ───────────────────────────────────────────────────

class TestCascadeCoherenceBoost:

    def _make_engine(self):
        from intelligence.coherence import CoherenceEngine
        return CoherenceEngine()

    def test_no_cascade_fired_no_boost(self):
        engine = self._make_engine()
        base = {"tier8_cascade_fired": False, "regime": "risk_on", "market_type": "trend"}
        score, _, components = engine.calculate_weighted_score("BTC-USD", base)
        assert components.get("cascade_aftermath", 0.0) == 0.0

    def test_cascade_fired_adds_boost(self):
        engine = self._make_engine()
        base = {"tier8_cascade_fired": True, "regime": "risk_on", "market_type": "trend"}
        score, _, components = engine.calculate_weighted_score("BTC-USD", base)
        assert components.get("cascade_aftermath", 0.0) == 1.0

    def test_tier7_cross_venue_bonus_applied(self):
        engine = self._make_engine()
        # With cross-venue bonus
        with_bonus = {"tier7_cross_venue_bonus": 0.4, "regime": "risk_on"}
        no_bonus   = {"tier7_cross_venue_bonus": 0.0, "regime": "risk_on"}
        score_with, _, _ = engine.calculate_weighted_score("BTC-USD", with_bonus)
        score_no,   _, _ = engine.calculate_weighted_score("BTC-USD", no_bonus)
        assert score_with > score_no


# ── Momentum Cascade Classification ───────────────────────────────────────────

class TestMomentumClassification:

    def test_high_velocity_high_notional_is_momentum(self):
        from intelligence.cascade_tracker import CascadeTracker
        cfg = MagicMock()
        cfg.momentum_velocity_threshold = 1.0
        cfg.momentum_notional_threshold = 10_000.0
        cfg.cascade_min_coherence = 3.0
        tracker = CascadeTracker(cfg)
        assert tracker._is_momentum_cascade(velocity=2.0, total_notional=50_000.0)

    def test_low_velocity_is_exhaustion(self):
        from intelligence.cascade_tracker import CascadeTracker
        cfg = MagicMock()
        cfg.momentum_velocity_threshold = 3.0
        cfg.momentum_notional_threshold = 50_000.0
        cfg.cascade_min_coherence = 3.0
        tracker = CascadeTracker(cfg)
        assert not tracker._is_momentum_cascade(velocity=0.5, total_notional=100_000.0)

    def test_high_velocity_low_notional_is_exhaustion(self):
        from intelligence.cascade_tracker import CascadeTracker
        cfg = MagicMock()
        cfg.momentum_velocity_threshold = 1.0
        cfg.momentum_notional_threshold = 100_000.0
        cfg.cascade_min_coherence = 3.0
        tracker = CascadeTracker(cfg)
        assert not tracker._is_momentum_cascade(velocity=5.0, total_notional=1_000.0)


# ── AdaptiveCalibrator ─────────────────────────────────────────────────────────

class TestAdaptiveCalibrator:

    def _make_calibrator(self, min_coh=2.0, cascade_min=3.0):
        from memory.adaptive_calibrator import AdaptiveCalibrator
        cfg = MagicMock()
        cfg.min_coherence = min_coh
        cfg.cascade_min_coherence = cascade_min
        cfg.momentum_velocity_threshold = 3.0
        return AdaptiveCalibrator(cfg)

    def test_initial_coherence_min_from_config(self):
        cal = self._make_calibrator(min_coh=2.0)
        assert cal.get_coherence_minimum() == 2.0

    def test_fast_loop_raises_coherence_on_loss_streak(self):
        from memory.adaptive_calibrator import LOSS_STREAK_TRIGGER, COHERENCE_STEP
        cal = self._make_calibrator(min_coh=2.0)
        # Fire enough losses to trigger
        for _ in range(LOSS_STREAK_TRIGGER):
            cal.on_trade_closed(won=False, pnl=-10.0)
        assert cal.get_coherence_minimum() > 2.0

    def test_win_decays_coherence_min_back(self):
        from memory.adaptive_calibrator import LOSS_STREAK_TRIGGER
        cal = self._make_calibrator(min_coh=2.0)
        for _ in range(LOSS_STREAK_TRIGGER):
            cal.on_trade_closed(won=False, pnl=-10.0)
        raised_min = cal.get_coherence_minimum()
        cal.on_trade_closed(won=True, pnl=20.0)
        # After a win, min should start decaying back
        assert cal.get_coherence_minimum() <= raised_min

    def test_medium_loop_adjusts_tier_weights(self):
        from memory.adaptive_calibrator import MEDIUM_WINDOW
        cal = self._make_calibrator()
        # Feed 10 trades, all with microstructure score, 80% wins
        for i in range(MEDIUM_WINDOW):
            cal.on_trade_closed(
                won=(i < 8),
                pnl=10.0 if i < 8 else -5.0,
                tier_scores={"microstructure": 1.5, "regime": 0.5},
            )
        weights = cal.get_tier_weights()
        # microstructure had 80% wr vs ~80% portfolio wr — ratio ≈ 1.0
        assert "microstructure" in weights or "regime" in weights  # weight was set

    def test_cascade_loop_raises_cascade_coherence(self):
        cal = self._make_calibrator(cascade_min=3.0)
        # Feed 5 cascade-primed trades that all lose
        for _ in range(5):
            cal.on_trade_closed(won=False, pnl=-10.0, cascade_phase="primed")
        assert cal.get_cascade_min_coherence() > 3.0


# ── SignalRanker ───────────────────────────────────────────────────────────────

class TestSignalRanker:

    def _make_candidate(self, symbol, score, direction, strategy="unknown", age_s=5.0):
        cand = MagicMock()
        cand.symbol = symbol
        cand.score = score
        cand.direction = direction
        cand.strategy_tag = strategy
        cand.age_s.return_value = age_s
        state = MagicMock()
        state.rr_ratio = 2.5
        cand.state = state
        return cand

    def test_higher_score_ranks_first(self):
        from intelligence.signal_ranker import SignalRanker
        ranker = SignalRanker()
        candidates = [
            self._make_candidate("ETH-USD", score=2.0, direction="long"),
            self._make_candidate("BTC-USD", score=4.0, direction="long"),
        ]
        ranked = ranker.rank_candidates(candidates)
        assert ranked[0].symbol == "BTC-USD"

    def test_cascade_primed_boosts_matching_direction(self):
        from intelligence.signal_ranker import SignalRanker
        cascade_tracker = MagicMock()
        cascade_tracker.is_primed.return_value = True
        cascade_tracker.get_primed_direction.return_value = "long"
        cascade_tracker.is_blocked.return_value = False

        ranker = SignalRanker()
        candidates = [
            self._make_candidate("BTC-USD", score=3.0, direction="long"),
            self._make_candidate("ETH-USD", score=3.0, direction="short"),
        ]
        ranked = ranker.rank_candidates(candidates, cascade_tracker=cascade_tracker)
        # BTC long should rank first (cascade boost)
        assert ranked[0].symbol == "BTC-USD"
        assert ranked[0].cascade_boosted

    def test_cascade_blocked_suppresses_all(self):
        from intelligence.signal_ranker import SignalRanker
        cascade_tracker = MagicMock()
        cascade_tracker.is_blocked.return_value = True
        cascade_tracker.is_primed.return_value = False

        ranker = SignalRanker()
        candidates = [self._make_candidate("BTC-USD", score=5.0, direction="long")]
        ranked = ranker.rank_candidates(candidates, cascade_tracker=cascade_tracker)
        result = ranker.should_fire_next(ranked, cascade_tracker=cascade_tracker)
        assert result is None

    def test_stale_signal_penalized(self):
        from intelligence.signal_ranker import SignalRanker
        ranker = SignalRanker()
        fresh = self._make_candidate("BTC-USD", score=3.0, direction="long", age_s=5.0)
        stale = self._make_candidate("ETH-USD", score=3.0, direction="long", age_s=28.0)
        ranked = ranker.rank_candidates([fresh, stale])
        assert ranked[0].symbol == "BTC-USD"  # fresh wins


# ── CrossVenueSignal ────────────────────────────────────────────────────────────

class TestCrossVenueSignal:

    def test_neutral_when_spread_below_threshold(self):
        from funding.cross_venue_signal import compute_cross_venue_signal
        sig = compute_cross_venue_signal("BTC-USD", sodex_rate=0.001, bybit_rate=0.001)
        assert sig.direction == "neutral"
        assert sig.bonus == 0.0

    def test_lead_short_when_bybit_more_bullish(self):
        from funding.cross_venue_signal import compute_cross_venue_signal
        # bybit_rate > sodex_rate = Bybit crowd over-leveraged long → fade → SHORT
        sig = compute_cross_venue_signal("BTC-USD", sodex_rate=0.0005, bybit_rate=0.0010)
        assert sig.direction == "lead_short"
        assert sig.bonus > 0.0

    def test_lead_long_when_bybit_more_bearish(self):
        from funding.cross_venue_signal import compute_cross_venue_signal
        # bybit_rate < sodex_rate = Bybit crowd over-leveraged short → fade → LONG
        sig = compute_cross_venue_signal("BTC-USD", sodex_rate=0.0010, bybit_rate=0.0005)
        assert sig.direction == "lead_long"
        assert sig.bonus > 0.0

    def test_extreme_spread_gives_max_bonus(self):
        from funding.cross_venue_signal import compute_cross_venue_signal, MAX_BONUS
        sig = compute_cross_venue_signal("BTC-USD", sodex_rate=0.0000, bybit_rate=0.0010)
        assert sig.bonus == MAX_BONUS
        assert sig.confidence == 1.0

    def test_direction_match_function(self):
        from funding.cross_venue_signal import compute_cross_venue_signal, cross_venue_direction_matches
        sig = compute_cross_venue_signal("BTC-USD", sodex_rate=0.0005, bybit_rate=0.0010)
        assert cross_venue_direction_matches(sig, "short")
        assert not cross_venue_direction_matches(sig, "long")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
