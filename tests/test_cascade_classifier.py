"""Pins for intelligence/cascade_classifier.py — C7 shadow classifier."""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.cascade_classifier import classify_aftermath  # noqa: E402


def _snap(phase="aftermath", velocity=-0.5, silence=120.0, xlag=False,
          xdir="none", funding=False):
    return SimpleNamespace(
        phase=SimpleNamespace(value=phase), velocity=velocity,
        silence_s=silence, cross_venue_lag=xlag, cross_venue_dir=xdir,
        funding_aligned=funding)


def test_exhaustion_verdict_fades_cascade():
    v = classify_aftermath(_snap(), cascade_direction="bearish")
    assert v["verdict"] == "exhaustion"
    assert v["trade_direction"] == "long"   # bearish cascade -> fade long
    assert v["scores"]["exhaustion"] >= 3


def test_continuation_verdict_rides_momentum():
    s = _snap(phase="expansion", velocity=0.6, silence=5.0,
              xlag=True, xdir="short")
    v = classify_aftermath(s, cascade_direction="bearish")
    assert v["verdict"] == "continuation"
    assert v["trade_direction"] == "short"  # bearish cascade continues down


def test_unclear_when_scores_close():
    s = _snap(phase="exhaustion", velocity=0.5, silence=10.0)
    v = classify_aftermath(s, cascade_direction="bullish")
    assert v["verdict"] == "unclear"
    assert v["trade_direction"] == "none"


def test_true_absorption_tips_to_exhaustion():
    s = _snap(phase="trigger", velocity=0.0, silence=10.0)
    was = {"direction": "long", "class": "true_absorption", "age_s": 60.0}
    v = classify_aftermath(s, was_evidence=was, cascade_direction="bearish")
    assert v["scores"]["exhaustion"] == 2


def test_stale_or_wrong_side_was_ignored():
    s = _snap(phase="trigger", velocity=0.0, silence=10.0)
    stale = {"direction": "long", "class": "true_absorption", "age_s": 3600.0}
    wrong = {"direction": "short", "class": "true_absorption", "age_s": 60.0}
    for was in (stale, wrong):
        v = classify_aftermath(s, was_evidence=was, cascade_direction="bearish")
        assert v["scores"]["exhaustion"] == 0
        assert v["features"]["was_class"] == "true_absorption"


def test_bullish_cascade_fades_short():
    v = classify_aftermath(_snap(), cascade_direction="bullish")
    assert v["fade_direction"] == "short"
    assert v["continuation_direction"] == "long"
