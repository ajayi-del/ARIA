"""Equity-open lead brain — behavioral pins (2026-10-17 Governor directive,
STOCK EXECUTION ARCHITECTURE: USTECH100 09:30 ET cash-open lead over BTC).

Window 09:30-10:30 ET weekdays (half-open [09:30, 10:30)); DST via
zoneinfo with fixed timestamps around the US transitions; noise floor 10bp;
min window 5min; leader needs >5bp magnitude edge. Textual pins for the
env kill switch and the splice event name.

Verified ET mappings (zoneinfo):
  2026-03-06T14:35Z = 09:35 Fri EST (last Friday before spring-forward)
  2026-03-09T13:35Z = 09:35 Mon EDT (first Monday after)
  2026-10-30T13:35Z = 09:35 Fri EDT (last Friday before fall-back)
  2026-11-02T14:35Z = 09:35 Mon EST (first Monday after)
  2026-11-02T13:35Z = 08:35 Mon EST (out — pre-open in winter)
  2026-10-17T13:35Z = 09:35 Sat EDT (weekend — closed)
  2026-10-19T13:30Z = 09:30 Mon EDT (open boundary, in)
  2026-10-19T14:29Z = 10:29 Mon EDT (in)
  2026-10-19T14:30Z = 10:30 Mon EDT (close boundary, out)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence import equity_open_lead as eol  # noqa: E402


def _ts(iso: str) -> float:
    import datetime as _dt
    return _dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


class _Env:
    """Scoped EQUITY_OPEN_LEAD_ENABLED setter/restorer."""

    def __init__(self, value):
        self._value = value
        self._old = os.environ.get("EQUITY_OPEN_LEAD_ENABLED")

    def __enter__(self):
        if self._value is None:
            os.environ.pop("EQUITY_OPEN_LEAD_ENABLED", None)
        else:
            os.environ["EQUITY_OPEN_LEAD_ENABLED"] = self._value
        return self

    def __exit__(self, *a):
        if self._old is None:
            os.environ.pop("EQUITY_OPEN_LEAD_ENABLED", None)
        else:
            os.environ["EQUITY_OPEN_LEAD_ENABLED"] = self._old


class TestWindow:
    def test_in_window_summer_edt(self):
        assert eol.is_equity_open_window(_ts("2026-10-19T13:35:00Z")) is True

    def test_open_boundary_inclusive(self):
        assert eol.is_equity_open_window(_ts("2026-10-19T13:30:00Z")) is True

    def test_last_minute_in(self):
        assert eol.is_equity_open_window(_ts("2026-10-19T14:29:00Z")) is True

    def test_close_boundary_exclusive(self):
        assert eol.is_equity_open_window(_ts("2026-10-19T14:30:00Z")) is False

    def test_pre_open_out(self):
        # 13:29Z = 09:29 EDT.
        assert eol.is_equity_open_window(_ts("2026-10-19T13:29:00Z")) is False

    def test_weekend_closed_saturday(self):
        assert eol.is_equity_open_window(_ts("2026-10-17T13:35:00Z")) is False

    def test_weekend_closed_sunday(self):
        assert eol.is_equity_open_window(_ts("2026-10-18T13:35:00Z")) is False

    def test_dst_spring_before_transition_est(self):
        # Friday Mar 6 2026, still EST: 09:35 ET = 14:35Z.
        assert eol.is_equity_open_window(_ts("2026-03-06T14:35:00Z")) is True

    def test_dst_spring_after_transition_edt(self):
        # Monday Mar 9 2026, now EDT: 09:35 ET = 13:35Z (one hour earlier).
        assert eol.is_equity_open_window(_ts("2026-03-09T13:35:00Z")) is True
        # The winter-time UTC reading is now 10:35 EDT -> out. The offset
        # flipped, so a hardcoded UTC window would be wrong in one season.
        assert eol.is_equity_open_window(_ts("2026-03-09T14:35:00Z")) is False

    def test_dst_fall_before_transition_edt(self):
        # Friday Oct 30 2026, still EDT: 09:35 ET = 13:35Z.
        assert eol.is_equity_open_window(_ts("2026-10-30T13:35:00Z")) is True

    def test_dst_fall_after_transition_est(self):
        # Monday Nov 2 2026, now EST: 09:35 ET = 14:35Z (one hour later)...
        assert eol.is_equity_open_window(_ts("2026-11-02T14:35:00Z")) is True
        # ...and the summer-time UTC reading is now 08:35 ET -> out.
        assert eol.is_equity_open_window(_ts("2026-11-02T13:35:00Z")) is False

    def test_bad_input_closed(self):
        assert eol.is_equity_open_window(None) is False
        assert eol.is_equity_open_window("garbage") is False

    def test_kill_switch_off_closes_window(self):
        with _Env("false"):
            assert eol.is_equity_open_window(
                _ts("2026-10-19T13:35:00Z")) is False
        with _Env(None):
            assert eol.is_equity_open_window(
                _ts("2026-10-19T13:35:00Z")) is True


class TestScore:
    def test_equity_leads(self):
        r = eol.lead_lag_score(0.6, 0.1, 15.0)
        assert r["divergence"] == 0.5 and r["leader"] == "equity"
        assert r["abstain_reason"] is None

    def test_crypto_leads(self):
        r = eol.lead_lag_score(0.1, -0.8, 15.0)
        assert r["divergence"] == 0.9 and r["leader"] == "crypto"

    def test_lockstep_no_leader(self):
        r = eol.lead_lag_score(0.42, 0.40, 15.0)
        assert r["leader"] == "none" and r["divergence"] == 0.02

    def test_divergence_floor_boundary(self):
        # |u| - |b| exactly 0.05 -> not beyond the floor -> none.
        r = eol.lead_lag_score(0.15, 0.10, 15.0)
        assert r["leader"] == "none"
        # Just beyond -> equity.
        r = eol.lead_lag_score(0.151, 0.10, 15.0)
        assert r["leader"] == "equity"

    def test_noise_floor_abstain(self):
        r = eol.lead_lag_score(0.05, -0.03, 15.0)
        assert r["divergence"] is None and r["leader"] is None
        assert r["abstain_reason"] == "below_noise_floor"

    def test_noise_floor_boundary(self):
        # |u| exactly 0.10 -> not below the floor -> scores.
        r = eol.lead_lag_score(0.10, 0.0, 15.0)
        assert r["abstain_reason"] is None

    def test_tiny_window_abstain(self):
        r = eol.lead_lag_score(0.6, 0.1, 3.0)
        assert r["abstain_reason"] == "window_too_small"
        # Boundary: exactly 5min scores.
        r = eol.lead_lag_score(0.6, 0.1, 5.0)
        assert r["abstain_reason"] is None

    def test_bad_inputs(self):
        assert eol.lead_lag_score(None, 0.1, 15.0)[
            "abstain_reason"] == "bad_input"
        assert eol.lead_lag_score(0.1, "x", 15.0)[
            "abstain_reason"] == "bad_input"
        assert eol.lead_lag_score(0.1, 0.1, None)[
            "abstain_reason"] == "bad_input"

    def test_kill_switch_off_abstains(self):
        with _Env("false"):
            r = eol.lead_lag_score(0.6, 0.1, 15.0)
            assert r["divergence"] is None
            assert r["abstain_reason"] == "disabled"


class TestTextualPins:
    def test_event_name(self):
        assert eol.EVENT_NAME == "equity_open_lead_score"

    def test_kill_switch_env_name_and_default(self):
        with _Env(None):
            assert eol.equity_open_lead_enabled() is True
        with _Env("off"):
            assert eol.equity_open_lead_enabled() is False

    def test_module_source_carries_doctrine(self):
        src = os.path.join(os.path.dirname(eol.__file__),
                           "equity_open_lead.py")
        text = open(src, encoding="utf-8").read()
        assert "EQUITY_OPEN_LEAD_ENABLED" in text
        assert "equity_open_lead_score" in text
        assert "America/New_York" in text
