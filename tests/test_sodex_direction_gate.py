"""SoDEX direction-gate pure-brain pins (Governor 2026-09-18, Phase 4).

The gate blocks counter-trend entries only when at least (min_agree - 1)
confirming extremes (funding carry / whale L-S positioning) agree — one
stale plane never blocks. SHADOW-first: the live flag defaults OFF; these
pins lock the pure decision logic the shadow evidence will be graded
against.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.sodex_direction_gate import evaluate  # noqa: E402


def test_short_into_up_trend_with_extreme_funding_blocks():
    d = evaluate("ETH-USD", "short", trend_4h="up", funding_bp=10.0,
                 whale_ls_ratio=None)
    assert d.block is True
    assert "counter_trend" in d.reasons and "funding_extreme" in d.reasons
    assert d.detail["leg_whale_extreme"] is None
    assert d.detail["planes_present"] == "trend,funding"


def test_short_into_up_trend_confluence_via_whale_blocks():
    d = evaluate("OP-USD", "short", trend_4h="up", funding_bp=1.0,
                 whale_ls_ratio=2.5)
    assert d.block is True
    assert "counter_trend" in d.reasons and "whale_extreme" in d.reasons
    assert d.detail["leg_funding_extreme"] is False


def test_counter_trend_alone_does_not_block():
    d = evaluate("ETH-USD", "short", trend_4h="up", funding_bp=1.0,
                 whale_ls_ratio=1.2)
    assert d.block is False
    assert d.reasons == ("counter_trend",)


def test_long_into_down_trend_mirror_blocks():
    d = evaluate("ETH-USD", "long", trend_4h="down", funding_bp=-10.0,
                 whale_ls_ratio=None)
    assert d.block is True
    assert set(d.reasons) == {"counter_trend", "funding_extreme"}


def test_trend_aligned_with_extremes_does_not_block():
    d = evaluate("ETH-USD", "long", trend_4h="up", funding_bp=10.0,
                 whale_ls_ratio=3.0)
    assert d.block is False
    assert d.detail["leg_counter_trend"] is False


def test_unknown_trend_never_blocks():
    d = evaluate("ETH-USD", "short", trend_4h=None, funding_bp=50.0,
                 whale_ls_ratio=10.0)
    assert d.block is False
    assert d.detail["leg_counter_trend"] is None
    assert d.detail["trend_4h"] is None


def test_counter_trend_without_confirming_leg_does_not_block():
    d = evaluate("ETH-USD", "short", trend_4h="up", funding_bp=None,
                 whale_ls_ratio=None)
    assert d.block is False
    assert d.reasons == ("counter_trend",)
    assert d.detail["leg_funding_extreme"] is None
    assert d.detail["leg_whale_extreme"] is None
    assert d.detail["planes_present"] == "trend"


def test_min_agree_3_requires_both_extremes():
    one = evaluate("ETH-USD", "short", trend_4h="up", funding_bp=10.0,
                   whale_ls_ratio=1.0, min_agree=3)
    assert one.block is False
    both = evaluate("ETH-USD", "short", trend_4h="up", funding_bp=10.0,
                    whale_ls_ratio=3.0, min_agree=3)
    assert both.block is True


def test_side_normalization():
    for s in ("SHORT", "Sell", " short "):
        d = evaluate("ETH-USD", s, trend_4h="up", funding_bp=10.0,
                     whale_ls_ratio=None)
        assert d.block is True, s
    for s in ("LONG", "Buy"):
        d = evaluate("ETH-USD", s, trend_4h="down", funding_bp=-10.0,
                     whale_ls_ratio=None)
        assert d.block is True, s
    bogus = evaluate("ETH-USD", "flat", trend_4h="up", funding_bp=10.0,
                     whale_ls_ratio=None)
    assert bogus.block is False
    assert bogus.detail["leg_counter_trend"] is None


def test_whale_mirror_boundary():
    short_at_min = evaluate("OP-USD", "short", trend_4h="up", funding_bp=0.0,
                            whale_ls_ratio=2.0)
    assert short_at_min.block is True
    short_below = evaluate("OP-USD", "short", trend_4h="up", funding_bp=0.0,
                           whale_ls_ratio=1.99)
    assert short_below.block is False
    long_at_inverse = evaluate("OP-USD", "long", trend_4h="down",
                               funding_bp=0.0, whale_ls_ratio=0.5)
    assert long_at_inverse.block is True
    long_above = evaluate("OP-USD", "long", trend_4h="down", funding_bp=0.0,
                          whale_ls_ratio=0.51)
    assert long_above.block is False


def test_funding_threshold_boundary_and_carry_both_ways():
    at = evaluate("ETH-USD", "short", trend_4h="up", funding_bp=8.0,
                  whale_ls_ratio=None)
    assert at.block is True
    below = evaluate("ETH-USD", "short", trend_4h="up", funding_bp=7.99,
                     whale_ls_ratio=None)
    assert below.block is False
    # long into extreme negative funding = fighting the carry the other way
    lg = evaluate("ETH-USD", "long", trend_4h="down", funding_bp=-8.0,
                  whale_ls_ratio=None)
    assert lg.block is True
