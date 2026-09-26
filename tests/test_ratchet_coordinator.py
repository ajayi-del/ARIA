"""RatchetCoordinator: rung arming/emission monotonicity, payloads, skip
injection, bounded state — doctrine pins. Zero I/O, no main.py import."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.ratchet_coordinator import (  # noqa: E402
    RatchetCoordinator, Intent, RATCHET_TRIGGERS, _parse_rungs,
    MAX_TRACKED_POSITIONS,
)


def _cfg(**kw):
    # Mechanics pins select ratchet_min_price_move_pct=0.0 (legacy ROE-only)
    # explicitly — they simulate ROE without a consistent mark, and the
    # leverage-blind price-move guard (cross-review P1, default 0.5) would
    # otherwise suppress every rung. The guard's own pins live in
    # TestMinPriceMoveGuard below and drive entry/mark honestly.
    base = dict(ratchet_coordinator_enabled=True,
                ratchet_rungs="10,20,30,50,80,100",
                ratchet_cross_side_budget_frac=0.30,
                ratchet_emit_tolerance_roe=2.0,
                ratchet_min_price_move_pct=0.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _tick(rc, cfg, **kw):
    base = dict(position_id="P1", symbol="BTC-USD", side="long",
                roe_pct=0.0, peak_roe_pct=0.0, entry_price=100.0,
                mark_price=100.0, initial_margin_usd=100.0,
                unrealized_pnl_usd=0.0, now_ts=1000.0)
    base.update(kw)
    return rc.on_roe_tick(cfg, **base)


# ── 1. Master gate ───────────────────────────────────────────────────────────

def test_disabled_returns_empty():
    rc = RatchetCoordinator()
    assert _tick(rc, _cfg(ratchet_coordinator_enabled=False),
                 roe_pct=120.0, peak_roe_pct=120.0,
                 unrealized_pnl_usd=120.0) == []


def test_disabled_no_state_mutation():
    rc = RatchetCoordinator()
    _tick(rc, _cfg(ratchet_coordinator_enabled=False),
          roe_pct=120.0, peak_roe_pct=120.0, unrealized_pnl_usd=120.0)
    assert rc.tracked_positions() == 0
    assert rc.rungs_fired("P1") == set()


# ── 2. Arming / emission band ────────────────────────────────────────────────

def test_below_first_rung_silent():
    rc = RatchetCoordinator()
    assert _tick(rc, _cfg(), roe_pct=9.9, peak_roe_pct=9.9,
                 unrealized_pnl_usd=9.9) == []


def test_cross_side_fires_at_rung():
    rc = RatchetCoordinator()
    out = _tick(rc, _cfg(), roe_pct=10.0, peak_roe_pct=10.0,
                unrealized_pnl_usd=10.0)
    assert len(out) == 1
    assert out[0].kind == "cross_side"
    assert isinstance(out[0], Intent)


def test_fires_once_no_refire():
    rc = RatchetCoordinator()
    _tick(rc, _cfg(), roe_pct=10.0, peak_roe_pct=10.0,
          unrealized_pnl_usd=10.0)
    assert _tick(rc, _cfg(), roe_pct=10.5, peak_roe_pct=10.5,
                 unrealized_pnl_usd=10.5) == []


def test_monotonic_dip_and_recross_no_refire():
    rc = RatchetCoordinator()
    _tick(rc, _cfg(), roe_pct=10.0, peak_roe_pct=10.0,
          unrealized_pnl_usd=10.0)
    _tick(rc, _cfg(), roe_pct=2.0, peak_roe_pct=10.0, unrealized_pnl_usd=2.0)
    assert _tick(rc, _cfg(), roe_pct=12.0, peak_roe_pct=12.0,
                 unrealized_pnl_usd=12.0) == []


def test_armed_by_peak_emit_within_tolerance():
    # peak reached the rung, current dipped to rung - 1.5 (< 2.0 tol) → fires
    rc = RatchetCoordinator()
    out = _tick(rc, _cfg(), roe_pct=8.5, peak_roe_pct=10.5,
                unrealized_pnl_usd=8.5)
    assert [i.kind for i in out] == ["cross_side"]


def test_collapsed_beyond_tolerance_no_fire():
    # peak armed but current collapsed below rung - tol → silent, NOT consumed
    rc = RatchetCoordinator()
    out = _tick(rc, _cfg(), roe_pct=7.5, peak_roe_pct=10.5,
                unrealized_pnl_usd=7.5)
    assert out == []
    assert rc.rungs_fired("P1") == set()


def test_armed_during_collapse_fires_on_recovery():
    rc = RatchetCoordinator()
    _tick(rc, _cfg(), roe_pct=7.5, peak_roe_pct=10.5, unrealized_pnl_usd=7.5)
    out = _tick(rc, _cfg(), roe_pct=8.3, peak_roe_pct=10.5,
                unrealized_pnl_usd=8.3)
    assert [i.kind for i in out] == ["cross_side"]


def test_tolerance_knob_respected():
    # tol 0.5: current 9.0 against rung 10 must NOT fire
    rc = RatchetCoordinator()
    out = _tick(rc, _cfg(ratchet_emit_tolerance_roe=0.5),
                roe_pct=9.0, peak_roe_pct=10.2, unrealized_pnl_usd=9.0)
    assert out == []


def test_peak_never_reached_rung_no_fire_even_at_tolerance():
    # peak 9.5 < 10 → never armed, even though current sits at rung - tol
    rc = RatchetCoordinator()
    out = _tick(rc, _cfg(), roe_pct=9.5, peak_roe_pct=9.5,
                unrealized_pnl_usd=9.5)
    assert out == []


# ── 3. Rung ladder ───────────────────────────────────────────────────────────

def test_all_rungs_fire_in_one_hot_tick_ascending():
    rc = RatchetCoordinator()
    out = _tick(rc, _cfg(), roe_pct=105.0, peak_roe_pct=105.0,
                unrealized_pnl_usd=105.0)
    assert [i.kind for i in out] == [
        "cross_side", "propagation", "stop_to_entry", "pyramid",
        "weak_stop", "respawn"]
    assert [i.payload["rung"] for i in out] == [10, 20, 30, 50, 80, 100]


def test_respawn_at_exactly_100():
    rc = RatchetCoordinator()
    out = _tick(rc, _cfg(), roe_pct=100.0, peak_roe_pct=100.0,
                unrealized_pnl_usd=100.0)
    assert any(i.kind == "respawn" for i in out)


def test_respawn_not_below_100():
    rc = RatchetCoordinator()
    out = _tick(rc, _cfg(), roe_pct=99.99, peak_roe_pct=99.99,
                unrealized_pnl_usd=99.99)
    assert not any(i.kind == "respawn" for i in out)


def test_custom_rung_string_subset():
    rc = RatchetCoordinator()
    out = _tick(rc, _cfg(ratchet_rungs="30,80"),
                roe_pct=110.0, peak_roe_pct=110.0,
                unrealized_pnl_usd=110.0)
    assert [i.kind for i in out] == ["stop_to_entry", "weak_stop"]


def test_rung_parse_drops_malformed_and_unknown():
    assert _parse_rungs("10,abc,13,20") == [10.0, 20.0]
    assert _parse_rungs("") == []
    assert _parse_rungs(None) == [10.0, 20.0, 30.0, 50.0, 80.0, 100.0]
    assert _parse_rungs([10, "50", 999]) == [10.0, 50.0]


def test_rung_triggers_table_locked():
    assert RATCHET_TRIGGERS == {10: "cross_side", 20: "propagation",
                                30: "stop_to_entry", 50: "pyramid",
                                80: "weak_stop", 100: "respawn"}


# ── 4. Payloads ──────────────────────────────────────────────────────────────

def test_cross_side_payload_budget_and_counter_side():
    rc = RatchetCoordinator()
    out = _tick(rc, _cfg(), roe_pct=10.0, peak_roe_pct=10.0,
                unrealized_pnl_usd=10.0)
    p = out[0].payload
    assert p["budget_usd"] == 0.30 * 10.0
    assert p["counter_side"] == "short"


def test_cross_side_counter_side_for_short_winner():
    rc = RatchetCoordinator()
    out = _tick(rc, _cfg(), side="short", roe_pct=10.0, peak_roe_pct=10.0,
                unrealized_pnl_usd=10.0)
    assert out[0].payload["counter_side"] == "long"


def test_cross_side_budget_frac_knob():
    rc = RatchetCoordinator()
    out = _tick(rc, _cfg(ratchet_cross_side_budget_frac=0.5),
                roe_pct=10.0, peak_roe_pct=10.0, unrealized_pnl_usd=10.0)
    assert out[0].payload["budget_usd"] == 5.0


def test_cross_side_budget_injected_cap():
    rc = RatchetCoordinator(cross_side_budget_cap_usd=2.0)
    out = _tick(rc, _cfg(), roe_pct=10.0, peak_roe_pct=10.0,
                unrealized_pnl_usd=10.0)
    assert out[0].payload["budget_usd"] == 2.0


def test_cross_side_nonpositive_upnl_no_intent_and_consumed():
    rc = RatchetCoordinator()
    out = _tick(rc, _cfg(), roe_pct=10.0, peak_roe_pct=10.0,
                unrealized_pnl_usd=0.0)
    assert out == []
    assert 10.0 in rc.rungs_fired("P1")   # monotonic even when degenerate
    assert _tick(rc, _cfg(), roe_pct=11.0, peak_roe_pct=11.0,
                 unrealized_pnl_usd=11.0) == []


def test_propagation_payload_family_injected():
    rc = RatchetCoordinator(family_fn=lambda sym: "crypto_beta")
    out = _tick(rc, _cfg(), roe_pct=20.0, peak_roe_pct=20.0,
                unrealized_pnl_usd=20.0)
    prop = [i for i in out if i.kind == "propagation"][0]
    assert prop.payload["family"] == "crypto_beta"


def test_propagation_family_none_without_fn():
    rc = RatchetCoordinator()
    out = _tick(rc, _cfg(), roe_pct=20.0, peak_roe_pct=20.0,
                unrealized_pnl_usd=20.0)
    prop = [i for i in out if i.kind == "propagation"][0]
    assert prop.payload["family"] is None


def test_propagation_family_fn_exception_safe():
    def boom(sym):
        raise RuntimeError("x")
    rc = RatchetCoordinator(family_fn=boom)
    out = _tick(rc, _cfg(), roe_pct=20.0, peak_roe_pct=20.0,
                unrealized_pnl_usd=20.0)
    prop = [i for i in out if i.kind == "propagation"][0]
    assert prop.payload["family"] is None


def test_stop_to_entry_payload_exact_entry_tighten_only():
    rc = RatchetCoordinator()
    out = _tick(rc, _cfg(), roe_pct=30.0, peak_roe_pct=30.0,
                entry_price=123.456, unrealized_pnl_usd=30.0)
    ste = [i for i in out if i.kind == "stop_to_entry"][0]
    assert ste.payload["new_stop"] == 123.456
    assert ste.payload["tighten_only"] is True


def test_pyramid_payload_half_original_margin():
    rc = RatchetCoordinator()
    out = _tick(rc, _cfg(), roe_pct=50.0, peak_roe_pct=50.0,
                initial_margin_usd=80.0, unrealized_pnl_usd=40.0)
    pyr = [i for i in out if i.kind == "pyramid"][0]
    assert pyr.payload["add_margin_frac"] == 0.5
    assert pyr.payload["add_margin_usd"] == 40.0


def test_weak_stop_uses_peak_not_current():
    rc = RatchetCoordinator()
    out = _tick(rc, _cfg(), roe_pct=82.0, peak_roe_pct=90.0,
                unrealized_pnl_usd=82.0)
    ws = [i for i in out if i.kind == "weak_stop"][0]
    assert ws.payload["stop_roe_floor"] == 0.50 * 90.0


def test_respawn_payload_cycle():
    rc = RatchetCoordinator()
    out = _tick(rc, _cfg(), roe_pct=100.0, peak_roe_pct=100.0,
                unrealized_pnl_usd=100.0)
    rs = [i for i in out if i.kind == "respawn"][0]
    assert rs.payload["cycle"] == "campaign_respawn"


# ── 5. Skip injection ────────────────────────────────────────────────────────

def test_skip_fn_reason_suppresses_without_consuming():
    rc = RatchetCoordinator(skip_fn=lambda sym: "treasury_managed")
    out = _tick(rc, _cfg(), roe_pct=50.0, peak_roe_pct=50.0,
                unrealized_pnl_usd=50.0)
    assert out == []
    assert rc.rungs_fired("P1") == set()


def test_skip_fn_release_fires_pending_rungs():
    flag = {"skip": True}
    rc = RatchetCoordinator(
        skip_fn=lambda sym: "treasury_managed" if flag["skip"] else None)
    _tick(rc, _cfg(), roe_pct=30.0, peak_roe_pct=30.0,
          unrealized_pnl_usd=30.0)
    flag["skip"] = False
    out = _tick(rc, _cfg(), roe_pct=30.0, peak_roe_pct=30.0,
                unrealized_pnl_usd=30.0)
    kinds = [i.kind for i in out]
    assert "cross_side" in kinds and "propagation" in kinds \
        and "stop_to_entry" in kinds


def test_skip_fn_exception_fail_closed():
    def boom(sym):
        raise RuntimeError("x")
    rc = RatchetCoordinator(skip_fn=boom)
    assert _tick(rc, _cfg(), roe_pct=50.0, peak_roe_pct=50.0,
                 unrealized_pnl_usd=50.0) == []


def test_skip_does_not_stop_peak_tracking():
    flag = {"skip": True}
    rc = RatchetCoordinator(
        skip_fn=lambda sym: "treasury_managed" if flag["skip"] else None)
    _tick(rc, _cfg(), roe_pct=12.0, peak_roe_pct=12.0,
          unrealized_pnl_usd=12.0)
    flag["skip"] = False
    # current now below rung but peak armed → tolerance band fires
    out = _tick(rc, _cfg(), roe_pct=10.5, peak_roe_pct=10.5,
                unrealized_pnl_usd=10.5)
    assert [i.kind for i in out] == ["cross_side"]


# ── 6. Lifecycle / state hygiene ─────────────────────────────────────────────

def test_on_position_closed_resets_ladder():
    rc = RatchetCoordinator()
    _tick(rc, _cfg(), roe_pct=30.0, peak_roe_pct=30.0,
          unrealized_pnl_usd=30.0)
    rc.on_position_closed("P1")
    out = _tick(rc, _cfg(), roe_pct=10.0, peak_roe_pct=10.0,
                unrealized_pnl_usd=10.0)
    assert [i.kind for i in out] == ["cross_side"]


def test_on_position_closed_unknown_id_safe():
    rc = RatchetCoordinator()
    rc.on_position_closed("NOPE")


def test_positions_tracked_independently():
    rc = RatchetCoordinator()
    _tick(rc, _cfg(), position_id="P1", roe_pct=30.0, peak_roe_pct=30.0,
          unrealized_pnl_usd=30.0)
    out = _tick(rc, _cfg(), position_id="P2", roe_pct=10.0,
                peak_roe_pct=10.0, unrealized_pnl_usd=10.0)
    assert [i.kind for i in out] == ["cross_side"]
    assert rc.tracked_positions() == 2


def test_same_symbol_two_positions_separate_state():
    rc = RatchetCoordinator()
    _tick(rc, _cfg(), position_id="P1", roe_pct=30.0, peak_roe_pct=30.0,
          unrealized_pnl_usd=30.0)
    out = _tick(rc, _cfg(), position_id="P2", roe_pct=30.0,
                peak_roe_pct=30.0, unrealized_pnl_usd=30.0)
    assert len(out) == 3   # P2 has its own ladder — not deduped by symbol


def test_bounded_state_eviction_fifo():
    rc = RatchetCoordinator(max_positions=5)
    for i in range(8):
        _tick(rc, _cfg(), position_id=f"P{i}", roe_pct=1.0,
              peak_roe_pct=1.0, unrealized_pnl_usd=1.0)
    assert rc.tracked_positions() == 5
    # oldest evicted: P0/P1/P2 gone, P3..P7 remain — P7 is still tracked
    # (re-firing a rung against it would prove state survived).
    out = _tick(rc, _cfg(), position_id="P7", roe_pct=10.0,
                peak_roe_pct=10.0, unrealized_pnl_usd=10.0)
    assert [i.kind for i in out] == ["cross_side"]
    assert rc.tracked_positions() == 5


def test_degenerate_inputs_return_empty():
    rc = RatchetCoordinator()
    assert _tick(rc, _cfg(), roe_pct="x", peak_roe_pct=10.0) == []
    assert _tick(rc, _cfg(), side="flat", roe_pct=50.0,
                 peak_roe_pct=50.0) == []
    assert _tick(rc, _cfg(), position_id=None, roe_pct=50.0,
                 peak_roe_pct=50.0) == []
    assert _tick(rc, _cfg(), entry_price=0.0, roe_pct=50.0,
                 peak_roe_pct=50.0) == []
    assert _tick(rc, _cfg(), mark_price=-1.0, roe_pct=50.0,
                 peak_roe_pct=50.0) == []


def test_max_tracked_positions_constant():
    assert MAX_TRACKED_POSITIONS == 10000


def test_intent_carries_identity_fields():
    rc = RatchetCoordinator()
    out = _tick(rc, _cfg(), position_id="PX", symbol="ETH-USD", side="short",
                roe_pct=10.0, peak_roe_pct=10.0, unrealized_pnl_usd=10.0)
    i = out[0]
    assert (i.symbol, i.side, i.position_id) == ("ETH-USD", "short", "PX")
    assert i.kind == "cross_side"


# ── Cross-review doctrine pins (2026-09-26) ─────────────────────────────────

class TestMinPriceMoveGuard:
    """Cross-review P1: at 15-38x a 10% ROE rung is a 0.26-0.67% price move
    — the fee-death scalp class. The guard (default 0.5%) suppresses rungs
    until the price move grows into them, WITHOUT consuming fire-once."""

    def test_default_knob_is_half_percent(self):
        # The brain's own default is the safe direction; the legacy helper
        # opts out explicitly.
        cfg = SimpleNamespace(ratchet_coordinator_enabled=True,
                              ratchet_rungs="10,20,30,50,80,100",
                              ratchet_cross_side_budget_frac=0.30,
                              ratchet_emit_tolerance_roe=2.0)
        rc = RatchetCoordinator()
        # ROE 12% but price move only 0.3% (38x geometry) → suppressed.
        out = _tick(rc, cfg, roe_pct=12.0, peak_roe_pct=12.0,
                    entry_price=100.0, mark_price=100.3,
                    unrealized_pnl_usd=12.0)
        assert out == []

    def test_suppression_does_not_consume_fire_once(self):
        rc = RatchetCoordinator()
        cfg = _cfg(ratchet_min_price_move_pct=0.5)
        _tick(rc, cfg, roe_pct=12.0, peak_roe_pct=12.0,
              entry_price=100.0, mark_price=100.3, unrealized_pnl_usd=12.0)
        assert rc.rungs_fired("P1") == set()
        # The move grows into the floor → the rung fires THEN, once.
        out = _tick(rc, cfg, roe_pct=12.0, peak_roe_pct=12.0,
                    entry_price=100.0, mark_price=100.8,
                    unrealized_pnl_usd=12.0)
        assert [i.kind for i in out] == ["cross_side"]
        assert rc.rungs_fired("P1") == {10.0}

    def test_zero_disables_guard_legacy(self):
        rc = RatchetCoordinator()
        out = _tick(rc, _cfg(ratchet_min_price_move_pct=0.0),
                    roe_pct=10.0, peak_roe_pct=10.0,
                    unrealized_pnl_usd=10.0)
        assert [i.kind for i in out] == ["cross_side"]


class TestSeedFired:
    """Cross-review P0: rungs_fired is memory-only — boot must seed adopted
    positions or the first tick re-fires pyramid adds (real money)."""

    def test_seeded_rungs_never_refire(self):
        rc = RatchetCoordinator()
        rc.seed_fired("P1", peak_roe=55.0, rungs=[10, 20, 30, 50])
        out = _tick(rc, _cfg(), roe_pct=55.0, peak_roe_pct=55.0,
                    unrealized_pnl_usd=55.0)
        # Only un-seeded higher rungs may fire; 80 needs peak >= 80.
        assert out == []
        assert rc.rungs_fired("P1") == {10.0, 20.0, 30.0, 50.0}

    def test_seed_preserves_peak_for_later_rungs(self):
        rc = RatchetCoordinator()
        rc.seed_fired("P1", peak_roe=55.0, rungs=[10, 20, 30, 50])
        out = _tick(rc, _cfg(), roe_pct=82.0, peak_roe_pct=82.0,
                    unrealized_pnl_usd=82.0)
        assert [i.kind for i in out] == ["weak_stop"]

    def test_seed_degenerate_inputs_safe(self):
        rc = RatchetCoordinator()
        rc.seed_fired("P1", peak_roe="x", rungs=None)
        assert rc.rungs_fired("P1") == set()


class TestSideAwareSkipFn:
    """Cross-review P1: the native loop's Hugo skip is side-dependent —
    a two-arg skip_fn is detected and called with side."""

    def test_two_arg_skip_receives_side(self):
        seen = []

        def skip(symbol, side):
            seen.append((symbol, side))
            return "hugo" if side == "long" else None

        rc = RatchetCoordinator(skip_fn=skip)
        out = _tick(rc, _cfg(), side="long", roe_pct=50.0,
                    peak_roe_pct=50.0, unrealized_pnl_usd=50.0)
        assert out == []
        assert seen == [("BTC-USD", "long")]

    def test_one_arg_skip_legacy_contract(self):
        seen = []

        def skip(symbol):
            seen.append(symbol)
            return None

        rc = RatchetCoordinator(skip_fn=skip)
        out = _tick(rc, _cfg(), roe_pct=10.0, peak_roe_pct=10.0,
                    unrealized_pnl_usd=10.0)
        assert [i.kind for i in out] == ["cross_side"]
        assert seen == ["BTC-USD"]

    def test_skip_exception_fail_closed(self):
        def skip(symbol, side):
            raise RuntimeError("boom")

        rc = RatchetCoordinator(skip_fn=skip)
        assert _tick(rc, _cfg(), roe_pct=50.0, peak_roe_pct=50.0,
                     unrealized_pnl_usd=50.0) == []
