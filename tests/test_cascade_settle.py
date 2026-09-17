"""Cascade post-print settle band (2026-09-16, Governor-endorsed,
cascade-post-print-settle-hole-0916) + calendar-guard observability (#67).

Replay evidence: [0-30m) post-print n=11 avg -$0.688 vs control n=188 avg
-$0.149; [30-120m) and [2-12h) arms POSITIVE — the band is exactly 30 min.
"""
import pathlib

from main import _cascade_settle_blocked


class TestSettleBandHelper:
    def test_none_is_fail_open(self):
        assert _cascade_settle_blocked(None, 0.5) is False

    def test_zero_is_inside_band(self):
        assert _cascade_settle_blocked(0.0, 0.5) is True

    def test_just_inside_band(self):
        assert _cascade_settle_blocked(0.4999, 0.5) is True

    def test_boundary_is_outside(self):
        assert _cascade_settle_blocked(0.5, 0.5) is False

    def test_well_outside(self):
        assert _cascade_settle_blocked(1.9, 0.5) is False

    def test_string_numeric_accepted(self):
        assert _cascade_settle_blocked("0.2", 0.5) is True

    def test_string_junk_fail_open(self):
        assert _cascade_settle_blocked("junk", 0.5) is False

    def test_negative_treated_as_inside(self):
        assert _cascade_settle_blocked(-0.1, 0.5) is True


class TestWiring:
    """Source-level pins: the splices exist and the silent swallows are gone."""

    SRC = pathlib.Path(__file__).resolve().parent.parent / "main.py"

    @classmethod
    def _text(cls):
        return cls.SRC.read_text()

    def test_settle_event_wired_both_paths(self):
        assert self._text().count("signal_rejected_calendar_settle") == 2

    def test_guard_error_event_wired_both_paths(self):
        assert self._text().count("cascade_calendar_guard_error") == 2

    def test_shadow_gate_registered(self):
        sj = (self.SRC.parent / "intelligence" / "shadow_journal.py").read_text()
        assert '"signal_rejected_calendar_settle": "calendar_settle"' in sj

    def test_no_silent_swallow_after_calendar_guard(self):
        text = self._text()
        marker = "Macro-print calendar block (2026-09-04"
        idx = 0
        hits = 0
        while True:
            idx = text.find(marker, idx)
            if idx == -1:
                break
            hits += 1
            window = text[idx:idx + 3000]
            assert "except Exception:\n" not in window.replace(
                "except Exception as _cal_err:", ""
            ) or "pass" not in window, "silent except:pass near calendar guard"
            idx += len(marker)
        assert hits == 2
