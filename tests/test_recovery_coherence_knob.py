"""Pins for the 2026-09-23 recovery coherence floor knob (Governor directive:
"reduce coherence score for trades to fire especially on sodex sleeve").

RECOVERY_COHERENCE 5.6 was the binding SoDEX constraint (Aster is
DD-recovery-exempt via _recovery_params_for, so the floor only ever bound
the SoDEX sleeve). New config knob recovery_coherence_min default 4.5;
config objects WITHOUT the attribute (older stubs, legacy tests) get 5.6
bit-for-bit. Env RECOVERY_COHERENCE_MIN=5.6 restores the old floor exactly.
"""
from memory.adaptive_calibrator import AdaptiveCalibrator, RECOVERY_COHERENCE
from core.config import Settings


class _LegacyCfg:
    """Config object without the knob — the pre-2026-09-23 shape."""
    pass


class _KnobCfg:
    recovery_coherence_min = 4.5


def _in_recovery(cal):
    cal.update_drawdown(0.05)   # above the 3% DD trigger
    assert cal.is_in_recovery()
    return cal


def test_settings_default_knob_is_4_5():
    assert Settings().recovery_coherence_min == 4.5


def test_legacy_config_without_attr_gets_5_6():
    cal = _in_recovery(AdaptiveCalibrator(_LegacyCfg()))
    assert cal._recovery_coherence_min() == RECOVERY_COHERENCE == 5.6
    assert cal.get_recovery_params()["coherence_min"] == 5.6
    assert cal.get_coherence_minimum() == 5.6


def test_legacy_config_keeps_max_semantics():
    # Legacy bit-for-bit: without the knob, a loss-streak-raised adaptive
    # floor ABOVE 5.6 still wins in recovery (the old max() formula).
    cal = AdaptiveCalibrator(_LegacyCfg())
    cal._coherence_min = 6.0
    _in_recovery(cal)
    assert cal.get_coherence_minimum() == 6.0


def test_knobbed_config_gets_4_5():
    cal = _in_recovery(AdaptiveCalibrator(_KnobCfg()))
    assert cal._recovery_coherence_min() == 4.5
    assert cal.get_recovery_params()["coherence_min"] == 4.5
    assert cal.get_coherence_minimum() == 4.5


def test_knobbed_config_floor_is_knob_not_max():
    # Governor 2026-09-23 semantics: with the knob present, recovery's floor
    # IS the knob — the max() with a higher adaptive base would mute the
    # knob on books whose env base floor sits above it (server
    # MIN_COHERENCE=5.0 — the exact book this knob was ordered for).
    cal = AdaptiveCalibrator(_KnobCfg())
    cal._coherence_min = 5.0
    _in_recovery(cal)
    assert cal.get_coherence_minimum() == 4.5


def test_live_settings_path_reads_4_5():
    cal = _in_recovery(AdaptiveCalibrator(Settings()))
    assert cal.get_recovery_params()["coherence_min"] == 4.5


def test_no_recovery_no_recovery_floor():
    cal = AdaptiveCalibrator(_KnobCfg())
    assert cal.get_recovery_params() == {}
    assert cal.get_coherence_minimum() == cal._coherence_min  # adaptive base, no recovery overlay


def test_env_override_restores_legacy_floor(monkeypatch):
    monkeypatch.setenv("RECOVERY_COHERENCE_MIN", "5.6")
    assert Settings().recovery_coherence_min == 5.6
