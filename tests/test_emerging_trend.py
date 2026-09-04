"""Pins for the emerging-trend plane (2026-09-03/04 operator directives):
the leading read that releases the base-rate veto on aligned candidates,
blocks counter-direction cascades and elite overrides, and boosts size —
wired through the cybernetic loop (publisher loop → param_store → gates)."""
import os

import pytest

from intelligence.day_type_classifier import emerging_trend_verdict


# ── verdict matrix ───────────────────────────────────────────────────────────

def test_aligned_long_when_both_legs_confirm():
    assert emerging_trend_verdict(2.0, 1.8, "long") == "aligned"


def test_aligned_short_when_both_legs_confirm_down():
    assert emerging_trend_verdict(-2.0, -1.8, "short") == "aligned"


def test_opposed_when_signal_fights_the_trend():
    assert emerging_trend_verdict(2.0, 1.8, "short") == "opposed"
    assert emerging_trend_verdict(-2.0, -1.8, "long") == "opposed"


def test_neutral_when_btc_leadership_sub_threshold():
    # Symbol participates (+2%) but BTC below the 1.5% leadership bar.
    assert emerging_trend_verdict(2.0, 1.2, "long") == "neutral"


def test_neutral_when_symbol_not_participating():
    # BTC confirms but the symbol is flat in the trend's direction.
    assert emerging_trend_verdict(0.4, 2.0, "long") == "neutral"
    assert emerging_trend_verdict(-0.4, -2.0, "short") == "neutral"


def test_neutral_when_symbol_diverges_from_trend():
    # Symbol moving AGAINST the BTC-led trend is not participation.
    assert emerging_trend_verdict(-1.5, 2.0, "long") == "neutral"
    assert emerging_trend_verdict(1.5, -2.0, "short") == "neutral"


def test_neutral_on_missing_data_fail_open():
    assert emerging_trend_verdict(None, 2.0, "long") == "neutral"
    assert emerging_trend_verdict(2.0, None, "long") == "neutral"
    assert emerging_trend_verdict("x", 2.0, "long") == "neutral"
    assert emerging_trend_verdict(2.0, "x", "long") == "neutral"


def test_neutral_on_invalid_signal_direction():
    assert emerging_trend_verdict(2.0, 2.0, "flat") == "neutral"
    assert emerging_trend_verdict(2.0, 2.0, "") == "neutral"


def test_thresholds_are_operator_knobs():
    # Custom thresholds bind: 1.2% sym move passes a 1.0 bar, not a 1.5 bar.
    assert emerging_trend_verdict(1.2, 2.0, "long", sym_threshold=1.0) == "aligned"
    assert emerging_trend_verdict(1.2, 2.0, "long", sym_threshold=1.5) == "neutral"
    assert emerging_trend_verdict(2.0, 1.6, "long", btc_threshold=2.0) == "neutral"


# ── config defaults ──────────────────────────────────────────────────────────

def test_config_knobs_exist_with_directive_defaults():
    from core.config import Settings
    c = Settings()
    assert c.emerging_trend_sym_move_pct == 1.0
    assert c.emerging_trend_btc_move_pct == 1.5
    assert c.base_rate_veto_emerging_trend_exempt_enabled is True
    assert c.emerging_trend_cascade_veto_enabled is True
    assert c.emerging_trend_size_boost_enabled is True
    assert c.emerging_trend_size_boost == 1.25


# ── wiring pins (source-order — the gate sequence is the doctrine) ───────────

_MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "main.py")


def _src():
    with open(_MAIN) as f:
        return f.read()


def test_veto_release_runs_before_the_veto_binds():
    src = _src()
    i_release = src.index("base_rate_veto_emerging_trend_exempt_enabled")
    i_veto = src.index("if _br_veto:")
    assert i_release < i_veto


def test_release_clears_the_latch():
    src = _src()
    block = src[src.index("base_rate_veto_emerging_trend_exempt_enabled"):]
    assert "_br_veto_latch.pop" in block[:1200]


def test_cascade_opposed_block_uses_counter_trend_shadow_gate():
    src = _src()
    i = src.index("emerging_trend_cascade_veto_enabled", src.index("_execute_cascade_momentum"))
    block = src[i:i + 1500]
    assert 'reason="emerging_trend"' in block
    assert "signal_rejected_counter_trend" in block


def test_elite_override_denied_when_opposed():
    src = _src()
    i = src.index("direction_loss_block_elite_override_denied")
    block = src[max(0, i - 900):i + 700]
    assert "_emerging_trend_verdict" in block
    assert '== "opposed"' in block
    assert 'source="standard"' in src[i:i + 900]


def test_publisher_loop_and_gather_registration():
    src = _src()
    assert "async def _emerging_trend_loop()" in src
    assert "_emerging_trend_loop()," in src
    assert 'set_ai_param(f"emerging_trend:{_sym}"' in src
    assert 'clear_ai_param(f"emerging_trend:{_sym}"' in src
    assert "emerging_trend:tick" in src


def test_size_boost_splice_and_sizing_chain_fields():
    src = _src()
    assert "emerging_trend_size_boost" in src
    assert "emerging_trend=_emergent_state" in src
    assert "emerging_mult=_emergent_mult" in src


# ── cascade calendar block (watchdog cycle-25 P0, 2026-09-04) ────────────────

def test_cascade_calendar_block_wired_in_both_fastpaths():
    src = _src()
    i_mom = src.index("async def _execute_cascade_momentum")
    i_aft = src.index("async def _execute_cascade_aftermath")
    for seg in (src[i_mom:i_aft], src[i_aft:]):
        assert "cascade_calendar_block_enabled" in seg
        assert 'calendar_engine.get_state(_cs)' in seg
        assert '"signal_rejected_calendar_block"' in seg
        assert '"BLOCK"' in seg


def test_cascade_calendar_block_shadow_registered():
    import intelligence.shadow_journal as sj
    assert sj.REJECTION_EVENTS["signal_rejected_calendar_block"] == "calendar"


def test_cascade_calendar_block_knob_default():
    from core.config import Settings
    assert Settings().cascade_calendar_block_enabled is True
