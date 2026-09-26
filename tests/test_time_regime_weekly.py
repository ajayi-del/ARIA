"""Time-regime weekly-leg removal pins (Governor 2026-09-26 "remove the
weekly calendar caution").

Default (env unset): sections 2-3 neutralize — no Sat/Sun 0.90x0.90 tax,
no Sunday-evening 0.80 / Monday-open 0.85 crypto tax, no weekday confidence
skew. Monthly cycle + macro-event override still bind.
TIME_REGIME_WEEKLY_CAUTION_ENABLED=true = legacy bit-for-bit.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk_calendar.time_regime import evaluate  # noqa: E402


def _dt(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=timezone.utc)


SAT = _dt(2026, 9, 19)          # Saturday, mid-month (11 days left)
SUN_LATE = _dt(2026, 9, 20, 21)  # Sunday 21:00 UTC, mid-month
MON_EARLY = _dt(2026, 9, 21, 3)  # Monday 03:00 UTC, mid-month
WED = _dt(2026, 9, 23)          # Wednesday, mid-month


def test_default_saturday_no_weekly_tax(monkeypatch):
    monkeypatch.delenv("TIME_REGIME_WEEKLY_CAUTION_ENABLED", raising=False)
    r = evaluate(SAT)
    assert r.risk_multiplier == 0.90       # monthly mid_month leg still binds
    assert r.confidence_multiplier == 1.00  # weekly 0.90 tax gone
    assert "weekly_leg_disabled" in r.notes


def test_default_sunday_evening_no_crypto_tax(monkeypatch):
    monkeypatch.delenv("TIME_REGIME_WEEKLY_CAUTION_ENABLED", raising=False)
    r = evaluate(SUN_LATE)
    # legacy: min(0.90, 0.90, 0.80) = 0.80; now: monthly mid_month 0.90 only
    assert r.risk_multiplier == 0.90
    assert r.confidence_multiplier == 1.00


def test_default_monday_open_no_extra_caution(monkeypatch):
    monkeypatch.delenv("TIME_REGIME_WEEKLY_CAUTION_ENABLED", raising=False)
    r = evaluate(MON_EARLY)
    # legacy: min(0.90, 1.00, 0.85)=0.85, conf 0.90; now: 0.90 / 1.00
    assert r.risk_multiplier == 0.90
    assert r.confidence_multiplier == 1.00


def test_default_tuewed_no_confidence_boost(monkeypatch):
    # removal is symmetric: the 1.10 Tue/Wed boost goes too (no free offense)
    monkeypatch.delenv("TIME_REGIME_WEEKLY_CAUTION_ENABLED", raising=False)
    r = evaluate(WED)
    assert r.confidence_multiplier == 1.00


def test_default_monthly_cycle_still_binds(monkeypatch):
    monkeypatch.delenv("TIME_REGIME_WEEKLY_CAUTION_ENABLED", raising=False)
    # month_start (day <= 5): monthly 1.15 — but base_risk = min(monthly,
    # weekly, crypto) = min(1.15, 1.00, 1.0) = 1.0. The 1.15 boost has always
    # been min-clamped away (legacy too: weekly_risk was 1.00 most days).
    r = evaluate(_dt(2026, 9, 1))   # Tuesday Sep 1
    assert r.risk_multiplier == 1.0
    # month_end (days_left < 5): 0.85 binds through the min-compose
    r2 = evaluate(_dt(2026, 9, 28))  # Monday Sep 28, 2 days left
    assert r2.risk_multiplier == 0.85


def test_default_macro_block_still_binds(monkeypatch):
    monkeypatch.delenv("TIME_REGIME_WEEKLY_CAUTION_ENABLED", raising=False)
    r = evaluate(SAT, event_type="FOMC", hours_to_event=1.0)
    assert r.risk_multiplier == 0.0
    assert r.cooldown_multiplier == 2.0


def test_legacy_env_true_saturday(monkeypatch):
    monkeypatch.setenv("TIME_REGIME_WEEKLY_CAUTION_ENABLED", "true")
    r = evaluate(SAT)
    assert r.risk_multiplier == 0.90       # min(0.90, 0.90, 1.0)
    assert r.confidence_multiplier == 0.90
    assert "saturday" in r.notes


def test_legacy_env_true_sunday_evening(monkeypatch):
    monkeypatch.setenv("TIME_REGIME_WEEKLY_CAUTION_ENABLED", "true")
    r = evaluate(SUN_LATE)
    assert r.risk_multiplier == 0.80       # min(0.90, 0.90, 0.80)
    assert r.confidence_multiplier == 0.90


def test_legacy_env_true_monday_open(monkeypatch):
    monkeypatch.setenv("TIME_REGIME_WEEKLY_CAUTION_ENABLED", "true")
    r = evaluate(MON_EARLY)
    assert r.risk_multiplier == 0.85
    assert r.confidence_multiplier == 0.90


def test_legacy_env_true_tuewed_boost(monkeypatch):
    monkeypatch.setenv("TIME_REGIME_WEEKLY_CAUTION_ENABLED", "true")
    r = evaluate(WED)
    assert r.confidence_multiplier == 1.10


def test_phase_label_preserved_when_disabled(monkeypatch):
    # telemetry continuity: phase string still names month + weekday phase
    monkeypatch.delenv("TIME_REGIME_WEEKLY_CAUTION_ENABLED", raising=False)
    assert evaluate(SAT).phase == "mid_month_saturday"
    assert evaluate(SUN_LATE).phase == "mid_month_sunday"
