"""Risk-parity sizing (Carver/Van Tharp): ratio math, clamps, abstains."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.risk_parity import (  # noqa: E402
    risk_parity_ratio, risk_parity_enabled,
    REF_STOP_PCT, MIN_RATIO, MAX_RATIO,
)


def test_stop_at_reference_distance_is_bit_for_bit():
    # 1% stop at ref 1% → ratio exactly 1.0 → legacy size unchanged
    assert risk_parity_ratio(100.0, 99.0) == 1.0


def test_tight_stop_earns_more_notional():
    # 0.5% stop → ratio 2.0 (twice the notional, same risk)
    assert risk_parity_ratio(100.0, 99.5) == 2.0


def test_wide_stop_loses_notional():
    # 2% stop → ratio 0.5
    assert risk_parity_ratio(100.0, 98.0) == 0.5


def test_short_side_symmetric():
    # short: stop above entry — absolute distance governs
    assert risk_parity_ratio(100.0, 100.5) == 2.0
    assert risk_parity_ratio(100.0, 102.0) == 0.5


def test_tight_stop_clamped_at_max_ratio():
    # 0.1% stop → raw ratio 10 → clamped to MAX_RATIO
    assert risk_parity_ratio(100.0, 99.9) == MAX_RATIO


def test_wide_stop_clamped_at_min_ratio():
    # 10% stop → raw ratio 0.1 → clamped to MIN_RATIO
    assert risk_parity_ratio(100.0, 90.0) == MIN_RATIO


def test_missing_or_degenerate_inputs_abstain():
    assert risk_parity_ratio(100.0, None) is None
    assert risk_parity_ratio(None, 99.0) is None
    assert risk_parity_ratio(100.0, 0.0) is None
    assert risk_parity_ratio(0.0, 99.0) is None
    assert risk_parity_ratio(-5.0, 99.0) is None
    assert risk_parity_ratio(100.0, 100.0) is None   # zero distance
    assert risk_parity_ratio("abc", 99.0) is None


def test_sub_1bp_stop_abstains_as_noise():
    # 0.005% distance < 1bp floor → data noise, never resize
    assert risk_parity_ratio(100.0, 99.995) is None


def test_kill_switch_default_true():
    os.environ.pop("RISK_PARITY_SIZING_ENABLED", None)
    assert risk_parity_enabled() is True


def test_kill_switch_env_false():
    os.environ["RISK_PARITY_SIZING_ENABLED"] = "false"
    try:
        assert risk_parity_enabled() is False
    finally:
        os.environ.pop("RISK_PARITY_SIZING_ENABLED", None)


def test_constants_match_doctrine():
    assert REF_STOP_PCT == 0.01
    assert MIN_RATIO == 0.25
    assert MAX_RATIO == 3.0
