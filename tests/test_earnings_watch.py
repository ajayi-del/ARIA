"""Earnings watch brain — behavioral pins (2026-10-17 Governor directive,
STOCK EXECUTION ARCHITECTURE: earnings proximity x expected move scorer).

Window [T-5d, T+1d]; phases pre/post; abstains on no date / stale calendar
(10-day STALE LAW) / kill switch. Textual pins for the env kill switch and
the splice event name.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence import earnings_watch as ew  # noqa: E402

DAY = 86400.0
T = 1_800_000_000.0  # arbitrary earnings print epoch


class _Env:
    """Scoped EARNINGS_WATCH_ENABLED setter/restorer."""

    def __init__(self, value):
        self._value = value
        self._old = os.environ.get("EARNINGS_WATCH_ENABLED")

    def __enter__(self):
        if self._value is None:
            os.environ.pop("EARNINGS_WATCH_ENABLED", None)
        else:
            os.environ["EARNINGS_WATCH_ENABLED"] = self._value
        return self

    def __exit__(self, *a):
        if self._old is None:
            os.environ.pop("EARNINGS_WATCH_ENABLED", None)
        else:
            os.environ["EARNINGS_WATCH_ENABLED"] = self._old


class TestWindow:
    def test_deep_pre_out_of_window(self):
        # T-10d: known, fresh, but the ramp has not started.
        r = ew.earnings_window_score("MSFT", T, T - 10 * DAY)
        assert r["in_window"] is False and r["phase"] == "pre"
        assert r["score"] == 0.0 and r["abstain_reason"] is None
        assert r["days_to_earnings"] == 10.0

    def test_window_opens_at_t_minus_5d_inclusive(self):
        r = ew.earnings_window_score("MSFT", T, T - 5 * DAY)
        assert r["in_window"] is True and r["phase"] == "pre"
        assert r["days_to_earnings"] == 5.0
        # w = (5 - 5)/5 = 0 at the boundary.
        assert r["score"] == 0.0

    def test_mid_ramp_scores_proximity_times_move(self):
        # T-2.5d, expected move 8%: w = (5-2.5)/5 = 0.5 -> score 4.0.
        r = ew.earnings_window_score("MSFT", T, T - 2.5 * DAY,
                                     expected_move_pct=8.0)
        assert r["in_window"] is True and r["phase"] == "pre"
        assert r["score"] == 4.0

    def test_at_print_full_weight(self):
        r = ew.earnings_window_score("MSFT", T, T, expected_move_pct=6.0)
        assert r["phase"] == "post" and r["in_window"] is True
        assert r["score"] == 6.0

    def test_post_window_half_day(self):
        # T+0.5d: w = (1 - 0.5)/1 = 0.5.
        r = ew.earnings_window_score("MSFT", T, T + 0.5 * DAY,
                                     expected_move_pct=8.0)
        assert r["in_window"] is True and r["phase"] == "post"
        assert r["score"] == 4.0

    def test_window_closes_at_t_plus_1d_inclusive(self):
        r = ew.earnings_window_score("MSFT", T, T + 1 * DAY)
        assert r["in_window"] is True and r["phase"] == "post"
        assert r["score"] == 0.0  # w = 0 at the boundary

    def test_just_past_window_out(self):
        r = ew.earnings_window_score("MSFT", T, T + 1.5 * DAY)
        assert r["in_window"] is False and r["phase"] == "post"
        assert r["score"] == 0.0 and r["abstain_reason"] is None

    def test_default_expected_move_when_none(self):
        # T exactly, no estimate: neutral 4.0 default at full weight.
        r = ew.earnings_window_score("MSFT", T, T - 1 * DAY)
        # w = (5-1)/5 = 0.8; 0.8 x 4.0 = 3.2
        assert r["score"] == 3.2


class TestAbstain:
    def test_no_earnings_date(self):
        r = ew.earnings_window_score("MSFT", None, T)
        assert r["score"] is None and r["abstain_reason"] == "no_earnings_date"
        assert r["in_window"] is False

    def test_stale_calendar_10_day_law(self):
        # Latest known print is 11d in the past with nothing newer: dark.
        r = ew.earnings_window_score("MSFT", T, T + 11 * DAY)
        assert r["score"] is None and r["abstain_reason"] == "stale_calendar"

    def test_stale_boundary_10d_not_stale(self):
        r = ew.earnings_window_score("MSFT", T, T + 10 * DAY)
        assert r["abstain_reason"] is None  # exactly 10d: still fresh

    def test_bad_inputs(self):
        assert ew.earnings_window_score("", T, T)["abstain_reason"] == "bad_input"
        assert ew.earnings_window_score("MSFT", T, "garbage")[
            "abstain_reason"] == "bad_input"
        assert ew.earnings_window_score("MSFT", "garbage", T)[
            "abstain_reason"] == "no_earnings_date"

    def test_kill_switch_off_abstains(self):
        with _Env("false"):
            r = ew.earnings_window_score("MSFT", T, T - 2 * DAY)
            assert r["score"] is None and r["abstain_reason"] == "disabled"
        # And back on with the env cleared -> default true.
        with _Env(None):
            r = ew.earnings_window_score("MSFT", T, T - 2 * DAY)
            assert r["score"] is not None


class TestCalendarCompose:
    def test_reads_existing_calendar_plane(self):
        # NVDA seed 2026-11-19T21:00Z; now 2d before -> in window, pre.
        import datetime as _dt
        ev_ts = _dt.datetime(2026, 11, 19, 21, 0,
                             tzinfo=_dt.timezone.utc).timestamp()
        now = ev_ts - 2 * DAY
        r = ew.earnings_window_score_for("NVDA-USD", now)
        assert r["abstain_reason"] is None and r["in_window"] is True
        assert r["phase"] == "pre" and r["calendar_source"] == "seed_estimate"

    def test_unknown_symbol_abstains_no_date(self):
        r = ew.earnings_window_score_for("ZZZ-USD", T)
        assert r["score"] is None and r["abstain_reason"] == "no_earnings_date"


class TestTextualPins:
    def test_event_name(self):
        assert ew.EVENT_NAME == "earnings_watch_window"

    def test_kill_switch_env_name_and_default(self):
        with _Env(None):
            assert ew.earnings_watch_enabled() is True
        with _Env("0"):
            assert ew.earnings_watch_enabled() is False

    def test_module_source_carries_doctrine(self):
        src = os.path.join(os.path.dirname(ew.__file__), "earnings_watch.py")
        text = open(src, encoding="utf-8").read()
        assert "EARNINGS_WATCH_ENABLED" in text
        assert "earnings_watch_window" in text
