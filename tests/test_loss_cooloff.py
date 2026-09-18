"""Loss-cooloff hardening pins (Governor 2026-09-18, Phase 2).

Root cause: `loss_cut_cooloff:{symbol}` armed only on conviction-decay
abandons and treasury loss-cuts — plain stop-loss closes never armed it, and
the direction-loss strike streak reset after a hardcoded 7200s gap, so losses
spaced >2h apart each read as strike 1 = a 5-min cooldown. The churn guard
structurally could not fire.

The arming splice and the decay comparison live inside main()'s `_record_close`
closure, which is not importable without refactor — this file pins the level
that IS importable: the config defaults exist, parse, and carry the approved
values (kill switch default ON; decay default 6h, was a hardcoded 2h).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Settings  # noqa: E402


def test_loss_cooloff_on_any_loss_enabled_default_true():
    assert Settings().loss_cooloff_on_any_loss_enabled is True


def test_direction_loss_strike_decay_default_6h():
    s = Settings()
    assert isinstance(s.direction_loss_strike_decay_s, float)
    assert s.direction_loss_strike_decay_s == 21600.0


def test_cross_sleeve_knobs_defaults():
    s = Settings()
    assert s.cross_sleeve_veto_enabled is True
    assert s.cross_sleeve_max_gross_usd_per_symbol == 0.0
