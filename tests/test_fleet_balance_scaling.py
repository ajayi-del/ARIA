"""tests/test_fleet_balance_scaling.py — pins for the 2026-09-30 fleet
balance scaling float (Governor directive "reduce the balance slightly per
trade... allow it to scale as balance increases or reduced", ultrathink +
ruling: ref=$500, clamps [0.6, 1.3] → mult ≈0.90 at the ~$450 book).

Doctrine under test: ONE multiplier clamp(equity/ref, min, max) floats the
four fixed-USD campaign surfaces — fleet rung margins (plan_fleet
size_mult), the engine pool + flat fallback margin (entry_verdict
size_mult), and the deterministic margin budget (main.py preflights).
Default/knob-off = legacy fixed-USD bit-for-bit. The dust floor applies
AFTER scaling (fail-safe: a scaled-down rung that lands sub-floor dies).
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.anticipator import plan_fleet  # noqa: E402
from intelligence.fast_cycle_engine import FastCycleEngine  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SRC = os.path.join(_REPO, "main.py")

NOW = 1_800_000_600.0


def _main_src() -> str:
    with open(_MAIN_SRC) as fh:
        return fh.read()


def acfg(**over):
    base = dict(
        anticipator_enabled=True,
        anticipator_min_distance_pct=0.3,
        anticipator_max_distance_pct=1.2,
        anticipator_stale_s=2700.0,
        anticipator_max_age_s=14400.0,
        anticipator_max_per_symbol=4,
        anticipator_max_global=12,
        anticipator_cage_min=3.0,
        anticipator_stop_atr_frac=1.0,
        anticipator_margin_usd=55.0,
        anticipator_margin_usd_by_symbol="XRP-USD:50,NEAR-USD:35",
        anticipator_min_margin_usd=10.0,
        anticipator_entry_nudge_pct=0.05,
        anticipator_residual_complete_frac=0.6,
        anticipator_residual_chase_pct=0.003,
        anticipator_level_coverage_enabled=True,
        anticipator_level_tolerance_pct=0.1,
        anticipator_min_rest_s=300.0,
        anticipator_inplace_upgrade_enabled=False,
        anticipator_drift_evict_enabled=True,
        anticipator_funding_clock_gate_enabled=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


def fcfg(**over):
    base = dict(fast_cycle_enabled=True,
                fast_cycle_margin_per_trade=55.0,
                fast_cycle_pool_usd=300.0,
                fast_cycle_max_leverage=20)
    base.update(over)
    return SimpleNamespace(**base)


_LEVELS = [(99.0, "long", 1.0)]  # 1% below mark — inside the [0.3,1.2] band


def _plan(c, size_mult=None):
    kw = {}
    if size_mult is not None:
        kw["size_mult"] = size_mult
    return plan_fleet(c, symbol="XRP-USD", mark_price=100.0, atr=1.0,
                      levels=_LEVELS, structure=None, now_ts=NOW,
                      open_fleet=[], **kw)


# ── plan_fleet: rung margins float ──────────────────────────────────────────

class TestPlanFleetSizeMult:
    def test_default_is_legacy(self):
        specs, _ = _plan(acfg())
        assert len(specs) == 1
        # rung 50 × (0.5 + 0.5×1.0) = 50 — pre-float value bit-for-bit
        assert specs[0].qty_margin_usd == pytest.approx(50.0)

    def test_explicit_one_is_legacy(self):
        specs, _ = _plan(acfg(), size_mult=1.0)
        assert specs[0].qty_margin_usd == pytest.approx(50.0)

    def test_reduction_floats_the_rung(self):
        specs, _ = _plan(acfg(), size_mult=0.9)
        assert specs[0].qty_margin_usd == pytest.approx(45.0)

    def test_expansion_floats_the_rung(self):
        specs, _ = _plan(acfg(), size_mult=1.3)
        assert specs[0].qty_margin_usd == pytest.approx(65.0)

    def test_strength_scaling_composes_with_the_float(self):
        # 50 × (0.5 + 0.5×0.5) × 0.9 = 50 × 0.75 × 0.9
        specs, _ = plan_fleet(
            acfg(), symbol="XRP-USD", mark_price=100.0, atr=1.0,
            levels=[(99.0, "long", 0.5)], structure=None, now_ts=NOW,
            open_fleet=[], size_mult=0.9)
        assert specs[0].qty_margin_usd == pytest.approx(33.75)

    def test_dust_floor_applies_after_scaling(self):
        # NEAR rung 35 × strength 0.5-scale 0.75 × mult 0.35 = 9.19 < 10 floor
        specs, _ = plan_fleet(
            acfg(), symbol="NEAR-USD", mark_price=100.0, atr=1.0,
            levels=[(99.0, "long", 0.5)], structure=None, now_ts=NOW,
            open_fleet=[], size_mult=0.35)
        assert specs == []

    def test_zero_mult_kills_specs_not_crash(self):
        specs, _ = _plan(acfg(), size_mult=0.0)
        assert specs == []


# ── entry_verdict: pool + flat fallback float ───────────────────────────────

class TestEntryVerdictSizeMult:
    def _engine_with_debited(self, amount):
        eng = FastCycleEngine()
        eng.on_entry("XRP-USD", amount)
        return eng

    def test_default_pool_unchanged(self):
        eng = self._engine_with_debited(250.0)
        # pool 300, debited 250, margin 55 → 300−250=50 < 55 → exhausted
        v = eng.entry_verdict(
            fcfg(), symbol="XRP-USD", side="long",
            entry_price=100.0, stop_price=99.0, tp_price=104.0,
            open_positions=[], now_ts=NOW)
        assert v.action == "standdown"
        assert v.reason == "pool_exhausted"

    def test_explicit_one_pool_unchanged(self):
        eng = self._engine_with_debited(250.0)
        v = eng.entry_verdict(
            fcfg(), symbol="XRP-USD", side="long",
            entry_price=100.0, stop_price=99.0, tp_price=104.0,
            open_positions=[], now_ts=NOW, size_mult=1.0)
        assert v.reason == "pool_exhausted"

    def test_scaled_pool_tightens(self):
        # mult 0.9 → pool 270 AND flat margin 49.5; debited 225 →
        # 270−225=45 < 49.5 → exhausted, while the unscaled 300−225=75 ≥ 55
        # would approve. (The float tightens via the FIXED debited ledger —
        # pool and margin scale together, so only existing debits shift the
        # boundary.)
        eng = self._engine_with_debited(225.0)
        v = eng.entry_verdict(
            fcfg(), symbol="XRP-USD", side="long",
            entry_price=100.0, stop_price=99.0, tp_price=104.0,
            open_positions=[], now_ts=NOW, size_mult=0.9)
        assert v.reason == "pool_exhausted"
        eng2 = self._engine_with_debited(225.0)
        v2 = eng2.entry_verdict(
            fcfg(), symbol="XRP-USD", side="long",
            entry_price=100.0, stop_price=99.0, tp_price=104.0,
            open_positions=[], now_ts=NOW)
        assert v2.action == "approve"

    def test_flat_fallback_margin_scales(self):
        eng = FastCycleEngine()
        v = eng.entry_verdict(
            fcfg(), symbol="XRP-USD", side="long",
            entry_price=100.0, stop_price=99.0, tp_price=104.0,
            open_positions=[], now_ts=NOW, size_mult=0.9)
        assert v.action == "approve"
        assert v.margin_usd == pytest.approx(49.5)   # 55 × 0.9
        assert v.notional_usd == pytest.approx(49.5 * v.leverage)

    def test_proposed_margin_not_double_scaled(self):
        # The caller's proposed margin arrives pre-scaled from plan_fleet —
        # size_mult must touch the POOL only on this path.
        eng = FastCycleEngine()
        v = eng.entry_verdict(
            fcfg(), symbol="XRP-USD", side="long",
            entry_price=100.0, stop_price=99.0, tp_price=104.0,
            open_positions=[], now_ts=NOW,
            proposed_margin_usd=45.0, size_mult=0.9)
        assert v.action == "approve"
        assert v.margin_usd == pytest.approx(45.0)


# ── wiring (source pins) ────────────────────────────────────────────────────

class TestWiring:
    def test_plan_fleet_call_carries_the_float(self):
        src = _main_src()
        assert "size_mult=_fc_size_mult," in src
        assert "size_mult=_fc_size_mult)" in src  # entry_verdict call sites

    def test_both_entry_verdict_sites_float(self):
        src = _main_src()
        assert src.count("size_mult=_fc_size_mult") >= 3  # plan + ant + xpr

    def test_budget_preflights_float(self):
        src = _main_src()
        assert src.count('"anticipator_margin_budget_usd", 250.0))\n'
                         '                                    * _fc_size_mult)') >= 2

    def test_state_and_clamp_block(self):
        src = _main_src()
        assert "_fc_size_mult_state = [1.0]" in src
        assert "fleet_balance_scaling_enabled" in src
        assert "fleet_balance_ref_usd" in src
        assert "fleet_balance_mult_min" in src
        assert "fleet_balance_mult_max" in src

    def test_telemetry(self):
        src = _main_src()
        assert "fleet_balance_scaling" in src
        assert "size_mult=round(_fc_size_mult, 3)" in src


# ── config knobs (Governor-ruled values) ────────────────────────────────────

class TestConfigKnobs:
    def test_knobs_exist_with_ruled_defaults(self):
        from core.config import Settings
        s = Settings()
        assert s.fleet_balance_scaling_enabled is True
        assert s.fleet_balance_ref_usd == pytest.approx(500.0)
        assert s.fleet_balance_mult_min == pytest.approx(0.6)
        assert s.fleet_balance_mult_max == pytest.approx(1.3)
