"""tests/test_pair_engine.py — pins for the 2026-09-28 Ratio Machine brain
(Governor TASK #8: "The netting problem is the hardest constraint").

  B1  size-delta ledger: status machine, conflict matrix, exit math,
      post-close verify, jsonl round-trip
  B2  PairPool debit/credit/rebuild + envelope preflight
  B4  mode-selector boundaries (ratio edges, whale age, band edges)
  B5  exit evaluator (E1 tol, E2 ratchet, E3 half-then-trail, correlation
      break + 28800s lockout)
  B7  sizing doctrine (step-up gates, auto-down, $280/$20 invariants)
  B8  AbortTracker 25% boundary + one-bad-line rebuild

cfg() SimpleNamespace harness per tests/test_anticipator_resilience.py.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.pair_engine import (  # noqa: E402
    ABORT_HALT_RATE,
    BAND_HI,
    BAND_LO,
    IllegalTransitionError,
    LedgerStore,
    MAX_TOTAL_MARGIN_USD,
    PairLedgerNetted,
    PairLeg,
    PairMode,
    PairPool,
    PairStatus,
    RATIO_CENTER,
    RESERVE_USD,
    VALID_HI,
    VALID_LO,
    AbortTracker,
    compute_ratio,
    detect_conflict,
    evaluate_exit,
    exit_size_for_leg,
    reserve_ok,
    select_mode,
    session_boundary_verdict,
    verify_post_close,
    within_margin_invariant,
)

NOW = 1_800_000_600.0


def cfg(**over):
    base = dict(
        pair_engine_enabled=True,
        pair_engine_pool_usd=90.0,          # Governor 2026-09-28 envelope
        pair_engine_leg_margin_usd=45.0,    # Governor 2026-09-28 session-fixed
    )
    base.update(over)
    return SimpleNamespace(**base)


def _ledger(mode=PairMode.E1_BAND, status=PairStatus.LIVE,
            trail_anchor=None, e3_half_done=False):
    led = PairLedgerNetted(
        mode=mode,
        leg_a=PairLeg(symbol="USTECH100-USD", direction="long",
                      margin=45.0, leverage=25, notional=1125.0,
                      size_before=0.0, size_placed=0.037,
                      size_expected=0.037, entry_price=30208.0, filled=True),
        leg_b=PairLeg(symbol="US500-USD", direction="short",
                      margin=45.0, leverage=20, notional=900.0,
                      size_before=0.0, size_placed=-0.117,
                      size_expected=-0.117, entry_price=7682.7, filled=True),
        trail_anchor=trail_anchor, e3_half_done=e3_half_done,
        created_ts=NOW)
    led.status = status
    return led


# ── B1 Layer 2: conflict matrix (SAFE/ADDITIVE/CONFLICT x resting/live) ──

class TestConflictMatrix:
    def test_safe_no_live_net_no_resting(self):
        v = detect_conflict("US500-USD", "long", 0.0, [])
        assert v.verdict == "SAFE" and v.size_before_hint == 0.0
        assert v.resting_context == 0

    def test_safe_with_resting_orders_context_only(self):
        # Resting fleet orders do not drive the verdict — the LIVE net does.
        resting = [{"side": "short"}, {"side": "short"}]
        v = detect_conflict("US500-USD", "long", 0.0, resting)
        assert v.verdict == "SAFE" and v.resting_context == 2

    def test_additive_same_direction_long(self):
        v = detect_conflict("USTECH100-USD", "long", 0.05, [])
        assert v.verdict == "ADDITIVE"
        # ADDITIVE records the fleet net as the size_before context.
        assert v.size_before_hint == 0.05

    def test_additive_same_direction_short_with_resting(self):
        v = detect_conflict("USTECH100-USD", "short", -0.05,
                            [{"side": "short"}])
        assert v.verdict == "ADDITIVE"
        assert v.size_before_hint == -0.05
        assert v.resting_context == 1

    def test_conflict_opposite_direction_live(self):
        assert detect_conflict("US500-USD", "short", 0.12, []).verdict == "CONFLICT"
        assert detect_conflict("US500-USD", "long", -0.12, []).verdict == "CONFLICT"

    def test_conflict_hint_zeroed(self):
        # CONFLICT carries no size_before context (caller skips outright).
        v = detect_conflict("US500-USD", "short", 0.12, [])
        assert v.size_before_hint == 0.0

    def test_fail_closed_on_degenerate_inputs(self):
        # Unknown direction or unknown fleet net -> CONFLICT, never place
        # blind into the fleet; never raises.
        assert detect_conflict("S", "sideways", 0.0, []).verdict == "CONFLICT"
        assert detect_conflict("S", "long", None, []).verdict == "CONFLICT"
        assert detect_conflict("S", None, 0.0, []).verdict == "CONFLICT"

    def test_buy_sell_aliases_normalize(self):
        assert detect_conflict("S", "buy", 0.1, []).verdict == "ADDITIVE"
        assert detect_conflict("S", "sell", 0.1, []).verdict == "CONFLICT"


# ── B4: mode-selector boundaries ──────────────────────────────────────────

class TestModeSelectorBoundaries:
    def test_ratio_379_no_trade(self):
        assert select_mode(3.79, 0.02, 0.5, 0.1, 100.0) == "NO_TRADE"

    def test_ratio_380_valid_edge(self):
        # Exactly 3.80 is inside the valid range; below the band -> E3.
        assert select_mode(VALID_LO, 0.02, 0.5, 0.1, 100.0) == "E3_TRAP"

    def test_ratio_410_valid_edge(self):
        assert select_mode(VALID_HI, 0.02, 0.5, 0.1, 100.0) == "E3_TRAP"

    def test_ratio_411_no_trade(self):
        assert select_mode(4.11, 0.02, 0.5, 0.1, 100.0) == "NO_TRADE"

    def test_whale_age_1799_ok(self):
        assert select_mode(RATIO_CENTER, 0.02, 0.5, 0.1, 1799.0) == "E1_BAND"

    def test_whale_age_1801_no_trade(self):
        assert select_mode(RATIO_CENTER, 0.02, 0.5, 0.1, 1801.0) == "NO_TRADE"

    def test_whale_age_1800_boundary_ok(self):
        # Governor 2026-09-28: stale is STRICTLY greater than 1800s.
        assert select_mode(RATIO_CENTER, 0.02, 0.5, 0.1, 1800.0) == "E1_BAND"

    def test_band_edge_3896_e3(self):
        assert select_mode(BAND_LO, 0.02, 0.5, 0.1, 100.0) == "E3_TRAP"

    def test_band_edge_3974_e3(self):
        assert select_mode(BAND_HI, 0.02, 0.5, 0.1, 100.0) == "E3_TRAP"

    def test_center_3935_e1(self):
        assert select_mode(RATIO_CENTER, 0.02, 0.5, 0.1, 100.0) == "E1_BAND"

    def test_e2_whale_confirmed_trend(self):
        assert select_mode(3.93, 0.05, 0.72, 0.7, 100.0) == "E2_AMP"
        assert select_mode(3.93, 0.05, 0.28, -0.7, 100.0) == "E2_AMP"

    def test_e2_needs_all_three_confirmations(self):
        # Weak body -> falls back to E1 even with strong whale share/range.
        assert select_mode(3.93, 0.05, 0.72, 0.3, 100.0) == "E1_BAND"
        # Flat range -> E1.
        assert select_mode(3.93, 0.01, 0.72, 0.7, 100.0) == "E1_BAND"
        # Neutral share -> E1.
        assert select_mode(3.93, 0.05, 0.5, 0.7, 100.0) == "E1_BAND"

    def test_degenerate_inputs_no_trade(self):
        assert select_mode(None, 0.02, 0.5, 0.1, 100.0) == "NO_TRADE"
        assert select_mode(3.93, 0.02, 0.5, 0.1, None) == "NO_TRADE"
        assert select_mode(0.0, 0.02, 0.5, 0.1, 100.0) == "NO_TRADE"

    def test_compute_ratio_contract(self):
        assert compute_ratio(30208.0, 7682.7) == pytest.approx(3.932, abs=1e-3)
        assert compute_ratio(None, 7682.7) is None
        assert compute_ratio(30208.0, 0.0) is None
        assert compute_ratio(-1.0, 7682.7) is None


# ── B1: status machine — legal chain passes, every illegal raises ────────

class TestStatusMachine:
    def test_legal_chain_to_closed(self):
        led = _ledger(status=PairStatus.INTENT)
        for nxt in (PairStatus.LEG_A_PENDING, PairStatus.BOTH_PENDING,
                    PairStatus.LIVE, PairStatus.CLOSING, PairStatus.CLOSED):
            led.transition(nxt)
        assert led.status == PairStatus.CLOSED

    def test_abort_legal_from_every_open_state(self):
        for st in (PairStatus.INTENT, PairStatus.LEG_A_PENDING,
                   PairStatus.BOTH_PENDING, PairStatus.LIVE,
                   PairStatus.CLOSING):
            led = _ledger(status=st)
            led.transition(PairStatus.ABORTED)
            assert led.status == PairStatus.ABORTED

    @pytest.mark.parametrize("src,dst", [
        (PairStatus.INTENT, PairStatus.BOTH_PENDING),
        (PairStatus.INTENT, PairStatus.LIVE),
        (PairStatus.INTENT, PairStatus.CLOSING),
        (PairStatus.INTENT, PairStatus.CLOSED),
        (PairStatus.LEG_A_PENDING, PairStatus.LIVE),
        (PairStatus.LEG_A_PENDING, PairStatus.CLOSED),
        (PairStatus.BOTH_PENDING, PairStatus.CLOSING),
        (PairStatus.BOTH_PENDING, PairStatus.CLOSED),
        (PairStatus.LIVE, PairStatus.CLOSED),
        (PairStatus.CLOSING, PairStatus.LIVE),
        (PairStatus.CLOSED, PairStatus.ABORTED),
        (PairStatus.CLOSED, PairStatus.LIVE),
        (PairStatus.ABORTED, PairStatus.INTENT),
        (PairStatus.ABORTED, PairStatus.LIVE),
    ])
    def test_illegal_transitions_raise(self, src, dst):
        led = _ledger(status=src)
        with pytest.raises(IllegalTransitionError):
            led.transition(dst)


# ── B1: size-delta exit math + post-close verify ─────────────────────────

class TestExitSizeMath:
    def test_exact_match_no_drift(self):
        p = exit_size_for_leg(0.037, 0.037)
        assert p.exit_size == pytest.approx(0.037) and p.drift is False
        assert p.reason == "ok"

    def test_live_less_than_placed_min_plus_drift(self):
        p = exit_size_for_leg(0.030, 0.037)
        assert p.exit_size == pytest.approx(0.030) and p.drift is True
        assert p.reason == "drift"

    def test_live_greater_than_placed_placed_plus_drift(self):
        p = exit_size_for_leg(0.050, 0.037)
        assert p.exit_size == pytest.approx(0.037) and p.drift is True

    def test_short_leg_signed(self):
        p = exit_size_for_leg(-0.100, -0.117)
        assert p.exit_size == pytest.approx(0.100) and p.drift is True
        p2 = exit_size_for_leg(-0.117, -0.117)
        assert p2.exit_size == pytest.approx(0.117) and p2.drift is False

    def test_sign_flip_is_drift_zero_exit(self):
        p = exit_size_for_leg(0.037, -0.117)
        assert p.exit_size == 0.0 and p.drift is True
        assert p.reason == "sign_flip"

    def test_degenerate_abstains_never_raises(self):
        assert exit_size_for_leg(None, 0.037).exit_size == 0.0
        assert exit_size_for_leg(0.037, None).exit_size == 0.0
        assert exit_size_for_leg(0.037, 0.0).exit_size == 0.0

    def test_post_close_verify(self):
        assert verify_post_close(0.05, 0.05) is True
        assert verify_post_close(0.06, 0.05) is False
        assert verify_post_close(None, 0.05) is False


# ── B5: exit evaluator ────────────────────────────────────────────────────

class TestExitEvaluator:
    def test_e1_tol_boundary_0009_closes(self):
        v = evaluate_exit(_ledger(), RATIO_CENTER + 0.009, 0.3, NOW)
        assert v.action == "close_both" and v.reason == "e1_convergence"
        assert v.ustech_first is True

    def test_e1_tol_boundary_0011_holds(self):
        v = evaluate_exit(_ledger(), RATIO_CENTER + 0.011, 0.3, NOW)
        assert v.action == "hold"

    def test_e1_exact_tol_closes(self):
        v = evaluate_exit(_ledger(), RATIO_CENTER - 0.010, 0.0, NOW)
        assert v.action == "close_both"

    def test_e2_ratchet_only_tightens(self):
        led = _ledger(mode=PairMode.E2_AMP)
        v1 = evaluate_exit(led, 3.95, 1.2, NOW)      # anchor -> 1.2
        assert v1.action == "hold" and led.trail_anchor == 1.2
        v2 = evaluate_exit(led, 3.95, 0.9, NOW)      # retrace 0.3 < 0.5
        assert v2.action == "hold" and led.trail_anchor == 1.2  # no loosen
        v3 = evaluate_exit(led, 3.95, 1.8, NOW)      # anchor -> 1.8
        assert v3.action == "hold" and led.trail_anchor == 1.8
        v4 = evaluate_exit(led, 3.95, 1.2, NOW)      # retrace 0.6 > 0.5
        assert v4.action == "close_both" and v4.reason == "e2_trail"

    def test_e3_half_then_trail_sequence(self):
        led = _ledger(mode=PairMode.E3_TRAP)
        v0 = evaluate_exit(led, 3.98, 1.2, NOW)      # below 1.5 trigger
        assert v0.action == "hold" and led.e3_half_done is False
        v1 = evaluate_exit(led, 3.98, 1.6, NOW)      # bank 50%
        assert v1.action == "close_half" and v1.close_fraction == 0.5
        assert led.e3_half_done is True and led.trail_anchor == 1.6
        v2 = evaluate_exit(led, 3.98, 2.2, NOW)      # anchor ratchets
        assert v2.action == "hold" and led.trail_anchor == 2.2
        v3 = evaluate_exit(led, 3.98, 1.6, NOW)      # retrace 0.6 > 0.5
        assert v3.action == "close_both" and v3.reason == "e3_trail"

    def test_correlation_break_low_lockout_28800(self):
        v = evaluate_exit(_ledger(), 3.79, 0.0, NOW)
        assert v.action == "close_both" and v.reason == "correlation_break"
        assert v.lockout_until == NOW + 28800.0

    def test_correlation_break_high_lockout_28800(self):
        v = evaluate_exit(_ledger(mode=PairMode.E2_AMP), 4.11, 3.0, NOW)
        assert v.action == "close_both" and v.reason == "correlation_break"
        assert v.lockout_until == NOW + 28800.0

    def test_correlation_break_outranks_e1_convergence(self):
        # E1 would hold at center ± tol; the break check fires first.
        v = evaluate_exit(_ledger(), VALID_LO - 0.001, 0.0, NOW)
        assert v.reason == "correlation_break"

    def test_degenerate_abstains_hold(self):
        assert evaluate_exit(None, 3.93, 0.0, NOW).action == "hold"
        assert evaluate_exit(_ledger(), None, 0.0, NOW).action == "hold"
        assert evaluate_exit(_ledger(), 0.0, 0.0, NOW).action == "hold"
        assert evaluate_exit(_ledger(), 3.93, 0.0, None).action == "hold"


# ── B8: AbortTracker 25% boundary ─────────────────────────────────────────

class TestAbortTracker:
    def test_5_of_20_exactly_025_not_halted(self):
        t = AbortTracker()
        for _ in range(5):
            t.record(True)
        for _ in range(15):
            t.record(False)
        assert t.abort_rate == pytest.approx(0.25)
        assert ABORT_HALT_RATE == 0.25
        assert t.halted is False

    def test_6_of_20_halted(self):
        t = AbortTracker()
        for _ in range(6):
            t.record(True)
        for _ in range(14):
            t.record(False)
        assert t.abort_rate == pytest.approx(0.30)
        assert t.halted is True

    def test_window_slides_over_20(self):
        t = AbortTracker()
        for _ in range(20):
            t.record(True)
        assert t.abort_rate == pytest.approx(1.0)
        for _ in range(20):
            t.record(False)
        assert t.abort_rate == pytest.approx(0.0)
        assert t.halted is False

    def test_empty_not_halted(self):
        assert AbortTracker().halted is False


# ── B2: PairPool debit/credit/rebuild + envelope preflight ───────────────

class TestPairPool:
    def test_debit_credit_rebuild_round_trip(self):
        p = PairPool()
        assert p.debited == 0.0
        p.debit(45.0)
        p.debit(45.0)
        assert p.debited == 90.0
        p.credit(45.0)
        assert p.debited == 45.0
        p.credit(99.0)                      # credit never drives negative
        assert p.debited == 0.0
        p.rebuild(90.0)                     # boot: two legs still open
        assert p.debited == 90.0

    def test_preflight_45x2_le_90_passes(self):
        p = PairPool()
        assert p.preflight(cfg(), 45.0) is True

    def test_preflight_46x2_le_90_fails(self):
        p = PairPool()
        assert p.preflight(cfg(), 46.0) is False

    def test_preflight_accounts_for_debited(self):
        p = PairPool()
        p.debit(45.0)
        assert p.preflight(cfg(), 45.0) is False   # 45 + 90 > 90
        assert p.preflight(cfg(), 22.5) is True    # 45 + 45 <= 90

    def test_preflight_degenerate_fail_closed(self):
        assert PairPool().preflight(cfg(), 0.0) is False
        assert PairPool().preflight(cfg(), -5.0) is False


# ── B1/B8: LedgerStore jsonl round-trip + one-bad-line survival ──────────

class TestLedgerStore:
    def test_round_trip(self, tmp_path):
        path = str(tmp_path / "pair_ledger.jsonl")
        store = LedgerStore(path)
        led = _ledger(status=PairStatus.CLOSED)
        led.trail_anchor = 1.8
        assert store.append(led) is True
        back = store.rebuild_from_jsonl()
        assert len(back) == 1
        r = back[0]
        assert r.pair_id == led.pair_id and r.mode == led.mode
        assert r.status == PairStatus.CLOSED
        assert r.trail_anchor == 1.8
        assert r.leg_a.size_placed == pytest.approx(0.037)
        assert r.leg_b.direction == "short"

    def test_one_bad_line_kills_one_record_never_the_boot(self, tmp_path):
        path = str(tmp_path / "pair_ledger.jsonl")
        store = LedgerStore(path)
        good1, good2 = _ledger(), _ledger()
        with open(path, "w") as fh:
            fh.write(json.dumps(good1.to_dict()) + "\n")
            fh.write("{not json at all\n")
            fh.write(json.dumps({"mode": "BANANA"}) + "\n")
            fh.write(json.dumps(good2.to_dict()) + "\n")
        back = store.rebuild_from_jsonl()
        assert [r.pair_id for r in back] == [good1.pair_id, good2.pair_id]

    def test_missing_file_is_empty_boot(self, tmp_path):
        store = LedgerStore(str(tmp_path / "nope.jsonl"))
        assert store.rebuild_from_jsonl() == []


# ── B7: sizing doctrine (Governor Decision-3) ─────────────────────────────

class TestSizingDoctrine:
    _OK = dict(fill_rate=0.08, session_net_usd=3.0, correlation_break=False,
               operator_confirmed=True, max_open_dd_pct=5.0,
               current_leg_margin_usd=45.0)

    def test_step_up_all_gates_pass(self):
        v = session_boundary_verdict(**self._OK)
        assert v.action == "step_up"
        assert v.new_leg_margin_usd == pytest.approx(45.0 * 1.25)

    @pytest.mark.parametrize("over,frag", [
        (dict(fill_rate=0.049), "fill_rate"),            # < 5% blocks
        (dict(session_net_usd=-2.01), "session_net"),    # < -$2 blocks
        (dict(correlation_break=True), "no_correlation_break"),
        (dict(operator_confirmed=False), "operator_confirmed"),
        (dict(max_open_dd_pct=20.1), "open_dd"),         # > 20% blocks
    ])
    def test_each_failing_gate_blocks_step_up(self, over, frag):
        kw = dict(self._OK); kw.update(over)
        v = session_boundary_verdict(**kw)
        assert v.action == "hold" and frag in v.reason
        assert v.new_leg_margin_usd == 45.0

    def test_auto_down_on_session_net_below_minus5(self):
        kw = dict(self._OK); kw.update(session_net_usd=-5.01)
        v = session_boundary_verdict(**kw)
        assert v.action == "step_down"
        assert v.new_leg_margin_usd == pytest.approx(45.0 * 0.80)

    def test_auto_down_on_fill_rate_below_2pct(self):
        kw = dict(self._OK); kw.update(fill_rate=0.019)
        v = session_boundary_verdict(**kw)
        assert v.action == "step_down" and v.reason == "fill_rate_below_2pct"

    def test_auto_down_needs_no_operator_confirmation(self):
        kw = dict(self._OK); kw.update(session_net_usd=-6.0,
                                       operator_confirmed=False)
        assert session_boundary_verdict(**kw).action == "step_down"

    def test_auto_down_outranks_step_up_gates(self):
        # All step-up gates green BUT session net < -$5 -> step down.
        kw = dict(self._OK); kw.update(session_net_usd=-5.01, fill_rate=0.50)
        assert session_boundary_verdict(**kw).action == "step_down"

    def test_step_up_clamped_by_280_invariant(self):
        kw = dict(self._OK); kw.update(current_leg_margin_usd=130.0)
        v = session_boundary_verdict(**kw)
        # 130 x 1.25 = 162.5 -> clamped to 140 (two legs <= $280).
        assert v.new_leg_margin_usd == pytest.approx(140.0)
        assert within_margin_invariant(2 * v.new_leg_margin_usd) is True

    def test_degenerate_stats_hold(self):
        v = session_boundary_verdict(
            fill_rate=None, session_net_usd=0.0, correlation_break=False,
            operator_confirmed=True, max_open_dd_pct=0.0,
            current_leg_margin_usd=45.0)
        assert v.action == "hold" and v.reason == "degenerate"

    def test_margin_invariant_280(self):
        assert within_margin_invariant(MAX_TOTAL_MARGIN_USD) is True
        assert within_margin_invariant(MAX_TOTAL_MARGIN_USD + 0.01) is False
        assert within_margin_invariant(None) is False

    def test_reserve_20_untouchable(self):
        assert reserve_ok(RESERVE_USD) is True
        assert reserve_ok(RESERVE_USD - 0.01) is False
        assert reserve_ok(None) is False
