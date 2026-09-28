"""SignalSynthesizer: three-type doctrine, alt-purity law, bounded clamp,
dark-plane abstain — doctrine pins. Zero I/O, no main.py import."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.signal_synthesizer import (  # noqa: E402
    SignalSynthesizer, ETF_BACKED, ETF_AMPLIFIED_INDIRECT,
)

SYN = SignalSynthesizer()
M = 1_000_000.0


def _cfg(**kw):
    base = dict(signal_synthesizer_enabled=True,
                synth_compression_enabled=True,
                synth_compression_threshold_pct=10.0,
                synth_compression_bonus_mult=1.25,
                synth_composite_floor=0.55,
                synth_composite_cap=1.40)
    base.update(kw)
    return SimpleNamespace(**base)


# ── 1. Classification ─────────────────────────────────────────────────────

def test_class_etf_backed():
    assert SYN.signal_class("BTC-USD") == "etf_backed"
    assert SYN.signal_class("ETH") == "etf_backed"
    assert SYN.signal_class("NVDA-USD") == "etf_backed"


def test_class_etf_amplified_indirect():
    assert SYN.signal_class("AMD") == "etf_amplified_indirect"
    assert SYN.signal_class("MSTR-USD") == "etf_amplified_indirect"
    assert SYN.signal_class("COIN") == "etf_amplified_indirect"
    assert SYN.signal_class("HOOD") == "etf_amplified_indirect"


def test_class_pure_whale_alt():
    assert SYN.signal_class("SUI-USD") == "pure_whale"
    assert SYN.signal_class("SOL") == "pure_whale"


def test_class_normalization():
    assert SYN.signal_class("btc") == "etf_backed"
    assert SYN.signal_class(" btc-USD ") == "etf_backed"
    assert SYN.signal_class("amd-usd") == "etf_amplified_indirect"


def test_class_unknown_none():
    assert SYN.signal_class("") is None
    assert SYN.signal_class(None) is None
    assert SYN.signal_class("-USD") is None


def test_tables_match_doctrine():
    assert ETF_BACKED == frozenset({"BTC", "ETH", "NVDA"})
    assert ETF_AMPLIFIED_INDIRECT == frozenset({"AMD", "MSTR", "COIN", "HOOD"})


# ── 2. ETF ladder ─────────────────────────────────────────────────────────

def test_etf_ladder_strong_above_500m():
    assert SYN.etf_modifier(_cfg(), symbol="BTC",
                            etf_daily_flow_usd=500.01 * M) == 1.40


def test_etf_ladder_500m_exactly_is_medium_rung():
    # strictly-greater: exactly $500M falls to the +0.20 rung (pinned)
    assert SYN.etf_modifier(_cfg(), symbol="BTC",
                            etf_daily_flow_usd=500.0 * M) == 1.20


def test_etf_ladder_medium_band():
    assert SYN.etf_modifier(_cfg(), symbol="ETH",
                            etf_daily_flow_usd=100.01 * M) == 1.20
    assert SYN.etf_modifier(_cfg(), symbol="ETH",
                            etf_daily_flow_usd=433.0 * M) == 1.20


def test_etf_ladder_zero_band():
    assert SYN.etf_modifier(_cfg(), symbol="BTC",
                            etf_daily_flow_usd=50.0 * M) == 1.00


def test_etf_ladder_measured_zero_is_zero_rung_not_dark():
    # measured 0.0 ≠ None: the 0-100M rung, reached via +0.0
    assert SYN.etf_modifier(_cfg(), symbol="BTC",
                            etf_daily_flow_usd=0.0) == 1.00


def test_etf_ladder_negative_outflow():
    assert SYN.etf_modifier(_cfg(), symbol="BTC",
                            etf_daily_flow_usd=-1.0) == 0.65
    assert SYN.etf_modifier(_cfg(), symbol="NVDA",
                            etf_daily_flow_usd=-900 * M) == 0.65


def test_etf_none_is_dark_abstain():
    assert SYN.etf_modifier(_cfg(), symbol="BTC",
                            etf_daily_flow_usd=None) == 1.00


def test_etf_non_etf_backed_identity():
    assert SYN.etf_modifier(_cfg(), symbol="AMD",
                            etf_daily_flow_usd=999 * M) == 1.00
    assert SYN.etf_modifier(_cfg(), symbol="SUI",
                            etf_daily_flow_usd=999 * M) == 1.00


def test_etf_degenerate_flow_abstains():
    assert SYN.etf_modifier(_cfg(), symbol="BTC",
                            etf_daily_flow_usd="junk") == 1.00
    assert SYN.etf_modifier(_cfg(), symbol="BTC",
                            etf_daily_flow_usd=float("nan")) == 1.00


# ── 3. Compression bonus ──────────────────────────────────────────────────

def test_compression_13_4pct_below_ath_fires():
    # ath 100, price 86.6 → 13.4% below → 1.25
    assert SYN.compression_bonus(_cfg(), symbol="BTC",
                                 current_price=86.6, ath_reference=100.0) == 1.25


def test_compression_9pct_below_ath_identity():
    assert SYN.compression_bonus(_cfg(), symbol="BTC",
                                 current_price=91.0, ath_reference=100.0) == 1.00


def test_compression_reclaim_above_line_reverses():
    assert SYN.compression_bonus(_cfg(), symbol="BTC",
                                 current_price=101.0, ath_reference=100.0) == 1.00


def test_compression_dark_references_abstain():
    assert SYN.compression_bonus(_cfg(), symbol="BTC",
                                 current_price=50.0, ath_reference=None) == 1.00
    assert SYN.compression_bonus(_cfg(), symbol="BTC",
                                 current_price=50.0, ath_reference=0.0) == 1.00
    assert SYN.compression_bonus(_cfg(), symbol="BTC",
                                 current_price=0.0, ath_reference=100.0) == 1.00
    assert SYN.compression_bonus(_cfg(), symbol="BTC",
                                 current_price=-5.0, ath_reference=100.0) == 1.00


def test_compression_disabled_knob_identity():
    cfg = _cfg(synth_compression_enabled=False)
    assert SYN.compression_bonus(cfg, symbol="BTC",
                                 current_price=50.0, ath_reference=100.0) == 1.00


# ── 4. NAV premium beta ───────────────────────────────────────────────────

def test_nav_beta_ladder():
    assert SYN.nav_premium_beta(_cfg(), symbol="MSTR", nav_premium=1.6) == 2.8
    assert SYN.nav_premium_beta(_cfg(), symbol="MSTR", nav_premium=1.5) == 1.8
    assert SYN.nav_premium_beta(_cfg(), symbol="MSTR", nav_premium=1.2) == 1.8
    assert SYN.nav_premium_beta(_cfg(), symbol="MSTR", nav_premium=1.0) == 1.3
    assert SYN.nav_premium_beta(_cfg(), symbol="MSTR", nav_premium=0.7) == 1.3
    assert SYN.nav_premium_beta(_cfg(), symbol="MSTR", nav_premium=0.69) == 1.1


def test_nav_beta_none_is_dark_abstain():
    assert SYN.nav_premium_beta(_cfg(), symbol="MSTR", nav_premium=None) is None


def test_nav_beta_non_mstr_none():
    assert SYN.nav_premium_beta(_cfg(), symbol="AMD", nav_premium=1.6) is None
    assert SYN.nav_premium_beta(_cfg(), symbol="BTC", nav_premium=1.6) is None


# ── 5. Composite clamp ────────────────────────────────────────────────────

def test_composite_cap_binds_compounding_hazard():
    # 1.15 x 1.25 x 1.30 x 1.15 = 2.28x unbounded → clamped to 1.40
    legs = {"a": 1.15, "b": 1.25, "c": 1.30, "d": 1.15}
    assert SYN.composite_modifier(_cfg(), symbol="BTC", legs=legs) == 1.40


def test_composite_floor_binds():
    legs = {"a": 0.55, "b": 0.55}   # 0.3025 → floor 0.55
    assert SYN.composite_modifier(_cfg(), symbol="BTC", legs=legs) == 0.55


def test_composite_none_legs_dropped():
    legs = {"etf": 1.20, "soxl": None, "nav": None}
    assert SYN.composite_modifier(_cfg(), symbol="BTC", legs=legs) == 1.20


def test_composite_empty_identity():
    assert SYN.composite_modifier(_cfg(), symbol="BTC", legs={}) == 1.00
    assert SYN.composite_modifier(_cfg(), symbol="BTC", legs=None) == 1.00
    assert SYN.composite_modifier(_cfg(), symbol="BTC",
                                  legs={"a": None}) == 1.00


def test_composite_in_band_product_unclamped():
    legs = {"etf": 1.20, "compression": 1.10}
    assert abs(SYN.composite_modifier(_cfg(), symbol="ETH", legs=legs)
               - 1.32) < 1e-9


def test_composite_knob_override():
    cfg = _cfg(synth_composite_cap=1.10)
    legs = {"a": 1.15, "b": 1.25}
    assert SYN.composite_modifier(cfg, symbol="BTC", legs=legs) == 1.10


# ── 6. ALT PURITY LAW (pinned hard) ───────────────────────────────────────

def test_alt_purity_composite_ignores_legs():
    legs = {"etf": 9.99, "compression": 9.99, "soxl": 9.99}
    assert SYN.composite_modifier(_cfg(), symbol="SUI", legs=legs) == 1.00
    assert SYN.composite_modifier(_cfg(), symbol="ARB", legs=legs) == 1.00


def test_alt_purity_effective_whale_verbatim():
    assert SYN.effective_whale_signal(
        _cfg(), symbol="SUI", whale_ratio=2.7,
        etf_daily_flow_usd=999 * M,
        compression_legs={"etf": 9.99}) == 2.7


def test_alt_purity_never_diluted():
    assert SYN.effective_whale_signal(
        _cfg(), symbol="FET", whale_ratio=1.5,
        etf_daily_flow_usd=-500 * M) == 1.5


# ── 7. effective_whale_signal ─────────────────────────────────────────────

def test_effective_whale_doctrine_worked_example():
    # BTC whale 1.5 + flow $433M (+0.20) → 1.5 x 1.20 = 1.80 (pinned)
    out = SYN.effective_whale_signal(_cfg(), symbol="BTC", whale_ratio=1.5,
                                     etf_daily_flow_usd=433 * M)
    assert abs(out - 1.80) < 1e-9


def test_effective_whale_etf_backed_dark_flow_abstains():
    assert SYN.effective_whale_signal(_cfg(), symbol="ETH", whale_ratio=2.0,
                                      etf_daily_flow_usd=None) == 2.0


def test_effective_whale_degenerate_ratio_none():
    assert SYN.effective_whale_signal(_cfg(), symbol="BTC",
                                      whale_ratio=0.0) is None
    assert SYN.effective_whale_signal(_cfg(), symbol="BTC",
                                      whale_ratio=-1.0) is None
    assert SYN.effective_whale_signal(_cfg(), symbol="BTC",
                                      whale_ratio="junk") is None
    assert SYN.effective_whale_signal(_cfg(), symbol="BTC",
                                      whale_ratio=float("nan")) is None


def test_effective_whale_extra_legs_composed():
    out = SYN.effective_whale_signal(
        _cfg(), symbol="BTC", whale_ratio=2.0,
        etf_daily_flow_usd=None,
        compression_legs={"compression": 1.25})
    assert abs(out - 2.5) < 1e-9


def test_effective_whale_unknown_symbol_abstains_legs():
    out = SYN.effective_whale_signal(_cfg(), symbol="", whale_ratio=2.0,
                                     etf_daily_flow_usd=999 * M)
    assert out == 2.0


# ── 8. Propagation deadline ───────────────────────────────────────────────

def test_propagation_deadline_arithmetic():
    assert SYN.propagation_deadline(_cfg(), trigger_ts=1000.0,
                                    lag_hours=12.0) == 1000.0 + 12.0 * 3600.0
    assert SYN.propagation_deadline(_cfg(), trigger_ts=0.0,
                                    lag_hours=24.0) == 86400.0


def test_propagation_deadline_degenerate_none():
    assert SYN.propagation_deadline(_cfg(), trigger_ts="x",
                                    lag_hours=12.0) is None
    assert SYN.propagation_deadline(_cfg(), trigger_ts=1000.0,
                                    lag_hours=None) is None


# ── 9. Alt whale tier ─────────────────────────────────────────────────────

def test_alt_whale_tier_rungs():
    assert SYN.alt_whale_tier(_cfg(), symbol="SUI", whale_ratio=4.0) == "TIER_1"
    assert SYN.alt_whale_tier(_cfg(), symbol="SUI", whale_ratio=4.5) == "TIER_1"
    assert SYN.alt_whale_tier(_cfg(), symbol="SUI", whale_ratio=3.0) == "TIER_2"
    assert SYN.alt_whale_tier(_cfg(), symbol="SUI", whale_ratio=2.0) == "TIER_3"
    assert SYN.alt_whale_tier(_cfg(), symbol="SUI", whale_ratio=1.5) == "TIER_4"


def test_alt_whale_tier_boundaries():
    assert SYN.alt_whale_tier(_cfg(), symbol="SUI", whale_ratio=3.99) == "TIER_2"
    assert SYN.alt_whale_tier(_cfg(), symbol="SUI", whale_ratio=1.49) is None


def test_alt_whale_tier_non_alt_none():
    assert SYN.alt_whale_tier(_cfg(), symbol="BTC", whale_ratio=5.0) is None
    assert SYN.alt_whale_tier(_cfg(), symbol="AMD", whale_ratio=5.0) is None


# ── 10. Master gate ───────────────────────────────────────────────────────

def test_disabled_master_gate_identities():
    cfg = _cfg(signal_synthesizer_enabled=False)
    assert SYN.etf_modifier(cfg, symbol="BTC",
                            etf_daily_flow_usd=999 * M) == 1.00
    assert SYN.compression_bonus(cfg, symbol="BTC", current_price=50.0,
                                 ath_reference=100.0) == 1.00
    assert SYN.nav_premium_beta(cfg, symbol="MSTR", nav_premium=1.6) is None
    assert SYN.composite_modifier(cfg, symbol="BTC",
                                  legs={"a": 9.99}) == 1.00
    assert SYN.effective_whale_signal(cfg, symbol="BTC", whale_ratio=1.5,
                                      etf_daily_flow_usd=433 * M) == 1.5
    assert SYN.propagation_deadline(cfg, trigger_ts=1000.0,
                                    lag_hours=12.0) is None
    assert SYN.alt_whale_tier(cfg, symbol="SUI", whale_ratio=5.0) is None
    # classification is cfg-free — doctrine table stands under any gate
    assert SYN.signal_class("BTC") == "etf_backed"
