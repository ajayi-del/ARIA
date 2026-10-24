"""tests for intelligence/squeeze_playbook.py — the multi-leg squeeze lifecycle
state machine. Shadow-first: intents are journal rows; the brain never does I/O.
"""
from __future__ import annotations

import pytest

from intelligence.squeeze_playbook import (
    PlaybookConfig,
    PlaybookInput,
    PlaybookState,
    SqueezePlaybook,
    squeeze_playbook_enabled,
)

T0 = 1_000_000.0
H = 3600.0


def mk(symbol="BMT", mark=100.0, funding=None, oi=None, spike=None,
       tier=3, arcus=False, now=T0):
    return PlaybookInput(symbol=symbol, mark_price=mark, funding_rate=funding,
                         oi_growth_3h=oi, volume_spike=spike, radar_tier=tier,
                         arcus_listed=arcus, now_s=now)


@pytest.fixture
def pb():
    return SqueezePlaybook(PlaybookConfig())


@pytest.fixture
def armed(pb):
    """Drive BMT to LEG1_ACTIVE at mark 100 (tier 3). Returns the playbook."""
    pb.advance({"BMT": 3}, [mk(mark=100.0, now=T0)])                     # watch
    pb.advance({"BMT": 3}, [mk(mark=100.0, spike=4.0, now=T0 + 60)])     # leg1
    return pb


def state_of(pb, sym="BMT"):
    return pb.state_snapshot()["contexts"][sym]["state"]


# ── kill switch ───────────────────────────────────────────────────────────────

def test_kill_switch_default_on(monkeypatch):
    monkeypatch.delenv("SQUEEZE_PLAYBOOK_ENABLED", raising=False)
    assert squeeze_playbook_enabled() is True


def test_kill_switch_off_no_intents_no_state(monkeypatch, pb):
    monkeypatch.setenv("SQUEEZE_PLAYBOOK_ENABLED", "false")
    assert squeeze_playbook_enabled() is False
    intents = pb.advance({"BMT": 3}, [mk(spike=9.0)])
    assert intents == []
    assert pb.state_snapshot()["contexts"] == {}
    assert pb.state_snapshot()["enabled"] is False


def test_kill_switch_off_freezes_existing_contexts(monkeypatch, armed):
    monkeypatch.setenv("SQUEEZE_PLAYBOOK_ENABLED", "false")
    intents = armed.advance({"BMT": 3}, [mk(mark=500.0, now=T0 + 120)])
    assert intents == []
    ctx = armed.state_snapshot()["contexts"]["BMT"]
    assert ctx["state"] == "LEG1_ACTIVE"
    assert ctx["peak_price"] == 100.0   # untouched while off


# ── IDLE -> WATCHING ──────────────────────────────────────────────────────────

def test_idle_to_watching(pb):
    intents = pb.advance({"BMT": 3}, [mk()])
    assert len(intents) == 1
    i = intents[0]
    assert i.kind == "watch"
    assert i.venue_hint == "none"
    assert i.state_from == "IDLE" and i.state_to == "WATCHING"
    assert i.meta["radar_tier"] == 3
    assert state_of(pb) == "WATCHING"


def test_tier_below_2_stays_idle(pb):
    for tier in (0, 1):
        pb2 = SqueezePlaybook(PlaybookConfig())
        intents = pb2.advance({"BMT": tier}, [mk(tier=tier)])
        assert intents == []
        assert state_of(pb2) == "IDLE"


def test_radar_verdicts_override_input_tier(pb):
    # radar_verdicts arg is authoritative; input.radar_tier is the fallback
    intents = pb.advance({"BMT": 2}, [mk(tier=0)])
    assert intents and intents[0].kind == "watch"
    pb2 = SqueezePlaybook(PlaybookConfig())
    assert pb2.advance({}, [mk(tier=3)])[0].kind == "watch"


def test_watching_is_quiet_without_trigger(pb):
    pb.advance({"BMT": 3}, [mk()])
    for k in range(3):
        assert pb.advance({"BMT": 3}, [mk(mark=100 + k, now=T0 + k)]) == []


# ── WATCHING -> LEG1_ACTIVE ───────────────────────────────────────────────────

def test_leg1_entry_on_volume_spike(pb):
    pb.advance({"BMT": 3}, [mk()])
    intents = pb.advance({"BMT": 3}, [mk(spike=3.5, now=T0 + 60)])
    assert len(intents) == 1
    i = intents[0]
    assert i.kind == "enter_long_shadow"
    assert i.venue_hint == "aster_if_listed"
    assert i.state_from == "WATCHING" and i.state_to == "LEG1_ACTIVE"
    assert i.trigger_price == 100.0       # entry = mark at the transition
    assert i.trail_floor == 100.0         # band 0: floor = entry
    assert state_of(pb) == "LEG1_ACTIVE"


def test_volume_spike_missing_defers(pb):
    pb.advance({"BMT": 3}, [mk()])
    assert pb.advance({"BMT": 3}, [mk(spike=None, now=T0 + 60)]) == []
    assert state_of(pb) == "WATCHING"


def test_volume_spike_below_mult_stays_watching(pb):
    pb.advance({"BMT": 3}, [mk()])
    assert pb.advance({"BMT": 3}, [mk(spike=3.0, now=T0 + 60)]) == []  # not >
    assert state_of(pb) == "WATCHING"


# ── trailer floor math ────────────────────────────────────────────────────────

def test_floor_math_tier3_pinned(armed):
    # entry 100, peak 320 -> mfe 2.20 -> tier-3 band 4 (lock 0.80)
    # floor = 100 + (320-100)*0.80 = 276
    intents = armed.advance({"BMT": 3}, [mk(mark=320.0, now=T0 + 600)])
    ratchets = [i for i in intents if i.meta.get("reason") == "floor_band_ratchet"]
    assert ratchets, "expected band ratchet intents"
    final = ratchets[-1]
    assert final.kind == "enter_long_shadow"
    assert final.state_from == "LEG1_ACTIVE" and final.state_to == "LEG1_ACTIVE"
    assert final.trail_floor == pytest.approx(276.0)
    assert final.meta["band"] == 4
    ctx = armed.state_snapshot()["contexts"]["BMT"]
    assert ctx["floor"] == pytest.approx(276.0)
    assert ctx["peak_price"] == 320.0
    assert ctx["mfe"] == pytest.approx(2.20)


def test_floor_math_tier2_pinned():
    pb = SqueezePlaybook(PlaybookConfig())
    pb.advance({"BMT": 2}, [mk(tier=2, now=T0)])
    pb.advance({"BMT": 2}, [mk(tier=2, spike=5.0, now=T0 + 60)])   # entry 100
    # peak 150 -> mfe 0.50 -> tier-2 band 2 (lock 0.55) -> floor 127.5
    intents = pb.advance({"BMT": 2}, [mk(tier=2, mark=150.0, now=T0 + 600)])
    ratchets = [i for i in intents if i.meta.get("reason") == "floor_band_ratchet"]
    assert ratchets[-1].trail_floor == pytest.approx(127.5)
    assert ratchets[-1].meta["band"] == 2


def test_floor_tighten_only(armed):
    armed.advance({"BMT": 3}, [mk(mark=320.0, now=T0 + 600)])       # floor 276
    intents = armed.advance({"BMT": 3}, [mk(mark=200.0, now=T0 + 900)])
    assert intents == []                                            # no new band
    ctx = armed.state_snapshot()["contexts"]["BMT"]
    assert ctx["floor"] == pytest.approx(276.0)                     # never lowers
    assert ctx["peak_price"] == 320.0                               # peak holds


def test_band_ratchet_fires_once_per_band(armed):
    # band 1 at mfe>=0.10 (mark 110): exactly one ratchet intent
    i1 = armed.advance({"BMT": 3}, [mk(mark=110.0, now=T0 + 600)])
    assert len([i for i in i1 if i.meta.get("reason") == "floor_band_ratchet"]) == 1
    # same band, higher mark: no intent
    assert armed.advance({"BMT": 3}, [mk(mark=115.0, now=T0 + 700)]) == []
    # band 2 at mfe>=0.30 (mark 130): exactly one more
    i2 = armed.advance({"BMT": 3}, [mk(mark=130.0, now=T0 + 800)])
    ratchets = [i for i in i2 if i.meta.get("reason") == "floor_band_ratchet"]
    assert len(ratchets) == 1 and ratchets[0].meta["band"] == 2
    assert ratchets[0].trail_floor == pytest.approx(100 + 30 * 0.65)  # 119.5


def test_no_intents_without_transition(armed):
    for k in range(5):
        assert armed.advance({"BMT": 3}, [mk(mark=100.0, now=T0 + 120 + k)]) == []


# ── LEG1 -> CONSOLIDATING ─────────────────────────────────────────────────────

def test_leg1_to_consolidating(armed):
    armed.advance({"BMT": 3}, [mk(mark=140.0, now=T0 + H)])     # peak 140 @ +1h
    intents = armed.advance({"BMT": 3}, [mk(mark=135.0, now=T0 + 3 * H + 1)])
    assert intents == []                                        # transition is silent
    assert state_of(armed) == "CONSOLIDATING"


def test_consolidation_waits_min_hours(armed):
    armed.advance({"BMT": 3}, [mk(mark=140.0, now=T0 + H)])     # gain 0.40 > 0.30
    # only 1h since peak -> still LEG1
    armed.advance({"BMT": 3}, [mk(mark=135.0, now=T0 + 2 * H)])
    assert state_of(armed) == "LEG1_ACTIVE"


def test_consolidation_needs_gain(armed):
    armed.advance({"BMT": 3}, [mk(mark=120.0, now=T0 + H)])     # gain 0.20 < 0.30
    armed.advance({"BMT": 3}, [mk(mark=115.0, now=T0 + 5 * H)])
    assert state_of(armed) == "LEG1_ACTIVE"


# ── CONSOLIDATING -> LEG2_SETUP / timeout ─────────────────────────────────────

def _to_consol(pb):
    pb.advance({"BMT": 3}, [mk(mark=100.0, now=T0)])
    pb.advance({"BMT": 3}, [mk(mark=100.0, spike=4.0, now=T0 + 60)])
    pb.advance({"BMT": 3}, [mk(mark=140.0, now=T0 + H)])        # peak @ +1h
    pb.advance({"BMT": 3}, [mk(mark=135.0, now=T0 + 3 * H + 1)])
    assert state_of(pb) == "CONSOLIDATING"


def test_leg2_setup_intent(pb):
    _to_consol(pb)
    intents = pb.advance({"BMT": 3}, [mk(mark=136.0, funding=-2e-5, oi=0.08,
                                         now=T0 + 4 * H)])
    assert len(intents) == 1
    i = intents[0]
    assert i.kind == "leg2_add_shadow"
    assert i.venue_hint == "aster_if_listed"
    assert i.state_from == "CONSOLIDATING" and i.state_to == "LEG2_SETUP"
    assert i.trigger_price == 136.0
    assert i.trail_floor == pytest.approx(126.0)   # floor from leg-1 (band 2)
    assert state_of(pb) == "LEG2_SETUP"


def test_leg2_gate_missing_oi_defers(pb):
    _to_consol(pb)
    assert pb.advance({"BMT": 3}, [mk(mark=136.0, funding=-2e-5, oi=None,
                                      now=T0 + 4 * H)]) == []
    assert state_of(pb) == "CONSOLIDATING"


def test_leg2_gate_funding_not_rebuilt_defers(pb):
    _to_consol(pb)
    assert pb.advance({"BMT": 3}, [mk(mark=136.0, funding=1e-5, oi=0.08,
                                      now=T0 + 4 * H)]) == []
    assert state_of(pb) == "CONSOLIDATING"


def test_leg2_gate_oi_too_small_defers(pb):
    _to_consol(pb)
    assert pb.advance({"BMT": 3}, [mk(mark=136.0, funding=-2e-5, oi=0.05,
                                      now=T0 + 4 * H)]) == []   # not >
    assert state_of(pb) == "CONSOLIDATING"


def test_consol_timeout_to_cascade_short(pb):
    _to_consol(pb)                                              # peak @ +1h
    # >8h since peak without the leg-2 conditions -> armed CASCADE_SHORT
    intents = pb.advance({"BMT": 3}, [mk(mark=130.0, funding=1e-5, oi=0.01,
                                         now=T0 + 10 * H)])
    assert intents == []                                        # "no intent yet"
    assert state_of(pb) == "CASCADE_SHORT"


def test_consol_timeout_fires_even_with_missing_data(pb):
    _to_consol(pb)
    pb.advance({"BMT": 3}, [mk(mark=130.0, funding=None, oi=None,
                               now=T0 + 10 * H)])
    assert state_of(pb) == "CASCADE_SHORT"


# ── LEG2_SETUP ────────────────────────────────────────────────────────────────

def _to_leg2(pb):
    _to_consol(pb)
    pb.advance({"BMT": 3}, [mk(mark=136.0, funding=-2e-5, oi=0.08,
                               now=T0 + 4 * H)])
    assert state_of(pb) == "LEG2_SETUP"


def test_leg2_tracks_second_peak_floor_anchored_leg1(pb):
    _to_leg2(pb)                                                # leg2 entry 136 @ +4h
    # combined peak 200 -> mfe 1.00 -> band 4, lock 0.80 -> floor 180
    intents = pb.advance({"BMT": 3}, [mk(mark=200.0, now=T0 + 5 * H)])
    ratchets = [i for i in intents if i.meta.get("reason") == "floor_band_ratchet"]
    assert ratchets and ratchets[-1].kind == "leg2_add_shadow"
    assert ratchets[-1].trail_floor == pytest.approx(180.0)
    ctx = pb.state_snapshot()["contexts"]["BMT"]
    assert ctx["combined_peak"] == 200.0
    assert ctx["leg2_peak"] == 200.0
    assert ctx["peak_price"] == 140.0                           # leg-1 peak intact


def test_leg2_timeout_to_cascade_short(pb):
    _to_leg2(pb)                                                # leg2 entry @ +4h
    # sub-peak mark: no ratchet, pullback 0.014 < 0.05, funding None -> pure timeout
    intents = pb.advance({"BMT": 3}, [mk(mark=138.0, now=T0 + 4 * H + 13 * H)])
    assert intents == []
    assert state_of(pb) == "CASCADE_SHORT"


def test_leg2_immediate_cascade_confirm(pb):
    _to_leg2(pb)
    pb.advance({"BMT": 3}, [mk(mark=200.0, now=T0 + 5 * H)])    # combined peak 200
    # pullback (200-185)/200 = 0.075 > 0.05 AND funding 3e-5 > 2e-5 -> fire now
    intents = pb.advance({"BMT": 3}, [mk(mark=185.0, funding=3e-5,
                                         now=T0 + 5 * H + 60)])
    kinds = [i.kind for i in intents]
    assert kinds == ["cascade_short_shadow", "exit_shadow"]
    assert state_of(pb) == "EXHAUSTED"


# ── CASCADE_SHORT confirmation ────────────────────────────────────────────────

def _to_cascade_via_consol_timeout(pb, arcus=False):
    _to_consol(pb)
    pb.advance({"BMT": 3}, [mk(mark=130.0, funding=1e-5, oi=0.01, arcus=arcus,
                               now=T0 + 10 * H)])
    assert state_of(pb) == "CASCADE_SHORT"


def test_cascade_confirm_from_consol_timeout_is_counter_short(pb):
    _to_cascade_via_consol_timeout(pb)
    # combined peak 140; mark 130 -> pullback 0.0714 > 0.05; funding flips
    intents = pb.advance({"BMT": 3}, [mk(mark=130.0, funding=3e-5,
                                         now=T0 + 10 * H + 60)])
    kinds = [i.kind for i in intents]
    assert kinds == ["counter_short_shadow", "exit_shadow"]
    short = intents[0]
    assert short.state_from == "CASCADE_SHORT" and short.state_to == "EXHAUSTED"
    assert short.trigger_price == 130.0
    assert short.trail_floor == pytest.approx(126.0)
    assert short.meta["origin"] == "consol_timeout"
    ex = intents[1]
    assert ex.venue_hint == "aster_if_listed"
    assert state_of(pb) == "EXHAUSTED"


def test_cascade_venue_arcus_when_listed(pb):
    _to_cascade_via_consol_timeout(pb, arcus=True)
    intents = pb.advance({"BMT": 3}, [mk(mark=130.0, funding=3e-5, arcus=True,
                                         now=T0 + 10 * H + 60)])
    assert intents[0].venue_hint == "arcus"
    assert "note" not in intents[0].meta


def test_cascade_venue_none_when_not_listed(pb):
    _to_cascade_via_consol_timeout(pb, arcus=False)
    intents = pb.advance({"BMT": 3}, [mk(mark=130.0, funding=3e-5, arcus=False,
                                         now=T0 + 10 * H + 60)])
    assert intents[0].venue_hint == "none"
    assert intents[0].meta["note"] == "cross-venue leg unavailable — abstain"


def test_cascade_waits_when_funding_missing(pb):
    _to_cascade_via_consol_timeout(pb)
    assert pb.advance({"BMT": 3}, [mk(mark=130.0, funding=None,
                                      now=T0 + 10 * H + 60)]) == []
    assert state_of(pb) == "CASCADE_SHORT"                      # armed, deferring


def test_cascade_waits_when_pullback_too_small(pb):
    _to_cascade_via_consol_timeout(pb)
    # peak 140; mark 134 -> pullback 0.0429 < 0.05
    assert pb.advance({"BMT": 3}, [mk(mark=134.0, funding=3e-5,
                                      now=T0 + 10 * H + 60)]) == []
    assert state_of(pb) == "CASCADE_SHORT"


def test_cascade_waits_when_funding_not_flipped(pb):
    _to_cascade_via_consol_timeout(pb)
    assert pb.advance({"BMT": 3}, [mk(mark=130.0, funding=1e-5,
                                      now=T0 + 10 * H + 60)]) == []
    assert state_of(pb) == "CASCADE_SHORT"


def test_cascade_one_shot_no_spam(pb):
    _to_cascade_via_consol_timeout(pb)
    pb.advance({"BMT": 3}, [mk(mark=130.0, funding=3e-5, now=T0 + 10 * H + 60)])
    assert state_of(pb) == "EXHAUSTED"
    for k in range(3):
        assert pb.advance({"BMT": 3},
                          [mk(mark=120.0, funding=3e-5,
                              now=T0 + 11 * H + k)]) == []


# ── EXHAUSTED expiry ──────────────────────────────────────────────────────────

def test_exhausted_ttl_drops_context(pb):
    _to_cascade_via_consol_timeout(pb)
    exhausted_at = T0 + 10 * H + 60
    pb.advance({"BMT": 3}, [mk(mark=130.0, funding=3e-5, now=exhausted_at)])
    # inside the 24h TTL: inert
    assert pb.advance({"BMT": 3}, [mk(mark=110.0, funding=3e-5,
                                      now=exhausted_at + 23 * H)]) == []
    assert "BMT" in pb.state_snapshot()["contexts"]
    # past the TTL: one "exhausted" intent, then the context drops
    intents = pb.advance({"BMT": 3}, [mk(mark=110.0, funding=3e-5,
                                         now=exhausted_at + 25 * H)])
    assert len(intents) == 1
    i = intents[0]
    assert i.kind == "exhausted"
    assert i.venue_hint == "none"
    assert i.state_from == "EXHAUSTED" and i.state_to == "EXHAUSTED"
    assert i.meta["reason"] == "ttl_expired"
    assert "BMT" not in pb.state_snapshot()["contexts"]
    # after the drop the symbol is a fresh IDLE context (tier 0 -> silent)
    assert pb.advance({"BMT": 0}, [mk(mark=110.0, now=exhausted_at + 26 * H)]) == []
    assert state_of(pb) == "IDLE"


# ── full lifecycle walk ───────────────────────────────────────────────────────

def test_full_lifecycle_walk(pb):
    seen_states, seen_kinds = [], []

    def tick(**kw):
        intents = pb.advance({"BMT": 3}, [mk(**kw)])
        seen_states.append(state_of(pb))
        seen_kinds.extend(i.kind for i in intents)
        return intents

    tick(mark=100.0, now=T0)                                    # WATCHING
    assert seen_states[-1] == "WATCHING"
    tick(mark=100.0, spike=4.0, now=T0 + 60)                    # LEG1_ACTIVE
    assert seen_states[-1] == "LEG1_ACTIVE"
    tick(mark=140.0, now=T0 + H)                                # peak 140
    assert seen_states[-1] == "LEG1_ACTIVE"
    tick(mark=135.0, now=T0 + 3 * H + 1)                        # CONSOLIDATING
    assert seen_states[-1] == "CONSOLIDATING"
    tick(mark=136.0, funding=-2e-5, oi=0.08, now=T0 + 4 * H)    # LEG2_SETUP
    assert seen_states[-1] == "LEG2_SETUP"
    tick(mark=200.0, now=T0 + 5 * H)                            # leg-2 peak 200
    assert seen_states[-1] == "LEG2_SETUP"
    tick(mark=190.0, now=T0 + 4 * H + 13 * H)                   # leg2 timeout
    assert seen_states[-1] == "CASCADE_SHORT"
    final = tick(mark=185.0, funding=3e-5, now=T0 + 18 * H)     # confirm fade
    assert seen_states[-1] == "EXHAUSTED"
    assert [i.kind for i in final] == ["cascade_short_shadow", "exit_shadow"]
    assert final[0].venue_hint == "none"
    # expected transition path with no unexpected intent kinds
    assert seen_states == ["WATCHING", "LEG1_ACTIVE", "LEG1_ACTIVE",
                           "CONSOLIDATING", "LEG2_SETUP", "LEG2_SETUP",
                           "CASCADE_SHORT", "EXHAUSTED"]
    assert seen_kinds[0] == "watch"
    assert "enter_long_shadow" in seen_kinds
    assert "leg2_add_shadow" in seen_kinds
    assert seen_kinds[-2:] == ["cascade_short_shadow", "exit_shadow"]


# ── fail-closed hygiene ───────────────────────────────────────────────────────

def test_mark_nonpositive_noop(pb):
    assert pb.advance({"BMT": 3}, [mk(mark=0.0)]) == []
    assert pb.advance({"BMT": 3}, [mk(mark=-5.0)]) == []
    assert pb.state_snapshot()["contexts"] == {}                # no context created


def test_never_raises_on_garbage(pb):
    bad = PlaybookInput(symbol="BMT", mark_price=float("nan"),
                        funding_rate=None, oi_growth_3h=None, volume_spike=None,
                        radar_tier=3, now_s=T0)
    assert pb.advance({"BMT": 3}, [bad]) == []                  # nan <= 0 is False...
    # nan mark: float(nan) succeeds, nan <= 0 is False — treated as a tick with
    # a non-comparable mark; must still not raise downstream
    pb.advance({"BMT": 3}, [mk(mark=100.0, spike=4.0, now=T0 + 60)])
    assert pb.advance({"BMT": 3}, [mk(mark=100.0, spike="x", now=T0 + 120)]) is not None


def test_multi_symbol_isolation(pb):
    pb.advance({"BMT": 3, "IRYS": 2}, [mk(symbol="BMT", mark=100.0, now=T0),
                                       mk(symbol="IRYS", mark=10.0, tier=2, now=T0)])
    intents = pb.advance({"BMT": 3, "IRYS": 2},
                         [mk(symbol="BMT", mark=100.0, spike=4.0, now=T0 + 60),
                          mk(symbol="IRYS", mark=10.0, spike=None, now=T0 + 60)])
    assert [i.kind for i in intents] == ["enter_long_shadow"]
    assert state_of(pb, "BMT") == "LEG1_ACTIVE"
    assert state_of(pb, "IRYS") == "WATCHING"


def test_state_snapshot_shape(armed):
    snap = armed.state_snapshot()
    assert snap["enabled"] is True
    ctx = snap["contexts"]["BMT"]
    for key in ("state", "tier", "entry_price", "entry_ts", "peak_price",
                "peak_ts", "leg2_entry_price", "combined_peak", "floor",
                "floor_band", "mfe", "cascade_origin"):
        assert key in ctx
    assert ctx["tier"] == 3
    assert ctx["entry_price"] == 100.0
