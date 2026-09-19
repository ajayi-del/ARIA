"""Pyramid stop-protection pins (2026-09-19 naked-stop incident).

Live incident (verified from production logs 2026-09-19): SoDEX positions
ETH (short) and ARB (long) were left NAKED on the exchange. Chain: pyramid
SCALE_OUT halved each position via _close_with_retry + _record_partial_close
but the resting native stop stayed sized for the OLD full size; the immune
purge then cancelled BOTH live stops as orphan_reduce_only on the size
mismatch while the trail/roe-ratchet loops were paused (pyramid owns stops
mid-build), so nothing re-placed them.

FIX 1 — _pyramid_scale_out_resize_plan / _pyramid_scale_out_resize_stop:
after a successful scale-out partial close, the native stop is re-placed
for the REMAINING size at the SAME price (tighten size only). Failure is
fail-open — the software stop guardian still covers the position.

FIX 2 — _immune_purge_reason: a reduce-only order opposing a live tracked
position's side is PROTECTIVE and is never purged for size mismatch alone
(the exchange caps reduce-only fills at position size, so an oversize RO
stop is still fully protective). True orphans (no exchange position) purge
exactly as before — fail-closed.
"""
import asyncio

from main import (
    _immune_purge_reason,
    _pyramid_scale_out_resize_plan,
    _pyramid_scale_out_resize_stop,
)


# ── FIX 1: scale-out stop resize plan ────────────────────────────────────────

def test_resize_plan_carries_remaining_size_and_unchanged_price():
    plan = _pyramid_scale_out_resize_plan(
        stop_order_id="23316059119", stop_price=99.9972, symbol_id=12,
        venue_name="sodex", remaining_size=0.496)
    assert plan is not None
    assert plan["new_stop_price"] == 99.9972      # price NEVER moved
    assert plan["old_stop_order_id"] == "23316059119"
    assert plan["size"] == 0.496                  # remaining, not original
    assert plan["symbol_id"] == 12


def test_resize_plan_no_native_stop_is_noop():
    # Nothing resting exchange-side → nothing to resize, skip silently.
    assert _pyramid_scale_out_resize_plan(
        stop_order_id=None, stop_price=99.9, symbol_id=12,
        venue_name="sodex", remaining_size=0.5) is None


def test_resize_plan_degenerate_price_or_size_is_noop():
    assert _pyramid_scale_out_resize_plan(
        stop_order_id="1", stop_price=0.0, symbol_id=12,
        venue_name="sodex", remaining_size=0.5) is None
    assert _pyramid_scale_out_resize_plan(
        stop_order_id="1", stop_price=99.9, symbol_id=12,
        venue_name="sodex", remaining_size=0.0) is None


def test_resize_plan_sodex_symbol_id_guard():
    # SoDEX replaces key on the numeric symbol id (trailing-loop _native_ok
    # idiom) — no id, no replace.
    assert _pyramid_scale_out_resize_plan(
        stop_order_id="1", stop_price=99.9, symbol_id=0,
        venue_name="sodex", remaining_size=0.5) is None


def test_resize_plan_aster_needs_no_symbol_id():
    # Aster keys on the symbol name, not an id — the guard is venue-aware.
    plan = _pyramid_scale_out_resize_plan(
        stop_order_id="8724", stop_price=4594.0, symbol_id=0,
        venue_name="aster", remaining_size=3.2)
    assert plan is not None and plan["size"] == 3.2


def test_resize_plan_kill_switch_off_is_prefix_noop():
    # False = pre-fix behavior bit-for-bit: no resize is ever planned.
    assert _pyramid_scale_out_resize_plan(
        stop_order_id="1", stop_price=99.9, symbol_id=12,
        venue_name="sodex", remaining_size=0.5, enabled=False) is None


# ── FIX 1: fail-open resize executor ─────────────────────────────────────────

class _Repl:
    def __init__(self, order_id=None, error=None):
        self.order_id = order_id
        self.error = error

    @property
    def success(self):
        return self.error is None


_PLAN = {"new_stop_price": 99.9, "old_stop_order_id": "old-1",
         "size": 0.5, "symbol_id": 12}


def test_resize_stop_success_returns_new_order_id():
    async def _ok(**kwargs):
        assert kwargs["new_stop_price"] == 99.9
        assert kwargs["old_stop_order_id"] == "old-1"
        assert kwargs["size"] == 0.5
        return _Repl(order_id="new-2")
    new_id, err = asyncio.run(_pyramid_scale_out_resize_stop(
        _ok, symbol="SOL-USD", plan=_PLAN, side="long",
        mark_price=105.0, entry_price=101.0, account_id=7))
    assert (new_id, err) == ("new-2", None)


def test_resize_stop_exchange_rejection_is_swallowed():
    async def _rej(**kwargs):
        return _Repl(error="stopPrice is invalid")
    new_id, err = asyncio.run(_pyramid_scale_out_resize_stop(
        _rej, symbol="ETH-USD", plan=_PLAN, side="short",
        mark_price=3100.0, entry_price=3050.0, account_id=7))
    assert new_id is None and "stopPrice" in err  # fail-open, never raises


def test_resize_stop_exception_is_swallowed():
    async def _boom(**kwargs):
        raise RuntimeError("rate limit")
    new_id, err = asyncio.run(_pyramid_scale_out_resize_stop(
        _boom, symbol="ARB-USD", plan=_PLAN, side="long",
        mark_price=1.1, entry_price=1.0, account_id=7))
    assert new_id is None and "rate limit" in err  # bookkeeping proceeds


# ── FIX 2: orphan purge matcher hardening ────────────────────────────────────

def test_oversize_protective_ro_stop_long_is_kept():
    # The incident itself: SELL reduce-only stop against a tracked LONG,
    # quantity stale after the scale-out — KEPT. Size never enters the
    # decision (exchange caps RO fills at position size).
    assert _immune_purge_reason(
        reduce_only=True, order_side=2, symbol_has_exchange_position=True,
        tracked_side="long", order_id_tracked=True, age_ms=9_000_000) is None


def test_oversize_protective_ro_stop_short_is_kept():
    # BUY reduce-only stop against a tracked SHORT — the ARB/ETH mirror.
    assert _immune_purge_reason(
        reduce_only=True, order_side=1, symbol_has_exchange_position=True,
        tracked_side="short", order_id_tracked=True, age_ms=9_000_000) is None


def test_non_protective_ro_debris_against_long_is_purged():
    # BUY reduce-only against a tracked LONG reduces nothing — debris.
    assert _immune_purge_reason(
        reduce_only=True, order_side=1, symbol_has_exchange_position=True,
        tracked_side="long", order_id_tracked=False, age_ms=9_000_000
    ) == "orphan_reduce_only"


def test_non_protective_ro_debris_against_short_is_purged():
    assert _immune_purge_reason(
        reduce_only=True, order_side=2, symbol_has_exchange_position=True,
        tracked_side="short", order_id_tracked=False, age_ms=9_000_000
    ) == "orphan_reduce_only"


def test_unreadable_side_ro_is_never_purged_against_live_position():
    # Side 0 can never be proven non-protective — canceling a real stop is
    # the catastrophic direction, so keep.
    assert _immune_purge_reason(
        reduce_only=True, order_side=0, symbol_has_exchange_position=True,
        tracked_side="long", order_id_tracked=False, age_ms=9_000_000) is None


def test_true_orphan_purges_exactly_as_before():
    # No exchange position on the symbol — the bracket outlived its parent.
    # Fail-closed legacy, kill switch irrelevant.
    for exempt in (True, False):
        assert _immune_purge_reason(
            reduce_only=True, order_side=2, symbol_has_exchange_position=False,
            tracked_side=None, order_id_tracked=False, age_ms=9_000_000,
            protective_exempt=exempt) == "orphan_reduce_only"


def test_exchange_position_but_untracked_keeps_ro_order():
    # Manual / sync-pending position — legacy kept these; never cancel a
    # possible stop.
    assert _immune_purge_reason(
        reduce_only=True, order_side=2, symbol_has_exchange_position=True,
        tracked_side=None, order_id_tracked=False, age_ms=9_000_000) is None


def test_kill_switch_off_is_prefix_bit_for_bit():
    # Exemption OFF: any RO order with an exchange position is kept,
    # protective or not — the pre-fix classification.
    for side in (0, 1, 2):
        assert _immune_purge_reason(
            reduce_only=True, order_side=side,
            symbol_has_exchange_position=True, tracked_side="long",
            order_id_tracked=False, age_ms=9_000_000,
            protective_exempt=False) is None


# ── FIX 2: legacy non-RO classes preserved ───────────────────────────────────

def test_stale_entry_legacy():
    assert _immune_purge_reason(
        reduce_only=False, order_side=1, symbol_has_exchange_position=False,
        tracked_side=None, order_id_tracked=False, age_ms=181_000
    ) == "stale_entry"


def test_stale_entry_never_purges_tracked_order_id():
    assert _immune_purge_reason(
        reduce_only=False, order_side=1, symbol_has_exchange_position=False,
        tracked_side=None, order_id_tracked=True, age_ms=999_999_999) is None


def test_young_entry_order_is_kept():
    # In-flight bracket entries resolve in <120s — the age floor protects
    # them (sub-180s dust-age exemptions unchanged).
    assert _immune_purge_reason(
        reduce_only=False, order_side=1, symbol_has_exchange_position=False,
        tracked_side=None, order_id_tracked=False, age_ms=60_000) is None


def test_dci_opposed_debris_legacy():
    # Non-RO resting order opposed to the tracked position, age > 120s.
    assert _immune_purge_reason(
        reduce_only=False, order_side=2, symbol_has_exchange_position=True,
        tracked_side="long", order_id_tracked=False, age_ms=121_000
    ) == "dci_opposed_debris"


def test_dci_outranks_stale_entry():
    # Both classes true → legacy reason-display precedence is DCI first.
    assert _immune_purge_reason(
        reduce_only=False, order_side=2, symbol_has_exchange_position=True,
        tracked_side="long", order_id_tracked=False, age_ms=999_999_999
    ) == "dci_opposed_debris"


def test_dci_same_side_non_ro_is_kept():
    assert _immune_purge_reason(
        reduce_only=False, order_side=1, symbol_has_exchange_position=True,
        tracked_side="long", order_id_tracked=False, age_ms=999_999_999
    ) == "stale_entry"  # not opposed → falls through to the stale class


def test_dci_young_order_is_kept():
    assert _immune_purge_reason(
        reduce_only=False, order_side=2, symbol_has_exchange_position=True,
        tracked_side="long", order_id_tracked=True, age_ms=60_000) is None
