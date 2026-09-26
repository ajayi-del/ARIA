"""tests/test_narrative_compass.py — doctrine pins for the narrative compass.

Every branch of the LOCKED composition doctrine is pinned:
  - trend-day outranks all (aligned_long/aligned_short -> weight 1.0)
  - rotation leader-aligned band 0.8-1.0, knife classes SOFT-cut to 0.3
    (never zero — Law 1: alignment is a multiplier, never a hard gate)
  - tide opposed x0.5 / aligned +0.1 (scales, never flips)
  - Free Option axiom: no narrative -> both sides at 0.5
  - kill switch False -> None baseline + permutation unchanged
  - permutation determinism (same cycle_ts reproduces; fresh minute
    re-deals; weight-proportional lead share over many seeds)
"""
from types import SimpleNamespace

from intelligence.narrative_compass import (
    compass_verdict,
    weighted_slot_permutation,
)


class _Cfg:
    narrative_compass_enabled = True


class _CfgOff:
    narrative_compass_enabled = False


CFG = _Cfg()
OFF = _CfgOff()


def _matrix(confidence=0.8, leading="large_cap", lagging="alt_l1"):
    return SimpleNamespace(confidence=confidence,
                           leading_category=leading,
                           lagging_category=lagging)


# ── kill switch ───────────────────────────────────────────────────────────────


def test_disabled_returns_none_baseline():
    assert compass_verdict(OFF, symbol="BTC-USD",
                           trend_day_verdict="aligned_long") is None


def test_disabled_permutation_input_unchanged():
    cands = [{"symbol": "BTC-USD", "side": "long", "weight": 0.9},
             {"symbol": "ETH-USD", "side": "short", "weight": 0.1}]
    out = weighted_slot_permutation(OFF, candidates=cands, cycle_ts=60000)
    assert [c["symbol"] for c in out] == ["BTC-USD", "ETH-USD"]


def test_bad_symbol_returns_none():
    assert compass_verdict(CFG, symbol="") is None
    assert compass_verdict(CFG, symbol=None) is None


# ── plane 1: locked trend day ─────────────────────────────────────────────────


def test_trend_day_aligned_long_outranks_all():
    v = compass_verdict(CFG, symbol="SOL-USD",
                        trend_day_verdict="aligned_long",
                        rotation_matrix=_matrix(),
                        emerging_trend="short",   # conflict — trend day wins
                        tide_aligned="opposed")
    assert v["side_bias"] == "long"
    assert v["weight"] == 1.0
    assert v["reason"] == "trend_day_aligned_long"


def test_trend_day_aligned_short_outranks_all():
    v = compass_verdict(CFG, symbol="BTC-USD",
                        trend_day_verdict="aligned_short")
    assert v["side_bias"] == "short"
    assert v["weight"] == 1.0
    assert v["reason"] == "trend_day_aligned_short"


# ── plane 2: rotation ─────────────────────────────────────────────────────────


def test_rotation_leader_long_lean_alone():
    # BTC-USD in leading large_cap, no other plane -> long lean at 0.8
    v = compass_verdict(CFG, symbol="BTC-USD", rotation_matrix=_matrix())
    assert v["side_bias"] == "long"
    assert v["weight"] == 0.8
    assert v["reason"] == "rotation_leader_aligned"


def test_rotation_lagging_cat_prefers_short_alone():
    # SOL-USD in lagging alt_l1, no other plane -> short lean at 0.8
    v = compass_verdict(CFG, symbol="SOL-USD", rotation_matrix=_matrix())
    assert v["side_bias"] == "short"
    assert v["weight"] == 0.8
    assert v["reason"] == "rotation_leader_aligned"


def test_rotation_agree_boosts_emerging_lean():
    # emerging long + BTC in leading cat -> 0.8 + 0.1 = 0.9
    v = compass_verdict(CFG, symbol="BTC-USD", rotation_matrix=_matrix(),
                        emerging_trend="long")
    assert v["side_bias"] == "long"
    assert abs(v["weight"] - 0.9) < 1e-9
    assert "rotation_agree" in v["reason"]


def test_lagging_knife_soft_cut_never_zero():
    # emerging LONG into the LAGGING category = the knife class:
    # soft cut to 0.3, side stays the lean (Law 1 — never gated).
    v = compass_verdict(CFG, symbol="SOL-USD", rotation_matrix=_matrix(),
                        emerging_trend="long")
    assert v["side_bias"] == "long"
    assert v["weight"] == 0.3
    assert v["reason"].startswith("lagging_knife_soft")


def test_leading_strength_soft_cut_mirror():
    # emerging SHORT into the LEADING category = fading strength: same
    # soft cut, mirror reason.
    v = compass_verdict(CFG, symbol="BTC-USD", rotation_matrix=_matrix(),
                        emerging_trend="short")
    assert v["side_bias"] == "short"
    assert v["weight"] == 0.3
    assert v["reason"].startswith("leading_strength_soft")


def test_low_confidence_matrix_abstains():
    v = compass_verdict(CFG, symbol="BTC-USD",
                        rotation_matrix=_matrix(confidence=0.4))
    assert v["side_bias"] == "both"
    assert v["reason"] == "no_narrative_free_option"


def test_uncategorized_symbol_rotation_abstains():
    v = compass_verdict(CFG, symbol="NOPE-USD", rotation_matrix=_matrix())
    assert v["reason"] == "no_narrative_free_option"


# ── tide (scales, never flips) ────────────────────────────────────────────────


def test_tide_opposed_halves_weight():
    v = compass_verdict(CFG, symbol="BTC-USD", emerging_trend="long",
                        tide_aligned="opposed")
    assert v["side_bias"] == "long"
    assert abs(v["weight"] - 0.4) < 1e-9
    assert "tide_opposed" in v["reason"]


def test_tide_opposed_applies_to_soft_cut_too():
    v = compass_verdict(CFG, symbol="SOL-USD", rotation_matrix=_matrix(),
                        emerging_trend="long", tide_aligned="opposed")
    assert v["weight"] == 0.15          # 0.3 x 0.5 — still not zero
    assert "lagging_knife_soft" in v["reason"]
    assert "tide_opposed" in v["reason"]


def test_tide_aligned_boosts_capped():
    v = compass_verdict(CFG, symbol="BTC-USD", rotation_matrix=_matrix(),
                        emerging_trend="long", tide_aligned="aligned")
    assert v["weight"] == 1.0           # 0.8 + 0.1 + 0.1 capped
    assert "tide_aligned" in v["reason"]


# ── Free Option axiom ─────────────────────────────────────────────────────────


def test_no_narrative_free_option():
    v = compass_verdict(CFG, symbol="ETH-USD")
    assert v["side_bias"] == "both"
    assert v["weight"] == 0.5
    assert v["reason"] == "no_narrative_free_option"


def test_neutral_strings_are_abstain():
    v = compass_verdict(CFG, symbol="ETH-USD", emerging_trend="neutral",
                        tide_aligned="neutral", trend_day_verdict="counter")
    assert v["side_bias"] == "both"


# ── no-hard-gate invariant ────────────────────────────────────────────────────


def test_weight_never_zero_for_eligible_symbol():
    grids = [
        dict(trend_day_verdict=t, rotation_matrix=m, emerging_trend=e,
             tide_aligned=d, ssi_state=s)
        for t in (None, "aligned_long", "aligned_short", "counter")
        for m in (None, _matrix(), _matrix(confidence=0.1))
        for e in (None, "long", "short", "neutral")
        for d in (None, "opposed", "aligned", "neutral")
        for s in (None, "long", "short")
    ]
    for g in grids:
        for sym in ("BTC-USD", "SOL-USD", "LINK-USD"):
            v = compass_verdict(CFG, symbol=sym, **g)
            assert v is not None
            assert 0.0 < v["weight"] <= 1.0, (sym, g, v)
            assert v["side_bias"] in ("long", "short", "both")


# ── permutation ───────────────────────────────────────────────────────────────


def _cands(n=6):
    return [{"symbol": f"S{i}-USD", "side": "long",
             "weight": (n - i) / n} for i in range(n)]


def test_permutation_deterministic_same_cycle():
    a = weighted_slot_permutation(CFG, candidates=_cands(), cycle_ts=123456)
    b = weighted_slot_permutation(CFG, candidates=_cands(), cycle_ts=123456)
    assert [c["symbol"] for c in a] == [c["symbol"] for c in b]


def test_permutation_same_minute_same_seed():
    # cycle_ts // 60 is the seed — two timestamps inside one minute match.
    a = weighted_slot_permutation(CFG, candidates=_cands(), cycle_ts=123420)
    b = weighted_slot_permutation(CFG, candidates=_cands(), cycle_ts=123479)
    assert [c["symbol"] for c in a] == [c["symbol"] for c in b]


def test_permutation_fresh_minute_redeals():
    orders = set()
    for ts in range(0, 60 * 60, 60):
        out = weighted_slot_permutation(CFG, candidates=_cands(), cycle_ts=ts)
        orders.add(tuple(c["symbol"] for c in out))
    assert len(orders) > 1            # spontaneity: not a static ordering


def test_permutation_weighted_lead_bias_over_seeds():
    # The heaviest candidate must lead the deal more often than the
    # lightest across many fresh cycles (size-biased permutation).
    cands = [{"symbol": "HEAVY", "side": "long", "weight": 1.0},
             {"symbol": "LIGHT", "side": "long", "weight": 0.1}]
    heavy_leads = 0
    trials = 400
    for i in range(trials):
        out = weighted_slot_permutation(CFG, candidates=cands,
                                        cycle_ts=i * 60)
        if out[0]["symbol"] == "HEAVY":
            heavy_leads += 1
    # Efraimidis-Spirakis: P(heavy first) = 1.0/1.1 ~= 0.909; allow slack.
    assert heavy_leads / trials > 0.75


def test_permutation_is_without_replacement_and_complete():
    cands = _cands(8)
    out = weighted_slot_permutation(CFG, candidates=cands, cycle_ts=999)
    assert sorted(c["symbol"] for c in out) == sorted(
        c["symbol"] for c in cands)
    assert out is not cands           # new list, input not mutated
    assert [c["symbol"] for c in cands] == [f"S{i}-USD" for i in range(8)]


def test_permutation_degenerate_inputs_unchanged():
    assert weighted_slot_permutation(CFG, candidates=[], cycle_ts=1) == []
    zeros = [{"symbol": "A", "side": "long", "weight": 0.0},
             {"symbol": "B", "side": "long", "weight": None}]
    out = weighted_slot_permutation(CFG, candidates=zeros, cycle_ts=1)
    assert [c["symbol"] for c in out] == ["A", "B"]


def test_permutation_nonpositive_weights_tail_in_input_order():
    cands = [{"symbol": "A", "side": "long", "weight": 0.0},
             {"symbol": "B", "side": "long", "weight": 1.0},
             {"symbol": "C", "side": "long", "weight": -2.0}]
    out = weighted_slot_permutation(CFG, candidates=cands, cycle_ts=42)
    assert out[0]["symbol"] == "B"
    assert [c["symbol"] for c in out[1:]] == ["A", "C"]
