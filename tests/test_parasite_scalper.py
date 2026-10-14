"""
tests/test_parasite_scalper.py — pins for the parasite scalper brain
(Three-Layer Network Phase 1, 2026-10-10). SHADOW-ONLY from birth.

Doctrine under test:
  - prediction score = L4 imbalance 25 + OI delta 20 + fleet velocity 20
    + cross-asset sync 20 + funding velocity 15; None input -> component
    0, never a crash; grade ladder >=70 A / >=45 B / else none (a "none"
    verdict is still returned — the shadow sees the full distribution).
  - loss-as-signal: fleet stop_hit -> Grade-A reversal (direction flip,
    TP 3.0% / stop 0.8%, score 75, ecosystem-routed); 3 consecutive
    same-symbol losses confirm -> score 85.
  - admission envelope: max_concurrent hard-clamped <=10; $2 margin /
    $20 budget getattr defaults; margin hard-clamped to remaining budget.
  - cycle constants 240/90/480 present for the integrator's loop.
  - journal_rows(): dense one-line dicts, dark legs None.
  - kill switch scalper_shadow_enabled=False -> bit-for-bit no-op, zero
    state mutation. tick() never raises.
  - ScalpSignalGrade frozen.

No network, no Settings() real env — SimpleNamespace cfgs per repo idiom.
"""
import dataclasses
from types import SimpleNamespace

import pytest

from intelligence.ecosystem_router import EcosystemRouter
from intelligence.fleet_events import make_event
from intelligence.parasite_scalper import (
    CYCLE_PERIOD_S,
    FILL_CHECK_S,
    FORCE_CLOSE_S,
    GRADE_A_MIN,
    GRADE_B_MIN,
    NONE_GRADE,
    ParasiteScalperEngine,
    ScalpSignalGrade,
    grade_for_score,
    score_cross_asset_sync,
    score_fleet_velocity,
    score_funding_velocity,
    score_l4_imbalance,
    score_oi_delta,
)

T0 = 1_760_000_000.0


def cfg(**over):
    base = dict(scalper_shadow_enabled=True)
    base.update(over)
    return SimpleNamespace(**base)


def engine(**over):
    return ParasiteScalperEngine(cfg(**over), EcosystemRouter())


def fill(sym, ts=T0, direction="long"):
    return make_event("fill", sym, direction, 100.0, ts)


def stop(sym, ts=T0, direction="long"):
    return make_event("stop_hit", sym, direction, 100.0, ts)


def tick_kw(**over):
    kw = dict(fleet_events=[], l4_signals=None, regime=None,
              oi_deltas=None, funding_rates=None, cross_asset_moves=None,
              open_scalps=[], narrative_direction=None, now=T0)
    kw.update(over)
    return kw


# ── sub-scorer math ──────────────────────────────────────────────────────────

class TestSubScorers:
    def test_l4_imbalance(self):
        assert score_l4_imbalance(0.30, "long") == pytest.approx(25.0)
        assert score_l4_imbalance(0.15, "long") == pytest.approx(12.5)
        assert score_l4_imbalance(-0.30, "short") == pytest.approx(25.0)
        assert score_l4_imbalance(0.60, "long") == pytest.approx(25.0)  # clamp
        assert score_l4_imbalance(-0.10, "long") == 0.0                 # disagree
        assert score_l4_imbalance(None, "long") == 0.0                  # dark
        assert score_l4_imbalance("junk", "long") == 0.0
        assert score_l4_imbalance(0.30, "flat") == 0.0                  # bad side

    def test_oi_delta(self):
        assert score_oi_delta(2.0, "long") == pytest.approx(20.0)
        assert score_oi_delta(1.0, "long") == pytest.approx(10.0)
        assert score_oi_delta(-2.0, "short") == pytest.approx(20.0)
        assert score_oi_delta(-1.0, "long") == 0.0
        assert score_oi_delta(None, "long") == 0.0

    def test_fleet_velocity(self):
        assert score_fleet_velocity(3) == pytest.approx(20.0)
        assert score_fleet_velocity(1) == pytest.approx(20.0 / 3)
        assert score_fleet_velocity(9) == pytest.approx(20.0)           # clamp
        assert score_fleet_velocity(0) == 0.0
        assert score_fleet_velocity(None) == 0.0

    def test_cross_asset_sync(self):
        moves = {"COIN": 0.5, "HOOD": 0.5}
        assert score_cross_asset_sync(moves, "long") == pytest.approx(20.0)
        moves = {"COIN": 0.5, "HOOD": -0.5}
        assert score_cross_asset_sync(moves, "long") == pytest.approx(10.0)
        assert score_cross_asset_sync(moves, "short") == pytest.approx(10.0)
        # below the 0.2% "moves" threshold -> not agreement
        assert score_cross_asset_sync({"COIN": 0.1}, "long") == 0.0
        # unparseable legs drop out of the denominator
        assert score_cross_asset_sync({"COIN": 0.5, "HOOD": "junk"},
                                      "long") == pytest.approx(20.0)
        assert score_cross_asset_sync(None, "long") == 0.0
        assert score_cross_asset_sync({}, "long") == 0.0

    def test_funding_velocity_contrarian(self):
        # positive funding = crowded longs = short-friendly
        assert score_funding_velocity(0.0005, "short") == pytest.approx(15.0)
        assert score_funding_velocity(0.0005, "long") == 0.0
        assert score_funding_velocity(-0.0005, "long") == pytest.approx(15.0)
        assert score_funding_velocity(-0.00025, "long") == pytest.approx(7.5)
        assert score_funding_velocity(None, "long") == 0.0

    def test_grade_ladder_boundaries(self):
        assert GRADE_A_MIN == 70 and GRADE_B_MIN == 45
        assert grade_for_score(70) == "A"
        assert grade_for_score(69.9) == "B"
        assert grade_for_score(45) == "B"
        assert grade_for_score(44.9) == "none"

    def test_cycle_constants(self):
        # Re-encoded 2026-10-10 for the Governor's lifecycle paste:
        # "MINUTE 4:00 — OUTCOME ... If neither: FORCE CLOSE at market"
        # puts the force-close at the 240s cycle boundary, not 480s.
        assert (CYCLE_PERIOD_S, FILL_CHECK_S, FORCE_CLOSE_S) == (240, 90, 240)

    def test_grade_frozen(self):
        e = engine()
        out = e.tick(**tick_kw(fleet_events=[stop("ETH")]))
        g = out[0]
        assert isinstance(g, ScalpSignalGrade)
        with pytest.raises(dataclasses.FrozenInstanceError):
            g.prediction_score = 1


# ── loss-as-signal ───────────────────────────────────────────────────────────

class TestLossReversal:
    def test_stop_hit_flips_to_grade_a_reversal(self):
        e = engine()
        out = e.tick(**tick_kw(fleet_events=[stop("ETH", direction="long")]))
        assert len(out) == 1
        g = out[0]
        assert g.grade == "A" and g.source == "loss_reversal"
        assert g.direction == "short"                     # flipped
        assert g.primary_symbol == "ETH"
        assert g.prediction_score == 75
        assert g.tp_pct == 0.03 and g.stop_pct == 0.008
        # ecosystem-routed: family rides the grade
        assert g.ecosystem_symbols == ("ETH", "LINK", "UNI", "AAVE")

    def test_short_stop_flips_long(self):
        e = engine()
        out = e.tick(**tick_kw(fleet_events=[stop("BTC", direction="short")]))
        assert out[0].direction == "long"

    def test_three_consecutive_losses_confirm(self):
        e = engine()
        for _ in range(3):
            assert e.record_outcome("ETH", "loss") is True
        assert e.consecutive_losses("ETH") == 3
        out = e.tick(**tick_kw(fleet_events=[stop("ETH")]))
        assert out[0].prediction_score == 85
        rows = e.journal_rows()
        assert rows[0]["reversal_confirmed"] is True
        assert rows[0]["source"] == "loss_reversal"

    def test_win_resets_the_loss_run(self):
        e = engine()
        for oc in ("loss", "loss", "win", "loss", "loss"):
            e.record_outcome("ETH", oc)
        assert e.consecutive_losses("ETH") == 2
        out = e.tick(**tick_kw(fleet_events=[stop("ETH")]))
        assert out[0].prediction_score == 75               # unconfirmed
        # other symbols unaffected
        assert e.consecutive_losses("BTC") == 0

    def test_record_outcome_drops_garbage(self):
        e = engine()
        assert e.record_outcome("", "loss") is False
        assert e.record_outcome("ETH", "flat") is False
        assert e.record_outcome(None, None) is False
        assert e.consecutive_losses("ETH") == 0


# ── predictive grading ───────────────────────────────────────────────────────

class TestPredictive:
    def test_full_agreement_scores_100_grade_a(self):
        e = engine()
        events = [fill("BTC"), fill("BTC"), fill("BTC")]
        out = e.tick(**tick_kw(
            fleet_events=events,
            l4_signals={"BTC": 0.30},
            oi_deltas={"BTC": 2.0},
            funding_rates={"BTC": -0.0005},      # contrarian long-friendly
            cross_asset_moves={"COIN": 0.5, "HOOD": 0.5},
            narrative_direction={"BTC": "long"},
        ))
        assert len(out) == 1
        g = out[0]
        assert g.grade == "A" and g.source == "predictive"
        assert g.prediction_score == 100
        assert g.direction == "long"
        assert g.tp_pct == 0.024 and g.stop_pct == 0.006
        assert g.margin_per_position == 2.0 and g.leverage == 8
        # MSTR joined the BTC family 2026-10-10 (ecosystem-spec reconcile:
        # the one real BTC-family gap; MSTR-USD is universe-registered).
        assert g.ecosystem_symbols == ("BTC", "COIN", "HOOD", "MSTR")
        row = e.journal_rows()[0]
        assert row["components"] == {"l4": 25.0, "oi": 20.0, "fleet": 20.0,
                                     "cross": 20.0, "funding": 15.0}

    def test_partial_score_grades_b(self):
        e = engine()
        # l4 full (25) + oi half (10) + 1 fill (6.67) + half the family
        # agreeing (10) = 51.67 -> 52 -> B.
        out = e.tick(**tick_kw(
            fleet_events=[fill("BTC")],
            l4_signals={"BTC": 0.30},
            oi_deltas={"BTC": 1.0},
            cross_asset_moves={"COIN": 0.5, "HOOD": -0.5},
            narrative_direction="long",
        ))
        assert out[0].grade == "B"
        assert 45 <= out[0].prediction_score < 70

    def test_all_none_inputs_degrade_to_none_grade(self):
        e = engine()
        # narrative names the side but every scorer is dark -> only the
        # fleet-velocity leg (1 fill = 6.67 -> 7) scores; grade stays none.
        out = e.tick(**tick_kw(fleet_events=[fill("BTC")],
                               narrative_direction="long"))
        assert len(out) == 1 and out[0] is NONE_GRADE
        row = e.journal_rows()[0]
        assert row["grade"] == "none" and row["score"] == 7
        assert row["components"] == {"l4": None, "oi": None, "fleet": 6.667,
                                     "cross": None, "funding": None}

    def test_no_direction_returns_none_verdict(self):
        e = engine()
        # tie vote: l4 long, oi short -> no direction (fail-closed)
        out = e.tick(**tick_kw(fleet_events=[fill("BTC")],
                               l4_signals={"BTC": 0.1},
                               oi_deltas={"BTC": -1.0}))
        assert out == [NONE_GRADE]
        assert e.journal_rows()[0]["reason"] == "no_direction"

    def test_direction_vote_without_narrative(self):
        e = engine()
        # l4 short + oi short vote short; score 25 + 20 + 6.67 -> 52 (B).
        out = e.tick(**tick_kw(fleet_events=[fill("BTC")],
                               l4_signals={"BTC": -0.30},
                               oi_deltas={"BTC": -2.0}))
        assert out[0].direction == "short"
        assert out[0].grade == "B"
        assert out[0].prediction_score == 52

    def test_narrative_mapping_base_name_fallback(self):
        e = engine()
        out = e.tick(**tick_kw(fleet_events=[fill("BTC-USD")],
                               narrative_direction={"BTC": "long"},
                               l4_signals={"BTC-USD": 0.30},
                               oi_deltas={"BTC": 2.0}))
        assert out[0].direction == "long"
        assert out[0].grade == "B"


# ── admission envelope ───────────────────────────────────────────────────────

class TestAdmission:
    def test_max_concurrent_hard_clamped_to_10(self):
        e = engine(scalper_max_concurrent=99)
        events = [stop(f"SYM{i}") for i in range(12)]
        out = e.tick(**tick_kw(fleet_events=events))
        admitted = [g for g in out if g.grade != "none"]
        assert len(admitted) == 10
        rows = e.journal_rows()
        assert sum(1 for r in rows if r["admitted"]) == 10
        assert len(rows) == 12                    # refusals still journaled

    def test_max_concurrent_one(self):
        e = engine(scalper_max_concurrent=1)
        out = e.tick(**tick_kw(fleet_events=[stop("BTC"), stop("ETH")]))
        assert len([g for g in out if g.grade != "none"]) == 1

    def test_open_scalps_count_against_capacity(self):
        e = engine(scalper_max_concurrent=2)
        out = e.tick(**tick_kw(fleet_events=[stop("BTC")],
                               open_scalps=["x", "y"]))
        assert [g for g in out if g.grade != "none"] == []
        assert e.journal_rows()[0]["admitted"] is False

    def test_reversal_slots_count_against_predictive(self):
        e = engine(scalper_max_concurrent=2)
        events = [stop("SOL")] + [fill("BTC")] * 3 + [fill("ETH")] * 3
        out = e.tick(**tick_kw(
            fleet_events=events,
            l4_signals={"BTC": 0.3, "ETH": 0.3},
            oi_deltas={"BTC": 2.0, "ETH": 2.0},
            funding_rates={"BTC": -0.0005, "ETH": -0.0005},
            cross_asset_moves={"COIN": 0.5, "HOOD": 0.5,
                               "LINK": 0.5, "UNI": 0.5, "AAVE": 0.5},
            narrative_direction={"BTC": "long", "ETH": "long"},
        ))
        admitted = [g for g in out if g.grade != "none"]
        assert len(admitted) == 2                  # SOL reversal + one predictive
        assert admitted[0].source == "loss_reversal"
        rows = e.journal_rows()
        eth_row = [r for r in rows if r["symbol"] == "ETH"][0]
        assert eth_row["grade"] == "A" and eth_row["admitted"] is False

    def test_budget_hard_clamps_margin(self):
        e = engine(scalper_budget_usd=5.0, scalper_margin_usd=2.0)
        out = e.tick(**tick_kw(fleet_events=[stop("BTC")],
                               open_scalps=["x", "y"]))   # 2x$2 spent, $1 left
        # capacity: default max_concurrent=10 -> admitted with clamped margin
        g = [g for g in out if g.grade != "none"][0]
        assert g.margin_per_position == pytest.approx(1.0)

    def test_budget_exhausted_zeroes_margin(self):
        # 9 open x $2 = $18 spent of an $18 budget -> margin clamps to 0
        # while capacity (9 < 10) still admits the grade.
        e = engine(scalper_budget_usd=18.0)
        out = e.tick(**tick_kw(fleet_events=[stop("BTC")],
                               open_scalps=["x"] * 9))
        g = [g for g in out if g.grade != "none"][0]
        assert g.margin_per_position == 0.0

    def test_defaults_via_getattr(self):
        e = engine()                                   # no scalper_* attrs set
        out = e.tick(**tick_kw(fleet_events=[stop("BTC")]))
        g = out[0]
        assert g.margin_per_position == 2.0 and g.leverage == 8

    def test_leverage_clamped_to_8(self):
        e = engine(scalper_leverage=50)
        out = e.tick(**tick_kw(fleet_events=[stop("BTC")]))
        assert out[0].leverage == 8


# ── kill switch + fail-closed ────────────────────────────────────────────────

class TestKillSwitch:
    def test_shadow_off_is_bit_for_bit_noop(self):
        e = engine()
        # warm the journal with a live tick
        e.tick(**tick_kw(fleet_events=[stop("BTC")]))
        rows_before = e.journal_rows()
        assert rows_before
        # disabled: empty return, zero state mutation
        e._cfg.scalper_shadow_enabled = False
        out = e.tick(**tick_kw(fleet_events=[stop("ETH"), fill("SOL")],
                               narrative_direction="long"))
        assert out == []
        assert e.journal_rows() == rows_before
        assert e.consecutive_losses("ETH") == 0

    def test_missing_cfg_attr_defaults_enabled(self):
        e = ParasiteScalperEngine(SimpleNamespace(), EcosystemRouter())
        out = e.tick(**tick_kw(fleet_events=[stop("BTC")]))
        assert len(out) == 1

    def test_tick_never_raises_on_garbage(self):
        e = engine()
        assert e.tick(**tick_kw(now="junk")) == []
        assert e.tick(**tick_kw(now=None)) == []
        assert e.tick(**tick_kw(fleet_events=None)) == []
        assert e.tick(**tick_kw(fleet_events=["not", "events", 123])) == []
        # foreign mappings are dug fail-closed, never consulted raw
        out = e.tick(**tick_kw(fleet_events=[fill("BTC")],
                               l4_signals="junk", oi_deltas=42,
                               narrative_direction="long"))
        assert out == [NONE_GRADE]

    def test_internal_fault_returns_empty(self, monkeypatch):
        e = engine()
        monkeypatch.setattr(e._router, "get_family",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError))
        assert e.tick(**tick_kw(fleet_events=[stop("BTC")])) == []

    def test_journal_rows_are_copies(self):
        e = engine()
        e.tick(**tick_kw(fleet_events=[stop("BTC")]))
        rows = e.journal_rows()
        rows[0]["grade"] = "MUTATED"
        assert e.journal_rows()[0]["grade"] == "A"
