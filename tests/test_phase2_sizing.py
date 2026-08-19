"""Phase 2a/2b: conviction-proportional campaign floor + trend-day TP room knobs."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import _campaign_conviction_floor  # noqa: E402


class _Cfg:
    campaign_min_notional_usd = 250.0
    campaign_conviction_floor_enabled = True


def test_high_conviction_gets_full_floor():
    assert _campaign_conviction_floor(_Cfg, 9.69) == 250.0
    assert _campaign_conviction_floor(_Cfg, 4.5) == 250.0


def test_mid_conviction_gets_three_quarter_floor():
    assert _campaign_conviction_floor(_Cfg, 3.5) == 187.5
    assert _campaign_conviction_floor(_Cfg, 3.0) == 187.5


def test_low_conviction_gets_half_floor():
    assert _campaign_conviction_floor(_Cfg, 2.9) == 125.0
    assert _campaign_conviction_floor(_Cfg, 0.0) == 125.0


def test_inversion_property_low_never_outsizes_high():
    for low in (0.0, 1.0, 2.0, 3.0, 3.5, 4.4):
        for high in (4.5, 6.0, 9.7):
            assert _campaign_conviction_floor(_Cfg, low) <= _campaign_conviction_floor(_Cfg, high)


def test_kill_switch_restores_flat_floor():
    class _Off(_Cfg):
        campaign_conviction_floor_enabled = False
    assert _campaign_conviction_floor(_Off, 2.0) == 250.0
    assert _campaign_conviction_floor(_Off, 9.7) == 250.0


def test_missing_attrs_default_to_scaled_floor():
    # Bare config: knob defaults to enabled → conviction-scaled floor.
    class _Bare:
        pass
    assert _campaign_conviction_floor(_Bare, 2.0) == 125.0
    assert _campaign_conviction_floor(_Bare, 9.7) == 250.0


def test_phase2b_knobs_exist_with_conservative_defaults():
    from core.config import Settings
    s = Settings()
    assert s.trend_day_tp_room_enabled is True
    assert s.trend_day_winner_escape_mult == 1.5
    assert s.campaign_conviction_floor_enabled is True
