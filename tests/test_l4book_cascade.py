"""
Tests for L4Book + Cascade Integration Sprint.

Covers:
  1. OrderbookStore.update_l4_diff() — diff merging
  2. sodex_feed.py l4Book handler — subscription + parsing
  3. cascade_tracker.py — orderbook_stores wiring, _check_orderbook_rebuilding,
     _check_l4_spread_normalised, _evaluate_aftermath 6-signal dict
  4. main.py — CascadeTracker instantiated with orderbook_stores

All tests must pass before the L4Book integration is considered wired correctly.
"""

import pytest
import time
from unittest.mock import MagicMock, patch, PropertyMock


# ── OrderbookStore L4 Diff ───────────────────────────────────────────────────

class TestOrderbookStoreL4Diff:

    def _make_store(self, symbol="BTC-USD"):
        from data.orderbook_store import OrderbookStore
        return OrderbookStore(symbol)

    def test_l4_diff_adds_new_levels(self):
        store = self._make_store()
        store.update([(100.0, 1.0)], [(101.0, 2.0)], 1_000_000)
        store.update_l4_diff([(99.5, 3.0)], [(101.5, 4.0)], 1_000_001)
        assert (99.5, 3.0) in store.bids
        assert (101.5, 4.0) in store.asks

    def test_l4_diff_updates_existing_levels(self):
        store = self._make_store()
        store.update([(100.0, 1.0)], [(101.0, 2.0)], 1_000_000)
        store.update_l4_diff([(100.0, 5.0)], [(101.0, 6.0)], 1_000_001)
        assert store.bids[0] == (100.0, 5.0)
        assert store.asks[0] == (101.0, 6.0)

    def test_l4_diff_removes_zero_qty_levels(self):
        store = self._make_store()
        store.update([(100.0, 1.0), (99.0, 2.0)], [(101.0, 3.0)], 1_000_000)
        store.update_l4_diff([(100.0, 0.0)], [], 1_000_001)
        assert (100.0, 1.0) not in store.bids
        assert (99.0, 2.0) in store.bids

    def test_l4_diff_maintains_bid_descending_sort(self):
        store = self._make_store()
        store.update([(100.0, 1.0)], [(101.0, 1.0)], 1_000_000)
        store.update_l4_diff([(98.0, 1.0), (102.0, 1.0)], [], 1_000_001)
        bids = [p for p, _ in store.bids]
        assert bids == sorted(bids, reverse=True)

    def test_l4_diff_maintains_ask_ascending_sort(self):
        store = self._make_store()
        store.update([(100.0, 1.0)], [(101.0, 1.0)], 1_000_000)
        store.update_l4_diff([], [(100.0, 1.0), (102.0, 1.0)], 1_000_001)
        asks = [p for p, _ in store.asks]
        assert asks == sorted(asks)

    def test_l4_diff_updates_timestamp_and_count(self):
        store = self._make_store()
        store.update([(100.0, 1.0)], [(101.0, 1.0)], 1_000_000)
        prev_count = store.update_count
        store.update_l4_diff([(99.5, 1.0)], [], 1_000_001)
        assert store.last_update_ms == 1_000_001
        assert store.update_count == prev_count + 1

    def test_l4_diff_only_bids(self):
        store = self._make_store()
        store.update([(100.0, 1.0)], [(101.0, 1.0)], 1_000_000)
        store.update_l4_diff([(99.5, 2.0)], [], 1_000_001)
        assert store.asks == [(101.0, 1.0)]
        assert (99.5, 2.0) in store.bids

    def test_l4_diff_only_asks(self):
        store = self._make_store()
        store.update([(100.0, 1.0)], [(101.0, 1.0)], 1_000_000)
        store.update_l4_diff([], [(102.0, 2.0)], 1_000_001)
        assert store.bids == [(100.0, 1.0)]
        assert (102.0, 2.0) in store.asks


# ── sodex_feed l4Book handler ────────────────────────────────────────────────

class TestSodexFeedL4Book:

    def test_l4book_subscription_in_subscribe_batch(self):
        """Verify l4Book subscribe message is sent alongside l2Book."""
        import json
        from data.sodex_feed import SoDEXFeed
        cfg = MagicMock()
        cfg.assets = ["BTC-USD"]
        feed = SoDEXFeed(cfg, {}, {}, {}, {})

        sent = []
        async def _capture(msg):
            sent.append(json.loads(msg))

        import asyncio
        ws = MagicMock()
        ws.send = _capture
        asyncio.run(feed._subscribe_batch(ws, ["BTC-USD"]))

        l4_params = [s["params"] for s in sent if s.get("op") == "subscribe" and s["params"].get("channel") == "l4Book"]
        assert len(l4_params) == 1
        assert l4_params[0].get("level") == 10
        channels = [s["params"]["channel"] for s in sent if s.get("op") == "subscribe"]
        assert "l4Book" in channels
        assert "l2Book" in channels

    def test_l4book_snapshot_calls_store_update(self):
        """Snapshot type triggers full store.update()."""
        from data.sodex_feed import SoDEXFeed
        from core.event_bus import EventType
        cfg = MagicMock()
        cfg.assets = ["BTC-USD"]
        store = MagicMock()
        feed = SoDEXFeed(cfg, {}, {"BTC-USD": store}, {}, {})

        msg = {
            "channel": "l4Book",
            "type": "snapshot",
            "data": {"s": "BTC-USD", "b": [[100.0, 1.0]], "a": [[101.0, 2.0]], "E": 1_000_000},
        }
        import asyncio
        asyncio.run(feed._handle(msg))
        store.update.assert_called_once()
        store.update_l4_diff.assert_not_called()

    def test_l4book_update_calls_store_update_l4_diff(self):
        """Update type triggers store.update_l4_diff()."""
        from data.sodex_feed import SoDEXFeed
        cfg = MagicMock()
        cfg.assets = ["BTC-USD"]
        store = MagicMock()
        feed = SoDEXFeed(cfg, {}, {"BTC-USD": store}, {}, {})

        msg = {
            "channel": "l4Book",
            "type": "update",
            "data": {"s": "BTC-USD", "b": [[100.0, 1.0]], "a": [[101.0, 2.0]], "E": 1_000_000},
        }
        import asyncio
        asyncio.run(feed._handle(msg))
        store.update_l4_diff.assert_called_once()
        store.update.assert_not_called()

    def test_l4book_publishes_event_with_imbalance(self):
        """Handler publishes ORDERBOOK_UPDATED with source=l4Book and imbalance."""
        from unittest.mock import patch
        from data.sodex_feed import SoDEXFeed
        cfg = MagicMock()
        cfg.assets = ["BTC-USD"]
        store = MagicMock()
        store.imbalance.return_value = 0.25
        feed = SoDEXFeed(cfg, {}, {"BTC-USD": store}, {}, {})

        published = []
        def _capture(event):
            published.append(event)

        with patch("data.sodex_feed.event_bus") as mock_bus:
            mock_bus.publish = _capture

            msg = {
                "channel": "l4Book",
                "type": "update",
                "data": {"s": "BTC-USD", "b": [[100.0, 1.0]], "a": [[101.0, 2.0]], "E": 1_000_000},
            }
            import asyncio
            asyncio.run(feed._handle(msg))

        assert len(published) == 1
        assert published[0].data.get("source") == "l4Book"
        assert published[0].data.get("imbalance") == 0.25

    def test_l4book_ignores_unknown_symbol(self):
        """Unknown symbols are dropped silently."""
        from data.sodex_feed import SoDEXFeed
        cfg = MagicMock()
        cfg.assets = ["BTC-USD"]
        store = MagicMock()
        feed = SoDEXFeed(cfg, {}, {"BTC-USD": store}, {}, {})

        msg = {
            "channel": "l4Book",
            "data": {"s": "UNKNOWN", "b": [[100.0, 1.0]], "a": [[101.0, 2.0]], "E": 1_000_000},
        }
        import asyncio
        asyncio.run(feed._handle(msg))
        store.update.assert_not_called()
        store.update_l4_diff.assert_not_called()

    def test_l4book_handles_malformed_data(self):
        """Malformed data logs warning without crashing."""
        from data.sodex_feed import SoDEXFeed
        cfg = MagicMock()
        cfg.assets = ["BTC-USD"]
        store = MagicMock()
        feed = SoDEXFeed(cfg, {}, {"BTC-USD": store}, {}, {})

        msg = {"channel": "l4Book", "data": "not_a_dict"}
        import asyncio
        asyncio.run(feed._handle(msg))
        store.update.assert_not_called()


# ── CascadeTracker L4Book wiring ─────────────────────────────────────────────

class TestCascadeTrackerL4Book:

    def _make_tracker(self, orderbook_stores=None):
        from intelligence.cascade_tracker import CascadeTracker
        cfg = MagicMock()
        cfg.momentum_velocity_threshold = 3.0
        cfg.momentum_notional_threshold = 50_000.0
        cfg.cascade_min_coherence = 3.0
        return CascadeTracker(cfg, orderbook_stores=orderbook_stores)

    def _make_ob(self, imbalance_val=0.0, spread_bps=5.0, age_ms=1_000):
        """Build a mock OrderbookStore with controllable outputs."""
        ob = MagicMock()
        ob.imbalance.return_value = imbalance_val
        ob.top_of_book.return_value = (100.0, 100.0 + spread_bps / 10_000 * 100.0, spread_bps / 10_000 * 100.0)
        ob.age_ms.return_value = age_ms
        return ob

    def test_init_accepts_orderbook_stores(self):
        stores = {"BTC-USD": MagicMock()}
        tracker = self._make_tracker(orderbook_stores=stores)
        assert tracker._orderbook_stores is stores

    def test_check_orderbook_rebuilding_bearish_recovery_true(self):
        tracker = self._make_tracker({"BTC-USD": self._make_ob(imbalance_val=0.10)})
        from intelligence.cascade_tracker import CascadeSnapshot
        tracker._last_snapshot = CascadeSnapshot(10_000, "bearish", 5, time.time(), 0.0)
        assert tracker._check_orderbook_rebuilding() is True

    def test_check_orderbook_rebuilding_bearish_not_recovered_false(self):
        tracker = self._make_tracker({"BTC-USD": self._make_ob(imbalance_val=0.50)})
        from intelligence.cascade_tracker import CascadeSnapshot
        tracker._last_snapshot = CascadeSnapshot(10_000, "bearish", 5, time.time(), 0.0)
        assert tracker._check_orderbook_rebuilding() is False

    def test_check_orderbook_rebuilding_bullish_recovery_true(self):
        tracker = self._make_tracker({"BTC-USD": self._make_ob(imbalance_val=-0.10)})
        from intelligence.cascade_tracker import CascadeSnapshot
        tracker._last_snapshot = CascadeSnapshot(10_000, "bullish", 5, time.time(), 0.0)
        assert tracker._check_orderbook_rebuilding() is True

    def test_check_orderbook_rebuilding_bullish_not_recovered_false(self):
        tracker = self._make_tracker({"BTC-USD": self._make_ob(imbalance_val=-0.50)})
        from intelligence.cascade_tracker import CascadeSnapshot
        tracker._last_snapshot = CascadeSnapshot(10_000, "bullish", 5, time.time(), 0.0)
        assert tracker._check_orderbook_rebuilding() is False

    def test_check_orderbook_rebuilding_neutral_moderate_true(self):
        tracker = self._make_tracker({"BTC-USD": self._make_ob(imbalance_val=0.30)})
        from intelligence.cascade_tracker import CascadeSnapshot
        tracker._last_snapshot = CascadeSnapshot(10_000, "mixed", 5, time.time(), 0.0)
        assert tracker._check_orderbook_rebuilding() is True

    def test_check_orderbook_rebuilding_neutral_extreme_false(self):
        tracker = self._make_tracker({"BTC-USD": self._make_ob(imbalance_val=0.50)})
        from intelligence.cascade_tracker import CascadeSnapshot
        tracker._last_snapshot = CascadeSnapshot(10_000, "mixed", 5, time.time(), 0.0)
        assert tracker._check_orderbook_rebuilding() is False

    def test_check_orderbook_rebuilding_stale_data_skips(self):
        tracker = self._make_tracker({"BTC-USD": self._make_ob(age_ms=35_000)})
        from intelligence.cascade_tracker import CascadeSnapshot
        tracker._last_snapshot = CascadeSnapshot(10_000, "bearish", 5, time.time(), 0.0)
        # Stale data skipped → checked=0 → fallback to mark_price (returns True)
        assert tracker._check_orderbook_rebuilding() is True

    def test_check_orderbook_rebuilding_no_l4book_fallback(self):
        tracker = self._make_tracker({})
        from intelligence.cascade_tracker import CascadeSnapshot
        tracker._last_snapshot = CascadeSnapshot(10_000, "bearish", 5, time.time(), 0.0)
        assert tracker._check_orderbook_rebuilding() is True

    def test_check_l4_spread_normalised_two_symbols_true(self):
        stores = {
            "BTC-USD": self._make_ob(spread_bps=5.0),
            "ETH-USD": self._make_ob(spread_bps=8.0),
        }
        tracker = self._make_tracker(stores)
        assert tracker._check_l4_spread_normalised() is True

    def test_check_l4_spread_normalised_one_symbol_false(self):
        stores = {
            "BTC-USD": self._make_ob(spread_bps=5.0),
            "ETH-USD": self._make_ob(spread_bps=50.0),  # too wide
        }
        tracker = self._make_tracker(stores)
        assert tracker._check_l4_spread_normalised() is False

    def test_check_l4_spread_normalised_no_data_true(self):
        tracker = self._make_tracker({})
        assert tracker._check_l4_spread_normalised() is True

    def test_check_l4_spread_normalised_stale_skips(self):
        stores = {
            "BTC-USD": self._make_ob(spread_bps=5.0, age_ms=35_000),
            "ETH-USD": self._make_ob(spread_bps=50.0, age_ms=35_000),
        }
        tracker = self._make_tracker(stores)
        assert tracker._check_l4_spread_normalised() is True

    def test_check_l4_spread_normalised_all_wide_false(self):
        stores = {
            "BTC-USD": self._make_ob(spread_bps=50.0),
            "ETH-USD": self._make_ob(spread_bps=50.0),
            "SOL-USD": self._make_ob(spread_bps=50.0),
        }
        tracker = self._make_tracker(stores)
        assert tracker._check_l4_spread_normalised() is False

    def test_evaluate_aftermath_returns_six_signals(self):
        tracker = self._make_tracker()
        from intelligence.cascade_tracker import CascadeSnapshot
        tracker._last_snapshot = CascadeSnapshot(10_000, "bearish", 5, time.time(), 0.0)
        result = tracker._evaluate_aftermath()
        assert set(result.keys()) == {
            "price_overshoot",
            "vpin_recovering",
            "funding_normalising",
            "orderbook_rebuilding",
            "cross_venue_normalising",
            "l4_spread_normalised",
        }

    def test_evaluate_aftermath_orderbook_rebuilding_not_always_true(self):
        """Critical: orderbook_rebuilding must NOT be a free pass."""
        tracker = self._make_tracker({"BTC-USD": self._make_ob(imbalance_val=0.90)})
        from intelligence.cascade_tracker import CascadeSnapshot
        tracker._last_snapshot = CascadeSnapshot(10_000, "bearish", 5, time.time(), 0.0)
        result = tracker._evaluate_aftermath()
        assert result["orderbook_rebuilding"] is False


# ── Main.py wiring ───────────────────────────────────────────────────────────

class TestMainL4BookWiring:

    def test_cascade_tracker_instantiated_with_orderbook_stores(self):
        """
        Smoke test: verify main.py imports CascadeTracker and passes
        orderbook_stores.  We inspect the source rather than running main().
        """
        import ast
        with open("main.py") as f:
            tree = ast.parse(f.read())

        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "CascadeTracker":
                    kw_names = [kw.arg for kw in node.keywords]
                    assert "orderbook_stores" in kw_names, (
                        "CascadeTracker missing orderbook_stores kwarg"
                    )
                    found = True
        assert found, "CascadeTracker instantiation not found in main.py"


# ── Live Verification Checklist (manual / server-side) ───────────────────────

LIVE_VERIFICATION = """
Run these commands on aria-prod after restart:

1. l4Book subscription confirmed (within 30s):
   grep -i 'sodex.*subscribed' logs/aria.log | grep l4Book
   # Expected: no errors; subscription sent for each symbol

2. l4Book data flowing (within 2 min):
   grep -i 'l4book_parse_error' logs/aria.log
   # Expected: ZERO errors (channel unsupported = no data, not an error)
   grep -i 'source.*l4Book' logs/aria.log | head -5
   # Expected: ORDERBOOK_UPDATED events with source=l4Book

3. orderbook_rebuilding is REAL (not always True):
   grep 'orderbook_rebuilding' logs/aria.log | tail -20
   # Expected: mix of true AND false (was always True before fix)

4. l4_spread_normalised appears in aftermath:
   grep 'l4_spread_normalised' logs/aria.log | tail -10
   # Expected: true/false values present

5. Aftermath fires with 6 signals (within 1h of cascade):
   grep 'cascade_aftermath_primed' logs/aria.log
   # Expected: confirmed_signals >= 2 (was 1-2 of 4; now 2 of 6)
"""
