"""Behavioral pins for intelligence/mag7_pairs.py (2026-10-17, Governor
directive "STOCK EXECUTION ARCHITECTURE" piece 2). SHADOW-FIRST brain —
tests cover pair spread sign correctness, fired boundary, leg abstain,
complex leg with 7/5/3 members available, and textual pins for the env
kill switch, splice event name, and the MAG7_PAIRS universe."""
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = (_ROOT / "intelligence" / "mag7_pairs.py").read_text()

import sys
sys.path.insert(0, str(_ROOT))
from intelligence.mag7_pairs import (  # noqa: E402
    COMPLEX_MIN_MEMBERS, DIVERGENCE_FIRE_PCT, MAG7_COMPLEX_MEMBERS,
    MAG7_MEMBERS, MAG7_PAIR_EVENT, MAG7_PAIRS, complex_return,
    mag7_pairs_enabled, pair_divergence)


# ---- pair_divergence: sign correctness ----

def test_spread_positive_rich_leg_a():
    out = pair_divergence(0.05, 0.02, "5d")     # A outperformed by 3%
    assert out["spread_pct"] == 3.0
    assert out["rich_leg"] == "leg_a"
    assert out["window"] == "5d"


def test_spread_negative_rich_leg_b():
    out = pair_divergence(0.01, 0.04, "5d")     # B outperformed by 3%
    assert out["spread_pct"] == -3.0
    assert out["rich_leg"] == "leg_b"


def test_spread_tie_no_rich_leg():
    out = pair_divergence(0.02, 0.02, "5d")
    assert out["spread_pct"] == 0.0
    assert out["rich_leg"] == "none"
    assert out["fired"] is False


# ---- fired boundary ----

def test_fired_at_materiality_bar():
    out = pair_divergence(0.03, 0.01, "5d")     # |spread| = 2.0 exactly
    assert abs(out["spread_pct"]) == DIVERGENCE_FIRE_PCT
    assert out["fired"] is True


def test_not_fired_below_materiality_bar():
    out = pair_divergence(0.029, 0.01, "5d")    # |spread| = 1.9
    assert out["fired"] is False


def test_fired_negative_spread():
    out = pair_divergence(-0.015, 0.025, "5d")  # |spread| = 4.0
    assert out["fired"] is True
    assert out["rich_leg"] == "leg_b"


# ---- leg abstain ----

def test_missing_leg_abstains():
    out = pair_divergence(None, 0.02, "5d")
    assert out["spread_pct"] is None
    assert out["rich_leg"] == "none"
    assert out["fired"] is False
    out2 = pair_divergence(0.02, None, "5d")
    assert out2["spread_pct"] is None and out2["fired"] is False


def test_degenerate_leg_abstains():
    out = pair_divergence("n/a", 0.02, "5d")
    assert out["spread_pct"] is None and out["fired"] is False


# ---- complex_return: 7 / 5 / 3 members available ----

def test_complex_all_7_members():
    rets = {s: 0.01 for s in MAG7_MEMBERS}      # all seven at +1%
    assert complex_return(rets) == 0.01


def test_complex_5_members_drops_missing():
    rets = {"NVDA-USD": 0.02, "AMD-USD": 0.02, "MSFT-USD": 0.04,
            "AAPL-USD": 0.04, "GOOGL-USD": 0.0}
    # 5 of 7 available >= COMPLEX_MIN_MEMBERS -> mean of the five
    assert abs(complex_return(rets) - 0.024) < 1e-12


def test_complex_3_members_abstains():
    rets = {"NVDA-USD": 0.02, "AMD-USD": 0.02, "MSFT-USD": 0.04}
    assert len(rets) < COMPLEX_MIN_MEMBERS
    assert complex_return(rets) is None


def test_complex_none_values_dropped():
    rets = {s: 0.01 for s in MAG7_MEMBERS}
    rets["TSLA-USD"] = None
    rets["META-USD"] = None
    assert complex_return(rets) == 0.01          # 5 available, mean holds


def test_complex_empty_abstains():
    assert complex_return({}) is None
    assert complex_return(None) is None


def test_complex_non_numeric_dropped():
    rets = {s: 0.01 for s in MAG7_MEMBERS}
    rets["TSLA-USD"] = "bad"
    rets["META-USD"] = "bad"
    rets["GOOGL-USD"] = "bad"
    assert complex_return(rets) == 0.01          # 4 numeric = minimum, mean holds
    rets["AAPL-USD"] = "bad"
    assert complex_return(rets) is None          # 3 numeric -> abstains


# ---- textual pins: env kill switch, event name, universe ----

def test_kill_switch_env_pinned():
    assert "MAG7_PAIRS_ENABLED" in _SRC
    assert '"true"' in _SRC


def test_kill_switch_helper_behavior(monkeypatch):
    monkeypatch.delenv("MAG7_PAIRS_ENABLED", raising=False)
    assert mag7_pairs_enabled() is True
    monkeypatch.setenv("MAG7_PAIRS_ENABLED", "false")
    assert mag7_pairs_enabled() is False


def test_event_name_pinned():
    assert MAG7_PAIR_EVENT == "mag7_pair_divergence"
    assert '"mag7_pair_divergence"' in _SRC


def test_universe_declares_all_four_pairs():
    names = [p["name"] for p in MAG7_PAIRS]
    assert names == ["NVDA/AMD", "MSFT/AAPL", "GOOGL/META", "TSLA/MAG7"]
    legs = {(p["leg_a"], p["leg_b"]) for p in MAG7_PAIRS}
    assert ("NVDA-USD", "AMD-USD") in legs
    assert ("MSFT-USD", "AAPL-USD") in legs
    assert ("GOOGL-USD", "META-USD") in legs
    assert ("TSLA-USD", "MAG7_COMPLEX") in legs
    kinds = {p["name"]: p["kind"] for p in MAG7_PAIRS}
    assert kinds["TSLA/MAG7"] == "complex"
    assert kinds["NVDA/AMD"] == "pair"


def test_complex_members_constant_covers_mag7():
    assert MAG7_COMPLEX_MEMBERS == MAG7_MEMBERS
    assert len(MAG7_MEMBERS) == 7
    for s in ("NVDA-USD", "AMD-USD", "MSFT-USD", "AAPL-USD",
              "GOOGL-USD", "META-USD", "TSLA-USD"):
        assert s in MAG7_MEMBERS
