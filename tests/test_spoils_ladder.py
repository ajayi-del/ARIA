"""Spoils-ladder pins (2026-10-22 partial-profit banking brain).

Governor directives bound here:
  "only campaign trades or trades above 200USD SO WE DONT BANK DUST" —
  SUPERSEDED by msg-992 (2026-10-22): "ensure the fix works for all trades
  not only 200usd trades" — eligibility gate is now notional >= $30 (rung1
  banks 1/3, so the smallest bank leg = $10 = the SoDEX close floor: dust-
  proof by construction) OR a campaign/fast_cycle/anticipator tag.
  "SOME PARTS NEED TO BE DYNAMIC" — rungs are ATR-scaled with clamps
  (k1 × atr_pct × leverage, clamped [rung_lo, rung_hi]; rung2 = 2×rung1),
  floor arms after rung1 at floor_frac × the highest rung hit, tighten-only.

Kill switch SPOILS_LADDER_ENABLED (default true): OFF = pre-module system —
no registration, no verdicts, no state. Read at call time, pinned below.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.spoils_ladder import (  # noqa: E402
    DEFAULT_CAMPAIGN_TAGS, STATIC_RUNG1_ROE, STATIC_RUNG2_ROE, SpoilsConfig,
    SpoilsTracker, compute_rungs, spoils_ladder_enabled)


def _cfg(**kw):
    return SpoilsConfig(**kw)


def _tracker(monkeypatch, tide_fn=None, **cfg_kw):
    monkeypatch.delenv("SPOILS_LADDER_ENABLED", raising=False)
    return SpoilsTracker(_cfg(**cfg_kw), tide_fn=tide_fn)


def _mark_for_roe(entry, roe_pct, leverage=1.0, side="long"):
    """Mark price that yields exactly roe_pct at the given leverage."""
    move = roe_pct / (100.0 * leverage)
    return entry * (1.0 + move) if side == "long" else entry * (1.0 - move)


class TestKillSwitch:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("SPOILS_LADDER_ENABLED", raising=False)
        assert spoils_ladder_enabled() is True

    def test_explicit_off(self, monkeypatch):
        monkeypatch.setenv("SPOILS_LADDER_ENABLED", "false")
        assert spoils_ladder_enabled() is False

    def test_env_read_at_call_time(self, monkeypatch):
        monkeypatch.setenv("SPOILS_LADDER_ENABLED", "false")
        assert spoils_ladder_enabled() is False
        monkeypatch.setenv("SPOILS_LADDER_ENABLED", "true")
        assert spoils_ladder_enabled() is True

    def test_off_blocks_registration_and_verdicts(self, monkeypatch):
        # OFF state reproduces the pre-module system exactly: nothing
        # registers, no state accrues, marks abstain.
        monkeypatch.setenv("SPOILS_LADDER_ENABLED", "false")
        tr = SpoilsTracker(_cfg())
        assert tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 1, 3.0) is False
        assert tr.state_snapshot()["n_tracked"] == 0
        v = tr.on_mark("p1", 110.0, now_s=1.0)
        assert v.action == "none"


class TestEligibility:
    def test_dust_excluded(self, monkeypatch):
        # $20 notional, no campaign tag — sub-$30 is dust (rung1 leg < venue
        # close floor); msg-992 lowered the floor 200->30, not to zero.
        tr = _tracker(monkeypatch)
        assert tr.on_open("p1", "SOL-USD", "long", 0.2, 100.0, 1, 3.0) is False

    def test_notional_included(self, monkeypatch):
        # $312 clears the $30 floor with no tag.
        tr = _tracker(monkeypatch)
        assert tr.on_open("p1", "SOL-USD", "long", 3.12, 100.0, 1, 3.0) is True

    def test_arb_class_included_post_msg992(self, monkeypatch):
        # The msg-992 wound: ARB ~$177 notional was excluded by the $200
        # floor and rode unbanked to -$11.07. $177 now registers.
        tr = _tracker(monkeypatch)
        assert tr.on_open("p1", "ARB-USD", "long", 931.1, 0.19, 1, 3.0) is True

    def test_campaign_tag_included_at_dust(self, monkeypatch):
        # Campaign family banks at ANY size ($37).
        tr = _tracker(monkeypatch)
        assert tr.on_open("p1", "SPCX-USD", "long", 0.37, 100.0, 1, 3.0,
                          tags=frozenset({"campaign"})) is True

    def test_family_tags_included_at_dust(self, monkeypatch):
        tr = _tracker(monkeypatch)
        for tag in ("fast_cycle", "anticipator"):
            assert tr.on_open(f"p_{tag}", "BTC-USD", "long", 0.37, 100.0,
                              1, 3.0, tags=frozenset({tag})) is True
        assert DEFAULT_CAMPAIGN_TAGS == frozenset(
            {"campaign", "fast_cycle", "anticipator"})

    def test_none_tags_treated_empty(self, monkeypatch):
        tr = _tracker(monkeypatch)
        assert tr.on_open("p1", "SOL-USD", "long", 0.2, 100.0, 1, 3.0,
                          tags=None) is False


class TestRungComputation:
    def test_mid_atr_no_clamp(self):
        # k1 × atr × lev = 1 × 3 × 1 = 3.0 inside [2, 6] → unclamped.
        assert compute_rungs(3.0, 1.0, 1.0, 2.0, 6.0) == (3.0, 6.0)

    def test_clamp_low(self):
        # raw 0.5 → floored at rung_lo; rung2 = 2× the CLAMPED rung1.
        assert compute_rungs(0.5, 1.0, 1.0, 2.0, 6.0) == (2.0, 4.0)

    def test_clamp_high(self):
        # raw 5 × 3 = 15 → capped at rung_hi; rung2 = 12.
        assert compute_rungs(5.0, 3.0, 1.0, 2.0, 6.0) == (6.0, 12.0)

    def test_atr_none_static_mid_rungs(self):
        assert compute_rungs(None, 5.0, 1.0, 2.0, 6.0) == (
            STATIC_RUNG1_ROE, STATIC_RUNG2_ROE) == (3.0, 6.0)

    def test_atr_nonpositive_static(self):
        assert compute_rungs(0.0, 5.0, 1.0, 2.0, 6.0) == (3.0, 6.0)
        assert compute_rungs(-1.0, 5.0, 1.0, 2.0, 6.0) == (3.0, 6.0)

    def test_leverage_zero_reads_one(self):
        assert compute_rungs(3.0, 0.0, 1.0, 2.0, 6.0) == (3.0, 6.0)

    def test_rungs_visible_in_snapshot(self, monkeypatch):
        tr = _tracker(monkeypatch)
        tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 3.0, 5.0)
        snap = tr.state_snapshot()["positions"]["p1"]
        # raw 5 × 3 = 15 → clamp 6.0 / 12.0.
        assert snap["rung1_roe"] == 6.0 and snap["rung2_roe"] == 12.0


class TestProgression:
    def test_below_rung1_none(self, monkeypatch):
        tr = _tracker(monkeypatch)   # rungs (3, 6) at atr 3, lev 1
        tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 1.0, 3.0)
        v = tr.on_mark("p1", _mark_for_roe(100.0, 2.9), now_s=1.0)
        assert v.action == "none" and v.rung_hit == 0

    def test_rung1_banks_third_and_arms_floor(self, monkeypatch):
        tr = _tracker(monkeypatch)
        tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 1.0, 3.0)
        v = tr.on_mark("p1", _mark_for_roe(100.0, 3.5), now_s=1.0)
        assert v.action == "bank" and v.reason == "rung1"
        assert abs(v.fraction - 1.0 / 3.0) < 1e-9
        assert v.rung_hit == 1
        assert v.floor_roe == 0.6 * 3.0     # armed at floor_frac × rung1

    def test_rung2_banks_third_after_rung1(self, monkeypatch):
        tr = _tracker(monkeypatch)
        tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 1.0, 3.0)
        tr.on_mark("p1", _mark_for_roe(100.0, 3.5), now_s=1.0)
        v = tr.on_mark("p1", _mark_for_roe(100.0, 6.5), now_s=2.0)
        assert v.action == "bank" and v.reason == "rung2"
        assert abs(v.fraction - 1.0 / 3.0) < 1e-9
        assert v.rung_hit == 2
        assert v.floor_roe == 0.6 * 6.0     # tightened to floor_frac × rung2

    def test_no_rebank_at_same_rung(self, monkeypatch):
        tr = _tracker(monkeypatch)
        tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 1.0, 3.0)
        tr.on_mark("p1", _mark_for_roe(100.0, 3.5), now_s=1.0)
        # ROE holds above rung1 but below rung2 — no second bank.
        v = tr.on_mark("p1", _mark_for_roe(100.0, 4.0), now_s=2.0)
        assert v.action == "none" and v.rung_hit == 1


class TestTide:
    def test_opposed_escalates_to_half(self, monkeypatch):
        tr = _tracker(monkeypatch, tide_fn=lambda sym: "opposed")
        tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 1.0, 3.0)
        v = tr.on_mark("p1", _mark_for_roe(100.0, 3.5), now_s=1.0)
        assert v.action == "bank" and v.fraction == 0.5

    def test_aligned_and_neutral_keep_default(self, monkeypatch):
        for tide in ("aligned", "neutral"):
            tr = _tracker(monkeypatch, tide_fn=lambda sym, _t=tide: _t)
            tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 1.0, 3.0)
            v = tr.on_mark("p1", _mark_for_roe(100.0, 3.5), now_s=1.0)
            assert abs(v.fraction - 1.0 / 3.0) < 1e-9

    def test_none_tide_fn_abstains_default(self, monkeypatch):
        tr = _tracker(monkeypatch, tide_fn=None)
        tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 1.0, 3.0)
        v = tr.on_mark("p1", _mark_for_roe(100.0, 3.5), now_s=1.0)
        assert abs(v.fraction - 1.0 / 3.0) < 1e-9

    def test_tide_fn_returns_none_abstains(self, monkeypatch):
        tr = _tracker(monkeypatch, tide_fn=lambda sym: None)
        tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 1.0, 3.0)
        v = tr.on_mark("p1", _mark_for_roe(100.0, 3.5), now_s=1.0)
        assert abs(v.fraction - 1.0 / 3.0) < 1e-9

    def test_tide_fn_raise_abstains(self, monkeypatch):
        def _boom(sym):
            raise RuntimeError("tide plane dark")
        tr = _tracker(monkeypatch, tide_fn=_boom)
        tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 1.0, 3.0)
        v = tr.on_mark("p1", _mark_for_roe(100.0, 3.5), now_s=1.0)
        assert abs(v.fraction - 1.0 / 3.0) < 1e-9


class TestFloor:
    def test_floor_not_armed_before_rung1(self, monkeypatch):
        # ROE dips below what the floor WOULD be pre-rung1 — no floor_close.
        tr = _tracker(monkeypatch)
        tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 1.0, 3.0)
        v = tr.on_mark("p1", _mark_for_roe(100.0, 1.0), now_s=1.0)
        assert v.action == "none" and v.floor_roe is None

    def test_floor_close_exits_remainder(self, monkeypatch):
        tr = _tracker(monkeypatch)
        tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 1.0, 3.0)
        tr.on_mark("p1", _mark_for_roe(100.0, 3.5), now_s=1.0)   # floor 1.8
        v = tr.on_mark("p1", _mark_for_roe(100.0, 1.8), now_s=2.0)
        assert v.action == "floor_close" and v.reason == "floor"
        assert v.fraction == 1.0      # ALL remaining
        assert abs(v.floor_roe - 1.8) < 1e-9

    def test_floor_tighten_only(self, monkeypatch):
        # After rung2 the floor sits at 3.6; it must never move back down.
        tr = _tracker(monkeypatch)
        tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 1.0, 3.0)
        tr.on_mark("p1", _mark_for_roe(100.0, 3.5), now_s=1.0)
        f1 = tr.state_snapshot()["positions"]["p1"]["floor_roe"]
        tr.on_mark("p1", _mark_for_roe(100.0, 6.5), now_s=2.0)
        f2 = tr.state_snapshot()["positions"]["p1"]["floor_roe"]
        assert abs(f1 - 1.8) < 1e-9 and abs(f2 - 3.6) < 1e-9 and f2 > f1
        # Giveback to the tightened floor closes; the pre-rung2 floor (1.8)
        # is dead — ROE 2.0 is between them and must NOT fire.
        v = tr.on_mark("p1", _mark_for_roe(100.0, 2.0), now_s=3.0)
        assert v.action == "floor_close" and abs(v.floor_roe - 3.6) < 1e-9

    def test_floor_close_fires_once(self, monkeypatch):
        tr = _tracker(monkeypatch)
        tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 1.0, 3.0)
        tr.on_mark("p1", _mark_for_roe(100.0, 3.5), now_s=1.0)
        tr.on_mark("p1", _mark_for_roe(100.0, 1.0), now_s=2.0)
        v = tr.on_mark("p1", _mark_for_roe(100.0, 0.5), now_s=3.0)
        assert v.action == "none"     # track spent, no verdict spam


class TestRegistration:
    def test_reregister_idempotent(self, monkeypatch):
        tr = _tracker(monkeypatch)
        assert tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 1.0, 3.0) is True
        assert tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 1.0, 3.0) is False

    def test_reregister_preserves_rung_state(self, monkeypatch):
        tr = _tracker(monkeypatch)
        tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 1.0, 3.0)
        tr.on_mark("p1", _mark_for_roe(100.0, 3.5), now_s=1.0)
        # A stray duplicate-registration attempt must not reset rung_hit.
        assert tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 1.0, 3.0) is False
        assert tr.state_snapshot()["positions"]["p1"]["rung_hit"] == 1

    def test_on_close_cleanup_and_unknown_noop(self, monkeypatch):
        tr = _tracker(monkeypatch)
        tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 1.0, 3.0)
        assert tr.on_close("p1") is True
        assert tr.on_close("p1") is False        # unknown id → no-op
        assert tr.state_snapshot()["n_tracked"] == 0


class TestFailClosed:
    def test_bad_entry_and_qty(self, monkeypatch):
        tr = _tracker(monkeypatch)
        assert tr.on_open("p1", "BTC-USD", "long", 3.0, 0.0, 1.0, 3.0) is False
        assert tr.on_open("p2", "BTC-USD", "long", 0.0, 100.0, 1.0, 3.0) is False
        assert tr.on_open("p3", "BTC-USD", "long", -1, 100.0, 1.0, 3.0) is False
        assert tr.on_open("p4", "BTC-USD", "long", 3.0, -5, 1.0, 3.0) is False

    def test_bad_side_and_bad_types(self, monkeypatch):
        tr = _tracker(monkeypatch)
        assert tr.on_open("p1", "BTC-USD", "flat", 3.0, 100.0, 1.0, 3.0) is False
        assert tr.on_open("p2", "BTC-USD", "long", "x", 100.0, 1.0, 3.0) is False

    def test_bad_mark_abstains(self, monkeypatch):
        tr = _tracker(monkeypatch)
        tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 1.0, 3.0)
        for bad in (0.0, -1.0, None, "x"):
            assert tr.on_mark("p1", bad, now_s=1.0).action == "none"

    def test_unknown_id_never_verdict(self, monkeypatch):
        tr = _tracker(monkeypatch)
        v = tr.on_mark("ghost", 110.0, now_s=1.0)
        assert v.action == "none" and v.fraction == 0.0


class TestShortSide:
    def test_short_roe_direction(self, monkeypatch):
        # A short banks when the mark FALLS.
        tr = _tracker(monkeypatch)
        tr.on_open("p1", "ETH-USD", "short", 3.0, 100.0, 1.0, 3.0)
        assert tr.on_mark("p1", 101.0, now_s=1.0).action == "none"
        v = tr.on_mark("p1", _mark_for_roe(100.0, 3.5, side="short"), now_s=2.0)
        assert v.action == "bank" and v.reason == "rung1"

    def test_short_floor_close(self, monkeypatch):
        tr = _tracker(monkeypatch)
        tr.on_open("p1", "ETH-USD", "short", 3.0, 100.0, 1.0, 3.0)
        tr.on_mark("p1", _mark_for_roe(100.0, 3.5, side="short"), now_s=1.0)
        v = tr.on_mark("p1", _mark_for_roe(100.0, 1.0, side="short"), now_s=2.0)
        assert v.action == "floor_close" and v.fraction == 1.0


class TestSnapshot:
    def test_shape(self, monkeypatch):
        tr = _tracker(monkeypatch)
        tr.on_open("p1", "BTC-USD", "long", 3.0, 100.0, 2.0, 3.0,
                   tags=frozenset({"campaign"}))
        snap = tr.state_snapshot()
        assert snap["enabled"] is True and snap["n_tracked"] == 1
        row = snap["positions"]["p1"]
        assert row["symbol"] == "BTC-USD" and row["rung_hit"] == 0
        assert row["tags"] == ["campaign"]
        assert snap["config"]["min_notional_usd"] == 30.0
