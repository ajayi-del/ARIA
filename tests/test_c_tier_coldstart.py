"""c_tier cold-start abstain gate pins (2026-10-19 equity deadlock repair).

Evidence: 12,612 equity signal_ready → 0 brackets since 2026-10-17 — hist_wr
defaults 0.5 on never-traded symbols (zero edge contribution) → c_tier → no
trades → no evidence, a permanent deadlock. The gate abstains (pass at 0.5
haircut, shadow gate c_tier_coldstart) only when the skeptic's matched
evidence is below VETO_MIN_N; real negative evidence rejects bit-for-bit.

Kill switch C_TIER_COLDSTART_PASS_ENABLED: OFF = legacy hard-reject. The
legacy reject stays shadow-scored under gate "c_tier" via REJECTION_EVENTS,
so the OFF state needs no extra record — pinned here at the function level.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.c_tier_coldstart import (  # noqa: E402
    COLDSTART_SIZE_MULT, c_tier_coldstart_enabled, coldstart_verdict)
from intelligence.skeptic import VETO_MIN_N  # noqa: E402


class TestKillSwitch:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("C_TIER_COLDSTART_PASS_ENABLED", raising=False)
        assert c_tier_coldstart_enabled() is True

    def test_explicit_off(self, monkeypatch):
        monkeypatch.setenv("C_TIER_COLDSTART_PASS_ENABLED", "false")
        assert c_tier_coldstart_enabled() is False

    def test_explicit_on(self, monkeypatch):
        monkeypatch.setenv("C_TIER_COLDSTART_PASS_ENABLED", "true")
        assert c_tier_coldstart_enabled() is True

    def test_env_read_at_call_time(self, monkeypatch):
        # The splice consults the switch per candidate, never cached at import.
        monkeypatch.setenv("C_TIER_COLDSTART_PASS_ENABLED", "false")
        assert c_tier_coldstart_enabled() is False
        monkeypatch.setenv("C_TIER_COLDSTART_PASS_ENABLED", "true")
        assert c_tier_coldstart_enabled() is True


class TestColdstartVerdict:
    def test_zero_evidence_abstains(self):
        assert coldstart_verdict(0, VETO_MIN_N) is True

    def test_below_min_abstains(self):
        assert coldstart_verdict(VETO_MIN_N - 1, VETO_MIN_N) is True

    def test_at_min_rejects(self):
        # n == min_n IS real evidence → legacy reject.
        assert coldstart_verdict(VETO_MIN_N, VETO_MIN_N) is False

    def test_above_min_rejects(self):
        assert coldstart_verdict(VETO_MIN_N + 50, VETO_MIN_N) is False

    def test_float_evidence(self):
        # Decay-weighted effective n arrives as float.
        assert coldstart_verdict(float(VETO_MIN_N) - 0.5, VETO_MIN_N) is True
        assert coldstart_verdict(float(VETO_MIN_N) + 0.5, VETO_MIN_N) is False

    def test_garbage_fails_closed(self):
        # Unreadable evidence counts as REAL evidence → legacy reject.
        assert coldstart_verdict(None, VETO_MIN_N) is False
        assert coldstart_verdict("x", VETO_MIN_N) is False

    def test_default_min_is_skeptic_veto_min(self):
        assert coldstart_verdict(VETO_MIN_N - 1) is True
        assert coldstart_verdict(VETO_MIN_N) is False


class TestConstants:
    def test_size_haircut(self):
        assert COLDSTART_SIZE_MULT == 0.5
