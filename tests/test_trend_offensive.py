"""Trend Offensive ("Hugo") brain pins (2026-08-22).

The right-tail doctrine switch. The 3-day autopsy (−$37.8 vs BTC +24%,
winner hold median 0.30h / MAX 3.98h on 16-hour trend days) priced the
fixed-ladder + harvest + veto stack's right-tail amputation. Hugo arms only
when N>=4 of 6 evidences align WITH the day move — a liquidation wick with
no day structure never arms; a real crash arms SHORT mode (symmetric).

State machine pins: confirm streak, day_move requirement, hysteresis band
(entry 4 / exit 3), decay timer, immediate qualified flip, modifier
neutrality off-mode (kill-switch bit-for-bit contract).
"""
import time

from intelligence.trend_offensive import TrendOffensive


def _brain(**kw):
    defaults = dict(entry_n=4, exit_n=3, confirm_evals=2, decay_s=900.0,
                    size_boost=2.0, veto_discount=0.35, grace_mult=4.0)
    defaults.update(kw)
    return TrendOffensive(**defaults)


def _votes(day_move=1, htf=1, cascade=1, funding=1, dispersion=0, rally=0):
    return {"day_move": day_move, "htf": htf, "cascade": cascade,
            "funding": funding, "dispersion": dispersion, "rally": rally}


# ── Arming ───────────────────────────────────────────────────────────────────

def test_four_aligned_votes_arm_after_confirm_streak():
    b = _brain()
    d1 = b.evaluate(_votes())            # 4 aligned (dm/htf/cascade/funding)
    assert d1.mode == "off"              # streak 1 of 2 — not yet
    d2 = b.evaluate(_votes())
    assert d2.mode == "long"
    assert d2.changed is True
    assert d2.previous_mode == "off"
    assert d2.n_aligned == 4


def test_three_votes_never_arm():
    b = _brain()
    for _ in range(5):
        d = b.evaluate(_votes(funding=0))   # 3 aligned
    assert d.mode == "off"


def test_day_move_is_mandatory_among_aligned():
    # 4 aligned WITHOUT day_move — a wick, not a trend. Never arms.
    b = _brain()
    v = _votes(day_move=0, dispersion=1, rally=1)  # htf/cascade/funding/disp/rally = 5
    for _ in range(4):
        d = b.evaluate(v)
    assert d.mode == "off"


def test_day_move_opposing_the_majority_blocks_arming():
    # Majority long but day_move votes short — the flush-with-bounce case.
    b = _brain()
    v = _votes(day_move=-1, htf=1, cascade=1, funding=1, dispersion=1)
    for _ in range(4):
        d = b.evaluate(v)
    assert d.mode == "off"


def test_conflicting_stack_never_arms():
    b = _brain()
    v = _votes(day_move=1, htf=1, cascade=-1, funding=-1, dispersion=1, rally=-1)
    for _ in range(4):
        d = b.evaluate(v)               # 3 long vs 3 short — no majority
    assert d.mode == "off"


def test_streak_resets_on_non_qualifying_eval():
    b = _brain()
    b.evaluate(_votes())                # streak 1
    b.evaluate(_votes(funding=0))       # broken
    d = b.evaluate(_votes())            # streak 1 again — not armed
    assert d.mode == "off"
    d = b.evaluate(_votes())
    assert d.mode == "long"


def test_short_mode_arms_symmetrically():
    # The 60K-crash case: day move down, HTF bearish, cascade shorts,
    # funding fuel for shorts, risk_off leadership.
    b = _brain()
    v = _votes(day_move=-1, htf=-1, cascade=-1, funding=-1, dispersion=-1)
    b.evaluate(v)
    d = b.evaluate(v)
    assert d.mode == "short"
    assert d.n_aligned == 5


# ── Hysteresis / decay / flip ────────────────────────────────────────────────

def _armed(mode_votes=None):
    b = _brain()
    v = mode_votes or _votes()
    b.evaluate(v)
    b.evaluate(v)
    assert b.mode != "off"
    return b


def test_hysteresis_holds_at_exit_n():
    b = _armed()
    d = b.evaluate(_votes(funding=0))   # 3 aligned — below entry, at exit
    assert d.mode == "long"             # hysteresis band: stays on
    assert b._decay_since == 0.0        # not decaying


def test_decay_timer_arms_below_exit_n():
    b = _armed()
    d = b.evaluate(_votes(funding=0, cascade=0))  # 2 aligned — decay starts
    assert d.mode == "long"
    assert b._decay_since > 0.0


def test_decay_resets_when_evidence_recovers():
    b = _armed()
    b.evaluate(_votes(funding=0, cascade=0))      # decaying
    assert b._decay_since > 0.0
    b.evaluate(_votes())                           # recovered
    assert b._decay_since == 0.0
    assert b.mode == "long"


def test_decay_expiry_turns_mode_off():
    t = [1000.0]
    b = TrendOffensive(entry_n=4, exit_n=3, confirm_evals=2, decay_s=900.0,
                       now_fn=lambda: t[0])
    b.evaluate(_votes())
    b.evaluate(_votes())
    assert b.mode == "long"
    t[0] += 100.0
    b.evaluate(_votes(funding=0, cascade=0))       # decay starts @1100
    t[0] += 899.0
    d = b.evaluate(_votes(funding=0, cascade=0))
    assert d.mode == "long"                        # 899s < 900s
    t[0] += 2.0
    d = b.evaluate(_votes(funding=0, cascade=0))
    assert d.mode == "off"
    assert d.changed is True
    assert d.previous_mode == "long"


def test_qualified_flip_is_immediate():
    b = _armed()
    flip = _votes(day_move=-1, htf=-1, cascade=-1, funding=-1)
    d = b.evaluate(flip)           # opposite fully qualifies — no decay wait
    assert d.mode == "short"
    assert d.changed is True
    assert d.previous_mode == "long"


def test_unqualified_counter_evidence_decays_not_flips():
    b = _armed()
    v = _votes(day_move=-1, htf=-1, cascade=0, funding=0)  # only 2 short
    d = b.evaluate(v)
    assert d.mode == "long"        # decaying, not flipped


# ── Modifiers — neutral off-mode, active on-mode ────────────────────────────

def test_modifiers_neutral_when_off():
    b = _brain()
    for d in ("long", "short"):
        assert b.size_mult(d) == 1.0
        assert b.veto_discount_mult(d) == 1.0
        assert b.tp_suspended(d) is False
        assert b.harvest_suspended(d) is False
        assert b.eviction_immune(d) is False
        assert b.grace_mult(d) == 1.0


def test_modifiers_active_only_for_mode_direction():
    b = _armed()
    assert b.size_mult("long") == 2.0
    assert b.veto_discount_mult("long") == 0.35
    assert b.tp_suspended("long") is True
    assert b.harvest_suspended("long") is True
    assert b.eviction_immune("long") is True
    assert b.grace_mult("long") == 4.0
    # Counter-direction positions get NOTHING — no subsidy for fighting mode.
    assert b.size_mult("short") == 1.0
    assert b.veto_discount_mult("short") == 1.0
    assert b.tp_suspended("short") is False
    assert b.harvest_suspended("short") is False
    assert b.eviction_immune("short") is False
    assert b.grace_mult("short") == 1.0


def test_reset_restores_neutral():
    b = _armed()
    b.reset()
    assert b.mode == "off"
    assert b.size_mult("long") == 1.0
    assert b.tp_suspended("long") is False


def test_rearm_after_decay():
    t = [0.0]
    b = TrendOffensive(entry_n=4, exit_n=3, confirm_evals=2, decay_s=60.0,
                       now_fn=lambda: t[0])
    b.evaluate(_votes())
    b.evaluate(_votes())
    assert b.mode == "long"
    t[0] += 120.0
    b.evaluate(_votes(funding=0, cascade=0))   # decay starts @120
    t[0] += 120.0
    b.evaluate(_votes(funding=0, cascade=0))   # 120s > 60s decay → off
    assert b.mode == "off"
    b.evaluate(_votes())                        # streak 1
    d = b.evaluate(_votes())
    assert d.mode == "long"                     # re-armed clean


def test_since_timestamp_set_on_activation():
    t = [500.0]
    b = TrendOffensive(entry_n=4, exit_n=3, confirm_evals=2,
                       now_fn=lambda: t[0])
    b.evaluate(_votes())
    t[0] = 530.0
    d = b.evaluate(_votes())
    assert d.since == 530.0
