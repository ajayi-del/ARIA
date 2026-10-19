"""Post-stop flip gate pins (2026-10-19, fakeout repair).

A stop-out whose mark recovers through the stop within 15 min is a FAKEOUT —
the level held. The gate blocks OPPOSITE-direction entries for 60 min;
same-direction re-entry is never blocked. Recovery is attested only at
entry-evaluation moments (tick history not retained — documented
approximation): the first opposite-direction evaluation inside the recovery
window that observes the mark through the stop ARMS the block; the armed
block then persists to the block-window expiry. Late first evaluations can
never arm (fail-open direction chosen deliberately).

Kill switch POST_STOP_FLIP_GATE_ENABLED gates ENFORCEMENT at the splice
sites; the verdict is computed in both modes (tp_invariant idiom).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.post_stop_flip import (  # noqa: E402
    BLOCK_WINDOW_MS, RECOVERY_WINDOW_MS, REGISTRY_TTL_MS, STOP_EXIT_REASONS,
    flip_verdict, post_stop_flip_gate_enabled, record_stop)

T0 = 1_000_000_000_000  # arbitrary epoch ms anchor


class TestKillSwitch:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("POST_STOP_FLIP_GATE_ENABLED", raising=False)
        assert post_stop_flip_gate_enabled() is True

    def test_explicit_off(self, monkeypatch):
        monkeypatch.setenv("POST_STOP_FLIP_GATE_ENABLED", "false")
        assert post_stop_flip_gate_enabled() is False

    def test_env_read_at_call_time(self, monkeypatch):
        monkeypatch.setenv("POST_STOP_FLIP_GATE_ENABLED", "false")
        assert post_stop_flip_gate_enabled() is False
        monkeypatch.setenv("POST_STOP_FLIP_GATE_ENABLED", "true")
        assert post_stop_flip_gate_enabled() is True


class TestRecordStop:
    def test_valid_write(self):
        r = {}
        record_stop(r, "SOL-USD", "long", 100.0, T0)
        assert r[("SOL-USD", "long")]["stop"] == 100.0
        assert r[("SOL-USD", "long")]["stopped_ms"] == T0
        assert r[("SOL-USD", "long")]["recovered_ms"] is None

    def test_invalid_side_dropped(self):
        r = {}
        record_stop(r, "SOL-USD", "flat", 100.0, T0)
        assert r == {}

    def test_nonpositive_stop_dropped(self):
        # A bad record must never manufacture a future block.
        r = {}
        record_stop(r, "SOL-USD", "long", 0.0, T0)
        record_stop(r, "SOL-USD", "long", -5.0, T0)
        assert r == {}

    def test_ttl_prune_on_write(self):
        r = {}
        record_stop(r, "OLD-USD", "long", 100.0, T0 - REGISTRY_TTL_MS - 1)
        record_stop(r, "NEW-USD", "short", 50.0, T0)
        assert ("OLD-USD", "long") not in r
        assert ("NEW-USD", "short") in r

    def test_garbage_never_raises(self):
        r = {}
        record_stop(r, None, None, "x", "y")
        assert r == {}


class TestFlipVerdict:
    def _reg(self, side="long", stop=100.0, stopped_ms=T0):
        r = {}
        record_stop(r, "SOL-USD", side, stop, stopped_ms)
        return r

    def test_no_record_no_block(self):
        assert flip_verdict({}, "SOL-USD", "short", 101.0, T0 + 60_000) is None

    def test_same_direction_not_blocked(self):
        # Stopped LONG → a fresh LONG looks up the SHORT record (absent).
        r = self._reg(side="long", stop=100.0)
        assert flip_verdict(r, "SOL-USD", "long", 101.0, T0 + 60_000) is None

    def test_recovered_long_stop_blocks_short(self):
        # The fakeout: stopped long at 100, mark back at 101 within 15 min.
        r = self._reg(side="long", stop=100.0)
        v = flip_verdict(r, "SOL-USD", "short", 101.0, T0 + 60_000)
        assert v is not None and v["stopped_side"] == "long"

    def test_recovered_short_stop_blocks_long(self):
        r = self._reg(side="short", stop=100.0)
        v = flip_verdict(r, "SOL-USD", "long", 99.0, T0 + 60_000)
        assert v is not None and v["stopped_side"] == "short"

    def test_not_recovered_no_block(self):
        # Mark still through the stop — the stop was honest, no fakeout.
        r = self._reg(side="long", stop=100.0)
        assert flip_verdict(r, "SOL-USD", "short", 99.0, T0 + 60_000) is None

    def test_armed_block_persists_after_unrecovery(self):
        # Arm at minute 5; at minute 30 the mark fell back — block holds.
        r = self._reg(side="long", stop=100.0)
        assert flip_verdict(r, "SOL-USD", "short", 101.0, T0 + 300_000) is not None
        v = flip_verdict(r, "SOL-USD", "short", 98.0, T0 + 1_800_000)
        assert v is not None

    def test_block_expires_after_window(self):
        r = self._reg(side="long", stop=100.0)
        assert flip_verdict(r, "SOL-USD", "short", 101.0, T0 + 300_000) is not None
        assert flip_verdict(r, "SOL-USD", "short", 101.0,
                            T0 + BLOCK_WINDOW_MS + 1) is None

    def test_late_first_evaluation_never_arms(self):
        # First opposite evaluation arrives after the 15-min recovery window:
        # recovery can never be attested → no block (documented approximation).
        r = self._reg(side="long", stop=100.0)
        assert flip_verdict(r, "SOL-USD", "short", 101.0,
                            T0 + RECOVERY_WINDOW_MS + 1) is None
        # ...and it stays unarmed for the rest of the block window.
        assert flip_verdict(r, "SOL-USD", "short", 101.0,
                            T0 + RECOVERY_WINDOW_MS + 60_000) is None

    def test_invalid_mark_abstains(self):
        r = self._reg(side="long", stop=100.0)
        assert flip_verdict(r, "SOL-USD", "short", 0.0, T0 + 60_000) is None

    def test_clock_skew_abstains(self):
        r = self._reg(side="long", stop=100.0)
        assert flip_verdict(r, "SOL-USD", "short", 101.0, T0 - 1) is None

    def test_bad_direction_no_block(self):
        r = self._reg(side="long", stop=100.0)
        assert flip_verdict(r, "SOL-USD", "flat", 101.0, T0 + 60_000) is None

    def test_garbage_never_raises(self):
        assert flip_verdict({"broken": object()}, "SOL-USD", "short", 1.0, T0) is None


class TestConstants:
    def test_stop_exit_reasons(self):
        assert STOP_EXIT_REASONS == frozenset({"software_stop", "native_stop"})

    def test_window_ordering(self):
        assert RECOVERY_WINDOW_MS == 900_000
        assert BLOCK_WINDOW_MS == 3_600_000
        assert REGISTRY_TTL_MS == 4_500_000
        assert RECOVERY_WINDOW_MS < BLOCK_WINDOW_MS < REGISTRY_TTL_MS
