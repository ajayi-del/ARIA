"""Wiring pin for the fearful-mode elimination (2026-10-16, Governor order
"eliminate the fearful mode bot is not trading").

The 30-min meta-cognition pulse's fearful mode wrote meta_size_mult 0.5 on
last-50 WR < 0.35. The WR window is polluted by scalper $8 churn closes, so
the reflex halved the whole book on contaminated evidence. Default OFF:
meta_fearful_mode_enabled=False means mode never becomes "fearful";
True restores the legacy reflex bit-for-bit.
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MAIN = (_ROOT / "main.py").read_text()
_CONFIG = (_ROOT / "core" / "config.py").read_text()


def test_config_knob_default_off():
    assert "meta_fearful_mode_enabled: bool = False" in _CONFIG


def test_pulse_fearful_branch_gated():
    m = re.search(
        r'elif \(wr < 0\.35 and n > 10\s*\n\s*and getattr\(config, '
        r'"meta_fearful_mode_enabled", False\)\):',
        _MAIN)
    assert m, "fearful branch must be gated on the config knob"


def test_fearful_actuation_unchanged():
    # The fearful branch's actuation is untouched — only its arming is gated.
    assert '_param_store.set_ai_param("meta_size_mult", 0.5, ttl_seconds=1800)' in _MAIN


def test_other_modes_ungated():
    # overconfident and distracted stay unconditional.
    assert re.search(r'if wr > 0\.75 and n > 10:\s*\n\s*mode = "overconfident"', _MAIN)
    assert re.search(r'elif dust_ratio > 0\.15:\s*\n\s*mode = "distracted"', _MAIN)
