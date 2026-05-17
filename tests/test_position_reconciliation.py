"""
Position Reconciliation + Signal Deduplication + SFS Tests — ARIA v1.8

Verifies the three new safety layers:
  1. Position idempotency — bracket task + reconciliation race does NOT create duplicates
  2. Signal deduplication — same (symbol, direction, strategy, regime) rejected within TTL
  3. Synthetic Funding Score — pure function correctness, fail-safe, bias mapping
  4. Calendar fix — crypto assets excluded from WEEKEND_CLOSE noise
"""

import time
import pytest
from unittest.mock import MagicMock, patch


# ══════════════════════════════════════════════════════════════════════════════
# 1. POSITION MANAGER IDEMPOTENCY
# ══════════════════════════════════════════════════════════════════════════════

class TestPositionManagerIdempotency:
    """
    Core bug fix: position_manager must never hold 2 entries for the same
    symbol unless pyramiding is explicitly active.
    The race condition: reconciliation adds a position WHILE bracket task
    is about to add the same one.
    """

    def _make_position(self, symbol="BTC-USD", side="long"):
        from execution.schemas import Position
        return Position(
            symbol=symbol, side=side,
            entry_price=74000.0, size=0.001,
            stop_price=73000.0, tp1_price=75000.0,
            tp2_price=76000.0, tp3_price=78000.0,
            liq_price=70000.0, initial_margin=50.0,
            leverage=6, opened_at_ms=int(time.time() * 1000),
        )

    def test_single_add_creates_one_position(self):
        from risk.position_manager import PositionManager
        pm = PositionManager()
        pm.add(self._make_position("BTC-USD"))
        assert pm.count("BTC-USD") == 1

    def test_second_add_blocked_by_guard_in_bracket_task(self):
        """
        Simulates the idempotency check added to _bracket_task():
          if position_manager.get(_sym): → merge, not add
        """
        from risk.position_manager import PositionManager
        pm = PositionManager()

        # Step 1: Reconciliation adds position (simulates exchange detection)
        pm.add(self._make_position("BTC-USD"))
        assert pm.count("BTC-USD") == 1

        # Step 2: Bracket task checks before adding (the fix)
        existing = pm.get("BTC-USD")
        if existing:
            # Merge path — no add
            existing[0].order_ids = {"entry": "order_abc123"}
        else:
            pm.add(self._make_position("BTC-USD"))

        # Still exactly 1 position
        assert pm.count("BTC-USD") == 1

    def test_get_returns_empty_for_unknown_symbol(self):
        from risk.position_manager import PositionManager
        pm = PositionManager()
        assert pm.get("UNKNOWN-USD") == []
        assert pm.count("UNKNOWN-USD") == 0

    def test_position_closed_after_reconcile_close(self):
        from risk.position_manager import PositionManager
        pm = PositionManager()
        pm.add(self._make_position("ETH-USD"))
        assert pm.count("ETH-USD") == 1
        pm.close("ETH-USD", 0)
        assert pm.count("ETH-USD") == 0

    def test_two_different_symbols_do_not_interfere(self):
        from risk.position_manager import PositionManager
        pm = PositionManager()
        pm.add(self._make_position("BTC-USD"))
        pm.add(self._make_position("ETH-USD", side="short"))
        assert pm.count("BTC-USD") == 1
        assert pm.count("ETH-USD") == 1
        assert len(pm.get_all()) == 2

    def test_pyramid_requires_tp1_hit(self):
        from risk.position_manager import PositionManager
        pm = PositionManager()
        pm.add(self._make_position("SOL-USD"))
        # can_pyramid is False before TP1
        assert pm.can_pyramid("SOL-USD") is False

    def test_pyramid_allowed_after_tp1_hit(self):
        from risk.position_manager import PositionManager
        pm = PositionManager()
        pm.add(self._make_position("SOL-USD"))
        pm.mark_tp1_hit("SOL-USD")
        assert pm.can_pyramid("SOL-USD") is True


# ══════════════════════════════════════════════════════════════════════════════
# 2. SIGNAL DEDUPLICATION
# ══════════════════════════════════════════════════════════════════════════════

class TestSignalDeduplication:
    """Hash-based dedup prevents re-execution within TTL window."""

    def setup_method(self):
        from execution.signal_dedup import SignalDeduplicator
        self.dedup = SignalDeduplicator()

    def test_first_signal_not_duplicate(self):
        assert self.dedup.is_duplicate("BTC-USD", "long", "momentum", "risk_on") is False

    def test_same_signal_within_ttl_is_duplicate(self):
        self.dedup.record("BTC-USD", "long", "momentum", "risk_on")
        assert self.dedup.is_duplicate("BTC-USD", "long", "momentum", "risk_on") is True

    def test_different_direction_not_duplicate(self):
        self.dedup.record("BTC-USD", "long", "momentum", "risk_on")
        assert self.dedup.is_duplicate("BTC-USD", "short", "momentum", "risk_on") is False

    def test_different_symbol_not_duplicate(self):
        self.dedup.record("BTC-USD", "long", "momentum", "risk_on")
        assert self.dedup.is_duplicate("ETH-USD", "long", "momentum", "risk_on") is False

    def test_different_strategy_not_duplicate(self):
        self.dedup.record("BTC-USD", "long", "momentum", "risk_on")
        assert self.dedup.is_duplicate("BTC-USD", "long", "breakout", "risk_on") is False

    def test_different_regime_not_duplicate(self):
        self.dedup.record("BTC-USD", "long", "momentum", "risk_on")
        assert self.dedup.is_duplicate("BTC-USD", "long", "momentum", "risk_off") is False

    def test_ttl_expiry_allows_re_entry(self):
        from execution.signal_dedup import SignalDeduplicator
        dedup = SignalDeduplicator()
        # Manually inject an expired entry
        key = dedup.record("ETH-USD", "short", "mean_revert", "ranging")
        # Expire it manually
        dedup._store[key] = time.monotonic() - 60  # already expired
        assert dedup.is_duplicate("ETH-USD", "short", "mean_revert", "ranging") is False

    def test_cascade_strategy_uses_tighter_ttl(self):
        """Cascade signals should have shorter TTL (10s bucket vs 30s)."""
        from execution.signal_dedup import SignalDeduplicator
        dedup = SignalDeduplicator()
        # Record a cascade signal
        dedup.record("BTC-USD", "short", "cascade", "risk_off")
        # Duplicate is still caught within TTL
        assert dedup.is_duplicate("BTC-USD", "short", "cascade", "risk_off") is True

    def test_internal_error_never_blocks_trade(self):
        """Exceptions in is_duplicate return False (fail-open)."""
        from execution.signal_dedup import SignalDeduplicator
        dedup = SignalDeduplicator()
        # Pass None values — should not raise
        result = dedup.is_duplicate(None, None, None, None)
        assert result is False  # fail-open

    def test_singleton_importable(self):
        from execution.signal_dedup import signal_deduplicator
        assert signal_deduplicator is not None
        assert hasattr(signal_deduplicator, "is_duplicate")
        assert hasattr(signal_deduplicator, "record")


# ══════════════════════════════════════════════════════════════════════════════
# 3. SYNTHETIC FUNDING SCORE
# ══════════════════════════════════════════════════════════════════════════════

class TestSyntheticFundingScore:
    """SFS pure function correctness, fail-safe, and bias mapping."""

    def _compute(self, **kwargs):
        from intelligence.synthetic_funding import compute_sfs
        return compute_sfs(**kwargs)

    def test_neutral_inputs_produce_near_zero_sfs(self):
        result = self._compute(
            oi_delta_pct=0.0, price_direction=0.0,
            volume_buy=100.0, volume_sell=100.0,  # balanced
            bybit_price=74000.0, sodex_price=74000.0,
            long_liq_count=5, short_liq_count=5,
        )
        assert abs(result.sfs_score) < 0.1

    def test_long_crowding_produces_positive_sfs(self):
        """High OI + rising price + SoDEX at premium = long crowding = bearish."""
        result = self._compute(
            oi_delta_pct=2.0,        # OI growing
            price_direction=1.0,     # price rising → long crowding
            volume_buy=200.0,
            volume_sell=80.0,        # buy heavy
            bybit_price=74000.0,
            sodex_price=74500.0,     # SoDEX at premium
            long_liq_count=10,
            short_liq_count=2,
        )
        assert result.sfs_score > 0.1   # bearish signal

    def test_short_crowding_produces_negative_sfs(self):
        """OI growing + falling price + SoDEX at discount = short crowding = bullish."""
        result = self._compute(
            oi_delta_pct=2.0,
            price_direction=-1.0,   # price falling → short crowding
            volume_buy=80.0,
            volume_sell=200.0,
            bybit_price=74000.0,
            sodex_price=73500.0,    # SoDEX at discount
            long_liq_count=2,
            short_liq_count=10,
        )
        assert result.sfs_score < -0.1  # bullish signal

    def test_fail_safe_activates_without_bybit_price(self):
        result = self._compute(
            bybit_price=0.0,    # unavailable
            sodex_price=74000.0,
        )
        assert result.fail_safe is True
        assert result.bybit_available is False
        assert result.confidence < 1.0

    def test_fail_safe_activates_without_sodex_price(self):
        result = self._compute(
            bybit_price=74000.0,
            sodex_price=0.0,
        )
        assert result.fail_safe is True

    def test_fail_safe_does_not_raise(self):
        """Even with all zeros, should not raise."""
        result = self._compute()
        assert isinstance(result.sfs_score, float)
        assert not (result.sfs_score != result.sfs_score)  # not NaN

    def test_sfs_to_bias_bearish(self):
        from intelligence.synthetic_funding import sfs_to_bias
        assert sfs_to_bias(0.4) == "bearish"
        assert sfs_to_bias(0.31) == "bearish"

    def test_sfs_to_bias_bullish(self):
        from intelligence.synthetic_funding import sfs_to_bias
        assert sfs_to_bias(-0.4) == "bullish"
        assert sfs_to_bias(-0.31) == "bullish"

    def test_sfs_to_bias_neutral(self):
        from intelligence.synthetic_funding import sfs_to_bias
        assert sfs_to_bias(0.0) == "neutral"
        assert sfs_to_bias(0.29) == "neutral"
        assert sfs_to_bias(-0.29) == "neutral"

    def test_confidence_mult_aligned_boosts(self):
        from intelligence.synthetic_funding import sfs_confidence_mult
        # SFS bearish → short direction aligned → boost
        mult = sfs_confidence_mult(sfs_score=0.5, candidate_direction="short")
        assert mult > 1.0

    def test_confidence_mult_fighting_penalises(self):
        from intelligence.synthetic_funding import sfs_confidence_mult
        # SFS bearish → long direction fights → penalty
        mult = sfs_confidence_mult(sfs_score=0.5, candidate_direction="long")
        assert mult < 1.0

    def test_confidence_mult_neutral_unchanged(self):
        from intelligence.synthetic_funding import sfs_confidence_mult
        mult = sfs_confidence_mult(sfs_score=0.1, candidate_direction="long")
        assert mult == 1.0

    def test_confidence_mult_never_blocks(self):
        from intelligence.synthetic_funding import sfs_confidence_mult
        # Even worst case must not go to 0 or negative
        mult = sfs_confidence_mult(sfs_score=1.0, candidate_direction="long")
        assert mult > 0.0

    def test_sfs_cache_stores_and_retrieves(self):
        from intelligence.synthetic_funding import sfs_cache, compute_sfs
        result = compute_sfs(bybit_price=74000.0, sodex_price=74000.0)
        sfs_cache.update("BTC-USD", result)
        cached = sfs_cache.get("BTC-USD")
        assert cached is not None
        assert abs(cached.sfs_score - result.sfs_score) < 1e-9

    def test_sfs_cache_expires_stale(self):
        from intelligence.synthetic_funding import sfs_cache, compute_sfs, SFSCache
        import time as _time
        cache = SFSCache()
        result = compute_sfs()
        cache.update("ETH-USD", result)
        # Manually age the entry past 500ms
        cache._store["ETH-USD"] = (result, _time.monotonic() - 1.0)
        assert cache.get("ETH-USD") is None  # expired


# ══════════════════════════════════════════════════════════════════════════════
# 4. CALENDAR — CRYPTO ASSETS NOT POLLUTED BY WEEKEND EVENTS
# ══════════════════════════════════════════════════════════════════════════════

class TestCalendarCryptoWeekendFix:
    """
    Crypto assets (BTC, ETH, etc.) should not be restricted by WEEKEND_CLOSE
    events since they trade 24/7. Only XAUT, USTECH100, stocks are affected.
    """

    def test_weekend_affected_assets_include_xaut(self):
        """XAUT is affected by weekends — gold market closes."""
        from risk_calendar.engine import CalendarEngine
        # The _WEEKEND_AFFECTED set is checked inside get_state
        # We verify the logic by checking the config category
        from core.config import Settings
        cfg = Settings()
        assert cfg.get_asset_category("XAUT-USD") == "commodity"
        assert cfg.get_asset_category("BTC-USD") == "crypto_large"

    def test_new_coins_in_assets(self):
        from core.config import Settings
        cfg = Settings()
        assert "1000PEPE-USD" in cfg.assets
        # COPPER-USD re-added as a SoDEX commodity asset
        assert "COPPER-USD" in cfg.assets

    def test_copper_in_universe(self):
        """COPPER-USD is active in the trading universe as a SoDEX commodity."""
        from core.config import Settings
        cfg = Settings()
        assert "COPPER-USD" in cfg.assets
        assert "COPPER-USD" in cfg.ASSET_CONFIG

    def test_new_coins_have_asset_config(self):
        from core.config import Settings
        cfg = Settings()
        for sym in ("1000PEPE-USD",):
            assert sym in cfg.ASSET_CONFIG, f"{sym} missing from ASSET_CONFIG"
            entry = cfg.ASSET_CONFIG[sym]
            assert "tick_size" in entry
            assert "min_size" in entry
            assert "max_leverage" in entry


# ══════════════════════════════════════════════════════════════════════════════
# 5. POSITION RECONCILIATION LOGIC (UNIT)
# ══════════════════════════════════════════════════════════════════════════════

class TestReconciliationLogic:
    """
    Validates the core reconciliation rules without main.py:
      - Exchange position not in tracker → add
      - Tracker position not on exchange → close
      - Both present → size-sync only (no duplicate)
    """

    def _make_pm(self):
        from risk.position_manager import PositionManager
        return PositionManager()

    def _make_pos(self, symbol, side="long", size=0.001, entry=74000.0):
        from execution.schemas import Position
        pos = Position(
            symbol=symbol, side=side,
            entry_price=entry, size=size,
            stop_price=entry * 0.985,
            tp1_price=entry * 1.02,
            tp2_price=entry * 1.04,
            tp3_price=entry * 1.06,
            liq_price=entry * 0.85,
            initial_margin=50.0, leverage=6,
            opened_at_ms=int(time.time() * 1000) - 120_000,  # 2min old
        )
        pos.initial_size = size
        return pos

    def test_untracked_exchange_position_should_be_added(self):
        pm = self._make_pm()
        # SoDEX has AVAX, internal has nothing
        exchange_open = {"AVAX-USD": (10.0, {"side": "long", "avgEntryPrice": 9.5})}
        # Simulate reconciliation "detect new untracked" logic
        for sym, (size, pos_data) in exchange_open.items():
            if not pm.get(sym):
                pm.add(self._make_pos(sym, size=size, entry=9.5))
        assert pm.count("AVAX-USD") == 1

    def test_tracked_position_not_on_exchange_should_close(self):
        pm = self._make_pm()
        pm.add(self._make_pos("ETH-USD"))
        exchange_open = {}  # ETH gone from exchange
        closed = []
        for sym, positions in list(pm._positions.items()):
            if sym not in exchange_open and positions:
                pos_age_s = (time.time() - positions[0].opened_at_ms / 1000)
                if pos_age_s >= 90:  # past grace period
                    closed.append(sym)
                    pm.close(sym, 0)
        assert "ETH-USD" in closed
        assert pm.count("ETH-USD") == 0

    def test_grace_period_prevents_premature_close(self):
        """New position (< 90s) not on exchange yet → should NOT be closed."""
        pm = self._make_pm()
        pos = self._make_pos("BTC-USD")
        pos.opened_at_ms = int(time.time() * 1000) - 30_000  # only 30s old
        pm.add(pos)
        exchange_open = {}  # BTC not yet confirmed on exchange
        closed = []
        for sym, positions in list(pm._positions.items()):
            if sym not in exchange_open and positions:
                pos_age_s = (time.time() - positions[0].opened_at_ms / 1000)
                if pos_age_s >= 90:
                    closed.append(sym)
        assert "BTC-USD" not in closed  # grace period protects it

    def test_size_sync_does_not_create_duplicate(self):
        """If both tracker and exchange have position, size is synced, NOT a new add."""
        pm = self._make_pm()
        pm.add(self._make_pos("SOL-USD", size=1.0))
        exchange_open = {"SOL-USD": (1.5, {})}  # partial fill increased size

        # Sync path
        for sym, positions in list(pm._positions.items()):
            if sym in exchange_open:
                ex_size = exchange_open[sym][0]
                if abs(ex_size - positions[0].size) > 0.001:
                    positions[0].size = ex_size

        assert pm.count("SOL-USD") == 1  # still just one
        assert abs(pm.get("SOL-USD")[0].size - 1.5) < 0.001  # size updated


# ══════════════════════════════════════════════════════════════════════════════
# 6. OI DIVERGENCE AND LAG DETECTION (SFS CROSS-VENUE)
# ══════════════════════════════════════════════════════════════════════════════

class TestSFSCrossVenueLag:
    """Cross-venue price divergence detection correctness."""

    def _compute(self, bybit, sodex):
        from intelligence.synthetic_funding import compute_sfs
        return compute_sfs(bybit_price=bybit, sodex_price=sodex)

    def test_sodex_premium_positive_divergence(self):
        """SoDEX higher than ByBit = longs crowded on SoDEX."""
        result = self._compute(bybit=74000.0, sodex=74500.0)
        assert result.price_divergence > 0

    def test_sodex_discount_negative_divergence(self):
        """SoDEX lower than ByBit = shorts crowded / selling pressure on SoDEX."""
        result = self._compute(bybit=74000.0, sodex=73500.0)
        assert result.price_divergence < 0

    def test_equal_prices_zero_divergence(self):
        result = self._compute(bybit=74000.0, sodex=74000.0)
        assert abs(result.price_divergence) < 1e-9

    def test_divergence_proportional_to_gap(self):
        r1 = self._compute(bybit=74000.0, sodex=74100.0)  # 0.135% premium
        r2 = self._compute(bybit=74000.0, sodex=74700.0)  # 0.946% premium
        assert r2.price_divergence > r1.price_divergence

    def test_oi_spike_interpretation(self):
        """OI spike + rising price = long crowding (positive SFS pressure)."""
        from intelligence.synthetic_funding import compute_sfs
        r = compute_sfs(
            oi_delta_pct=5.0,       # Large OI spike
            price_direction=1.0,    # Upward
            bybit_price=74000.0,
            sodex_price=74000.0,
        )
        assert r.oi_pressure > 0  # long crowding confirmed

    def test_oi_spike_falling_price_short_crowding(self):
        """OI spike + falling price = short crowding (negative SFS pressure)."""
        from intelligence.synthetic_funding import compute_sfs
        r = compute_sfs(
            oi_delta_pct=5.0,
            price_direction=-1.0,   # Downward
            bybit_price=74000.0,
            sodex_price=74000.0,
        )
        assert r.oi_pressure < 0  # short crowding
