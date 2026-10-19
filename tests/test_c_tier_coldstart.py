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
    COLDSTART_EVIDENCE_DAYS, COLDSTART_SIZE_MULT, count_real_closes,
    c_tier_coldstart_enabled, coldstart_verdict)
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

    def test_evidence_window(self):
        assert COLDSTART_EVIDENCE_DAYS == 30


def _close(symbol, outcome="win", **kw):
    row = {"symbol": symbol, "outcome": outcome, "pnl_usd": 1.0}
    row.update(kw)
    return row


class TestCountRealCloses:
    """The verdict keys on REAL journal closes — never skeptic shadow
    records (dead-register finding 2026-10-19: shadow n counts gate
    refusals, thousands deep on starved equities → pass never fires)."""

    def test_never_traded_symbol_counts_zero(self):
        rows = [_close("BTC-USD"), _close("ETH-USD", "loss")]
        assert count_real_closes(rows, "NVDA-USD") == 0
        # …and zero real closes = no-evidence abstain.
        assert coldstart_verdict(
            count_real_closes(rows, "NVDA-USD"), VETO_MIN_N) is True

    def test_counts_win_and_loss_only(self):
        rows = [_close("AMD-USD", "win"), _close("AMD-USD", "loss"),
                _close("AMD-USD", "open"), _close("AMD-USD", None),
                _close("AMD-USD", "abandoned"), _close("AMD-USD", "rejected")]
        assert count_real_closes(rows, "AMD-USD") == 2

    def test_other_symbols_ignored(self):
        rows = [_close("META-USD")] * 5 + [_close("TSLA-USD")] * 3
        assert count_real_closes(rows, "META-USD") == 5
        assert count_real_closes(rows, "TSLA-USD") == 3

    def test_real_evidence_at_min_rejects(self):
        rows = [_close("HOOD-USD", "win" if i % 2 else "loss")
                for i in range(VETO_MIN_N)]
        n = count_real_closes(rows, "HOOD-USD")
        assert n == VETO_MIN_N
        assert coldstart_verdict(n, VETO_MIN_N) is False

    def test_operator_orphan_closes_excluded(self):
        # Synthetic orphan closes from adopted manual positions are not
        # ARIA evidence (same read-path filter as journal.get_closed).
        rows = [_close("MSTR-USD", "loss", orphan_close=True,
                       closed_at_ms=1791000000000)  # 2026-10-03, post-epoch
                for _ in range(VETO_MIN_N + 5)]
        assert count_real_closes(rows, "MSTR-USD") == 0

    def test_phantom_scale_ghosts_excluded(self):
        # The SPCX 2026-08-21/22 |pnl|>$100 scale ghosts are not evidence.
        rows = [_close("SPCX-USD", "win", pnl_usd=792.0,
                       closed_at="2026-08-21T10:00:00Z")]
        assert count_real_closes(rows, "SPCX-USD") == 0

    def test_garbage_fails_closed(self):
        assert count_real_closes(None, "AMD-USD") is None
        assert count_real_closes("not-a-list", "AMD-USD") is None
        assert count_real_closes([], "") is None
        assert count_real_closes([{"no_symbol": 1}, "junk", None],
                                 "AMD-USD") == 0

    def test_non_dict_rows_skipped(self):
        rows = ["junk", None, 42, _close("COIN-USD")]
        assert count_real_closes(rows, "COIN-USD") == 1
