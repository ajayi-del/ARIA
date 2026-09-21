"""Fix 2 pins (2026-09-20): post-boot entry-rate throttle.

Restart-window census: 11 entries, 10 losers, -$10.89, 9 in one burst.
The throttle caps APPROVED entries inside the post-boot window; the salvo
retracement filter owns price geometry, this owns raw rate.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402

BOOT = 1_000_000.0


class TestPostBootThrottle:
    def setup_method(self):
        main._post_boot_entries.clear()

    def test_under_limit_passes(self):
        main._post_boot_entries.extend([BOOT + 1, BOOT + 2])
        assert main._post_boot_throttle_ok(BOOT + 100, BOOT, 900.0, 3) is True

    def test_at_limit_rejects(self):
        main._post_boot_entries.extend([BOOT + 1, BOOT + 2, BOOT + 3])
        assert main._post_boot_throttle_ok(BOOT + 100, BOOT, 900.0, 3) is False

    def test_window_expired_passes(self):
        main._post_boot_entries.extend([BOOT + 1, BOOT + 2, BOOT + 3])
        assert main._post_boot_throttle_ok(BOOT + 900.0, BOOT, 900.0, 3) is True

    def test_only_post_boot_approvals_count(self):
        # pre-boot timestamps (stale/skewed) never consume the cap
        main._post_boot_entries.extend([BOOT - 50, BOOT - 10, BOOT - 1])
        assert main._post_boot_throttle_ok(BOOT + 100, BOOT, 900.0, 3) is True

    def test_kill_switch_false_is_legacy(self):
        main._post_boot_entries.extend([BOOT + 1, BOOT + 2, BOOT + 3])
        assert main._post_boot_throttle_verdict(
            False, BOOT + 100, BOOT, 900.0, 3) is None

    def test_verdict_names_the_reject(self):
        main._post_boot_entries.extend([BOOT + 1, BOOT + 2, BOOT + 3])
        assert main._post_boot_throttle_verdict(
            True, BOOT + 100, BOOT, 900.0, 3) == "reject"
        assert main._post_boot_throttle_verdict(
            True, BOOT + 100, BOOT, 900.0, 4) is None
        assert main._post_boot_throttle_verdict(
            True, BOOT + 901, BOOT, 900.0, 3) is None

    def test_record_prunes_entries_beyond_window(self):
        for i in range(300):
            main._post_boot_record_entry(BOOT + i * 10, 900.0)  # spans 3000s
        assert len(main._post_boot_entries) <= 257
        # first-window entries are pruned once the list overflows
        assert min(main._post_boot_entries) > BOOT + 900.0

    def test_wiring_event_and_gate_names_present(self):
        src = inspect.getsource(main)
        assert "signal_rejected_post_boot_throttle" in src
        assert 'gate="post_boot_throttle"' in src
