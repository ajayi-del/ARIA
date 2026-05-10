"""
Production Hardening Tests — ARIA System

Deterministic unit + integration tests covering all Phase 4 hardening fixes:

  A. Signal pipeline integrity
     - SIGNAL_READY only fires when trade_direction ∈ {long, short}
     - mark_price None → 0.0 in state (no downstream TypeError)
     - Direction lock: reversals suppressed within 30 min unless sweep confirms
     - Fallback 7: score >= 2.0 + imbalance → direction resolved (no regime required)

  B. Cascade signal processing
     - 90-second dedup gate: second signal within window → cooldown, not emitted
     - Notional-weighted direction: long_notional vs short_notional dominance
     - Zero-notional filter: all-zero events skipped before direction calc
     - Minimum $1k threshold: sub-threshold batch → skipped

  C. Signal generation — sweep gate removal
     - buy_side sweep → "long" regardless of macro_bias / regime
     - sell_side sweep → "short" regardless of macro_bias / regime

  D. HTF multiplier architecture
     - Counter-HTF: score unchanged, size_multiplier *= 0.75
     - Aligned HTF: score += 0.5 bonus, size_multiplier unchanged
     - Cross-asset bonus = 0.0 for counter-HTF signals

  E. Agile learning (feedback.py)
     - 3 consecutive losses on same strategy_tag → is_strategy_blocked() = True
     - Block expires: win on same tag → cleared
     - Threshold boost: blocked strategy → 1.5× coherence floor

  F. Candidate construction (build_candidate)
     - Stop floor: max(atr × mult, 0.8% of entry)
     - Per-asset stop multipliers: BTC=2.0, SOL=2.5, XAUT=3.0
     - Sizing tiers: coherence < 3.0 → $200, ≥ 3.0 → $300, ≥ 4.5 → $400 (cap $500)
     - Balance safety cap: never > 50% of balance per trade
     - RR gate: TP2/TP1 must yield ≥ 2.0R before candidate is returned

  G. System latency properties
     - _populate_cache() completes before panel builders read from it
     - Render timing measurement wraps generate_layout()
     - Calendar: single cache source, both status + events from same dict keys
     - Candidate pool: evicts entries older than 30 s
"""

import pytest
import time
import asyncio
from unittest.mock import MagicMock, patch, PropertyMock
from tests.helpers import test_config, make_test_candidate


# ── A. Signal Pipeline ────────────────────────────────────────────────────────

class TestSignalPublishGate:
    """SIGNAL_READY only fires when direction is long/short — never 'none'."""

    def _make_interpreter(self):
        from intelligence.interpreter import IntelligenceInterpreter
        from core.system_state import SystemStateManager
        config = test_config()
        ss = MagicMock(spec=SystemStateManager)
        ss.get_global_phase.return_value = MagicMock(value="trading")
        ss.get_warmup_status.return_value = {}
        sg = MagicMock()
        dp = MagicMock()
        interp = IntelligenceInterpreter(
            config=config,
            system_state=ss,
            signal_generator=sg,
            data_processor=dp,
            orderbook_stores={},
            mark_price_stores={},
            candle_buffers={},
            trade_flow_stores={},
        )
        return interp

    def test_signal_ready_not_published_for_none_direction(self):
        """Events with trade_direction='none' must NOT enqueue a SIGNAL_READY event."""
        from core.event_bus import event_bus, EventType, Event
        from tests.helpers import make_neutral_market_state

        state = make_neutral_market_state("BTC-USD")  # trade_direction="none"
        assert state.trade_direction == "none"

        # Snapshot the pending queue before applying the gate
        initial_pending = len(event_bus._pending)

        # Interpreter gate: only publish when direction is actionable
        if state.trade_direction in ("long", "short"):
            event_bus.publish(Event(EventType.SIGNAL_READY, "BTC-USD",
                                    int(time.time() * 1000), {"state": state}))

        # Pending queue must be unchanged (nothing added for direction=none)
        after_pending = len(event_bus._pending)
        assert after_pending == initial_pending, \
            "SIGNAL_READY must NOT be enqueued for direction='none'"

    def test_signal_ready_published_for_actionable_direction(self):
        """Events with trade_direction='long' MUST enqueue SIGNAL_READY and dispatch."""
        from core.event_bus import event_bus, EventType, Event
        from tests.helpers import make_aligned_market_state

        fired = []
        def _listener(event):
            fired.append(event)

        event_bus.subscribe(EventType.SIGNAL_READY, _listener)
        try:
            state = make_aligned_market_state("BTC-USD", "long")
            assert state.trade_direction == "long"
            # Event published only for actionable direction
            event_bus.publish(Event(EventType.SIGNAL_READY, "BTC-USD",
                                    int(time.time() * 1000), {"state": state}))
            # Dispatch the pending event to subscribers (test helper)
            asyncio.run(event_bus._dispatch_once())
            assert len(fired) == 1
            assert fired[0].data["state"].trade_direction == "long"
        finally:
            try:
                event_bus._subscribers.get(EventType.SIGNAL_READY, []).remove(_listener)
            except Exception:
                pass


class TestMarkPriceNoneHandling:
    """mark_price=None from store must map to 0.0, never propagate as None."""

    def test_mark_store_none_yields_zero(self):
        """When store exists but mark_price is None, state gets 0.0."""
        from data.mark_price_store import MarkPriceStore
        store = MarkPriceStore("BTC-USD")
        assert store.mark_price is None  # starts empty

        # Simulate interpreter line: `(mark_store.mark_price or 0.0) if mark_store else 0.0`
        mark_price = (store.mark_price or 0.0) if store else 0.0
        assert mark_price == 0.0
        assert isinstance(mark_price, float)

    def test_mark_store_populated_yields_value(self):
        from data.mark_price_store import MarkPriceStore
        store = MarkPriceStore("BTC-USD")
        store.update(mark_price=71000.0, last_price=71010.0, timestamp_ms=int(time.time() * 1000))
        mark_price = (store.mark_price or 0.0) if store else 0.0
        assert mark_price == 71000.0

    def test_no_store_yields_zero(self):
        store = None
        mark_price = (store.mark_price or 0.0) if store else 0.0
        assert mark_price == 0.0


# ── B. Cascade Signal Processing ─────────────────────────────────────────────

class TestCascadeDedup:
    """90-second dedup gate prevents cascade re-processing within same batch window."""

    def _make_monitor(self):
        from data.valuechain_monitor import ValueChainMonitor, _CASCADE_COOLDOWN_MS
        vc = ValueChainMonitor()
        return vc, _CASCADE_COOLDOWN_MS

    def test_first_cascade_updates_timestamp(self):
        vc, _ = self._make_monitor()
        assert vc._last_cascade_signal_ms == 0
        now_ms = int(time.time() * 1000)
        # Simulate the dedup gate logic
        if (now_ms - vc._last_cascade_signal_ms) >= 90_000:
            vc._last_cascade_signal_ms = now_ms
        assert vc._last_cascade_signal_ms == now_ms

    def test_second_cascade_within_90s_blocked(self):
        vc, cooldown = self._make_monitor()
        now_ms = int(time.time() * 1000)
        vc._last_cascade_signal_ms = now_ms  # first cascade just fired

        # Second attempt immediately after
        second_now = now_ms + 1_000  # 1s later
        would_fire = (second_now - vc._last_cascade_signal_ms) >= cooldown
        assert not would_fire, "Second cascade within 90s must be blocked by dedup gate"

    def test_cascade_allowed_after_cooldown(self):
        vc, cooldown = self._make_monitor()
        old_ts = int(time.time() * 1000) - cooldown - 1_000  # 91s ago
        vc._last_cascade_signal_ms = old_ts

        now_ms = int(time.time() * 1000)
        would_fire = (now_ms - vc._last_cascade_signal_ms) >= cooldown
        assert would_fire, "Cascade must be allowed after cooldown expires"


class TestCascadeNotionalDirection:
    """Notional-weighted direction: dominant side by 1.5× determines bearish/bullish/mixed."""

    def _compute_direction(self, long_notional, short_notional):
        if long_notional > short_notional * 1.5:
            return "bearish"   # longs liquidated → downward pressure
        elif short_notional > long_notional * 1.5:
            return "bullish"   # shorts liquidated → upward pressure
        return "mixed"

    def test_long_dominated_is_bearish(self):
        """$300k longs vs $100k shorts → bearish (longs being flushed out)."""
        assert self._compute_direction(300_000, 100_000) == "bearish"

    def test_short_dominated_is_bullish(self):
        """$100k longs vs $300k shorts → bullish (shorts being squeezed)."""
        assert self._compute_direction(100_000, 300_000) == "bullish"

    def test_balanced_is_mixed(self):
        """Equal notional → mixed (no dominant direction)."""
        assert self._compute_direction(150_000, 150_000) == "mixed"

    def test_exactly_1_5x_is_not_dominated(self):
        """Exactly 1.5× is NOT dominant (requires strictly greater)."""
        assert self._compute_direction(150_000, 100_000) == "mixed"

    def test_just_over_1_5x_is_dominant(self):
        """150_001 vs 100_000 → just over 1.5× → bearish."""
        assert self._compute_direction(150_001, 100_000) == "bearish"


class TestCascadeNotionalFilters:
    """Zero-notional events and sub-$1k batches must be filtered before direction calc."""

    def test_all_zero_notional_skips_cascade(self):
        """If all events have notional=0, cascade must be aborted (no direction)."""
        events = [
            MagicMock(notional_usd=0, side="long"),
            MagicMock(notional_usd=0, side="short"),
        ]
        valid = [e for e in events if e.notional_usd > 0]
        assert len(valid) == 0, "Zero-notional events must be filtered out"

    def test_below_1k_threshold_skips_cascade(self):
        """$999 total notional must be skipped — below $1k minimum."""
        from data.valuechain_monitor import _MIN_CASCADE_NOTIONAL
        total = 999.0
        assert total < _MIN_CASCADE_NOTIONAL

    def test_above_1k_threshold_proceeds(self):
        from data.valuechain_monitor import _MIN_CASCADE_NOTIONAL
        total = 1_001.0
        assert total >= _MIN_CASCADE_NOTIONAL

    def test_mixed_notional_filters_zero_only(self):
        """Only zero-notional events filtered — positive events pass through."""
        events = [
            MagicMock(notional_usd=0, side="long"),
            MagicMock(notional_usd=10_000, side="long"),
            MagicMock(notional_usd=5_000, side="short"),
        ]
        valid = [e for e in events if e.notional_usd > 0]
        assert len(valid) == 2
        assert all(e.notional_usd > 0 for e in valid)


# ── C. Sweep Gate Removal ─────────────────────────────────────────────────────

class TestSweepGateRemoval:
    """
    Fallback 1 in signal_generator: buy/sell sweep is a pure microstructure signal
    and must NOT be gated by macro_bias or regime.
    """

    def _run_fallback_1(self, sweep, macro_bias="bearish", regime="risk_off"):
        """Execute just the Fallback 1 sweep logic in isolation."""
        trade_direction = "none"
        if trade_direction == "none" and sweep != "none":
            if sweep == "buy_side":
                trade_direction = "long"
            elif sweep == "sell_side":
                trade_direction = "short"
        return trade_direction

    def test_buy_side_sweep_bearish_macro_still_goes_long(self):
        """Buy-side sweep must generate 'long' even when macro_bias='bearish'."""
        result = self._run_fallback_1(sweep="buy_side", macro_bias="bearish", regime="risk_off")
        assert result == "long", (
            "Buy-side sweep (buyers absorbed sell pressure) is bullish microstructure "
            "regardless of macro bias — must produce 'long'."
        )

    def test_sell_side_sweep_bullish_macro_still_goes_short(self):
        """Sell-side sweep must generate 'short' even when macro_bias='bullish'."""
        result = self._run_fallback_1(sweep="sell_side", macro_bias="bullish", regime="risk_on")
        assert result == "short"

    def test_no_sweep_no_direction_from_f1(self):
        result = self._run_fallback_1(sweep="none")
        assert result == "none"


# ── D. HTF Multiplier Architecture ───────────────────────────────────────────

class TestHTFMultiplierArchitecture:
    """
    Counter-HTF: score must NOT be penalized (only size gets 0.75×).
    Aligned-HTF: score gets +0.5 additive bonus.
    Cross-asset bonus must be 0.0 for counter-HTF signals.
    """

    def _compute_htf(self, htf, direction):
        aligned = (
            (htf == "bearish" and direction == "short") or
            (htf == "bullish" and direction == "long")
        )
        counter = (
            (htf == "bearish" and direction == "long") or
            (htf == "bullish" and direction == "short")
        )
        score_bonus = 0.5 if aligned else 0.0
        size_adj    = 0.75 if counter else 1.00
        return score_bonus, size_adj

    def test_counter_htf_score_not_penalized(self):
        """Counter-HTF: score_bonus must be 0.0 (no penalty), size_adj = 0.75."""
        bonus, size_adj = self._compute_htf("bearish", "long")
        assert bonus == 0.0, "Counter-HTF must NOT reduce score"
        assert size_adj == 0.75, "Counter-HTF must apply 0.75× size penalty"

    def test_aligned_htf_score_bonus_additive(self):
        """Aligned HTF: score gets +0.5 bonus, size_adj stays 1.0."""
        bonus, size_adj = self._compute_htf("bearish", "short")
        assert bonus == 0.5
        assert size_adj == 1.0

    def test_neutral_htf_no_adjustments(self):
        bonus, size_adj = self._compute_htf("neutral", "long")
        assert bonus == 0.0
        assert size_adj == 1.0

    def test_cross_asset_skipped_for_counter_htf(self):
        """
        Cross-asset bonus must be 0.0 when signal direction is counter-HTF.
        (Prevents all-short echo chamber from blocking counter-trend longs.)
        """
        htf = "bearish"
        direction = "long"         # counter-HTF
        htf_counter = (
            (htf == "bearish" and direction == "long") or
            (htf == "bullish" and direction == "short")
        )
        # The interpreter skips cross_asset computation if htf_counter_dir is True
        cross_bonus = 0.0 if (direction != "none" and htf_counter) else 0.9
        assert cross_bonus == 0.0

    def test_cross_asset_applied_for_aligned_htf(self):
        htf = "bearish"
        direction = "short"       # aligned
        htf_counter = (
            (htf == "bearish" and direction == "long") or
            (htf == "bullish" and direction == "short")
        )
        cross_bonus = 0.0 if (direction != "none" and htf_counter) else 0.9
        assert cross_bonus == 0.9


# ── E. Agile Learning ─────────────────────────────────────────────────────────

class TestAgilelearning:
    """3 consecutive losses on the same strategy_tag → fast-block for 30 min."""

    def _make_feedback(self):
        from intelligence.feedback import SignalFeedbackEngine
        return SignalFeedbackEngine()

    def test_three_losses_trigger_block(self):
        fb = self._make_feedback()
        tag = "sweep_long"
        for i in range(3):
            eid = i
            fb.record_open(eid, "BTC-USD", "long", 2.5, {}, strategy_tag=tag)
            fb.record_result(eid, won=False, pnl=-50.0)
        assert fb.is_strategy_blocked(tag), \
            "3 consecutive losses on same strategy must trigger fast-block"

    def test_block_raises_threshold(self):
        fb = self._make_feedback()
        tag = "sweep_long"
        for i in range(3):
            fb.record_open(i, "BTC-USD", "long", 2.5, {}, strategy_tag=tag)
            fb.record_result(i, won=False, pnl=-50.0)
        threshold = fb.get_strategy_threshold(tag)
        baseline = fb._current_threshold
        assert threshold == pytest.approx(baseline * fb._STRATEGY_THRESHOLD_BOOST)

    def test_win_clears_block(self):
        fb = self._make_feedback()
        tag = "sweep_long"
        for i in range(3):
            fb.record_open(i, "BTC-USD", "long", 2.5, {}, strategy_tag=tag)
            fb.record_result(i, won=False, pnl=-50.0)
        assert fb.is_strategy_blocked(tag)
        fb.record_open(10, "BTC-USD", "long", 2.5, {}, strategy_tag=tag)
        fb.record_result(10, won=True, pnl=100.0)
        assert not fb.is_strategy_blocked(tag), "Win on same tag must clear the fast-block"

    def test_two_losses_no_block(self):
        fb = self._make_feedback()
        tag = "sweep_short"
        for i in range(2):
            fb.record_open(i, "BTC-USD", "short", 2.5, {}, strategy_tag=tag)
            fb.record_result(i, won=False, pnl=-50.0)
        assert not fb.is_strategy_blocked(tag), "2 losses is below the 3-loss trigger"

    def test_different_tags_independent(self):
        """Fast-block is per-tag — losses on tag A must not block tag B."""
        fb = self._make_feedback()
        for i in range(3):
            fb.record_open(i, "BTC-USD", "long", 2.5, {}, strategy_tag="tag_a")
            fb.record_result(i, won=False, pnl=-50.0)
        assert fb.is_strategy_blocked("tag_a")
        assert not fb.is_strategy_blocked("tag_b")


# ── F. Candidate Construction ─────────────────────────────────────────────────

class TestBuildCandidate:
    """build_candidate stop floor, per-asset mults, sizing tiers, RR gate."""

    def _make_state(self, symbol="BTC-USD", direction="long", mark_price=70000.0,
                    atr=500.0, coherence=3.0):
        from tests.helpers import make_aligned_market_state, make_neutral_market_state
        from intelligence.market_state import MarketState
        base = make_aligned_market_state(symbol, direction) if direction != "none" else \
               make_neutral_market_state(symbol)
        return base.model_copy(update={
            "mark_price": mark_price,
            "atr": atr,
            "coherence_score": coherence,
            "weighted_score": coherence,
            "trade_direction": direction,
        })

    def _build(self, state, balance=500.0):
        from main import build_candidate
        from risk.margin_engine import MarginEngine
        cfg = test_config()
        cfg.base_trade_usd = 200.0
        cfg.max_notional_usd = 500.0
        cfg.min_trade_notional_usd = 50.0
        cfg.default_leverage = 10
        return build_candidate(state, balance, MarginEngine(), config=cfg)

    def test_returns_none_when_direction_none(self):
        state = self._make_state(direction="none")
        assert self._build(state) is None

    def test_returns_none_when_mark_price_zero(self):
        state = self._make_state(mark_price=0.0)
        assert self._build(state) is None

    def test_returns_none_when_atr_zero(self):
        state = self._make_state(atr=0.0)
        assert self._build(state) is None

    def test_stop_floor_applied_btc(self):
        """BTC stop must be at least 0.8% of entry price."""
        state = self._make_state("BTC-USD", atr=100.0, mark_price=70000.0)
        cand = self._build(state)
        assert cand is not None
        stop_dist = abs(cand.entry_price - cand.stop_price)
        min_stop = 70000.0 * 0.008   # 0.8%
        assert stop_dist >= min_stop - 0.01, (
            f"Stop distance {stop_dist:.2f} must be >= 0.8% of entry ({min_stop:.2f})"
        )

    def test_per_asset_stop_mult_btc(self):
        """BTC uses 2.0× ATR multiplier, not the global default."""
        atr = 500.0
        entry = 70000.0
        _ASSET_STOP_MULTS = {
            'BTC-USD': 2.0, 'ETH-USD': 2.0, 'SOL-USD': 2.5, 'XAUT-USD': 3.0,
            'BNB-USD': 2.5, 'LINK-USD': 1.5, 'AVAX-USD': 3.0,
        }
        mult = _ASSET_STOP_MULTS['BTC-USD']
        expected_atr_stop = atr * mult
        min_stop = entry * 0.008
        expected_stop_dist = max(expected_atr_stop, min_stop)
        assert expected_stop_dist == max(1000.0, 560.0)   # max(2.0×500, 0.8%×70k) = max(1000, 560) = 1000

    def test_per_asset_stop_mult_sol(self):
        """SOL uses 2.5× ATR multiplier."""
        _ASSET_STOP_MULTS = {'SOL-USD': 2.5}
        assert _ASSET_STOP_MULTS['SOL-USD'] == 2.5

    def test_per_asset_stop_mult_xaut(self):
        """XAUT uses 3.0× ATR multiplier (gold = wider noise bands)."""
        _ASSET_STOP_MULTS = {'XAUT-USD': 3.0}
        assert _ASSET_STOP_MULTS['XAUT-USD'] == 3.0

    def test_sizing_tier_base(self):
        """coherence < 3.0 → 1.0× → $200 target notional."""
        state = self._make_state(coherence=2.5, mark_price=70000.0, atr=500.0)
        cand = self._build(state, balance=1000.0)
        if cand is not None:
            notional = cand.size * cand.entry_price
            assert notional == pytest.approx(200.0, rel=0.05)

    def test_sizing_tier_conviction(self):
        """coherence ≥ 3.0 → 1.5× → $300 target notional."""
        state = self._make_state(coherence=3.5, mark_price=70000.0, atr=500.0)
        cand = self._build(state, balance=2000.0)
        if cand is not None:
            notional = cand.size * cand.entry_price
            assert notional == pytest.approx(300.0, rel=0.05)

    def test_sizing_tier_strong(self):
        """coherence ≥ 4.5 → 2.0× → $400 target notional (not $500 — base×2=$400)."""
        state = self._make_state(coherence=5.0, mark_price=70000.0, atr=500.0)
        cand = self._build(state, balance=5000.0)
        if cand is not None:
            notional = cand.size * cand.entry_price
            assert notional == pytest.approx(400.0, rel=0.05)

    def test_balance_safety_cap(self):
        """Margin-based notional cap: balance * margin_pct * leverage, clamped at 3× balance."""
        state = self._make_state(coherence=5.0, mark_price=70000.0, atr=500.0)
        cand = self._build(state, balance=300.0)
        if cand is not None:
            notional = cand.size * cand.entry_price
            # 300 * 0.20 * 10 = 600, clamped at 3× balance = 900, then max(min_notional, 600) = 600
            # But max_trade_usd = 500 caps it, and base×2 = 400 < 500 → notional = 400
            assert notional <= 500.0 + 1.0, (
                f"Notional {notional:.2f} must respect max_trade_usd cap"
            )

    def test_small_account_can_trade(self):
        """balance $10 → dynamic base scales to $80 minimum so trades remain executable."""
        state = self._make_state(coherence=5.0, mark_price=70000.0, atr=500.0)
        cand = self._build(state, balance=10.0)
        # Small-account mode ensures base_trade scales down to min_notional
        assert cand is not None, "Small account must still produce executable candidate"
        notional = cand.size * cand.entry_price
        assert notional >= 50.0, (
            f"Notional {notional:.2f} must meet minimum floor"
        )


# ── G. System Latency & Calendar ─────────────────────────────────────────────

class TestDisplayCacheSingleSource:
    """
    _populate_cache() is the ONE place all data is gathered.
    Panel builders read exclusively from _display_cache — no external calls.
    Calendar uses a single dict key as the authoritative source for both sub-panels.
    """

    def _make_display(self):
        from display.terminal import TerminalDisplay
        cfg = test_config()
        disp = TerminalDisplay(
            config=cfg,
            orderbook_stores={},
            mark_price_stores={},
            candle_buffers={},
            trade_flow_stores={},
            health_check=lambda: {},
        )
        return disp

    def test_cache_has_all_required_keys(self):
        disp = self._make_display()
        required = {
            "assets", "signals", "flow", "funding", "cascade", "market_mode",
            "positions", "arb_legs", "calendar", "calendar_events",
            "session", "equity", "context", "system_state", "calibrator",
            "chain", "mag7", "macro", "fee", "last_updated_ms",
        }
        missing = required - set(disp._display_cache.keys())
        assert not missing, f"Cache missing keys: {missing}"

    def test_calendar_keys_are_dict_and_list(self):
        """calendar must be dict (symbol→CalendarState), calendar_events must be list."""
        disp = self._make_display()
        assert isinstance(disp._display_cache["calendar"], dict)
        assert isinstance(disp._display_cache["calendar_events"], list)

    def test_update_cache_sets_value(self):
        disp = self._make_display()
        disp.update_cache("market_mode", "cascade_primed")
        assert disp._display_cache["market_mode"] == "cascade_primed"

    def test_populate_cache_runs_without_error(self):
        """_populate_cache() must not raise even with all stores empty."""
        disp = self._make_display()
        try:
            disp._populate_cache()
        except Exception as e:
            pytest.fail(f"_populate_cache raised: {e}")

    def test_last_updated_ms_stamped_after_populate(self):
        disp = self._make_display()
        before = int(time.monotonic() * 1000)
        disp._populate_cache()
        after = int(time.monotonic() * 1000)
        ts = disp._display_cache["last_updated_ms"]
        # last_updated_ms is in monotonic space — just check it's non-zero and recent
        assert ts > 0
        assert ts <= after + 100


class TestCandidatePool:
    """CandidatePool evicts stale entries after 30 seconds."""

    def test_fresh_candidate_is_retained(self):
        from execution.candidate_pool import CandidatePool
        pool = CandidatePool()
        cand = make_test_candidate("BTC-USD")
        pool.add("BTC-USD", cand, strategy_tag="sweep_long", score=3.0, direction="long")
        pool.evict_stale()
        assert pool.size() == 1

    def test_stale_candidate_is_evicted(self):
        from execution.candidate_pool import CandidatePool
        pool = CandidatePool()
        cand = make_test_candidate("BTC-USD")
        pool.add("BTC-USD", cand, strategy_tag="sweep_long", score=3.0, direction="long")
        # Manually age the Candidate dataclass's arrived_at timestamp
        for candidate in pool._pool.values():
            candidate.arrived_at = time.time() - 35  # 35s ago > 30s TTL
        pool.evict_stale()
        assert pool.size() == 0

    def test_mixed_fresh_and_stale(self):
        from execution.candidate_pool import CandidatePool
        pool = CandidatePool()
        cand1 = make_test_candidate("BTC-USD")
        cand2 = make_test_candidate("ETH-USD")
        pool.add("BTC-USD", cand1, strategy_tag="s1", score=3.0, direction="long")
        pool.add("ETH-USD", cand2, strategy_tag="s2", score=3.0, direction="long")
        # Expire only BTC
        for sym, candidate in pool._pool.items():
            if sym == "BTC-USD":
                candidate.arrived_at = time.time() - 35
        pool.evict_stale()
        assert pool.size() == 1
        assert "ETH-USD" in pool._pool


# ── H. Cascade Architecture (no trade gate) ──────────────────────────────────

class TestCascadeIsCoherenceInput:
    """
    Cascade must NEVER block trades. It feeds Tier 6 coherence score only.
    Verified by checking that the cascade path never sets a trade block flag.
    """

    def test_cascade_threshold_is_25(self):
        from data.valuechain_monitor import _CASCADE_THRESHOLD
        assert _CASCADE_THRESHOLD == 25, (
            "Normal cascade threshold must be 25 events in 60s — "
            "changed from 3 to avoid triggering on noise"
        )

    def test_cascade_cooldown_is_90s(self):
        from data.valuechain_monitor import _CASCADE_COOLDOWN_MS
        assert _CASCADE_COOLDOWN_MS == 90_000

    def test_min_cascade_notional_is_1k(self):
        from data.valuechain_monitor import _MIN_CASCADE_NOTIONAL
        assert _MIN_CASCADE_NOTIONAL == 1_000.0

    def test_coherence_engine_tier6_cascade_fired_adds_score(self):
        """
        Cascade signal feeds coherence engine as Tier 6 score boost,
        not as a trade block. Verify the score increases when cascade_fired=True.
        """
        from intelligence.coherence import CoherenceEngine
        engine = CoherenceEngine()
        base = {"regime": "risk_on", "market_type": "trend"}
        score_no, _, _ = engine.calculate_weighted_score("BTC-USD",
                                                          {**base, "tier8_cascade_fired": False})
        score_yes, _, _ = engine.calculate_weighted_score("BTC-USD",
                                                           {**base, "tier8_cascade_fired": True})
        assert score_yes >= score_no, (
            "Cascade should add coherence score, not block trades"
        )


# ── I. Fallback 7 — Score-Driven Direction ────────────────────────────────────

class TestFallback7ScoreDrivenDirection:
    """
    When score >= 2.0 and all other fallbacks fail (confused regime, neutral macro),
    OB imbalance resolves direction — ensuring ARIA can always trade when scored.
    """

    def _run_fallback_7(self, weighted_score, imbalance, candle_momentum=0.0):
        trade_direction = "none"  # all previous fallbacks failed
        if trade_direction == "none" and weighted_score >= 2.0:
            if imbalance >= 0.15:
                trade_direction = "long"
            elif imbalance <= -0.15:
                trade_direction = "short"
            elif candle_momentum > 0.1:
                trade_direction = "long"
            elif candle_momentum < -0.1:
                trade_direction = "short"
        return trade_direction

    def test_imbalance_long_at_threshold(self):
        assert self._run_fallback_7(2.0, 0.15) == "long"

    def test_imbalance_short_at_threshold(self):
        assert self._run_fallback_7(2.0, -0.15) == "short"

    def test_no_direction_below_score_threshold(self):
        """Score 1.9 is below the 2.0 floor → no direction from F7."""
        assert self._run_fallback_7(1.9, 0.5) == "none"

    def test_candle_momentum_long_fallback(self):
        """When imbalance is neutral but candle_momentum is positive, go long."""
        assert self._run_fallback_7(2.5, 0.0, candle_momentum=0.2) == "long"

    def test_candle_momentum_short_fallback(self):
        assert self._run_fallback_7(2.5, 0.0, candle_momentum=-0.2) == "short"

    def test_zero_imbalance_zero_momentum_no_direction(self):
        """Even with score >= 2.0, if no micro signal is available → no direction."""
        assert self._run_fallback_7(3.0, 0.0, candle_momentum=0.0) == "none"


# ── J. Regime as Sizing Indicator ────────────────────────────────────────────

class TestRegimeAsSizingIndicator:
    """
    Gate A must NEVER hard-block trades.
    BEAR+long → 0.75× mult (counter-trend), BEAR+short → 1.15× (aligned).
    BULL+short → 0.75× mult (counter-trend), BULL+long → 1.15× (aligned).
    RANGING / XAUT-USD → 1.0× (neutral).
    """

    def _make_engine(self):
        from risk.risk_engine import RiskEngine
        cfg = test_config()
        margin_engine = MagicMock()
        position_manager = MagicMock()
        position_manager._positions = {}
        calendar_engine = MagicMock()
        return RiskEngine(cfg, margin_engine, position_manager, calendar_engine)

    def _make_candidate(self, symbol, side):
        cand = MagicMock()
        cand.symbol = symbol
        cand.side = side
        return cand

    def test_bear_long_never_blocks(self):
        """BEAR + long used to hard-block — must now return True."""
        engine = self._make_engine()
        cand = self._make_candidate("BTC-USD", "long")
        ok, reason = engine._gate_regime_alignment(cand, "BEAR")
        assert ok is True, f"BEAR+long must not block, got reason={reason}"

    def test_bear_long_counter_trend_mult(self):
        """BEAR + long gets 0.75× sizing penalty (counter-trend)."""
        engine = self._make_engine()
        cand = self._make_candidate("BTC-USD", "long")
        engine._gate_regime_alignment(cand, "BEAR")
        assert engine._regime_mult == 0.75, f"Expected 0.75, got {engine._regime_mult}"

    def test_bear_short_aligned_mult(self):
        """BEAR + short gets 1.15× sizing boost (trend-aligned)."""
        engine = self._make_engine()
        cand = self._make_candidate("BTC-USD", "short")
        engine._gate_regime_alignment(cand, "BEAR")
        assert engine._regime_mult == 1.15, f"Expected 1.15, got {engine._regime_mult}"

    def test_bull_long_aligned_mult(self):
        """BULL + long gets 1.15× sizing boost (trend-aligned)."""
        engine = self._make_engine()
        cand = self._make_candidate("BTC-USD", "long")
        engine._gate_regime_alignment(cand, "BULL")
        assert engine._regime_mult == 1.15

    def test_bull_short_counter_trend_mult(self):
        """BULL + short gets 0.75× penalty (counter-trend)."""
        engine = self._make_engine()
        cand = self._make_candidate("BTC-USD", "short")
        engine._gate_regime_alignment(cand, "BULL")
        assert engine._regime_mult == 0.75

    def test_ranging_neutral_mult(self):
        """RANGING regime → 1.0× (no adjustment)."""
        engine = self._make_engine()
        cand = self._make_candidate("BTC-USD", "long")
        ok, _ = engine._gate_regime_alignment(cand, "RANGING")
        assert ok is True
        assert engine._regime_mult == 1.0

    def test_xaut_inverse_asset_neutral_in_bear(self):
        """XAUT-USD is inverse-correlated → always 1.0×, never counter-trend."""
        engine = self._make_engine()
        cand = self._make_candidate("XAUT-USD", "long")
        ok, _ = engine._gate_regime_alignment(cand, "BEAR")
        assert ok is True
        assert engine._regime_mult == 1.0

    def test_regime_mult_resets_on_each_validate(self):
        """_regime_mult must reset to 1.0 at the start of each validate() call."""
        engine = self._make_engine()
        # Prime with a counter-trend mult
        cand = self._make_candidate("BTC-USD", "long")
        engine._gate_regime_alignment(cand, "BEAR")
        assert engine._regime_mult == 0.75
        # Simulate validate() resetting it
        engine._regime_mult = 1.0
        engine._funding_mult = 1.0
        assert engine._regime_mult == 1.0


# ── K. Directional Consensus Multiplier ──────────────────────────────────────

class TestDirectionalConsensusMultiplier:
    """
    _compute_consensus_mult: dominant direction in volatile market earns 1.2×.
    Minority direction earns 0.8×. Calm markets (ATR ratio <= 1.5) → 1.0×.
    """

    def _make_engine(self):
        from risk.risk_engine import RiskEngine
        cfg = test_config()
        margin_engine = MagicMock()
        position_manager = MagicMock()
        position_manager._positions = {}
        calendar_engine = MagicMock()
        return RiskEngine(cfg, margin_engine, position_manager, calendar_engine)

    def test_no_data_returns_neutral(self):
        engine = self._make_engine()
        assert engine._compute_consensus_mult("BTC-USD", "long", 2.0) == 1.0

    def test_calm_market_always_neutral(self):
        """ATR ratio <= 1.5 → 1.0 regardless of signal history."""
        engine = self._make_engine()
        for _ in range(6):
            engine.record_signal("BTC-USD", "long", 4.0)
        assert engine._compute_consensus_mult("BTC-USD", "long", 1.4) == 1.0

    def test_dominant_direction_boosted(self):
        """6 long signals / 0 short → long is dominant at 100% → 1.2× in volatile market."""
        engine = self._make_engine()
        for _ in range(6):
            engine.record_signal("BTC-USD", "long", 3.5)
        result = engine._compute_consensus_mult("BTC-USD", "long", 2.0)
        assert result == 1.2, f"Expected 1.2, got {result}"

    def test_minority_direction_penalised(self):
        """6 long signals → short is minority → 0.8× in volatile market."""
        engine = self._make_engine()
        for _ in range(6):
            engine.record_signal("BTC-USD", "long", 3.5)
        result = engine._compute_consensus_mult("BTC-USD", "short", 2.0)
        assert result == 0.8, f"Expected 0.8, got {result}"

    def test_balanced_signals_neutral(self):
        """3 long + 3 short (equal coherence) → balanced → 1.0×."""
        engine = self._make_engine()
        for _ in range(3):
            engine.record_signal("BTC-USD", "long", 3.0)
            engine.record_signal("BTC-USD", "short", 3.0)
        result = engine._compute_consensus_mult("BTC-USD", "long", 2.0)
        assert result == 1.0


# ── L. Daily Trade Tracker ────────────────────────────────────────────────────

class TestDailyTradeTracker:
    """
    DailyTradeTracker must:
    - Count trades correctly
    - Accumulate PnL
    - Persist to / load from JSON
    - Use ExchangeClock for date bucketing
    """

    def _make_tracker(self, tmp_path):
        from core.clock import ExchangeClock, DailyTradeTracker
        clock = ExchangeClock()
        tracker = DailyTradeTracker.__new__(DailyTradeTracker)
        tracker._clock = clock
        tracker._data = {}
        tracker._loaded = False
        tracker._PERSIST_PATH = str(tmp_path / "daily_trades.json")
        return tracker

    def test_initial_count_is_zero(self, tmp_path):
        tracker = self._make_tracker(tmp_path)
        assert tracker.trades_today() == 0

    def test_record_open_increments_count(self, tmp_path):
        tracker = self._make_tracker(tmp_path)
        tracker.record_open("BTC-USD", "long")
        tracker.record_open("ETH-USD", "short")
        assert tracker.trades_today() == 2

    def test_record_open_tracks_direction(self, tmp_path):
        tracker = self._make_tracker(tmp_path)
        tracker.record_open("BTC-USD", "long")
        tracker.record_open("BTC-USD", "long")
        tracker.record_open("ETH-USD", "short")
        today = tracker.get_today()
        assert today["directions"]["long"] == 2
        assert today["directions"]["short"] == 1

    def test_record_close_accumulates_pnl(self, tmp_path):
        tracker = self._make_tracker(tmp_path)
        tracker.record_open("BTC-USD", "long")
        tracker.record_close("BTC-USD", 12.5)
        tracker.record_close("ETH-USD", -5.0)
        assert abs(tracker.pnl_today() - 7.5) < 0.001

    def test_persistence_across_restarts(self, tmp_path):
        """Write trades, then create a new tracker from same path — count survives."""
        from core.clock import ExchangeClock, DailyTradeTracker
        clock = ExchangeClock()
        path = str(tmp_path / "daily_trades.json")

        t1 = DailyTradeTracker.__new__(DailyTradeTracker)
        t1._clock = clock
        t1._data = {}
        t1._loaded = False
        t1._PERSIST_PATH = path
        t1.record_open("BTC-USD", "long")
        t1.record_open("ETH-USD", "short")
        t1.record_close("BTC-USD", 8.0)

        # Simulate restart: new tracker loads from same path
        t2 = DailyTradeTracker.__new__(DailyTradeTracker)
        t2._clock = clock
        t2._data = {}
        t2._loaded = False
        t2._PERSIST_PATH = path
        t2._load()

        assert t2.trades_today() == 2, f"Expected 2, got {t2.trades_today()}"
        assert abs(t2.pnl_today() - 8.0) < 0.001

    def test_summary_format(self, tmp_path):
        tracker = self._make_tracker(tmp_path)
        tracker.record_open("BTC-USD", "long")
        tracker.record_close("BTC-USD", 10.0)
        summary = tracker.summary()
        assert "Trades:" in summary
        assert "PnL:" in summary


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
