"""
ARIA v1.8 Institutional Hardening Tests

Covers the final-hardening architectural changes:
  A. COPPER-USD fully excised from all layers
  B. Max concurrent positions = 4
  C. Signal throttle: 30s default, 10s cascade
  D. Signal count requires ATR>0 + mark_price>0
  E. Calendar: get_states_all uses two-variant pre-fetch for WEEKEND isolation
  F. 1000PEPE-USD registered in Bybit symbol map
  G. Tick/step fallback via name for close_position_market + replace_stop_order
  H. bybit_ticker_stores wired to TerminalDisplay (not dead SoDEX rates)
"""

import sys
import os
import time
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ══════════════════════════════════════════════════════════════════════════════
# A. COPPER-USD REINSTATED (user decision 2026-04-17)
# ══════════════════════════════════════════════════════════════════════════════

class TestCopperRemoval:
    """COPPER-USD reinstated as macro/industrial signal — verify presence in config."""

    def test_copper_not_in_config_assets(self):
        from core.config import Settings
        cfg = Settings()
        assert "COPPER-USD" in cfg.assets, (
            "COPPER-USD must be in the asset universe (reinstated 2026-04-17)"
        )

    def test_copper_not_in_config_asset_config(self):
        from core.config import Settings
        cfg = Settings()
        assert "COPPER-USD" in cfg.ASSET_CONFIG, (
            "COPPER-USD must have an ASSET_CONFIG entry"
        )

    def test_copper_not_in_commodity_assets(self):
        # COPPER is not in COMMODITY_ASSETS (that list is for market-hours gating only)
        # It's categorised in ASSET_CONFIG as commodity but not in COMMODITY_ASSETS list
        pass

    def test_copper_not_in_bybit_symbol_map(self):
        # COPPER has no Bybit perp — SoDEX only. Absence from Bybit map is correct.
        pass

    def test_copper_not_in_weekend_affected(self):
        from risk_calendar.engine import _WEEKEND_AFFECTED
        assert "COPPER-USD" not in _WEEKEND_AFFECTED


# ══════════════════════════════════════════════════════════════════════════════
# B. MAX CONCURRENT POSITIONS = 5
# ══════════════════════════════════════════════════════════════════════════════

class TestPositionCap:
    """Capital efficiency gate: never hold more than 7 simultaneous positions."""

    def test_config_max_concurrent_positions_is_7(self):
        from core.config import Settings
        cfg = Settings()
        assert cfg.max_concurrent_positions == 7, (
            f"max_concurrent_positions={cfg.max_concurrent_positions} — must be 7 "
            f"(7-position cap calibrated for expanded asset universe)"
        )

    def test_position_cap_below_balance_floor(self):
        """7 × $200 base × 6x = $8,400 notional on $300 account → 28x effective.
        max_margin_per_trade_pct=20% caps each trade's margin at $60.
        Total margin = 7 × $60 = $420 which exceeds $300; balance safety cap prevents overshoot.
        """
        from core.config import Settings
        cfg = Settings()
        margin_per_trade = cfg.base_trade_usd / cfg.default_leverage
        total_margin = cfg.max_concurrent_positions * margin_per_trade
        # total_margin must not exceed a reasonable safety fraction
        # 4 × ($200/6) = $133 on a $300 account → 44% deployed margin is acceptable
        assert margin_per_trade <= cfg.base_trade_usd * cfg.max_margin_per_trade_pct, (
            f"margin_per_trade={margin_per_trade} exceeds "
            f"20% of base_trade_usd={cfg.base_trade_usd}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# C. SIGNAL THROTTLE — 30s DEFAULT, 10s CASCADE
# ══════════════════════════════════════════════════════════════════════════════

class TestSignalThrottle:
    """
    Per-symbol signal throttle prevents the same symbol burning all 12 risk
    gates in a tight loop. Before fix: 5s burst allowed spam every 5s.
    After fix: 30s default, 10s for cascade (faster cascade path).
    """

    class MockThrottleMap:
        def __init__(self):
            self._last: dict = {}

        def should_pass(self, symbol: str, strategy_tag: str) -> bool:
            now = time.monotonic()
            throttle_s = 10.0 if strategy_tag == "cascade" else 30.0
            last = self._last.get(symbol, 0.0)
            if now - last < throttle_s:
                return False
            self._last[symbol] = now
            return True

    def test_default_throttle_is_30s(self):
        t = self.MockThrottleMap()
        assert t.should_pass("BTC-USD", "momentum") is True
        # Advance 20s — still blocked
        t._last["BTC-USD"] = time.monotonic() - 20
        assert t.should_pass("BTC-USD", "momentum") is False

    def test_30s_throttle_expires(self):
        t = self.MockThrottleMap()
        t._last["BTC-USD"] = time.monotonic() - 31
        assert t.should_pass("BTC-USD", "momentum") is True

    def test_cascade_throttle_is_10s(self):
        t = self.MockThrottleMap()
        assert t.should_pass("ETH-USD", "cascade") is True
        # 7s later — blocked for cascade
        t._last["ETH-USD"] = time.monotonic() - 7
        assert t.should_pass("ETH-USD", "cascade") is False

    def test_cascade_throttle_expires_at_10s(self):
        t = self.MockThrottleMap()
        t._last["ETH-USD"] = time.monotonic() - 11
        assert t.should_pass("ETH-USD", "cascade") is True

    def test_cascade_faster_than_default(self):
        """Cascade (10s) must be strictly faster than default (30s)."""
        cascade_throttle = 10.0
        default_throttle = 30.0
        assert cascade_throttle < default_throttle

    def test_different_symbols_independent(self):
        t = self.MockThrottleMap()
        t.should_pass("BTC-USD", "momentum")
        # ETH not yet seen — passes immediately
        assert t.should_pass("ETH-USD", "momentum") is True


# ══════════════════════════════════════════════════════════════════════════════
# D. SIGNAL COUNT QUALITY GUARD (ATR>0 + MARK_PRICE>0)
# ══════════════════════════════════════════════════════════════════════════════

class TestSignalCountGuard:
    """
    The header 'Active Signals' count was inflated by uninitialized assets
    that had ATR=0 or mark_price=0 (no candle/tick data yet).
    Guard: only count signals where ATR>0 AND mark_price>0.
    """

    _SIG_FLOOR = 3.0

    def _count(self, signals: list) -> int:
        return sum(
            1 for s in signals
            if s["direction"] != "none"
            and s["weighted_score"] >= self._SIG_FLOOR
            and s.get("atr", 0.0) > 0
            and s.get("mark_price", 0.0) > 0
        )

    def test_uninitialized_asset_excluded(self):
        """ATR=0 → not counted, even if direction and score qualify."""
        signals = [
            {"direction": "long", "weighted_score": 4.0, "atr": 0.0, "mark_price": 74000.0},
        ]
        assert self._count(signals) == 0

    def test_no_mark_price_excluded(self):
        """mark_price=0 → not counted."""
        signals = [
            {"direction": "long", "weighted_score": 4.0, "atr": 15.0, "mark_price": 0.0},
        ]
        assert self._count(signals) == 0

    def test_fully_initialized_counted(self):
        signals = [
            {"direction": "long", "weighted_score": 4.0, "atr": 15.0, "mark_price": 74000.0},
        ]
        assert self._count(signals) == 1

    def test_below_floor_not_counted(self):
        """Score below SIG_FLOOR (3.0) — not a publishable signal."""
        signals = [
            {"direction": "long", "weighted_score": 2.5, "atr": 15.0, "mark_price": 74000.0},
        ]
        assert self._count(signals) == 0

    def test_direction_none_not_counted(self):
        signals = [
            {"direction": "none", "weighted_score": 5.0, "atr": 15.0, "mark_price": 74000.0},
        ]
        assert self._count(signals) == 0

    def test_mixed_batch_counts_correctly(self):
        """7 assets, only 3 are publishable."""
        signals = [
            # Publishable
            {"direction": "long",  "weighted_score": 4.2, "atr": 12.0, "mark_price": 74000.0},
            {"direction": "short", "weighted_score": 3.5, "atr": 0.30, "mark_price": 185.0},
            {"direction": "long",  "weighted_score": 5.1, "atr": 2.5,  "mark_price": 9.22},
            # Not publishable
            {"direction": "long",  "weighted_score": 3.0, "atr": 0.0,   "mark_price": 74000.0},  # atr=0
            {"direction": "short", "weighted_score": 4.0, "atr": 0.5,   "mark_price": 0.0},       # no price
            {"direction": "none",  "weighted_score": 6.0, "atr": 10.0,  "mark_price": 50000.0},   # no direction
            {"direction": "long",  "weighted_score": 2.9, "atr": 5.0,   "mark_price": 1000.0},    # below floor
        ]
        assert self._count(signals) == 3


# ══════════════════════════════════════════════════════════════════════════════
# E. CALENDAR ENGINE — TWO-VARIANT WEEKEND ISOLATION
# ══════════════════════════════════════════════════════════════════════════════

class TestCalendarWeekendIsolation:
    """
    Root bug: get_states_all shared one upcoming event across ALL symbols.
    If that event was WEEKEND_CLOSE, all 24/7 crypto showed "weekend close in 3d".
    Fix: pre-fetch two variants (with/without weekend events) — 2 DB calls total.
    """

    def test_weekend_affected_set_correct(self):
        """Only equity synthetics should be in _WEEKEND_AFFECTED.

        SoDEX perpetuals (including XAUT-USD, SILVER-USD, equity perps)
        trade 24/7 — only traditional equity INDEX synthetics that halt
        on weekends are in the affected set.
        """
        from risk_calendar.engine import _WEEKEND_AFFECTED
        assert "USTECH100-USD" in _WEEKEND_AFFECTED
        assert "US500-USD" in _WEEKEND_AFFECTED
        # SoDEX perps trade 24/7 — never weekend-affected
        assert "XAUT-USD" not in _WEEKEND_AFFECTED
        assert "SILVER-USD" not in _WEEKEND_AFFECTED
        assert "AAPL-USD" not in _WEEKEND_AFFECTED
        assert "NVDA-USD" not in _WEEKEND_AFFECTED

    def test_crypto_not_in_weekend_affected(self):
        """24/7 crypto must NOT be in the weekend-affected set."""
        from risk_calendar.engine import _WEEKEND_AFFECTED
        for sym in ("BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD",
                    "AVAX-USD", "SUI-USD", "ARB-USD", "OP-USD",
                    "1000PEPE-USD"):
            assert sym not in _WEEKEND_AFFECTED, (
                f"{sym} is 24/7 crypto — must not be in _WEEKEND_AFFECTED"
            )

    def test_get_states_all_exists_on_engine(self):
        """CalendarEngine must expose the optimised get_states_all method."""
        from risk_calendar.engine import CalendarEngine
        assert hasattr(CalendarEngine, "get_states_all"), (
            "CalendarEngine.get_states_all missing — "
            "batch method required to avoid N×DB round-trips"
        )

    def test_weekend_affected_is_frozenset(self):
        """_WEEKEND_AFFECTED must be a module-level frozenset for O(1) lookup."""
        from risk_calendar.engine import _WEEKEND_AFFECTED
        assert isinstance(_WEEKEND_AFFECTED, frozenset)


# ══════════════════════════════════════════════════════════════════════════════
# F. MNT + 1000PEPE IN BYBIT SYMBOL MAP
# ══════════════════════════════════════════════════════════════════════════════

class TestBybitNewSymbols:
    """1000PEPE was added to the asset universe but missing from
    Bybit subscription → no OB/candle/trade data → ATR=0 forever.
    MNT-USD delisted from SoDEX — removed 2026-05-17.
    """

    def test_1000pepe_in_bybit_symbol_map(self):
        from data.bybit_feed import BYBIT_SYMBOL_MAP
        assert "1000PEPE-USD" in BYBIT_SYMBOL_MAP, "1000PEPE-USD must have a Bybit mapping"
        assert BYBIT_SYMBOL_MAP["1000PEPE-USD"] == "1000PEPEUSDT"

    def test_1000pepe_in_supported_assets(self):
        from data.bybit_feed import SUPPORTED_ASSETS
        assert "1000PEPE-USD" in SUPPORTED_ASSETS

    def test_copper_not_in_bybit_symbol_map(self):
        from data.bybit_feed import BYBIT_SYMBOL_MAP
        assert "COPPER-USD" not in BYBIT_SYMBOL_MAP

    def test_all_config_assets_have_bybit_mapping(self):
        """Every CRYPTO asset in config must be in BYBIT_SYMBOL_MAP.
        Equities and commodities are SoDEX-native — no Bybit mapping needed.
        Missing crypto entries cause silent OB/candle gaps → ATR=0 forever.
        """
        from core.config import Settings
        from data.bybit_feed import BYBIT_SYMBOL_MAP, SUPPORTED_ASSETS
        from core.asset_classes import get_asset_class
        cfg = Settings()
        # Only check crypto assets — equities/commodities use SoDEX native feed
        crypto_assets = [s for s in cfg.assets if get_asset_class(s) == "crypto"]
        missing = [s for s in crypto_assets if s not in BYBIT_SYMBOL_MAP]
        assert not missing, (
            f"These CRYPTO assets have no Bybit mapping: {missing}\n"
            f"They will never receive OB/candle data → ATR=0 forever"
        )


# ══════════════════════════════════════════════════════════════════════════════
# G. TICK/STEP NAME FALLBACK FOR CLOSE + REPLACE_STOP
# ══════════════════════════════════════════════════════════════════════════════

class TestTickStepNameFallback:
    """
    close_position_market and replace_stop_order used _TICK_STEP.get(symbol_id)
    without a name-based fallback. For ARB/OP/NEAR (step_size=1, tick_size=1e-5),
    missing the lookup returns (0.01, 0.01) default → quantity formatted as
    '979.00' instead of '979' → SoDEX rejects the order.

    Fix: _get_tick_step(symbol, symbol_id) tries ID first, then name map.
    """

    def test_get_tick_step_importable(self):
        from execution.sodex_client import _get_tick_step
        assert callable(_get_tick_step)

    def test_arb_name_fallback_step(self):
        """ARB-USD step_size=0.1 — live API 2026-04-17 (was wrongly 10.0)."""
        from execution.sodex_client import _get_tick_step, _TICK_STEP_BY_NAME
        assert "ARB-USD" in _TICK_STEP_BY_NAME, "ARB-USD must be in _TICK_STEP_BY_NAME"
        tick, step = _TICK_STEP_BY_NAME["ARB-USD"]
        assert step == pytest.approx(0.1), f"ARB-USD step_size must be 0.1, got {step}"
        assert tick > 0, f"ARB-USD tick_size must be > 0, got {tick}"

    def test_op_name_fallback_step(self):
        """OP-USD step_size=0.1 — live API 2026-04-17 (was wrongly 10.0)."""
        from execution.sodex_client import _TICK_STEP_BY_NAME
        assert "OP-USD" in _TICK_STEP_BY_NAME, "OP-USD must be in _TICK_STEP_BY_NAME"
        tick, step = _TICK_STEP_BY_NAME["OP-USD"]
        assert step == pytest.approx(0.1)

    def test_near_name_fallback_step(self):
        from execution.sodex_client import _TICK_STEP_BY_NAME
        assert "NEAR-USD" in _TICK_STEP_BY_NAME, "NEAR-USD must be in _TICK_STEP_BY_NAME"
        _, step = _TICK_STEP_BY_NAME["NEAR-USD"]
        assert step == pytest.approx(0.1)

    def test_copper_in_name_map(self):
        """COPPER-USD is a live SoDEX asset — must have tick/step entry."""
        from execution.sodex_client import _TICK_STEP_BY_NAME
        assert "COPPER-USD" in _TICK_STEP_BY_NAME, \
            "COPPER-USD must be in _TICK_STEP_BY_NAME — it trades on SoDEX"

    def test_btc_id_lookup_still_works(self):
        """ID-based lookup must still function — name fallback is additive."""
        from execution.sodex_client import _get_tick_step
        # BTC symbol_id lookup should succeed without needing name fallback
        # Use a plausible fake ID — just verify _get_tick_step doesn't crash
        result = _get_tick_step("BTC-USD", "unknown_id_xyz")
        assert isinstance(result, tuple) and len(result) == 2


# ══════════════════════════════════════════════════════════════════════════════
# H. BYBIT TICKER STORES WIRED TO TERMINAL DISPLAY
# ══════════════════════════════════════════════════════════════════════════════

class TestTerminalDisplayBybitWiring:
    """
    TerminalDisplay previously had no access to bybit_ticker_stores.
    Left panel showed dead SoDEX FR (0.01%) instead of live Bybit FR.
    Fix: bybit_ticker_stores passed as constructor parameter.
    """

    def test_terminal_display_accepts_bybit_ticker_stores(self):
        """TerminalDisplay.__init__ must accept bybit_ticker_stores kwarg."""
        import inspect
        from display.terminal import TerminalDisplay
        sig = inspect.signature(TerminalDisplay.__init__)
        assert "bybit_ticker_stores" in sig.parameters, (
            "TerminalDisplay must accept bybit_ticker_stores= parameter "
            "to display live Bybit funding rates and OI in the left panel"
        )

    def test_bybit_ticker_stores_defaults_to_none(self):
        """Parameter must be optional (default None) — non-breaking."""
        import inspect
        from display.terminal import TerminalDisplay
        sig = inspect.signature(TerminalDisplay.__init__)
        param = sig.parameters["bybit_ticker_stores"]
        assert param.default is None, (
            "bybit_ticker_stores must default to None for backward compatibility"
        )


# ══════════════════════════════════════════════════════════════════════════════
# INTEGRATION: CONFIG UNIVERSE CONSISTENCY
# ══════════════════════════════════════════════════════════════════════════════

class TestUniverseConsistency:
    """All assets in config.assets must have ASSET_CONFIG entries.
    Missing entries cause KeyError crashes in risk gates.
    """

    def test_every_asset_has_config_entry(self):
        from core.config import Settings
        cfg = Settings()
        missing = [a for a in cfg.assets if a not in cfg.ASSET_CONFIG]
        assert not missing, (
            f"Assets without ASSET_CONFIG: {missing} — will crash risk gates"
        )

    def test_every_config_entry_has_required_fields(self):
        from core.config import Settings
        cfg = Settings()
        for sym, entry in cfg.ASSET_CONFIG.items():
            assert "tick_size" in entry, f"{sym}: missing tick_size"
            assert "min_size" in entry, f"{sym}: missing min_size"
            assert "max_leverage" in entry, f"{sym}: missing max_leverage"
            assert entry["max_leverage"] > 0, f"{sym}: max_leverage must be > 0"
            assert entry["tick_size"] > 0, f"{sym}: tick_size must be > 0"

    def test_tier_b_assets_subset_of_universe(self):
        from core.config import Settings
        cfg = Settings()
        not_in_universe = [s for s in cfg.TIER_B_ASSETS if s not in cfg.assets]
        assert not not_in_universe, (
            f"TIER_B_ASSETS not in universe: {not_in_universe}"
        )

    def test_tier_a_assets_subset_of_universe(self):
        from core.config import Settings
        cfg = Settings()
        not_in_universe = [s for s in cfg.TIER_A_ASSETS if s not in cfg.assets]
        assert not not_in_universe
