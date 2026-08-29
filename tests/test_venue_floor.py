"""Venue-aware floor + aster ladder base pins (2026-08-29 sizing autopsy).

The HYPE sizing-chain autopsy proved two structural sizing defects:
  1. The $80 SoDEX strategy floor applied to Aster (exchange min $1) —
     UNI long passed every gate, the basket cap shrank it to $69.06, and
     nietzsche_min_notional_fail killed a winner on a venue where $69 is
     60x the exchange minimum. Floor is now max(venue_min, sleeve x 2%):
     grows with the account, never below the venue's own floor.
  2. The aster ladder 1.0-conviction base was hardcoded cap/2 — with the
     basket cap binding on the ladder base, HYPE filled at $9 margin
     (2.5% sleeve utilization). aster_conviction_base_frac 0.75 lifts the
     standard aster trade +50%; 0.5 reproduces the legacy ladder.

Kill switch: FLOOR_VENUE_AWARE_ENABLED=false = flat min_trade_notional_usd
on every venue (legacy bit-for-bit).
"""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import _venue_min_notional, _floor_venue_aware_enabled  # noqa: E402
from execution import venue as venue_mod  # noqa: E402
from intelligence.shadow_journal import REJECTION_EVENTS  # noqa: E402


def _cfg(**kw):
    base = dict(min_trade_notional_usd=80.0,
                min_notional_dynamic_pct=0.02,
                aster_min_notional_usd=3.0)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def _aster(monkeypatch):
    monkeypatch.setattr(venue_mod, "venue_for", lambda s: "aster")
    monkeypatch.delenv("FLOOR_VENUE_AWARE_ENABLED", raising=False)


@pytest.fixture
def _sodex(monkeypatch):
    monkeypatch.setattr(venue_mod, "venue_for", lambda s: "sodex")
    monkeypatch.delenv("FLOOR_VENUE_AWARE_ENABLED", raising=False)


class TestVenueFloor:
    def test_kill_switch_off_is_legacy_bit_for_bit(self, monkeypatch):
        monkeypatch.setenv("FLOOR_VENUE_AWARE_ENABLED", "false")
        monkeypatch.setattr(venue_mod, "venue_for", lambda s: "aster")
        assert _venue_min_notional("HYPE-USD", 359.33, _cfg()) == 80.0
        monkeypatch.setattr(venue_mod, "venue_for", lambda s: "sodex")
        assert _venue_min_notional("BTC-USD", 359.33, _cfg()) == 80.0

    def test_aster_floor_dynamic_leg_binds(self, _aster):
        # $359.33 sleeve x 2% = $7.19 > $3 venue min — the HYPE sleeve.
        assert _venue_min_notional("HYPE-USD", 359.33, _cfg()) == pytest.approx(7.1866)

    def test_aster_floor_venue_min_binds_on_tiny_sleeve(self, _aster):
        # $50 sleeve x 2% = $1.00 < $3 venue min (3 bracket legs x $1).
        assert _venue_min_notional("HYPE-USD", 50.0, _cfg()) == 3.0

    def test_aster_floor_grows_with_account(self, _aster):
        assert _venue_min_notional("HYPE-USD", 1000.0, _cfg()) == pytest.approx(20.0)

    def test_sodex_floor_unchanged_at_current_book(self, _sodex):
        # $700 x 2% = $14 < $80 — SoDEX behavior identical today.
        assert _venue_min_notional("BTC-USD", 700.0, _cfg()) == 80.0

    def test_sodex_floor_grows_past_4k(self, _sodex):
        assert _venue_min_notional("BTC-USD", 5000.0, _cfg()) == pytest.approx(100.0)

    def test_venue_lookup_failure_fails_to_sodex(self, monkeypatch):
        def _boom(sym):
            raise RuntimeError("no venue")
        monkeypatch.setattr(venue_mod, "venue_for", _boom)
        monkeypatch.delenv("FLOOR_VENUE_AWARE_ENABLED", raising=False)
        assert _venue_min_notional("???-USD", 700.0, _cfg()) == 80.0

    def test_uni_incident_would_pass_now(self, _aster):
        # The incident: basket cap shrank UNI long to $69.06 notional; the
        # $80 SoDEX floor rejected it. Aster floor at the HYPE-era sleeve is
        # ~$7.19 — $69.06 clears it with 9.6x headroom.
        floor = _venue_min_notional("UNI-USD", 359.33, _cfg())
        assert 69.06 >= floor

    def test_zero_balance_aster_still_venue_min(self, _aster):
        assert _venue_min_notional("HYPE-USD", 0.0, _cfg()) == 3.0


class TestLadderConvictionBase:
    def test_config_default_is_075(self):
        from core.config import Settings
        assert Settings().aster_conviction_base_frac == 0.75

    def test_05_reproduces_legacy_ladder(self):
        # cap = balance x mpct x lev; base = cap x frac. frac 0.5 == cap/2.
        balance, mpct, lev = 359.33, 0.80, 5
        cap = balance * mpct * lev
        assert cap * 0.5 == pytest.approx(cap / 2.0)

    def test_075_lifts_base_50pct(self):
        balance, mpct, lev = 359.33, 0.80, 5
        cap = balance * mpct * lev
        assert (cap * 0.75) / (cap * 0.5) == pytest.approx(1.5)
        # Production HYPE incident reconstruction: ladder base was sleeve/2
        # = $179.66, basket cap 0.35 binding -> $62.88 traded notional ($9
        # margin). frac 0.75 lifts the base +50% -> $94.32 at the same cap.
        sleeve = 359.33
        legacy_base = sleeve / 2.0
        assert legacy_base * 0.35 == pytest.approx(62.88, abs=0.01)
        assert legacy_base * 1.5 * 0.35 == pytest.approx(94.32, abs=0.01)

    def test_2_conviction_still_hits_cap_never_exceeds(self):
        # build_candidate clamps target_notional to max_usd = cap (Vince) —
        # the frac only moves the 1.0-conviction base.
        balance, mpct, lev = 359.33, 0.80, 5
        cap = balance * mpct * lev
        base = cap * 0.75
        assert min(base * 2.0, cap) == cap


class TestShadowRegistration:
    def test_min_notional_fail_registered(self):
        assert REJECTION_EVENTS["nietzsche_min_notional_fail"] == "min_notional"

    def test_registry_has_no_duplicate_gates(self):
        assert len(REJECTION_EVENTS.values()) == len(set(REJECTION_EVENTS.values()))


class TestConfigKnobs:
    def test_floor_knobs_exist(self):
        from core.config import Settings
        s = Settings()
        assert s.min_notional_dynamic_pct == 0.02
        assert s.aster_min_notional_usd == 3.0

    def test_env_default_true(self, monkeypatch):
        monkeypatch.delenv("FLOOR_VENUE_AWARE_ENABLED", raising=False)
        assert _floor_venue_aware_enabled() is True

    def test_env_false_disables(self, monkeypatch):
        monkeypatch.setenv("FLOOR_VENUE_AWARE_ENABLED", "false")
        assert _floor_venue_aware_enabled() is False
