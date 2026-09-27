"""tests/test_anticipator_resilience.py — pins for the 2026-09-27
anticipator resilience bundle (the campaign-killer repair: 0 fills in the
fleet's first 3h — 558 placed / 548 evicted / 705 rejected).

  C  level-coverage idempotency (keystone: near-static cluster levels must
     not re-mint the same resting order every 60s tick)
  D  300s eviction grace + cancel-then-pop (the conveyor killer — the
     median order was dying 40-80s after placement)
  A  margin preflight + min-notional floor + dust sweeper (main.py source
     pins — the 1,449 insufficient-margin rejects and the venue-shrink
     dust rows)
  E  leverage set-and-hold + verify-before-place + idle-only restore
     (main.py source pins — the 14-call storm and the indeterminate-
     leverage XAUT fill)
  F  signed wire-payload logging (main.py source pin)

Behavioral pins cover the zero-I/O brain (plan_fleet); the wiring is
source-pinned per the tests/test_main_fast_cycle_splice.py convention.
Kill-switch off-state (coverage False + min_rest_s 0.0 + all main.py
knobs False) must reproduce the pre-repair system bit-for-bit.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.anticipator import (  # noqa: E402
    _order_side,
    plan_fleet,
)

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SRC = os.path.join(_REPO, "main.py")

NOW = 1_800_000_000.0  # fixed epoch seconds


def _main_src() -> str:
    with open(_MAIN_SRC) as fh:
        return fh.read()


def cfg(**over):
    # Production defaults (the resilience layer ON). The legacy layer is
    # pinned in tests/test_anticipator.py with the layer explicitly OFF.
    base = dict(
        anticipator_enabled=True,
        anticipator_min_distance_pct=0.8,
        anticipator_max_distance_pct=6.0,
        anticipator_stale_s=2700.0,
        anticipator_max_age_s=14400.0,
        anticipator_max_per_symbol=4,
        anticipator_max_global=12,
        anticipator_cage_min=3.0,
        anticipator_stop_atr_frac=1.0,
        anticipator_margin_usd=100.0,
        anticipator_entry_nudge_pct=0.05,
        anticipator_residual_complete_frac=0.6,
        anticipator_residual_chase_pct=0.003,
        anticipator_level_coverage_enabled=True,
        anticipator_level_tolerance_pct=0.1,
        anticipator_min_rest_s=300.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _row(tag, symbol="S", side="short", price=103.0,
         created=NOW - 400.0, strength=0.5):
    return {"tag": tag, "symbol": symbol, "side": side,
            "limit_price": price, "created_ts": created,
            "strength": strength}


def _plan(c, levels, open_fleet, symbol="S", mark=100.0):
    return plan_fleet(c, symbol=symbol, mark_price=mark, atr=1.0,
                      levels=levels, structure=None, now_ts=NOW,
                      open_fleet=open_fleet)


def _reasons(skips):
    return [s.reason for s in skips]


def _evicted_tags(skips):
    return {s.detail for s in skips if s.reason == "evicted"}


# ── C: coverage-satisfied candidates are skipped, incumbents protected ──

class TestCoverageSatisfied:
    def test_matching_level_is_covered(self):
        inc = _row("ant-S-short-1", price=103.0)
        specs, skips = _plan(cfg(), [(103.05, "short_stops", 0.5)], [inc])
        assert specs == []
        assert "covered" in _reasons(skips)
        assert _evicted_tags(skips) == set()

    def test_covered_incumbent_protected_from_capacity(self):
        # Cap full (1/1). Two further candidates at OTHER levels would
        # normally evict the incumbent to free room — the covered tag
        # protects it, so they die at fleet_capacity instead.
        inc = _row("ant-S-short-1", price=103.0)
        specs, skips = _plan(
            cfg(anticipator_max_per_symbol=1),
            [(103.05, "short_stops", 0.5),
             (105.0, "short_stops", 0.9),
             (104.0, "short_stops", 0.8)],
            [inc])
        assert specs == []
        assert "covered" in _reasons(skips)
        assert _reasons(skips).count("fleet_capacity") == 2
        assert _evicted_tags(skips) == set()

    def test_different_level_same_side_is_a_second_trade(self):
        # Governor ruling 2026-09-26: same-symbol probes at DIFFERENT
        # levels = two trades (the ladder is legitimate). 104.5 is beyond
        # the 0.1% bucket from 103.0 -> planned, never covered.
        inc = _row("ant-S-short-1", price=103.0)
        specs, skips = _plan(cfg(), [(104.5, "short_stops", 0.5)], [inc])
        assert len(specs) == 1
        assert "covered" not in _reasons(skips)

    def test_opposite_side_not_covered(self):
        inc = _row("ant-S-short-1", price=103.0, side="short")
        specs, skips = _plan(cfg(), [(97.0, "long_stops", 0.5)], [inc])
        assert len(specs) == 1
        assert specs[0].side == "long"

    def test_coverage_off_is_legacy(self):
        inc = _row("ant-S-short-1", price=103.0)
        specs, skips = _plan(
            cfg(anticipator_level_coverage_enabled=False),
            [(103.05, "short_stops", 0.5)], [inc])
        assert len(specs) == 1
        assert "covered" not in _reasons(skips)


# ── C: refresh — an in-place strength upgrade at the same level ─────────

class TestRefresh:
    def test_refresh_evicts_weak_incumbent(self):
        # 0.7 >= 0.5 + 0.15 and the incumbent is past its grace: evict-
        # replace at the same level (replacement, not churn).
        inc = _row("ant-S-short-1", price=103.0, strength=0.5,
                   created=NOW - 400.0)
        specs, skips = _plan(cfg(), [(103.05, "short_stops", 0.7)], [inc])
        assert _evicted_tags(skips) == {"ant-S-short-1"}
        assert len(specs) == 1
        assert specs[0].side == "short"

    def test_refresh_grace_protects_young_incumbent(self):
        # Same strength gap but the incumbent rests inside its 300s grace
        # — the upgrade waits; no eviction, no placement.
        inc = _row("ant-S-short-1", price=103.0, strength=0.5,
                   created=NOW - 100.0)
        specs, skips = _plan(cfg(), [(103.05, "short_stops", 0.7)], [inc])
        assert specs == []
        assert "refresh_grace" in _reasons(skips)
        assert _evicted_tags(skips) == set()

    def test_strength_delta_boundary(self):
        # 0.64 < 0.5 + 0.15 -> ordinary coverage, no eviction.
        inc = _row("ant-S-short-1", price=103.0, strength=0.5,
                   created=NOW - 400.0)
        specs, skips = _plan(cfg(), [(103.05, "short_stops", 0.64)], [inc])
        assert specs == []
        assert "covered" in _reasons(skips)
        assert _evicted_tags(skips) == set()


# ── C: duplicate covering orders are deduped immediately ────────────────

class TestDuplicates:
    def test_duplicate_eviction_bypasses_grace(self):
        # Two YOUNG incumbents covering the same bucket: the weaker
        # duplicate is evicted at once (dedup, not churn — grace does not
        # apply); the stronger representative is kept and covers.
        t1 = _row("ant-S-short-1", price=103.0, strength=0.5,
                  created=NOW - 100.0)
        t2 = _row("ant-S-short-2", price=103.04, strength=0.6,
                  created=NOW - 50.0)
        specs, skips = _plan(cfg(), [(103.02, "short_stops", 0.5)], [t1, t2])
        assert specs == []
        assert _evicted_tags(skips) == {"ant-S-short-1"}
        assert "covered" in _reasons(skips)

    def test_side_parsed_from_tag_when_key_missing(self):
        # Boot-rebuilt rows carry no side until the splice's open_fleet
        # read; the brain must parse ant-{symbol}-{side}-{ms} (symbols
        # themselves contain "-", so the side is the second-to-last
        # segment).
        inc = {"tag": "ant-S-short-1799999900000", "symbol": "S",
               "limit_price": 103.0, "created_ts": NOW - 400.0,
               "strength": 0.5}
        specs, skips = _plan(cfg(), [(103.05, "short_stops", 0.5)], [inc])
        assert specs == []
        assert "covered" in _reasons(skips)

    def test_order_side_helper(self):
        assert _order_side({"side": "short"}) == "short"
        assert _order_side({"tag": "ant-SOL-USD-long-123"}) == "long"
        assert _order_side({"tag": "ant-BTC-USD-short-456"}) == "short"
        assert _order_side({"tag": "weird"}) is None
        assert _order_side({}) is None


# ── D: capacity eviction grace — young orders never feed the conveyor ───

class TestCapacityGrace:
    def test_young_incumbent_never_capacity_evicted(self):
        # Cap full (1/1), candidate at a DIFFERENT level. Legacy would
        # evict the 0.9-strength incumbent to place the 0.5 candidate
        # (the conveyor); the 300s grace refuses.
        inc = _row("ant-S-short-1", price=105.0, strength=0.9,
                   created=NOW - 100.0)
        specs, skips = _plan(
            cfg(anticipator_max_per_symbol=1),
            [(103.0, "short_stops", 0.5)], [inc])
        assert specs == []
        assert "fleet_capacity" in _reasons(skips)
        assert _evicted_tags(skips) == set()

    def test_old_incumbent_eligible(self):
        inc = _row("ant-S-short-1", price=105.0, strength=0.9,
                   created=NOW - 400.0)
        specs, skips = _plan(
            cfg(anticipator_max_per_symbol=1),
            [(103.0, "short_stops", 0.5)], [inc])
        assert _evicted_tags(skips) == {"ant-S-short-1"}
        assert len(specs) == 1

    def test_legacy_conveyor_when_grace_zero(self):
        # min_rest_s 0.0 = the pre-repair conveyor bit-for-bit: the young
        # 0.9 incumbent IS evicted for the 0.5 candidate.
        inc = _row("ant-S-short-1", price=105.0, strength=0.9,
                   created=NOW - 100.0)
        specs, skips = _plan(
            cfg(anticipator_max_per_symbol=1, anticipator_min_rest_s=0.0),
            [(103.0, "short_stops", 0.5)], [inc])
        assert _evicted_tags(skips) == {"ant-S-short-1"}
        assert len(specs) == 1


# ── Kill switch: coverage off + grace 0 = pre-repair bit-for-bit ────────

class TestLegacyBitForBit:
    def test_cap_eviction_weakest_first(self):
        fleet = [
            _row("ant-S-short-1", price=101.0, strength=0.1,
                 created=NOW - 300.0),
            _row("ant-S-short-2", price=102.0, strength=0.2,
                 created=NOW - 200.0),
            _row("ant-S-short-3", price=103.0, strength=0.3,
                 created=NOW - 100.0),
            _row("ant-S-short-4", price=104.0, strength=0.4,
                 created=NOW - 10.0),
        ]
        specs, skips = _plan(
            cfg(anticipator_level_coverage_enabled=False,
                anticipator_min_rest_s=0.0),
            [(105.5, "short_stops", 0.9), (105.0, "short_stops", 0.8)],
            fleet)
        # need_sym = 2 - (4-4) = 2 -> two weakest evicted, both planned.
        assert _evicted_tags(skips) == {"ant-S-short-1", "ant-S-short-2"}
        assert len(specs) == 2


# ── Config defaults (the code of record) ────────────────────────────────

class TestConfigDefaults:
    def test_resilience_knobs_default_on(self):
        from core.config import Settings
        s = Settings()
        assert s.anticipator_level_coverage_enabled is True
        assert s.anticipator_level_tolerance_pct == 0.1
        assert s.anticipator_min_rest_s == 300.0
        assert s.anticipator_margin_preflight_enabled is True
        assert s.anticipator_margin_buffer_usd == 5.0
        assert s.anticipator_margin_budget_usd == 250.0  # Governor 2026-09-27
        assert s.anticipator_min_order_notional_usd == 50.0
        assert s.anticipator_dust_sweep_enabled is True
        assert s.anticipator_leverage_hold_enabled is True


# ── Wiring source pins (main.py) ─────────────────────────────────────────

class TestMainSourcePins:
    def test_open_fleet_carries_side(self):
        # The coverage bucket is per (symbol, side, level) — the splice
        # must feed the brain the row's side.
        src = _main_src()
        assert src.count('"side": _r["side"]') == 1

    def test_margin_preflight_standdowns(self):
        src = _main_src()
        assert "anticipator_place_standdown" in src
        assert 'reason="margin_unknown"' in src
        assert 'reason="margin_preflight"' in src
        assert 'reason="min_notional"' in src
        assert "_fc_reserved_this_tick" in src
        assert "_fc_margin_exhausted" in src

    def test_margin_budget_standdowns(self):
        # Governor 2026-09-27 ($250 total campaign margin): both placement
        # paths enforce the deterministic fleet+book budget.
        src = _main_src()
        assert src.count('reason="margin_budget"') == 2   # ant- + xpr-
        assert src.count("anticipator_margin_budget_usd") == 2
        assert 'if _r.get("state") == "resting"' in src
        assert '"debited", 0.0' in src

    def test_leverage_hold_wiring(self):
        src = _main_src()
        assert "_fc_set_leverage" in src
        assert "_fc_restore_leverage" in src
        assert "_fc_lev_state" in src
        assert "anticipator_leverage_set_failed" in src
        assert 'reason="leverage_set_failed"' in src
        assert "anticipator_leverage_restored" in src
        # End-of-tick sweep owns restores in hold mode.
        assert "if _lev_hold and _fc_lev_state:" in src

    def test_wire_payload_log(self):
        assert "anticipator_wire" in _main_src()

    def test_dust_sweeper(self):
        src = _main_src()
        assert "anticipator_dust_cancelled" in src
        assert 'exit_reason="anticipator_dust"' in src
        assert "anticipator_dust_sweep_failed" in src

    def test_cancel_then_pop(self):
        # A failed eviction cancel keeps the registry row (retried next
        # tick) — the orphan vector is closed.
        src = _main_src()
        assert "anticipator_evict_cancel_failed" in src

    def test_coverage_telemetry(self):
        assert "anticipator_coverage" in _main_src()
