"""Tide-Aligned Consensus pins (2026-08-30): bounded ladder matrix, opposed-
tide abstain precedence, effective-breadth wiring, never-raises verdict."""
import math

import pytest

from intelligence.tide_consensus import (LADDER, TideConsensus, tac_rung)


class TestLadder:
    def test_bounded_placeholders(self):
        assert LADDER == {"none": 1.00, "weak": 1.05,
                          "strong": 1.15, "tide_strong": 1.25}

    def test_opposed_tide_abstains_first(self):
        # Even maximal breadth earns nothing into the institutional tide.
        assert tac_rung(10.0, "opposed") == ("abstain_opposed_tide", 1.00)

    def test_strong_plus_aligned(self):
        assert tac_rung(2.0, "aligned") == ("strong_tide_aligned", 1.25)
        assert tac_rung(3.7, "aligned") == ("strong_tide_aligned", 1.25)

    def test_strong_neutral(self):
        assert tac_rung(2.0, "neutral") == ("strong", 1.15)

    def test_weak(self):
        assert tac_rung(1.0, "neutral") == ("weak", 1.05)
        assert tac_rung(1.99, "aligned") == ("weak", 1.05)   # tide never
        # lifts a sub-strong consensus — amplification needs breadth first

    def test_none(self):
        assert tac_rung(0.0, "aligned") == ("none", 1.00)
        assert tac_rung(0.99, "neutral") == ("none", 1.00)


class TestVerdict:
    def _tc(self):
        return TideConsensus(time_fn=lambda: 1_000.0)

    def test_strong_verdict_two_venues(self):
        flows = [{"venue": "aster", "quality": "direct"},
                 {"venue": "hyperliquid", "quality": "direct"}]
        mult, ev = self._tc().verdict("BTC-USD", "long", flows,
                                      tide_state="aligned", freshness_s=42.0)
        assert mult == 1.25
        assert ev.effective_breadth == pytest.approx(2.0)
        assert ev.features["rung"] == "strong_tide_aligned"
        assert ev.features["tide_state"] == "aligned"
        assert ev.features["n_flows"] == 2
        assert ev.features["direct_flows"] == 2
        assert ev.freshness_s == 42.0
        assert ev.confidence is None            # learned — shadow journal
        assert ev.event_type == "tide_consensus"

    def test_same_venue_pair_only_weak(self):
        flows = [{"venue": "aster"}, {"venue": "aster"}]
        mult, ev = self._tc().verdict("ETH-USD", "short", flows)
        assert mult == 1.05                     # sqrt(2) < strong floor
        assert ev.effective_breadth == pytest.approx(math.sqrt(2), abs=1e-3)

    def test_no_flows_no_boost(self):
        mult, ev = self._tc().verdict("SOL-USD", "long", [])
        assert mult == 1.00
        assert ev.features["rung"] == "none"

    def test_opposed_abstains_with_breadth(self):
        flows = [{"venue": "a"}, {"venue": "b"}, {"venue": "c"}]
        mult, ev = self._tc().verdict("BTC-USD", "long", flows,
                                      tide_state="opposed")
        assert mult == 1.00
        assert ev.features["rung"] == "abstain_opposed_tide"

    def test_never_raises_on_garbage(self):
        mult, ev = self._tc().verdict("BTC-USD", "long", None,
                                      tide_state=None)
        assert mult == 1.00
        mult2, _ = self._tc().verdict("BTC-USD", "long", [{"broken": 1}])
        assert mult2 == 1.05                    # one flow → breadth 1 → weak


class TestWiring:
    @staticmethod
    def _main_src():
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent / "main.py").read_text()

    def test_kill_switch_defaults(self):
        from core.config import Settings
        s = Settings()
        assert s.tide_consensus_enabled is True
        assert s.whale_positions_enabled is True
        assert s.whale_absorption_enabled is True
        assert s.tac_strong_breadth_floor == 2.0

    def test_tac_splice_with_legacy_fallback(self):
        src = self._main_src()
        i_tac = src.index('getattr(config, "tide_consensus_enabled", True)')
        i_legacy = src.index("whale_mirror_consensus_boost\", 1.5)")
        assert i_tac < i_legacy          # TAC branch precedes legacy ladder

    def test_sizing_chain_carries_tac_fields(self):
        src = self._main_src()
        assert "tac_rung=_tac_rung" in src
        assert "tac_breadth=_tac_breadth" in src

    def test_loops_registered(self):
        src = self._main_src()
        assert '"whale_positions"),' in src
        assert '"whale_absorption"),' in src

    def test_was_shadow_gate_registered(self):
        src = self._main_src()
        assert '"whale_absorption", "whale_absorption_detected"' in src
