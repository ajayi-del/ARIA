"""tests/test_anticipator.py — pins for intelligence/anticipator.py.

Zero network, zero main.py import. SimpleNamespace cfg throughout.
"""
from types import SimpleNamespace

import pytest

from intelligence.anticipator import (
    OrderSpec,
    ResidualVerdict,
    SkipReason,
    fill_attribution,
    plan_fleet,
    prune_verdicts,
    residual_completion_verdict,
)

NOW = 1_800_000_000.0  # fixed epoch seconds


def cfg(**over):
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
        # 2026-09-27 resilience re-encode: this file pins the LEGACY planning
        # layer (cap accounting, eviction ordering, geometry). The new
        # coverage/grace layer defaults ON in production and would collide
        # with these pins (incumbent ages 0-300s sit inside the 300s grace),
        # so the base cfg pins it OFF bit-for-bit; the new layer is pinned in
        # tests/test_anticipator_resilience.py.
        anticipator_level_coverage_enabled=False,
        anticipator_min_rest_s=0.0,
        # 2026-09-28: this file's NOW (1_800_000_000) lands at sec_in_hour
        # 0 — inside the hourly funding-clock gate window. The gate is
        # pinned in test_anticipator_resilience.py; here it stays OFF so
        # the legacy pins hold bit-for-bit.
        anticipator_funding_clock_gate_enabled=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


# Levels: mark 100.0. Short cluster above, long cluster below, both in band.
def levels_basic():
    return [
        (102.0, "short_stops", 0.8),   # 2.0% above mark
        (98.0, "long_stops", 0.7),     # 2.0% below mark
    ]


def reasons(skips):
    return [s.reason for s in skips]


# ---------------------------------------------------------------------------
# Fleet planning geometry
# ---------------------------------------------------------------------------


class TestFleetGeometry:
    def test_short_level_above_mark_plans_short_resting_above(self):
        specs, skips = plan_fleet(cfg(), symbol="CL-USD", mark_price=100.0,
                                  atr=0.5, levels=[(102.0, "short_stops", 0.8)],
                                  structure={}, now_ts=NOW)
        assert len(specs) == 1
        s = specs[0]
        assert s.side == "short"
        assert s.limit_price > 100.0          # rests above mark
        assert s.limit_price < 102.0          # nudged toward mark (front-run)

    def test_long_level_below_mark_plans_long_resting_below(self):
        specs, _ = plan_fleet(cfg(), symbol="CL-USD", mark_price=100.0,
                              atr=0.5, levels=[(98.0, "long_stops", 0.7)],
                              structure={}, now_ts=NOW)
        assert len(specs) == 1
        s = specs[0]
        assert s.side == "long"
        assert s.limit_price < 100.0
        assert s.limit_price > 98.0           # nudged up toward mark

    def test_entry_nudge_exact_5bp_toward_mark(self):
        specs, _ = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                              levels=[(102.0, "short_stops", 0.8),
                                      (98.0, "long_stops", 0.7)],
                              structure={}, now_ts=NOW)
        by_side = {s.side: s for s in specs}
        assert by_side["short"].limit_price == pytest.approx(102.0 * (1 - 0.0005))
        assert by_side["long"].limit_price == pytest.approx(98.0 * (1 + 0.0005))

    def test_stop_beyond_cluster_short(self):
        specs, _ = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                              levels=[(102.0, "short_stops", 0.8)],
                              structure={}, now_ts=NOW)
        assert specs[0].stop_price > 102.0    # beyond the cluster

    def test_stop_beyond_cluster_long(self):
        specs, _ = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                              levels=[(98.0, "long_stops", 0.7)],
                              structure={}, now_ts=NOW)
        assert specs[0].stop_price < 98.0

    def test_stop_floor_03pct_when_atr_small(self):
        # atr tiny -> 0.3% floor binds
        specs, _ = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.05,
                              levels=[(102.0, "short_stops", 0.8)],
                              structure={}, now_ts=NOW)
        s = specs[0]
        assert s.stop_price == pytest.approx(102.0 * 1.003, rel=1e-9)

    def test_stop_atr_leg_when_atr_dominates(self):
        # atr 1.0 at price 102 -> atr frac ~0.98% > 0.3% floor
        specs, _ = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=1.0,
                              levels=[(102.0, "short_stops", 0.8)],
                              structure={}, now_ts=NOW)
        s = specs[0]
        assert s.stop_price == pytest.approx(102.0 * (1 + 1.0 / 102.0))

    def test_cage_floor_tp_at_least_3x_stop_distance(self):
        specs, _ = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                              levels=[(102.0, "short_stops", 0.8)],
                              structure={}, now_ts=NOW)
        s = specs[0]
        stop_dist = abs(s.stop_price - s.limit_price)
        tp_dist = abs(s.tp_price - s.limit_price)
        assert tp_dist >= 3.0 * stop_dist - 1e-9

    def test_tp_snaps_to_opposite_cluster_when_it_gives_more(self):
        # opposite long cluster at 96 gives ~6% TP distance >> 3x stop
        specs, _ = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.2,
                              levels=[(102.0, "short_stops", 0.8),
                                      (96.0, "long_stops", 0.6)],
                              structure={}, now_ts=NOW)
        short = next(s for s in specs if s.side == "short")
        stop_dist = abs(short.stop_price - short.limit_price)
        assert abs(short.tp_price - short.limit_price) > 3.0 * stop_dist
        # snapped near the 96 cluster (nudged back toward entry)
        assert short.tp_price == pytest.approx(96.0 * 1.0005)

    def test_tp_for_short_is_below_entry(self):
        specs, _ = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                              levels=[(102.0, "short_stops", 0.8)],
                              structure={}, now_ts=NOW)
        assert specs[0].tp_price < specs[0].limit_price

    def test_tag_format(self):
        specs, _ = plan_fleet(cfg(), symbol="CL-USD", mark_price=100.0, atr=0.5,
                              levels=[(102.0, "short_stops", 0.8)],
                              structure={}, now_ts=NOW)
        ms = int(NOW * 1000)
        assert specs[0].tag == f"ant-CL-USD-short-{ms}"

    def test_ttl_from_stale_knob(self):
        specs, _ = plan_fleet(cfg(anticipator_stale_s=999), symbol="S",
                              mark_price=100.0, atr=0.5,
                              levels=[(102.0, "short_stops", 0.8)],
                              structure={}, now_ts=NOW)
        assert specs[0].ttl_s == 999


class TestDistanceBand:
    def test_level_inside_min_distance_rejected(self):
        specs, skips = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                                  levels=[(100.5, "short_stops", 0.8)],
                                  structure={}, now_ts=NOW)
        assert specs == []
        assert "inside_min_distance" in reasons(skips)

    def test_level_exactly_at_min_distance_rejected(self):
        specs, skips = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                                  levels=[(100.8, "short_stops", 0.8)],
                                  structure={}, now_ts=NOW)
        assert specs == []
        assert "inside_min_distance" in reasons(skips)

    def test_level_just_outside_min_distance_accepted(self):
        specs, _ = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                              levels=[(100.81, "short_stops", 0.8)],
                              structure={}, now_ts=NOW)
        assert len(specs) == 1

    def test_level_beyond_max_distance_rejected(self):
        specs, skips = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                                  levels=[(107.0, "short_stops", 0.8)],
                                  structure={}, now_ts=NOW)
        assert specs == []
        assert "beyond_max_distance" in reasons(skips)

    def test_level_at_max_distance_boundary_accepted(self):
        specs, _ = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                              levels=[(106.0, "short_stops", 0.8)],
                              structure={}, now_ts=NOW)
        assert len(specs) == 1


class TestFailClosed:
    def test_empty_levels_empty_fleet_no_crash(self):
        specs, skips = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                                  levels=[], structure={}, now_ts=NOW)
        assert specs == [] and skips == []

    def test_mark_none_skips_all(self):
        specs, skips = plan_fleet(cfg(), symbol="S", mark_price=None, atr=0.5,
                                  levels=levels_basic(), structure={}, now_ts=NOW)
        assert specs == []
        assert all(r.reason == "mark_or_atr_unknown" for r in skips)

    def test_mark_zero_skips_all(self):
        specs, _ = plan_fleet(cfg(), symbol="S", mark_price=0.0, atr=0.5,
                              levels=levels_basic(), structure={}, now_ts=NOW)
        assert specs == []

    def test_atr_none_skips_all(self):
        specs, _ = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=None,
                              levels=levels_basic(), structure={}, now_ts=NOW)
        assert specs == []

    def test_atr_zero_skips_all(self):
        specs, _ = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.0,
                              levels=levels_basic(), structure={}, now_ts=NOW)
        assert specs == []

    def test_kill_switch_off_returns_empty(self):
        specs, skips = plan_fleet(cfg(anticipator_enabled=False), symbol="S",
                                  mark_price=100.0, atr=0.5,
                                  levels=levels_basic(), structure={}, now_ts=NOW)
        assert specs == [] and skips == []


class TestTwoSided:
    def test_two_sided_false_suppresses_counter_side(self):
        specs, skips = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                                  levels=levels_basic(),
                                  structure={"direction": "short",
                                             "two_sided": False},
                                  now_ts=NOW)
        assert [s.side for s in specs] == ["short"]
        assert "counter_side_suppressed" in reasons(skips)

    def test_two_sided_true_plans_both(self):
        specs, _ = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                              levels=levels_basic(),
                              structure={"direction": "short",
                                         "two_sided": True},
                              now_ts=NOW)
        assert {s.side for s in specs} == {"short", "long"}

    def test_no_structure_direction_plans_both_naturally(self):
        specs, _ = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                              levels=levels_basic(), structure={}, now_ts=NOW)
        assert {s.side for s in specs} == {"short", "long"}

    def test_structure_none_is_safe(self):
        specs, _ = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                              levels=levels_basic(), structure=None, now_ts=NOW)
        assert len(specs) == 2


class TestLevelParsing:
    def test_wrong_side_of_mark_short_below_rejected(self):
        specs, skips = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                                  levels=[(98.0, "short_stops", 0.8)],
                                  structure={}, now_ts=NOW)
        assert specs == []
        assert "wrong_side_of_mark" in reasons(skips)

    def test_wrong_side_of_mark_long_above_rejected(self):
        specs, skips = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                                  levels=[(102.0, "long_stops", 0.8)],
                                  structure={}, now_ts=NOW)
        assert specs == []
        assert "wrong_side_of_mark" in reasons(skips)

    def test_unknown_side_inferred_from_position(self):
        specs, _ = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                              levels=[(102.0, "oi_level", 0.8),
                                      (98.0, "round_number", 0.7)],
                              structure={}, now_ts=NOW)
        assert {s.side for s in specs} == {"short", "long"}

    def test_malformed_level_no_crash(self):
        specs, skips = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                                  levels=[("abc", "short_stops", 0.8), (0.0, "x", 0.5)],
                                  structure={}, now_ts=NOW)
        assert specs == []
        assert "malformed_level" in reasons(skips)
        assert "nonpositive_level" in reasons(skips)

    def test_pre_cascade_regime_boosts_strength(self):
        specs, _ = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                              levels=[(102.0, "short_stops", 0.8)],
                              structure={"regime": "pre_cascade_armed"},
                              now_ts=NOW)
        assert specs[0].strength == pytest.approx(min(1.0, 0.8 * 1.15))

    def test_family_hint_matching_direction_boosts(self):
        specs, _ = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                              levels=[(102.0, "short_stops", 0.8)],
                              structure={"family_hint": ("BTC-USD", "short", 30)},
                              now_ts=NOW)
        assert specs[0].strength == pytest.approx(0.8 * 1.10)


class TestEviction:
    def test_per_symbol_cap_evicts_weakest_first(self):
        open_fleet = [
            {"tag": f"t{i}", "symbol": "S", "limit_price": 101 + i,
             "created_ts": NOW - i * 100, "strength": st}
            for i, st in enumerate([0.9, 0.4, 0.7, 0.6])  # 4 incumbents = cap
        ]
        specs, skips = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                                  levels=[(102.0, "short_stops", 0.95)],
                                  structure={}, now_ts=NOW,
                                  open_fleet=open_fleet)
        assert len(specs) == 1
        evicted = [s.detail for s in skips if s.reason == "evicted"]
        assert evicted == ["t1"]          # weakest strength 0.4

    def test_eviction_tie_breaks_oldest(self):
        open_fleet = [
            {"tag": "new", "symbol": "S", "limit_price": 101,
             "created_ts": NOW - 100, "strength": 0.5},
            {"tag": "old", "symbol": "S", "limit_price": 102,
             "created_ts": NOW - 900, "strength": 0.5},
            {"tag": "a", "symbol": "S", "limit_price": 103,
             "created_ts": NOW - 50, "strength": 0.9},
            {"tag": "b", "symbol": "S", "limit_price": 104,
             "created_ts": NOW - 60, "strength": 0.9},
        ]
        specs, skips = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                                  levels=[(105.0, "short_stops", 0.95)],
                                  structure={}, now_ts=NOW,
                                  open_fleet=open_fleet)
        evicted = [s.detail for s in skips if s.reason == "evicted"]
        assert evicted == ["old"]

    def test_global_cap_eviction(self):
        other = [
            {"tag": f"g{i}", "symbol": f"X{i}", "limit_price": 50.0,
             "created_ts": NOW - i, "strength": 0.1}
            for i in range(12)  # global full
        ]
        specs, skips = plan_fleet(cfg(), symbol="S", mark_price=100.0, atr=0.5,
                                  levels=[(102.0, "short_stops", 0.95)],
                                  structure={}, now_ts=NOW,
                                  open_fleet=other)
        assert len(specs) == 1
        assert any(s.reason == "evicted" for s in skips)

    def test_capacity_overflow_skips_weakest_new(self):
        open_fleet = [
            {"tag": "inc", "symbol": "S", "limit_price": 101,
             "created_ts": NOW - 10, "strength": 0.99},
        ]
        # cap 2 per symbol, 1 incumbent (strength .99, unevictable need=1 but
        # only room for 1 after cap) -> 3 candidates, strongest kept
        specs, skips = plan_fleet(cfg(anticipator_max_per_symbol=2), symbol="S",
                                  mark_price=100.0, atr=0.5,
                                  levels=[(102.0, "short_stops", 0.5),
                                          (103.0, "short_stops", 0.9),
                                          (104.0, "short_stops", 0.7)],
                                  structure={}, now_ts=NOW,
                                  open_fleet=open_fleet)
        # room = 2 - 1 = 1, plus eviction of incumbent if needed:
        # need_sym = 3 - (2-1) = 2 -> incumbent evicted only once, room 2
        assert len(specs) == 2
        assert {round(s.strength, 2) for s in specs} == {0.9, 0.7}
        assert any(s.reason == "fleet_capacity" for s in skips)


# ---------------------------------------------------------------------------
# Prune verdicts
# ---------------------------------------------------------------------------


def _order(tag, sym, limit, created, placed_mark=None):
    o = {"tag": tag, "symbol": sym, "limit_price": limit, "created_ts": created}
    if placed_mark is not None:
        o["placed_mark"] = placed_mark
    return o


class TestPrune:
    def test_stale_and_moved_away_cancels(self):
        # placed at 100 (level 102, dist 1.96%); mark now 99 (dist 2.94%)
        # -> moved away ~0.98% > 0.5%
        oo = [_order("a", "S", 102.0, NOW - 3000, placed_mark=100.0)]
        out = prune_verdicts(cfg(), open_orders=oo,
                             mark_prices={"S": 99.0}, now_ts=NOW)
        assert out == ["a"]

    def test_exactly_45min_moved_toward_keeps(self):
        # age exactly 2700 (== stale_s, not >), mark moved toward level
        oo = [_order("a", "S", 102.0, NOW - 2700, placed_mark=100.0)]
        out = prune_verdicts(cfg(), open_orders=oo,
                             mark_prices={"S": 101.5}, now_ts=NOW)
        assert out == []

    def test_stale_moved_toward_keeps(self):
        oo = [_order("a", "S", 102.0, NOW - 4000, placed_mark=100.0)]
        out = prune_verdicts(cfg(), open_orders=oo,
                             mark_prices={"S": 101.5}, now_ts=NOW)
        assert out == []

    def test_stale_moved_away_under_threshold_keeps(self):
        # moved away only 0.2%
        oo = [_order("a", "S", 102.0, NOW - 3000, placed_mark=100.0)]
        out = prune_verdicts(cfg(), open_orders=oo,
                             mark_prices={"S": 99.8}, now_ts=NOW)
        assert out == []

    def test_absolute_age_cap_cancels_regardless(self):
        # > 4h old even though mark moved TOWARD the level
        oo = [_order("a", "S", 102.0, NOW - 14500, placed_mark=100.0)]
        out = prune_verdicts(cfg(), open_orders=oo,
                             mark_prices={"S": 101.9}, now_ts=NOW)
        assert out == ["a"]

    def test_young_order_kept(self):
        oo = [_order("a", "S", 102.0, NOW - 600, placed_mark=100.0)]
        out = prune_verdicts(cfg(), open_orders=oo,
                             mark_prices={"S": 95.0}, now_ts=NOW)
        assert out == []

    def test_missing_mark_kept(self):
        oo = [_order("a", "S", 102.0, NOW - 3000, placed_mark=100.0)]
        out = prune_verdicts(cfg(), open_orders=oo, mark_prices={}, now_ts=NOW)
        assert out == []

    def test_missing_placed_mark_kept(self):
        # away-leg unprovable -> free option stays
        oo = [_order("a", "S", 102.0, NOW - 3000)]
        out = prune_verdicts(cfg(), open_orders=oo,
                             mark_prices={"S": 99.0}, now_ts=NOW)
        assert out == []

    def test_ms_created_ts_tolerated(self):
        oo = [_order("a", "S", 102.0, (NOW - 14500) * 1000, placed_mark=100.0)]
        out = prune_verdicts(cfg(), open_orders=oo,
                             mark_prices={"S": 101.9}, now_ts=NOW)
        assert out == ["a"]

    def test_empty_orders_no_crash(self):
        assert prune_verdicts(cfg(), open_orders=[], mark_prices={},
                              now_ts=NOW) == []


# ---------------------------------------------------------------------------
# Fill attribution
# ---------------------------------------------------------------------------


class TestFillAttribution:
    def test_fill_within_proximity_after_placement_attributed(self):
        fills = [{"price": 102.05, "qty": 1.5, "ts": NOW + 10}]
        q = fill_attribution(order_tag="t", fills=fills,
                             limit_price=102.0, placed_ts=NOW)
        assert q == pytest.approx(1.5)

    def test_fill_beyond_01pct_not_attributed(self):
        fills = [{"price": 102.2, "qty": 1.5, "ts": NOW + 10}]  # 0.196% away
        q = fill_attribution(order_tag="t", fills=fills,
                             limit_price=102.0, placed_ts=NOW)
        assert q == 0.0

    def test_fill_exactly_at_01pct_boundary_attributed(self):
        fills = [{"price": 102.0 * 1.001, "qty": 2.0, "ts": NOW + 10}]
        q = fill_attribution(order_tag="t", fills=fills,
                             limit_price=102.0, placed_ts=NOW)
        assert q == pytest.approx(2.0)

    def test_fill_before_placement_not_attributed(self):
        fills = [{"price": 102.0, "qty": 1.5, "ts": NOW - 5}]
        q = fill_attribution(order_tag="t", fills=fills,
                             limit_price=102.0, placed_ts=NOW)
        assert q == 0.0

    def test_mismatched_tag_ignored(self):
        fills = [{"price": 102.0, "qty": 1.5, "ts": NOW + 10, "tag": "other"}]
        q = fill_attribution(order_tag="t", fills=fills,
                             limit_price=102.0, placed_ts=NOW)
        assert q == 0.0

    def test_matching_tag_short_circuits_proximity_eligibility(self):
        fills = [{"price": 102.0, "qty": 1.5, "ts": NOW + 10, "tag": "t"}]
        q = fill_attribution(order_tag="t", fills=fills,
                             limit_price=102.0, placed_ts=NOW)
        assert q == pytest.approx(1.5)

    def test_multiple_fills_summed(self):
        fills = [{"price": 102.01, "qty": 1.0, "ts": NOW + 10},
                 {"price": 101.99, "qty": 0.5, "ts": NOW + 20},
                 {"price": 105.0, "qty": 9.0, "ts": NOW + 30}]  # too far
        q = fill_attribution(order_tag="t", fills=fills,
                             limit_price=102.0, placed_ts=NOW)
        assert q == pytest.approx(1.5)

    def test_ms_fill_ts_normalized(self):
        fills = [{"price": 102.0, "qty": 1.0, "ts": (NOW + 10) * 1000}]
        q = fill_attribution(order_tag="t", fills=fills,
                             limit_price=102.0, placed_ts=NOW)
        assert q == pytest.approx(1.0)

    def test_no_limit_no_placed_accepts_untagged(self):
        fills = [{"price": 1.0, "qty": 3.0, "ts": 0.0}]
        q = fill_attribution(order_tag="t", fills=fills)
        assert q == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Residual completion verdict
# ---------------------------------------------------------------------------


def _spec(**over):
    base = dict(tag="t", symbol="S", side="short", limit_price=102.0,
                stop_price=103.0, tp_price=96.0, qty_margin_usd=100.0,
                strength=0.8, ttl_s=2700.0, created_ts=NOW)
    base.update(over)
    return OrderSpec(**base)


class TestResidual:
    def test_within_ttl_holds(self):
        v = residual_completion_verdict(cfg(), spec=_spec(), filled_qty=0.7,
                                        target_qty=1.0, mark_price=102.0,
                                        now_ts=NOW + 100)
        assert v.action == "hold" and v.reason == "within_ttl"

    def test_fully_filled_holds(self):
        v = residual_completion_verdict(cfg(), spec=_spec(), filled_qty=1.0,
                                        target_qty=1.0, mark_price=102.0,
                                        now_ts=NOW + 3000)
        assert v.action == "hold" and v.reason == "fully_filled"

    def test_60pct_at_ttl_mark_within_band_completes(self):
        v = residual_completion_verdict(cfg(), spec=_spec(), filled_qty=0.6,
                                        target_qty=1.0, mark_price=102.1,
                                        now_ts=NOW + 2701)
        assert v.action == "complete_market"

    def test_exactly_60pct_boundary_completes(self):
        v = residual_completion_verdict(cfg(), spec=_spec(), filled_qty=0.6,
                                        target_qty=1.0, mark_price=102.0,
                                        now_ts=NOW + 99999)
        assert v.action == "complete_market"

    def test_mark_exactly_at_chase_band_completes(self):
        v = residual_completion_verdict(cfg(), spec=_spec(), filled_qty=0.7,
                                        target_qty=1.0,
                                        mark_price=102.0 * 1.003,
                                        now_ts=NOW + 99999)
        assert v.action == "complete_market"

    def test_60pct_mark_beyond_band_cancels(self):
        v = residual_completion_verdict(cfg(), spec=_spec(), filled_qty=0.7,
                                        target_qty=1.0,
                                        mark_price=102.0 * 1.004,
                                        now_ts=NOW + 99999)
        assert v.action == "cancel_remainder"
        assert v.reason == "chase_would_kill_edge"

    def test_below_60pct_at_ttl_cancels(self):
        v = residual_completion_verdict(cfg(), spec=_spec(), filled_qty=0.59,
                                        target_qty=1.0, mark_price=102.0,
                                        now_ts=NOW + 99999)
        assert v.action == "cancel_remainder"
        assert v.reason == "below_complete_frac"

    def test_mark_unknown_at_ttl_fail_closed_cancels(self):
        v = residual_completion_verdict(cfg(), spec=_spec(), filled_qty=0.9,
                                        target_qty=1.0, mark_price=None,
                                        now_ts=NOW + 99999)
        assert v.action == "cancel_remainder"
        assert v.reason == "mark_unknown"

    def test_zero_target_cancels(self):
        v = residual_completion_verdict(cfg(), spec=_spec(), filled_qty=0.0,
                                        target_qty=0.0, mark_price=102.0,
                                        now_ts=NOW)
        assert v.action == "cancel_remainder"
        assert v.reason == "zero_target"

    def test_zero_fill_at_ttl_cancels(self):
        v = residual_completion_verdict(cfg(), spec=_spec(), filled_qty=0.0,
                                        target_qty=1.0, mark_price=102.0,
                                        now_ts=NOW + 99999)
        assert v.action == "cancel_remainder"
