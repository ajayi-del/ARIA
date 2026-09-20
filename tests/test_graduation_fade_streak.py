"""B1/A3 pins (2026-09-19): ENA fade-streak graduation revocation.

Pins the module-level pure decisions in main.py:
  fade_streak_decide       — streak accumulation / interval dedup / revoke
  rally_fade_revoke_verdict — legacy bit-for-bit when disabled; noise/boot/
                              fade cooloff routing when enabled.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402


def _streak():
    return {"count": 0, "last_ts": 0.0}


class TestFadeStreakDecide:
    def test_first_read_accumulates(self):
        s = _streak()
        assert main.fade_streak_decide(s, now=1000.0, interval_s=300.0, required=3) == "accumulate"
        assert s["count"] == 1
        assert s["last_ts"] == 1000.0

    def test_sub_interval_read_skips(self):
        s = _streak()
        main.fade_streak_decide(s, now=1000.0, interval_s=300.0, required=3)
        assert main.fade_streak_decide(s, now=1100.0, interval_s=300.0, required=3) == "skip"
        assert s["count"] == 1   # unchanged

    def test_revoke_at_required_and_resets(self):
        s = _streak()
        assert main.fade_streak_decide(s, now=1000.0, interval_s=300.0, required=3) == "accumulate"
        assert main.fade_streak_decide(s, now=1400.0, interval_s=300.0, required=3) == "accumulate"
        assert main.fade_streak_decide(s, now=1800.0, interval_s=300.0, required=3) == "revoke"
        assert s["count"] == 0   # reset on revoke

    def test_required_one_revokes_immediately(self):
        s = _streak()
        assert main.fade_streak_decide(s, now=1000.0, interval_s=300.0, required=1) == "revoke"


class TestRallyFadeRevokeVerdict:
    def test_disabled_is_legacy_bit_for_bit(self):
        s = _streak()
        base = dict(enabled=False, streak=s, now=1000.0, interval_s=300.0,
                    required=3, noise_cooloff_s=1800.0, fade_cooloff_s=3600.0)
        assert main.rally_fade_revoke_verdict(boot_grace=True, noisy=False, **base) == {
            "action": "revoke_boot", "cooloff_s": 0.0}
        assert main.rally_fade_revoke_verdict(boot_grace=False, noisy=True, **base) == {
            "action": "revoke_noise", "cooloff_s": 0.0}
        assert main.rally_fade_revoke_verdict(boot_grace=False, noisy=False, **base) == {
            "action": "revoke_fade", "cooloff_s": 7200.0}   # legacy 2h hardcode
        assert s["count"] == 0   # legacy never accumulates

    def test_enabled_boot_revoke_no_cooloff(self):
        v = main.rally_fade_revoke_verdict(
            enabled=True, streak=_streak(), now=1000.0, interval_s=300.0,
            required=3, boot_grace=True, noisy=False,
            noise_cooloff_s=1800.0, fade_cooloff_s=3600.0)
        assert v == {"action": "revoke_boot", "cooloff_s": 0.0}

    def test_enabled_noise_revoke_uses_noise_cooloff(self):
        v = main.rally_fade_revoke_verdict(
            enabled=True, streak=_streak(), now=1000.0, interval_s=300.0,
            required=3, boot_grace=False, noisy=True,
            noise_cooloff_s=1800.0, fade_cooloff_s=3600.0)
        assert v == {"action": "revoke_noise", "cooloff_s": 1800.0}

    def test_enabled_accumulate_then_revoke_with_fade_cooloff(self):
        s = _streak()
        base = dict(enabled=True, streak=s, interval_s=300.0, required=3,
                    boot_grace=False, noisy=False,
                    noise_cooloff_s=1800.0, fade_cooloff_s=3600.0)
        assert main.rally_fade_revoke_verdict(now=1000.0, **base)["action"] == "accumulate"
        assert main.rally_fade_revoke_verdict(now=1100.0, **base)["action"] == "skip"
        assert main.rally_fade_revoke_verdict(now=1400.0, **base)["action"] == "accumulate"
        v = main.rally_fade_revoke_verdict(now=1800.0, **base)
        assert v == {"action": "revoke_fade", "cooloff_s": 3600.0}
        assert s["count"] == 0
