"""tests/test_exit_geometry.py — pins for intelligence/exit_geometry.py.

RrShadowCohort: shadow resolution both directions, pairing + JSONL schema,
timeout, eviction, report math, direction normalization, fail-silence.
TrimGate: every branch incl. the 2026-09-17 calibration incidents (false
session-transition trim gated; adverse trim confirmed; structural-break
defer -> recheck -> trim), direction normalization, fail-silence.
"""
import json

import pytest

from intelligence.exit_geometry import (
    MAX_OPEN,
    SHADOW_TIMEOUT_S,
    TRIM_TYPES,
    RrShadowCohort,
    TrimDecision,
    TrimGate,
    normalize_direction,
    rr_cohort,
    rr_shadow_enabled,
    trim_confirm_enabled,
    trim_gate,
)


@pytest.fixture()
def cohort(tmp_path):
    return RrShadowCohort(path=tmp_path / "rr_shadow_cohort.jsonl")


def _read_jsonl(path):
    return [json.loads(line) for line in
            path.read_text().splitlines() if line.strip()]


# ── RrShadowCohort: registration ─────────────────────────────────────────────

class TestRegistration:
    def test_rerung_tp_is_stop_times_rr(self, cohort):
        cohort.on_entry("t1", "HYPE-USD", "long", 50.0, 4.52, 2.40, now=1000.0)
        sh = cohort._open["t1"]
        assert sh["tp_pct_rerung"] == pytest.approx(4.52 * 2.5)
        assert sh["tp_pct_original"] == pytest.approx(2.40)

    def test_custom_rr_target(self, cohort):
        cohort.on_entry("t1", "BTC-USD", "short", 100.0, 2.0, 1.0,
                        rr_target=3.0, now=1000.0)
        assert cohort._open["t1"]["tp_pct_rerung"] == pytest.approx(6.0)

    def test_invalid_inputs_rejected(self, cohort):
        cohort.on_entry("", "BTC-USD", "long", 100.0, 1.0, 1.0)
        cohort.on_entry("t2", "BTC-USD", "long", 0.0, 1.0, 1.0)
        cohort.on_entry("t3", "BTC-USD", "sideways", 100.0, 1.0, 1.0)
        cohort.on_entry("t4", "BTC-USD", "long", 100.0, 0.0, 1.0)
        assert len(cohort._open) == 0

    def test_duplicate_registration_idempotent(self, cohort):
        cohort.on_entry("t1", "BTC-USD", "long", 100.0, 1.0, 1.0, now=1.0)
        cohort.on_entry("t1", "BTC-USD", "long", 200.0, 9.0, 9.0, now=2.0)
        assert cohort._open["t1"]["entry_price"] == 100.0


# ── RrShadowCohort: resolution both directions ───────────────────────────────

class TestResolution:
    def test_long_stop_touch(self, cohort):
        cohort.on_entry("t1", "BTC-USD", "long", 100.0, 2.0, 1.0, now=0.0)
        cohort.on_price("t1", 97.9, now=10.0)   # <= 98.0 stop
        assert cohort._resolved["t1"]["shadow_pnl_pct"] == pytest.approx(-2.0)
        assert "t1" not in cohort._open

    def test_long_tp_touch(self, cohort):
        cohort.on_entry("t1", "BTC-USD", "long", 100.0, 2.0, 1.0, now=0.0)
        cohort.on_price("t1", 105.1, now=10.0)  # >= 105.0 rerung TP
        assert cohort._resolved["t1"]["shadow_pnl_pct"] == pytest.approx(5.0)

    def test_short_stop_touch(self, cohort):
        cohort.on_entry("t1", "BTC-USD", "short", 100.0, 2.0, 1.0, now=0.0)
        cohort.on_price("t1", 102.1, now=10.0)  # >= 102.0 stop
        assert cohort._resolved["t1"]["shadow_pnl_pct"] == pytest.approx(-2.0)

    def test_short_tp_touch(self, cohort):
        cohort.on_entry("t1", "BTC-USD", "short", 100.0, 2.0, 1.0, now=0.0)
        cohort.on_price("t1", 94.9, now=10.0)   # <= 95.0 rerung TP
        assert cohort._resolved["t1"]["shadow_pnl_pct"] == pytest.approx(5.0)

    def test_no_touch_stays_open(self, cohort):
        cohort.on_entry("t1", "BTC-USD", "long", 100.0, 2.0, 1.0, now=0.0)
        cohort.on_price("t1", 101.0, now=10.0)
        assert "t1" in cohort._open
        assert cohort._open["t1"]["last_price"] == 101.0

    def test_timeout_resolves_at_last_price(self, cohort):
        cohort.on_entry("t1", "BTC-USD", "long", 100.0, 2.0, 1.0, now=0.0)
        cohort.on_price("t1", 101.5, now=SHADOW_TIMEOUT_S + 1.0)
        assert cohort._resolved["t1"]["shadow_pnl_pct"] == pytest.approx(1.5)

    def test_timeout_short_sign(self, cohort):
        cohort.on_entry("t1", "BTC-USD", "short", 100.0, 2.0, 1.0, now=0.0)
        cohort.on_price("t1", 101.5, now=SHADOW_TIMEOUT_S + 1.0)
        assert cohort._resolved["t1"]["shadow_pnl_pct"] == pytest.approx(-1.5)

    def test_unknown_trade_id_noop(self, cohort):
        cohort.on_price("ghost", 100.0, now=0.0)  # must not raise
        assert len(cohort._open) == 0

    def test_eviction_oldest(self, cohort):
        for i in range(MAX_OPEN + 5):
            cohort.on_entry(f"t{i}", "BTC-USD", "long", 100.0, 1.0, 1.0,
                            now=float(i))
        assert len(cohort._open) == MAX_OPEN
        assert "t0" not in cohort._open
        assert f"t{MAX_OPEN + 4}" in cohort._open


# ── RrShadowCohort: pairing + JSONL + report ─────────────────────────────────

class TestPairing:
    def test_pair_after_touch_jsonl_schema(self, cohort, tmp_path):
        cohort.on_entry("t1", "HYPE-USD", "long", 50.0, 4.52, 2.40, now=0.0)
        cohort.on_price("t1", 61.4, now=60.0)   # rerung TP touch (+11.3)
        cohort.on_realized("t1", 2.40, now=61.0)
        rows = _read_jsonl(tmp_path / "rr_shadow_cohort.jsonl")
        assert len(rows) == 1
        r = rows[0]
        assert r["trade_id"] == "t1"
        assert r["symbol"] == "HYPE-USD"
        assert r["direction"] == "long"
        assert r["stop_pct"] == pytest.approx(4.52)
        assert r["tp_pct_original"] == pytest.approx(2.40)
        assert r["tp_pct_rerung"] == pytest.approx(11.30)
        assert r["real_pnl_pct"] == pytest.approx(2.40)
        assert r["shadow_pnl_pct"] == pytest.approx(11.30)
        assert r["ev_delta"] == pytest.approx(11.30 - 2.40)
        assert r["ts"] == pytest.approx(61.0)

    def test_pair_resolves_open_shadow_at_last_price(self, cohort, tmp_path):
        cohort.on_entry("t1", "BTC-USD", "short", 100.0, 2.0, 1.0, now=0.0)
        cohort.on_price("t1", 99.0, now=30.0)   # +1.0 in favor, no touch
        cohort.on_realized("t1", 0.5, now=40.0)
        rows = _read_jsonl(tmp_path / "rr_shadow_cohort.jsonl")
        assert rows[0]["shadow_pnl_pct"] == pytest.approx(1.0)
        assert rows[0]["ev_delta"] == pytest.approx(0.5)
        assert len(cohort._open) == 0 and len(cohort._resolved) == 0

    def test_on_realized_unknown_id_noop(self, cohort, tmp_path):
        cohort.on_realized("ghost", 1.0)
        assert not (tmp_path / "rr_shadow_cohort.jsonl").exists()

    def test_on_realized_bad_pnl_noop(self, cohort):
        cohort.on_entry("t1", "BTC-USD", "long", 100.0, 1.0, 1.0, now=0.0)
        cohort.on_realized("t1", "not-a-number")
        assert "t1" in cohort._open  # untouched

    def test_report_math(self, cohort):
        cohort.on_entry("w", "A", "long", 100.0, 1.0, 1.0, now=0.0)
        cohort.on_price("w", 102.6, now=1.0)    # shadow +2.5 (TP touch)
        cohort.on_realized("w", 1.0, now=2.0)
        cohort.on_entry("l", "B", "long", 100.0, 1.0, 1.0, now=0.0)
        cohort.on_price("l", 98.9, now=1.0)     # shadow -1.0 (stop touch)
        cohort.on_realized("l", -1.0, now=2.0)
        rep = cohort.report()
        assert rep["n"] == 2
        assert rep["mean_real"] == pytest.approx(0.0)
        assert rep["mean_shadow"] == pytest.approx(0.75)
        assert rep["mean_delta"] == pytest.approx(0.75)
        assert rep["win_rate_real"] == pytest.approx(0.5)
        assert rep["win_rate_shadow"] == pytest.approx(0.5)

    def test_report_empty(self, cohort):
        rep = cohort.report()
        assert rep["n"] == 0
        assert rep["mean_real"] == 0.0

    def test_jsonl_append_two_pairs(self, cohort, tmp_path):
        for i in range(2):
            cohort.on_entry(f"t{i}", "BTC-USD", "long", 100.0, 1.0, 1.0,
                            now=0.0)
            cohort.on_realized(f"t{i}", float(i), now=1.0)
        assert len(_read_jsonl(tmp_path / "rr_shadow_cohort.jsonl")) == 2


# ── Direction normalization ──────────────────────────────────────────────────

class TestDirectionNormalization:
    @pytest.mark.parametrize("raw,expected", [
        ("long", "long"), ("LONG", "long"), ("BUY", "long"), ("buy", "long"),
        ("short", "short"), ("Short", "short"), ("SELL", "short"),
        ("s", "short"), ("b", "long"),
        ("", None), (None, None), ("flat", None),
    ])
    def test_normalize(self, raw, expected):
        assert normalize_direction(raw) == expected

    def test_cohort_accepts_order_side_spelling(self, cohort):
        cohort.on_entry("t1", "BTC-USD", "BUY", 100.0, 1.0, 1.0, now=0.0)
        assert cohort._open["t1"]["direction"] == "long"
        cohort.on_price("t1", 102.6, now=1.0)
        assert cohort._resolved["t1"]["shadow_pnl_pct"] == pytest.approx(2.5)


# ── TrimGate: calibration incidents (pinned) ─────────────────────────────────

class TestTrimGateCalibration:
    """The two 2026-09-17 incidents, pinned as arithmetic."""

    def test_false_trim_blocked(self):
        """pnl +1.2%, atr 4.5, session_transition: threshold 1.35% ->
        price has NOT moved adversely -> BLOCK (today's false trim gated)."""
        g = TrimGate()
        d = g.evaluate("session_transition", "pos1", "long",
                       pnl_pct_vs_entry=1.2, atr_pct=4.5, now=1000.0)
        assert d is TrimDecision.BLOCK

    def test_adverse_trim_confirmed(self):
        """pnl -2.0% <= -1.35% -> TRIM."""
        g = TrimGate()
        d = g.evaluate("session_transition", "pos1", "long",
                       pnl_pct_vs_entry=-2.0, atr_pct=4.5, now=1000.0)
        assert d is TrimDecision.TRIM

    def test_structural_break_defers_then_trims(self):
        """atr 4.5 * 0.15 = 0.675% bar; pnl -0.5% unconfirmed -> DEFER;
        recheck at -0.5% (still unconfirmed) -> TRIM (window expired)."""
        g = TrimGate()
        d1 = g.evaluate("coherence_structural_break", "pos1", "long",
                        pnl_pct_vs_entry=-0.5, atr_pct=4.5, now=1000.0)
        assert d1 is TrimDecision.DEFER
        assert not g.recheck_due("pos1", now=1000.0 + 1799.0)
        assert g.recheck_due("pos1", now=1000.0 + 1800.0)
        d2 = g.evaluate("coherence_structural_break", "pos1", "long",
                        pnl_pct_vs_entry=-0.5, atr_pct=4.5,
                        now=1000.0 + 1800.0)
        assert d2 is TrimDecision.TRIM
        assert not g.recheck_due("pos1", now=1000.0 + 1801.0)  # consumed


# ── TrimGate: every branch ───────────────────────────────────────────────────

class TestTrimGateBranches:
    def test_hard_stop_cascade_never_confirms(self):
        g = TrimGate()
        d = g.evaluate("hard_stop_cascade", "pos1", "short",
                       pnl_pct_vs_entry=5.0, atr_pct=4.5, now=0.0)
        assert d is TrimDecision.TRIM

    def test_unknown_trim_type_fail_open(self):
        g = TrimGate()
        assert g.evaluate("brand_new_reason", "p", "long", 1.0, 4.5,
                          now=0.0) is TrimDecision.TRIM

    def test_boundary_exactly_at_threshold_trims(self):
        g = TrimGate()
        # threshold 4.5*0.30 = 1.35; pnl exactly -1.35 confirms
        assert g.evaluate("session_transition", "p", "long", -1.35, 4.5,
                          now=0.0) is TrimDecision.TRIM

    def test_defer_idempotent_single_schedule(self):
        g = TrimGate()
        g.evaluate("coherence_structural_break", "p", "long", -0.5, 4.5,
                   now=1000.0)
        first = g._deferred["p"]
        g.evaluate("coherence_structural_break", "p", "long", -0.4, 4.5,
                   now=1100.0)
        assert g._deferred["p"] == first  # not rescheduled

    def test_confirmed_trim_clears_deferral(self):
        g = TrimGate()
        g.evaluate("coherence_structural_break", "p", "long", -0.5, 4.5,
                   now=1000.0)
        assert "p" in g._deferred
        d = g.evaluate("coherence_structural_break", "p", "long",
                       -1.0, 4.5, now=1100.0)
        assert d is TrimDecision.TRIM
        assert "p" not in g._deferred

    def test_recheck_confirmed_also_trims(self):
        g = TrimGate()
        g.evaluate("coherence_structural_break", "p", "long", -0.5, 4.5,
                   now=0.0)
        d = g.evaluate("coherence_structural_break", "p", "long",
                       -2.0, 4.5, now=1800.0)
        assert d is TrimDecision.TRIM

    def test_recheck_due_unknown_position(self):
        assert TrimGate().recheck_due("ghost", now=0.0) is False

    def test_forget_clears_deferral(self):
        g = TrimGate()
        g.evaluate("coherence_structural_break", "p", "long", -0.5, 4.5,
                   now=0.0)
        g.forget("p")
        assert g.recheck_due("p", now=99999.0) is False

    def test_degenerate_atr_fail_open(self):
        g = TrimGate()
        assert g.evaluate("session_transition", "p", "long", 1.0, 0.0,
                          now=0.0) is TrimDecision.TRIM

    def test_invalid_inputs_fail_open(self):
        g = TrimGate()
        assert g.evaluate("session_transition", "p", "long",
                          "bad", 4.5, now=0.0) is TrimDecision.TRIM

    def test_direction_spelling_accepted(self):
        g = TrimGate()
        assert g.evaluate("session_transition", "p", "SELL", 1.2, 4.5,
                          now=0.0) is TrimDecision.BLOCK

    def test_trim_types_table_shape(self):
        assert TRIM_TYPES["session_transition"]["confirm_fraction"] == 0.30
        assert (TRIM_TYPES["session_transition"]["on_unconfirmed"]
                is TrimDecision.BLOCK)
        assert TRIM_TYPES["coherence_structural_break"]["defer_s"] == 1800.0
        assert TRIM_TYPES["hard_stop_cascade"]["confirm_required"] is False


# ── Fail-silence + kill switches + singletons ────────────────────────────────

class TestFailSilence:
    def test_cohort_methods_swallow_garbage(self, cohort):
        cohort.on_entry(None, None, None, None, None, None)
        cohort.on_price(None, None)
        cohort.on_realized(None, None)
        assert cohort.report()["n"] == 0

    def test_gate_evaluate_never_raises(self):
        g = TrimGate()
        assert g.evaluate(None, None, None, None, None) is TrimDecision.TRIM

    def test_kill_switch_helpers_return_bools(self):
        assert isinstance(rr_shadow_enabled(), bool)
        assert isinstance(trim_confirm_enabled(), bool)

    def test_singletons_stable(self):
        assert rr_cohort() is rr_cohort()
        assert trim_gate() is trim_gate()
