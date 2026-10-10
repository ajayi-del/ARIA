"""tests/test_runner_eligibility.py — pins for the wave-2 RES brain.

Doctrine pinned (quant spec /tmp/aria_wave1_quant.md §1): RES =
0.25·T + 0.20·R + 0.15·C + 0.15·W + 0.15·P + 0.10·F with hard vetoes
(tide opposed / whale crowding ≥90pct against); decision ladder
runner ≥0.70 / standard / standard_tight <0.45; re-eval would_demote <
RES_TIGHT; four abort floors; fail-closed internal fault; kill-switch
disabled semantics; the runner-plan shadow (fixed-pct triggers, fr-only
warmup, funding-flip abort, breakeven floor by construction).

Includes the spec's SOL 10-06 reproduction case (runner) and the 10-08/09
follow-on cluster case (standard/tight) with module-true inputs.
"""
from types import SimpleNamespace

import pytest

from intelligence import runner_eligibility as re_mod
from intelligence.runner_eligibility import (
    RunnerEligibility, funding_view_for, runner_breakeven_floor,
    runner_plan_shadow, whale_pct_against, whale_ratio_against,
)


def _cfg(**over):
    base = dict(runner_eligibility_enabled=True,
                runner_demotion_enabled=False,
                runner_res_runner=0.70, runner_res_tight=0.45,
                runner_plan_shadow_enabled=True)
    base.update(over)
    return SimpleNamespace(**base)


# ── decision ladder ──────────────────────────────────────────────────────────

def test_sol_1006_reproduction_is_runner():
    # SOL 10-06 11:15 short (spec §1): tide aligned, alt_season conf 0.72,
    # SOL in leading category, whale dark (None→0.5), pillars 2/3 warm,
    # funding neutral. RES = .25+.20+.15+.075+.10+.05 = 0.825 → runner.
    v = RunnerEligibility().evaluate(
        cfg=_cfg(), symbol="SOL-USD", side="short",
        tide_align="aligned", regime_conf=0.72,
        category="alt_l1", leading_category="alt_l1",
        lagging_category="large_cap",
        whale_pct_against=None, pillar_warm=2.0 / 3.0,
        funding_view="neutral")
    assert v["decision"] == "runner"
    assert v["res_score"] == pytest.approx(0.825, abs=1e-3)
    assert v["veto"] is None
    assert v["would_demote"] is False


def test_followon_cluster_is_standard_tight():
    # XRP/ETH/LINK/BCH 10-08/09 cluster (spec §1): pillars ≤1/3 warm,
    # funding opposed, category not leading, tide neutral, conf low.
    # RES = .25(.4)+.20(.2)+.15(.4)+.15(.5)+.15(1/3)+.10(0) = 0.325 → tight.
    v = RunnerEligibility().evaluate(
        cfg=_cfg(), symbol="XRP-USD", side="short",
        tide_align="neutral", regime_conf=0.40,
        category="alt_l1", leading_category="large_cap",
        lagging_category="meme",
        whale_pct_against=None, pillar_warm=1.0 / 3.0,
        funding_view="opposed")
    assert v["decision"] == "standard_tight"
    assert v["res_score"] == pytest.approx(0.325, abs=1e-3)
    assert v["would_demote"] is True


def test_mid_band_is_standard():
    # tide neutral .4, conf 0.6, cat leading, whale mid, pillar 1, funding
    # favors: .10+.12+.15+.075+.15+.10 = 0.695 → standard (just under 0.70)
    v = RunnerEligibility().evaluate(
        cfg=_cfg(), symbol="BTC-USD", side="long",
        tide_align="neutral", regime_conf=0.60,
        category="large_cap", leading_category="large_cap",
        lagging_category="meme",
        whale_pct_against=50.0, pillar_warm=1.0,
        funding_view="favors")
    assert v["decision"] == "standard"
    assert v["res_score"] == pytest.approx(0.695, abs=1e-3)


# ── hard vetoes ──────────────────────────────────────────────────────────────

def test_v1_tide_opposed_vetoes():
    v = RunnerEligibility().evaluate(
        cfg=_cfg(), symbol="ETH-USD", side="long",
        tide_align="opposed", regime_conf=0.9,
        category="alt_l1", leading_category="alt_l1",
        lagging_category="meme",
        whale_pct_against=0.0, pillar_warm=1.0, funding_view="favors")
    assert v["decision"] == "standard"
    assert v["veto"] == "V1_tide_opposed"
    assert v["res_score"] is None
    assert v["stamp"]["tide_align"] == "opposed"


def test_v2_whale_crowding_vetoes():
    v = RunnerEligibility().evaluate(
        cfg=_cfg(), symbol="ETH-USD", side="short",
        tide_align="aligned", regime_conf=0.9,
        category="alt_l1", leading_category="alt_l1",
        lagging_category="meme",
        whale_pct_against=92.0, pillar_warm=1.0, funding_view="favors")
    assert v["veto"] == "V2_whale_crowding"
    assert v["res_score"] is None


def test_whale_veto_boundary_90_trips_89_passes():
    brain = RunnerEligibility()
    v89 = brain.evaluate(cfg=_cfg(), symbol="S", side="short",
                         tide_align="aligned", whale_pct_against=89.9)
    assert v89["veto"] is None
    v90 = brain.evaluate(cfg=_cfg(), symbol="S", side="short",
                         tide_align="aligned", whale_pct_against=90.0)
    assert v90["veto"] == "V2_whale_crowding"


# ── kill switch + fail-closed ────────────────────────────────────────────────

def test_disabled_returns_disabled_decision():
    v = RunnerEligibility().evaluate(
        cfg=_cfg(runner_eligibility_enabled=False),
        symbol="SOL-USD", side="long", tide_align="aligned")
    assert v["enabled"] is False
    assert v["decision"] == "disabled"
    assert v["res_score"] is None


def test_internal_fault_fails_closed():
    # cfg whose getattr chain explodes (res_runner coercion failure is
    # guarded by `or RES_RUNNER`; force a real fault via a cfg property)
    class _Boom:
        def __getattr__(self, k):
            raise RuntimeError("boom")
    v = RunnerEligibility().evaluate(cfg=_Boom(), symbol="S", side="long")
    assert v["decision"] == "standard"
    assert v["reason"] == "internal_error"
    assert v["res_score"] is None


def test_no_direction_abstains():
    v = RunnerEligibility().evaluate(cfg=_cfg(), symbol="S", side="flat")
    assert v["decision"] == "standard"
    assert v["reason"] == "no_direction"


# ── axis helpers ─────────────────────────────────────────────────────────────

def test_whale_pct_against_axis():
    # ratio>1 = crowd long. Short → pct as-is; long → 100−pct.
    assert whale_pct_against("short", 80.0) == 80.0
    assert whale_pct_against("long", 80.0) == pytest.approx(20.0)
    assert whale_pct_against("short", None) is None
    assert whale_pct_against("flat", 50.0) is None


def test_whale_ratio_against_axis():
    assert whale_ratio_against("short", 1.8) == pytest.approx(1.8)
    assert whale_ratio_against("long", 0.5) == pytest.approx(2.0)
    assert whale_ratio_against("long", 0.0) is None
    assert whale_ratio_against("short", None) is None


def test_funding_view_for_side():
    # positive rate = longs pay shorts (crowd long): favors shorts
    assert funding_view_for("short", 0.0005) == "favors"
    assert funding_view_for("long", 0.0005) == "opposed"
    assert funding_view_for("long", -0.0005) == "favors"
    assert funding_view_for("short", None) == "neutral"
    assert funding_view_for("long", 0.0) == "neutral"


# ── re-eval + demotion gate ──────────────────────────────────────────────────

def test_reeval_would_demote_but_demotion_dark():
    v = RunnerEligibility().reeval(
        cfg=_cfg(), symbol="SOL-USD", side="short",
        tide_align="neutral", regime_conf=0.40,
        category="alt_l1", leading_category="large_cap",
        lagging_category="meme",
        whale_pct_against=None, pillar_warm=1.0 / 3.0,
        funding_view="opposed")
    assert v["would_demote"] is True
    assert v["demotion_live"] is False   # runner_demotion_enabled DARK


def test_reeval_demotion_live_only_when_armed():
    v = RunnerEligibility().reeval(
        cfg=_cfg(runner_demotion_enabled=True),
        symbol="SOL-USD", side="short",
        tide_align="neutral", regime_conf=0.40,
        category="alt_l1", leading_category="large_cap",
        lagging_category="meme",
        whale_pct_against=None, pillar_warm=0.0,
        funding_view="opposed")
    assert v["would_demote"] is True
    assert v["demotion_live"] is True


# ── abort floors ─────────────────────────────────────────────────────────────

def test_abort_regime_collapse():
    v = RunnerEligibility().abort_verdict(
        cfg=_cfg(), side="long", regime_conf_now=0.55)
    assert v["abort"] is True
    assert "regime_collapse" in v["reasons"]
    assert v["demotion_live"] is False


def test_abort_tide_flip_opposed():
    v = RunnerEligibility().abort_verdict(
        cfg=_cfg(), side="long", tide_align="opposed",
        regime_conf_now=0.9)
    assert v["abort"] is True
    assert "tide_flip_opposed" in v["reasons"]


def test_abort_whale_raw_ratio_preferred():
    v = RunnerEligibility().abort_verdict(
        cfg=_cfg(), side="short", whale_ratio=1.9, whale_pct_against=10.0,
        regime_conf_now=0.9)
    assert v["abort"] is True
    assert "whale_crowding_against" in v["reasons"]


def test_abort_whale_percentile_fallback_when_ratio_dark():
    v = RunnerEligibility().abort_verdict(
        cfg=_cfg(), side="short", whale_ratio=None, whale_pct_against=95.0,
        regime_conf_now=0.9)
    assert v["abort"] is True
    assert "whale_crowding_against" in v["reasons"]


def test_abort_funding_sign_flip_needs_entry_baseline():
    brain = RunnerEligibility()
    # entry favors → now opposed: flip
    v = brain.abort_verdict(cfg=_cfg(), side="long",
                            funding_view_entry="favors",
                            funding_view_now="opposed", regime_conf_now=0.9)
    assert v["abort"] is True
    assert "funding_sign_flip" in v["reasons"]
    # entry baseline unknown → abstain, never invent a flip
    v2 = brain.abort_verdict(cfg=_cfg(), side="long",
                             funding_view_entry=None,
                             funding_view_now="opposed", regime_conf_now=0.9)
    assert v2["abort"] is False


def test_abort_disabled_is_silent():
    v = RunnerEligibility().abort_verdict(
        cfg=_cfg(runner_eligibility_enabled=False),
        side="long", tide_align="opposed", regime_conf_now=0.1)
    assert v["abort"] is False
    assert v["reasons"] == []


# ── runner-plan shadow ───────────────────────────────────────────────────────

def test_runner_plan_first_rung_fires_at_4p5pct():
    out = runner_plan_shadow(
        cfg=_cfg(), side="long", base_entry=100.0, base_qty=1.0,
        mark=105.0, rungs_fired=set(), fr_leg_present=True)
    assert out["would_add"] is True
    assert out["rung"] == 0
    assert out["runner_qty"] == pytest.approx(0.30)
    assert out["floor_after_l1"] is not None
    assert out["runner_keep_frac"] == 0.25


def test_runner_plan_rungs_ascend_in_order():
    cfg = _cfg()
    out0 = runner_plan_shadow(cfg=cfg, side="long", base_entry=100.0,
                              base_qty=1.0, mark=105.0, rungs_fired=set(),
                              fr_leg_present=True)
    assert out0["rung"] == 0
    # 9.5% move with rung 0 fired → rung 1 (0.20 of base)
    out1 = runner_plan_shadow(cfg=cfg, side="long", base_entry=100.0,
                              base_qty=1.0, mark=109.5, rungs_fired={0},
                              fr_leg_present=True)
    assert out1["rung"] == 1
    assert out1["runner_qty"] == pytest.approx(0.20)
    # 12%+ with 0,1 fired → rung 2 (0.10)
    out2 = runner_plan_shadow(cfg=cfg, side="long", base_entry=100.0,
                              base_qty=1.0, mark=113.0, rungs_fired={0, 1},
                              fr_leg_present=True)
    assert out2["rung"] == 2
    assert out2["runner_qty"] == pytest.approx(0.10)


def test_runner_plan_short_side_mirror():
    out = runner_plan_shadow(
        cfg=_cfg(), side="short", base_entry=100.0, base_qty=2.0,
        mark=95.0, rungs_fired=set(), fr_leg_present=True)
    assert out["would_add"] is True
    assert out["add_px"] == pytest.approx(95.5)


def test_runner_plan_warmup_fr_dark_blocks():
    out = runner_plan_shadow(
        cfg=_cfg(), side="long", base_entry=100.0, base_qty=1.0,
        mark=105.0, rungs_fired=set(), fr_leg_present=False)
    assert out["would_add"] is False
    assert out["reason"] == "warmup_fr_dark"


def test_runner_plan_funding_flip_aborts():
    out = runner_plan_shadow(
        cfg=_cfg(), side="long", base_entry=100.0, base_qty=1.0,
        mark=105.0, rungs_fired=set(), fr_leg_present=True,
        funding_view_entry="favors", funding_view_now="opposed")
    assert out["would_add"] is False
    assert out["funding_abort"] is True
    assert out["reason"] == "funding_sign_flip_abort"


def test_runner_plan_disabled_is_inert():
    out = runner_plan_shadow(
        cfg=_cfg(runner_plan_shadow_enabled=False),
        side="long", base_entry=100.0, base_qty=1.0,
        mark=105.0, rungs_fired=set(), fr_leg_present=True)
    assert out["would_add"] is False
    assert out["reason"] == "disabled"


def test_runner_breakeven_floor_by_construction():
    # combined VWAP ∓ 0.4% buffer (aster_swing_floor_price idiom)
    f = runner_breakeven_floor("long", 1.0, 100.0, 0.30, 105.0)
    vwap = (1.0 * 100.0 + 0.30 * 105.0) / 1.30
    assert f == pytest.approx(vwap * (1.0 - re_mod.RUNNER_BE_BUFFER))
    fs = runner_breakeven_floor("short", 1.0, 100.0, 0.30, 95.0)
    vwap_s = (1.0 * 100.0 + 0.30 * 95.0) / 1.30
    assert fs == pytest.approx(vwap_s * (1.0 + re_mod.RUNNER_BE_BUFFER))
    assert runner_breakeven_floor("long", 0, 100.0, 0.3, 105.0) is None
