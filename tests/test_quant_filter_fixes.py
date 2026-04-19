"""
Tests for two surgical quant filter fixes:

Fix 1: Circuit breaker preserves quiet-market cooldowns instead of blindly clearing.
Fix 2: Cascade aftermath overrides quiet market filter.
"""

import time
import pytest
from unittest.mock import MagicMock, patch


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_vc_monitor(events_60s: int):
    """Mock vc_monitor returning the given events_60s count."""
    m = MagicMock()
    m.get_status.return_value = {"events_60s": events_60s, "cascade_zscore": 0.0,
                                 "cascade_direction": "none", "cascade_phase": "none"}
    return m


def _make_cascade_tracker(block_zscore: float = 4.07):
    """Mock cascade_tracker with a known _block_zscore."""
    m = MagicMock()
    m._block_zscore = block_zscore
    return m


# ── Fix 1: Circuit breaker preserves quiet cooldowns ──────────────────────────

class TestCircuitBreakerPreservesQuiet:
    """
    Simulate the logic inside the 3-hour cooldown purge block.
    Extracted into a pure function for unit testing.
    """

    def _run_purge(self, rejection_cooldown: dict, vc_monitor, last_active_ts: float):
        """
        Replicate the quiet-aware purge logic from execution_cleanup_loop.
        Returns (cleared, preserved).
        """
        import structlog
        log = structlog.get_logger("test")

        now_purge = time.time()
        stale = [s for s, exp in rejection_cooldown.items() if exp < now_purge]

        purge_vc    = vc_monitor.get_status() if vc_monitor is not None else {}
        purge_ev60  = int(purge_vc.get("events_60s", 999))
        purge_quiet = now_purge - last_active_ts
        still_quiet = (
            purge_ev60 != 999 and
            purge_ev60 < 40 and
            purge_quiet > 1800.0
        )

        cleared   = []
        preserved = []
        for s in stale:
            if still_quiet:
                rejection_cooldown[s] = now_purge + 1800.0
                preserved.append(s)
            else:
                del rejection_cooldown[s]
                cleared.append(s)

        return cleared, preserved

    def test_quiet_market_preserves_cooldown(self):
        """Symbol with events_60s=4 and 120min quiet should NOT be cleared."""
        # Symbol's cooldown expired 1 second ago (stale)
        rejection_cooldown = {"BASED-USD": time.time() - 1}
        vc = _make_vc_monitor(events_60s=4)
        # Last active timestamp was 120 minutes ago
        last_active_ts = time.time() - (120 * 60)

        cleared, preserved = self._run_purge(rejection_cooldown, vc, last_active_ts)

        assert "BASED-USD" not in cleared, "Quiet symbol should NOT be cleared"
        assert "BASED-USD" in preserved, "Quiet symbol should be preserved"
        # Cooldown should be re-armed (future expiry)
        assert rejection_cooldown["BASED-USD"] > time.time(), (
            "Re-armed cooldown should expire in the future"
        )

    def test_active_market_clears_cooldown(self):
        """Symbol with events_60s=47 (active market) should be cleared normally."""
        rejection_cooldown = {"BTC-USD": time.time() - 1}
        vc = _make_vc_monitor(events_60s=47)
        last_active_ts = time.time() - 60  # active 1 min ago

        cleared, preserved = self._run_purge(rejection_cooldown, vc, last_active_ts)

        assert "BTC-USD" in cleared, "Active market symbol should be cleared"
        assert "BTC-USD" not in preserved
        assert "BTC-USD" not in rejection_cooldown, "Cleared symbol removed from dict"

    def test_mixed_symbols_quiet_market(self):
        """Multiple stale symbols, all preserved when market is quiet."""
        now = time.time()
        rejection_cooldown = {
            "AVAX-USD":  now - 1,
            "ARB-USD":   now - 5,
            "BNB-USD":   now - 10,
            "BASED-USD": now - 2,
        }
        vc = _make_vc_monitor(events_60s=4)
        last_active_ts = now - (120 * 60)  # 2h quiet

        cleared, preserved = self._run_purge(rejection_cooldown, vc, last_active_ts)

        assert len(cleared) == 0, "No symbols should be cleared in quiet market"
        assert set(preserved) == {"AVAX-USD", "ARB-USD", "BNB-USD", "BASED-USD"}

    def test_vc_unavailable_clears_normally(self):
        """When vc_monitor is None, fall back to clearing all stale cooldowns."""
        rejection_cooldown = {"SOL-USD": time.time() - 1}
        last_active_ts = time.time() - (200 * 60)

        cleared, preserved = self._run_purge(rejection_cooldown, None, last_active_ts)

        # _purge_ev60 = 999 when vc_monitor is None → _still_quiet = False
        assert "SOL-USD" in cleared
        assert len(preserved) == 0

    def test_not_quiet_enough_clears(self):
        """Events < 40 but quiet < 30 min should not preserve (not yet triggering quiet filter)."""
        rejection_cooldown = {"ETH-USD": time.time() - 1}
        vc = _make_vc_monitor(events_60s=10)
        last_active_ts = time.time() - (20 * 60)  # only 20 min quiet

        cleared, preserved = self._run_purge(rejection_cooldown, vc, last_active_ts)

        assert "ETH-USD" in cleared, "20min quiet is below 30min threshold — should clear"


# ── Fix 2: Cascade aftermath overrides quiet filter ───────────────────────────

class TestAftermathOverridesQuietFilter:
    """
    Test the quiet filter bypass logic in isolation.
    Replicates the if/elif structure from Filter 5.
    """

    def _run_quiet_filter(
        self,
        aftermath_active: bool,
        cascade_zscore: float,
        events_60s: int,
        quiet_s: float,
    ) -> str:
        """
        Replicate Filter 5 logic. Returns:
          "bypassed_aftermath"  — aftermath bypass fired
          "blocked_quiet"       — quiet filter blocked
          "passed"              — filter passed through
        """
        cascade_tracker = _make_cascade_tracker(block_zscore=cascade_zscore)

        if aftermath_active and cascade_tracker._block_zscore >= 2.0:
            return "bypassed_aftermath"
        elif events_60s != 999 and events_60s < 40 and quiet_s > 1800.0:
            return "blocked_quiet"
        return "passed"

    def test_aftermath_bypasses_quiet_filter(self):
        """
        Cascade aftermath with z=4.07 and events_60s=1 should NOT be blocked.
        """
        result = self._run_quiet_filter(
            aftermath_active=True,
            cascade_zscore=4.07,
            events_60s=1,
            quiet_s=183 * 60,  # 183 minutes quiet
        )
        assert result == "bypassed_aftermath", (
            "Cascade aftermath with z=4.07 must bypass quiet filter"
        )

    def test_non_aftermath_blocked_by_quiet(self):
        """
        Non-aftermath signal with events_60s=1 and 183min quiet must be blocked.
        """
        result = self._run_quiet_filter(
            aftermath_active=False,
            cascade_zscore=0.0,
            events_60s=1,
            quiet_s=183 * 60,
        )
        assert result == "blocked_quiet", (
            "Non-aftermath signal must still be blocked by quiet filter"
        )

    def test_weak_cascade_not_bypassed(self):
        """
        Aftermath with z=1.5 (below 2.0 threshold) must NOT bypass quiet filter.
        """
        result = self._run_quiet_filter(
            aftermath_active=True,
            cascade_zscore=1.5,
            events_60s=1,
            quiet_s=183 * 60,
        )
        assert result == "blocked_quiet", (
            "Weak cascade (z<2.0) aftermath must not bypass quiet filter"
        )

    def test_aftermath_z_exactly_2_bypasses(self):
        """Boundary: z=2.0 exactly should bypass."""
        result = self._run_quiet_filter(
            aftermath_active=True,
            cascade_zscore=2.0,
            events_60s=5,
            quiet_s=200 * 60,
        )
        assert result == "bypassed_aftermath"

    def test_aftermath_active_market_passes_normally(self):
        """Aftermath in an active market (events >= 40) passes quietly — no bypass needed."""
        result = self._run_quiet_filter(
            aftermath_active=True,
            cascade_zscore=4.0,
            events_60s=55,
            quiet_s=10 * 60,  # only 10 min quiet — filter wouldn't block anyway
        )
        # aftermath bypass fires first, but quiet filter wouldn't have blocked either
        assert result in ("bypassed_aftermath", "passed")

    def test_no_aftermath_active_market_passes(self):
        """Normal (non-aftermath) signal in active market passes normally."""
        result = self._run_quiet_filter(
            aftermath_active=False,
            cascade_zscore=0.0,
            events_60s=60,
            quiet_s=5 * 60,
        )
        assert result == "passed"
