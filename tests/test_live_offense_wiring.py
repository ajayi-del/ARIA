"""Live-offense wiring pins (2026-09-22, Governor directive "live execution
from day one with kill switches"): the 4 signal-side modules (narrative
propagation boost, catalyst calendar, beta-anchor sizing, fear-greed long
veto) ship LIVE — every kill-switch off-state restores the pre-module
system bit-for-bit, and every new live gate registers in the shadow journal
for counterfactual measurement ("live AND measured")."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SRC = os.path.join(_REPO, "main.py")


def _main_src() -> str:
    with open(_MAIN_SRC) as fh:
        return fh.read()


class TestConfigKnobs:
    def test_live_offense_knobs_default_true(self):
        from core.config import Settings
        s = Settings()
        assert s.narrative_boost_enabled is True
        assert s.catalyst_calendar_enabled is True
        assert s.beta_sizing_enabled is True
        assert s.fear_greed_enabled is True
        assert s.fear_greed_gate_enabled is True

    def test_bounded_multipliers(self):
        # Quant bounds: every new multiplier is capped — no unbounded degree
        # of freedom enters the sizing chain.
        from core.config import Settings
        s = Settings()
        assert s.narrative_size_boost == 0.25          # hard cap 1.25
        assert s.catalyst_size_boost == 1.20
        assert 0.0 < s.offense_intel_cadence_s <= 3600.0
        assert s.fear_greed_poll_s >= 300.0


class TestShadowRegistration:
    def test_fear_greed_gate_registered(self):
        from intelligence.shadow_journal import REJECTION_EVENTS
        assert REJECTION_EVENTS["signal_rejected_fear_greed_extreme"] == "fear_greed"

    def test_no_unregistered_duplicate_gates(self):
        # The only documented alias pair remains regime_alignment (dead log
        # event + live emitter sharing one gate). The new registration must
        # not introduce a second.
        from collections import Counter
        from intelligence.shadow_journal import REJECTION_EVENTS
        dupes = {g for g, n in Counter(REJECTION_EVENTS.values()).items() if n > 1}
        assert dupes == {"regime_alignment"}


class TestCatalystSeedFile:
    def test_seed_loads_clean(self):
        # The live seed is JSONL (one object per line) — the loop reads it
        # line-by-line; a json.load of the whole file must FAIL by shape.
        from intelligence.catalyst_calendar import load_catalysts
        path = os.path.join(_REPO, "logs", "catalyst_events.json")
        rows = []
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        assert rows, "seed file must carry at least one honest row"
        events, errors = load_catalysts(rows)
        assert errors == []
        assert len(events) == len(rows)

    def test_seed_rows_carry_required_fields(self):
        path = os.path.join(_REPO, "logs", "catalyst_events.json")
        with open(path) as fh:
            for line in fh:
                row = json.loads(line)
                for field in ("symbol_or_cluster", "event_type", "ts_utc",
                              "source", "confidence"):
                    assert row.get(field) is not None


class TestWiring:
    def test_loops_defined_and_registered(self):
        src = _main_src()
        assert "async def _fear_greed_loop" in src
        assert "async def _offense_intel_loop" in src
        assert '_supervise(_fear_greed_loop' in src
        assert '_supervise(_offense_intel_loop' in src

    def test_fear_greed_veto_sites(self):
        # Three entry paths refuse new longs in a fresh red zone: standard,
        # cascade momentum, cascade aftermath. Shorts are never touched.
        src = _main_src()
        assert src.count("signal_rejected_fear_greed_extreme") == 3

    def test_sizing_chain_registers_new_legs(self):
        # Q4 decorrelation audit hazard: every new sizing multiplier MUST be
        # a named leg in the sizing_chain log AND the decorrelation tuple.
        src = _main_src()
        for leg in ("narrative_mult", "catalyst_mult", "beta_mult"):
            assert leg in src
        for name in ('"narrative"', '"catalyst"', '"beta_anchor"'):
            assert name in src

    def test_singletons_wired(self):
        src = _main_src()
        assert "_narrative_tracker = NarrativeTracker(" in src
        assert "_rolling_beta = RollingBeta(" in src
        assert "_fng_state = FearGreedState()" in src
        assert "def _fng_armed" in src
