"""Operator crypto-long firewall pins (Governor 2026-09-26).

"my crypto longs should not be managed by aria" — a crypto LONG carrying no
ARIA journal intent (approved+open entry in the last 7 day-files) and no
in-flight ARIA entry is the OPERATOR's manual trade: telemetry plane only,
NEVER PositionManager. _operator_long_firewall_verdict is the pure
classification predicate; the journal scan + pending-entry check live at the
call sites (boot sync, 5s untracked loop). Fail-safe direction is ADOPT:
managing his trade by accident beats a naked ARIA orphan.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


def _v(side="long", category="crypto", intent=False, pending=False, enabled=True):
    return main._operator_long_firewall_verdict(side, category, intent, pending, enabled)


class TestOperatorLongFirewallVerdict:
    def test_operator_crypto_long_classified(self):
        # The directive class: his manual BTC/ETH/etc long, no ARIA intent.
        assert _v() is True

    def test_kill_switch_off_adopts(self):
        assert _v(enabled=False) is False

    def test_short_never_operator(self):
        # The directive names crypto LONGS only — his shorts stay managed.
        assert _v(side="short") is False

    def test_equity_long_never_operator(self):
        assert _v(category="equity") is False

    def test_commodity_long_never_operator(self):
        assert _v(category="commodity") is False

    def test_journal_intent_adopts(self):
        # ARIA's own prior-process trade (journal carries the approved+open
        # entry) — adopt with stops at every boot.
        assert _v(intent=True) is False

    def test_pending_entry_adopts(self):
        # ARIA is filling this symbol right now — never release mid-fill.
        assert _v(pending=True) is False

    def test_fail_safe_combination(self):
        # Doubt on any leg = adopt. intent=True is the journal-error path.
        assert _v(intent=True, pending=True) is False
        assert _v(side="short", intent=True) is False

    def test_every_leg_required(self):
        # Flipping exactly one leg away from the operator class must adopt.
        base = dict(side="long", category="crypto", intent=False,
                    pending=False, enabled=True)
        assert _v(**base) is True
        for key, bad in (("side", "short"), ("category", "equity"),
                         ("intent", True), ("pending", True),
                         ("enabled", False)):
            alt = dict(base)
            alt[key] = bad
            assert _v(**alt) is False, key


class TestFirewallKnob:
    def test_knob_default_on(self):
        from core.config import Settings
        assert Settings().operator_long_firewall_enabled is True

    def test_floor_knob_untouched(self):
        # W1 and W2 are independent workstreams riding one deploy.
        from core.config import Settings
        assert Settings().min_trade_notional_usd == 100.0
        assert Settings().final_floor_enforcement_enabled is True

    def test_kant_release_covers_notional_floor(self):
        # Bug-hunt P1-4: a standard-path floor rejection must release the
        # Kant daily-cap reservation or the dust class burns the 120/day
        # budget by midday.
        assert "notional_floor" in main._KANT_PRE_VENUE_REJECTIONS


def _p(ex, aria, residual=0.0, recent=False):
    return main._operator_overlap_partition(ex, aria, residual, recent)


class TestOperatorOverlapPartition:
    def test_unexplained_excess_registers(self):
        # His manual add: no recent ARIA order → partition, residual = excess.
        assert _p(1.5, 1.0) == ("partition", 0.5)

    def test_recent_aria_order_preserves_grow_heal(self):
        # Bug-hunt P1-5: ARIA's own late fill (entry/pyramid/swing order just
        # fired) must read as legacy grow, never as operator.
        assert _p(1.5, 1.0, recent=True) == ("none", 0.0)

    def test_parity_no_overlap_is_legacy(self):
        assert _p(1.0, 1.0) == ("none", 0.0)

    def test_shrink_without_overlap_is_legacy(self):
        # No registered overlap: honest native fills book via shrink_book.
        assert _p(0.7, 1.0) == ("none", 0.0)

    def test_registered_excess_updates_residual(self):
        # Residual already registered; excess grew 0.5 → 0.8: replace with the
        # full observed operator leg (ex - aria), never accumulate.
        assert _p(1.8, 1.0, residual=0.5) == ("partition", 0.8)

    def test_p0_ambiguous_shrink_defers_never_books(self):
        # Bug-hunt P0-1: aria 1.0 + his 0.2 registered; native TP takes the
        # exchange qty to 0.7 (< tracked 1.0). Legacy would shrink_book 0.3
        # (true fill 0.5) and absorb his 0.2 into pos.size. Must DEFER.
        assert _p(0.7, 1.0, residual=0.2) == ("defer", 0.2)

    def test_his_full_trim_also_defers(self):
        # He trims his whole leg: ex = true aria, but tracked aria is stale
        # if a fill went unbooked — indistinguishable, so still defer.
        assert _p(0.8, 1.0, residual=0.2) == ("defer", 0.2)

    def test_parity_resolves(self):
        # ex == tracked aria: his leg is gone, book untouched → pop.
        action, r = _p(1.0, 1.0, residual=0.2)
        assert action == "resolve"
        assert r == 0.0

    def test_degenerate_inputs_fail_to_legacy(self):
        assert _p("bad", 1.0) == ("none", 0.0)
        assert _p(1.0, None) == ("none", 0.0)

    def test_within_tolerance_is_parity(self):
        # Float-noise equality resolves, never defers.
        assert _p(1.0 + 1e-9, 1.0, residual=0.2)[0] == "resolve"
